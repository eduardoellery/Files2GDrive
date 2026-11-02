from pathlib import Path
import tempfile
import unittest
from files2gdrive.filesystem import open_source


class SafeOpeningTests(unittest.TestCase):
    def test_never_follows_file_or_parent_links(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'actual').mkdir(); (base / 'actual/file').write_bytes(b'contents')
            (base / 'link').symlink_to(base / 'actual', target_is_directory=True)
            (base / 'filelink').symlink_to(base / 'actual/file')
            for name in ('link/file', 'filelink'):
                with self.assertRaises(OSError):
                    with open_source(base / name): pass
            with open_source(base / 'actual/file') as stream:
                self.assertEqual(stream.read(), b'contents')
