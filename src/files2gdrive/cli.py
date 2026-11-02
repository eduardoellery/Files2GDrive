"""Development CLI for the local simulator and explicitly configured Drive."""
import argparse
import json
import signal
import socket
import threading
from pathlib import Path

from .database import Database
from .engine import Engine
from .remote import SimulatedRemote


def _display_name(value):
    if not value.strip() or value in ('.', '..') or '/' in value or '\x00' in value:
        raise ValueError('Choose a non-empty display name without slashes')
    return value


def _auth(db):
    from .auth import AuthManager, KeyringTokenStore
    client = db.get('oauth_client_file')
    if not client:
        raise RuntimeError('Run configure-drive with the application OAuth client JSON first')
    return AuthManager(client, KeyringTokenStore(db.get('credential_store_id', db.get('computer_id'))))


def _remote(db, state):
    backend = db.get('backend', 'simulator')
    if backend == 'simulator':
        return SimulatedRemote(state / 'simulated-remote')
    credentials = _auth(db).credentials()
    service = _drive_service(credentials)
    # Serialize once on the database owner. Each worker gets independent
    # credentials and HTTP connections, with no keyring/SQLite access.
    worker_credentials = credentials.to_json()
    def service_factory():
        from .auth import _credentials_factory, SCOPES
        return _drive_service(_credentials_factory(json.loads(worker_credentials), SCOPES))
    from .drive import GoogleDriveRemote
    return GoogleDriveRemote(db, service, folder_name=db.get('drive_folder_name', 'Files2GDrive'),
                             computer_name=db.get('computer_name', socket.gethostname()),
                             service_factory=service_factory)


