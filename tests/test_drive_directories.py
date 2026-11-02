from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4
from unittest.mock import patch

from files2gdrive.database import Database
from files2gdrive.drive import GoogleDriveRemote, DriveError, _key
from files2gdrive.drive_directories import prepare_directories
from test_drive import Service, HttpFailure


class Batch:
    def __init__(self, service):
        self.service = service
        self.items = []
    def add(self, request, callback, request_id):
        self.items.append((request, callback, request_id))
    def execute(self):
        self.service.batch_sizes.append(len(self.items))
        for request, callback, key in reversed(self.items):
            try:
                response = request.execute()
            except Exception as error:
                callback(key, None, error)
            else:
                callback(key, response, None)


class BatchService(Service):
    def __init__(self):
        super().__init__()
        self.batch_sizes = []
        self.fail_folder = None
        self.lose_folder = None
    def new_batch_http_request(self):
        return Batch(self)
    def create(self, body, **kwargs):
        request = super().create(body, **kwargs)
        original = request.call
        def create():
            if body['name'] == self.fail_folder:
                raise HttpFailure(503)
            if body.get('parents'):
                assert body['parents'][0] in self.items, 'Parent must exist before child'
            response = original()
            if body['name'] == self.lose_folder:
                self.lose_folder = None
                raise HttpFailure(503)
            return response
        request.call = create
        return request


class DirectoryBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name)/'state'); self.addCleanup(self.db.close)
        self.root = str(uuid4())
        self.db.connection.execute('INSERT INTO roots(id,path,name) VALUES(?,?,?)',(self.root,'/source','Source'))
        self.db.connection.commit()
        self.service = BatchService()
        self.remote = GoogleDriveRemote(self.db,self.service)
    def metadata(self,path):
        return dict(root_id=self.root,path=path,mode=0o644,mtime_ns=1)

    def test_dependencies_batched_and_cache_prevents_extra_requests(self):
        prepare_directories(self.remote,[self.metadata('a/sub/file'),self.metadata('b/sub/file')])
        self.assertEqual(len(self.service.creates),7)
        self.assertIn(7,self.service.batch_sizes)  # One lookup wave for all identities.
        self.assertEqual(self.service.batch_sizes[-2:],[2,2])
        batches=len(self.service.batch_sizes)
        prepare_directories(self.remote,[self.metadata('a/sub/other')])
        self.assertEqual(len(self.service.batch_sizes),batches)
        self.remote._folder_cache.clear()
        prepare_directories(self.remote,[self.metadata('a/sub/other')])
        self.assertEqual(self.service.batch_sizes[-1],5)  # Existing IDs GET in one batch.
        self.assertEqual(len(self.service.creates),7)

    def test_batches_are_bounded_and_all_sql_stays_on_owner(self):
        owner=threading.get_ident()
        remember=self.remote._remember
        def check(*args):
            self.assertEqual(threading.get_ident(),owner)
            return remember(*args)
        with patch.object(self.remote,'_remember',side_effect=check):
            prepare_directories(self.remote,[self.metadata(f'dir{i}/file') for i in range(205)])
        self.assertEqual(len(self.service.creates),208)
        self.assertTrue(all(size<=100 for size in self.service.batch_sizes))

    def test_lost_folder_create_response_reuses_original_id(self):
        self.service.lose_folder='a'
        with self.assertRaises(DriveError):prepare_directories(self.remote,[self.metadata('a/file')])
        key=_key(f'directory:{self.remote.computer_id}:{self.root}:a')
        row=self.db.connection.execute('SELECT * FROM drive_objects WHERE object_key=?',(key,)).fetchone()
        original=row['drive_id']; self.assertEqual(row['confirmed'],0)
        count=len(self.service.creates)
        prepare_directories(self.remote,[self.metadata('a/file')])
        self.assertEqual(len(self.service.creates),count)
        self.assertEqual(self.remote._folder_cache[key][0],original)

    def test_partial_create_failure_preserves_successful_sibling(self):
        self.service.fail_folder='b'
        with self.assertRaises(DriveError):
            prepare_directories(self.remote,[self.metadata('a/file'),self.metadata('b/file')])
        self.assertEqual(self.remote._folder_cache,{})
        a=next(item['id'] for item in self.service.items.values() if item['name']=='a')
        self.service.fail_folder=None
        prepare_directories(self.remote,[self.metadata('a/file'),self.metadata('b/file')])
        self.assertIn(a,self.service.items)
        self.assertEqual(sum(item['name']=='a' for item in self.service.items.values()),1)

    def test_invalid_identity_and_duplicates_rejected(self):
        prepare_directories(self.remote,[self.metadata('a/file')]);self.remote._folder_cache.clear()
        item=next(item for item in self.service.items.values() if item['name']=='a')
        item['appProperties']['f2g_key']='wrong'
        with self.assertRaisesRegex(DriveError,'identity'):prepare_directories(self.remote,[self.metadata('a/file')])

    def test_uncertain_absent_folder_retries_reserved_id(self):
        self.service.fail_folder='a'
        with self.assertRaises(DriveError):prepare_directories(self.remote,[self.metadata('a/file')])
        key=_key(f'directory:{self.remote.computer_id}:{self.root}:a')
        original=self.db.connection.execute('SELECT drive_id FROM drive_objects WHERE object_key=?',(key,)).fetchone()[0]
        self.assertNotIn(original,self.service.items)
        self.service.fail_folder=None
        prepare_directories(self.remote,[self.metadata('a/file')])
        self.assertEqual(self.remote._folder_cache[key][0],original)

    def test_paginated_duplicate_directory_identity_rejected(self):
        # An ambiguous identity must never be resolved by picking the first page.
        self.service.pages=[{'files':[{'id':'one'}],'nextPageToken':'1'}, {'files':[{'id':'two'}]}]
        with self.assertRaisesRegex(DriveError,'duplicate'):
            prepare_directories(self.remote,[self.metadata('a/file')])
        self.assertEqual(self.service.creates,[])

    def test_legacy_service_falls_back(self):
        service=Service();remote=GoogleDriveRemote(self.db,service)
        prepare_directories(remote,[self.metadata('a/file')])
        self.assertEqual(len(service.creates),4)

    def test_unknown_root_and_unsafe_path_rejected_before_batch(self):
        for metadata in (self.metadata('../file'),dict(self.metadata('file'),root_id='missing')):
            with self.assertRaises(ValueError):prepare_directories(self.remote,[metadata])
        self.assertEqual(self.service.batch_sizes,[])
