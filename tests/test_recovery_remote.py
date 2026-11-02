import copy
import hashlib
import io
import json
import unittest
from uuid import uuid4

from files2gdrive.drive import DriveError, _key
from files2gdrive.recovery_remote import DriveRecovery


class Request:
    def __init__(self, result): self.result = result
    def execute(self, **kwargs):
        if isinstance(self.result, Exception): raise self.result
        return copy.deepcopy(self.result)


class Service:
    def __init__(self, pages):
        self.pages = pages; self.queries = []; self.items = {}; self.get_calls = 0
        for page in pages:
            self.items.update({item['id']: item for item in page.get('files', [])})
    def files(self): return self
    def list(self, **kwargs):
        self.queries.append(kwargs)
        return Request(self.pages[int(kwargs.get('pageToken') or 0)])
    def get(self, fileId, **kwargs):
        self.get_calls += 1
        return Request(self.items[fileId])
    def get_media(self, fileId): return ('media', fileId)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.computer = str(uuid4()); self.root = str(uuid4()); self.file = str(uuid4())
        self.payload = b'test generated backup'
        self.metadata = dict(version=1, computer_id=self.computer, root_id=self.root,
                             file_id=self.file, path='.config/app', mode=0o640,
                             mtime_ns=123456789, state='active', computer_name='Ubuntu',
                             root_name='Config', folder_name='Backup')
        self.item = dict(id='drive-1', name='app', mimeType='application/octet-stream',
                         trashed=False, description=json.dumps(self.metadata), size=str(len(self.payload)),
                         md5Checksum=hashlib.md5(self.payload).hexdigest(), appProperties={
                             'f2g_version': '1', 'f2g_computer': self.computer, 'f2g_root': self.root,
                             'f2g_key': _key('file:' + self.computer + ':' + self.file)})
        self.service = Service([{'files': [self.item]}])
        self.remote = DriveRecovery(self.service)

    def test_catalog_normalizes_and_preserves_labels(self):
        entries = self.remote.list_files(self.computer, self.root)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['size'], len(self.payload))
        self.assertEqual(entries[0]['root_name'], 'Config')
        self.assertEqual(entries[0]['drive_id'], 'drive-1')
        query = self.service.queries[0]['q']
        self.assertIn('trashed = false', query)
        self.assertIn(self.computer, query)
        self.assertIn(self.root, query)

    def test_summary_groups_computer_and_roots(self):
        summary = self.remote.list_backups()
        self.assertEqual(summary[0]['name'], 'Ubuntu')
        self.assertEqual(summary[0]['active_files'], 1)
        self.assertEqual(summary[0]['roots'][0]['name'], 'Config')
        self.assertEqual(summary[0]['folder_name'], 'Backup')

    def test_pagination_and_excluded_filter(self):
        excluded = copy.deepcopy(self.item)
        metadata = dict(self.metadata, file_id=str(uuid4()), state='excluded', deleted_at=123)
        excluded.update(id='drive-2', description=json.dumps(metadata))
        excluded['appProperties']['f2g_key'] = _key('file:' + self.computer + ':' + metadata['file_id'])
        self.service.pages = [{'files': [self.item], 'nextPageToken': '1'}, {'files': [excluded]}]
        entries = self.remote.list_files(self.computer)
        self.assertEqual(len(entries), 2)  # identical paths are valid separate retained identities
        self.assertEqual(len(self.remote.list_files(self.computer, include_excluded=False)), 1)

    def test_duplicate_identity_and_pagination_loop_rejected(self):
        other = dict(self.item, id='drive-2')
        self.service.pages = [{'files': [self.item, other]}]
        with self.assertRaisesRegex(DriveError, 'duplicate'): self.remote.list_backups()
        self.service.pages = [{'files': [], 'nextPageToken': '0'}]
        with self.assertRaisesRegex(DriveError, 'pagination'): self.remote.list_backups()

    def test_unsafe_metadata_wrong_identity_and_missing_checksum_rejected(self):
        for change in ('path', 'identity', 'checksum', 'trashed'):
            broken = copy.deepcopy(self.item)
            if change == 'path':
                broken['description'] = json.dumps(dict(self.metadata, path='../escape'))
            elif change == 'identity': broken['appProperties']['f2g_root'] = str(uuid4())
            elif change == 'checksum': broken.pop('md5Checksum')
            else: broken['trashed'] = True
            self.service.pages = [{'files': [broken]}]
            with self.subTest(change=change), self.assertRaises(DriveError): self.remote.list_backups()

    def test_download_chunks_and_revalidates_selection(self):
        options = []; calls = []
        payload = self.payload
        class Downloader:
            def __init__(self, stream, request, **kwargs):
                self.stream = stream; options.append(kwargs); self.number = 0
            def next_chunk(self, **kwargs):
                calls.append(kwargs); self.number += 1
                if self.number == 1:
                    self.stream.write(payload[:3]); return None, False
                self.stream.write(payload[3:]); return None, True
        remote = DriveRecovery(self.service, download_factory=Downloader)
        stream = io.BytesIO(); entry = remote.list_files(self.computer)[0]
        remote.download(entry, stream)
        self.assertEqual(stream.getvalue(), payload)
        self.assertFalse(stream.closed)
        self.assertEqual(options, [{'chunksize': 8 * 1024 * 1024}])
        self.assertEqual(calls, [{'num_retries': 2}, {'num_retries': 2}])
        self.assertEqual(self.service.get_calls, 2)

    def test_changed_remote_selection_fails_before_download(self):
        entry = self.remote.list_files(self.computer)[0]
        self.service.items['drive-1']['md5Checksum'] = 'a' * 32
        with self.assertRaisesRegex(DriveError, 'changed'):
            self.remote.download(entry, io.BytesIO())

    def test_download_errors_never_expose_raw_response(self):
        entry = self.remote.list_files(self.computer)[0]
        class Failing:
            def __init__(self, *args, **kwargs): raise OSError('SECRET TOKEN AND URL')
        remote = DriveRecovery(self.service, download_factory=Failing)
        with self.assertRaises(DriveError) as failure: remote.download(entry, io.BytesIO())
        self.assertNotIn('SECRET', str(failure.exception))


if __name__ == '__main__': unittest.main()
