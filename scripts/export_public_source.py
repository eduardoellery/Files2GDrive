#!/usr/bin/env python3
"""Export reviewed source files, never local profiles or OAuth credentials."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

TOP = {'README.md', 'LICENSE', 'pyproject.toml', '.gitignore', 'PUBLISHING.md'}
GUIDES = {'ARQUITETURA-PROPOSTA.md', 'REQUISITOS.md', 'GUIA-GOOGLE-DRIVE.md',
          'INSTALACAO-DEB.md', 'INTEGRACAO-DESKTOP.md', 'INTERFACE-WEB.md',
          'RESTAURACAO-E-RECONEXAO.md'}
EXTENSIONS = {'src': {'.py', '.html', '.css', '.js', '.svg'}, 'tests': {'.py'},
              'scripts': {'.py'}, 'packaging': {'.txt', '.lock'}}


def selected_files(root):
    result = []
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        parts = relative.parts
        if any(part.startswith('.') or part == '__pycache__' or part.endswith('.egg-info')
               for part in parts) and str(relative) != '.gitignore':
            continue
        selected = str(relative) in TOP
        selected |= len(parts) > 1 and parts[0] in EXTENSIONS and path.suffix in EXTENSIONS[parts[0]]
        selected |= len(parts) == 2 and parts[0] == 'docs' and parts[1] in GUIDES
        selected |= relative.as_posix() == 'docs/images/visao-geral.png'
        if selected:
            if path.is_symlink():
                raise ValueError('Symlinks are not allowed in public source: ' + str(relative))
            if path.is_file():
                result.append(path)
    return sorted(result)


def export(root, output):
    root = root.resolve()
    files = selected_files(root)
    if not all(root / name in files for name in ('README.md', 'LICENSE', 'pyproject.toml')):
        raise ValueError('Missing public project metadata')
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = []
    with output.open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode='w') as archive:
            for path in files:
                relative = path.relative_to(root).as_posix()
                data = path.read_bytes()
                info = tarfile.TarInfo('Files2GDrive/' + relative)
                info.size = len(data)
                info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                archive.addfile(info, io.BytesIO(data))
                manifest.append(dict(path=relative, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
    output.with_suffix(output.suffix + '.manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    output.with_suffix(output.suffix + '.sha256').write_text(hashlib.sha256(output.read_bytes()).hexdigest() + '  ' + output.name + '\n')
    return len(files)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('dist/files2gdrive-source-0.1.0-alpha3.tar.gz'))
    args = parser.parse_args()
    print(f'{export(Path(__file__).resolve().parents[1], args.output)} public files exported to {args.output}')
