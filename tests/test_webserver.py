import json
import unittest
from unittest.mock import Mock

try:
    from fastapi.testclient import TestClient
    from files2gdrive.webserver import create_app, COOKIE, MAX_BODY
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, 'Web interface dependencies are not installed')
class WebServerTests(unittest.TestCase):
    def setUp(self):
        self.controller = Mock()
        self.controller.snapshot.return_value = {'paused': True, 'roots': []}
        self.controller.call.return_value = {'ok': True}
        self.app = create_app(self.controller, 8765)
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 45678))
        self.origin = 'http://127.0.0.1:8765'

    def tearDown(self):
        self.client.close()

    def authenticate(self):
        response = self.client.get('/launch', params={'token': self.app.state.launch_token}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        session = self.client.get('/api/session')
        self.csrf = session.json()['csrf_token']
        return response

    def headers(self):
        return {'Origin': self.origin, 'X-CSRF-Token': self.csrf}

    def test_no_status_or_session_without_bootstrap_cookie(self):
        for path in ('/', '/api/status', '/api/session', '/app.js'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers['cache-control'], 'no-store')
        self.controller.snapshot.assert_not_called()

    def test_launch_is_single_use_and_cookie_is_private(self):
        token = self.app.state.launch_token
        response = self.authenticate()
        cookie = response.headers['set-cookie'].lower()
        self.assertIn('httponly', cookie)
        self.assertIn('samesite=strict', cookie)
        self.assertNotIn('domain=', cookie)
        self.assertNotIn(token, response.headers.get('location', ''))
        self.assertEqual(self.client.get('/launch', params={'token': token}).status_code, 401)
        self.assertEqual(self.client.get('/api/status').status_code, 200)
        self.assertEqual(self.client.get('/api/status').json()['paused'], True)

    def test_host_origin_and_remote_client_are_rejected(self):
        self.authenticate()
        for headers in ({'Host': 'localhost:8765'}, {'Host': 'evil.example'},
                        {'Origin': 'https://evil.example'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.client.get('/api/status', headers=headers).status_code, 403)
        remote = TestClient(self.app, base_url=self.origin, client=('192.0.2.1', 45678))
        try:
            self.assertEqual(remote.get('/launch', params={'token': self.app.state.issue_launch_token()}).status_code, 403)
        finally:
            remote.close()
        self.controller.snapshot.assert_not_called()

    def test_mutations_require_both_exact_origin_and_csrf(self):
        self.authenticate()
        for headers in ({}, {'Origin': self.origin}, {'X-CSRF-Token': self.csrf},
                        {'Origin': self.origin, 'X-CSRF-Token': 'wrong'}):
            response = self.client.post('/api/control', json={'action': 'resume'}, headers=headers)
            self.assertEqual(response.status_code, 403)
        self.controller.call.assert_not_called()
        response = self.client.post('/api/control', json={'action': 'pause'}, headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.controller.call.assert_called_once_with('control', {'action': 'pause'})

    def test_route_dispatch_and_delete(self):
        self.authenticate()
        routes = {'/api/roots': 'add_root', '/api/filters/preview': 'preview_filters',
                  '/api/filters': 'set_filters', '/api/settings': 'settings',
                  '/api/auth/login': 'login', '/api/browse': 'browse',
                  '/api/recovery/catalog': 'catalog', '/api/recovery/files': 'recovery_files',
                  '/api/recovery/restore': 'restore',
                  '/api/folder-selection/browse': 'folder_selection_browse',
                  '/api/folder-selection/preview': 'folder_selection_preview',
                  '/api/folder-selection/apply': 'folder_selection_apply'}
        for route, action in routes.items():
            self.controller.call.reset_mock()
            response = self.client.post(route, json={'test': 'value'}, headers=self.headers())
            self.assertEqual(response.status_code, 200)
            self.controller.call.assert_called_once_with(action, {'test': 'value'})
        self.controller.call.reset_mock()
        response = self.client.delete('/api/roots/root-id', headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.controller.call.assert_called_once_with('remove_root', {'root_id': 'root-id'})

    def test_json_content_type_size_and_shape_limits(self):
        self.authenticate()
        for content, media, expected in [('x', 'text/plain', 415), ('[]', 'application/json', 422),
                                         ('{invalid', 'application/json', 422),
                                         ('x' * (MAX_BODY + 1), 'application/json', 413)]:
            headers = dict(self.headers(), **{'Content-Type': media})
            response = self.client.post('/api/control', content=content, headers=headers)
            self.assertEqual(response.status_code, expected)
        self.controller.call.assert_not_called()
        chunks = iter([b'{"large":"', b'x' * MAX_BODY, b'"}'])
        headers = dict(self.headers(), **{'Content-Type': 'application/json'})
        response = self.client.post('/api/control', content=chunks, headers=headers)
        self.assertEqual(response.status_code, 413)

    def test_logs_export_is_jsonl_and_filtered(self):
        self.authenticate()
        self.controller.call.return_value = {'entries': [{'message': '<unsafe>', 'level': 'WARNING'}]}
        response = self.client.get('/api/logs/export?level=WARNING&root_id=root&since=2026-11-01')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.text)['message'], '<unsafe>')
        self.assertIn('attachment', response.headers['content-disposition'])
        self.controller.call.assert_called_once_with('logs', dict(level='WARNING', root_id='root', since='2026-11-01', export=True))

    def test_safe_error_responses_and_security_headers(self):
        self.authenticate()
        for error, status in ((ValueError('Choose a valid folder'), 400),
                              (RuntimeError('SECRET TOKEN'), 409), (TimeoutError('SECRET'), 503),
                              (KeyError('SECRET'), 500)):
            self.controller.call.side_effect = error
            response = self.client.post('/api/control', json={}, headers=self.headers())
            self.assertEqual(response.status_code, status)
            self.assertNotIn('SECRET', response.text)
            self.assertEqual(response.headers['referrer-policy'], 'no-referrer')
            self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])
            self.assertEqual(response.headers['x-content-type-options'], 'nosniff')

    def test_no_openapi_or_arbitrary_static_paths(self):
        self.authenticate()
        for path in ('/docs', '/redoc', '/openapi.json', '/database.py', '/web/../auth.py'):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_only_fixed_assets_are_served_to_authenticated_session(self):
        self.authenticate()
        for path, content in (('/', 'text/html'), ('/static/app.js', 'text/javascript'),
                              ('/static/app.css', 'text/css')):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(content, response.headers['content-type'])
            self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertIn('Files2GDrive', self.client.get('/').text)

    def test_cookie_tampering_and_duplicate_authority_are_rejected(self):
        self.authenticate()
        response = self.client.get('/api/status', headers={'Cookie': COOKIE + '=forged'})
        self.assertEqual(response.status_code, 401)
        response = self.client.get('/api/status', headers=[('Host', '127.0.0.1:8765'), ('Host', 'evil.example')])
        self.assertEqual(response.status_code, 403)
        headers = list(self.headers().items()) + [('Origin', 'http://evil.example')]
        response = self.client.post('/api/control', json={}, headers=headers)
        self.assertEqual(response.status_code, 403)

    def test_issued_token_opens_a_new_session(self):
        self.authenticate()
        token = self.app.state.issue_launch_token()
        other = TestClient(self.app, base_url=self.origin, client=('127.0.0.1', 45679))
        try:
            response = other.get('/launch', params={'token': token}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(other.get('/api/session').status_code, 200)
        finally:
            other.close()


if __name__ == '__main__':
    unittest.main()
