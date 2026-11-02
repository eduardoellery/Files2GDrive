import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from files2gdrive.auth import AuthenticationError
from files2gdrive.database import Database
from files2gdrive.remote import SimulatedRemote
from files2gdrive.web_controller import WebController


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = self.base / 'state'
        self.source = self.base / 'source'; self.source.mkdir()
        self.controllers = []
    def tearDown(self):
        for controller in self.controllers: controller.stop()
        self.temp.cleanup()
    def controller(self, **kwargs):
        kwargs.setdefault('remote_factory', lambda db, state: SimulatedRemote(state / 'remote'))
        controller = WebController(self.state, tick_interval=0.05, **kwargs)
        self.controllers.append(controller)
        return controller
    def wait(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if condition(): return
            time.sleep(0.03)
        self.fail('Condition timed out')

    def test_directory_warmup_respects_pause_and_retry_deadline(self):
        from files2gdrive.engine import Engine
        from unittest.mock import Mock
        db = Database(self.state / 'state.sqlite3')
        self.addCleanup(db.close)
        remote = SimulatedRemote(self.state / 'remote')
        remote.prepare_directories = Mock()
        controller = WebController.__new__(WebController)
        controller.db = db
        controller.engine = Engine(db, remote, settle_seconds=0)
        (self.source / 'item').write_text('content')
        controller.engine.add_root(self.source)
        controller.engine.reconcile()
        controller._prepare_pending_directories()
        self.assertEqual(remote.prepare_directories.call_count, 1)
        self.assertEqual(remote.prepare_directories.call_args.args[0][0]['path'], 'item')
        remote.prepare_directories.reset_mock()
        controller.engine.pause()
        controller._prepare_pending_directories()
        remote.prepare_directories.assert_not_called()
        controller.engine.pause(False)
        with db.connection:
            db.connection.execute('UPDATE jobs SET next_attempt=?', (time.time()+1000,))
        controller._prepare_pending_directories()
        remote.prepare_directories.assert_not_called()

    def test_directory_warmup_auth_failure_preserves_jobs(self):
        from files2gdrive.drive import DriveError
        from files2gdrive.engine import Engine
        from unittest.mock import Mock
        db = Database(self.state / 'state.sqlite3')
        self.addCleanup(db.close)
        remote = SimulatedRemote(self.state / 'remote')
        remote.prepare_directories = Mock(side_effect=DriveError('authentication required',requires_auth=True))
        controller = WebController.__new__(WebController)
        controller.db = db
        controller.engine = Engine(db, remote, settle_seconds=0)
        (self.source / 'item').write_text('content')
        controller.engine.add_root(self.source); controller.engine.reconcile()
        with self.assertRaises(AuthenticationError): controller._prepare_pending_directories()
        self.assertEqual(db.connection.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
        remote.prepare_directories.side_effect = DriveError('rate limit')
        controller._prepare_pending_directories()  # Per-job dispatcher owns backoff.
        self.assertEqual(db.connection.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)

    def test_snapshot_and_commands_share_worker_not_sqlite_connection(self):
        controller = self.controller()
        self.wait(lambda: controller.snapshot()['auth_state'] == 'simulator')
        (self.source / '.config').write_text('configuration')
        controller.call('add_root', dict(path=str(self.source), name='Config'))
        self.wait(lambda: controller.snapshot()['queue_total'] == 1)
        self.assertEqual(controller.snapshot()['roots'][0]['name'], 'Config')
        self.wait(lambda: len(list((self.state / 'remote').glob('*.json'))) == 1)
        self.wait(lambda: controller.snapshot()['queue_total'] == 0)
        controller.call('control', dict(action='pause'))
        self.assertTrue(controller.snapshot()['paused'])

    def test_filters_require_unchanged_preview(self):
        controller = self.controller()
        (self.source / 'a.tmp').write_text('a')
        controller.call('add_root', dict(path=str(self.source)))
        payload = dict(patterns=['*.tmp'], ignore_case=False)
        preview = controller.call('preview_filters', payload)
        self.assertEqual(preview['matched_count'], 1)
        with self.assertRaises(ValueError): controller.call('set_filters', payload)
        (self.source / 'b.tmp').write_text('b')
        with self.assertRaises(ValueError):
            controller.call('set_filters', dict(payload, preview_id=preview['preview_id']))
        fresh = controller.call('preview_filters', payload)
        controller.call('set_filters', dict(payload, preview_id=fresh['preview_id']))
        self.assertEqual(controller.snapshot()['settings']['global_patterns'], ['*.tmp'])

    def test_private_state_and_empty_paths_are_not_monitored(self):
        controller = self.controller()
        for path in ('', str(self.base), str(self.state)):
            with self.assertRaises(ValueError): controller.call('add_root', dict(path=path))
        self.assertEqual(controller.snapshot()['roots'], [])

    def test_expired_auth_preserves_manual_pause_and_reconnects_explicitly(self):
        db = Database(self.state / 'state.sqlite3'); db.set('backend', 'drive'); db.set('paused', '1'); db.close()
        attempts = []
        def remote(db, state):
            attempts.append(True)
            if len(attempts) == 1: raise AuthenticationError('test expiration')
            return SimulatedRemote(state / 'remote')
        controller = self.controller(remote_factory=remote)
        self.wait(lambda: controller.snapshot()['auth_state'] == 'required')
        self.assertTrue(controller.snapshot()['paused'])
        controller.call('control', dict(action='check'))
        self.assertEqual(controller.snapshot()['auth_state'], 'connected')
        self.assertTrue(controller.snapshot()['paused'])

    def test_login_is_async_and_status_remains_available(self):
        entered = threading.Event(); release = threading.Event()
        class Auth:
            def login(self):
                entered.set(); release.wait(3)
        controller = self.controller(auth_factory=lambda db: Auth())
        result = controller.call('login')
        self.assertTrue(result['accepted']); self.assertTrue(entered.wait(1))
        self.assertEqual(controller.snapshot()['auth_state'], 'connecting')
        with self.assertRaises(RuntimeError): controller.call('login')
        release.set()
        self.wait(lambda: controller.snapshot()['task']['state'] == 'done')

    def test_queue_drains_without_waiting_for_next_reconciliation(self):
        from files2gdrive.engine import Engine
        class SettledEngine(Engine):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, settle_seconds=0, **kwargs)
        for name in ('a', 'b', 'c'):
            (self.source / name).write_text(name)
        # A long scan interval exposes the former one-job-per-scan stall.
        with patch('files2gdrive.web_controller.Engine', SettledEngine), patch('files2gdrive.watcher.Observer', None):
            controller = WebController(self.state, tick_interval=60,
                remote_factory=lambda db, state: SimulatedRemote(state / 'remote'))
            self.controllers.append(controller)
            controller.call('control', {'action': 'pause'})
            controller.call('add_root', {'path': str(self.source)})
            self.assertEqual(list((self.state / 'remote').glob('*.json')), [])
            controller.call('control', {'action': 'resume'})
            self.wait(lambda: len(list((self.state / 'remote').glob('*.json'))) == 3)
            self.wait(lambda: controller.snapshot()['queue_total'] == 0)

    def test_folder_preview_runs_as_background_task(self):
        entered = threading.Event(); release = threading.Event()
        def preview(manager, rules, revision):
            entered.set(); release.wait(3)
            return 'digest', {'upload_count': 0}
        controller = self.controller()
        with patch('files2gdrive.folder_selection.FolderSelection.preview', preview):
            result = controller.call('folder_selection_preview', {'rules': [], 'revision': 'test'})
            self.assertTrue(result['accepted']); self.assertTrue(entered.wait(1))
            self.assertEqual(controller.snapshot()['task']['state'], 'running')
            release.set()
            self.wait(lambda: controller.snapshot()['task']['state'] == 'done')
            self.assertIn('preview_id', controller.snapshot()['task']['result'])

    def test_picker_includes_hidden_only_when_requested_and_skips_links(self):
        (self.source / '.hidden').mkdir(); (self.source / 'visible').mkdir()
        (self.source / 'link').symlink_to(self.source / 'visible', target_is_directory=True)
        controller = self.controller()
        first = controller.call('browse', dict(path=str(self.source), show_hidden=False))
        second = controller.call('browse', dict(path=str(self.source), show_hidden=True))
        self.assertEqual([folder['name'] for folder in first['folders']], ['visible'])
        self.assertEqual([folder['name'] for folder in second['folders']], ['.hidden', 'visible'])

    def test_no_google_tokens_or_client_json_in_status(self):
        db = Database(self.state / 'state.sqlite3'); db.set('oauth_client_file', '/private/secret-client.json'); db.close()
        controller = self.controller()
        self.assertNotIn('secret-client', json.dumps(controller.snapshot()))

    def test_identity_request_auth_failure_requests_reauthentication(self):
        from files2gdrive.drive import DriveError
        def expired(db, state): raise DriveError('expired', requires_auth=True)
        controller = self.controller(remote_factory=expired)
        self.wait(lambda: controller.snapshot()['auth_state'] == 'required')
        self.assertFalse(controller.snapshot()['paused'])

    def test_shutdown_waits_for_owner_to_exit(self):
        entered = threading.Event(); release = threading.Event()
        class Auth:
            def login(self): entered.set(); release.wait(3)
        controller = self.controller(auth_factory=lambda db: Auth())
        controller.call('login'); self.assertTrue(entered.wait(1))
        stopped = threading.Event()
        def stop(): controller.stop(); stopped.set()
        closer = threading.Thread(target=stop); closer.start()
        self.assertFalse(stopped.wait(0.1))
        release.set(); self.assertTrue(stopped.wait(3)); closer.join()
        self.assertFalse(controller.thread.is_alive())

    def test_log_view_is_bounded_but_export_keeps_all_retained_entries(self):
        from unittest.mock import patch
        controller = self.controller()
        def records(*args, **kwargs): return iter([dict(message=str(index)) for index in range(1005)])
        with patch('files2gdrive.logging_setup.read_logs', side_effect=records):
            page = controller.call('logs')
            export = controller.call('logs', dict(export=True))
        self.assertEqual(len(page['entries']), 1000)
        self.assertEqual(len(export['entries']), 1005)
