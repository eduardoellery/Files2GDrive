import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from files2gdrive.scanner import scan_root


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_hidden_files_and_metadata(self):
        folder = self.root / ".config"
        folder.mkdir()
        target = folder / ".settings"
        target.write_bytes(b"settings")
        target.chmod(0o754)
        result = scan_root(self.root)
        self.assertTrue(result.complete)
        snapshot = result.files[".config/.settings"]
        info = target.stat()
        self.assertEqual(snapshot.fingerprint, (8, info.st_mtime_ns, 0o754))
        self.assertEqual((snapshot.device, snapshot.inode), (info.st_dev, info.st_ino))

    def test_symlinks_are_reported_without_following(self):
        (self.root / "file").write_text("hello")
        (self.root / "link").symlink_to(self.root / "file")
        (self.root / "loop").symlink_to(self.root, target_is_directory=True)
        (self.root / "broken").symlink_to(self.root / "missing")
        result = scan_root(self.root)
        self.assertTrue(result.complete)
        self.assertEqual(set(result.files), {"file"})
        self.assertEqual(len(result.issues), 3)

    def test_missing_and_symlink_roots_are_incomplete(self):
        self.assertFalse(scan_root(self.root / "missing").complete)
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        self.assertFalse(scan_root(link).complete)

    def test_unreadable_files_and_directories_are_incomplete(self):
        target = self.root / "private"
        target.write_text("secret")
        target.chmod(0)
        folder = self.root / "folder"
        folder.mkdir()
        folder.chmod(0)
        self.addCleanup(folder.chmod, 0o700)
        result = scan_root(self.root)
        self.assertFalse(result.complete)
        self.assertEqual(result.files, {})
        self.assertEqual(len(result.issues), 2)

    def test_enumeration_failure_is_not_a_complete_empty_scan(self):
        with patch("files2gdrive.scanner.os.scandir", side_effect=PermissionError("denied")):
            result = scan_root(self.root)
        self.assertFalse(result.complete)
        self.assertIn("denied", result.issues[0])

    def test_special_file_ignored_and_special_permission_bits_removed(self):
        os.mkfifo(self.root / "pipe")
        file = self.root / "file"
        file.write_text("data")
        file.chmod(0o4755)
        result = scan_root(self.root)
        self.assertTrue(result.complete)
        self.assertEqual(set(result.files), {"file"})
        self.assertEqual(result.files["file"].mode, 0o755)
        self.assertIn("non-regular", result.issues[0])

    def test_symlink_in_root_ancestor_is_incomplete_not_a_different_tree(self):
        actual = self.root / 'actual'
        (actual / 'source').mkdir(parents=True)
        (actual / 'source' / 'foreign').write_text('must not scan')
        link = self.root / 'parent-link'
        link.symlink_to(actual, target_is_directory=True)
        result = scan_root(link / 'source')
        self.assertFalse(result.complete)
        self.assertEqual(result.files, {})
        self.assertTrue(result.issues)

    def test_replaced_root_parent_does_not_infer_an_empty_complete_scan(self):
        parent = self.root / 'parent'
        source = parent / 'source'
        source.mkdir(parents=True)
        (source / 'original').write_text('preserve backup')
        self.assertTrue(scan_root(source).complete)
        parent.rename(self.root / 'original-parent')
        replacement = self.root / 'replacement'
        (replacement / 'source').mkdir(parents=True)
        parent.symlink_to(replacement, target_is_directory=True)
        result = scan_root(source)
        self.assertFalse(result.complete)
        self.assertEqual(result.files, {})


if __name__ == "__main__":
    unittest.main()
