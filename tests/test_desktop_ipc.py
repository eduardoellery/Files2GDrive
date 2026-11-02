import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from files2gdrive.desktop_ipc import (LocalControlServer, LocalControlError, request,
                                     SOCKET_NAME, MAX_MESSAGE)


class LocalControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / 'state'
        self.state.mkdir(mode=0o700)
        self.servers = []
    def tearDown(self):
        for server in reversed(self.servers): server.stop()
        self.temp.cleanup()
    def server(self, handler=None):
        server = LocalControlServer(self.state, handler or (lambda action, payload: dict(action=action, **payload))).start()
        self.servers.append(server)
        return server
    def raw(self, data):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2); connection.connect(str(self.state / SOCKET_NAME)); connection.sendall(data)
            result = bytearray()
            while not result.endswith(b'\n'):
                part = connection.recv(4096)
                if not part: break
                result.extend(part)
            return json.loads(result)

    def test_round_trip_whitelisted_controls_and_socket_permissions(self):
        self.server()
        for action in ('status', 'pause', 'resume', 'open', 'quit'):
            self.assertEqual(request(self.state, action, dict(marker='test')), dict(action=action, marker='test'))
        self.assertEqual(stat.S_IMODE((self.state / SOCKET_NAME).stat().st_mode), 0o600)

    def test_context_manager_removes_only_its_socket(self):
        with LocalControlServer(self.state, lambda *_: {'ok': True}):
            self.assertTrue(request(self.state, 'status')['ok'])
        self.assertFalse((self.state / SOCKET_NAME).exists())

    def test_unknown_actions_and_invalid_payloads_never_reach_handler(self):
        handler = Mock(return_value={}); self.server(handler)
        for payload in (b'{"action":"delete-all"}\n', b'{"action":"pause","payload":[]}\n',
                        b'{"action":"pause","unexpected":true}\n'):
            self.assertFalse(self.raw(payload)['ok'])
        handler.assert_not_called()
        with self.assertRaises(LocalControlError): request(self.state, 'arbitrary')

    def test_malformed_nonobject_and_nonfinite_json_rejected(self):
        self.server()
        for message in (b'not JSON\n', b'[]\n', b'{"action":"status","payload":{"x":NaN}}\n'):
            self.assertFalse(self.raw(message)['ok'])

    def test_oversized_requests_and_responses_are_bounded(self):
        self.server(lambda *_: {'too_large': 'x' * MAX_MESSAGE})
        with self.assertRaises(LocalControlError): request(self.state, 'status')
        with self.assertRaises(LocalControlError): request(self.state, 'status', {'large': 'x' * MAX_MESSAGE})
        response = self.raw(b'x' * MAX_MESSAGE)
        self.assertFalse(response['ok'])

    def test_peer_uid_is_verified_on_server(self):
        handler = Mock(return_value={}); self.server(handler)
        with patch('files2gdrive.desktop_ipc._peer_uid', return_value=os.getuid() + 1):
            result = self.raw(b'{"action":"quit"}\n')
        self.assertFalse(result['ok']); handler.assert_not_called()

    def test_client_verifies_peer_uid_too(self):
        self.server()
        with patch('files2gdrive.desktop_ipc._peer_uid', return_value=os.getuid() + 1):
            with self.assertRaises(LocalControlError): request(self.state, 'status')

    def test_live_duplicate_refused_without_removing_first_socket(self):
        self.server()
        first = (self.state / SOCKET_NAME).stat().st_ino
        with self.assertRaises(LocalControlError): LocalControlServer(self.state, lambda *_: {}).start()
        self.assertEqual((self.state / SOCKET_NAME).stat().st_ino, first)
        self.assertEqual(request(self.state, 'status')['action'], 'status')

    def test_stale_private_socket_is_replaced(self):
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.state / SOCKET_NAME)); stale.close()
        (self.state / SOCKET_NAME).chmod(0o600)
        self.server()
        self.assertEqual(request(self.state, 'status')['action'], 'status')

    def test_files_symlinks_and_insecure_directories_are_not_modified(self):
        path = self.state / SOCKET_NAME
        path.write_text('keep me')
        with self.assertRaises(LocalControlError): self.server()
        self.assertEqual(path.read_text(), 'keep me'); path.unlink()
        target = self.state / 'target'; target.write_text('keep target'); path.symlink_to(target)
        with self.assertRaises(LocalControlError): self.server()
        self.assertEqual(target.read_text(), 'keep target'); path.unlink()
        self.state.chmod(0o755)
        with self.assertRaises(LocalControlError): self.server()
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o755)

    def test_stop_does_not_remove_replaced_socket(self):
        server = self.server()
        path = self.state / SOCKET_NAME
        path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.bind(str(path)); path.chmod(0o600)
        try:
            server.stop()
            self.assertTrue(path.exists())
        finally:
            replacement.close(); path.unlink(missing_ok=True)

    def test_internal_errors_do_not_disclose_handler_details(self):
        handler = Mock(side_effect=RuntimeError('SECRET TOKEN'))
        self.server(handler)
        with self.assertRaises(LocalControlError) as error: request(self.state, 'status')
        self.assertNotIn('SECRET', str(error.exception))

    def test_client_rejects_missing_or_nonprivate_socket(self):
        with self.assertRaises(LocalControlError): request(self.state, 'status')
        self.server()
        (self.state / SOCKET_NAME).chmod(0o666)
        with self.assertRaises(LocalControlError): request(self.state, 'status')
