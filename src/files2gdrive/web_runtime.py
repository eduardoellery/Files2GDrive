"""Loopback-only web launcher. Owns the same exclusive state lock as the CLI."""
import fcntl
import html
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
        launch_file = state / 'web-launch.html'
        try:
            listener.bind(('127.0.0.1', port))
            listener.listen(128)
            bound_port = listener.getsockname()[1]
            token = secrets.token_urlsafe(32)
            app = create_app(controller, port=bound_port, launch_token=token)
            def write_launch(value):
                safe_token = html.escape(value, quote=True)
                document = (
                    '<!doctype html><html lang="pt-BR"><meta charset="utf-8">'
                    '<title>Abrindo Files2GDrive</title><body>'
                    f'<form method="post" action="http://127.0.0.1:{bound_port}/launch">'
                    f'<input type="hidden" name="token" value="{safe_token}">'
                    '<noscript><button type="submit">Abrir Files2GDrive</button></noscript>'
                    '</form><script>document.forms[0].submit()</script></body></html>'
                )
                descriptor = os.open(launch_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, 'w') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(document)
            write_launch(token)
            app.state.on_launch = lambda: launch_file.unlink(missing_ok=True)
            config = uvicorn.Config(app, host='127.0.0.1', port=bound_port, proxy_headers=False,
                                    access_log=False, log_level='warning')
            server = uvicorn.Server(config)
            from .desktop_ipc import LocalControlServer
            from .notifications import desktop_status
            def control(action, payload):
                if action == 'status':
                    return desktop_status(controller.snapshot())
                if action in ('pause', 'resume'):
                    controller.call('control', {'action': action})
                    return desktop_status(controller.snapshot())
                if action == 'open':
                    write_launch(app.state.issue_launch_token())
                    return {'url': launch_file.as_uri()}
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
                        webbrowser.open(launch_file.as_uri())
                threading.Thread(target=open_when_ready, daemon=True).start()
            print(f'Interface local: http://127.0.0.1:{bound_port}/ (acesso pelo navegador iniciado pelo aplicativo)', flush=True)
            print('Ctrl+C encerra a interface e o motor. Nenhuma pasta nova é cadastrada automaticamente.', flush=True)
            server.run(sockets=[listener])
        finally:
            if ipc:
                ipc.stop()
            controller.stop()
            listener.close()
            launch_file.unlink(missing_ok=True)
