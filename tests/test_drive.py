import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import Mock, patch

from files2gdrive.database import Database
from files2gdrive.drive import GoogleDriveRemote, DriveError, FOLDER, _key


class HttpFailure(Exception):
    def __init__(self, status):
        self.resp = type('Response', (), {'status': status})()
        super().__init__('SECRET TOKEN AND PRIVATE RESPONSE')


class Request:
    def __init__(self, call): self.call = call
    def execute(self, **kwargs): return self.call()
    def next_chunk(self, **kwargs): return None, self.call()


class Service:
    def __init__(self):
        self.items = {}; self.sequence = 0; self.creates = []; self.account = 'account-one'
        self.fail_upload = False; self.lost_create_response = False; self.pages = None
    def files(self): return self
    def about(self): return self
    def get(self, fileId=None, **kwargs):
        if fileId is None:
            return Request(lambda: {'user': {'permissionId': self.account}})
        def action():
            if fileId not in self.items: raise HttpFailure(404)
            return dict(self.items[fileId])
        return Request(action)
    def generateIds(self, **kwargs):
        ids = [f'drive-{value}' for value in range(self.sequence + 1, self.sequence + kwargs.get('count', 1) + 1)]
        self.sequence += len(ids)
        return Request(lambda: {'ids': ids})
    def list(self, q, pageToken=None, **kwargs):
        def action():
            if self.pages is not None:
                return self.pages[int(pageToken or 0)]
            key = re.search("value='([^']+)'", q)[1]
            return {'files': [dict(item) for item in self.items.values()
                              if not item.get('trashed') and item.get('appProperties', {}).get('f2g_key') == key]}
        return Request(action)
    def create(self, body, media_body=None, **kwargs):
        def action():
            if self.fail_upload and media_body: raise HttpFailure(503)
            file_id = body['id']
            if file_id in self.items: raise HttpFailure(409)
            self.creates.append(file_id)
            self.items[file_id] = dict(body, trashed=False)
            if media_body: self.items[file_id]['bytes'] = media_body.read()
            if self.lost_create_response and media_body:
                self.lost_create_response = False
                raise HttpFailure(503)
            return {'id': file_id}
        return Request(action)
    def update(self, fileId, body, media_body=None, addParents=None, removeParents=None, **kwargs):
        def action():
            if self.fail_upload and media_body: raise HttpFailure(503)
            item = self.items[fileId]; item.update(body)
            if addParents: item['parents'] = [addParents]
            if media_body: item['bytes'] = media_body.read()
            return {'id': fileId}
        return Request(action)


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'db')
        self.root_id = str(uuid4()); self.file_id = str(uuid4())
        with self.db.connection:
            self.db.connection.execute('INSERT INTO roots(id,path,name) VALUES(?,?,?)',
                                       (self.root_id, '/test/root', 'Config'))
        self.service = Service()
        self.media_options = []
        def media(stream, **options):
            self.media_options.append(options)
            return stream
        self.remote = GoogleDriveRemote(self.db, self.service, computer_name='Ubuntu', media_factory=media)
        self.metadata = dict(root_id=self.root_id, path='.config/app/settings.json', mode=0o640, mtime_ns=12345)
    def tearDown(self): self.db.close(); self.temp.cleanup()
    def item(self):
        key = _key('file:' + self.db.get('computer_id') + ':' + self.file_id)
        return next(item for item in self.service.items.values() if item.get('appProperties', {}).get('f2g_key') == key and not item.get('trashed'))
    def upload(self):
        stream = io.BytesIO(b'configuration')
        self.remote.upload(self.file_id, stream, self.metadata)
        self.assertFalse(stream.closed)
        return self.item()
    def test_small_upload_uses_execute_and_reports_initial_and_final_progress(self):
        progress = []
        self.remote.progress_callback = progress.append
        with patch.object(Request, 'next_chunk', side_effect=AssertionError('Unexpected resumable upload')):
            self.upload()
        self.assertEqual([item['bytes'] for item in progress], [0, len(b'configuration')])
        self.assertEqual([item['total'] for item in progress], [len(b'configuration')] * 2)
        self.assertFalse(self.media_options[-1]['resumable'])

    def test_upload_mode_boundary_preserves_chunking_for_large_files(self):
        from files2gdrive.drive import MULTIPART_UPLOAD_LIMIT
        original_create = self.service.create
        requests = []
        def create(**kwargs):
            request = original_create(**kwargs)
            if kwargs.get('media_body') is not None:
                request = Mock(wraps=request)
                requests.append(request)
            return request
        self.service.create = create
        for size in (0, MULTIPART_UPLOAD_LIMIT, MULTIPART_UPLOAD_LIMIT + 1):
            self.remote.upload(str(uuid4()), io.BytesIO(b'x' * size), self.metadata)
        self.assertEqual([option['resumable'] for option in self.media_options], [False, False, True])
        for request in requests[:2]:
            request.execute.assert_called_once_with(num_retries=2)
            request.next_chunk.assert_not_called()
        requests[-1].next_chunk.assert_called_once_with(num_retries=2)
        requests[-1].execute.assert_not_called()

    def test_small_upload_auth_failure_remains_sanitized_and_clears_cache(self):
        original_create = self.service.create
        def create(**kwargs):
            if kwargs.get('media_body') is not None:
                return Request(lambda: (_ for _ in ()).throw(HttpFailure(401)))
            return original_create(**kwargs)
        self.service.create = create
        with self.assertRaises(DriveError) as caught:
            self.upload()
        self.assertTrue(caught.exception.requires_auth)
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertEqual(self.remote._folder_cache, {})

    def test_id_pool_batches_allocation_and_commits_each_identity(self):
        with patch.object(self.service, 'generateIds', wraps=self.service.generateIds) as generate:
            first = self.remote._allocate('one')
            second = self.remote._allocate('two')
        generate.assert_called_once_with(count=100, space='drive', type='files')
        self.assertNotEqual(first, second)
        rows = self.db.connection.execute('SELECT object_key,drive_id,confirmed FROM drive_objects ORDER BY object_key').fetchall()
        self.assertEqual([tuple(row) for row in rows], [('one', first, 0), ('two', second, 0)])

    def test_folder_cache_avoids_repeated_ancestor_requests_but_not_file_lookup(self):
        self.upload()
        with patch.object(self.remote, '_get', wraps=self.remote._get) as get:
            self.upload()
            self.assertEqual(get.call_count, 1)  # File identity is always revalidated.
        self.assertEqual(len(self.remote._folder_cache), 5)

    def test_folder_cache_expires_without_sliding_and_revalidates_type(self):
        with patch('files2gdrive.drive.time.monotonic', return_value=100):
            self.upload()
        with patch('files2gdrive.drive.time.monotonic', return_value=129), patch.object(self.remote, '_get', wraps=self.remote._get) as get:
            self.upload()
            self.assertEqual(get.call_count, 1)
        folder = next(item for item in self.service.items.values() if item.get('mimeType') == FOLDER)
        folder['mimeType'] = 'text/plain'
        with patch('files2gdrive.drive.time.monotonic', return_value=130):
            with self.assertRaisesRegex(DriveError, 'invalid type'):
                self.upload()
        self.assertEqual(self.remote._folder_cache, {})

    def test_upload_failure_invalidates_cached_ancestors(self):
        self.upload()
        self.service.fail_upload = True
        with self.assertRaises(DriveError):
            self.upload()
        self.assertEqual(self.remote._folder_cache, {})
        self.service.fail_upload = False
        with patch.object(self.remote, '_get', wraps=self.remote._get) as get:
            self.upload()
            self.assertEqual(get.call_count, 6)  # Five folders plus file.

    def test_retention_and_trash_failures_invalidate_folder_cache(self):
        for operation in ('retain', 'trash'):
            with self.subTest(operation=operation):
                self.remote._cache_folder('cached', 'drive-folder')
                with patch.object(self.remote, '_find', side_effect=HttpFailure(404)):
                    with self.assertRaises(DriveError):
                        if operation == 'retain':
                            self.remote.retain(self.file_id, 123)
                        else:
                            self.remote.trash(self.file_id)
                self.assertEqual(self.remote._folder_cache, {})

    def test_folder_cache_is_bounded(self):
        with patch('files2gdrive.drive.FOLDER_CACHE_LIMIT', 2):
            self.remote._cache_folder('a', 'id-a')
            self.remote._cache_folder('b', 'id-b')
            self.remote._cache_folder('c', 'id-c')
        self.assertEqual(set(self.remote._folder_cache), {'b', 'c'})

    def test_nested_upload_hidden_path_metadata_and_chunking(self):
        item = self.upload()
        self.assertEqual(item['bytes'], b'configuration')
        self.assertEqual(item['name'], 'settings.json')
        self.assertEqual(json.loads(item['description'])['mode'], 0o640)
        self.assertEqual(self.media_options[0]['chunksize'], 8 * 1024 * 1024)
        self.assertFalse(self.media_options[0]['resumable'])
        folders = [item['name'] for item in self.service.items.values() if item.get('mimeType') == FOLDER]
        self.assertEqual(folders, ['Files2GDrive', 'Ubuntu', 'Config', '.config', 'app'])
    def test_repeated_upload_updates_same_id_and_rename(self):
        initial = self.upload()['id']; count = len(self.service.items)
        self.metadata['path'] = '.config/app/renamed.json'
        self.upload()
        self.assertEqual(self.item()['id'], initial)
        self.assertEqual(len(self.service.items), count)
        self.assertEqual(self.item()['name'], 'renamed.json')
    def test_retention_then_restore_then_trash(self):
        initial = self.upload()['id']
        self.remote.retain(self.file_id, 123)
        retained = self.item()
        self.assertEqual(json.loads(retained['description'])['deleted_at'], 123)
        self.assertEqual(json.loads(retained['description'])['state'], 'excluded')
        parent = retained['parents'][0]
        self.assertEqual(self.service.items[parent]['name'], self.file_id)
        self.upload(); self.assertEqual(self.item()['id'], initial)
        self.assertEqual(json.loads(self.item()['description'])['state'], 'active')
        self.remote.trash(self.file_id)
        self.assertTrue(self.service.items[initial]['trashed'])
        self.remote.trash(self.file_id)  # idempotent, not permanent deletion
    def test_missing_or_manually_trashed_not_recreated_by_retention(self):
        self.remote.retain(self.file_id, 123); self.assertEqual(self.service.items, {})
        item = self.upload(); item['trashed'] = True; count = len(self.service.items)
        self.remote.retain(self.file_id, 999)
        self.assertEqual(len(self.service.items), count)
        self.upload(); self.assertNotEqual(self.item()['id'], item['id'])
    def seed_unattempted_local_file(self):
        with self.db.connection:
            self.db.connection.execute("INSERT INTO files(id,root_id,path,fingerprint,observed_at,device,inode) VALUES(?,?,?,?,?,?,?)",
                (self.file_id,self.root_id,self.metadata['path'],'[]',1,1,1))
            self.db.connection.execute("INSERT INTO jobs(file_id,operation) VALUES(?,'upload')",(self.file_id,))

    def test_new_local_file_skips_remote_search_and_repeat_still_validates(self):
        self.seed_unattempted_local_file()
        key = _key('file:' + self.db.get('computer_id') + ':' + self.file_id)
        with patch.object(self.remote, '_find', wraps=self.remote._find) as find:
            self.upload()
            self.assertNotIn(unittest.mock.call(key), find.call_args_list)
            find.reset_mock()
            first = self.item()['id']
            self.upload()
            self.assertIn(unittest.mock.call(key), find.call_args_list)
            self.assertEqual(self.item()['id'], first)

    def test_incomplete_or_imported_history_never_skips_file_lookup(self):
        self.seed_unattempted_local_file()
        key = _key('file:' + self.db.get('computer_id') + ':' + self.file_id)
        changes = [
            ("UPDATE files SET remote_fingerprint='[]'", "UPDATE files SET remote_fingerprint=NULL"),
            ("UPDATE files SET device=NULL", "UPDATE files SET device=1"),
            ("UPDATE files SET state='excluded'", "UPDATE files SET state='active'"),
            ("UPDATE jobs SET attempts=1", "UPDATE jobs SET attempts=0"),
            ("UPDATE jobs SET operation='retain'", "UPDATE jobs SET operation='upload'"),
        ]
        for change, reset in changes:
            with self.subTest(change=change):
                self.db.connection.execute(change)
                self.assertFalse(self.remote._unattempted_local_file(self.file_id,key,self.metadata))
                self.db.connection.execute(reset)
        self.db.set('reconnect_pending','{}')
        self.assertFalse(self.remote._unattempted_local_file(self.file_id,key,self.metadata))
        self.db.connection.execute("DELETE FROM settings WHERE key='reconnect_pending'")
        self.assertFalse(self.remote._unattempted_local_file(self.file_id,key,dict(self.metadata,path='other')))
        self.db.connection.execute('DELETE FROM jobs')
        self.assertFalse(self.remote._unattempted_local_file(self.file_id,key,self.metadata))
        self.db.connection.commit()
        with patch.object(self.remote,'_find',wraps=self.remote._find) as find:
            self.upload()
            self.assertIn(unittest.mock.call(key),find.call_args_list)

    def test_first_create_lost_response_survives_remote_reinitialization(self):
        self.seed_unattempted_local_file()
        self.service.lost_create_response=True
        with self.assertRaises(DriveError): self.upload()
        original=self.item()['id']; count=len(self.service.creates)
        self.remote=GoogleDriveRemote(self.db,self.service,media_factory=lambda stream,**kwargs:stream)
        self.upload()
        self.assertEqual(self.item()['id'],original)
        self.assertEqual(len(self.service.creates),count)

    def test_lost_create_response_reuses_committed_id(self):
        self.service.lost_create_response = True
        with self.assertRaises(DriveError) as caught: self.upload()
        self.assertNotIn('SECRET', str(caught.exception))
        first_id = self.item()['id']; count = len(self.service.creates)
        self.upload()
        self.assertEqual(self.item()['id'], first_id)
        self.assertEqual(len(self.service.creates), count)
    def test_failed_create_retries_same_reserved_id(self):
        self.seed_unattempted_local_file()
        self.service.fail_upload = True
        with self.assertRaises(DriveError): self.upload()
        key = _key('file:' + self.db.get('computer_id') + ':' + self.file_id)
        reserved = self.db.connection.execute('SELECT drive_id FROM drive_objects WHERE object_key=?', (key,)).fetchone()[0]
        self.service.fail_upload = False; self.upload()
        self.assertEqual(self.item()['id'], reserved)
    def test_query_paginates_and_rejects_duplicate_identity(self):
        key = 'arbitrary'
        self.service.pages = [{'files': [], 'nextPageToken': '1'}, {'files': [{'id': 'found'}]}]
        item, drive_id = self.remote._find(key)
        self.assertEqual(drive_id, 'found')
        self.service.pages = [{'files': [{'id': 'first'}], 'nextPageToken': '1'}, {'files': [{'id': 'second'}]}]
        with self.assertRaises(DriveError): self.remote._find('duplicate')
    def test_account_mismatch_refuses_existing_state(self):
        self.service.account = 'different'
        with self.assertRaisesRegex(DriveError, 'different Google account'):
            GoogleDriveRemote(self.db, self.service)
        self.assertEqual(self.db.get('drive_account_id'), 'account-one')
    def test_unsafe_paths_and_special_permissions_rejected(self):
        for path in ('../secret', '/absolute', 'a//b'):
            self.metadata['path'] = path
            with self.assertRaises(ValueError): self.upload()
        self.metadata['path'] = 'valid'; self.metadata['mode'] = 0o4755
        with self.assertRaises(ValueError): self.upload()

    def test_different_installations_respect_destination_names(self):
        self.upload()
        other_db = Database(Path(self.temp.name) / 'other-state')
        try:
            with other_db.connection:
                other_db.connection.execute('INSERT INTO roots(id,path,name) VALUES(?,?,?)',
                                           (self.root_id, '/other/root', 'Config'))
            other = GoogleDriveRemote(other_db, self.service, folder_name='Other backup',
                                      media_factory=lambda stream, **kwargs: stream)
            other.upload(str(uuid4()), io.BytesIO(b'new'), self.metadata)
            containers = [item for item in self.service.items.values() if not item.get('parents')]
            self.assertEqual({item['name'] for item in containers}, {'Files2GDrive', 'Other backup'})
            new_file = next(item for item in self.service.items.values() if item.get('bytes') == b'new')
            ancestor = new_file
            while ancestor.get('parents'):
                ancestor = self.service.items[ancestor['parents'][0]]
            self.assertEqual(ancestor['name'], 'Other backup')
        finally:
            other_db.close()

    def test_upload_drives_multiple_chunks_to_completion(self):
        original_create = self.service.create
        chunks = []
        def create(**kwargs):
            request = original_create(**kwargs)
            if kwargs.get('media_body') is None:
                return request
            class Chunked:
                def next_chunk(self, **options):
                    chunks.append(options)
                    if len(chunks) < 3:
                        return object(), None
                    return request.next_chunk(**options)
            return Chunked()
        self.service.create = create
        with patch('files2gdrive.drive.MULTIPART_UPLOAD_LIMIT', 0):
            self.upload()
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(options['num_retries'] == 2 for options in chunks))
