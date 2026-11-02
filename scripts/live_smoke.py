"""Opt-in live Drive test. Only creates/modifies its own generated test files.

Leaves remote test files in Excluídos with the normal 30-day retention; never
permanently deletes files or accelerates the retention clock.
"""
import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from files2gdrive.cli import _remote
from files2gdrive.database import Database
from files2gdrive.drive import _key
from files2gdrive.engine import Engine
from files2gdrive.logging_setup import configure_logging


def run(state, source_parent):
    import fcntl
    with (state / 'engine.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = Database(state / 'state.sqlite3')
        root_id = None
        report = {'checks': [], 'source': None, 'root_id': None, 'success': False}
        try:
            if db.get('backend') != 'drive':
                raise RuntimeError('This test requires explicitly configured Drive state')
            if db.get('paused', '0') == '1':
                raise RuntimeError('Resume the configured state before running this test')
            if db.connection.execute('SELECT count(*) FROM roots WHERE enabled=1').fetchone()[0]:
                raise RuntimeError('Live smoke requires no other monitored roots')
            remote = _remote(db, state)
            configure_logging(state / 'logs')
            source = source_parent / str(uuid4())
            source.mkdir(parents=True)
            report['source'] = str(source)
            (source / '.config').mkdir()
            text = source / '.config/settings.txt'; text.write_text('Files2GDrive generated test v1\n')
            text.chmod(0o640)
            (source / 'empty.txt').touch()
            (source / 'chunked.bin').write_bytes(b'F2G-test-block\x00' * 650000)  # >8 MiB, no personal data
            engine = Engine(db, remote, settle_seconds=0)
            root_id = engine.add_root(source, 'Validacao Files2GDrive ' + source.name[:8])
            report['root_id'] = root_id

            def check(label):
                report['checks'].append(label)
                print(label, flush=True)

            def tick():
                engine.tick()
                pending = db.connection.execute('SELECT operation,error FROM jobs').fetchall()
                if pending:
                    raise RuntimeError('Test left pending operations: ' + json.dumps([dict(row) for row in pending]))

            def local(relative):
                return db.connection.execute('SELECT * FROM files WHERE root_id=? AND path=?', (root_id, relative)).fetchone()

            def item(row):
                value, _ = remote._find(_key('file:' + remote.computer_id + ':' + row['id']))
                if not value:
                    raise AssertionError('Expected test file on Drive')
                return value

            def verify_content(row, path):
                value = item(row)
                details = remote.service.files().get(fileId=value['id'], fields='id,size,md5Checksum,description').execute()
                expected = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
                assert details['md5Checksum'] == expected, 'Remote checksum differs'
                metadata = json.loads(details['description'])
                assert metadata['mode'] == (path.stat().st_mode & 0o777)
                assert metadata['mtime_ns'] == path.stat().st_mtime_ns
                return value

            tick()
            for relative in ('.config/settings.txt', 'empty.txt', 'chunked.bin'):
                verify_content(local(relative), source / relative)
            check('PASS: initial upload, hidden config, empty file, >8 MiB file, checksums and Linux metadata')
            original = item(local('.config/settings.txt'))['id']
            text.write_text('Files2GDrive generated test v2\n'); tick()
            assert verify_content(local('.config/settings.txt'), text)['id'] == original
            check('PASS: content update preserves Drive ID')
            renamed = source / '.config/renamed.txt'; text.rename(renamed); tick()
            assert verify_content(local('.config/renamed.txt'), renamed)['id'] == original
            check('PASS: rename preserves Drive ID and content')
            row = local('.config/renamed.txt'); renamed.unlink(); tick()
            retained = item(row)
            metadata = json.loads(retained['description'])
            assert metadata['state'] == 'excluded' and not retained.get('trashed')
            assert metadata['deleted_at'] is not None
            check('PASS: local deletion retains remote file in Excluídos, outside trash')
            renamed.write_text('Files2GDrive generated restored config\n'); tick()
            assert verify_content(local('.config/renamed.txt'), renamed)['id'] == original
            assert json.loads(item(local('.config/renamed.txt'))['description'])['state'] == 'active'
            check('PASS: reappearance restores file and cancels retention')
            engine.pause(); renamed.write_text('Files2GDrive generated paused change\n')
            before = item(local('.config/renamed.txt'))['description']; engine.tick()
            assert item(local('.config/renamed.txt'))['description'] == before
            engine.pause(False); tick(); verify_content(local('.config/renamed.txt'), renamed)
            check('PASS: pause and resume')
            report['functional_checks_passed'] = True
        finally:
            if root_id:
                engine.pause(False)
                engine.remove_root(root_id)
                engine.process()
                report['test_root_disabled'] = True
                report['pending_jobs'] = db.connection.execute('SELECT count(*) FROM jobs').fetchone()[0]
            report['success'] = bool(report.get('functional_checks_passed') and report.get('pending_jobs') == 0)
            (state / 'live-smoke-report.json').write_text(json.dumps(report, indent=2))
            db.close()
        if not report['success']:
            raise RuntimeError('Live validation incomplete; consult the private test report')
        print('PASS: test root disabled; generated backups retained for 30 days, no permanent deletions', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--source-parent', type=Path, required=True)
    args = parser.parse_args()
    run(args.state_dir.resolve(), args.source_parent.resolve())
