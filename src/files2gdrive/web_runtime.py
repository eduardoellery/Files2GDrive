"""Loopback-only web launcher. Owns the same exclusive state lock as the CLI."""
import fcntl
import os
from pathlib import Path
import secrets
import socket
import threading
import webbrowser


def serve(state_directory, port=8765, open_browser=True):
    import uvicorn
    from .logging_setup import configure_logging
    from .web_controller import WebController
    from .webserver import create_app
    state = Path(state_directory).resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / 'engine.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('O motor já está em execução para esse estado') from None
        configure_logging(state / 'logs')
        controller = WebController(state)
        ipc = None
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(('127.0.0.1', port))
            listener.listen(128)
            bound_port = listener.getsockname()[1]
            token = secrets.token_urlsafe(32)
            app = create_app(controller, port=bound_port, launch_token=token)
            url = f'http://127.0.0.1:{bound_port}/launch?token={token}'
            # Private runtime file permits a local desktop launcher to reopen the UI.
            launch_file = state / 'web-launch-url'
            descriptor = os.open(launch_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(url)
            config = uvicorn.Config(app, host='127.0.0.1', port=bound_port, proxy_headers=False,
                                    access_log=False, log_level='warning')
            server = uvicorn.Server(config)
            from .desktop_ipc import LocalControlServer
            from .notifications import desktop_status
            def write_launch(value):
                descriptor = os.open(launch_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, 'w') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(value)
            def control(action, payload):
                if action == 'status':
                    return desktop_status(controller.snapshot())
                if action in ('pause', 'resume'):
                    controller.call('control', {'action': action})
                    return desktop_status(controller.snapshot())
                if action == 'open':
                    value = f'http://127.0.0.1:{bound_port}/launch?token={app.state.issue_launch_token()}'
                    write_launch(value)
                    return {'url': value}
                if action == 'quit':
                    server.should_exit = True
                    return {'accepted': True}
                raise ValueError('Controle desconhecido')
            ipc = LocalControlServer(state, control).start()
            if open_browser:
                def open_when_ready():
                    while not server.started and not controller.stopped.wait(0.1):
                        pass
                    if server.started:
                        webbrowser.open(url)
                threading.Thread(target=open_when_ready, daemon=True).start()
            print(f'Interface local: http://127.0.0.1:{bound_port}/ (acesso pelo navegador iniciado pelo aplicativo)', flush=True)
            print('Ctrl+C encerra a interface e o motor. Nenhuma pasta nova é cadastrada automaticamente.', flush=True)
            server.run(sockets=[listener])
        finally:
            if ipc:
                ipc.stop()
            controller.stop()
            listener.close()
            (state / 'web-launch-url').unlink(missing_ok=True)
