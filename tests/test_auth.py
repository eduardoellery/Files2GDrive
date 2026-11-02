import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from files2gdrive.auth import (AuthManager, AuthenticationError, KeyringTokenStore,
                              TokenStoreError, DRIVE_SCOPE, SERVICE_NAME)

CLIENT = {'installed': {
    'client_id': 'test.apps.googleusercontent.com', 'client_secret': 'client-secret',
    'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
    'token_uri': 'https://oauth2.googleapis.com/token',
}}


class MemoryBackend:
    def __init__(self): self.data = {}
    def get_password(self, service, account): return self.data.get((service, account))
    def set_password(self, service, account, value): self.data[service, account] = value
    def delete_password(self, service, account): del self.data[service, account]


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.client = Path(self.temporary.name) / 'client.json'
        self.client.write_text(json.dumps(CLIENT))
        self.backend = MemoryBackend()
        self.store = KeyringTokenStore('computer', backend=self.backend)

    def tearDown(self): self.temporary.cleanup()

    def manager(self, **kwargs): return AuthManager(self.client, self.store, **kwargs)

    def info(self):
        return dict(CLIENT['installed'], token='access-secret', refresh_token='refresh-secret',
                    scopes=[DRIVE_SCOPE])

    def credential(self, valid=True):
        credential = Mock(valid=valid, refresh_token='refresh-secret', granted_scopes=[DRIVE_SCOPE])
        credential.to_json.return_value = json.dumps(self.info())
        return credential

    def test_store_is_namespaced_and_delete_is_idempotent(self):
        self.store.set('private')
        self.assertEqual(self.backend.data[(SERVICE_NAME, 'computer')], 'private')
        other = KeyringTokenStore('other-computer', backend=self.backend)
        self.assertIsNone(other.get())
        self.store.delete(); self.store.delete(); self.assertIsNone(self.store.get())

    def test_store_errors_do_not_expose_credentials(self):
        backend = Mock(); backend.set_password.side_effect = RuntimeError('refresh-secret')
        store = KeyringTokenStore('computer', backend=backend)
        with self.assertRaises(TokenStoreError) as failure: store.set('access-secret')
        self.assertNotIn('secret', str(failure.exception))

    def test_login_loopback_pkce_factory_and_storage(self):
        credential = self.credential()
        flow = Mock(); flow.run_local_server.return_value = credential
        factory = Mock(return_value=flow)
        self.assertIs(self.manager(flow_factory=factory).login(), credential)
        factory.assert_called_once_with(CLIENT, (DRIVE_SCOPE,))
        options = flow.run_local_server.call_args.kwargs
        self.assertEqual(options['host'], '127.0.0.1'); self.assertEqual(options['port'], 0)
        self.assertEqual(options['access_type'], 'offline')
        self.assertEqual(options['authorization_prompt_message'], '')
        self.assertTrue(options['open_browser'])
        self.assertEqual(json.loads(self.store.get())['refresh_token'], 'refresh-secret')

    def test_missing_offline_access_is_not_saved(self):
        credential = self.credential(); credential.refresh_token = None
        flow = Mock(); flow.run_local_server.return_value = credential
        with self.assertRaises(AuthenticationError): self.manager(flow_factory=lambda *_: flow).login()
        self.assertIsNone(self.store.get())

    def test_refresh_expired_credentials_and_save(self):
        self.store.set(json.dumps(self.info()))
        credential = self.credential(valid=False)
        credential.refresh.side_effect = lambda request: setattr(credential, 'valid', True)
        factory = Mock(return_value=credential); request = object()
        manager = self.manager(credentials_factory=factory, request_factory=lambda: request)
        self.assertIs(manager.credentials(), credential)
        credential.refresh.assert_called_once_with(request)
        self.assertEqual(json.loads(self.store.get())['token'], 'access-secret')

    def test_valid_credentials_do_not_refresh(self):
        self.store.set(json.dumps(self.info())); credential = self.credential()
        self.assertIs(self.manager(credentials_factory=lambda *_: credential).credentials(), credential)
        credential.refresh.assert_not_called()

    def test_no_tokens_requires_login(self):
        with self.assertRaisesRegex(AuthenticationError, 'first'): self.manager().credentials()

    def test_client_config_rejects_web_and_non_google_endpoints(self):
        for configuration in ({'web': CLIENT['installed']}, {'installed': dict(CLIENT['installed'], token_uri='https://evil.invalid/token')}):
            self.client.write_text(json.dumps(configuration))
            with self.assertRaises(AuthenticationError): self.manager()

    def test_saved_credentials_cannot_redirect_refresh(self):
        info = dict(self.info(), token_uri='https://evil.invalid/token')
        self.store.set(json.dumps(info)); factory = Mock()
        with self.assertRaises(AuthenticationError): self.manager(credentials_factory=factory).credentials()
        factory.assert_not_called()

    def test_failure_messages_are_sanitized(self):
        factory = Mock(side_effect=RuntimeError('secret credentials'))
        with self.assertRaises(AuthenticationError) as error: self.manager(flow_factory=factory).login()
        self.assertNotIn('secret credentials', str(error.exception))

    def test_logout_only_removes_local_store(self):
        self.store.set(json.dumps(self.info())); self.manager().logout()
        self.assertIsNone(self.store.get())


if __name__ == '__main__': unittest.main()
