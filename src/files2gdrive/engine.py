"""Reconciliation and persistent work queue for a single local worker."""
from contextlib import ExitStack, contextmanager
from collections import Counter
import json
import logging
import os
import time
import tempfile
import shutil
from pathlib import Path
from uuid import uuid4

from .filesystem import open_source
from .directory_rules import DirectoryRules
from .filters import FilterSet
from .scanner import scan_root

RETENTION = 30 * 24 * 60 * 60
log = logging.getLogger('files2gdrive')


class Engine:
    def __init__(self, database, remote, clock=time.time, settle_seconds=3):
        self.db = database
        self.sql = database.connection
        self.remote = remote
        self.clock = clock
        self.settle_seconds = settle_seconds

    @property
    def paused(self):
        return self.db.get('paused', '0') == '1' or self.db.get('reconnect_pending') is not None

    def pause(self, value=True):
        if not value and self.db.get('reconnect_pending') is not None:
            raise ValueError('Review and confirm or cancel pending reconnect before resuming')
        self.db.set('paused', '1' if value else '0')
        log.info('pause=%s', value)

    def add_root(self, path, name=None):
        if self.db.get('reconnect_pending') is not None:
            raise ValueError('Confirm or cancel pending reconnect before adding roots')
        original = Path(path).absolute()
        if original.is_symlink():
            raise ValueError('A monitored root cannot be a symbolic link')
        path = original.resolve(strict=True)
        if not path.is_dir():
            raise ValueError('Root must be a directory')
        for row in self.sql.execute('SELECT * FROM roots WHERE enabled=1'):
            existing = Path(row['path'])
            if path == existing or path.is_relative_to(existing) or existing.is_relative_to(path):
                raise ValueError('Monitored roots cannot overlap')
        old = self.sql.execute('SELECT id FROM roots WHERE path=?', (str(path),)).fetchone()
        root_id = old['id'] if old else str(uuid4())
        with self.sql:
            if old:
                self.sql.execute("UPDATE roots SET enabled=1,status='pending',selection_partition=1 WHERE id=?", (root_id,))
            else:
                self.sql.execute('INSERT INTO roots(id,path,name,selection_partition) VALUES(?,?,?,1)',
                                 (root_id, str(path), name or path.name))
        if self.db.get('directory_rules') is not None:
            rules = DirectoryRules(json.loads(self.db.get('directory_rules')))
            self.db.set('directory_rules', json.dumps(rules.with_toggle(str(path), True)))
        return root_id

    def _queue(self, file_id, operation):
        old = self.sql.execute('SELECT operation FROM jobs WHERE file_id=?', (file_id,)).fetchone()
        if old and old['operation'] == operation:
            return
        self.sql.execute('INSERT OR REPLACE INTO jobs(file_id,operation) VALUES(?,?)', (file_id, operation))

    def _exclude(self, row, now):
        if row['state'] == 'active':
            self.sql.execute("UPDATE files SET state='excluded',deleted_at=? WHERE id=?", (now, row['id']))
            self._queue(row['id'], 'retain')

    def remove_root(self, root_id):
        row = self.sql.execute('SELECT path FROM roots WHERE id=?', (root_id,)).fetchone()
        if row and self.db.get('directory_rules') is not None:
            rules = DirectoryRules(json.loads(self.db.get('directory_rules')))
            self.db.set('directory_rules', json.dumps(rules.with_toggle(row['path'], False)))
        now = self.clock()
        with self.sql:
            self.sql.execute("UPDATE roots SET enabled=0,status='removed' WHERE id=?", (root_id,))
            for row in self.sql.execute('SELECT * FROM files WHERE root_id=?', (root_id,)).fetchall():
                self._exclude(row, now)
        log.info('root_removed root=%s', root_id)

    def set_filters(self, patterns, root_id=None, ignore_case=False):
        if self.db.get('reconnect_pending') is not None:
            raise ValueError('Confirm or cancel pending reconnect before changing filters')
        patterns = list(patterns)
        FilterSet(patterns, ignore_case=ignore_case)  # validate before saving
        encoded = json.dumps(list(patterns))
        if root_id:
            with self.sql:
                self.sql.execute('UPDATE roots SET patterns=?,ignore_case=? WHERE id=?',
                                 (encoded, int(ignore_case), root_id))
        else:
            self.db.set('global_patterns', encoded)
            self.db.set('global_ignore_case', int(ignore_case))

    def directory_selection(self):
        encoded = self.db.get('directory_rules')
        if encoded is None:
            return None
        return DirectoryRules(json.loads(encoded), blocked_paths=[str(self.db.path.parent)])

    def partition_owner(self, absolute, roots=None):
        roots = roots if roots is not None else self.sql.execute(
            'SELECT * FROM roots WHERE selection_partition=1').fetchall()
        candidates = [row for row in roots if Path(absolute).is_relative_to(Path(row['path']))]
        return max(candidates, key=lambda row: len(Path(row['path']).parts))['id'] if candidates else None

    def scan_selected(self, root, *, apply_filters=True):
        selection = self.directory_selection()
        # The reviewed selector reserves dormant partitions to preserve their
        # historical file IDs. Legacy CLI inclusion has no such plan: a removed
        # child must not silently leave a hole in a newly added parent backup.
        partitions = self.sql.execute('SELECT * FROM roots WHERE selection_partition=1' +
                                      (' AND enabled=1' if selection is None else '')).fetchall()
        base = Path(root['path'])
        nested = [Path(row['path']) for row in partitions if row['id'] != root['id']
                  and Path(row['path']).is_relative_to(base)]
        private = self.db.path.parent
        global_filter = FilterSet(json.loads(self.db.get('global_patterns', '[]')),
                                  ignore_case=self.db.get('global_ignore_case', '0') == '1')
        local_filter = FilterSet(json.loads(root['patterns']), ignore_case=bool(root['ignore_case']))
        def allowed(relative, is_dir):
            absolute = base / relative
            if absolute.is_relative_to(private) or any(absolute.is_relative_to(child) for child in nested):
                return False
            if selection is not None:
                if is_dir and selection.state(str(absolute)) == 'excluded': return False
                if not is_dir and not selection.includes(str(absolute)): return False
            return not apply_filters or not (global_filter.excludes(relative, is_dir=is_dir) or local_filter.excludes(relative, is_dir=is_dir))
        return scan_root(base, should_descend=lambda path: allowed(path, True),
                         include_file=lambda path: allowed(path, False))

    def reconcile(self):
        if self.paused:
            return
        now = self.clock()
        global_filters = FilterSet(json.loads(self.db.get('global_patterns', '[]')),
                                   ignore_case=self.db.get('global_ignore_case', '0') == '1')
        for root in self.sql.execute('SELECT * FROM roots WHERE enabled=1').fetchall():
            result = self.scan_selected(root)
            for issue in result.issues:
                log.warning('scan issue=%s', issue, extra={'root_id': root['id'], 'operation': 'scan'})
            if not result.complete:
                with self.sql:
                    self.sql.execute("UPDATE roots SET status='unavailable' WHERE id=?", (root['id'],))
                continue
            filters = FilterSet(json.loads(root['patterns']), ignore_case=bool(root['ignore_case']))
            seen = set()
            identity_counts = Counter((item.device, item.inode) for item in result.files.values())
            with self.sql:
                self.sql.execute("UPDATE roots SET status='ready' WHERE id=?", (root['id'],))
                for relative, snapshot in result.files.items():
                    if global_filters.excludes(relative) or filters.excludes(relative):
                        continue
                    seen.add(relative)
                    fingerprint = json.dumps(list(snapshot.fingerprint))
                    row = self.sql.execute('SELECT * FROM files WHERE root_id=? AND path=?',
                                           (root['id'], relative)).fetchone()
                    if not row:
                        # Recognize unambiguous same-root moves by filesystem identity.
                        candidates = self.sql.execute(
                            "SELECT * FROM files WHERE root_id=? AND device=? AND inode=? AND state='active'",
                            (root['id'], snapshot.device, snapshot.inode)).fetchall()
                        identities = identity_counts[snapshot.device, snapshot.inode]
                        if (len(candidates) == 1 and identities == 1 and candidates[0]['path'] not in result.files
                                and candidates[0]['fingerprint'] == fingerprint):
                            row = candidates[0]
                            self.sql.execute('UPDATE files SET path=?,observed_at=? WHERE id=?',
                                             (relative, now, row['id']))
                            self.sql.execute('DELETE FROM jobs WHERE file_id=?', (row['id'],))
                            self._queue(row['id'], 'upload')
                    if not row:
                        file_id = str(uuid4())
                        self.sql.execute('INSERT INTO files(id,root_id,path,fingerprint,observed_at,device,inode) VALUES(?,?,?,?,?,?,?)',
                                         (file_id, root['id'], relative, fingerprint, now, snapshot.device, snapshot.inode))
                        self._queue(file_id, 'upload')
                    elif (row['fingerprint'] != fingerprint or row['state'] != 'active'
                          or (row['device'], row['inode']) != (snapshot.device, snapshot.inode)):
                        self.sql.execute("UPDATE files SET fingerprint=?,observed_at=?,device=?,inode=?,state='active',deleted_at=NULL WHERE id=?",
                                         (fingerprint, now, snapshot.device, snapshot.inode, row['id']))
                        # Changed content must reset any failed upload's retry timer.
                        self.sql.execute('DELETE FROM jobs WHERE file_id=?', (row['id'],))
                        self._queue(row['id'], 'upload')
                for row in self.sql.execute('SELECT * FROM files WHERE root_id=?', (root['id'],)).fetchall():
                    if row['path'] not in seen:
                        self._exclude(row, now)

    @contextmanager
    def _upload_snapshot(self, job):
        """Open a stable private snapshot; changed entries are skipped safely."""
        with open_source(Path(job['root_path']) / job['path']) as stream:
            before = os.fstat(stream.fileno())
            signature = [before.st_size, before.st_mtime_ns, before.st_mode & 0o777]
            if (signature != json.loads(job['fingerprint'])
                    or (before.st_dev, before.st_ino) != (job['device'], job['inode'])):
                yield None
                return
            with tempfile.TemporaryFile(mode='w+b') as snapshot:
                shutil.copyfileobj(stream, snapshot, length=1024 * 1024)
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_mode) != (
                        after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_mode):
                    raise OSError('File changed during snapshot; will retry')
                if snapshot.tell() != before.st_size:
                    raise OSError('Incomplete file snapshot; will retry')
                snapshot.seek(0)
                yield dict(file_id=job['file_id'], stream=snapshot,
                           metadata=dict(root_id=job['root_id'], path=job['path'],
                                         mode=signature[2], mtime_ns=signature[1]))

    def _job_completed(self, job):
        with self.sql:
            if job['operation'] == 'upload':
                self.sql.execute('UPDATE files SET remote_fingerprint=? WHERE id=?',
                                 (job['fingerprint'], job['file_id']))
            elif job['operation'] == 'trash':
                self.sql.execute("UPDATE files SET state='trashed' WHERE id=?", (job['file_id'],))
            self.sql.execute('DELETE FROM jobs WHERE file_id=?', (job['file_id'],))
        log.info('operation completed', extra={'operation': job['operation'], 'path': job['path'],
                                              'root_id': job['root_id'], 'result': 'success'})

    def _job_failed(self, job, error, now):
        delay = min(3600, 2 ** min(job['attempts'] + 1, 12))
        with self.sql:
            self.sql.execute('UPDATE jobs SET attempts=attempts+1,next_attempt=?,error=?,error_category=? WHERE file_id=?',
                             (self.clock() + delay, str(error), getattr(error, 'category', 'unknown'), job['file_id']))
        log.warning('operation failed', extra={'operation': job['operation'], 'path': job['path'],
                                              'root_id': job['root_id'], 'result': 'error', 'reason': str(error)})

    def process(self, max_jobs=None):
        """Run eligible work, with at most four stable snapshots in flight.

        SQLite and queue ownership stay on this thread. Parallel-capable remotes
        receive private snapshots and return per-item results before any queue
        changes. Files still being written never consume a remote batch slot.
        """
        if self.paused:
            return 0
        now = self.clock()
        with self.sql:
            for row in self.sql.execute("SELECT * FROM files WHERE state='excluded' AND deleted_at<=?",
                                        (now - RETENTION,)).fetchall():
                self._queue(row['id'], 'trash')
        jobs = self.sql.execute('''SELECT j.*,f.root_id,f.path,f.fingerprint,f.observed_at,
          f.deleted_at,f.device,f.inode,r.path AS root_path,r.enabled,r.status FROM jobs j
          JOIN files f ON f.id=j.file_id JOIN roots r ON r.id=f.root_id
          WHERE j.next_attempt<=?''', (now,)).fetchall()
        upload_many = getattr(self.remote, 'upload_many', None)
        batch_limit = 4 if callable(upload_many) else 1
        attempted = 0
        cursor = 0
        while cursor < len(jobs) and (max_jobs is None or attempted < max_jobs):
            if self.paused:
                break
            with ExitStack() as snapshots:
                batch = []
                items = []
                while cursor < len(jobs) and len(batch) < batch_limit:
                    job = jobs[cursor]
                    if max_jobs is not None and attempted + len(batch) >= max_jobs:
                        break
                    if job['enabled'] and job['status'] != 'ready':
                        cursor += 1
                        continue
                    if job['operation'] != 'upload':
                        if batch:
                            break
                        cursor += 1
                        attempted += 1
                        try:
                            if job['operation'] == 'retain':
                                self.remote.retain(job['file_id'], job['deleted_at'])
                            else:
                                self.remote.trash(job['file_id'])
                            self._job_completed(job)
                        except (OSError, RuntimeError, ValueError) as error:
                            self._job_failed(job, error, now)
                            if getattr(error, 'requires_auth', False):
                                from .auth import AuthenticationError
                                raise AuthenticationError('Conecte sua conta Google novamente; a fila foi preservada') from None
                        continue
                    cursor += 1
                    if now - job['observed_at'] < self.settle_seconds:
                        continue
                    context = self._upload_snapshot(job)
                    try:
                        item = context.__enter__()
                        if item is None:
                            context.__exit__(None, None, None)
                            continue
                        snapshots.push(context)
                    except (OSError, RuntimeError, ValueError) as error:
                        self._job_failed(job, error, now)
                        continue
                    batch.append(job)
                    items.append(item)
                if not batch:
                    continue
                attempted += len(batch)
                if callable(upload_many):
                    try:
                        results = upload_many(items)
                        if not isinstance(results, (list, tuple)) or len(results) != len(items):
                            raise RuntimeError('Remote returned an incomplete upload batch')
                    except (OSError, RuntimeError, ValueError) as error:
                        results = [error] * len(batch)
                else:
                    item = items[0]
                    try:
                        self.remote.upload(item['file_id'], item['stream'], item['metadata'])
                        results = [None]
                    except (OSError, RuntimeError, ValueError) as error:
                        results = [error]
                requires_auth = False
                for job, result in zip(batch, results):
                    if result is None:
                        self._job_completed(job)
                    else:
                        if not isinstance(result, Exception):
                            result = RuntimeError('Remote returned an invalid upload result')
                        self._job_failed(job, result, now)
                        requires_auth |= bool(getattr(result, 'requires_auth', False))
                if requires_auth:
                    from .auth import AuthenticationError
                    raise AuthenticationError('Conecte sua conta Google novamente; a fila foi preservada') from None
        return attempted

    def tick(self):
        self.reconcile()
        self.process()
