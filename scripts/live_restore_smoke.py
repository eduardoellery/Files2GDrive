"""Read-only Drive validation using only the previous generated smoke-test root.

Restores into a new local folder and exercises reviewed reconnection in a
throwaway local database. Never enables a worker or mutates remote files.
"""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from files2gdrive.cli import _remote
from files2gdrive.database import Database
from files2gdrive.reconnect import ReconnectManager
from files2gdrive.recovery_remote import DriveRecovery
from files2gdrive.restore import RestoreManager


def run(state, destination_parent):
    import fcntl
    with (state / 'engine.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = json.loads((state / 'live-smoke-report.json').read_text())
        if not previous.get('success'):
            raise RuntimeError('A successful generated upload test is required')
        db = Database(state / 'state.sqlite3')
        try:
            remote = _remote(db, state)
            recovery = DriveRecovery(remote.service)
            computer = db.get('computer_id')
            entries = recovery.list_files(computer, root_id=previous['root_id'])
            assert entries and all(entry['state'] == 'excluded' for entry in entries)
            destination_parent.mkdir(parents=True, exist_ok=True)
            destination = destination_parent / str(uuid4())
            paths = RestoreManager(recovery).restore(entries, destination)
            source = Path(previous['source'])
            for entry in entries:
                restored = destination / entry['path']
                original = source / entry['path']
                assert restored.read_bytes() == original.read_bytes()
                assert restored.stat().st_mode & 0o777 == entry['mode']
                assert restored.stat().st_mtime_ns == entry['mtime_ns']
            print('PASS: retained files restored, including hidden config, empty file and multi-chunk content', flush=True)
            print('PASS: bytes, permissions and modification times match generated originals', flush=True)
            try:
                RestoreManager(recovery).restore(entries, destination)
                raise AssertionError('Existing destination was accepted')
            except FileExistsError:
                pass
            print('PASS: existing destination refused without overwriting', flush=True)
            with TemporaryDirectory(prefix='files2gdrive-reconnect-') as temporary:
                fresh = Database(Path(temporary) / 'state/db')
                try:
                    manager = ReconnectManager(fresh, recovery, Path(temporary) / 'state')
                    plan = manager.prepare(computer, {previous['root_id']: str(destination)})
                    assert fresh.connection.execute('SELECT count(*) FROM roots').fetchone()[0] == 0
                    assert plan['counts']['cancel_retention'] == len(entries)
                    result = manager.confirm(plan['review_id'])
                    assert result['paused']
                    assert fresh.connection.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
                    assert fresh.get('computer_id') == computer
                finally:
                    fresh.close()
            print('PASS: fresh-database reconnection requires review and imports paused; no jobs executed', flush=True)
            report = dict(success=True, root_id=previous['root_id'], restored_paths=paths,
                          destination=str(destination), remote_mutations=False,
                          active_roots=db.connection.execute('SELECT count(*) FROM roots WHERE enabled=1').fetchone()[0])
            (state / 'live-restore-report.json').write_text(json.dumps(report, indent=2))
            print('PASS: no remote writes, no monitored roots enabled', flush=True)
        finally:
            db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--destination-parent', type=Path, required=True)
    args = parser.parse_args()
    run(args.state_dir.resolve(), args.destination_parent.resolve())
