import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('public_export', Path(__file__).parents[1] / 'scripts/export_public_source.py')
public_export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(public_export)


class PublicExportTests(unittest.TestCase):
    def test_private_profiles_credentials_and_history_never_enter_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'source'; root.mkdir()
            safe = ['README.md', 'LICENSE', 'pyproject.toml', '.gitignore',
                    'src/files2gdrive/engine.py', 'docs/INTERFACE-WEB.md']
            private = ['.runtime-drive/oauth-client.json', '.runtime-drive/state.sqlite3',
                       '.private-notes/DECISOES.md', '.env', 'client_secret.json',
                       '.git/config', '.venv/lib/private.py', 'dist/private.py',
                       'src/files2gdrive/__pycache__/private.py',
                       'docs/private-report.json', 'docs/UNREVIEWED.md']
            for name in safe + private:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
            output = Path(temporary) / 'public.tar.gz'
            public_export.export(root, output)
            with tarfile.open(output) as archive:
                self.assertEqual({item.name for item in archive}, {'Files2GDrive/' + name for name in safe})
                self.assertTrue(all(item.uid == item.gid == item.mtime == 0 for item in archive))
            manifest = json.loads(output.with_suffix('.gz.manifest.json').read_text())
            self.assertEqual({item['path'] for item in manifest}, set(safe))

    def test_symlink_cannot_export_external_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'source'; root.mkdir()
            for name in ('README.md', 'LICENSE', 'pyproject.toml'):
                (root / name).write_text('metadata')
            outside = Path(temporary) / 'secret'; outside.write_text('private')
            (root / 'src').mkdir()
            (root / 'src/leak.py').symlink_to(outside)
            with self.assertRaises(ValueError):
                public_export.export(root, Path(temporary) / 'public.tar.gz')
