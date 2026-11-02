import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'scripts/build_deb.py'
spec = importlib.util.spec_from_file_location('build_deb', MODULE)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

CLIENT = {'installed': dict(client_id='test.apps.googleusercontent.com', client_secret='desktop-app-id',
                           auth_uri='https://accounts.google.com/o/oauth2/auth',
                           token_uri='https://oauth2.googleapis.com/token', project_id='public-project')}


@unittest.skipUnless(shutil.which('dpkg-deb'), 'Debian packaging tools are not installed')
class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'app'; self.source.mkdir()
        (self.source / '__init__.py').write_text('')
        (self.source / 'packaged_launcher.py').write_text('def main(**kwargs): print("staged launcher works")\n')
        (self.source / 'web').mkdir(); (self.source / 'web/index.html').write_text('<h1>Files2GDrive</h1>')
        (self.source / 'assets').mkdir(); (self.source / 'assets/files2gdrive.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        (self.source / 'token.json').write_text('USER-TOKEN-MUST-NOT-BE-COPIED')
        self.vendor = self.root / 'vendor'; self.vendor.mkdir()
        (self.vendor / 'dependency.py').write_text('VALUE = 1\n')
        (self.vendor / 'site.pth').write_text('/private/workspace')
        (self.vendor / 'dependency.dist-info').mkdir()
        (self.vendor / 'dependency.dist-info/METADATA').write_text('Name: dependency\nVersion: 1\n')
        (self.vendor / 'dependency.dist-info/direct_url.json').write_text('{"url":"file:///private/workspace"}')
        self.client = self.root / 'oauth.json'; self.client.write_text(json.dumps(CLIENT))
    def tearDown(self): self.temporary.cleanup()
    def build(self, **kwargs):
        return builder.build_package(self.vendor, self.client, self.root / 'output', source_root=self.source, **kwargs)
    def extract(self, artifact):
        target = self.root / 'extracted'
        subprocess.run(['dpkg-deb', '--extract', str(artifact), str(target)], check=True, capture_output=True)
        return target

    def test_public_release_version_matches_metadata_and_upgrades_alpha(self):
        import tomllib
        artifact = self.build(); target = self.extract(artifact)
        version = tomllib.loads((builder.REPOSITORY / 'pyproject.toml').read_text())['project']['version']
        self.assertEqual(version, '0.1.0')
        self.assertEqual(artifact.name, f'files2gdrive_{version}_amd64.deb')
        self.assertEqual((target / 'usr/share/files2gdrive/VERSION').read_text().strip(), version)
        actual = subprocess.check_output(['dpkg-deb', '--field', str(artifact), 'Version'], text=True).strip()
        self.assertEqual(actual, version)
        subprocess.run(['dpkg', '--compare-versions', actual, 'gt', '0.1.0~alpha3'], check=True)

    def test_payload_contains_only_app_dependencies_and_public_client(self):
        artifact = self.build(); target = self.extract(artifact)
        self.assertTrue(artifact.is_file())
        self.assertTrue(artifact.with_suffix('.deb.sha256').is_file())
        launcher = target / 'usr/bin/files2gdrive'
        self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
        self.assertEqual(subprocess.check_output([str(launcher)], text=True).strip(), 'staged launcher works')
        copyright_text = (target / 'usr/share/doc/files2gdrive/copyright').read_text()
        self.assertIn((builder.REPOSITORY / 'LICENSE').read_text(), copyright_text)
        self.assertIn('Third-party Python dependencies', copyright_text)
        self.assertNotIn('No project-wide redistribution license', copyright_text)
        self.assertTrue((target / 'usr/lib/files2gdrive/vendor/dependency.py').is_file())
        self.assertFalse((target / 'usr/lib/files2gdrive/vendor/site.pth').exists())
        self.assertFalse((target / 'usr/lib/files2gdrive/vendor/dependency.dist-info/direct_url.json').exists())
        self.assertFalse((target / 'usr/lib/files2gdrive/app/files2gdrive/token.json').exists())
        public = json.loads((target / 'usr/share/files2gdrive/oauth-client.json').read_text())
        self.assertEqual(set(public['installed']), set(builder.CLIENT_KEYS))
        self.assertEqual((target / 'usr/share/applications/files2gdrive.desktop').stat().st_mode & 0o777, 0o644)
        self.assertIn('Exec=/usr/bin/files2gdrive --background', (target / 'etc/xdg/autostart/files2gdrive.desktop').read_text())
        contents = subprocess.check_output(['dpkg-deb', '--contents', str(artifact)], text=True)
        self.assertNotIn('USER-TOKEN', contents)
        self.assertNotIn('/home/', contents)
        self.assertIn('root/root', contents)

    def test_control_has_native_dependencies_but_no_maintainer_scripts(self):
        artifact = self.build(); metadata = self.root / 'control'
        subprocess.run(['dpkg-deb', '--control', str(artifact), str(metadata)], check=True, capture_output=True)
        control = (metadata / 'control').read_text()
        self.assertIn('python3 (>= 3.12)', control)
        self.assertIn('gir1.2-ayatanaappindicator3-0.1', control)
        self.assertIn('Architecture: amd64', control)
        self.assertEqual({path.name for path in metadata.iterdir()}, {'control', 'md5sums', 'conffiles'})
        if shutil.which('desktop-file-validate'):
            target = self.extract(artifact)
            for path in (target / 'usr/share/applications/files2gdrive.desktop', target / 'etc/xdg/autostart/files2gdrive.desktop'):
                subprocess.run(['desktop-file-validate', str(path)], check=True, capture_output=True)

    def test_user_tokens_in_oauth_configuration_are_rejected(self):
        for field in ('token', 'refresh_token', 'access_token', 'id_token'):
            content = json.loads(json.dumps(CLIENT)); content['installed'][field] = 'PRIVATE'
            self.client.write_text(json.dumps(content))
            with self.assertRaisesRegex(ValueError, 'authorization/token'): self.build()

    def test_web_oauth_configuration_is_rejected(self):
        self.client.write_text(json.dumps({'web': CLIENT['installed']}))
        with self.assertRaises(RuntimeError): self.build()

    def test_architecture_and_version_cannot_inject_paths_or_control_fields(self):
        for version in ('../escape', '1\nDepends: malicious', '-1', '1;touch'):
            with self.assertRaises(ValueError): self.build(version=version)
        with self.assertRaises(ValueError): self.build(architecture='all')

    def test_vendor_symlinks_and_credential_like_files_are_rejected(self):
        link = self.vendor / 'link.py'; link.symlink_to(self.vendor / 'dependency.py')
        with self.assertRaisesRegex(ValueError, 'symbolic links'): self.build()
        link.unlink(); (self.vendor / 'token.json').write_text('private')
        with self.assertRaisesRegex(ValueError, 'credential-like'): self.build()

    def test_source_symlinks_are_rejected(self):
        (self.source / 'outside.py').symlink_to(self.vendor / 'dependency.py')
        with self.assertRaisesRegex(ValueError, 'symbolic links'): self.build()