def _drive_service(credentials):
    try:
        import google_auth_httplib2
        from googleapiclient.discovery import build
        from googleapiclient.http import build_http
        transport = build_http()
        transport.timeout = 30
        http = google_auth_httplib2.AuthorizedHttp(credentials, http=transport)
        return build('drive', 'v3', http=http, cache_discovery=False)
    except ImportError:
        raise RuntimeError('Install the project Google integration dependencies first') from None
    except Exception:
        raise RuntimeError('Could not initialize the Google Drive client') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Files2GDrive backup engine; simulator is the default')
    parser.add_argument('--state-dir', type=Path, required=True, help='Private state directory outside monitored roots')
    sub = parser.add_subparsers(dest='command', required=True)
    add = sub.add_parser('add'); add.add_argument('path', type=Path); add.add_argument('--name')
    remove = sub.add_parser('remove'); remove.add_argument('root_id')
    filters = sub.add_parser('filters'); filters.add_argument('patterns', nargs='*')
    filters.add_argument('--root-id'); filters.add_argument('--ignore-case', action='store_true')
    logs = sub.add_parser('logs'); logs.add_argument('--level'); logs.add_argument('--root-id')
    configure = sub.add_parser('configure-drive')
    configure.add_argument('--client-file', type=Path, required=True)
    configure.add_argument('--folder-name', default='Files2GDrive')
    configure.add_argument('--computer-name', default=socket.gethostname())
    web = sub.add_parser('serve')
    web.add_argument('--port', type=int, default=8765)
    web.add_argument('--no-browser', action='store_true')
    desktop_parser = sub.add_parser('desktop')
    desktop_parser.add_argument('--background', action='store_true')
    sub.add_parser('install-session'); sub.add_parser('remove-session')
    sub.add_parser('open'); sub.add_parser('quit')
    sub.add_parser('backups')
    catalog = sub.add_parser('backup-files')
    catalog.add_argument('computer_id'); catalog.add_argument('--root-id'); catalog.add_argument('--active-only', action='store_true')
    restore = sub.add_parser('restore')
    restore.add_argument('computer_id'); restore.add_argument('root_id'); restore.add_argument('destination', type=Path)
    restore.add_argument('--file-id', action='append', default=[])
    restore.add_argument('--path', dest='paths', action='append', default=[], help='Relative file or subfolder path; repeat to select several')
    restore.add_argument('--active-only', action='store_true')
    prepare = sub.add_parser('reconnect-prepare')
    prepare.add_argument('computer_id'); prepare.add_argument('--map', dest='mappings', action='append', required=True,
                                                          help='ROOT_UUID=/absolute/local/path (repeat per root)')
    sub.add_parser('reconnect-review'); sub.add_parser('reconnect-cancel')
    confirm = sub.add_parser('reconnect-confirm'); confirm.add_argument('--review-id', required=True)
    for command in ('login', 'logout', 'pause', 'resume', 'status', 'tick', 'run'):
        sub.add_parser(command)
    args = parser.parse_args(argv)
    state = args.state_dir.absolute()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command in ('desktop', 'install-session', 'remove-session', 'open', 'quit'):
        try:
            if args.command == 'desktop':
                from .desktop import desktop
                desktop(state, background=args.background)
            elif args.command in ('install-session', 'remove-session'):
                from .session_integration import install_session, remove_session
                result = (install_session if args.command == 'install-session' else remove_session)(state)
                print(json.dumps(result, default=str, indent=2))
            elif args.command == 'open':
                from .desktop import open_configuration
                open_configuration(state)
            else:
                from .desktop_ipc import request
                request(state, 'quit')
                print('Encerramento solicitado; a fila e os prazos serão preservados.')
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f'Files2GDrive: {error}\n')
        return
    if args.command == 'serve':
        from .web_runtime import serve
        try:
            serve(state, port=args.port, open_browser=not args.no_browser)
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f'Files2GDrive: {error}\n')
        return
    import fcntl
    with (state / 'engine.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('Another engine command is running; stop it before changing configuration')
        from .logging_setup import configure_logging
        logger = configure_logging(state / 'logs')
        db = Database(state / 'state.sqlite3')
        try:
            # Configuration/status remain usable offline and without Google dependencies.
            engine = Engine(db, None)
            for row in db.connection.execute('SELECT path FROM roots WHERE enabled=1'):
                if ((state.resolve().is_relative_to(Path(row['path'])) and db.get('directory_rules') is None)
                        or Path(row['path']).is_relative_to(state.resolve())):
                    raise ValueError('State directory and monitored roots cannot overlap')
            if args.command == 'configure-drive':
                if db.get('reconnect_pending') is not None:
                    raise ValueError('Confirm or cancel pending reconnect before changing Google configuration')
                from .auth import _client_config
                _client_config(args.client_file)
                folder = _display_name(args.folder_name)
                computer = _display_name(args.computer_name)
                if db.connection.execute('SELECT count(*) FROM files').fetchone()[0]:
                    if (db.get('backend', 'simulator') != 'drive'
                            or db.get('drive_folder_name') != folder
                            or db.get('computer_name') != computer):
                        raise ValueError('Use a separate state directory; backend/destination changes after backup are not supported yet')
                with db.connection:
                    for key, value in dict(backend='drive', oauth_client_file=str(args.client_file.resolve()),
                                           drive_folder_name=folder, computer_name=computer).items():
                        db.connection.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, value))
                print('Drive configured. Run login to authenticate; no files were uploaded.')
            elif args.command == 'login':
                _auth(db).login()
                print('Google authentication stored in the Ubuntu credential store.')
            elif args.command == 'logout':
                from .auth import KeyringTokenStore
                KeyringTokenStore(db.get('credential_store_id', db.get('computer_id'))).delete()
                print('Local Google credentials removed. Remote backups were not changed.')
            elif args.command in ('backups', 'backup-files', 'restore', 'reconnect-prepare', 'reconnect-review',
                                   'reconnect-confirm', 'reconnect-cancel'):
                _recovery_command(args, db, state)
            elif args.command == 'add':
                if (state.resolve().is_relative_to(args.path.resolve())
                        or args.path.resolve().is_relative_to(state.resolve())):
                    raise ValueError('Monitored roots and state directory cannot overlap')
                print(engine.add_root(args.path, _display_name(args.name) if args.name else None))
            elif args.command == 'remove':
                engine.remove_root(args.root_id)
            elif args.command == 'filters':
                engine.set_filters(args.patterns, args.root_id, args.ignore_case)
            elif args.command in ('pause', 'resume'):
                engine.pause(args.command == 'pause')
            elif args.command == 'logs':
                from .logging_setup import read_logs
                for event in read_logs(state / 'logs', level=args.level, root_id=args.root_id):
                    print(json.dumps(event))
            elif args.command == 'status':
                print(json.dumps(dict(backend=db.get('backend', 'simulator'), paused=engine.paused,
                    reconnect_pending=db.get('reconnect_pending') is not None,
                    roots=[dict(row) for row in db.connection.execute('SELECT * FROM roots')],
                    pending_jobs=db.connection.execute('SELECT count(*) FROM jobs').fetchone()[0]), indent=2))
            else:
                # Once files are observed, never silently change the backend for this state.
                if db.get('backend') is None:
                    db.set('backend', 'simulator')
                engine.remote = _remote(db, state)
                if args.command == 'tick':
                    engine.tick()
                else:
                    _run(engine, db)
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f'Files2GDrive: {error}\n')
        finally:
            remote = getattr(engine, 'remote', None)
            close = getattr(remote, 'close', None)
            if callable(close): close()
            db.close()
            from .logging_setup import JsonLogHandler
            for handler in logger.handlers[:]:
                if isinstance(handler, JsonLogHandler):
                    logger.removeHandler(handler)
                    handler.close()


