"""Google Drive v3 adapter. Tokens and upload-session URLs are never persisted here.

Small snapshots use one multipart request; large uploads resume in chunks
within one invocation. A failed invocation is retried with the same pre-generated
Drive ID, preventing duplicate creates; byte-level resume across process restarts
is not yet implemented.
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import PurePosixPath
import socket
import time
import threading
from uuid import UUID

FOLDER = 'application/vnd.google-apps.folder'
FIELDS = 'id,name,parents,trashed,description,appProperties,mimeType'
FOLDER_CACHE_TTL = 30
FOLDER_CACHE_LIMIT = 4096
MULTIPART_UPLOAD_LIMIT = 5 * 1024 * 1024


class DriveError(RuntimeError):
    """Sanitized remote error suitable for persistent queue diagnostics."""
    def __init__(self, message, *, requires_auth=False, category='unknown'):
        super().__init__(message)
        self.requires_auth = requires_auth
        self.category = category


def _status(error):
    return getattr(getattr(error, 'resp', None), 'status', None)


def _safe_error(error):
    status = _status(error)
    requires_auth = status == 401 or (type(error).__name__ == 'RefreshError'
                                    and type(error).__module__.startswith('google.auth'))
    reasons = set()
    try:
        content = getattr(error, 'content', b'')
        parsed = json.loads(content) if len(content) < 65536 else {}
        reasons = {item.get('reason') for item in parsed.get('error', {}).get('errors', []) if isinstance(item, dict)}
    except (ValueError, TypeError, AttributeError):
        pass
    category = 'auth' if requires_auth else 'unknown'
    if 'storageQuotaExceeded' in reasons:
        category = 'quota'
    elif status == 403 and not reasons.intersection({'rateLimitExceeded', 'userRateLimitExceeded', 'dailyLimitExceeded'}):
        category = 'access'
    label = {401: 'authentication required', 403: 'permission or quota failure',
             404: 'remote item unavailable', 409: 'remote create conflict',
             429: 'rate limit reached'}.get(status, 'request failed')
    if category == 'quota': label = 'storage quota exceeded'
    return DriveError(f'Google Drive: {label}' + (f' (HTTP {status})' if isinstance(status, int) else ''),
                      requires_auth=requires_auth, category=category)


def _key(value):
    return sha256(value.encode()).hexdigest()


def _quote(value):
    return value.replace('\\', '\\\\').replace("'", "\\'")


class GoogleDriveRemote:
    def __init__(self, database, service, folder_name='Files2GDrive', computer_name=None,
                 media_factory=None, service_factory=None):
        self.db = database
        self.sql = database.connection
        self.service = service
        self.folder_name = folder_name
        self.computer_id = database.get('computer_id')
        self.computer_name = computer_name or database.get('computer_name') or socket.gethostname()
        self.media_factory = media_factory
        self.service_factory = service_factory
        self._upload_pool = None
        self._worker_local = threading.local()
        self._worker_services = []
        self._worker_services_lock = threading.Lock()
        self.progress_callback = None
        self._folder_cache = {}
        self._unused_ids = []
        self.sql.execute('''CREATE TABLE IF NOT EXISTS drive_objects(
            object_key TEXT PRIMARY KEY, drive_id TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0)''')
        self.sql.commit()
        try:
            account = self._execute(service.about().get(fields='user(permissionId)'))['user']['permissionId']
        except Exception as error:
            raise _safe_error(error) from None
        previous = database.get('drive_account_id')
        if previous and previous != account:
            raise DriveError('Google Drive: this backup state belongs to a different Google account')
        database.set('drive_account_id', account)

    def _execute(self, request):
        return request.execute(num_retries=2)

    def _get(self, drive_id):
        try:
            return self._execute(self.service.files().get(fileId=drive_id, fields=FIELDS))
        except Exception as error:
            if _status(error) == 404:
                return None
            raise

    def _remember(self, key, drive_id, confirmed):
        with self.sql:
            self.sql.execute('INSERT OR REPLACE INTO drive_objects VALUES(?,?,?)',
                             (key, drive_id, int(confirmed)))

    def _find(self, key):
        row = self.sql.execute('SELECT * FROM drive_objects WHERE object_key=?', (key,)).fetchone()
        if row:
            item = self._get(row['drive_id'])
            if item and not item.get('trashed'):
                if item.get('appProperties', {}).get('f2g_key') != key:
                    raise DriveError('Google Drive: stored remote identity does not match backup')
                return item, row['drive_id']
            if not item and not row['confirmed']:
                return None, row['drive_id']  # retry uncertain create with original ID
        token = None
        found = []
        while True:
            result = self._execute(self.service.files().list(
                q="trashed = false and appProperties has { key='f2g_key' and value='%s' }" % _quote(key),
                spaces='drive', fields='nextPageToken,files(' + FIELDS + ')',
                pageToken=token, pageSize=100))
            found.extend(result.get('files', []))
            token = result.get('nextPageToken')
            if not token:
                break
        if len(found) > 1:
            raise DriveError('Google Drive: duplicate backup identities require review')
        if found:
            self._remember(key, found[0]['id'], True)
            return found[0], found[0]['id']
        return None, None

    def _allocate(self, key):
        if not self._unused_ids:
            result = self._execute(self.service.files().generateIds(count=100, space='drive', type='files'))
            self._unused_ids = list(reversed(result['ids']))
        drive_id = self._unused_ids.pop()
        self._remember(key, drive_id, False)  # commit BEFORE issuing create
        return drive_id

    def _cache_folder(self, key, drive_id):
        # Bound memory and never extend freshness merely because an entry is hit.
        if key not in self._folder_cache and len(self._folder_cache) >= FOLDER_CACHE_LIMIT:
            self._folder_cache.pop(next(iter(self._folder_cache)))
        self._folder_cache[key] = (drive_id, time.monotonic() + FOLDER_CACHE_TTL)

    def _folder(self, logical_key, name, parent=None):
        key = _key(logical_key)
        cached = self._folder_cache.get(key)
        if cached:
            if time.monotonic() < cached[1]:
                return cached[0]
            del self._folder_cache[key]
        item, drive_id = self._find(key)
        if item:
            if item.get('mimeType') != FOLDER:
                raise DriveError('Google Drive: backup folder identity has an invalid type')
            self._cache_folder(key, item['id'])
            return item['id']
        drive_id = drive_id or self._allocate(key)
        body = dict(id=drive_id, name=name, mimeType=FOLDER,
                    appProperties={'f2g_key': key, 'f2g_version': '1'})
        if parent:
            body['parents'] = [parent]
        self._execute(self.service.files().create(body=body, fields='id'))
        self._remember(key, drive_id, True)
        self._cache_folder(key, drive_id)
        return drive_id

    def _computer_folder(self):
        container = self._folder('container:' + self.folder_name, self.folder_name)
        return self._folder('computer:' + self.computer_id, self.computer_name, container)

    def _active_parent(self, root_id, relative):
        row = self.sql.execute('SELECT name FROM roots WHERE id=?', (root_id,)).fetchone()
        if not row:
            raise ValueError('Unknown local backup root')
        parent = self._folder(f'root:{self.computer_id}:{root_id}', row['name'], self._computer_folder())
        prefix = ''
        for component in PurePosixPath(relative).parts[:-1]:
            prefix = f'{prefix}/{component}' if prefix else component
            parent = self._folder(f'directory:{self.computer_id}:{root_id}:{prefix}', component, parent)
        return parent

    @staticmethod
    def _validate(metadata):
        relative = metadata['path']
        parts = relative.split('/')
        if not relative or relative.startswith('/') or any(part in ('', '.', '..') for part in parts):
            raise ValueError('Expected a safe relative backup path')
        if '\x00' in relative:
            raise ValueError('Invalid backup path')
        mode = metadata['mode']
        if not isinstance(mode, int) or not 0 <= mode <= 0o777:
            raise ValueError('Only ordinary Linux permission bits are supported')

    def _unattempted_local_file(self, file_id, key, metadata):
        """Only bypass discovery when durable local state proves a first create.

        Every Drive create reserves its ID in drive_objects before sending.
        Imported backups have both that mapping and a remote fingerprint.
        Missing/ambiguous history always takes the conservative lookup path.
        """
        try:
            if str(UUID(file_id)) != file_id:
                return False
        except (ValueError, TypeError, AttributeError):
            return False
        if self.db.get('reconnect_pending') is not None:
            return False
        return self.sql.execute("""SELECT 1 FROM files f JOIN jobs j ON j.file_id=f.id
            WHERE f.id=? AND f.root_id=? AND f.path=? AND f.state='active'
              AND f.remote_fingerprint IS NULL AND f.device IS NOT NULL AND f.inode IS NOT NULL
              AND j.operation='upload' AND j.attempts=0
              AND NOT EXISTS(SELECT 1 FROM drive_objects WHERE object_key=?)""",
            (file_id, metadata['root_id'], metadata['path'], key)).fetchone() is not None

    def _prepare_upload(self, file_id, stream, metadata):
        """Resolve identities and commit reserved IDs on the database owner."""
        self._validate(metadata)
        parent = self._active_parent(metadata['root_id'], metadata['path'])
        key = _key('file:' + self.computer_id + ':' + file_id)
        if self._unattempted_local_file(file_id, key, metadata):
            item, drive_id = None, None
        else:
            item, drive_id = self._find(key)
        drive_id = drive_id or self._allocate(key)
        root = self.sql.execute('SELECT name FROM roots WHERE id=?', (metadata['root_id'],)).fetchone()
        description = json.dumps(dict(version=1, file_id=file_id, computer_id=self.computer_id,
                                      computer_name=self.computer_name, root_name=root['name'],
                                      folder_name=self.folder_name, state='active', **metadata), separators=(',', ':'))
        body = dict(name=PurePosixPath(metadata['path']).name, description=description,
                    appProperties={'f2g_key': key, 'f2g_version': '1',
                                   'f2g_computer': self.computer_id, 'f2g_root': metadata['root_id']})
        options = dict(body=body, fields='id')
        if item:
            options['fileId'] = drive_id
            old_parents = item.get('parents', [])
            if parent not in old_parents:
                options['addParents'] = parent
                if old_parents:
                    options['removeParents'] = ','.join(old_parents)
        else:
            body.update(id=drive_id, parents=[parent])
        return dict(key=key, drive_id=drive_id, update=bool(item), options=options,
                    stream=stream, path=metadata['path'])

    def _transfer_upload(self, service, plan):
        """Transfer one private snapshot. No SQLite access is allowed here."""
        stream = plan['stream']
        factory = self.media_factory
        if factory is None:
            from googleapiclient.http import MediaIoBaseUpload
            factory = MediaIoBaseUpload
        total = stream.seek(0, 2)
        stream.seek(0)
        resumable = total > MULTIPART_UPLOAD_LIMIT
        media = factory(stream, mimetype='application/octet-stream', chunksize=8 * 1024 * 1024, resumable=resumable)
        options = dict(plan['options'], media_body=media)
        request = (service.files().update if plan['update'] else service.files().create)(**options)
        if not resumable:
            if self.progress_callback:
                self.progress_callback(dict(path=plan['path'], bytes=0, total=total))
            self._execute(request)
            if self.progress_callback:
                self.progress_callback(dict(path=plan['path'], bytes=total, total=total))
        else:
            response = None
            while response is None:
                if self.progress_callback:
                    self.progress_callback(dict(path=plan['path'], bytes=request.resumable_progress, total=total))
                status, response = request.next_chunk(num_retries=2)
                if self.progress_callback:
                    self.progress_callback(dict(path=plan['path'], bytes=total if response else request.resumable_progress, total=total))

    def upload(self, file_id, stream, metadata):
        self._validate(metadata)
        try:
            plan = self._prepare_upload(file_id, stream, metadata)
            self._transfer_upload(self.service, plan)
            self._remember(plan['key'], plan['drive_id'], True)
        except DriveError:
            self._folder_cache.clear()
            raise
        except Exception as error:
            self._folder_cache.clear()
            raise _safe_error(error) from None

    def prepare_directories(self, metadatas):
        from .drive_directories import prepare_directories
        return prepare_directories(self, metadatas)

    def upload_many(self, items):
        """Upload at most four snapshots using isolated HTTP clients.

        Preparation and confirmation happen synchronously on the SQLite owner;
        workers receive immutable plans and never touch local persistent state.
        Each aligned result is None on success or a sanitized exception.
        """
        if len(items) > 4:
            raise ValueError('An upload batch may contain at most four files')
        if len({item['file_id'] for item in items}) != len(items):
            raise ValueError('Duplicate file identities in upload batch')
        if not items:
            return []
        if self.service_factory is None:
            results = []
            auth_error = None
            for item in items:
                if auth_error:
                    results.append(auth_error)
                    continue
                try:
                    self.upload(item['file_id'], item['stream'], item['metadata'])
                    results.append(None)
                except Exception as error:
                    safe = error if isinstance(error, (DriveError, ValueError)) else _safe_error(error)
                    results.append(safe)
                    if getattr(safe, 'requires_auth', False):
                        auth_error = safe
            return results
        results = [None] * len(items)
        plans = []
        try:
            prepare = getattr(self, 'prepare_directories', None)
            if prepare:
                prepare([item['metadata'] for item in items])
        except Exception as error:
            self._folder_cache.clear()
            safe = error if isinstance(error, (DriveError, ValueError)) else _safe_error(error)
            return [safe] * len(items)
        auth_error = None
        for index, item in enumerate(items):
            if auth_error:
                results[index] = auth_error
                continue
            try:
                plans.append((index, self._prepare_upload(item['file_id'], item['stream'], item['metadata'])))
            except Exception as error:
                safe = error if isinstance(error, (DriveError, ValueError)) else _safe_error(error)
                results[index] = safe
                if getattr(safe, 'requires_auth', False):
                    auth_error = safe
        if auth_error:
            # No transfer has started yet, so stop the complete prepared group.
            for index, plan in plans:
                results[index] = auth_error
            self._folder_cache.clear()
            return results

        def transfer(plan):
            try:
                service = getattr(self._worker_local, 'service', None)
                if service is None:
                    service = self.service_factory()
                    self._worker_local.service = service
                    with self._worker_services_lock:
                        self._worker_services.append(service)
                self._transfer_upload(service, plan)
                return None
            except Exception as error:
                return error if isinstance(error, DriveError) else _safe_error(error)

        if self._upload_pool is None:
            self._upload_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='files2gdrive-upload')
        futures = [(index, plan, self._upload_pool.submit(transfer, plan)) for index, plan in plans]
        for index, plan, future in futures:
            result = future.result()
            results[index] = result
            if result is None:
                self._remember(plan['key'], plan['drive_id'], True)
        if any(result is not None for result in results):
            self._folder_cache.clear()
        return results

    def close(self):
        """Wait for in-flight transfers before releasing each private transport."""
        if self._upload_pool is not None:
            self._upload_pool.shutdown(wait=True)
            self._upload_pool = None
        for service in [*self._worker_services, self.service]:
            close = getattr(service, 'close', None)
            if close:
                try:
                    close()
                except Exception:
                    pass
        self._worker_services.clear()
        self._worker_local = threading.local()

    def retain(self, file_id, deleted_at):
        try:
            key = _key('file:' + self.computer_id + ':' + file_id)
            item, _ = self._find(key)
            if not item:
                return  # never recreate an externally deleted backup
            metadata = json.loads(item.get('description', '{}'))
            if metadata.get('file_id') != file_id or metadata.get('version') != 1:
                raise DriveError('Google Drive: backup metadata missing or incompatible')
            excluded = self._folder('excluded:' + self.computer_id, 'Excluídos', self._computer_folder())
            root = self._folder('excluded-root:' + self.computer_id + ':' + metadata['root_id'],
                                metadata['root_id'], excluded)
            parent = self._folder('excluded-file:' + self.computer_id + ':' + file_id, file_id, root)
            metadata.update(state='excluded', deleted_at=deleted_at)
            options = dict(fileId=item['id'], body={'description': json.dumps(metadata)}, fields='id')
            old_parents = item.get('parents', [])
            if parent not in old_parents:
                options['addParents'] = parent
                if old_parents:
                    options['removeParents'] = ','.join(old_parents)
            self._execute(self.service.files().update(**options))
        except DriveError:
            self._folder_cache.clear()
            raise
        except Exception as error:
            self._folder_cache.clear()
            raise _safe_error(error) from None

    def trash(self, file_id):
        try:
            item, _ = self._find(_key('file:' + self.computer_id + ':' + file_id))
            if item:
                self._execute(self.service.files().update(fileId=item['id'], body={'trashed': True}, fields='id'))
        except DriveError:
            self._folder_cache.clear()
            raise
        except Exception as error:
            self._folder_cache.clear()
            raise _safe_error(error) from None
