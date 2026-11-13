import json
import io
from contextlib import redirect_stdout
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from files2gdrive.database import Database
from files2gdrive.packaged_launcher import default_state_directory,prepare_new_state,main
from files2gdrive import runtime_context
from files2gdrive.session_integration import install_session


class PackagedLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.base=Path(self.temp.name)
        self.resources=self.base/'resources'; self.resources.mkdir()
        self.resources.joinpath('oauth-client.json').write_text(json.dumps({'installed':{
            'client_id':'application.apps.googleusercontent.com','client_secret':'public-desktop-client',
            'auth_uri':'https://accounts.google.com/o/oauth2/auth',
            'token_uri':'https://oauth2.googleapis.com/token'}}))
        self.context=patch('files2gdrive.runtime_context._launcher',None); self.context.start()
    def tearDown(self): self.context.stop(); self.temp.cleanup()
    def test_release_version_uses_packaged_metadata_or_public_fallback(self):
        output = io.StringIO()
        with redirect_stdout(output):
            main(self.resources, Path('/usr/bin/files2gdrive'), ['--version'])
        self.assertEqual(output.getvalue().strip(), 'Files2GDrive 0.1.1')
        (self.resources / 'VERSION').write_text('0.1.1\n')
        output = io.StringIO()
        with redirect_stdout(output):
            main(self.resources, Path('/usr/bin/files2gdrive'), ['--version'])
        self.assertEqual(output.getvalue().strip(), 'Files2GDrive 0.1.1')
        self.assertFalse((self.base / 'state').exists())

    def test_new_profile_uses_drive_without_any_google_authorization(self):
        state=self.base/'state'
        self.assertTrue(prepare_new_state(state,self.resources))
        db=Database(state/'state.sqlite3')
        try:
            self.assertEqual(db.get('backend'),'drive')
            self.assertEqual(db.get('oauth_client_file'),str(self.resources/'oauth-client.json'))
            self.assertEqual(db.connection.execute('SELECT count(*) FROM roots').fetchone()[0],0)
            self.assertIsNone(db.get('refresh_token'))
        finally: db.close()
    def test_existing_profile_never_changed_even_if_client_missing(self):
        state=self.base/'state';db=Database(state/'state.sqlite3');db.set('backend','simulator');db.close()
        self.assertFalse(prepare_new_state(state,self.base/'no-resources'))
        db=Database(state/'state.sqlite3')
        try:self.assertEqual(db.get('backend'),'simulator')
        finally:db.close()
    def test_packaged_child_reexecutes_the_wrapper(self):
        runtime_context.configure_launcher('/usr/bin/files2gdrive')
        self.assertEqual(runtime_context.command('/state','serve'),['/usr/bin/files2gdrive','--state-dir','/state','serve'])
    def test_packaged_session_exec_uses_wrapper_not_system_python_module(self):
        runtime_context.configure_launcher('/usr/bin/files2gdrive')
        paths=install_session(self.base/'state',config_home=self.base/'config',data_home=self.base/'data')
        text=Path(paths['launcher']).read_text()
        self.assertIn('Exec="/usr/bin/files2gdrive" "--state-dir"',text)
        self.assertNotIn('"-m"',text)
    def test_default_paths_follow_only_absolute_xdg_state_home(self):
        with patch.dict(os.environ,{'XDG_STATE_HOME':str(self.base)}):
            self.assertEqual(default_state_directory(),self.base/'files2gdrive')
        with patch.dict(os.environ,{'XDG_STATE_HOME':'relative'}):
            self.assertEqual(default_state_directory(),Path.home()/'.local/state/files2gdrive')
    def test_no_arguments_launch_desktop_and_background_stays_quiet(self):
        state=self.base/'state'
        with patch('files2gdrive.cli.main') as cli:
            main(self.resources,Path('/usr/bin/files2gdrive'),['--state-dir',str(state),'--background'])
        cli.assert_called_once_with(['--state-dir',str(state),'desktop','--background'])
    def test_help_does_not_initialize_or_change_a_profile(self):
        state=self.base/'state'
        with patch('files2gdrive.cli.main'):
            main(self.resources,Path('/usr/bin/files2gdrive'),['--state-dir',str(state),'--help'])
        self.assertFalse(state.exists())