def _run(engine, db):
    from .watcher import DirectoryWatcher
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    roots = [row[0] for row in db.connection.execute('SELECT path FROM roots WHERE enabled=1')]
    with DirectoryWatcher(roots) as watcher:
        print(f'backend={db.get("backend")}; watcher={watcher.backend}; Ctrl+C to stop', flush=True)
        while not stopped.is_set():
            engine.tick()
            for _ in range(10):
                if stopped.wait(0.5):
                    break
                if watcher.dirty.is_set():
                    watcher.dirty.clear()
                    break


def _recovery_command(args, db, state):
    from .reconnect import ReconnectManager
    if args.command == 'reconnect-cancel':
        ReconnectManager(db, None, state).cancel()
        print('Pending reconnection cancelled; remote files were not changed.')
        return
    if db.get('backend') != 'drive':
        raise ValueError('Recovery commands require an explicitly configured Google Drive backend')
    from .recovery_remote import DriveRecovery
    remote = _remote(db, state)
    recovery = DriveRecovery(remote.service)
    manager = ReconnectManager(db, recovery, state)
    if args.command == 'backups':
        result = recovery.list_backups()
    elif args.command == 'backup-files':
        result = recovery.list_files(args.computer_id, root_id=args.root_id, include_excluded=not args.active_only)
    elif args.command == 'restore':
        from .restore import RestoreManager
        destination = args.destination.absolute()
        resolved = destination.resolve()
        if resolved.is_relative_to(state.resolve()) or state.resolve().is_relative_to(resolved):
            raise ValueError('Restore destination and private state cannot overlap')
        for row in db.connection.execute('SELECT path FROM roots WHERE enabled=1'):
            monitored = Path(row['path'])
            if resolved.is_relative_to(monitored) or monitored.is_relative_to(resolved):
                raise ValueError('Choose a restore destination outside monitored roots')
        entries = recovery.list_files(args.computer_id, root_id=args.root_id, include_excluded=not args.active_only)
        if args.file_id:
            requested = set(args.file_id)
            entries = [entry for entry in entries if entry['file_id'] in requested]
            if {entry['file_id'] for entry in entries} != requested:
                raise ValueError('One or more selected file IDs are unavailable')
        if args.paths:
            for path in args.paths:
                if (not path or path.startswith('/') or '\x00' in path
                        or any(part in ('', '.', '..') for part in path.split('/'))):
                    raise ValueError('Select a safe relative backup path')
                if not any(entry['path'] == path or entry['path'].startswith(path + '/') for entry in entries):
                    raise ValueError('One or more selected paths are unavailable')
            entries = [entry for entry in entries if any(entry['path'] == path or entry['path'].startswith(path + '/')
                                                        for path in args.paths)]
        if not entries:
            raise ValueError('No backup files selected for restoration')
        restored = RestoreManager(recovery).restore(entries, destination)
        result = dict(destination=str(destination), restored=restored, monitoring_enabled=False)
    elif args.command == 'reconnect-prepare':
        mappings = {}
        for item in args.mappings:
            root_id, separator, path = item.partition('=')
            if not separator or not path or root_id in mappings:
                raise ValueError('Use unique --map ROOT_UUID=/absolute/local/path values')
            mappings[root_id] = path
        result = manager.prepare(args.computer_id, mappings)
    elif args.command == 'reconnect-review':
        result = manager.review()
    else:
        result = manager.confirm(args.review_id)
    print(json.dumps(result, indent=2, ensure_ascii=True))
