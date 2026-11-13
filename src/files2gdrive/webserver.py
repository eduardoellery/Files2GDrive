"""Authenticated loopback-only HTTP boundary for the local desktop interface.

The launch token is an ephemeral bootstrap capability, never a Google token.
Uvicorn must bind 127.0.0.1 with proxy_headers=False and access_log=False.
"""
import hashlib
import hmac
import html
import ipaddress
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs

from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.datastructures import Headers

MAX_BODY = 64 * 1024
SESSION_TTL = 12 * 60 * 60
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
        file_bootstrap = (scope['path'] == '/launch' and scope['method'] == 'POST'
                          and headers.get('content-type', '').split(';', 1)[0].strip() == 'application/x-www-form-urlencoded')
        if (len(headers.getlist('origin')) > 1
                or headers.get('origin') not in ((None, self.origin, 'null') if file_bootstrap else (None, self.origin))
                or (headers.get('sec-fetch-site') == 'cross-site' and not file_bootstrap)):
            await reject(403, 'Origem da requisição não permitida.')
            return
        launch = scope['path'] == '/launch'
        method = scope['method']
        if launch:
            if method != 'POST':
                await reject(405, 'Abra a configuração pelo aplicativo Files2GDrive.')
                return
            if headers.get('content-type', '').split(';', 1)[0].strip() != 'application/x-www-form-urlencoded':
                await reject(415, 'Inicialização local inválida.')
                return
            if headers.get('origin') not in (None, 'null', self.origin):
                await reject(403, 'Origem da requisição não permitida.')
                return
        elif scope['path'].startswith('/api/'):
            authorization = headers.getlist('authorization')
            if len(authorization) != 1 or not authorization[0].startswith('Bearer '):
                await reject(401, 'A sessão local expirou. Reabra a configuração pelo aplicativo.')
                return
            bearer = authorization[0][7:]
            session = self.state.sessions.get(bearer)
            now = time.monotonic()
            if not session or session['expires_at'] <= now:
                if session:
                    self.state.sessions.pop(bearer, None)
                await reject(401, 'A sessão local expirou. Reabra a configuração pelo aplicativo.')
                return
            session['expires_at'] = now + SESSION_TTL
            scope.setdefault('state', {})['files2gdrive_session'] = session
        request = Request(scope, receive)
        if method not in ('GET', 'HEAD'):
            if not launch:
                if (headers.get('origin') != self.origin
                        or not scope.get('state', {}).get('files2gdrive_session')
                        or not _matches(headers.get('x-csrf-token'), scope['state']['files2gdrive_session']['csrf_token'])):
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
    """Create a loopback app with one-use bootstrap and expiring bearer sessions.

    ``state.issue_launch_token()`` creates a new one-use bootstrap token for a
    browser session. Controller calls execute in FastAPI's worker pool, never
    its event loop.
    """
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Choose a concrete loopback port before constructing the app')
    if launch_token is not None and (not isinstance(launch_token, str) or len(launch_token) < 24):
        raise ValueError('Launch token must be an unpredictable string of at least 24 characters')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.launch_token = launch_token or secrets.token_urlsafe(32)
    app.state.launch_token_expires_at = time.monotonic() + 60
    app.state.sessions = {}
    app.state.used_launch_tokens = {}
    launch_lock = threading.Lock()
    def issue_launch_token():
        with launch_lock:
            app.state.launch_token = secrets.token_urlsafe(32)
            app.state.launch_token_expires_at = time.monotonic() + 60
            now = time.monotonic()
            for digest, entry in tuple(app.state.used_launch_tokens.items()):
                if entry['expires_at'] <= now:
                    app.state.used_launch_tokens.pop(digest, None)
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

    @app.post('/launch')
    async def launch(request: Request):
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return JSONResponse({'detail': 'Inicialização local inválida.'}, status_code=413)
        try:
            fields = parse_qs(raw.decode('ascii'), keep_blank_values=True, strict_parsing=True)
        except (UnicodeDecodeError, ValueError):
            fields = {}
        tokens = fields.get('token', [])
        with launch_lock:
            now = time.monotonic()
            if (set(fields) != {'token'} or len(tokens) != 1
                    or app.state.launch_token is None
                    or now >= app.state.launch_token_expires_at
                    or not _matches(tokens[0], app.state.launch_token)):
                if len(tokens) == 1:
                    digest = hashlib.sha256(tokens[0].encode()).hexdigest()
                    previous = app.state.used_launch_tokens.pop(digest, None)
                    if previous and previous['expires_at'] > now:
                        app.state.sessions.pop(previous['bearer'], None)
                        return JSONResponse({'detail': 'Este link já foi utilizado e sua sessão foi revogada. Reabra a configuração pelo aplicativo.'}, status_code=401)
                return JSONResponse({'detail': 'Link de abertura inválido ou expirado. Abra pelo aplicativo.'}, status_code=401)
            launch_token = tokens[0]
            app.state.launch_token = None
            bearer = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(32)
            app.state.sessions[bearer] = {'csrf_token': csrf_token, 'expires_at': now + SESSION_TTL}
            app.state.used_launch_tokens[hashlib.sha256(launch_token.encode()).hexdigest()] = {
                'bearer': bearer, 'expires_at': now + SESSION_TTL}
            for digest, entry in tuple(app.state.used_launch_tokens.items()):
                if entry['expires_at'] <= now:
                    app.state.used_launch_tokens.pop(digest, None)
            for key, session in tuple(app.state.sessions.items()):
                if session['expires_at'] <= now:
                    app.state.sessions.pop(key, None)
        callback = getattr(app.state, 'on_launch', None)
        if callback:
            callback()
        session_value = html.escape(bearer, quote=True)
        response = Response(
            '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
            f'<meta name="files2gdrive-session" content="{session_value}">'
            '<title>Abrindo Files2GDrive</title></head><body>'
            '<script src="/static/bootstrap.js"></script></body></html>',
            media_type='text/html', status_code=200)
        response.delete_cookie('files2gdrive_session', path='/', samesite='strict')
        return response

    @app.get('/')
    def index():
        return FileResponse(ASSETS / 'index.html', media_type='text/html')

    @app.get('/static/app.js')
    @app.get('/app.js')
    def javascript():
        return FileResponse(ASSETS / 'app.js', media_type='text/javascript')

    @app.get('/static/bootstrap.js')
    def bootstrap_javascript():
        return FileResponse(ASSETS / 'bootstrap.js', media_type='text/javascript')

    @app.get('/static/app.css')
    @app.get('/app.css')
    def stylesheet():
        return FileResponse(ASSETS / 'app.css', media_type='text/css')

    @app.get('/api/session')
    def session(request: Request):
        return {'csrf_token': request.state.files2gdrive_session['csrf_token']}

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
