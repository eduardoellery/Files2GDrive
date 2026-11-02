from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from files2gdrive.cli import main
from files2gdrive.database import Database


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = self.base / 'state'
        self.root = self.base / 'source'; self.root.mkdir()
        self.client = self.base / 'client.json'
        self.client.write_text(json.dumps({'installed': dict(
            client_id='test.apps.googleusercontent.com', client_secret='client-secret',
            auth_uri='https://accounts.google.com/o/oauth2/auth',
            token_uri='https://oauth2.googleapis.com/token')}))
    def tearDown(self): self.temp.cleanup()
    def call(self, *args):
        output = io.StringIO()
        with redirect_stdout(output): main(['--state-dir', str(self.state), *map(str, args)])
        return output.getvalue()
    def test_simulator_is_default_and_status_works_offline(self):
        self.call('add', self.root)
        status = json.loads(self.call('status'))
        self.assertEqual(status['backend'], 'simulator')
        self.assertEqual(len(status['roots']), 1)
    def test_configuration_does_not_authenticate_or_contact_google(self):
        with patch('files2gdrive.cli._auth', side_effect=AssertionError('must not authenticate')):
            self.call('configure-drive', '--client-file', self.client, '--computer-name', 'Ubuntu')
            self.assertEqual(json.loads(self.call('status'))['backend'], 'drive')
        db = Database(self.state / 'state.sqlite3')
        try:
            self.assertIsNone(db.get('token'))
            self.assertNotIn('client-secret', json.dumps([tuple(row) for row in db.connection.execute('SELECT * FROM settings')]))
        finally: db.close()
    def test_cannot_switch_observed_simulation_to_drive(self):
        (self.root / 'file').write_text('one')
        self.call('add', self.root); self.call('tick')
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
            self.call('configure-drive', '--client-file', self.client)
        self.assertEqual(failure.exception.code, 1)
        self.assertEqual(json.loads(self.call('status'))['backend'], 'simulator')
    def test_login_only_explicitly_requested(self):
        auth = Mock()
        with patch('files2gdrive.cli._auth', return_value=auth):
            self.call('login')
            auth.login.assert_called_once()
    def test_both_state_overlap_directions_rejected(self):
        self.state.mkdir(); nested = self.state / 'nested'; nested.mkdir()
        for root in (self.base, nested):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.call('add', root)

    def test_sdk_transport_keeps_308_for_resumable_uploads(self):
        try:
            from google.oauth2.credentials import Credentials
            import googleapiclient.discovery
        except ImportError:
            self.skipTest('Google SDK integration dependencies are not installed')
        from files2gdrive.cli import _remote
        from files2gdrive.database import Database
        db = Database(self.state / 'state.sqlite3')
        db.set('backend', 'drive')
        auth = Mock(); auth.credentials.return_value = Credentials(token='test-token', refresh_token='test-refresh', token_uri='https://oauth2.googleapis.com/token', client_id='test-client', client_secret='test-secret')
        try:
            with patch('files2gdrive.cli._auth', return_value=auth), \
                 patch('googleapiclient.discovery.build') as build, \
                 patch('files2gdrive.drive.GoogleDriveRemote'):
                _remote(db, self.state)
            http = build.call_args.kwargs['http'].http
            self.assertNotIn(308, http.redirect_codes)
            self.assertIn(307, http.redirect_codes)
            self.assertEqual(http.timeout, 30)
        finally:
            db.close()

    def test_auth_uses_stable_namespace_after_adopting_computer_id(self):
        self.call('configure-drive', '--client-file', self.client)
        db = Database(self.state / 'state.sqlite3')
        from files2gdrive.cli import _auth
        try:
            db.set('credential_store_id', 'namespace-before-reconnection')
            db.set('computer_id', 'adopted-identity')
            with patch('files2gdrive.auth.KeyringTokenStore') as store:
                _auth(db)
                store.assert_called_once_with('namespace-before-reconnection')
        finally:
            db.close()

    def test_pending_reconnect_blocks_configuration_changes(self):
        db = Database(self.state / 'state.sqlite3')
        db.set('reconnect_pending', '{}'); db.close()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.call('configure-drive', '--client-file', self.client)
        self.assertTrue(json.loads(self.call('status'))['reconnect_pending'])

    def test_restore_selects_subfolder_and_never_enables_monitoring(self):
        self.call('configure-drive', '--client-file', self.client)
        catalog = Mock()
        catalog.list_files.return_value = [dict(file_id='one', path='sub/a'), dict(file_id='two', path='other')]
        writer = Mock(); writer.restore.return_value = ['sub/a']
        with patch('files2gdrive.cli._remote'), \
             patch('files2gdrive.recovery_remote.DriveRecovery', return_value=catalog), \
             patch('files2gdrive.restore.RestoreManager', return_value=writer):
            result = json.loads(self.call('restore', 'computer', 'root', self.base / 'restored', '--path', 'sub'))
        writer.restore.assert_called_once_with([dict(file_id='one', path='sub/a')], self.base / 'restored')
        self.assertFalse(result['monitoring_enabled'])
        self.assertEqual(json.loads(self.call('status'))['roots'], [])
