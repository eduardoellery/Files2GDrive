#!/usr/bin/env python3
"""Build a self-contained Python app payload with native Ubuntu dependencies.

Only app sources/assets, an explicitly prepared vendor tree and a validated
Desktop OAuth client are packaged. User state, refresh tokens and the workspace
virtual environment are never copied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

REPOSITORY = Path(__file__).resolve().parents[1]
VERSION = re.compile(r'[0-9][A-Za-z0-9.+~\-]*\Z')
CLIENT_KEYS = ('client_id', 'client_secret', 'auth_uri', 'token_uri')
FORBIDDEN = frozenset({'token', 'access_token', 'refresh_token', 'id_token', 'expiry', 'authorized_user'})
WRAPPER = '''#!/usr/bin/python3
"""Files2GDrive Debian launcher; paths also work in an extracted package."""
from pathlib import Path
import sys
prefix = Path(__file__).resolve().parents[1]
application = prefix / 'lib' / 'files2gdrive'
sys.path[:0] = [str(application / 'app'), str(application / 'vendor')]
from files2gdrive.packaged_launcher import main
main(resources=prefix / 'share' / 'files2gdrive', executable=Path(__file__).resolve())
'''
DESKTOP = '''[Desktop Entry]
Version=1.0
Type=Application
Name=Files2GDrive
Comment=Backup de arquivos locais para o Google Drive
Exec=/usr/bin/files2gdrive{arguments}
TryExec=/usr/bin/files2gdrive
Icon=files2gdrive
Terminal=false
StartupNotify=false
Categories=Utility;Archiving;
X-GNOME-UsesNotifications=true
{extra}'''
README = '''Files2GDrive — Ubuntu 24.04 amd64 development package

Start Files2GDrive from the applications menu. It opens the local configuration
and tray helper. A global XDG autostart entry starts it silently on session login.
The backup runs as the logged-in user, never as root. Native GTK/Ayatana and
Secret Service support are required in the desktop session.

The package contains the OAuth identification of this installed desktop app.
It does NOT contain any user's Google authorization, refresh tokens, local
backup state or monitored files. Each user authorizes their own account.
If the OAuth project is still in Testing, the account must be an approved tester.
Do not distribute as a public production release before OAuth publication.

User state lives in $XDG_STATE_HOME/files2gdrive or ~/.local/state/files2gdrive.
Removal/purge does not delete that state, user keyring entries or remote backups.
No maintainer script starts processes, touches home directories or deletes data.
Quit the application before upgrading/removing it; an already running process
is not forcibly terminated by package installation or removal.

This is an early test build: installation on a clean Ubuntu 24.04 machine,
large-volume performance and all previously documented production limitations
still need validation before relying on it for important backups.
'''


def client_configuration(path: Path) -> dict:
    sys.path.insert(0, str(REPOSITORY / 'src'))
    from files2gdrive.auth import _client_config
    configuration = _client_config(path)
    def has_tokens(value):
        if isinstance(value, dict):
            return bool(set(value) & FORBIDDEN) or any(has_tokens(item) for item in value.values())
        if isinstance(value, list):
            return any(has_tokens(item) for item in value)
        return False
    if has_tokens(configuration):
        raise ValueError('User authorization/token fields must never be bundled in a package')
    installed = configuration['installed']
    return {'installed': {key: installed[key] for key in CLIENT_KEYS}}


def _write(path: Path, content: str, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    path.chmod(mode)


def _copy_sources(source: Path, destination: Path):
    for path in sorted(source.rglob('*')):
        relative = path.relative_to(source)
        if '__pycache__' in relative.parts:
            continue
        permitted = path.suffix == '.py' or (
            relative.parts[0] == 'web' and path.suffix in ('.html', '.css', '.js')) or (
            relative.parts[0] == 'assets' and path.suffix == '.svg')
        if path.is_symlink():
            raise ValueError('Application source must not contain symbolic links')
        if path.is_file() and permitted:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o644)


def _copy_vendor(source: Path, destination: Path):
    if not source.is_dir() or source.is_symlink():
        raise ValueError('Provide an existing vendor directory prepared from the dependency lock')
    count = 0
    for path in sorted(source.rglob('*')):
        relative = path.relative_to(source)
        if path.is_symlink():
            raise ValueError('Vendor tree must not contain symbolic links')
        if any(part in ('__pycache__', 'bin', 'files2gdrive') for part in relative.parts):
            continue
        if path.suffix in ('.pyc', '.pyo', '.pth') or path.name == 'direct_url.json' or path.name.startswith('__editable__'):
            continue
        if path.is_file():
            if path.name in ('token.json', 'credentials.json') or path.name.startswith('client_secret'):
                raise ValueError('Vendor tree contains a credential-like file; inspect the prepared dependencies')
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o644)
            count += 1
        elif not path.is_dir():
            raise ValueError('Vendor tree may contain only regular files and directories')
    if not count:
        raise ValueError('Vendor tree is empty')


def build_package(vendor_dir, oauth_client, output_dir=None, version='0.1.0~alpha3', architecture='amd64', *, source_root=None):
    if not isinstance(version, str) or not VERSION.fullmatch(version) or version.endswith('-'):
        raise ValueError('Invalid Debian package version')
    if architecture != 'amd64':
        raise ValueError('This initial package supports Ubuntu 24.04 amd64 only')
    source = Path(source_root) if source_root else REPOSITORY / 'src' / 'files2gdrive'
    if not source.is_dir() or source.is_symlink():
        raise ValueError('Application source directory is missing or unsafe')
    client = client_configuration(Path(oauth_client))
    output = Path(output_dir) if output_dir else REPOSITORY / 'dist'
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / f'files2gdrive_{version}_{architecture}.deb'
    manifest = {}
    with tempfile.TemporaryDirectory(prefix='files2gdrive-deb-') as temporary:
        tree = Path(temporary) / 'package'
        app = tree / 'usr/lib/files2gdrive'
        _copy_sources(source, app / 'app/files2gdrive')
        _copy_vendor(Path(vendor_dir), app / 'vendor')
        _write(tree / 'usr/bin/files2gdrive', WRAPPER, 0o755)
        _write(tree / 'usr/share/files2gdrive/VERSION', version + '\n')
        _write(tree / 'usr/share/files2gdrive/oauth-client.json', json.dumps(client, indent=2) + '\n')
        _write(tree / 'usr/share/applications/files2gdrive.desktop', DESKTOP.format(arguments='', extra=''))
        _write(tree / 'etc/xdg/autostart/files2gdrive.desktop',
               DESKTOP.format(arguments=' --background', extra='X-GNOME-Autostart-enabled=true\n'))
        icon = source / 'assets/files2gdrive.svg'
        if not icon.is_file() or icon.is_symlink():
            raise ValueError('Application icon is missing')
        target = tree / 'usr/share/icons/hicolor/scalable/apps/files2gdrive.svg'
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(icon, target)
        _write(tree / 'usr/share/doc/files2gdrive/README.Debian', README)
        project_license = REPOSITORY / 'LICENSE'
        if not project_license.is_file() or project_license.is_symlink():
            raise ValueError('Project LICENSE is missing or unsafe')
        _write(tree / 'usr/share/doc/files2gdrive/copyright',
               project_license.read_text() + '\n\n'
               'Third-party Python dependencies retain their respective copyright and\n'
               'licenses. Their METADATA and license files are preserved beneath\n'
               '/usr/lib/files2gdrive/vendor/*.dist-info/.\n')
        dependency_lock = REPOSITORY / 'packaging/requirements-release.lock'
        if dependency_lock.is_file():
            _write(tree / 'usr/share/doc/files2gdrive/requirements-release.lock', dependency_lock.read_text())
        installed_size = sum(path.stat().st_size for path in tree.rglob('*') if path.is_file())
        control = f'''Package: files2gdrive
Version: {version}
Section: utils
Priority: optional
Architecture: {architecture}
Maintainer: Files2GDrive contributors <files2gdrive@localhost>
Depends: python3 (>= 3.12), python3 (<< 3.13), python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, libnotify-bin, xdg-utils
Recommends: gnome-shell-extension-appindicator, gnome-keyring
Installed-Size: {(installed_size + 1023) // 1024}
Description: Local Ubuntu to Google Drive backup with desktop controls
 Monitors selected local folders, queues backup operations and provides a local
 web configuration page, system tray controls and desktop notifications.
 This initial build targets Ubuntu 24.04 amd64 and requires user Google OAuth.
'''
        _write(tree / 'DEBIAN/control', control)
        _write(tree / 'DEBIAN/conffiles', '/etc/xdg/autostart/files2gdrive.desktop\n')
        checksums = []
        for path in sorted(tree.rglob('*')):
            if path.is_dir():
                path.chmod(0o755)
            elif 'DEBIAN' not in path.relative_to(tree).parts:
                checksum = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
                checksums.append(f'{checksum}  {path.relative_to(tree)}')
                manifest[str(path.relative_to(tree))] = hashlib.sha256(path.read_bytes()).hexdigest()
        _write(tree / 'DEBIAN/md5sums', '\n'.join(checksums) + '\n')
        subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(tree), str(artifact)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    artifact.with_suffix('.deb.sha256').write_text(f'{digest}  {artifact.name}\n')
    artifact.with_suffix('.deb.manifest.json').write_text(json.dumps(
        dict(package='files2gdrive', version=version, architecture=architecture, sha256=digest, files=manifest),
        sort_keys=True, indent=2) + '\n')
    return artifact


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vendor-dir', type=Path, required=True)
    parser.add_argument('--oauth-client', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=REPOSITORY / 'dist')
    parser.add_argument('--version', default='0.1.0~alpha3')
    parser.add_argument('--architecture', default='amd64')
    args = parser.parse_args(argv)
    try:
        artifact = build_package(args.vendor_dir, args.oauth_client, args.output_dir, args.version, args.architecture)
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Cannot build package: {error}\n')
    print(artifact)


if __name__ == '__main__':
    main()
