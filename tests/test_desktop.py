from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from files2gdrive.desktop import ensure_engine,open_configuration


class DesktopTests(unittest.TestCase):
    def test_existing_engine_is_reused_without_process_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('files2gdrive.desktop.request',return_value={'running':True}), patch('files2gdrive.desktop.subprocess.Popen') as spawn:
                self.assertTrue(ensure_engine(directory)['running'])
                spawn.assert_not_called()
    def test_open_configuration_requires_valid_local_launch_url(self):
        with patch('files2gdrive.desktop.request',return_value={'url':'https://evil.example'}):
            with self.assertRaises(ValueError): open_configuration('/unused',opener=lambda url: True)
