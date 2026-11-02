import base64
import json
from unittest.mock import patch
from files2gdrive.scanner import ScanResult, FileSnapshot
from pathlib import Path
import tempfile
import unittest

from files2gdrive.database import Database
from files2gdrive.engine import Engine, RETENTION
from files2gdrive.remote import SimulatedRemote


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'source'; self.root.mkdir()
        self.db = Database(self.base / 'state/db')
        self.remote = SimulatedRemote(self.base / 'remote')
        self.now = 1000
        self.engine = Engine(self.db, self.remote, clock=lambda: self.now, settle_seconds=3)
        self.root_id = self.engine.add_root(self.root)

    def tearDown(self):
        self.db.close(); self.temp.cleanup()

    def file(self):
        return self.db.connection.execute('SELECT * FROM files LIMIT 1').fetchone()

    def upload(self, name='file.txt'):
        path = self.root / name; path.write_text('original')
        self.engine.tick(); self.now += 4; self.engine.tick()
        return path, self.file()['id']

    def test_settling_idempotence_and_update(self):
        path = self.root / '.config'; path.write_text('a')
        self.engine.tick()
        file_id = self.file()['id']
        self.assertIsNone(self.remote.read(file_id))
        path.write_text('ab'); self.now += 4; self.engine.tick()
        self.assertIsNone(self.remote.read(file_id))
        self.now += 4; self.engine.tick(); self.engine.tick()
        self.assertEqual(self.remote.read(file_id)['state'], 'active')
        self.assertEqual(len(list(self.remote.directory.glob('*.json'))), 1)
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_delete_retention_and_trash(self):
        path, file_id = self.upload(); path.unlink(); self.engine.tick()
        self.assertEqual(self.remote.read(file_id)['state'], 'excluded')
        self.now += RETENTION - 1; self.engine.tick()
        self.assertEqual(self.remote.read(file_id)['state'], 'excluded')
        self.now += 1; self.engine.tick()
        self.assertEqual(self.remote.read(file_id)['state'], 'trashed')

    def test_reappearance_cancels_retention(self):
        path, file_id = self.upload(); path.unlink(); self.engine.tick()
        path.write_text('back'); self.engine.tick(); self.now += 4; self.engine.tick()
        self.assertEqual(self.remote.read(file_id)['state'], 'active')
        self.assertIsNone(self.file()['deleted_at'])

    def test_missing_root_does_not_delete_or_clean(self):
        path, file_id = self.upload()
        moved = self.base / 'unavailable'; self.root.rename(moved)
        self.now += RETENTION; self.engine.tick()
        self.assertEqual(self.file()['state'], 'active')
        self.assertEqual(self.remote.read(file_id)['state'], 'active')
        self.assertEqual(self.db.connection.execute('SELECT status FROM roots').fetchone()[0], 'unavailable')

    def test_pause_persists_and_suspends_expiration(self):
        path, file_id = self.upload(); path.unlink(); self.engine.tick()
        self.engine.pause(); self.db.close()
        self.db = Database(self.base / 'state/db')
        self.engine = Engine(self.db, self.remote, clock=lambda: self.now)
        self.assertTrue(self.engine.paused)
        self.now += RETENTION; self.engine.tick()
        self.assertEqual(self.remote.read(file_id)['state'], 'excluded')
        self.engine.pause(False); self.engine.tick()
        self.assertEqual(self.remote.read(file_id)['state'], 'trashed')

    def test_remove_and_reregister_preserves_absent_deadline(self):
        path, file_id = self.upload(); path.unlink(); self.engine.tick()
        deleted = self.file()['deleted_at']; self.now += 99
        self.engine.remove_root(self.root_id); self.engine.tick()
        self.assertEqual(self.file()['deleted_at'], deleted)
        self.assertEqual(self.engine.add_root(self.root), self.root_id)
        self.engine.tick(); self.assertEqual(self.file()['deleted_at'], deleted)
        path.write_text('restored'); self.engine.tick(); self.now += 4; self.engine.tick()
        self.assertEqual(self.remote.read(file_id)['state'], 'active')

    def test_filters_and_reversal(self):
        path, file_id = self.upload('file.tmp')
        self.engine.set_filters(['*.tmp']); self.engine.tick()
        self.assertEqual(self.remote.read(file_id)['state'], 'excluded')
        self.engine.set_filters([]); self.engine.tick(); self.now += 4; self.engine.tick()
        self.assertEqual(self.remote.read(file_id)['state'], 'active')

    def test_queue_survives_restart(self):
        (self.root / 'file').write_text('a'); self.engine.tick()
        file_id = self.file()['id']; self.db.close()
        self.db = Database(self.base / 'state/db')
        self.engine = Engine(self.db, self.remote, clock=lambda: self.now)
        self.now += 4; self.engine.tick()
        self.assertEqual(self.remote.read(file_id)['state'], 'active')

    def test_same_root_move_preserves_identity(self):
        path, file_id = self.upload(); path.rename(self.root / 'renamed')
        self.engine.tick(); self.now += 4; self.engine.tick()
        rows = self.db.connection.execute('SELECT * FROM files').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], file_id)
        self.assertEqual(self.remote.read(file_id)['path'], 'renamed')

    def test_remote_deletion_not_recreated_without_local_change(self):
        path, file_id = self.upload(); self.remote.trash(file_id)
        self.engine.tick(); self.assertEqual(self.remote.read(file_id)['state'], 'trashed')
        path.write_text('changed'); self.engine.tick(); self.now += 4; self.engine.tick()
        self.assertEqual(self.remote.read(file_id)['state'], 'active')

    def test_overlaps_rejected(self):
        child = self.root / 'child'; child.mkdir()
        with self.assertRaises(ValueError): self.engine.add_root(child)
        with self.assertRaises(ValueError): self.engine.add_root(self.base)

    def test_failure_keeps_queue_and_retries(self):
        class Failing:
            def upload(self, *args): raise OSError('offline')
        path = self.root / 'file'; path.write_text('a'); self.engine.tick()
        self.now += 4; self.engine.remote = Failing(); self.engine.tick()
        self.assertEqual(self.db.connection.execute('SELECT attempts FROM jobs').fetchone()[0], 1)
        self.engine.remote = self.remote; self.now += 3; self.engine.tick()
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_mutation_during_snapshot_never_publishes(self):
        path = self.root / 'file'; path.write_text('original')
        self.engine.reconcile(); self.now += 4
        def unstable(source, destination, length):
            destination.write(source.read())
            path.write_text('changed during snapshot')
        with patch('files2gdrive.engine.shutil.copyfileobj', side_effect=unstable):
            self.engine.process()
        self.assertIsNone(self.remote.read(self.file()['id']))
        self.assertEqual(self.db.connection.execute('SELECT attempts FROM jobs').fetchone()[0], 1)

    def test_remote_upload_reads_stable_snapshot(self):
        path = self.root / 'file'; path.write_text('original')
        self.engine.reconcile(); self.now += 4
        remote = self.remote
        class EditingRemote:
            def upload(self, file_id, source, metadata):
                path.write_text('new version while transferring')
                remote.upload(file_id, source, metadata)
        self.engine.remote = EditingRemote()
        self.engine.process()
        self.assertEqual(base64.b64decode(remote.read(self.file()['id'])['content']), b'original')
        self.engine.remote = remote; self.engine.tick(); self.now += 4; self.engine.tick()
        self.assertEqual(base64.b64decode(remote.read(self.file()['id'])['content']), b'new version while transferring')

    def test_reused_inode_with_different_fingerprint_is_not_move(self):
        path, file_id = self.upload()
        previous = self.file()
        snapshot = FileSnapshot(size=999, mtime_ns=789, mode=0o600,
                                device=previous['device'], inode=previous['inode'])
        with patch('files2gdrive.engine.scan_root', return_value=ScanResult(files={'other': snapshot})):
            self.engine.reconcile()
        rows = self.db.connection.execute('SELECT * FROM files').fetchall()
        self.assertEqual(len(rows), 2)
        old = next(row for row in rows if row['id'] == file_id)
        self.assertEqual(old['state'], 'excluded')

    def test_auth_failure_preserves_queue_and_manual_pause_setting(self):
        from files2gdrive.auth import AuthenticationError
        from files2gdrive.drive import DriveError
        class Expired:
            def upload(self, *args): raise DriveError('renew authorization', requires_auth=True)
        (self.root / 'file').write_text('content'); self.engine.reconcile(); self.now += 4
        self.engine.remote = Expired()
        with self.assertRaises(AuthenticationError): self.engine.process()
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
        self.assertFalse(self.engine.paused)

    def test_batch_limit_skips_unsettled_head_and_counts_remote_attempts(self):
        (self.root / 'first').write_text('first')
        (self.root / 'second').write_text('second')
        self.engine.reconcile(); self.now += 4
        rows = self.db.connection.execute('SELECT j.file_id,f.path FROM jobs j JOIN files f ON f.id=j.file_id').fetchall()
        head, ready = rows
        with self.db.connection:
            self.db.connection.execute('UPDATE files SET observed_at=? WHERE id=?', (self.now, head['file_id']))
        self.assertEqual(self.engine.process(max_jobs=1), 1)
        self.assertIsNone(self.remote.read(head['file_id']))
        self.assertEqual(self.remote.read(ready['file_id'])['state'], 'active')
        self.assertEqual(self.engine.process(max_jobs=1), 0)
        self.now += 4
        self.assertEqual(self.engine.process(max_jobs=1), 1)
        self.assertEqual(self.engine.process(max_jobs=1), 0)
        self.engine.pause()
        self.assertEqual(self.engine.process(max_jobs=1), 0)

    def test_unavailable_root_head_does_not_starve_ready_root(self):
        (self.root / 'blocked').write_text('blocked')
        self.engine.reconcile()
        other = self.base / 'other'; other.mkdir(); (other / 'ready').write_text('ready')
        other_id = self.engine.add_root(other)
        self.engine.reconcile(); self.now += 4
        with self.db.connection:
            self.db.connection.execute("UPDATE roots SET status='unavailable' WHERE id=?", (self.root_id,))
        self.assertEqual(self.engine.process(max_jobs=1), 1)
        rows = self.db.connection.execute('SELECT * FROM files').fetchall()
        for row in rows:
            self.assertEqual(self.remote.read(row['id']) is not None, row['root_id'] == other_id)
        self.assertEqual(self.engine.process(max_jobs=1), 0)

    def test_legacy_parent_add_does_not_skip_a_previously_removed_child(self):
        self.engine.remove_root(self.root_id)
        parent = self.root / 'Parent'
        child = parent / 'Child'
        child.mkdir(parents=True)
        (child / 'important.txt').write_text('must still be backed up')
        child_id = self.engine.add_root(child)
        self.engine.tick(); self.now += 4; self.engine.tick()
        old = self.db.connection.execute('SELECT * FROM files WHERE root_id=?', (child_id,)).fetchone()
        self.engine.remove_root(child_id); self.engine.process()
        parent_id = self.engine.add_root(parent)
        self.assertIsNone(self.db.get('directory_rules'))
        self.engine.tick(); self.now += 4; self.engine.tick()
        new = self.db.connection.execute('SELECT * FROM files WHERE root_id=?', (parent_id,)).fetchone()
        self.assertIsNotNone(new)
        self.assertEqual(new['path'], 'Child/important.txt')
        self.assertEqual(self.remote.read(new['id'])['state'], 'active')
        self.assertEqual(self.remote.read(old['id'])['state'], 'excluded')


if __name__ == '__main__': unittest.main()
