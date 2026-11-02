"""Actionable desktop alerts, once per unresolved condition (also across restarts)."""
import json
import logging
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

log = logging.getLogger('files2gdrive')
MESSAGES = {
    'auth': ('Conecte novamente sua conta Google', 'O backup aguarda autorização. Abra Configuração no ícone do Files2GDrive; a fila está preservada.'),
    'quota': ('Seu Google Drive está sem espaço', 'Libere espaço no Drive e verifique o backup. Os arquivos pendentes continuam na fila.'),
    'folders': ('Uma pasta do backup ficou inacessível', 'Confira as pastas na configuração. O conteúdo remoto não será excluído por falta de acesso local.'),
    'access': ('O Google Drive recusou o acesso', 'Confira as permissões da conta e as políticas da organização. Abra Configuração para consultar a atividade.'),
}


def send_notification(title, body):
    """Use the installed Ubuntu notification tool, with fixed arguments/no shell."""
    try:
        result = subprocess.run(['/usr/bin/notify-send', '--app-name=Files2GDrive', '--icon=' + str(Path(__file__).with_name('assets') / 'files2gdrive.svg'),
                                 '--urgency=normal', '--expire-time=10000',
                                 '--hint=string:desktop-entry:files2gdrive', '--', title, body],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class NotificationPolicy:
    def __init__(self, state_directory, sender=send_notification, clock=time.time):
        self.path = Path(state_directory) / 'desktop-notifications.json'
        self.sender = sender
        self.clock = clock
        self.delivered = set()
        self.retry_at = {}
        try:
            if not self.path.is_symlink():
                saved = json.loads(self.path.read_text())
                if isinstance(saved, dict):
                    self.delivered = set(saved.get('delivered', [])) & MESSAGES.keys()
        except (OSError, ValueError, TypeError):
            pass

    def _save(self):
        temporary = self.path.with_name('.notification-' + uuid4().hex)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                json.dump({'delivered': sorted(self.delivered)}, stream)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def __call__(self, status):
        # Unknown/connecting states do not prove an old condition has cleared.
        issues = status.get('issues') or {}
        active = set()
        if status.get('auth_state') == 'required' or issues.get('auth'):
            active.add('auth')
        for key in ('quota', 'folders', 'access'):
            if issues.get(key): active.add(key)
        if not status.get('running', True): return
        known = status.get('auth_state') not in ('unknown', 'connecting', 'error')
        resolved = self.delivered - active if known else set()
        changed = bool(resolved)
        self.delivered -= resolved
        now = self.clock()
        for kind in sorted(active - self.delivered):
            if now < self.retry_at.get(kind, 0): continue
            title, body = MESSAGES[kind]
            if self.sender(title, body):
                self.delivered.add(kind); changed = True; self.retry_at.pop(kind, None)
            else:
                self.retry_at[kind] = now + 60
        if changed:
            try: self._save()
            except OSError:
                log.warning('Não foi possível persistir o estado das notificações')


def desktop_status(snapshot):
    jobs = snapshot.get('queue') or []
    return dict(running=bool(snapshot.get('running')), paused=bool(snapshot.get('paused')),
                auth_state=snapshot.get('auth_state', 'unknown'), auth_message=snapshot.get('auth_message', ''),
                queue_total=snapshot.get('queue_total', len(jobs)),
                active_roots=sum(1 for root in snapshot.get('roots', []) if root.get('enabled')),
                reconnect_pending=bool(snapshot.get('reconnect_pending')),
                issues=dict(auth=snapshot.get('auth_state') == 'required',
                            quota=bool(snapshot.get('error_counts', {}).get('quota')) or any(job.get('error_category') == 'quota' for job in jobs),
                            access=bool(snapshot.get('error_counts', {}).get('access')) or any(job.get('error_category') == 'access' for job in jobs),
                            folders=sum(1 for root in snapshot.get('roots', []) if root.get('enabled') and root.get('status') == 'unavailable')))
