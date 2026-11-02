"""Read-only discovery and downloads of Files2GDrive blob backups.

Descriptions and app properties are untrusted remote input. Reject malformed
metadata and ambiguous identities rather than silently selecting a different
copy. The restore writer separately checks bytes before publishing files.
"""
import json
import re
from uuid import UUID

from .drive import DriveError, FOLDER, _key, _quote, _safe_error

FIELDS = 'id,name,mimeType,trashed,description,appProperties,size,md5Checksum'
_MD5 = re.compile(r'[0-9a-fA-F]{32}\Z')
_DRIVE_ID = re.compile(r'[A-Za-z0-9_-]{1,256}\Z')


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('Expected a canonical backup UUID')
    return value


def _size(value):
    if isinstance(value, bool):
        raise ValueError('Invalid remote file size')
    if isinstance(value, str):
        if not value.isascii() or not value.isdigit():
            raise ValueError('Invalid remote file size')
        value = int(value)
    if not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise ValueError('Invalid remote file size')
    return value


def _label(value, fallback):
    return value if isinstance(value, str) and value.strip() and '\x00' not in value and len(value) <= 255 else fallback


class DriveRecovery:
    def __init__(self, service, download_factory=None):
        self.service = service
        self.download_factory = download_factory

    def _execute(self, request):
        return request.execute(num_retries=2)

    @staticmethod
    def _entry(item):
        from .restore import validate_metadata
        try:
            if item.get('trashed') or item.get('mimeType') == FOLDER:
                raise ValueError('Expected a non-trashed regular backup file')
            drive_id = item['id']
            if not isinstance(drive_id, str) or not _DRIVE_ID.fullmatch(drive_id):
                raise ValueError('Invalid remote identity')
            raw = json.loads(item['description'])
            metadata = validate_metadata(raw)
            properties = item['appProperties']
            if (properties.get('f2g_version') != '1'
                    or properties.get('f2g_computer') != metadata['computer_id']
                    or properties.get('f2g_root') != metadata['root_id']
                    or properties.get('f2g_key') != _key('file:' + metadata['computer_id'] + ':' + metadata['file_id'])):
                raise ValueError('Backup identity mismatch')
            checksum = item['md5Checksum']
            if not isinstance(checksum, str) or not _MD5.fullmatch(checksum):
                raise ValueError('Expected checksum for a binary backup')
            entry = dict(metadata, drive_id=drive_id, size=_size(item['size']), md5Checksum=checksum.lower())
            # Labels are advisory only and never used as filesystem paths.
            for field in ('computer_name', 'root_name', 'folder_name'):
                if field in raw:
                    entry[field] = _label(raw[field], None)
            return entry
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise DriveError('Google Drive: backup metadata is invalid or incompatible') from None

    def _catalog(self, computer_id=None, root_id=None):
        clauses = ["trashed = false", "mimeType != '%s'" % FOLDER,
                   "appProperties has { key='f2g_version' and value='1' }"]
        if computer_id is not None:
            _uuid(computer_id)
            clauses.append("appProperties has { key='f2g_computer' and value='%s' }" % _quote(computer_id))
        if root_id is not None:
            _uuid(root_id)
            clauses.append("appProperties has { key='f2g_root' and value='%s' }" % _quote(root_id))
        entries = []
        identities = set()
        drive_ids = set()
        seen_tokens = set()
        token = None
        try:
            while True:
                result = self._execute(self.service.files().list(
                    q=' and '.join(clauses), spaces='drive',
                    fields='nextPageToken,files(' + FIELDS + ')', pageSize=100, pageToken=token))
                for item in result.get('files', []):
                    entry = self._entry(item)
                    if ((computer_id is not None and entry['computer_id'] != computer_id)
                            or (root_id is not None and entry['root_id'] != root_id)):
                        raise DriveError('Google Drive: catalog returned an unexpected backup identity')
                    identity = (entry['computer_id'], entry['file_id'])
                    if identity in identities or entry['drive_id'] in drive_ids:
                        raise DriveError('Google Drive: duplicate backup identities require review')
                    identities.add(identity)
                    drive_ids.add(entry['drive_id'])
                    entries.append(entry)
                token = result.get('nextPageToken')
                if not token:
                    break
                if not isinstance(token, str) or token in seen_tokens:
                    raise DriveError('Google Drive: invalid catalog pagination')
                seen_tokens.add(token)
            return sorted(entries, key=lambda entry: (entry['computer_id'], entry['root_id'], entry['path'], entry['file_id']))
        except DriveError:
            raise
        except Exception as error:
            raise _safe_error(error) from None

    def list_files(self, computer_id, root_id=None, include_excluded=True):
        entries = self._catalog(computer_id, root_id)
        return entries if include_excluded else [entry for entry in entries if entry['state'] == 'active']

    def list_backups(self):
        backups = {}
        for entry in self._catalog():
            computer = entry['computer_id']
            backup = backups.setdefault(computer, dict(computer_id=computer, name=computer, roots={},
                                                       active_files=0, excluded_files=0))
            backup['name'] = _label(entry.get('computer_name'), backup['name'])
            if entry.get('folder_name'):
                backup['folder_name'] = entry['folder_name']
            root_id = entry['root_id']
            root = backup['roots'].setdefault(root_id, dict(root_id=root_id, name=root_id,
                                                           active_files=0, excluded_files=0))
            root['name'] = _label(entry.get('root_name'), root['name'])
            counter = 'active_files' if entry['state'] == 'active' else 'excluded_files'
            root[counter] += 1
            backup[counter] += 1
        result = []
        for backup in backups.values():
            backup['roots'] = sorted(backup['roots'].values(), key=lambda root: root['root_id'])
            result.append(backup)
        return sorted(result, key=lambda backup: backup['computer_id'])

    def download(self, entry, stream):
        """Download selected bytes, rejecting metadata changes before/after transfer.

        The caller owns stream and must verify checksum/size before publishing it.
        No remote writes occur, including when downloading retained files.
        """
        try:
            drive_id = entry['drive_id']
            if not isinstance(drive_id, str) or not _DRIVE_ID.fullmatch(drive_id):
                raise DriveError('Google Drive: invalid selected remote identity')
            def current():
                return self._entry(self._execute(self.service.files().get(fileId=drive_id, fields=FIELDS)))
            fields = ('version', 'computer_id', 'root_id', 'file_id', 'path', 'mode', 'mtime_ns',
                      'state', 'deleted_at', 'drive_id', 'size', 'md5Checksum')
            def verify(actual):
                if any(actual.get(field) != entry.get(field) for field in fields):
                    raise DriveError('Google Drive: selected file changed; refresh the catalog before restoring')
            verify(current())
            factory = self.download_factory
            if factory is None:
                from googleapiclient.http import MediaIoBaseDownload
                factory = MediaIoBaseDownload
            downloader = factory(stream, self.service.files().get_media(fileId=drive_id), chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=2)
            verify(current())
        except DriveError:
            raise
        except Exception as error:
            raise _safe_error(error) from None
