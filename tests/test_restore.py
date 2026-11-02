import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from files2gdrive.restore import RestoreManager, validate_metadata


class Remote:
    def __init__(self, content=b'config data'):
        self.content = content
        self.before = None
    def download(self, entry, stream):
        if self.before: self.before(entry, stream)
        stream.write(self.content)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.destination = self.base / 'restored'
        self.remote = Remote()
        self.entry = dict(version=1, file_id=str(uuid4()), computer_id=str(uuid4()), root_id=str(uuid4()),
                          path='.config/app/settings', mode=0o640, mtime_ns=1700000000123456789,
                          state='active', drive_id='remote-one', size=len(self.remote.content),
                          md5Checksum=hashlib.md5(self.remote.content, usedforsecurity=False).hexdigest())
        self.manager = RestoreManager(self.remote)
    def tearDown(self): self.temp.cleanup()
    def test_restore_content_hidden_file_mode_and_timestamp(self):
        self.assertEqual(self.manager.restore([self.entry], self.destination), [self.entry['path']])
        output = self.destination / self.entry['path']
        self.assertEqual(output.read_bytes(), self.remote.content)
        self.assertEqual(output.stat().st_mode & 0o7777, 0o640)
        self.assertEqual(output.stat().st_mtime_ns, self.entry['mtime_ns'])
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(output.parent.glob('.files2gdrive-*')), [])
    def test_existing_destination_is_never_used(self):
        self.destination.mkdir(); (self.destination / 'keep').write_text('original')
        with self.assertRaises(FileExistsError): self.manager.restore([self.entry], self.destination)
        self.assertEqual((self.destination / 'keep').read_text(), 'original')
    def test_path_traversal_invalid_identifiers_and_metadata_rejected_before_writes(self):
        variants = [dict(path=value) for value in ('../escape', '/absolute', 'a//b', 'a/./b', 'a/../b', 'a\0b', '')]
        variants += [dict(file_id='not-uuid'), dict(version=True), dict(version=2), dict(mode=True),
                     dict(mode=0o4755), dict(mtime_ns=-1), dict(mtime_ns=True), dict(mtime_ns=2**63),
                     dict(state='trashed'), dict(size=-1), dict(size=True), dict(md5Checksum='invalid')]
        for changes in variants:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError): self.manager.restore([dict(self.entry, **changes)], self.destination)
                self.assertFalse(self.destination.exists())
    def test_retained_metadata_requires_finite_valid_timestamp(self):
        for value in (None, float('nan'), float('inf'), -1, True):
            with self.assertRaises(ValueError): validate_metadata(dict(self.entry, state='excluded', deleted_at=value))
        clean = validate_metadata(dict(self.entry, state='excluded', deleted_at=123.5))
        self.assertEqual(clean['deleted_at'], 123.5)
    def test_duplicate_and_parent_file_conflicts_rejected_before_writes(self):
        for second in (dict(self.entry), dict(self.entry, file_id=str(uuid4()), path='.config')):
            with self.assertRaises(ValueError): self.manager.restore([self.entry, second], self.destination)
            self.assertFalse(self.destination.exists())
    def test_mixed_roots_rejected(self):
        second = dict(self.entry, root_id=str(uuid4()), file_id=str(uuid4()), path='other')
        with self.assertRaises(ValueError): self.manager.restore([self.entry, second], self.destination)
    def test_symlink_in_destination_ancestors_refused(self):
        real = self.base / 'actual'; real.mkdir()
        alias = self.base / 'alias'; alias.symlink_to(real, target_is_directory=True)
        with self.assertRaises(OSError): self.manager.restore([self.entry], alias / 'restore')
        self.assertFalse((real / 'restore').exists())
    def test_symlink_swap_during_download_cannot_redirect_later_file(self):
        outside = self.base / 'outside'; outside.mkdir()
        first = dict(self.entry, path='first')
        second = dict(self.entry, file_id=str(uuid4()), path='folder/second')
        def inject(entry, stream):
            if entry['path'] == 'first': (self.destination / 'folder').symlink_to(outside, target_is_directory=True)
        self.remote.before = inject
        with self.assertRaises(OSError): self.manager.restore([first, second], self.destination)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual((self.destination / 'first').read_bytes(), self.remote.content)
    def test_existing_file_injected_during_download_is_not_overwritten(self):
        def inject(entry, stream):
            target = self.destination / entry['path']; target.write_text('existing')
        self.remote.before = inject
        with self.assertRaises(FileExistsError): self.manager.restore([self.entry], self.destination)
        self.assertEqual((self.destination / self.entry['path']).read_text(), 'existing')
        self.assertEqual(list(self.destination.rglob('.files2gdrive-*')), [])
    def test_size_and_checksum_mismatch_never_publish(self):
        for changes in (dict(size=self.entry['size'] + 1), dict(md5Checksum='0' * 32)):
            destination = self.base / uuid4().hex
            with self.assertRaises(ValueError): self.manager.restore([dict(self.entry, **changes)], destination)
            self.assertFalse((destination / self.entry['path']).exists())
            self.assertEqual(list(destination.rglob('.files2gdrive-*')), [])
    def test_download_failure_removes_partial_temporary(self):
        def fail(entry, stream):
            stream.write(b'partial'); raise OSError('network unavailable')
        self.remote.before = fail
        with self.assertRaises(OSError): self.manager.restore([self.entry], self.destination)
        self.assertFalse((self.destination / self.entry['path']).exists())
        self.assertEqual(list(self.destination.rglob('.files2gdrive-*')), [])
    def test_empty_file_restores_with_zero_mode(self):
        self.remote.content = b''
        entry = dict(self.entry, path='empty', size=0, mode=0,
                     md5Checksum=hashlib.md5(b'', usedforsecurity=False).hexdigest())
        self.manager.restore([entry], self.destination)
        self.assertEqual((self.destination / 'empty').stat().st_mode & 0o777, 0)
