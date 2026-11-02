"""Private, same-user desktop control over a bounded Unix-domain JSON protocol.

This channel carries local control messages only, never Google credentials. The
caller still owns the application's exclusive state lock and database worker.
"""
import errno
import json
import os
from pathlib import Path
import socket
import stat
import struct
import threading

MAX_MESSAGE = 64 * 1024
SOCKET_NAME = 'desktop-control.sock'
ACTIONS = frozenset({'status', 'pause', 'resume', 'open', 'quit'})


class LocalControlError(RuntimeError):
    """Safe-to-display local control failure."""


def _private_state(directory, *, create=False):
    path = Path(directory).absolute()
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = path.lstat()
    except OSError:
        raise LocalControlError('O estado local não está disponível') from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise LocalControlError('O diretório de estado deve pertencer ao usuário e ser privado (0700)')
    return path


def _socket_info(path):
    try:
        info = path.lstat()
    except OSError:
        raise LocalControlError('O aplicativo não está em execução') from None
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise LocalControlError('O canal local não é um socket privado do usuário')
    return info


def _identity(info):
    return info.st_dev, info.st_ino


def _remove_owned_socket(path, identity):
    try:
        info = path.lstat()
        if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid() and _identity(info) == identity:
            path.unlink()
    except FileNotFoundError:
        pass


def _encode(value):
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode() + b'\n'
    except (TypeError, ValueError):
        raise LocalControlError('A mensagem de controle é inválida') from None
    if len(encoded) > MAX_MESSAGE:
        raise LocalControlError('A mensagem de controle excede o limite permitido')
    return encoded


def _read(connection):
    body = bytearray()
    while True:
        chunk = connection.recv(min(4096, MAX_MESSAGE + 1 - len(body)))
        if not chunk:
            raise LocalControlError('O canal de controle foi encerrado sem resposta completa')
        body.extend(chunk)
        if len(body) > MAX_MESSAGE:
            raise LocalControlError('A mensagem de controle excede o limite permitido')
        if b'\n' in chunk:
            line, remainder = bytes(body).split(b'\n', 1)
            if remainder:
                raise LocalControlError('Envie somente uma mensagem por conexão')
            try:
                value = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeDecodeError):
                raise LocalControlError('A mensagem de controle não é JSON válido') from None
            if not isinstance(value, dict):
                raise LocalControlError('A mensagem de controle deve ser um objeto JSON')
            return value
        if len(body) == MAX_MESSAGE:
            raise LocalControlError('A mensagem de controle excede o limite permitido')


def _peer_uid(connection):
    if not hasattr(socket, 'SO_PEERCRED'):
        raise LocalControlError('O sistema não oferece verificação segura do usuário local')
    _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
    return uid


def request(state_directory, action, payload=None, timeout=25):
    """Send one same-user control command and return its dictionary result."""
    if not isinstance(action, str) or action not in ACTIONS:
        raise LocalControlError('Comando local desconhecido')
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise LocalControlError('Os dados de controle devem ser um objeto JSON')
    if type(timeout) not in (int, float) or not 0 < timeout <= 300:
        raise LocalControlError('Tempo de espera de controle inválido')
    state = _private_state(state_directory)
    path = state / SOCKET_NAME
    _socket_info(path)
    encoded = _encode(dict(action=action, payload=payload))
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(path))
            if _peer_uid(connection) != os.getuid():
                raise LocalControlError('O servidor local pertence a outro usuário')
            connection.sendall(encoded)
            response = _read(connection)
    except LocalControlError:
        raise
    except (OSError, ValueError):
        raise LocalControlError('Não foi possível comunicar com o aplicativo local') from None
    if type(response.get('ok')) is not bool:
        raise LocalControlError('Resposta inválida do aplicativo local')
    if not response['ok']:
        error = response.get('error')
        raise LocalControlError(error if isinstance(error, str) else 'O controle local falhou')
    if not isinstance(response.get('result'), dict):
        raise LocalControlError('Resposta inválida do aplicativo local')
    return response['result']


class LocalControlServer:
    def __init__(self, state_directory, handler):
        self.state_directory = state_directory
        self.handler = handler
        self.path = None
        self.socket = None
        self.identity = None
        self.thread = None
        self.stopped = threading.Event()
        self._workers = set()
        self._connections = set()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(8)

    def start(self):
        if self.thread and self.thread.is_alive():
            return self
        state = _private_state(self.state_directory, create=True)
        self.path = state / SOCKET_NAME
        if self.path.exists() or self.path.is_symlink():
            # A previous socket may be stale, but never remove arbitrary files.
            info = _socket_info(self.path)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.3)
                try:
                    probe.connect(str(self.path))
                except OSError as error:
                    if error.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                        raise LocalControlError('O canal de controle já está em uso') from None
                else:
                    raise LocalControlError('O canal de controle já está em uso')
            _remove_owned_socket(self.path, _identity(info))
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            self.identity = _identity(self.path.lstat())
            self.path.chmod(0o600)
            listener.listen(8)
            listener.settimeout(0.2)
        except OSError:
            listener.close()
            if self.identity:
                _remove_owned_socket(self.path, self.identity)
            raise LocalControlError('Não foi possível abrir o canal de controle local') from None
        self.socket = listener
        self.stopped.clear()
        self.thread = threading.Thread(target=self._accept, name='files2gdrive-local-control', daemon=True)
        self.thread.start()
        return self

    def _accept(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._slots.acquire(blocking=False):
                connection.close()
                continue
            with self._lock:
                self._connections.add(connection)
                thread = threading.Thread(target=self._handle, args=(connection,), daemon=True)
                self._workers.add(thread)
            thread.start()

    def _handle(self, connection):
        try:
            connection.settimeout(25)
            if _peer_uid(connection) != os.getuid():
                raise LocalControlError('O controle local permite apenas o mesmo usuário')
            message = _read(connection)
            action = message.get('action')
            payload = message.get('payload', {})
            if not isinstance(action, str) or action not in ACTIONS:
                raise LocalControlError('Comando local desconhecido')
            if not isinstance(payload, dict) or set(message) - {'action', 'payload'}:
                raise LocalControlError('Mensagem de controle inválida')
            result = self.handler(action, payload)
            if not isinstance(result, dict):
                raise LocalControlError('O controle retornou uma resposta inválida')
            response = _encode(dict(ok=True, result=result))
        except (LocalControlError, ValueError) as error:
            try:
                response = _encode(dict(ok=False, error=str(error)))
            except LocalControlError:
                response = _encode(dict(ok=False, error='A operação de controle falhou'))
        except Exception:
            response = _encode(dict(ok=False, error='Não foi possível concluir o controle local'))
        try:
            connection.sendall(response)
        except OSError:
            pass
        finally:
            connection.close()
            with self._lock:
                self._connections.discard(connection)
                self._workers.discard(threading.current_thread())
            self._slots.release()

    def stop(self):
        self.stopped.set()
        if self.socket:
            self.socket.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        with self._lock:
            connections = list(self._connections)
            workers = list(self._workers)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=1)
        if self.path and self.identity:
            _remove_owned_socket(self.path, self.identity)
        self.socket = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()
