"""Authenticated loopback-only HTTP boundary for the local desktop interface.

The launch token is an ephemeral bootstrap capability, never a Google token.
Uvicorn must bind 127.0.0.1 with proxy_headers=False and access_log=False.
"""
import hmac
import ipaddress
import json
from pathlib import Path
import secrets
import threading

from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import Headers

MAX_BODY = 64 * 1024
COOKIE = 'files2gdrive_session'
ASSETS = Path(__file__).with_name('web')
SECURITY_HEADERS = {
    'cache-control': 'no-store',
    'pragma': 'no-cache',
    'referrer-policy': 'no-referrer',
    'x-content-type-options': 'nosniff',
    'x-frame-options': 'DENY',
    'content-security-policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


def _matches(actual, expected):
    return isinstance(actual, str) and isinstance(expected, str) and hmac.compare_digest(actual.encode(), expected.encode())


class _LocalBoundary:
    def __init__(self, app, *, state, port):
        self.app = app
        self.state = state
        self.authority = f'127.0.0.1:{port}'
        self.origin = 'http://' + self.authority

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1008})
                return
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        async def secured_send(message):
            if message['type'] == 'http.response.start':
                existing = {key.lower(): value for key, value in message.get('headers', [])}
                for name, value in SECURITY_HEADERS.items():
                    existing[name.encode()] = value.encode()
                message = dict(message, headers=list(existing.items()))
            await send(message)
        async def reject(status, detail):
            await JSONResponse({'detail': detail}, status_code=status)(scope, receive, secured_send)
        try:
            client = ipaddress.ip_address(scope.get('client', ('', 0))[0])
            local = client.is_loopback
        except (ValueError, TypeError, IndexError):
            local = False
        # Do not trust forwarding headers or DNS aliases, including localhost.
        if not local or headers.getlist('host') != [self.authority] or scope.get('scheme') != 'http':
            await reject(403, 'Acesso permitido somente pelo endereço local do aplicativo.')
            return
        if (len(headers.getlist('origin')) > 1 or headers.get('origin') not in (None, self.origin)
                or headers.get('sec-fetch-site') == 'cross-site'):
            await reject(403, 'Origem da requisição não permitida.')
            return
        request = Request(scope, receive)
        launch = scope['path'] == '/launch'
        if not launch and not _matches(request.cookies.get(COOKIE), self.state.session_secret):
            await reject(401, 'Abra esta página pelo aplicativo Files2GDrive para iniciar uma sessão local.')
            return
        method = scope['method']
        if method not in ('GET', 'HEAD'):
            if (headers.get('origin') != self.origin
                    or not _matches(headers.get('x-csrf-token'), self.state.csrf_token)):
                await reject(403, 'Sessão ou confirmação de origem inválida. Reabra a configuração.')
                return
            if headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json' and method != 'DELETE':
                await reject(415, 'Envie os dados como JSON.')
                return
            try:
                length = int(headers.get('content-length', '0'))
            except ValueError:
                await reject(400, 'Tamanho da requisição inválido.')
                return
            if length < 0 or length > MAX_BODY:
                await reject(413, 'Requisição muito grande.')
                return
            body = bytearray()
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                if message['type'] != 'http.request':
                    continue
                body.extend(message.get('body', b''))
                if len(body) > MAX_BODY:
                    await reject(413, 'Requisição muito grande.')
                    return
                if not message.get('more_body', False):
                    break
            delivered = False
            original_receive = receive
            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await original_receive()
            receive = replay
        await self.app(scope, receive, secured_send)


