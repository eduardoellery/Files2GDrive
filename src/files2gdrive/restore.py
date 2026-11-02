"""Restore untrusted remote metadata into a new Linux directory, without overwrite."""
from contextlib import contextmanager
import hashlib
import logging
import math
import os
from pathlib import Path
import re
from uuid import UUID, uuid4

log = logging.getLogger('files2gdrive')
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_MAX_MTIME_NS = 2 ** 63 - 1


def validate_metadata(metadata):
    """Validate the version-one regular-file schema before trusting remote paths."""
    if not isinstance(metadata, dict) or type(metadata.get('version')) is not int or metadata['version'] != 1:
        raise ValueError('Unsupported backup metadata version')
    clean = {'version': 1}
    for field in ('file_id', 'computer_id', 'root_id'):
        value = metadata.get(field)
        if not isinstance(value, str):
            raise ValueError('Backup identifiers must be UUID strings')
        try:
            identifier = str(UUID(value))
        except (ValueError, AttributeError):
            raise ValueError('Invalid backup UUID') from None
        if value != identifier:
            raise ValueError('Backup UUID must use canonical form')
        clean[field] = identifier
    path = metadata.get('path')
    if (not isinstance(path, str) or not path or path.startswith('/') or '\x00' in path
            or any(part in ('', '.', '..') for part in path.split('/'))):
        raise ValueError('Expected a safe relative backup path')
    try:
        if len(os.fsencode(path)) > 4095 or any(len(os.fsencode(part)) > 255 for part in path.split('/')):
            raise ValueError('Backup path exceeds supported Linux limits')
    except UnicodeEncodeError:
        raise ValueError('Backup path cannot be encoded on this system') from None
    clean['path'] = path
    mode = metadata.get('mode')
    if type(mode) is not int or not 0 <= mode <= 0o777:
        raise ValueError('Only ordinary permission bits may be restored')
    clean['mode'] = mode
    mtime = metadata.get('mtime_ns')
    if type(mtime) is not int or not 0 <= mtime <= _MAX_MTIME_NS:
        raise ValueError('Modification time is outside the supported range (1970–2262)')
    clean['mtime_ns'] = mtime
    state = metadata.get('state')
    if state not in ('active', 'excluded'):
        raise ValueError('Only active or retained backup files can be restored')
    clean['state'] = state
    if state == 'excluded':
        deleted = metadata.get('deleted_at')
        if type(deleted) not in (int, float) or deleted < 0:
            raise ValueError('Retained backup must have a valid deletion timestamp')
        try:
            finite = math.isfinite(deleted)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError('Retained backup must have a valid deletion timestamp')
        clean['deleted_at'] = deleted
    return clean


@contextmanager
def _open_absolute_directory(path):
    """Open each component without following any symlink, including ancestors."""
    path = Path(path).absolute()
    descriptor = os.open('/', _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            if part in ('.', '..'):
                raise ValueError('Unsafe destination path')
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _parent(root_fd, parts):
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


class RestoreManager:
    def __init__(self, remote):
        self.remote = remote

    def restore(self, entries, destination):
        """Restore selected entries to a *new* directory, never overwriting a file.

        Download failures leave completed files available but remove incomplete
        temporary files. The fresh destination is retained for inspection/retry.
        A call may contain only a single computer/root to avoid path collisions.
        """
        validated = []
        identities = set()
        paths = set()
        file_ids = set()
        for entry in entries:
            metadata = validate_metadata(entry)
            size = entry.get('size')
            checksum = entry.get('md5Checksum')
            drive_id = entry.get('drive_id')
            if type(size) is not int or size < 0:
                raise ValueError('Backup requires a nonnegative content size')
            if not isinstance(checksum, str) or re.fullmatch('[0-9a-fA-F]{32}', checksum) is None:
                raise ValueError('Backup requires a valid MD5 content checksum')
            if not isinstance(drive_id, str) or not drive_id or '\x00' in drive_id:
                raise ValueError('Backup requires a remote file identifier')
            if metadata['path'] in paths or metadata['file_id'] in file_ids:
                raise ValueError('Duplicate restore path or file identity')
            paths.add(metadata['path']); file_ids.add(metadata['file_id'])
            identities.add((metadata['computer_id'], metadata['root_id']))
            validated.append(dict(metadata, size=size, md5Checksum=checksum.lower(), drive_id=drive_id))
        if len(identities) > 1:
            raise ValueError('Restore one computer and root at a time')
        for path in paths:
            parts = path.split('/')
            if any('/'.join(parts[:index]) in paths for index in range(1, len(parts))):
                raise ValueError('A restore file conflicts with a parent directory')
        destination = Path(destination).absolute()
        if destination.name in ('', '.', '..'):
            raise ValueError('Choose a new destination directory')
        restored = []
        with _open_absolute_directory(destination.parent) as parent_fd:
            os.mkdir(destination.name, mode=0o700, dir_fd=parent_fd)  # existing destinations always fail
            root_fd = os.open(destination.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            try:
                for entry in validated:
                    self._restore_file(root_fd, entry)
                    restored.append(entry['path'])
            finally:
                os.close(root_fd)
        return restored

    def _restore_file(self, root_fd, entry):
        parts = entry['path'].split('/')
        temporary = '.files2gdrive-restore-' + uuid4().hex
        with _parent(root_fd, parts[:-1]) as directory:
            created = False
            try:
                descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     0o600, dir_fd=directory)
                created = True
                with os.fdopen(descriptor, 'w+b') as stream:
                    self.remote.download(entry, stream)
                    stream.flush()
                    if os.fstat(stream.fileno()).st_size != entry['size']:
                        raise ValueError('Downloaded backup size does not match its metadata')
                    stream.seek(0)
                    digest = hashlib.md5(usedforsecurity=False)
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(block)
                    if digest.hexdigest() != entry['md5Checksum']:
                        raise ValueError('Downloaded backup checksum does not match its metadata')
                    os.fchmod(stream.fileno(), entry['mode'])
                    os.utime(stream.fileno(), ns=(entry['mtime_ns'], entry['mtime_ns']))
                    os.fsync(stream.fileno())
                    os.link(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory,
                            follow_symlinks=False)  # atomic publication, EEXIST means no overwrite
                    os.fsync(directory)
                log.info('backup restored', extra={'operation': 'restore', 'root_id': entry['root_id'],
                                                  'path': entry['path'], 'result': 'success'})
            except Exception:
                log.warning('restore failed', extra={'operation': 'restore', 'root_id': entry['root_id'],
                                                     'path': entry['path'], 'result': 'error'})
                raise
            finally:
                if created:
                    os.unlink(temporary, dir_fd=directory)
