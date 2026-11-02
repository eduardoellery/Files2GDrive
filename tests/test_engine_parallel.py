import json
from pathlib import Path
import tempfile
import threading
import unittest

from files2gdrive.auth import AuthenticationError
from files2gdrive.database import Database
from files2gdrive.drive import DriveError
from files2gdrive.engine import Engine
from files2gdrive.remote import SimulatedRemote


class ParallelRemote(SimulatedRemote):
    def __init__(self, directory):
        super().__init__(directory)
        self.batches = []
        self.streams = []
        self.failures = {}
        self.before = None
        self.owner = threading.get_ident()

    def upload_many(self, items):
        assert threading.get_ident() == self.owner
        self.batches.append([item['file_id'] for item in items])
        self.streams.extend(item['stream'] for item in items)
        if self.before:
            self.before(items)
        results = []
        for item in items:
            error = self.failures.get(item['metadata']['path'])
            if error:
                results.append(error)
            else:
                self.upload(item['file_id'], item['stream'], item['metadata'])
                results.append(None)
        return results


class EngineParallelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'source'; self.root.mkdir()
        self.db = Database(self.base / 'state/db')
        self.remote = ParallelRemote(self.base / 'remote')
        self.now = 1000
        self.engine = Engine(self.db, self.remote, clock=lambda:self.now, settle_seconds=3)
        self.root_id = self.engine.add_root(self.root)

    def tearDown(self):
        self.db.close(); self.temp.cleanup()

    def prepare(self, count=6):
        for index in range(count):
            (self.root / str(index)).write_text('original' + str(index))
        self.engine.reconcile(); self.now += 4

    def queued(self):
        return [dict(row) for row in self.db.connection.execute('SELECT j.*,f.path FROM jobs j JOIN files f ON f.id=j.file_id')]

    def test_four_snapshots_max_and_limit_and_cleanup(self):
        self.prepare()
        self.assertEqual(self.engine.process(max_jobs=5), 5)
        self.assertEqual([len(batch) for batch in self.remote.batches], [4,1])
        self.assertEqual(len(self.queued()), 1)
        self.assertTrue(all(stream.closed for stream in self.remote.streams))
        self.assertEqual(self.engine.process(), 1)
        self.assertEqual(len(self.queued()), 0)

    def test_mixed_results_commit_success_before_auth_stops_next_batch(self):
        self.prepare(8)
        first_paths = []
        def fail_first(items):
            first_paths.extend(item['metadata']['path'] for item in items)
            self.remote.failures[first_paths[0]] = DriveError('authenticate', requires_auth=True, category='auth')
            self.remote.failures[first_paths[1]] = DriveError('rate limit', category='unknown')
        self.remote.before = fail_first
        with self.assertRaises(AuthenticationError):
            self.engine.process()
        self.assertEqual([len(batch) for batch in self.remote.batches], [4])
        jobs = {row['path']:row for row in self.queued()}
        self.assertNotIn(first_paths[2], jobs); self.assertNotIn(first_paths[3], jobs)
        self.assertEqual(jobs[first_paths[0]]['attempts'], 1)
        self.assertEqual(jobs[first_paths[1]]['attempts'], 1)
        self.assertTrue(all(job['attempts']==0 for path,job in jobs.items() if path not in first_paths))
        self.assertTrue(all(stream.closed for stream in self.remote.streams))

    def test_pause_and_pending_reconnect_prevent_dispatch(self):
        self.prepare()
        self.engine.pause()
        self.assertEqual(self.engine.process(), 0)
        self.engine.pause(False)
        self.db.set('reconnect_pending', '{}')
        self.assertEqual(self.engine.process(), 0)
        self.assertEqual(self.remote.batches, [])

    def test_changed_or_unsettled_files_do_not_consume_batch_slots(self):
        self.prepare()
        (self.root / '0').write_text('different')
        self.db.connection.execute('UPDATE files SET observed_at=? WHERE path=?', (self.now,'1'))
        self.db.connection.commit()
        self.assertEqual(self.engine.process(max_jobs=4), 4)
        self.assertEqual([len(batch) for batch in self.remote.batches], [4])
        self.assertEqual({row['path'] for row in self.queued()}, {'0','1'})

    def test_snapshots_do_not_follow_live_source_mutations(self):
        self.prepare(4)
        def mutate(items):
            for item in items:
                (self.root / item['metadata']['path']).write_text('new-content')
        self.remote.before = mutate
        self.engine.process()
        for item_id in self.remote.batches[0]:
            item = self.remote.read(item_id)
            import base64
            self.assertTrue(base64.b64decode(item['content']).startswith(b'original'))

    def test_retention_remains_sequential_and_does_not_reupload(self):
        self.prepare(4); self.engine.process()
        for file in self.root.iterdir(): file.unlink()
        self.engine.reconcile()
        self.assertEqual(self.engine.process(max_jobs=4), 4)
        self.assertEqual(len(self.remote.batches), 1)
        self.assertEqual(self.queued(), [])
        self.assertTrue(all(self.remote.read(item_id)['state']=='excluded' for item_id in self.remote.batches[0]))
