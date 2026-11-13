from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from files2gdrive.tray import (TrayActions, _Dispatcher, menu_state, run_tray, tray_lock,
                               validate_launch_file)


class TrayTests(unittest.TestCase):
    def test_only_private_state_bootstrap_file_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory); state.chmod(0o700)
            launch = state / 'web-launch.html'; launch.write_text('<form></form>'); launch.chmod(0o600)
            valid = launch.as_uri()
            self.assertEqual(validate_launch_file(valid, state), valid)
            for invalid in ('https://127.0.0.1:8766/launch?token=' + 'a'*32,
                            'file:///etc/passwd', valid + '#fragment', valid + '?token=x'):
                with self.assertRaises(ValueError): validate_launch_file(invalid, state)
            launch.chmod(0o644)
            with self.assertRaises(ValueError): validate_launch_file(valid, state)

    def test_open_validates_before_invoking_browser(self):
        opener = Mock(return_value=True)
        client = Mock(return_value={'url': 'https://evil.example'})
        actions = TrayActions(client, opener=opener)
        with self.assertRaises(ValueError): actions.perform('open')
        opener.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory); state.chmod(0o700)
            launch = state / 'web-launch.html'; launch.write_text('<form></form>'); launch.chmod(0o600)
            client.return_value = {'url': launch.as_uri()}
            actions = TrayActions(client, opener=opener,
                                  launch_validator=lambda value: validate_launch_file(value, state))
            actions.perform('open'); opener.assert_called_once_with(launch.as_uri())

    def test_quit_only_exits_after_acknowledgment(self):
        quit_callback = Mock(); client = Mock(side_effect=RuntimeError('offline'))
        actions = TrayActions(client, exit_callback=quit_callback)
        with self.assertRaises(RuntimeError): actions.perform('quit')
        quit_callback.assert_not_called()
        client.side_effect = None; client.return_value = {'ok': True}
        actions.perform('quit'); quit_callback.assert_called_once()

    def test_control_actions_are_whitelisted(self):
        client = Mock(return_value={'ok': True}); actions = TrayActions(client)
        actions.perform('pause'); actions.perform('resume')
        self.assertEqual([call.args[0] for call in client.call_args_list], ['pause', 'resume'])
        with self.assertRaises(ValueError): actions.perform('delete')

    def test_menu_states_preserve_manual_pause_while_auth_required(self):
        status = dict(running=True, paused=True, auth_state='required', queue_total=4)
        state = menu_state(status)
        self.assertEqual(state['icon'], 'dialog-warning-symbolic')
        self.assertIn('pausado', state['title'])
        self.assertFalse(state['can_pause']); self.assertTrue(state['can_resume'])
        status['reconnect_pending'] = True
        self.assertFalse(menu_state(status)['can_resume'])
        self.assertEqual(menu_state(None)['icon'], 'dialog-warning-symbolic')
        self.assertEqual(menu_state(dict(running=True, paused=True, auth_state='connected'))['icon'], 'emblem-pause-symbolic')
        self.assertEqual(menu_state(dict(running=True, active_roots=0))['title'], 'Pronto para configurar')

    def test_single_helper_lock_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with tray_lock(state):
                with self.assertRaises(RuntimeError):
                    with tray_lock(state): pass
            self.assertEqual((state / 'tray.lock').stat().st_mode & 0o777, 0o600)
            (state / 'tray.lock').unlink()
            (state / 'target').write_text('unchanged'); (state / 'tray.lock').symlink_to(state / 'target')
            with self.assertRaises(OSError):
                with tray_lock(state): pass
            self.assertEqual((state / 'target').read_text(), 'unchanged')

    def test_insecure_state_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory).chmod(0o755)
            with self.assertRaises(RuntimeError):
                with tray_lock(directory): pass

    def test_dispatcher_never_runs_ui_callback_in_worker_and_prevents_overlap(self):
        entered = threading.Event(); release = threading.Event(); scheduled = threading.Event()
        callback = Mock(); deliveries = []
        def client(action): entered.set(); release.wait(2); return {'running': True}
        def schedule(function, *args): deliveries.append((function, args)); scheduled.set()
        dispatcher = _Dispatcher(TrayActions(client), schedule, callback)
        self.assertTrue(dispatcher.submit('status')); self.assertTrue(entered.wait(1))
        self.assertFalse(dispatcher.submit('status'))
        callback.assert_not_called(); release.set(); self.assertTrue(scheduled.wait(1))
        callback.assert_not_called()
        function, args = deliveries[0]; function(*args); callback.assert_called_once()
        dispatcher.close(); self.assertFalse(dispatcher.submit('status'))

    def test_missing_gtk_is_friendly_and_does_not_touch_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Mock()
            with patch('files2gdrive.tray._load_gtk', side_effect=RuntimeError('Bandeja indisponível')):
                with self.assertRaisesRegex(RuntimeError, 'indisponível'): run_tray(directory, client=client)
            client.assert_not_called()

    def test_explicit_control_is_queued_while_status_request_is_busy(self):
        entered = threading.Event(); release = threading.Event(); paused = threading.Event()
        calls = []
        def client(action):
            calls.append(action)
            if len(calls) == 1: entered.set(); release.wait(2)
            if action == 'pause': paused.set()
            return {'running': True}
        dispatcher = _Dispatcher(TrayActions(client), lambda callback, *args: None, Mock())
        dispatcher.submit('status'); self.assertTrue(entered.wait(1))
        self.assertTrue(dispatcher.submit('pause'))
        self.assertFalse(dispatcher.submit('status'))
        release.set(); self.assertTrue(paused.wait(1))
        dispatcher.close()
        self.assertEqual(calls[:2], ['status', 'pause'])


if __name__ == '__main__': unittest.main()
