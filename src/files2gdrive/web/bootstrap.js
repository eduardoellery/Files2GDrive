'use strict';
const meta = document.querySelector('meta[name="files2gdrive-session"]');
const token = meta?.content || '';
meta?.remove();
if (/^[A-Za-z0-9_-]{32,128}$/.test(token)) {
  try {
    sessionStorage.setItem('files2gdrive.session', token);
    window.location.replace('/');
  } catch (_) {
    document.body.textContent = 'Não foi possível guardar a sessão do navegador. Reabra a configuração pelo aplicativo.';
  }
} else {
  document.body.textContent = 'Não foi possível iniciar a sessão local. Reabra a configuração pelo aplicativo.';
}
