/* Analytics is loaded only after opt-in, exclusively on the public site. */
(() => {
  'use strict';
  if (location.hostname !== 'files2gdrive.erservices.top') return;
  const id = 'G-K4FX8QK1YV';
  const key = 'files2gdrive-analytics-v1';
  let accepted = false;
  let started = false;
  function readChoice() {
    try { return localStorage.getItem(key); } catch (_) { return null; }
  }
  function start() {
    window['ga-disable-' + id] = false;
    if (started) return;
    started = true;
    window.dataLayer = window.dataLayer || [];
    window.gtag = function () { window.dataLayer.push(arguments); };
    window.gtag('consent', 'default', {
      analytics_storage: 'granted', ad_storage: 'denied',
      ad_user_data: 'denied', ad_personalization: 'denied'
    });
    window.gtag('js', new Date());
    window.gtag('config', id, {
      cookie_domain: 'none', cookie_expires: 60 * 60 * 24 * 365,
      allow_google_signals: false, allow_ad_personalization_signals: false,
      page_location: location.origin + location.pathname
    });
    const script = document.createElement('script');
    script.async = true;
    script.src = 'https://www.googletagmanager.com/gtag/js?id=' + id;
    document.head.appendChild(script);
  }
  const panel = document.createElement('section');
  panel.className = 'analytics-consent';
  panel.setAttribute('aria-label', 'Preferências de estatísticas');
  panel.innerHTML = '<p>Podemos usar o Google Analytics para entender as visitas e os cliques de download? Você pode recusar e continuar usando o site. <a href="/privacidade/">Saiba mais</a>.</p><div><button type="button" class="button secondary" data-choice="denied">Recusar</button> <button type="button" class="button primary" data-choice="granted">Aceitar estatísticas</button></div>';
  document.body.appendChild(panel);
  const settings = document.createElement('button');
  settings.type = 'button';
  settings.className = 'analytics-settings';
  settings.textContent = 'Preferências de cookies';
  settings.addEventListener('click', () => {
    panel.hidden = false;
    panel.querySelector('button').focus();
  });
  document.querySelector('footer').appendChild(settings);
  function apply(choice) {
    accepted = choice === 'granted';
    if (accepted) {
      start();
      window.gtag('consent', 'update', {analytics_storage: 'granted'});
    } else {
      window['ga-disable-' + id] = true;
      // Host-only cookies are scoped to this site, so withdrawal can remove them.
      document.cookie.split(';').forEach(cookie => {
        const name = cookie.split('=')[0].trim();
        if (name === '_ga' || name.startsWith('_ga_')) {
          document.cookie = name + '=; Max-Age=0; path=/';
        }
      });
    }
  }
  panel.addEventListener('click', event => {
    const button = event.target.closest('[data-choice]');
    if (!button) return;
    const choice = button.dataset.choice;
    try { localStorage.setItem(key, choice); } catch (_) { /* Session only. */ }
    apply(choice);
    panel.hidden = true;
    settings.focus();
  });
  const choice = readChoice();
  panel.hidden = choice === 'granted' || choice === 'denied';
  apply(choice);
  window.addEventListener('storage', event => {
    if (event.key !== key && event.key !== null) return;
    const current = readChoice();
    apply(current);
    panel.hidden = current === 'granted' || current === 'denied';
  });
  document.addEventListener('click', event => {
    const link = event.target.closest('a[data-download]');
    if (!accepted || !link) return;
    window.gtag('event', 'download_click', {
      file_name: new URL(link.href).pathname.split('/').pop(),
      link_url: link.href, transport_type: 'beacon'
    });
  });
})();
