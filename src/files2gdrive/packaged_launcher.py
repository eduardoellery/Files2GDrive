"""First-run defaults for the installed .deb; never imports a user's auth tokens."""
import argparse
import fcntl
import os
from pathlib import Path
import socket
import sys


def default_state_directory():
    value = os.environ.get('XDG_STATE_HOME')
    parent = Path(value) if value and Path(value).is_absolute() else Path.home() / '.local/state'
    return parent / 'files2gdrive'


def prepare_new_state(state, resources):
    from .auth import _client_config
    from .database import Database
    from .desktop_ipc import _private_state
    state = _private_state(state, create=True)
    descriptor = os.open(state / 'bootstrap.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if (state / 'state.sqlite3').exists():
            return False  # Existing profiles and live workers are never changed here.
        client = Path(resources) / 'oauth-client.json'
        _client_config(client)
        db = Database(state / 'state.sqlite3')
        try:
            with db.connection:
                for key, value in dict(backend='drive', oauth_client_file=str(client),
                                       drive_folder_name='Files2GDrive', computer_name=socket.gethostname()).items():
                    db.connection.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, value))
        finally:
            db.close()
        return True
    finally:
        os.close(descriptor)


def main(resources, executable, argv=None):
    from . import runtime_context
    runtime_context.configure_launcher(executable)
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ['--version']:
        version = Path(resources) / 'VERSION'
        print('Files2GDrive ' + (version.read_text().strip() if version.is_file() else '0.1.0~alpha3'))
        return
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--state-dir', type=Path)
    options, rest = parser.parse_known_args(arguments)
    state = (options.state_dir or default_state_directory()).expanduser().absolute()
    if rest in ([], ['--background']):
        rest = ['desktop', *rest]
    if '--help' not in rest and '-h' not in rest:
        prepare_new_state(state, resources)
    from .cli import main as cli_main
    return cli_main(['--state-dir', str(state), *rest])
