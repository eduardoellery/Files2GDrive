import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from files2gdrive.database import Database
from files2gdrive.drive import GoogleDriveRemote, DriveError, _key
from test_drive import Service, Request, HttpFailure


class ParallelDriveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'db')
        self.root_id = str(uuid4())
        self.db.connection.execute('INSERT INTO roots(id,path,name) VALUES(?,?,?)',
                                   (self.root_id, '/source', 'Source'))
        self.db.connection.commit()
        self.backend = Service()
        self.owner = threading.get_ident()
        self.clients = []
        self.barrier = None
        self.fail_index = None
        self.lose_index = None
        outer = self
        class Client:
            def __init__(self):
                self.thread = threading.get_ident()
                self.index = len(outer.clients)
                self.closed = False
                outer.clients.append(self)
            def files(self): return self
            def create(self, **kwargs): return self.request('create', kwargs)
            def update(self, **kwargs): return self.request('update', kwargs)
            def request(self, operation, kwargs):
                assert threading.get_ident() == self.thread != outer.owner
                request = getattr(outer.backend, operation)(**kwargs)
                def action():
                    if outer.barrier: outer.barrier.wait(timeout=3)
                    if self.index == outer.fail_index: raise HttpFailure(401)
                    result = request.execute()
                    if self.index == outer.lose_index:
                        outer.lose_index = None
                        raise HttpFailure(503)
                    return result
                return Request(action)
            def close(self): self.closed = True
        self.remote = GoogleDriveRemote(self.db, self.backend,
            media_factory=lambda stream, **kwargs: stream, service_factory=Client)
    def tearDown(self):
        self.remote.close()
        self.db.close()
        self.temp.cleanup()
    def items(self, count=4):
        result = []
        with self.db.connection:
            for index in range(count):
                file_id = str(uuid4()); path = f'folder/file-{file_id}.txt'
                self.db.connection.execute('INSERT INTO files(id,root_id,path,fingerprint,observed_at,device,inode) VALUES(?,?,?,?,?,?,?)',
                    (file_id,self.root_id,path,'[]',1,1,index+1))
                self.db.connection.execute("INSERT INTO jobs(file_id,operation) VALUES(?,'upload')",(file_id,))
                result.append(dict(file_id=file_id,stream=io.BytesIO(b'payload'),
                                   metadata=dict(root_id=self.root_id,path=path,mode=0o600,mtime_ns=1)))
        return result
    def test_four_transfers_overlap_with_isolated_reused_clients_and_owner_sqlite(self):
        self.barrier = threading.Barrier(4)
        observed = []
        remember = self.remote._remember
        def owner_remember(*args):
            observed.append(threading.get_ident())
            return remember(*args)
        with patch.object(self.remote, '_remember', side_effect=owner_remember):
            self.assertEqual(self.remote.upload_many(self.items()), [None]*4)
            self.assertEqual(self.remote.upload_many(self.items()), [None]*4)
        self.assertEqual(len(self.clients),4)
        self.assertEqual(set(observed),{self.owner})
        self.assertTrue(all(not client.closed for client in self.clients))
        self.remote.close()
        self.assertTrue(all(client.closed for client in self.clients))
    def test_partial_auth_failure_preserves_successful_confirmations(self):
        self.barrier = threading.Barrier(4); self.fail_index = 0
        items = self.items(); result = self.remote.upload_many(items)
        self.assertEqual(sum(error is None for error in result),3)
        errors = [error for error in result if error is not None]
        self.assertTrue(errors[0].requires_auth)
        for item,error in zip(items,result):
            key = _key('file:'+self.db.get('computer_id')+':'+item['file_id'])
            confirmed = self.db.connection.execute('SELECT confirmed FROM drive_objects WHERE object_key=?',(key,)).fetchone()[0]
            self.assertEqual(confirmed,int(error is None))
        self.assertFalse(self.remote._folder_cache)
    def test_lost_response_retries_same_id_after_concurrent_batch(self):
        self.barrier = threading.Barrier(4); self.lose_index = 0
        items = self.items(); result = self.remote.upload_many(items)
        failed = next(item for item,error in zip(items,result) if error is not None)
        key = _key('file:'+self.db.get('computer_id')+':'+failed['file_id'])
        before = self.db.connection.execute('SELECT drive_id FROM drive_objects WHERE object_key=?',(key,)).fetchone()[0]
        creates = len(self.backend.creates)
        self.barrier = None
        self.assertEqual(self.remote.upload_many([failed]),[None])
        after = self.db.connection.execute('SELECT drive_id FROM drive_objects WHERE object_key=?',(key,)).fetchone()[0]
        self.assertEqual(before,after);self.assertEqual(len(self.backend.creates),creates)
    def test_preparation_auth_failure_starts_no_transfer(self):
        error = DriveError('authentication required',requires_auth=True)
        with patch.object(self.remote,'_prepare_upload',side_effect=error):
            result = self.remote.upload_many(self.items())
        self.assertTrue(all(item.requires_auth for item in result))
        self.assertEqual(self.clients,[])
    def test_fallback_and_input_boundaries(self):
        self.remote.service_factory = None
        self.assertEqual(self.remote.upload_many(self.items(2)),[None,None])
        self.assertEqual(self.remote.upload_many([]),[])
        with self.assertRaises(ValueError):self.remote.upload_many(self.items(5))
        item = self.items(1)[0]
        with self.assertRaises(ValueError):self.remote.upload_many([item,item])
