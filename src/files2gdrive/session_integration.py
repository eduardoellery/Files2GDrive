"""Explicit per-user application launcher and XDG autostart registration.

No shell is used by Exec. Files are installed only when install_session is called;
this module never enables autostart merely by being imported.
"""
from contextlib import ExitStack, contextmanager
import configparser
import os
from pathlib import Path
import stat
import sys
from uuid import uuid4

_FILENAME = 'files2gdrive.desktop'
_MARKER = 'X-Files2GDrive-Managed'
_STATE = 'X-Files2GDrive-State'
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _path(value):
    result = Path(value).expanduser().absolute()
    if any(character in str(result) for character in ('\x00', '\n', '\r', '\t')):
        raise ValueError('Session paths cannot contain NUL, newline, carriage return or tab')
    return result


def quote_exec_argument(value):
    """Quote a literal argument using both Desktop Entry escaping layers.

    Exec quoting is decoded *after* the string-value backslash escaping. Four
    backslashes therefore encode one literal backslash inside a quoted argument.
    Literal percent signs are doubled so they cannot expand as field codes.
    """
    value = str(value)
    if any(character in value for character in ('\x00', '\n', '\r', '\t')):
        raise ValueError('Desktop command arguments cannot contain control characters')
    escaped = ''.join('\\' + character if character in '\\"`$' else character for character in value)
    escaped = escaped.replace('%', '%%').replace('\\', '\\\\')
    return '"' + escaped + '"'


def _base(explicit, variable, fallback):
    if explicit is not None:
        value = Path(explicit).expanduser()
        if not value.is_absolute():
            raise ValueError('Session installation directories must be absolute')
        return _path(value)
    value = os.environ.get(variable)
    # The XDG specification ignores relative environment values.
    return _path(value) if value and Path(value).is_absolute() else _path(fallback)


def _locations(config_home=None, data_home=None):
    config = _base(config_home, 'XDG_CONFIG_HOME', Path.home() / '.config')
    data = _base(data_home, 'XDG_DATA_HOME', Path.home() / '.local/share')
    return {'launcher': data / 'applications' / _FILENAME,
            'autostart': config / 'autostart' / _FILENAME}


def _desktop_text(state, executable, background):
    from .runtime_context import launcher
    executable = _path(executable or launcher() or sys.executable)
    if '=' in str(executable):
        raise ValueError('Desktop executable paths cannot contain an equals sign')
    arguments = ([str(executable)] if launcher() == executable else [str(executable), '-m', 'files2gdrive']) + ['--state-dir', str(state), 'desktop']
    if background:
        arguments.append('--background')
    return '\n'.join([
        '[Desktop Entry]', 'Version=1.0', 'Type=Application', 'Name=Files2GDrive',
        'Comment=Backup de arquivos locais para o Google Drive',
        'Exec=' + ' '.join(quote_exec_argument(argument) for argument in arguments),
        'Icon=' + str(Path(__file__).with_name('assets') / 'files2gdrive.svg'), 'Terminal=false', 'StartupNotify=false',
        'Categories=Utility;Archiving;', _MARKER + '=true',
        _STATE + '=' + str(state).replace('\\', '\\\\'),
        'X-GNOME-Autostart-enabled=true' if background else 'X-GNOME-UsesNotifications=true',
        '',
    ])


@contextmanager
def _directory(path, create):
    descriptor = os.open('/', _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            if part in ('.', '..'):
                raise ValueError('Session installation paths must not contain parent references')
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError('Session entry directory must belong to this user and not be writable by others')
        yield descriptor
    finally:
        os.close(descriptor)


def _inspect(directory, name, state):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                             dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 64 * 1024:
            raise ValueError('Refusing to replace an unrelated session entry')
        try:
            parser = configparser.ConfigParser(interpolation=None, strict=True)
            parser.read_string(stream.read().decode('utf-8'))
            section = parser['Desktop Entry']
            stored_state = section.get(_STATE, '').replace('\\\\', '\\')
            if (section.get(_MARKER) != 'true' or section.get('Type') != 'Application'
                    or stored_state != str(state)):
                raise ValueError('Unmanaged entry')
        except (ValueError, UnicodeError, KeyError, configparser.Error):
            raise ValueError('Refusing to replace or remove an unrelated session entry') from None
        return info.st_dev, info.st_ino


def _assert_identity(directory, name, expected):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        if expected is not None:
            raise ValueError('Session entry changed during installation') from None
        return
    if expected is None or not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != expected:
        raise ValueError('Session entry changed during installation')


def _write(directory, name, text, existing):
    temporary = '.files2gdrive-' + uuid4().hex + '.tmp'
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o644, dir_fd=directory)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(text.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        _assert_identity(directory, name, existing)
        if existing is None:
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        else:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def install_session(state_directory, executable=None, config_home=None, data_home=None):
    """Install menu launcher and autostart entry for this user, explicitly.

    Existing files must be managed by Files2GDrive for the same state directory.
    Symlink targets or parent directories and unrelated files are never followed.
    """
    state = _path(state_directory)
    locations = _locations(config_home, data_home)
    texts = {kind: _desktop_text(state, executable, kind == 'autostart') for kind in locations}
    with ExitStack() as stack:
        directories = {kind: stack.enter_context(_directory(path.parent, True)) for kind, path in locations.items()}
        existing = {kind: _inspect(directories[kind], path.name, state) for kind, path in locations.items()}
        for kind, path in locations.items():
            _write(directories[kind], path.name, texts[kind], existing[kind])
    return {kind: str(path) for kind, path in locations.items()}


def remove_session(state_directory, executable=sys.executable, config_home=None, data_home=None):
    """Remove only this user's managed entries, leaving state/backups untouched."""
    state = _path(state_directory)
    locations = _locations(config_home, data_home)
    with ExitStack() as stack:
        directories = {}
        for kind, path in locations.items():
            try:
                directories[kind] = stack.enter_context(_directory(path.parent, False))
            except FileNotFoundError:
                pass
        existing = {kind: _inspect(directory, locations[kind].name, state) for kind, directory in directories.items()}
        for kind, identity in existing.items():
            if identity is not None:
                directory = directories[kind]
                _assert_identity(directory, locations[kind].name, identity)
                os.unlink(locations[kind].name, dir_fd=directory)
                os.fsync(directory)
    return {kind: str(path) for kind, path in locations.items()}