def create_app(controller, port, launch_token=None):
    """Create a private session-bound app; launcher reads ``state.launch_token``.

    ``state.issue_launch_token()`` creates a new one-use bootstrap URL token for
    opening a different browser session. Existing session cookies remain valid.
    Controller calls execute in FastAPI's worker pool, never its event loop.
    """
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Choose a concrete loopback port before constructing the app')
    if launch_token is not None and (not isinstance(launch_token, str) or len(launch_token) < 24):
        raise ValueError('Launch token must be an unpredictable string of at least 24 characters')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.session_secret = secrets.token_urlsafe(32)
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.launch_token = launch_token or secrets.token_urlsafe(32)
    launch_lock = threading.Lock()
    def issue_launch_token():
        with launch_lock:
            app.state.launch_token = secrets.token_urlsafe(32)
            return app.state.launch_token
    app.state.issue_launch_token = issue_launch_token
    app.add_middleware(_LocalBoundary, state=app.state, port=port)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({'detail': 'Dados da requisição inválidos.'}, status_code=422)

    def invoke(action, payload=None):
        try:
            return controller.call(action, payload or {})
        except TimeoutError:
            return JSONResponse({'detail': 'O aplicativo está ocupado. Tente novamente em instantes.'}, status_code=503)
        except ValueError as error:
            return JSONResponse({'detail': str(error)}, status_code=400)
        except RuntimeError:
            return JSONResponse({'detail': 'Não foi possível concluir a operação. Consulte o estado do aplicativo.'}, status_code=409)
        except Exception:
            return JSONResponse({'detail': 'Falha interna ao executar a operação.'}, status_code=500)

    @app.get('/launch')
    def launch(request: Request, token: str = ''):
        with launch_lock:
            if not _matches(token, app.state.launch_token):
                return JSONResponse({'detail': 'Link de abertura inválido ou expirado. Abra pelo aplicativo.'}, status_code=401)
            app.state.launch_token = None
        response = RedirectResponse('/', status_code=303)
        response.set_cookie(COOKIE, app.state.session_secret, httponly=True, samesite='strict',
                            secure=False, path='/')
        return response

    @app.get('/')
    def index():
        return FileResponse(ASSETS / 'index.html', media_type='text/html')

    @app.get('/static/app.js')
    @app.get('/app.js')
    def javascript():
        return FileResponse(ASSETS / 'app.js', media_type='text/javascript')

    @app.get('/static/app.css')
    @app.get('/app.css')
    def stylesheet():
        return FileResponse(ASSETS / 'app.css', media_type='text/css')

    @app.get('/api/session')
    def session():
        return {'csrf_token': app.state.csrf_token}

    @app.get('/api/status')
    def status():
        try:
            return controller.snapshot()
        except Exception:
            return JSONResponse({'detail': 'Não foi possível consultar o estado.'}, status_code=503)

    @app.post('/api/control')
    def control(payload: dict = Body(...)):
        return invoke('control', payload)

    @app.get('/api/folder-selection')
    def folder_selection():
        return invoke('folder_selection')

    @app.post('/api/folder-selection/browse')
    def folder_selection_browse(payload: dict = Body(...)):
        return invoke('folder_selection_browse',payload)

    @app.post('/api/folder-selection/preview')
    def folder_selection_preview(payload: dict = Body(...)):
        return invoke('folder_selection_preview',payload)

    @app.post('/api/folder-selection/apply')
    def folder_selection_apply(payload: dict = Body(...)):
        return invoke('folder_selection_apply',payload)

    @app.post('/api/roots')
    def add_root(payload: dict = Body(...)):
        return invoke('add_root', payload)

    @app.delete('/api/roots/{root_id}')
    def remove_root(root_id: str):
        return invoke('remove_root', {'root_id': root_id})

    @app.post('/api/filters/preview')
    def preview_filters(payload: dict = Body(...)):
        return invoke('preview_filters', payload)

    @app.post('/api/filters')
    def set_filters(payload: dict = Body(...)):
        return invoke('set_filters', payload)

    @app.post('/api/settings')
    def settings(payload: dict = Body(...)):
        return invoke('settings', payload)

    @app.post('/api/auth/login')
    def login(payload: dict = Body(...)):
        return invoke('login', payload)

    @app.post('/api/browse')
    def browse(payload: dict = Body(...)):
        return invoke('browse', payload)

    @app.post('/api/recovery/catalog')
    def catalog(payload: dict = Body(...)):
        return invoke('catalog', payload)

    @app.post('/api/recovery/files')
    def recovery_files(payload: dict = Body(...)):
        return invoke('recovery_files', payload)

    @app.post('/api/recovery/restore')
    def restore(payload: dict = Body(...)):
        return invoke('restore', payload)

    @app.get('/api/logs')
    def logs(level: str | None = None, root_id: str | None = None, since: str | None = None):
        return invoke('logs', dict(level=level, root_id=root_id, since=since))

    @app.get('/api/logs/export')
    def export_logs(level: str | None = None, root_id: str | None = None, since: str | None = None):
        result = invoke('logs', dict(level=level, root_id=root_id, since=since, export=True))
        if isinstance(result, Response):
            return result
        entries = result.get('entries', []) if isinstance(result, dict) else result
        content = ''.join(json.dumps(entry, ensure_ascii=True) + '\n' for entry in entries)
        return Response(content, media_type='application/x-ndjson',
                        headers={'content-disposition': 'attachment; filename="files2gdrive-operations.jsonl"'})

    return app
