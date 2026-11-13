"""Optional GTK3/Ayatana tray helper; the backup engine runs separately.

GTK is imported only by run_tray. IPC and browser launching run in one background
worker at a time; only GLib callbacks touch menu/indicator widgets.
"""
from collections import deque
from contextlib import contextmanager
import fcntl
import hashlib
import logging
import os
from pathlib import Path
import stat
import threading
from urllib.parse import unquote, urlsplit
import webbrowser

log = logging.getLogger('files2gdrive')


def _reject_unvalidated_launch(_value):
    raise ValueError('O caminho privado de configuração não foi validado')


def validate_launch_file(url, state_directory):
    """Accept only the private, token-bearing bootstrap file in this state."""
    if not isinstance(url, str) or len(url) > 4096 or any(character.isspace() for character in url):
        raise ValueError('O motor retornou um arquivo de configuração inválido')
    try:
        parsed = urlsplit(url)
        path = Path(unquote(parsed.path))
        expected = Path(state_directory).resolve() / 'web-launch.html'
        info = path.lstat()
    except (OSError, ValueError):
        raise ValueError('O motor retornou um arquivo de configuração inválido') from None
    if (parsed.scheme != 'file' or parsed.netloc or parsed.query or parsed.fragment
            or path != expected or expected.as_uri() != url
            or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError('O motor retornou um arquivo de configuração inválido')
    return url


class TrayActions:
    """UI-independent action model, suitable for tests without GTK installed."""
    def __init__(self, client, opener=webbrowser.open, exit_callback=None, launch_validator=None):
        self.client = client
        self.opener = opener
        self.exit_callback = exit_callback or (lambda: None)
        self.launch_validator = launch_validator or _reject_unvalidated_launch

    def perform(self, action):
        if action not in ('status', 'pause', 'resume', 'open', 'quit'):
            raise ValueError('Ação da bandeja desconhecida')
        result = self.client(action)
        if not isinstance(result, dict):
            raise ValueError('O motor retornou uma resposta inválida')
        if action == 'open':
            url = self.launch_validator(result.get('url'))
            if self.opener(url) is False:
                raise RuntimeError('Não foi possível abrir o navegador padrão')
        elif action == 'quit':
            self.exit_callback()
        return result


def menu_state(status):
    """Return only labels/icons/availability, never paths or credential details."""
    if not isinstance(status, dict) or not status.get('running'):
        return dict(title='Motor indisponível', tooltip='Files2GDrive — motor indisponível',
                    icon='dialog-warning-symbolic', can_pause=False, can_resume=False)
    paused = bool(status.get('paused'))
    issues = status.get('issues') if isinstance(status.get('issues'), dict) else {}
    auth = status.get('auth_state')
    if auth == 'connecting':
        title = 'Aguardando autenticação'
    elif auth == 'required' or issues.get('auth'):
        title = 'Autenticação necessária'
    elif issues.get('quota'):
        title = 'Verifique o espaço no Drive'
    elif issues.get('access'):
        title = 'Acesso ao Drive recusado'
    elif issues.get('folders'):
        title = 'Pasta sem acesso'
    elif auth == 'error':
        title = 'Verifique a conexão'
    elif paused:
        title = 'Backup pausado'
    elif status.get('active_roots') == 0:
        title = 'Pronto para configurar'
    else:
        count = status.get('queue_total', 0)
        title = f'{count} operação(ões) na fila' if isinstance(count, int) and count > 0 else 'Monitorando arquivos'
    warning = auth in ('required', 'error') or any(issues.get(key) for key in ('auth', 'quota', 'folders', 'access'))
    if paused and title != 'Backup pausado':
        title += ' · pausado'
    return dict(title=title, tooltip='Files2GDrive — ' + title,
                icon='dialog-warning-symbolic' if warning else 'emblem-pause-symbolic' if paused else 'cloud-upload-symbolic',
                can_pause=not paused, can_resume=paused and not bool(status.get('reconnect_pending')))


@contextmanager
def tray_lock(state_directory):
    """Keep one helper per private state, without following the lock symlink."""
    state = Path(state_directory).absolute()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = state.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise RuntimeError('A pasta de estado da bandeja deve ser privada e pertencer ao usuário')
    descriptor = os.open(state / 'tray.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise RuntimeError('Arquivo de controle da bandeja inválido')
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('O ícone do Files2GDrive já está em execução') from None
        yield
    finally:
        os.close(descriptor)


class _Dispatcher:
    """Serialize IPC off GTK; queue controls rather than dropping busy clicks."""
    def __init__(self, actions, schedule, completed, notify=None):
        self.actions = actions
        self.schedule = schedule
        self.completed = completed
        self.notify = notify
        self.lock = threading.Lock()
        self.active = False
        self.pending = deque()
        self.closed = threading.Event()

    def submit(self, action):
        with self.lock:
            if self.closed.is_set():
                return False
            if self.active:
                # A slow status poll must not discard a user's explicit control.
                if action == 'status' or len(self.pending) >= 8:
                    return False
                self.pending.append(action)
                return True
            self.active = True
        threading.Thread(target=self._work, args=(action,), name='files2gdrive-tray-ipc', daemon=True).start()
        return True

    def _work(self, action):
        while True:
            result = None; failed = False
            try:
                result = self.actions.perform(action)
                if action not in ('status', 'quit'):
                    result = self.actions.perform('status')
                if action != 'quit' and self.notify is not None:
                    try:
                        self.notify(result)
                    except Exception:
                        log.warning('desktop notification unavailable', extra={'operation': 'notify', 'result': 'error'})
            except Exception:
                failed = True
            if not self.closed.is_set():
                self.schedule(self.completed, result, failed)
            with self.lock:
                if self.closed.is_set() or (action == 'quit' and not failed):
                    self.pending.clear()
                    self.active = False
                    return
                if self.pending:
                    action = self.pending.popleft()
                else:
                    self.active = False
                    return

    def close(self):
        with self.lock:
            self.closed.set()
            self.pending.clear()


def _load_gtk():
    try:
        import gi
        gi.require_version('Gtk', '3.0')
        gi.require_version('AyatanaAppIndicator3', '0.1')
        from gi.repository import AyatanaAppIndicator3, GLib, Gtk
        return Gtk, GLib, AyatanaAppIndicator3
    except (ImportError, ValueError):
        raise RuntimeError('Bandeja indisponível. Instale python3-gi, gir1.2-gtk-3.0 e gir1.2-ayatanaappindicator3-0.1; o motor pode continuar sem o ícone.') from None


def run_tray(state_directory, client=None, notify=None):
    """Run GTK until Sair is acknowledged by the engine; return zero on exit.

    notify(status) is optional and invoked in the background, never on GTK's main
    loop. All IPC is serial and never accesses the engine's SQLite connection.
    """
    with tray_lock(state_directory):
        Gtk, GLib, Indicator = _load_gtk()
        initialized = Gtk.init_check(None)
        if not (initialized[0] if isinstance(initialized, tuple) else initialized):
            raise RuntimeError('A bandeja precisa de uma sessão gráfica Ubuntu ativa')
        if client is None:
            from .desktop_ipc import request
            client = lambda action, payload=None: request(state_directory, action, payload)
        identity = hashlib.sha256(str(Path(state_directory).resolve()).encode()).hexdigest()[:12]
        assets = Path(__file__).with_name('assets')
        icon_names = {'cloud-upload-symbolic': 'files2gdrive', 'emblem-pause-symbolic': 'files2gdrive-paused',
                      'dialog-warning-symbolic': 'files2gdrive-warning'}
        indicator = Indicator.Indicator.new('files2gdrive-' + identity, 'files2gdrive',
                                            Indicator.IndicatorCategory.APPLICATION_STATUS)
        indicator.set_icon_theme_path(str(assets))
        indicator.set_title('Files2GDrive')
        indicator.set_status(Indicator.IndicatorStatus.ACTIVE)
        menu = Gtk.Menu()
        status_item = Gtk.MenuItem(label='Conectando ao motor…'); status_item.set_sensitive(False)
        menu.append(status_item); menu.append(Gtk.SeparatorMenuItem())
        items = {}
        for label, action in (('Configuração', 'open'), ('Pausar backup', 'pause'),
                              ('Retomar backup', 'resume'), ('Sair', 'quit')):
            if action == 'quit': menu.append(Gtk.SeparatorMenuItem())
            item = Gtk.MenuItem(label=label); menu.append(item); items[action] = item
        items['pause'].set_sensitive(False); items['resume'].set_sensitive(False)
        menu.show_all(); indicator.set_menu(menu)

        def finished(result, failed):
            state = menu_state(None if failed else result)
            status_item.set_label(state['title'])
            indicator.set_title(state['tooltip'])
            indicator.set_icon_full(icon_names[state['icon']], state['tooltip'])
            items['pause'].set_sensitive(state['can_pause'])
            items['resume'].set_sensitive(state['can_resume'])
            return False

        actions = TrayActions(client, exit_callback=lambda: GLib.idle_add(Gtk.main_quit),
                              launch_validator=lambda value: validate_launch_file(value, state_directory))
        dispatcher = _Dispatcher(actions, GLib.idle_add, finished, notify)
        for action, item in items.items():
            item.connect('activate', lambda widget, action=action: dispatcher.submit(action))
        def poll():
            dispatcher.submit('status')
            return True
        timer = GLib.timeout_add_seconds(3, poll)
        dispatcher.submit('status')
        try:
            Gtk.main()
        finally:
            dispatcher.close()
            GLib.source_remove(timer)
            indicator.set_status(Indicator.IndicatorStatus.PASSIVE)
        return 0
