'use strict';
const $ = id => document.getElementById(id);
const folderSelection = {loaded: false, loading: false, navigating: false, busy: false, home: '', path: '', parent: null, folders: [], truncated: false, baseRules: [], draftRules: [], revision: '', legacyRoots: [], blockedPaths: [], preview: null, reloadSuggested: false, pendingMessage: ''};
const state = {csrf: '', status: null, view: 'overview', preview: null, taskSeen: null, backups: [], files: [], browse: null, polling: false, connectionError: false};
const views = {overview: 'Visão geral', folders: 'Pastas', filters: 'Filtros', activity: 'Atividade', recovery: 'Recuperar', settings: 'Configurações'};
const operationNames = {upload: 'Enviar arquivo', retain: 'Mover para Excluídos', trash: 'Enviar à lixeira', restore: 'Restaurar', scan: 'Verificar arquivos'};
function element(tag, text, className) {const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node;}
function replace(id, children) {$(id).replaceChildren(...children);}
function error(message) {$('error').textContent = message; $('error').classList.remove('hidden');}
function clearError() {$('error').classList.add('hidden');}
function notify(message) {$('notice').textContent = message; $('notice').classList.remove('hidden'); window.setTimeout(() => $('notice').classList.add('hidden'), 8000);}
function empty(title, subtitle, symbol = '✓') {const box = element('div', undefined, 'empty'); box.append(element('span', symbol, 'empty-symbol'), element('strong', title)); if (subtitle) box.append(element('p', subtitle)); return box;}
async function api(path, method = 'GET', body) {
  const headers = {'Accept': 'application/json'};
  if (method !== 'GET') {headers['X-CSRF-Token'] = state.csrf; if (body !== undefined) headers['Content-Type'] = 'application/json';}
  const response = await fetch(path, {method, credentials: 'same-origin', headers, body: body === undefined ? undefined : JSON.stringify(body)});
  let data; try {data = await response.json();} catch (_) {throw new Error('Não foi possível interpretar a resposta do aplicativo.');}
  if (!response.ok) {const detail = data.detail ?? data.error ?? 'Não foi possível concluir a operação.'; throw new Error(typeof detail === 'string' ? detail : 'Verifique os campos e tente novamente.');}
  return data;
}
async function action(button, operation) {
  clearError(); const disabled = button?.disabled;
  if (button) {button.disabled = true; button.classList.add('busy');}
  try {return await operation();} catch (failure) {error(failure.message || 'Ocorreu uma falha. Tente novamente.');}
  finally {if (button) {button.disabled = disabled; button.classList.remove('busy');} syncButtonStates();}
}
function syncButtonStates() {
  const taskBusy = ['pending', 'running', 'queued'].includes(state.status?.task?.state);
  const connecting = state.status?.auth_state === 'connecting' || (taskBusy && ['login', 'auth'].includes(state.status?.task?.kind));
  for (const id of ['banner-login', 'settings-login']) $(id).disabled = connecting || state.status?.backend === 'simulator';
  $('save-filters').disabled = !state.preview;
  $('toggle-pause').disabled = !state.status || Boolean(state.status.reconnect_pending);
  for (const id of ['load-backups', 'load-backup-files']) $(id).disabled = taskBusy;
  syncSelectionButtons();
}
function setView(view) {
  state.view = view; for (const key of Object.keys(views)) $('view-' + key).classList.toggle('hidden', key !== view);
  document.querySelectorAll('.nav').forEach(button => {const active = button.dataset.view === view; button.classList.toggle('active', active); if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');});
  $('breadcrumb').textContent = views[view]; if (view === 'activity') action(null, loadLogs);
  if (view === 'folders') {if (!folderSelection.loaded && !folderSelection.loading) action(null, () => loadFolderSelection()); else renderFolderSelection();}
}
function rootPatterns(root) {try {return Array.isArray(root.patterns) ? root.patterns : JSON.parse(root.patterns || '[]');} catch (_) {return [];}}
function option(value, text) {const node = element('option', text); node.value = value; return node;}
function fillRootOptions(roots) {
  const active = roots.filter(root => root.enabled); const selected = $('filter-scope').value; const logSelected = $('log-root').value;
  $('filter-scope').replaceChildren(option('', 'Todas as pastas — regras globais'), ...active.map(root => option(root.id, root.name)));
  $('log-root').replaceChildren(option('', 'Todas as pastas'), ...roots.map(root => option(root.id, root.name)));
  if (active.some(root => root.id === selected)) $('filter-scope').value = selected;
  if (roots.some(root => root.id === logSelected)) $('log-root').value = logSelected;
}
function renderDriveConnection(data, unavailable = false) {
  const badge = $('backend-chip');
  let label = 'Google Drive · Verificando autenticação…';
  let tone = 'pending';
  let explanation = 'Aguardando o estado da autenticação informado pelo motor local.';
  if (unavailable) {label = 'Conexão local indisponível'; tone = 'warning'; explanation = 'Não foi possível consultar o aplicativo. O estado da autenticação não pôde ser atualizado.';}
  else if (data.backend === 'simulator') {label = 'Modo de teste local'; tone = 'pending'; explanation = 'O armazenamento simulado local está em uso.';}
  else if (data.auth_state === 'connected') {label = 'Google Drive · Autenticado'; tone = 'connected'; explanation = 'Conta autorizada. O aplicativo avisa se for necessário autenticar novamente.';}
  else if (data.auth_state === 'connecting') {label = 'Google Drive · Autenticando…'; explanation = 'Conclua a autorização na janela do Google.';}
  else if (data.auth_state === 'required') {label = 'Google Drive · Autenticação necessária'; tone = 'warning'; explanation = data.auth_message || 'Conecte sua conta para retomar os envios.';}
  else if (data.auth_state === 'error') {label = 'Google Drive · Falha na conexão'; tone = 'warning'; explanation = data.auth_message || 'Não foi possível verificar a conexão com o Drive.';}
  badge.textContent = label; badge.className = 'chip connection-' + tone; badge.title = explanation;
}
function renderStatus(data) {
  const previousRoots = state.status?.roots || []; const roots = data.roots || []; const rootsChanged = JSON.stringify(previousRoots.map(root => [root.id, root.name, root.enabled])) !== JSON.stringify(roots.map(root => [root.id, root.name, root.enabled]));
  const initial = !state.status; state.status = data;
  const active = roots.filter(root => root.enabled); const queue = data.queue || []; const isSimulator = data.backend === 'simulator';
  renderDriveConnection(data);
  $('stat-folders').textContent = String(active.length); $('stat-queue').textContent = String(data.queue_total ?? queue.length);
  const required = ['required', 'error'].includes(data.auth_state); const connecting = data.auth_state === 'connecting';
  $('connection-banner').classList.toggle('hidden', isSimulator || (!required && !connecting && data.auth_state !== 'unknown'));
  $('connection-title').textContent = connecting ? 'Conclua a conexão no navegador' : required ? 'Sua conta precisa de atenção' : 'Conecte seu Google Drive';
  $('connection-message').textContent = data.auth_message || (connecting ? 'Entre na sua conta Google e conceda as permissões solicitadas.' : 'Autorize o acesso para começar seu backup.');
  $('banner-login').disabled = connecting; $('banner-login').textContent = connecting ? 'Aguardando autorização…' : 'Conectar ao Google';
  $('settings-login').disabled = connecting || isSimulator; $('settings-login').textContent = connecting ? 'Aguardando autorização…' : data.auth_state === 'connected' ? 'Autenticar novamente' : 'Conectar ao Google';
  $('settings-auth-state').textContent = isSimulator ? 'Modo de teste: os arquivos ficam no armazenamento simulado local.' : data.auth_state === 'connected' ? 'Sua conta está conectada ao Google Drive.' : data.auth_message || 'Conecte sua conta Google para iniciar.';
  const paused = Boolean(data.paused); const pendingReconnect = Boolean(data.reconnect_pending);
  const title = pendingReconnect ? 'Reconexão aguardando conferência' : paused ? 'Backup pausado' : required ? 'Aguardando autenticação' : !active.length ? 'Tudo pronto para começar' : queue.length ? 'Cuidando dos seus arquivos' : 'Seu backup está em dia';
  const description = pendingReconnect ? 'Conclua ou cancele a conferência da reconexão antes de retomar.' : paused ? 'A fila está guardada. Os prazos de retenção continuam correndo.' : required ? 'Seus arquivos pendentes continuam na fila até você conectar novamente.' : !active.length ? 'Adicione uma pasta para manter uma cópia dos seus arquivos no Drive.' : queue.length ? 'As alterações são processadas automaticamente. Você pode continuar trabalhando.' : 'Monitorando alterações nas pastas selecionadas.';
  $('backup-state').textContent = title; $('backup-description').textContent = description;
  $('status-orb').textContent = paused || pendingReconnect ? 'Ⅱ' : required ? '!' : '✓'; $('status-orb').classList.toggle('paused', paused || required || pendingReconnect);
  $('toggle-pause').textContent = paused ? 'Retomar backup' : 'Pausar backup'; $('toggle-pause').disabled = pendingReconnect;
  renderQueue(queue);
  if (initial || rootsChanged) {fillRootOptions(roots); loadFilterFields();}
  if (initial) {$('drive-folder').value = data.settings?.folder_name || 'Files2GDrive'; $('computer-name').value = data.settings?.computer_name || '';}
  const progress = data.progress;
  $('upload-progress').classList.toggle('hidden', !progress);
  if (progress) {
    const percent = progress.total > 0 ? Math.min(100, Math.round((progress.bytes || 0) * 100 / progress.total)) : 0;
    $('upload-path').textContent = progress.path || 'Envio em andamento';
    $('upload-percent').textContent = percent + '%'; $('upload-bar').value = percent;
  }
  renderTask(data.task); syncButtonStates();
}
function renderQueue(queue) {
  if (!queue.length) {replace('queue-list', [empty('Nenhuma operação pendente', 'Novos arquivos e alterações aparecerão aqui.')]); return;}
  replace('queue-list', queue.slice(0, 60).map(job => {const row = element('div', undefined, 'row'); const main = element('div', undefined, 'row-main'); main.append(element('strong', job.path || 'Operação do backup'), element('small', job.error || operationNames[job.operation] || job.operation)); row.append(element('div', '↑', 'row-icon'), main, element('span', job.error ? 'Nova tentativa' : 'Na fila', 'badge' + (job.error ? ' warning' : ''))); return row;}));
}
async function refresh() {if (!state.csrf) state.csrf = (await api('/api/session')).csrf_token; renderStatus(await api('/api/status'));}
function invalidatePreview() {state.preview = null; $('save-filters').disabled = true; $('filter-preview').classList.add('hidden');}
function loadFilterFields() {if (!state.status) return; const selected = $('filter-scope').value; const root = (state.status.roots || []).find(item => item.id === selected); $('filter-patterns').value = (root ? rootPatterns(root) : state.status.settings?.global_patterns || []).join('\n'); $('filter-ignore-case').checked = Boolean(root ? root.ignore_case : state.status.settings?.global_ignore_case); invalidatePreview();}
function filterPayload() {return {root_id: $('filter-scope').value || null, patterns: $('filter-patterns').value.split('\n').map(line => line.replace(/\r$/, '')).filter(line => line.length > 0), ignore_case: $('filter-ignore-case').checked};}
async function previewFilters() {const payload = filterPayload(); const preview = await api('/api/filters/preview', 'POST', payload); state.preview = {payload, id: preview.preview_id}; $('save-filters').disabled = false; const box = $('filter-preview'); box.replaceChildren(element('h2', 'Conferência das regras'), element('p', String(preview.matched_count ?? 0) + ' item(ns) correspondem aos padrões.')); if (preview.retention_count) box.append(element('p', String(preview.retention_count) + ' cópia(s) já enviadas entrarão em Excluídos por 30 dias.')); const list = element('ul'); for (const path of preview.examples || []) list.append(element('li', path)); box.append(list); box.classList.remove('hidden');}
function logQuery() {const query = new URLSearchParams(); if ($('log-level').value) query.set('level', $('log-level').value); if ($('log-root').value) query.set('root_id', $('log-root').value); if ($('log-since').value) query.set('since', new Date($('log-since').value).toISOString()); return query.toString();}
async function loadLogs() {const data = await api('/api/logs?' + logQuery()); const entries = data.entries || []; if (!entries.length) {replace('log-list', [empty('Nenhum registro neste período', 'Tente ampliar os filtros ou aguarde uma operação.')]); return;} replace('log-list', entries.slice().reverse().map(entry => {const row = element('div', undefined, 'log-entry'); const meta = element('div', undefined, 'log-meta'); const level = {INFO: 'Informação', WARNING: 'Aviso', ERROR: 'Erro'}[entry.level] || entry.level; const date = new Date(entry.timestamp); meta.append(element('span', level, 'badge' + (entry.level === 'WARNING' ? ' warning' : entry.level === 'ERROR' ? ' error' : '')), element('time', Number.isNaN(date.valueOf()) ? entry.timestamp : date.toLocaleString('pt-BR'))); row.append(meta, element('p', [operationNames[entry.operation] || entry.operation || entry.message, entry.result === 'error' ? 'Falha' : '', entry.reason].filter(Boolean).join(' · '))); if (entry.path) row.append(element('code', entry.path)); return row;}));}
function renderTask(task) {
  if (!task) {$('task-status').classList.add('hidden'); return;}
  const busy = ['pending', 'running', 'queued'].includes(task.state);
  const labels = {login: 'Aguardando autorização no navegador', auth: 'Aguardando autorização no navegador', catalog: 'Buscando backups no Drive', recovery_catalog: 'Buscando backups no Drive', files: 'Buscando arquivos do backup', recovery_files: 'Buscando arquivos do backup', restore: 'Restaurando os arquivos selecionados', recovery_restore: 'Restaurando os arquivos selecionados'};
  $('task-status').classList.toggle('hidden', !busy); $('task-status').textContent = (labels[task.kind] || 'Processando operação') + '…';
  const signature = task.id + ':' + task.state;
  if (busy || signature === state.taskSeen) return;
  state.taskSeen = signature;
  if (['failed', 'error'].includes(task.state)) {error(task.error || 'A operação não foi concluída.'); return;}
  if (!['completed', 'done', 'success', 'succeeded'].includes(task.state)) return;
  if (['catalog', 'recovery_catalog'].includes(task.kind)) {state.backups = Array.isArray(task.result) ? task.result : task.result?.backups || []; renderBackups();}
  else if (['files', 'recovery_files'].includes(task.kind)) {state.files = Array.isArray(task.result) ? task.result : task.result?.files || []; renderRecoveryFiles();}
  else if (['restore', 'recovery_restore'].includes(task.kind)) {notify('Restauração concluída. Os arquivos foram recuperados sem ativar monitoramento.');}
  else if (['login', 'auth'].includes(task.kind)) notify('Conta Google conectada.');
}
function renderBackups() {$('recovery-computer').replaceChildren(option('', state.backups.length ? 'Selecione o computador' : 'Nenhum backup encontrado'), ...state.backups.map(backup => option(backup.computer_id, backup.name || backup.computer_id))); $('recovery-root').replaceChildren(option('', 'Selecione um computador')); state.files = []; renderRecoveryFiles();}
function renderRecoveryRoots() {const backup = state.backups.find(item => item.computer_id === $('recovery-computer').value); $('recovery-root').replaceChildren(option('', 'Selecione a pasta'), ...(backup?.roots || []).map(root => option(root.root_id, root.name || root.root_id))); state.files = []; renderRecoveryFiles();}
function renderRecoveryFiles() {if (!state.files.length) {replace('recovery-files', [empty('Nenhum arquivo selecionado', 'Escolha o computador e a pasta para consultar os arquivos.', '↶')]); $('restore-form').classList.add('hidden'); return;} const header = element('label', undefined, 'check'); const all = element('input'); all.type = 'checkbox'; all.checked = true; header.append(all, document.createTextNode('Selecionar todos (' + state.files.length + ')')); all.addEventListener('change', () => document.querySelectorAll('.restore-file-check').forEach(input => input.checked = all.checked)); const rows = state.files.map(entry => {const label = element('label', undefined, 'recovery-check'); const check = element('input'); check.type = 'checkbox'; check.checked = true; check.value = entry.file_id; check.className = 'restore-file-check'; label.append(check, element('span', entry.path), element('small', entry.state === 'excluded' ? 'Em Excluídos' : 'Ativo', 'badge' + (entry.state === 'excluded' ? ' warning' : ''))); return label;}); replace('recovery-files', [header, ...rows]); $('restore-form').classList.remove('hidden');}
async function beginTask(path, body) {const response = await api(path, 'POST', body); if (response.task) renderTask(response.task); await refresh();}
// Rules are directory selections, independent from the existing glob filters.
function selectionWithin(path, parent) {return path === parent || path.startsWith(parent === '/' ? '/' : parent + '/');}
function canonicalSelectionRules(rules) {
  const byPath = new Map();
  for (const rule of Array.isArray(rules) ? rules : []) {
    if (!rule || typeof rule.path !== 'string' || typeof rule.included !== 'boolean') continue;
    const path = rule.path.length > 1 ? rule.path.replace(/\/+$/, '') : rule.path;
    if (!path.startsWith('/') || path.includes('\0') || path.split('/').slice(1).some(part => !part || part === '.' || part === '..')) continue;
    byPath.set(path, {path, included: rule.included});
  }
  const ordered = [...byPath.values()].sort((a, b) => a.path.length - b.path.length || a.path.localeCompare(b.path));
  const result = [];
  for (const rule of ordered) {if (selectionIncluded(rule.path, result) !== rule.included) result.push(rule);}
  return result;
}
function selectionIncluded(path, rules = folderSelection.draftRules) {
  let match = null;
  for (const rule of rules) if (selectionWithin(path, rule.path) && (!match || rule.path.length > match.path.length)) match = rule;
  return match ? match.included : false;
}
function selectionMode(path) {
  if (folderSelection.blockedPaths.some(blocked => selectionWithin(path, blocked))) return 'excluded';
  const own = selectionIncluded(path);
  const mixed = folderSelection.draftRules.some(rule => rule.path !== path && selectionWithin(rule.path, path) && rule.included !== own);
  const internal = own && folderSelection.blockedPaths.some(blocked => selectionWithin(blocked, path));
  return mixed || internal ? 'partial' : own ? 'included' : 'excluded';
}
function selectionDirty() {return JSON.stringify(folderSelection.baseRules) !== JSON.stringify(folderSelection.draftRules);}
function selectionRelative(path) {return path === folderSelection.home ? 'Pasta pessoal' : selectionWithin(path, folderSelection.home) ? path.slice(folderSelection.home.length + 1) : path;}
function selectionPayload() {return {rules: folderSelection.draftRules.map(rule => ({...rule})), revision: folderSelection.revision};}
function invalidateSelectionPreview() {folderSelection.preview = null; $('selection-review').classList.add('hidden'); $('selection-review').replaceChildren();}
function syncSelectionButtons() {
  const disabled = !folderSelection.loaded || folderSelection.loading || folderSelection.navigating || folderSelection.busy;
  const reconnectPending = Boolean(state.status?.reconnect_pending);
  $('selection-preview').disabled = disabled || !selectionDirty() || reconnectPending;
  $('selection-discard').disabled = disabled || !selectionDirty();
  $('selection-reload').disabled = folderSelection.loading || folderSelection.navigating || folderSelection.busy;
  $('selection-show-hidden').disabled = disabled;
  for (const control of document.querySelectorAll('#selection-directory-list input, #selection-directory-list button, #selection-breadcrumb button')) control.disabled = disabled || control.dataset.blocked === 'true' || (control.type === 'checkbox' && reconnectPending);
  if ($('selection-apply')) $('selection-apply').disabled = disabled || !folderSelection.preview || reconnectPending;
}
async function selectionOperation(button, operation) {
  return action(button, async () => {
    try {return await operation();}
    catch (failure) {folderSelection.reloadSuggested = true; renderSelectionFeedback(); throw failure;}
  });
}
function renderSelectionFeedback(message) {
  const feedback = $('selection-feedback');
  if (message) {feedback.textContent = message; feedback.classList.remove('hidden'); return;}
  if (folderSelection.busy && folderSelection.pendingMessage) {feedback.textContent = folderSelection.pendingMessage; feedback.classList.remove('hidden');}
  else if (folderSelection.loading || folderSelection.navigating) {feedback.textContent = 'Carregando diretórios…'; feedback.classList.remove('hidden');}
  else if (state.status?.reconnect_pending) {feedback.textContent = 'Conclua ou cancele a conferência de reconexão antes de alterar as pastas.'; feedback.classList.remove('hidden');}
  else if (folderSelection.reloadSuggested) {feedback.textContent = 'Se a seleção foi alterada em outra janela, use “Recarregar seleção”. Suas alterações pendentes foram mantidas.'; feedback.classList.remove('hidden');}
  else {feedback.classList.add('hidden');}
}
async function loadFolderSelection() {
  folderSelection.loading = true; renderSelectionFeedback(); syncSelectionButtons();
  try {
    const configuration = await api('/api/folder-selection');
    if (!configuration || typeof configuration.home !== 'string' || typeof configuration.revision !== 'string') throw new Error('Não foi possível ler a seleção de pastas.');
    const rules = canonicalSelectionRules(configuration.rules);
    const previousPath = folderSelection.path;
    folderSelection.home = configuration.home; folderSelection.revision = configuration.revision;
    folderSelection.baseRules = rules; folderSelection.draftRules = rules.map(rule => ({...rule}));
    folderSelection.blockedPaths = Array.isArray(configuration.blocked_paths) ? configuration.blocked_paths.filter(path => typeof path === 'string') : [];
    folderSelection.legacyRoots = Array.isArray(configuration.legacy_roots) ? configuration.legacy_roots : [];
    folderSelection.loaded = true; folderSelection.reloadSuggested = false; invalidateSelectionPreview();
    await browseSelection(previousPath && selectionWithin(previousPath, configuration.home) ? previousPath : configuration.home);
  } finally {folderSelection.loading = false; renderFolderSelection();}
}
async function browseSelection(path) {
  if (!folderSelection.loaded) return;
  folderSelection.navigating = true; renderSelectionFeedback(); syncSelectionButtons();
  try {
    const directory = await api('/api/folder-selection/browse', 'POST', {path: path || folderSelection.home, show_hidden: $('selection-show-hidden').checked});
    if (!directory || typeof directory.path !== 'string' || !selectionWithin(directory.path, folderSelection.home)) throw new Error('O aplicativo retornou um diretório fora da pasta pessoal.');
    folderSelection.path = directory.path; folderSelection.parent = directory.parent;
    folderSelection.folders = Array.isArray(directory.folders) ? directory.folders : [];
    folderSelection.truncated = Boolean(directory.truncated);
  } finally {folderSelection.navigating = false; renderFolderSelection();}
}
function toggleSelection(path, included) {
  const remaining = folderSelection.draftRules.filter(rule => !selectionWithin(rule.path, path));
  if (selectionIncluded(path, remaining) !== included) remaining.push({path, included});
  folderSelection.draftRules = canonicalSelectionRules(remaining);
  folderSelection.reloadSuggested = false; invalidateSelectionPreview(); renderFolderSelection();
}
function renderSelectionBreadcrumb() {
  const holder = $('selection-breadcrumb'); holder.replaceChildren();
  const parts = folderSelection.path === folderSelection.home ? [] : folderSelection.path.slice(folderSelection.home.length + 1).split('/');
  const crumb = (label, path, current) => {
    const button = element('button', label, 'directory-crumb'); button.type = 'button';
    button.disabled = folderSelection.loading || folderSelection.navigating || folderSelection.busy;
    if (current) button.setAttribute('aria-current', 'location');
    button.addEventListener('click', () => selectionOperation(button, () => browseSelection(path)));
    return button;
  };
  holder.append(crumb('Pasta pessoal', folderSelection.home, !parts.length));
  let path = folderSelection.home;
  for (const part of parts) {path += '/' + part; holder.append(element('span', '/', 'directory-divider'), crumb(part, path, path === folderSelection.path));}
}
function renderFolderSelection() {
  if (!folderSelection.loaded) {renderSelectionFeedback(folderSelection.loading ? 'Carregando sua pasta pessoal…' : 'Abra ou recarregue a seleção para consultar seus diretórios.'); syncSelectionButtons(); return;}
  renderSelectionBreadcrumb(); $('selection-home').textContent = folderSelection.path;
  const list = $('selection-directory-list'); list.replaceChildren();
  if (folderSelection.navigating) list.append(empty('Carregando diretórios', 'A seleção será mantida durante a navegação.', '↻'));
  else if (!folderSelection.folders.length) list.append(empty('Nenhuma subpasta visível', 'A seleção desta pasta e dos diretórios ocultos continua valendo.', '▱'));
  else for (const folder of folderSelection.folders) {
    if (typeof folder.path !== 'string' || !selectionWithin(folder.path, folderSelection.home)) continue;
    const row = element('div', undefined, 'directory-row');
    const mode = selectionMode(folder.path);
    const label = element('label', undefined, 'directory-check');
    const check = element('input'); check.type = 'checkbox'; check.checked = mode === 'included'; check.indeterminate = mode === 'partial';
    check.setAttribute('aria-label', 'Incluir ' + selectionRelative(folder.path) + ' no backup');
    check.dataset.blocked = String(Boolean(folder.blocked));
    check.disabled = Boolean(folder.blocked) || folderSelection.busy || Boolean(state.status?.reconnect_pending);
    check.addEventListener('change', () => toggleSelection(folder.path, check.checked)); label.append(check);
    const open = element('button', undefined, 'directory-open'); open.type = 'button'; open.setAttribute('aria-label', 'Abrir ' + folder.name);
    open.dataset.blocked = String(Boolean(folder.blocked));
    open.disabled = Boolean(folder.blocked) || folderSelection.busy;
    open.append(element('span', '▱', 'directory-icon'), element('span', folder.name, 'directory-name'), element('span', '›', 'directory-chevron'));
    open.addEventListener('click', () => selectionOperation(open, () => browseSelection(folder.path)));
    const status = element('div', undefined, 'directory-state');
    status.append(element('span', folder.blocked ? 'Indisponível' : mode === 'partial' ? 'Seleção parcial' : mode === 'included' ? 'Incluída' : 'Não incluída', 'badge' + (folder.blocked || mode === 'partial' ? ' warning' : '')));
    if (folder.blocked && folder.reason) status.append(element('small', folder.reason));
    if (folder.hidden) open.append(element('small', 'oculta', 'directory-hidden-label'));
    row.append(label, open, status); list.append(row);
  }
  $('selection-truncated').classList.toggle('hidden', !folderSelection.truncated);
  const summary = $('selection-summary'); summary.replaceChildren(element('strong', 'Seleção:'));
  const included = folderSelection.draftRules.filter(rule => rule.included && selectionWithin(rule.path, folderSelection.home));
  const tops = included.filter(rule => !included.some(other => other.path !== rule.path && selectionWithin(rule.path, other.path)));
  if (!tops.length) summary.append(element('span', 'Nenhuma pasta selecionada', 'selection-empty'));
  for (const rule of tops.slice(0, 12)) summary.append(element('span', selectionRelative(rule.path) + (selectionMode(rule.path) === 'partial' ? ' · parcial' : ''), 'selection-chip'));
  if (tops.length > 12) summary.append(element('span', '+ ' + (tops.length - 12) + ' pastas', 'selection-chip'));
  const legacy = folderSelection.legacyRoots.filter(root => root.enabled && typeof root.path === 'string' && !selectionWithin(root.path, folderSelection.home));
  const legacyHolder = $('selection-legacy'); legacyHolder.replaceChildren(); legacyHolder.classList.toggle('hidden', !legacy.length);
  if (legacy.length) {
    const copy = element('div'); copy.append(element('p', 'Pastas fora da pasta pessoal permanecem como estão. Esta seleção não altera esses backups.'));
    const entries = element('ul'); for (const root of legacy) entries.append(element('li', root.name + ' — ' + root.path)); copy.append(entries); legacyHolder.append(element('span', 'ⓘ'), copy);
  }
  $('selection-draft-state').textContent = selectionDirty() ? 'Alterações ainda não aplicadas' : 'Nenhuma alteração pendente';
  renderSelectionFeedback(); syncSelectionButtons();
}
async function folderSelectionRequest(path, payload, message) {
  const accepted = await api(path, 'POST', payload);
  if (!accepted.accepted || !accepted.task_id) return accepted;
  folderSelection.pendingMessage = message; renderSelectionFeedback();
  try {
    while (true) {
      const snapshot = await api('/api/status'); renderStatus(snapshot);
      const task = snapshot.task;
      if (task?.id === accepted.task_id) {
        if (['failed', 'error'].includes(task.state)) throw new Error(task.error || 'A operação não foi concluída.');
        if (['completed', 'done', 'success', 'succeeded'].includes(task.state)) return task.result || {};
      }
      await new Promise(resolve => window.setTimeout(resolve, 1000));
    }
  } finally {folderSelection.pendingMessage = ''; renderSelectionFeedback();}
}
async function previewFolderSelection() {
  const payload = selectionPayload(); folderSelection.busy = true; syncSelectionButtons();
  try {
    const result = await folderSelectionRequest('/api/folder-selection/preview', payload, 'Conferindo arquivos e efeitos da seleção… Em pastas grandes, isso pode levar alguns minutos.');
    folderSelection.preview = {id: result.preview_id, revision: result.revision || payload.revision, payload};
    const holder = $('selection-review'); holder.replaceChildren(element('h2', 'Conferir mudanças'));
    for (const [label, paths] of [['Incluir no backup', result.additions || []], ['Retirar do backup', result.removals || []]]) {
      if (!paths.length) continue; holder.append(element('h3', label)); const list = element('ul');
      for (const path of paths) list.append(element('li', typeof path === 'string' ? path : path.path || 'Pasta selecionada'));
      holder.append(list);
    }
    holder.append(element('p', String(result.upload_count ?? 0) + ' arquivo(s) serão enviados ou atualizados.'));
    if (result.retention_count) holder.append(element('p', String(result.retention_count) + ' cópia(s) já enviadas entrarão em “Excluídos”.', 'selection-warning'));
    if ((result.removals || []).length || result.retention_count) holder.append(element('p', 'As cópias retiradas ficam em “Excluídos” por 30 dias; depois seguem para a lixeira do Drive.', 'selection-warning'));
    for (const warning of result.warnings || []) holder.append(element('p', warning, 'hint'));
    if (result.examples?.length) {
      const details = element('details'); details.append(element('summary', 'Exemplos de arquivos afetados')); const list = element('ul');
      for (const item of result.examples) list.append(element('li', typeof item === 'string' ? item : item.path || 'Arquivo afetado'));
      details.append(list); holder.append(details);
    }
    const apply = element('button', 'Aplicar alterações', 'button primary'); apply.type = 'button'; apply.id = 'selection-apply';
    apply.addEventListener('click', () => selectionOperation(apply, applyFolderSelection)); holder.append(apply); holder.classList.remove('hidden');
    folderSelection.reloadSuggested = false;
  } finally {folderSelection.busy = false; syncSelectionButtons();}
}
async function applyFolderSelection() {
  const preview = folderSelection.preview;
  if (!preview) throw new Error('Revise as alterações antes de aplicar.');
  folderSelection.busy = true; syncSelectionButtons();
  try {
    await folderSelectionRequest('/api/folder-selection/apply', {...preview.payload, revision: preview.revision, preview_id: preview.id}, 'Aplicando a seleção de pastas… Aguarde a conclusão.');
    invalidateSelectionPreview(); await loadFolderSelection(); await refresh(); notify('Seleção de pastas atualizada.');
  } catch (failure) {invalidateSelectionPreview(); throw failure;}
  finally {folderSelection.busy = false; renderFolderSelection();}
}
$('selection-show-hidden').addEventListener('change', () => selectionOperation($('selection-show-hidden'), () => browseSelection(folderSelection.path)));
$('selection-preview').addEventListener('click', () => selectionOperation($('selection-preview'), previewFolderSelection));
$('selection-discard').addEventListener('click', () => {
  folderSelection.draftRules = folderSelection.baseRules.map(rule => ({...rule}));
  folderSelection.reloadSuggested = false; invalidateSelectionPreview(); renderFolderSelection();
});
$('selection-reload').addEventListener('click', () => selectionOperation($('selection-reload'), async () => {
  if (selectionDirty() && !window.confirm('Recarregar a seleção vai descartar suas alterações ainda não aplicadas. Continuar?')) return;
  await loadFolderSelection();
}));

for (const button of document.querySelectorAll('.nav')) button.addEventListener('click', () => setView(button.dataset.view));
$('overview-folders').addEventListener('click', () => setView('folders'));
$('refresh').addEventListener('click', () => action($('refresh'), refresh));
$('toggle-pause').addEventListener('click', () => action($('toggle-pause'), async () => {await api('/api/control', 'POST', {action: state.status.paused ? 'resume' : 'pause'}); await refresh();}));
$('check-now').addEventListener('click', () => action($('check-now'), async () => {await api('/api/control', 'POST', {action: 'check'}); notify('Verificação solicitada.'); await refresh();}));
for (const id of ['banner-login', 'settings-login']) $(id).addEventListener('click', () => action($(id), () => beginTask('/api/auth/login', {})));
$('filter-scope').addEventListener('change', loadFilterFields); $('filter-patterns').addEventListener('input', invalidatePreview); $('filter-ignore-case').addEventListener('change', invalidatePreview);
$('filter-form').addEventListener('submit', event => {event.preventDefault(); action(event.submitter, previewFilters);});
$('save-filters').addEventListener('click', () => action($('save-filters'), async () => {if (!state.preview) throw new Error('Confira os arquivos afetados antes de salvar.'); await api('/api/filters', 'POST', {...state.preview.payload, preview_id: state.preview.id}); invalidatePreview(); notify('Filtros atualizados.'); await refresh();}));
$('logs-form').addEventListener('submit', event => {event.preventDefault(); action(event.submitter, loadLogs);});
$('export-logs').addEventListener('click', () => {window.location.assign('/api/logs/export?' + logQuery());});
$('settings-form').addEventListener('submit', event => {event.preventDefault(); action(event.submitter, async () => {await api('/api/settings', 'POST', {folder_name: $('drive-folder').value.trim(), computer_name: $('computer-name').value.trim()}); notify('Configurações salvas.'); await refresh();});});
$('load-backups').addEventListener('click', () => action($('load-backups'), () => beginTask('/api/recovery/catalog', {})));
$('recovery-computer').addEventListener('change', renderRecoveryRoots);
$('recovery-root').addEventListener('change', () => {state.files = []; renderRecoveryFiles();});
$('load-backup-files').addEventListener('click', () => action($('load-backup-files'), async () => {const computer_id = $('recovery-computer').value, root_id = $('recovery-root').value; if (!computer_id || !root_id) throw new Error('Selecione o computador e a pasta do backup.'); await beginTask('/api/recovery/files', {computer_id, root_id});}));
$('restore-form').addEventListener('submit', event => {event.preventDefault(); action(event.submitter, async () => {const file_ids = [...document.querySelectorAll('.restore-file-check:checked')].map(input => input.value); if (!file_ids.length) throw new Error('Selecione pelo menos um arquivo.'); await beginTask('/api/recovery/restore', {computer_id: $('recovery-computer').value, root_id: $('recovery-root').value, destination: $('restore-destination').value.trim(), file_ids});});});
async function poll() {if (state.polling) return; state.polling = true; try {await refresh(); if(state.connectionError) {clearError(); state.connectionError=false;}} catch (failure) {state.connectionError=true; renderDriveConnection({}, true); error('Não foi possível consultar o aplicativo local. ' + failure.message);} finally {state.polling = false;}}
(async () => {try {state.csrf = (await api('/api/session')).csrf_token; await refresh();} catch (failure) {error('Não foi possível conectar ao aplicativo local. ' + failure.message);} window.setInterval(poll, 2500);})();
