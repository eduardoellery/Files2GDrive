"""Prepare Drive directory dependencies in bounded, synchronous HTTP batches.

Callbacks only collect responses. The caller thread owns all SQLite reads and
writes, including durable ID reservations before any remote create.
"""
from pathlib import PurePosixPath
import time

from .drive import DriveError, FOLDER, FIELDS, _key, _quote, _safe_error, _status

BATCH_LIMIT = 100


def _batch(remote, requests):
    results = {}
    for offset in range(0, len(requests), BATCH_LIMIT):
        chunk = requests[offset:offset + BATCH_LIMIT]
        batch = remote.service.new_batch_http_request()
        def completed(request_id, response, exception):
            results[request_id] = (response, exception)
        for key, request in chunk:
            batch.add(request, callback=completed, request_id=key)
        batch.execute()
        for key, _ in chunk:
            if key not in results:
                raise DriveError('Google Drive: incomplete directory batch response')
    return results


def _nodes(remote, metadatas):
    nodes = {}
    container = _key('container:' + remote.folder_name)
    computer = _key('computer:' + remote.computer_id)
    nodes[container] = (remote.folder_name, None)
    nodes[computer] = (remote.computer_name, container)
    for metadata in metadatas:
        remote._validate(metadata)
        root_id = metadata['root_id']
        row = remote.sql.execute('SELECT name FROM roots WHERE id=?', (root_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown local backup root')
        parent = _key(f'root:{remote.computer_id}:{root_id}')
        nodes[parent] = (row['name'], computer)
        prefix = ''
        for component in PurePosixPath(metadata['path']).parts[:-1]:
            prefix = f'{prefix}/{component}' if prefix else component
            key = _key(f'directory:{remote.computer_id}:{root_id}:{prefix}')
            nodes[key] = (component, parent)
            parent = key
    return nodes


def prepare_directories(remote, metadatas):
    """Warm directory IDs for metadata paths, respecting parent dependencies.

    Legacy/fake services without batch support retain the original behavior.
    Empty directories are intentionally outside this file-derived preparation.
    """
    metadatas = list(metadatas)
    if not metadatas:
        return
    if not callable(getattr(remote.service, 'new_batch_http_request', None)):
        for metadata in metadatas:
            remote._validate(metadata)
            remote._active_parent(metadata['root_id'], metadata['path'])
        return
    try:
        nodes = _nodes(remote, metadatas)
        resolved = {}
        reserved = {}
        mappings = {}
        lookups = []
        needs_search = []
        now = time.monotonic()
        for key in nodes:
            cached = remote._folder_cache.get(key)
            if cached and now < cached[1]:
                resolved[key] = cached[0]
                continue
            if cached:
                del remote._folder_cache[key]
            row = remote.sql.execute('SELECT * FROM drive_objects WHERE object_key=?', (key,)).fetchone()
            if row is None:
                needs_search.append(key)
            else:
                mappings[key] = dict(row)
                lookups.append((key, remote.service.files().get(fileId=row['drive_id'], fields=FIELDS)))
        for key, (item, error) in _batch(remote, lookups).items():
            row = mappings[key]
            if error and _status(error) != 404:
                raise error
            if item and not item.get('trashed'):
                _validate_folder(item, key)
                resolved[key] = item['id']
            elif not item and not row['confirmed']:
                reserved[key] = row['drive_id']
            else:
                needs_search.append(key)
        # Search all unknown identities together; preserve pagination and the
        # duplicate-identity guard instead of assuming a missing local mapping.
        found = {key: [] for key in needs_search}
        pages = {key: None for key in needs_search}
        while pages:
            requests = [(key, remote.service.files().list(
                q="trashed = false and appProperties has { key='f2g_key' and value='%s' }" % _quote(key),
                spaces='drive', fields='nextPageToken,files(' + FIELDS + ')',
                pageToken=token, pageSize=100)) for key, token in pages.items()]
            pages = {}
            for key, (response, error) in _batch(remote, requests).items():
                if error:
                    raise error
                found[key].extend(response.get('files', []))
                if response.get('nextPageToken'):
                    pages[key] = response['nextPageToken']
        for key, items in found.items():
            if len(items) > 1:
                raise DriveError('Google Drive: duplicate backup identities require review')
            if items:
                _validate_folder(items[0], key)
                resolved[key] = items[0]['id']
                remote._remember(key, items[0]['id'], True)
        for key, drive_id in resolved.items():
            remote._cache_folder(key, drive_id)
        pending = set(nodes) - set(resolved)
        while pending:
            ready = sorted(key for key in pending if nodes[key][1] is None or nodes[key][1] in resolved)
            if not ready:
                raise DriveError('Google Drive: invalid directory dependencies')
            creates = []
            for key in ready:
                name, parent = nodes[key]
                drive_id = reserved.get(key) or remote._allocate(key)
                reserved[key] = drive_id
                body = dict(id=drive_id, name=name, mimeType=FOLDER,
                            appProperties={'f2g_key': key, 'f2g_version': '1'})
                if parent:
                    body['parents'] = [resolved[parent]]
                creates.append((key, remote.service.files().create(body=body, fields='id')))
            error = None
            for key, (response, failure) in _batch(remote, creates).items():
                if failure:
                    error = error or failure
                    continue
                if not response or response.get('id') != reserved[key]:
                    error = error or DriveError('Google Drive: directory create identity mismatch')
                    continue
                remote._remember(key, reserved[key], True)
                resolved[key] = reserved[key]
                remote._cache_folder(key, reserved[key])
                pending.remove(key)
            if error:
                raise error
    except Exception as error:
        remote._folder_cache.clear()
        if isinstance(error, (DriveError, ValueError)):
            raise
        raise _safe_error(error) from None


def _validate_folder(item, key):
    if item.get('appProperties', {}).get('f2g_key') != key:
        raise DriveError('Google Drive: stored remote identity does not match backup')
    if item.get('mimeType') != FOLDER:
        raise DriveError('Google Drive: backup folder identity has an invalid type')
