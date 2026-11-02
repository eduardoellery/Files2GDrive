"""Single-owner SQLite/backup worker with a responsive web-facing snapshot."""
from concurrent.futures import Future, TimeoutError as FutureTimeout
from datetime import datetime
import copy
import hashlib
import json
import os
from pathlib import Path
import queue
import threading
import time
from uuid import uuid4

from .auth import AuthenticationError
from .database import Database
from .engine import Engine
from .filters import FilterSet
from .scanner import scan_root


class WebController:
    def __init__(self, state_directory, remote_factory=None, auth_factory=None, *, tick_interval=5):
        self.state = Path(state_directory).resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.remote_factory = remote_factory
        self.auth_factory = auth_factory
        self.tick_interval = tick_interval
        self.commands = queue.Queue(maxsize=64)
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.data = dict(running=False, auth_state='unknown', auth_message='', task=None, progress=None,
                         roots=[], queue=[], settings={}, paused=False, backend='simulator')
        self.thread = threading.Thread(target=self._worker, name='files2gdrive-worker', daemon=True)
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError('Não foi possível iniciar o motor local')
        if self.data.get('startup_error'):
            raise RuntimeError(self.data['startup_error'])

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def _publish(self, **changes):
        with self.lock:
            self.data.update(changes)

    def call(self, action, payload=None):
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError('A solicitação deve ser um objeto JSON')
        long_actions = {'login', 'catalog', 'recovery_files', 'restore', 'folder_selection_preview', 'folder_selection_apply'}
        task_id = None
        if action in long_actions:
            with self.lock:
                old = self.data.get('task')
                if old and old['state'] in ('queued', 'running'):
                    raise RuntimeError('Há uma operação em andamento; aguarde sua conclusão')
                task_id = str(uuid4())
                self.data['task'] = dict(id=task_id, kind=action, state='queued', result=None, error=None)
        future = Future()
        try:
            self.commands.put_nowait((action, payload, future, task_id))
        except queue.Full:
            if task_id:
                self._publish(task=dict(id=task_id, kind=action, state='error', result=None, error='Motor ocupado'))
            raise RuntimeError('Motor ocupado; tente novamente em alguns segundos') from None
        if task_id:
            return dict(task_id=task_id, accepted=True)
        try:
            return future.result(timeout=20)
        except FutureTimeout:
            if future.cancel():
                raise TimeoutError('Motor ocupado; a alteração não foi aplicada. Tente novamente.') from None
            raise TimeoutError('Operação em andamento. Confira o estado antes de repetir.') from None

    def stop(self):
        self.stopped.set()
        self.thread.join()  # Keep the state lock until the owner really exits.

    def _disconnect(self):
        remote = self.engine.remote
        self.engine.remote = None
        close = getattr(remote, 'close', None)
        if callable(close): close()

    def _connect(self):
        from .cli import _remote
        self._disconnect()
        try:
            remote = (self.remote_factory or _remote)(self.db, self.state)
            if hasattr(remote, 'progress_callback'):
                remote.progress_callback = self._progress
            self.engine.remote = remote
            self._publish(auth_state='connected' if self.db.get('backend') == 'drive' else 'simulator', auth_message='')
        except AuthenticationError:
            self._publish(auth_state='required', auth_message='Conecte sua conta Google para continuar o backup.')
        except Exception as error:
            if getattr(error, 'requires_auth', False):
                self._publish(auth_state='required', auth_message='Conecte sua conta Google novamente. A fila foi preservada.')
            else:
                self._publish(auth_state='error', auth_message='Não foi possível acessar o Drive. Verifique a conexão e tente novamente.')

    def _progress(self, value):
        if self.stopped.is_set():
            raise RuntimeError('Operação interrompida pelo encerramento')
        self._publish(progress=value)

    def _refresh(self):
        roots = []
        for row in self.db.connection.execute('SELECT * FROM roots ORDER BY name'):
            root = dict(row); root['patterns'] = json.loads(root['patterns']); roots.append(root)
        jobs = [dict(row) for row in self.db.connection.execute('''SELECT f.path,j.operation,j.attempts,j.error,j.error_category
          FROM jobs j JOIN files f ON f.id=j.file_id ORDER BY f.path LIMIT 100''')]
        self._publish(error_counts={row[0]:row[1] for row in self.db.connection.execute(
                'SELECT error_category,count(*) FROM jobs WHERE error_category IS NOT NULL GROUP BY error_category')},
            running=not self.stopped.is_set(), backend=self.db.get('backend', 'simulator'),
            paused=self.engine.paused, roots=roots, queue=jobs,
            queue_total=self.db.connection.execute('SELECT count(*) FROM jobs').fetchone()[0],
            reconnect_pending=self.db.get('reconnect_pending') is not None,
            settings=dict(folder_name=self.db.get('drive_folder_name', 'Files2GDrive'),
                          computer_name=self.db.get('computer_name', ''),
                          global_patterns=json.loads(self.db.get('global_patterns', '[]')),
                          global_ignore_case=self.db.get('global_ignore_case', '0') == '1'))

    def _prepare_pending_directories(self):
        prepare = getattr(self.engine.remote, 'prepare_directories', None)
        if self.engine.paused or not callable(prepare):
            return
        # Warm a bounded part of the backlog, so directory siblings can be
        # prepared together instead of paying a request round trip per file.
        rows = self.db.connection.execute("""SELECT f.root_id,f.path,f.fingerprint
            FROM jobs j JOIN files f ON f.id=j.file_id JOIN roots r ON r.id=f.root_id
            WHERE j.operation='upload' AND j.next_attempt<=? AND r.enabled=1 AND r.status='ready'
              AND f.observed_at<=? LIMIT 128""",
            (time.time(), time.time() - self.engine.settle_seconds)).fetchall()
        if not rows:
            return
        try:
            prepare([dict(root_id=row['root_id'],path=row['path'],
                          mode=json.loads(row['fingerprint'])[2]) for row in rows])
        except Exception as error:
            if isinstance(error, AuthenticationError) or getattr(error, 'requires_auth', False):
                raise AuthenticationError('Conecte sua conta Google novamente; a fila foi preservada') from None
            # Warming is optional. The regular per-job path records failure
            # categories/backoff and allows unrelated groups to keep advancing.
            return

    def _worker(self):
        self.db = None
        try:
            self.db = Database(self.state / 'state.sqlite3')
            self.engine = Engine(self.db, None)
            self.previews = {}
            self._refresh()
        except Exception:
            self._publish(startup_error='Não foi possível abrir o estado local')
            self.ready.set()
            if self.db:
                self.db.close()
            return
        self.ready.set()
        self._connect()
        next_tick = 0
        next_connect = time.monotonic() + 30
        watcher = None
        watched = None
        queue_active = False
        try:
            while not self.stopped.is_set():
                try:
                    action, payload, future, task_id = self.commands.get(timeout=0 if queue_active else 0.4)
                except queue.Empty:
                    action = None
                if action is not None:
                    if future.set_running_or_notify_cancel():
                        if task_id:
                            self._publish(task=dict(id=task_id, kind=action, state='running', result=None, error=None))
                        try:
                            result = self._dispatch(action, payload)
                            if task_id:
                                self._publish(task=dict(id=task_id, kind=action, state='done', result=result, error=None))
                            self._refresh()
                            future.set_result(result)
                        except Exception as error:
                            if isinstance(error, AuthenticationError) or getattr(error, 'requires_auth', False):
                                self._disconnect()
                                self._publish(auth_state='required', auth_message='Autentique novamente para continuar. A fila foi preservada.')
                            safe = str(error) if isinstance(error, (ValueError, AuthenticationError)) else 'Não foi possível concluir a operação. Verifique os dados e tente novamente.'
                            if task_id:
                                self._publish(task=dict(id=task_id, kind=action, state='error', result=None, error=safe))
                            self._refresh()
                            future.set_exception(ValueError(safe))
                        self._refresh()
                    next_tick = 0
                current_roots = tuple(row[0] for row in self.db.connection.execute('SELECT path FROM roots WHERE enabled=1'))
                if current_roots != watched:
                    if watcher:
                        watcher.stop()
                    from .watcher import DirectoryWatcher
                    watcher = DirectoryWatcher(list(current_roots)).start()
                    watched = current_roots
                due = time.monotonic() >= next_tick or (watcher and watcher.dirty.is_set())
                if self.engine.remote is None and self.snapshot()['auth_state'] == 'error' and time.monotonic() >= next_connect:
                    self._connect()
                    next_connect = time.monotonic() + 30
                queue_active = False
                if self.commands.empty():
                    try:
                        if due:
                            if watcher:
                                watcher.dirty.clear()
                            self.engine.reconcile()
                            next_tick = time.monotonic() + self.tick_interval
                            self._prepare_pending_directories()
                        if self.engine.remote is not None:
                            # Keep command handling between jobs, but never wait for
                            # the next filesystem scan while transfers are ready.
                            queue_active = bool(self.engine.process(max_jobs=4))
                    except AuthenticationError:
                        self._disconnect()
                        self._publish(auth_state='required', auth_message='Sua autorização precisa ser renovada. Conecte novamente; a fila está salva.')
                    except Exception:
                        self._publish(auth_message='O backup encontrou uma falha. Consulte a atividade e tente verificar novamente.')
                    finally:
                        self._publish(progress=None)
                    self._refresh()
        finally:
            if watcher:
                watcher.stop()
            self._disconnect()
            self.db.close()
            self._publish(running=False)

    @staticmethod
    def _patterns(payload):
        patterns = payload.get('patterns', [])
        if not isinstance(patterns, list) or len(patterns) > 500 or any(not isinstance(item, str) or len(item) > 4096 for item in patterns):
            raise ValueError('Informe até 500 padrões de exclusão, um por linha')
        ignore = payload.get('ignore_case', False)
        if not isinstance(ignore, bool):
            raise ValueError('A opção de maiúsculas deve ser verdadeira ou falsa')
        FilterSet(patterns, ignore_case=ignore)
        return patterns, ignore

    def _root(self, root_id):
        row = self.db.connection.execute('SELECT * FROM roots WHERE id=?', (root_id,)).fetchone()
        if not row:
            raise ValueError('Pasta monitorada não encontrada')
        return row

    def _preview(self, payload):
        patterns, ignore = self._patterns(payload)
        root_id = payload.get('root_id')
        roots = [self._root(root_id)] if root_id else self.db.connection.execute('SELECT * FROM roots WHERE enabled=1').fetchall()
        matched = []; retained = 0; manifest = []
        new_filter = FilterSet(patterns, ignore_case=ignore)
        for root in roots:
            scan = self.engine.scan_selected(root,apply_filters=False)
            if not scan.complete:
                raise ValueError('Não foi possível conferir todos os arquivos. Verifique o acesso à pasta.')
            global_filter = new_filter if not root_id else FilterSet(json.loads(self.db.get('global_patterns', '[]')),
                ignore_case=self.db.get('global_ignore_case', '0') == '1')
            local_filter = new_filter if root_id else FilterSet(json.loads(root['patterns']), ignore_case=bool(root['ignore_case']))
            paths = set(scan.files)
            rows = self.db.connection.execute('SELECT path,state,fingerprint FROM files WHERE root_id=?', (root['id'],)).fetchall()
            paths.update(row['path'] for row in rows)
            states = {row['path']: row['state'] for row in rows}
            for path in sorted(paths):
                excluded = global_filter.excludes(path) or local_filter.excludes(path)
                if excluded:
                    matched.append(root['name'] + '/' + path)
                    if states.get(path) == 'active':
                        retained += 1
                snap = scan.files.get(path)
                manifest.append((root['id'], path, list(snap.fingerprint) if snap else None, states.get(path), excluded))
        signature = hashlib.sha256(json.dumps(dict(manifest=manifest, patterns=patterns, ignore_case=ignore,
            root_id=root_id, global_patterns=self.db.get('global_patterns', '[]'),
            global_ignore_case=self.db.get('global_ignore_case', '0')), sort_keys=True).encode()).hexdigest()
        return signature, dict(matched_count=len(matched), examples=matched[:50], retention_count=retained)

    def _dispatch(self, action, payload):
        if action.startswith('folder_selection'):
            from .folder_selection import FolderSelection
            manager = FolderSelection(self.db)
            if action == 'folder_selection': return manager.snapshot()
            if action == 'folder_selection_browse':
                return manager.browse(payload.get('path'),payload.get('show_hidden',False))
            if action == 'folder_selection_preview':
                digest, result = manager.preview(payload.get('rules'),payload.get('revision'))
                token=str(uuid4())
                if len(self.previews)>64: self.previews.clear()
                self.previews[token]=('folder_selection',digest,time.monotonic())
                return dict(result,preview_id=token)
            if action == 'folder_selection_apply':
                old=self.previews.pop(payload.get('preview_id'),None)
                if not old or len(old)!=3 or old[0]!='folder_selection' or time.monotonic()-old[2]>600:
                    raise ValueError('A prévia expirou. Revise a seleção novamente.')
                return manager.apply(payload.get('rules'),payload.get('revision'),old[1])
            raise ValueError('Operação de seleção desconhecida')
        if action == 'control':
            control = payload.get('action')
            if control == 'pause': self.engine.pause()
            elif control == 'resume': self.engine.pause(False)
            elif control == 'check': self._connect()
            else: raise ValueError('Controle desconhecido')
            return {'ok': True}
        if action == 'add_root':
            if not isinstance(payload.get('path'), str) or not payload['path'].strip():
                raise ValueError('Escolha uma pasta local')
            path = Path(payload['path']).expanduser().absolute()
            resolved = path.resolve(strict=True)
            if resolved.is_relative_to(self.state) or self.state.is_relative_to(resolved):
                raise ValueError('A pasta de backup não pode conter os dados internos do aplicativo')
            from .cli import _display_name
            name = payload.get('name')
            if name is not None:
                if not isinstance(name, str): raise ValueError('Informe um nome válido')
                name = _display_name(name)
            if self.db.get('backend') is None: self.db.set('backend', 'simulator')
            return dict(id=self.engine.add_root(path, name))
        if action == 'remove_root':
            self._root(payload.get('root_id'))
            self.engine.remove_root(payload['root_id'])
            return {'ok': True}
        if action == 'preview_filters':
            digest, result = self._preview(payload)
            token = str(uuid4())
            if len(self.previews) > 64: self.previews.clear()
            self.previews[token] = (digest, time.monotonic())
            return dict(result, preview_id=token)
        if action == 'set_filters':
            token = payload.get('preview_id')
            old = self.previews.pop(token, None)
            digest, _ = self._preview(payload)
            if not old or old[0] != digest or time.monotonic() - old[1] > 600:
                raise ValueError('A prévia mudou ou expirou. Confira novamente antes de salvar.')
            patterns, ignore = self._patterns(payload)
            self.engine.set_filters(patterns, payload.get('root_id'), ignore)
            return {'ok': True}
        if action == 'settings':
            from .cli import _display_name
            folder = payload.get('folder_name'); computer = payload.get('computer_name')
            if not isinstance(folder, str) or not isinstance(computer, str):
                raise ValueError('Informe os nomes do destino e do computador')
            folder = _display_name(folder); computer = _display_name(computer)
            if self.db.get('reconnect_pending') is not None:
                raise ValueError('Conclua a conferência de reconexão antes de alterar o destino')
            used = self.db.connection.execute('SELECT count(*) FROM files').fetchone()[0]
            if used and (folder != self.db.get('drive_folder_name', 'Files2GDrive') or computer != self.db.get('computer_name', '')):
                raise ValueError('A alteração de destino após iniciar o backup ainda não está disponível')
            self.db.set('drive_folder_name', folder); self.db.set('computer_name', computer)
            self._connect()
            return {'ok': True}
        if action == 'login':
            from .cli import _auth
            self._publish(auth_state='connecting', auth_message='Conclua a autorização na janela do Google.')
            (self.auth_factory or _auth)(self.db).login()
            self._connect()
            if self.engine.remote is None:
                raise ValueError('O login terminou, mas ainda não foi possível acessar o Drive. Tente verificar novamente.')
            return {'ok': True}
        if action == 'logs':
            from .logging_setup import read_logs
            since = datetime.fromisoformat(payload['since'].replace('Z', '+00:00')) if payload.get('since') else None
            records = read_logs(self.state / 'logs', level=payload.get('level') or None,
                                root_id=payload.get('root_id') or None, since=since)
            if payload.get('export'):
                entries = list(records)
            else:
                from collections import deque
                entries = list(deque(records, maxlen=1000))
            return {'entries': entries}
        if action == 'browse':
            path = Path(payload.get('path') or Path.home()).expanduser().absolute()
            from .restore import _open_absolute_directory
            with _open_absolute_directory(path) as descriptor:
                folders = []
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        if entry.is_dir(follow_symlinks=False) and (payload.get('show_hidden') or not entry.name.startswith('.')):
                            folders.append(dict(name=entry.name, path=str(path / entry.name), hidden=entry.name.startswith('.')))
                return dict(path=str(path), parent=str(path.parent), folders=sorted(folders, key=lambda value: value['name'].casefold())[:1000])
        if action in ('catalog', 'recovery_files', 'restore'):
            if self.db.get('backend') != 'drive' or self.engine.remote is None:
                raise ValueError('Conecte sua conta Google antes de acessar os backups')
            from .recovery_remote import DriveRecovery
            recovery = DriveRecovery(self.engine.remote.service)
            if action == 'catalog': return recovery.list_backups()
            entries = recovery.list_files(payload.get('computer_id'), root_id=payload.get('root_id'))
            if action == 'recovery_files': return entries
            from .restore import RestoreManager
            if not isinstance(payload.get('destination'), str) or not payload['destination'].strip():
                raise ValueError('Escolha uma pasta nova para a restauração')
            destination = Path(payload['destination']).expanduser().absolute()
            resolved = destination.resolve()
            if resolved.is_relative_to(self.state) or self.state.is_relative_to(resolved):
                raise ValueError('Escolha um destino separado dos dados internos do aplicativo')
            for row in self.db.connection.execute('SELECT path FROM roots WHERE enabled=1'):
                root = Path(row['path'])
                if root.is_relative_to(resolved) or resolved.is_relative_to(root):
                    raise ValueError('Restaure fora das pastas monitoradas')
            selected = payload.get('file_ids', [])
            if not isinstance(selected, list) or not selected or any(not isinstance(value, str) for value in selected):
                raise ValueError('Selecione os arquivos que deseja restaurar')
            entries = [entry for entry in entries if entry['file_id'] in selected]
            if {entry['file_id'] for entry in entries} != set(selected):
                raise ValueError('A seleção mudou. Atualize o catálogo antes de recuperar.')
            restored = RestoreManager(recovery).restore(entries, destination)
            return dict(destination=str(destination), restored=restored, monitoring_enabled=False)
        raise ValueError('Operação desconhecida')
