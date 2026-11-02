import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from files2gdrive.session_integration import install_session, remove_session, quote_exec_argument


def parse_exec(text):
    line = next(line[5:] for line in text.splitlines() if line.startswith('Exec='))
    # First Desktop Entry string escaping, then Exec quoting, then literal %%.
    decoded = ''
    index = 0
    escapes = {'\\': '\\', 's': ' ', 'n': '\n', 't': '\t', 'r': '\r'}
    while index < len(line):
        if line[index] == '\\':
            index += 1
            decoded += escapes[line[index]]
        else:
            decoded += line[index]
        index += 1
    arguments = shlex.split(decoded, posix=True)
    # POSIX shell lexing leaves an escaped backtick/dollar intact inside quotes;
    # Desktop Entry Exec decoding removes these literal quoting escapes.
    return [argument.replace('\\$', '$').replace('\\`', '`').replace('%%', '%') for argument in arguments]


class SessionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.config = self.base / 'config'; self.data = self.base / 'data'
        self.state = self.base / 'state'
        self.executable = self.base / 'venv/bin/python'
    def tearDown(self): self.temp.cleanup()
    def install(self, **kwargs):
        return install_session(self.state, executable=self.executable, config_home=self.config, data_home=self.data, **kwargs)
    def remove(self):
        return remove_session(self.state, config_home=self.config, data_home=self.data)

    def test_launcher_and_autostart_are_distinct_and_explicit(self):
        self.assertFalse(self.config.exists())
        locations = self.install()
        launcher = Path(locations['launcher']); autostart = Path(locations['autostart'])
        arguments = [str(self.executable), '-m', 'files2gdrive', '--state-dir', str(self.state), 'desktop']
        self.assertEqual(parse_exec(launcher.read_text()), arguments)
        self.assertEqual(parse_exec(autostart.read_text()), arguments + ['--background'])
        self.assertEqual(launcher.stat().st_mode & 0o777, 0o644)
        self.assertIn('X-Files2GDrive-Managed=true', launcher.read_text())
        self.assertNotIn('shell', launcher.read_text())
        self.assertFalse(self.state.exists())

    def test_reinstall_and_removal_do_not_touch_backup_state(self):
        self.state.mkdir(); (self.state / 'keep').write_text('backup settings')
        locations = self.install(); self.install()
        self.remove(); self.remove()
        self.assertFalse(any(Path(path).exists() for path in locations.values()))
        self.assertEqual((self.state / 'keep').read_text(), 'backup settings')

    def test_unrelated_entry_blocks_install_and_removal_before_mutating_either(self):
        self.config.joinpath('autostart').mkdir(parents=True)
        self.config.joinpath('autostart').chmod(0o755)
        unrelated = self.config / 'autostart/files2gdrive.desktop'
        unrelated.write_text('[Desktop Entry]\nType=Application\nName=Other\nExec=other\n')
        with self.assertRaises(ValueError): self.install()
        self.assertFalse((self.data / 'applications/files2gdrive.desktop').exists())
        self.assertIn('Name=Other', unrelated.read_text())
        with self.assertRaises(ValueError): self.remove()
        self.assertTrue(unrelated.exists())

    def test_another_state_cannot_replace_or_remove_managed_entries(self):
        locations = self.install()
        for function in (install_session, remove_session):
            with self.assertRaises(ValueError):
                function(self.base / 'other-state', config_home=self.config, data_home=self.data)
        self.assertTrue(all(Path(path).exists() for path in locations.values()))

    def test_symbolic_link_targets_are_never_followed(self):
        self.config.joinpath('autostart').mkdir(parents=True)
        self.config.joinpath('autostart').chmod(0o755)
        target = self.base / 'unrelated'; target.write_text('untouched')
        (self.config / 'autostart/files2gdrive.desktop').symlink_to(target)
        for function in (self.install, self.remove):
            with self.assertRaises(OSError): function()
        self.assertEqual(target.read_text(), 'untouched')

    def test_symbolic_link_parent_is_refused(self):
        actual = self.base / 'actual'; actual.mkdir()
        self.config.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(OSError): self.install()
        self.assertEqual(list(actual.iterdir()), [])

    def test_nonregular_entry_is_refused_without_blocking(self):
        self.config.joinpath('autostart').mkdir(parents=True)
        self.config.joinpath('autostart').chmod(0o755)
        os.mkfifo(self.config / 'autostart/files2gdrive.desktop')
        with self.assertRaises(ValueError): self.install()

    def test_exec_escapes_special_characters_and_percent_field_codes(self):
        self.state = self.base / 'space $cash `tick` "quote" back\\slash %u %F %%'
        self.executable = self.base / 'my $venv `test` "quote" back\\slash %u/python'
        result = self.install()
        text = Path(result['launcher']).read_text()
        self.assertEqual(parse_exec(text), [str(self.executable), '-m', 'files2gdrive', '--state-dir', str(self.state), 'desktop'])
        self.assertIn('%%u', text)
        self.assertIn('%%F', text)
        self.assertNotIn('sh -c', text)
        self.remove()

    def test_control_characters_and_equals_executable_are_refused(self):
        for executable in ('/tmp/python\nExec=bad', '/tmp/with=equals/python', '/tmp/with\tTab'):
            with self.assertRaises(ValueError):
                install_session(self.state, executable=executable, config_home=self.config, data_home=self.data)
        for value in ('line\n', 'line\r', '\t', '\x00'):
            with self.assertRaises(ValueError): quote_exec_argument(value)
        self.assertFalse(self.config.exists())

    def test_relative_explicit_directories_are_refused(self):
        with self.assertRaises(ValueError):
            install_session(self.state, config_home='relative', data_home=self.data)

    def test_xdg_locations_are_respected_without_modifying_home(self):
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.config), 'XDG_DATA_HOME': str(self.data)}):
            paths = install_session(self.state, executable=self.executable)
        self.assertEqual(paths['autostart'], str(self.config / 'autostart/files2gdrive.desktop'))
        self.assertEqual(paths['launcher'], str(self.data / 'applications/files2gdrive.desktop'))

    def test_world_writable_entry_directory_is_refused(self):
        directory = self.config / 'autostart'; directory.mkdir(parents=True); directory.chmod(0o777)
        with self.assertRaises(ValueError): self.install()
