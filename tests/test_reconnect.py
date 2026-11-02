import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from files2gdrive.database import Database
from files2gdrive.engine import Engine, RETENTION
from files2gdrive.reconnect import ReconnectManager


class Recovery:
    def __init__(self, entries): self.entries = entries
    def list_files(self, computer_id):
        return copy.deepcopy([entry for entry in self.entries if entry['computer_id'] == computer_id])


class ReconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'source'; self.root.mkdir()
        self.state = self.base / 'state'
        self.db = Database(self.state / 'db')
        self.old_computer = str(uuid4()); self.old_root = str(uuid4())
        path = self.root / '.settings'; path.write_bytes(b'config')
        path.chmod(0o640); os.utime(path, ns=(123456789, 123456789))
        self.entry = dict(version=1, file_id=str(uuid4()), computer_id=self.old_computer,
                          root_id=self.old_root, path='.settings', mode=0o640, mtime_ns=123456789,
                          size=6, md5Checksum=hashlib.md5(b'config', usedforsecurity=False).hexdigest(),
                          drive_id='remote-file', state='active', computer_name='Previous Ubuntu',
                          root_name='Configuration', folder_name='My Backup')
        self.remote = Recovery([self.entry])
        self.manager = ReconnectManager(self.db, self.remote, self.state, clock=lambda: 10000000)
    def tearDown(self): self.db.close(); self.temp.cleanup()
    def prepare(self): return self.manager.prepare(self.old_computer, {self.old_root: str(self.root)})

    def test_prepare_never_enables_roots_and_blocks_engine(self):
        result = self.prepare()
        self.assertEqual(result['counts']['unchanged'], 1)
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM roots').fetchone()[0], 0)
        engine = Engine(self.db, None)
        self.assertTrue(engine.paused)
        engine.tick()  # must not call any remote mutations
        with self.assertRaises(ValueError): engine.pause(False)
        with self.assertRaises(ValueError): engine.add_root(self.root)

    def test_confirm_imports_paused_and_preserves_credential_namespace(self):
        original = self.db.get('computer_id')
        plan = self.prepare(); result = self.manager.confirm(plan['review_id'])
        self.assertTrue(result['paused'])
        self.assertEqual(self.db.get('computer_id'), self.old_computer)
        self.assertEqual(self.db.get('credential_store_id'), original)
        self.assertEqual(self.db.get('computer_name'), 'Previous Ubuntu')
        self.assertEqual(self.db.get('drive_folder_name'), 'My Backup')
        self.assertIsNone(self.db.get('reconnect_pending'))
        file = self.db.connection.execute('SELECT * FROM files').fetchone()
        self.assertEqual(file['id'], self.entry['file_id'])
        self.assertIsNotNone(file['inode'])
        engine = Engine(self.db, None); engine.pause(False); engine.reconcile()
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_changed_local_content_with_same_metadata_is_uploaded_after_resume(self):
        path = self.root / '.settings'; path.write_bytes(b'CHANGED'[:6])
        os.utime(path, ns=(123456789, 123456789))
        plan = self.prepare(); self.assertEqual(plan['counts']['upload'], 1)
        self.manager.confirm(plan['review_id'])
        engine = Engine(self.db, None); engine.pause(False); engine.reconcile()
        self.assertEqual(self.db.connection.execute('SELECT operation FROM jobs').fetchone()[0], 'upload')

    def test_missing_active_file_only_starts_retention_after_explicit_resume(self):
        (self.root / '.settings').unlink()
        plan = self.prepare(); self.assertEqual(plan['counts']['start_retention'], 1)
        self.manager.confirm(plan['review_id'])
        engine = Engine(self.db, None, clock=lambda: 10000000); engine.tick()
        self.assertEqual(self.db.connection.execute('SELECT state FROM files').fetchone()[0], 'active')
        engine.pause(False); engine.reconcile()
        self.assertEqual(self.db.connection.execute('SELECT state,deleted_at FROM files').fetchone()[:], ('excluded', 10000000))

    def test_existing_retention_date_survives_import(self):
        (self.root / '.settings').unlink()
        self.remote.entries[0].update(state='excluded', deleted_at=1234)
        plan = self.prepare(); self.manager.confirm(plan['review_id'])
        self.assertEqual(self.db.connection.execute('SELECT deleted_at FROM files').fetchone()[0], 1234)

    def test_local_changes_invalidate_confirmation_and_review_refreshes(self):
        plan = self.prepare()
        (self.root / 'new-file').write_text('new')
        with self.assertRaisesRegex(ValueError, 'changed'): self.manager.confirm(plan['review_id'])
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM roots').fetchone()[0], 0)
        refreshed = self.manager.review()
        self.assertNotEqual(refreshed['review_id'], plan['review_id'])
        self.assertEqual(refreshed['counts']['new_local'], 1)
        self.manager.confirm(refreshed['review_id'])

    def test_remote_changes_invalidate_confirmation(self):
        plan = self.prepare(); self.remote.entries[0]['size'] = 99
        with self.assertRaisesRegex(ValueError, 'changed'): self.manager.confirm(plan['review_id'])

    def test_wrong_confirmation_id_cannot_import(self):
        self.prepare()
        with self.assertRaises(ValueError): self.manager.confirm('incorrect')
        self.assertNotEqual(self.db.get('computer_id'), self.old_computer)

    def test_overlapping_or_state_mappings_are_rejected(self):
        child = self.root / 'sub'; child.mkdir()
        other = str(uuid4())
        for maps in ({self.old_root: str(self.root), other: str(child)},
                     {self.old_root: str(self.state)}):
            with self.assertRaises(ValueError): self.manager.prepare(self.old_computer, maps)

    def test_nonfresh_state_cannot_be_reconnected(self):
        Engine(self.db, None).add_root(self.root)
        with self.assertRaises(ValueError): self.prepare()

    def test_ambiguous_historical_paths_refuse_import(self):
        old = dict(self.entry, file_id=str(uuid4()), drive_id='older', state='excluded', deleted_at=1234)
        self.remote.entries.append(old)
        with self.assertRaisesRegex(ValueError, 'multiple retained identities'): self.prepare()

    def test_cancel_does_not_adopt_remote_identity(self):
        previous = self.db.get('computer_id')
        self.prepare(); self.manager.cancel()
        self.assertIsNone(self.db.get('reconnect_pending'))
        self.assertEqual(self.db.get('computer_id'), previous)
        self.assertFalse(Engine(self.db, None).paused)

    def test_filters_cannot_silently_change_review(self):
        plan = self.prepare()
        with self.assertRaises(ValueError): Engine(self.db, None).set_filters(['*.txt'])
        self.manager.cancel()
        Engine(self.db, None).set_filters(['*.txt'])
        with self.assertRaisesRegex(ValueError, 'without filters'): self.prepare()

    def test_expired_retention_is_visible_in_review(self):
        (self.root / '.settings').unlink()
        self.remote.entries[0].update(state='excluded', deleted_at=1)
        self.assertEqual(self.prepare()['counts']['trash_expired'], 1)
