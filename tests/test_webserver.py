import json
import re
import unittest
from unittest.mock import Mock

try:
    from fastapi.testclient import TestClient
    from files2gdrive.webserver import create_app, MAX_BODY
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
        token = self.app.state.launch_token
        response = self.client.post('/launch', data={'token': token}, headers={'Origin': 'null'}, follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        match = re.search(r'name="files2gdrive-session" content="([A-Za-z0-9_-]+)"', response.text)
        self.assertIsNotNone(match)
        self.session_token = match.group(1)
        session = self.client.get('/api/session', headers=self.headers())
        self.csrf = session.json()['csrf_token']
        return response

    def headers(self):
        result = {'Authorization': 'Bearer ' + getattr(self, 'session_token', '')}
        if hasattr(self, 'csrf'):
            result.update({'Origin': self.origin, 'X-CSRF-Token': self.csrf})
        return result

    def test_api_requires_bearer_even_if_legacy_cookie_is_present(self):
        for path in ('/api/status', '/api/session'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers['cache-control'], 'no-store')
        response = self.client.get('/api/status', headers={'Cookie': 'files2gdrive_session=legacy-secret'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.controller.snapshot.assert_not_called()

    def test_launch_is_single_use_and_bootstraps_a_bearer_session(self):
        token = self.app.state.launch_token
        response = self.authenticate()
        self.assertNotIn(token, response.text)
        self.assertIn('max-age=0', response.headers['set-cookie'].lower())
        self.assertEqual(self.client.get('/launch', params={'token': token}).status_code, 405)
        replay = self.client.post('/launch', data={'token': token}, headers={'Origin': 'null'})
        self.assertEqual(replay.status_code, 401)
        self.assertIn('sessão foi revogada', replay.json()['detail'])
        status = self.client.get('/api/status', headers=self.headers())
        self.assertEqual(status.status_code, 401)

    def test_bootstrap_token_is_one_use_and_sessions_are_distinct(self):
        self.authenticate()
        first = self.session_token
        second_token = self.app.state.issue_launch_token()
        response = self.client.post('/launch', data={'token': second_token}, headers={'Origin': 'null'})
        second = re.search(r'name="files2gdrive-session" content="([A-Za-z0-9_-]+)"', response.text).group(1)
        self.assertNotEqual(first, second)
        self.assertIn(first, self.app.state.sessions)
        self.assertIn(second, self.app.state.sessions)

    def test_expired_launch_token_and_expired_session_are_rejected(self):
        self.app.state.launch_token_expires_at = 0
        self.assertEqual(self.client.post('/launch', data={'token': self.app.state.launch_token},
                                          headers={'Origin': 'null'}).status_code, 401)
        self.app.state.issue_launch_token()
        self.authenticate()
        self.app.state.sessions[self.session_token]['expires_at'] = 0
        self.assertEqual(self.client.get('/api/status', headers=self.headers()).status_code, 401)

    def test_host_origin_and_remote_client_are_rejected(self):
        self.authenticate()
        for headers in ({'Host': 'localhost:8765'}, {'Host': 'evil.example'},
                        {'Origin': 'https://evil.example'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.client.get('/api/status', headers=headers).status_code, 403)
        remote = TestClient(self.app, base_url=self.origin, client=('192.0.2.1', 45678))
        try:
            self.assertEqual(remote.post('/launch', data={'token': self.app.state.issue_launch_token()},
                                         headers={'Origin': 'null'}).status_code, 403)
        finally:
            remote.close()
        self.controller.snapshot.assert_not_called()

    def test_mutations_require_both_exact_origin_and_csrf(self):
        self.authenticate()
        authorization = {'Authorization': 'Bearer ' + self.session_token}
        for headers in ({}, {'Origin': self.origin}, {'X-CSRF-Token': self.csrf},
                        {'Origin': self.origin, 'X-CSRF-Token': 'wrong'}):
            headers = dict(authorization, **headers)
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
        response = self.client.get('/api/logs/export?level=WARNING&root_id=root&since=2026-11-01',
                                   headers=self.headers())
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
                              ('/static/bootstrap.js', 'text/javascript'), ('/static/app.css', 'text/css')):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(content, response.headers['content-type'])
            self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertIn('Files2GDrive', self.client.get('/').text)

    def test_duplicate_authority_and_cross_origin_mutations_are_rejected(self):
        self.authenticate()
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
            response = other.post('/launch', data={'token': token}, headers={'Origin': 'null'}, follow_redirects=False)
            self.assertEqual(response.status_code, 200)
            session_token = re.search(r'name="files2gdrive-session" content="([A-Za-z0-9_-]+)"', response.text).group(1)
            self.assertEqual(other.get('/api/session', headers={'Authorization': 'Bearer ' + session_token}).status_code, 200)
        finally:
            other.close()


if __name__ == '__main__':
    unittest.main()
