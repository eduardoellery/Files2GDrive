"""Desktop launcher: attach to one engine, keep a separate optional tray helper."""
import fcntl
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import webbrowser

from .desktop_ipc import LocalControlError, _private_state, request
from .notifications import NotificationPolicy
from .tray import validate_launch_url

log = logging.getLogger('files2gdrive')


def open_configuration(state_directory, opener=webbrowser.open):
    result = request(state_directory, 'open')
    url = validate_launch_url(result.get('url'))
    if opener(url) is False:
        raise RuntimeError('Não foi possível abrir o navegador padrão')
    return url


def ensure_engine(state_directory, *, timeout=15):
    state = _private_state(state_directory, create=True)
    try:
        return request(state, 'status', timeout=1)
    except LocalControlError:
        pass
    # An old CLI/server holding the state must not be duplicated or killed.
    descriptor = os.open(state / 'engine.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another launcher may still be starting; wait below without spawning.
            spawn = False
        else:
            spawn = True
    finally:
        os.close(descriptor)
    if spawn:
        output_fd = os.open(state / 'desktop-engine.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(output_fd, 'ab') as output:
            from .runtime_context import command
            subprocess.Popen(command(state, 'serve', '--port', '0', '--no-browser'),
                             stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return request(state, 'status', timeout=1)
        except LocalControlError:
            time.sleep(0.15)
    raise RuntimeError('Não foi possível acessar o motor. Encerre a CLI antiga antes de abrir o aplicativo.')


def desktop(state_directory, *, background=False):
    state = _private_state(state_directory, create=True)
    # Keep a single helper, including when GTK is missing and notifications remain.
    descriptor = os.open(state / 'desktop-helper.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            ensure_engine(state)
            if not background:
                open_configuration(state)
            return 0
        ensure_engine(state)
        if not background:
            open_configuration(state)
        policy = NotificationPolicy(state)
        from .tray import run_tray
        try:
            return run_tray(state, notify=policy)
        except RuntimeError as error:
            log.warning(str(error), extra={'operation': 'desktop', 'result': 'error'})
            # Missing tray support must not stop backup or notification delivery.
            while True:
                try:
                    status = request(state, 'status', timeout=3)
                except LocalControlError:
                    return 0
                if not status.get('running'):
                    return 0
                policy(status)
                time.sleep(3)
    finally:
        os.close(descriptor)
