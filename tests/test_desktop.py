from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from files2gdrive.desktop import ensure_engine,open_configuration


class DesktopTests(unittest.TestCase):
    def test_existing_engine_is_reused_without_process_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('files2gdrive.desktop.request',return_value={'running':True}), patch('files2gdrive.desktop.subprocess.Popen') as spawn:
                self.assertTrue(ensure_engine(directory)['running'])
                spawn.assert_not_called()
    def test_open_configuration_rejects_untrusted_bootstrap_path(self):
        with patch('files2gdrive.desktop.request',return_value={'url':'https://evil.example'}):
            with self.assertRaises(ValueError): open_configuration('/unused',opener=lambda url: True)

    def test_open_configuration_accepts_only_private_bootstrap_in_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory); state.chmod(0o700)
            launch = state / 'web-launch.html'; launch.write_text('<form></form>'); launch.chmod(0o600)
            opener = Mock(return_value=True)
            with patch('files2gdrive.desktop.request',return_value={'url':launch.as_uri()}):
                self.assertEqual(open_configuration(state, opener=opener), launch.as_uri())
            opener.assert_called_once_with(launch.as_uri())
