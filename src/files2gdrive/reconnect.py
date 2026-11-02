"""Explicit, reviewed import of remote backup identity into a fresh local state.

Preparation and review are read-only remotely. Confirmation requires the exact
review digest and refuses changed remote/local inputs. No queue work is run here.
"""
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import UUID

from .filesystem import open_source
from .scanner import scan_root


class ReconnectManager:
    def __init__(self, database, recovery, state_directory=None, clock=time.time):
        self.db = database
        self.sql = database.connection
        self.recovery = recovery
        self.state_directory = Path(state_directory).resolve() if state_directory else None
        self.clock = clock

    def _fresh(self):
        if json.loads(self.db.get('global_patterns', '[]')):
            raise ValueError('Reconnect fresh state without filters; configure filters after the reviewed import')
        if (self.sql.execute('SELECT count(*) FROM roots').fetchone()[0]
                or self.sql.execute('SELECT count(*) FROM files').fetchone()[0]
                or self.sql.execute('SELECT count(*) FROM jobs').fetchone()[0]):
            raise ValueError('Reconnect requires a fresh state without local backup roots/files')

    def _build(self, computer_id, mappings):
        self._fresh()
        computer_id = str(UUID(computer_id))
        if not mappings:
            raise ValueError('Explicit root mappings are required')
        canonical = {}
        scans = {}
        local_checksums = {}
        for root_id, path in mappings.items():
            root_id = str(UUID(root_id))
            original = Path(path).absolute()
            if original.is_symlink():
                raise ValueError('Root mapping cannot be a symbolic link')
            path = original.resolve(strict=True)
            if not path.is_dir():
                raise ValueError('Mapped root must be an existing directory')
            if self.state_directory and (path.is_relative_to(self.state_directory)
                                        or self.state_directory.is_relative_to(path)):
                raise ValueError('Mapped roots and private state cannot overlap')
            if any(path.is_relative_to(Path(other)) or Path(other).is_relative_to(path)
                   for other in canonical.values()):
                raise ValueError('Mapped roots cannot overlap')
            scan = scan_root(path)
            if not scan.complete:
                raise ValueError('Mapped root scan is incomplete; resolve access issues before reviewing')
            canonical[root_id] = str(path)
            scans[root_id] = scan
            # Compare bytes as well as metadata after loss of the local database.
            for relative, snapshot in scan.files.items():
                with open_source(path / relative) as stream:
                    before = os.fstat(stream.fileno())
                    if (before.st_size, before.st_mtime_ns, before.st_mode & 0o777,
                            before.st_dev, before.st_ino) != (*snapshot.fingerprint, snapshot.device, snapshot.inode):
                        raise ValueError('Local files changed during review; review again')
                    digest = hashlib.md5(usedforsecurity=False)
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                    after = os.fstat(stream.fileno())
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise ValueError('Local files changed during review; review again')
                local_checksums[root_id, relative] = digest.hexdigest()
        all_entries = self.recovery.list_files(computer_id)
        entries = [entry for entry in all_entries if entry['root_id'] in canonical]
        available = {entry['root_id'] for entry in all_entries}
        if any(root_id not in available for root_id in canonical):
            raise ValueError('A mapped root is not present in the selected remote backup')
        # One historical excluded item and a new active item may share a path.
        # The local table cannot represent both: refuse instead of dropping history.
        paths = set()
        for entry in entries:
            key = entry['root_id'], entry['path']
            if key in paths:
                raise ValueError('Remote paths have multiple retained identities; restore/review them before reconnecting')
            paths.add(key)
        counts = dict(unchanged=0, upload=0, new_local=0, start_retention=0,
                      cancel_retention=0, retained_missing=0, trash_expired=0)
        local_manifest = []
        for root_id, scan in scans.items():
            for relative, snapshot in sorted(scan.files.items()):
                local_manifest.append(dict(root_id=root_id, path=relative, fingerprint=list(snapshot.fingerprint),
                    device=snapshot.device, inode=snapshot.inode, md5=local_checksums[root_id, relative]))
                if (root_id, relative) not in paths:
                    counts['new_local'] += 1
        for entry in entries:
            snapshot = scans[entry['root_id']].files.get(entry['path'])
            if snapshot is None:
                counts['start_retention' if entry['state'] == 'active' else 'retained_missing'] += 1
                if entry['state'] == 'excluded' and entry['deleted_at'] + 30 * 86400 <= self.clock():
                    counts['trash_expired'] += 1
            elif entry['state'] == 'excluded':
                counts['cancel_retention'] += 1
            elif (list(snapshot.fingerprint) == [entry['size'], entry['mtime_ns'], entry['mode']]
                  and local_checksums[entry['root_id'], entry['path']] == entry['md5Checksum']):
                counts['unchanged'] += 1
            else:
                counts['upload'] += 1
        stable_entries = sorted(entries, key=lambda item: (item['root_id'], item['path'], item['file_id']))
        core = dict(computer_id=computer_id, mappings=canonical, entries=stable_entries,
                    local_manifest=local_manifest)
        review_id = hashlib.sha256(json.dumps(core, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return dict(core, review_id=review_id, counts=counts,
                    unmanaged_roots=sorted(available - set(canonical)))

    def prepare(self, computer_id, mappings):
        plan = self._build(computer_id, mappings)
        self.db.set('reconnect_pending', json.dumps(plan))
        return self.summary(plan)

    @staticmethod
    def summary(plan):
        return {key: plan[key] for key in ('computer_id', 'mappings', 'review_id', 'counts', 'unmanaged_roots')}

    def review(self):
        raw = self.db.get('reconnect_pending')
        if not raw:
            raise ValueError('Prepare reconnect mappings first')
        plan = json.loads(raw)
        return self.prepare(plan['computer_id'], plan['mappings'])

    def confirm(self, review_id):
        raw = self.db.get('reconnect_pending')
        if not raw:
            raise ValueError('Prepare and review reconnect mappings first')
        plan = json.loads(raw)
        if review_id != plan['review_id']:
            raise ValueError('Review identifier does not match the pending plan')
        current = self._build(plan['computer_id'], plan['mappings'])
        if current['review_id'] != review_id:
            raise ValueError('Local or remote files changed; run reconnect-review before confirming')
        now = self.clock()
        local = {(item['root_id'], item['path']): item for item in current['local_manifest']}
        from .drive import _key
        with self.sql:
            # Credential namespace must survive adoption of the old computer identity.
            credential_id = self.db.get('credential_store_id', self.db.get('computer_id'))
            self.sql.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('credential_store_id', credential_id))
            self.sql.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('computer_id', plan['computer_id']))
            # Even confirmed imports start paused. User explicitly resumes after review.
            self.sql.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('paused', '1'))
            self.sql.execute('''CREATE TABLE IF NOT EXISTS drive_objects(
              object_key TEXT PRIMARY KEY,drive_id TEXT NOT NULL,confirmed INTEGER NOT NULL DEFAULT 0)''')
            for root_id, path in current['mappings'].items():
                root_entries = [entry for entry in current['entries'] if entry['root_id'] == root_id]
                name = root_entries[0].get('root_name') or Path(path).name
                self.sql.execute("INSERT INTO roots(id,path,name,status,selection_partition) VALUES(?,?,?,'pending',1)", (root_id, path, name))
            for entry in current['entries']:
                observed = local.get((entry['root_id'], entry['path']))
                signature = [entry['size'], entry['mtime_ns'], entry['mode']]
                matches = (observed and observed['fingerprint'] == signature
                           and observed['md5'] == entry['md5Checksum'])
                device = observed['device'] if matches else None
                inode = observed['inode'] if matches else None
                self.sql.execute('''INSERT INTO files(id,root_id,path,fingerprint,observed_at,
                    remote_fingerprint,state,deleted_at,device,inode) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (entry['file_id'], entry['root_id'], entry['path'], json.dumps(signature), now,
                     json.dumps(signature), entry['state'], entry.get('deleted_at'), device, inode))
                self.sql.execute('INSERT OR REPLACE INTO drive_objects VALUES(?,?,1)',
                    (_key('file:' + plan['computer_id'] + ':' + entry['file_id']), entry['drive_id']))
            # Bind chosen remote destination labels when known, not a default on a fresh install.
            first = current['entries'][0]
            for key, field in (('drive_folder_name', 'folder_name'), ('computer_name', 'computer_name')):
                if first.get(field):
                    self.sql.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, first[field]))
            self.sql.execute("DELETE FROM settings WHERE key='reconnect_pending'")
        return dict(self.summary(current), confirmed=True, paused=True)

    def cancel(self):
        with self.sql:
            self.sql.execute("DELETE FROM settings WHERE key='reconnect_pending'")
