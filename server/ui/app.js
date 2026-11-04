// The shell: sidebar, top bar, status bar, hash router and the background polling. One module per screen lives in views/.
export const APP_NAME = 'ModelDepot';   // the one place to rename the app (index.html has the same text in <title>)

import { h, icon, api, bytes, speed, plural, copyBtn, origin, errorBox, empty, state, on, emit, setJobs, refreshStatus, every,
  themePref, app, setTitle, ApiError } from './lib.js';

app.name = APP_NAME;

const VIEWS = {
  library: './views/library.js', repo: './views/repo.js', download: './views/download.js', upload: './views/upload.js', queue: './views/queue.js',
  storage: './views/storage.js', connect: './views/connect.js', settings: './views/settings.js',
};
const NAV = [['library', 'Library', 'library'], ['download', 'Download', 'download'], ['upload', 'Upload', 'upload'], ['queue', 'Queue', 'queue'],
  ['storage', 'Storage', 'storage'], ['connect', 'Connect', 'connect'], ['settings', 'Settings', 'settings']];

// ---------------------------------------------------------------- theme
export function applyTheme() {
  const t = themePref.get();
  if (t === 'dark' || t === 'light') document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
}
applyTheme();
on('theme', applyTheme);

// ---------------------------------------------------------------- shell
const el = {
  count: h('span', { class: 'nav-count' }), badge: h('span', { class: 'nav-badge', hidden: true }), up: h('span', { class: 'nav-badge', hidden: true, title: 'An upload is running' }, '↑'),
  search: h('input', { type: 'search', id: 'q', placeholder: 'Search the library', 'aria-label': 'Search the library (does not search Hugging Face)', autocomplete: 'off', spellcheck: 'false' }),
  xfer: h('a', { class: 'xfer idle', href: '#/queue', title: 'Open the download queue' }),
  main: h('main', { id: 'main', tabindex: '-1' }),
  status: h('footer', { id: 'statusbar' }),
};

function nav() {
  return h('nav', { 'aria-label': 'Main' }, NAV.map(([key, label, ic]) => h('a', { href: '#/' + key, 'data-nav': key },
    icon(ic, 18), h('span', { class: 'nav-label' }, label), key === 'library' ? el.count : key === 'queue' ? el.badge : key === 'upload' ? el.up : null)));
}

function buildShell() {
  const root = document.getElementById('app');
  root.replaceChildren(
    h('button', { class: 'skip', type: 'button', onclick: () => el.main.focus() }, 'Skip to content'),
    h('aside', { id: 'side' }, h('a', { class: 'brand', href: '#/library' }, h('img', { src: '/ui/favicon.svg', alt: '', width: 22, height: 22 }), h('span', null, APP_NAME)), nav()),
    h('header', { id: 'top' },
      h('form', { class: 'search', role: 'search', onsubmit: (e) => { e.preventDefault(); const q = el.search.value.trim(); location.hash = '#/library' + (q ? '?q=' + encodeURIComponent(q) : ''); el.search.blur(); } },
        icon('search', 16), el.search, h('kbd', { 'aria-hidden': 'true' }, '/')),
      el.xfer),
    el.main, el.status);
}

function renderXfer() {
  const s = state.jobs ? state.jobs.summary : { running: 0, queued: 0, speed_bps: 0, checks_running: 0 };
  const parts = state.offline ? ['server not reachable'] : [
    s.running && `${plural(s.running, 'download')}${s.speed_bps >= 1 ? ' · ↓ ' + speed(s.speed_bps) : ''}`, s.queued && `${s.queued} waiting`, s.checks_running && 'checking files'].filter(Boolean);
  el.xfer.replaceChildren(...(parts.length ? [parts.join(' · ')] : [icon('arrowdown', 14), 'No downloads']));
  el.xfer.classList.toggle('idle', !parts.length);
  const active = s.running + s.queued + (s.checks_running || 0);
  el.badge.textContent = String(active);
  el.badge.hidden = !active;
}

function renderStatus(s, failed) {
  const bar = el.status;
  bar.replaceChildren();
  if (!s) {
    bar.append(h('span', { class: 'sb' + (failed ? ' bad' : '') }, failed ? 'The server cannot be reached right now.' : 'Connecting…'));
    return;
  }
  const free = s.total_bytes ? s.free_bytes / s.total_bytes : 1;
  const cls = free < 0.05 ? 'bad' : free < 0.10 ? 'warn' : '';
  bar.append(
    h('span', { class: 'sb path', title: s.store_path }, h('span', { class: 'sb-l' }, 'Store'), s.store_path),
    h('span', { class: 'sb ' + cls, title: cls ? 'Low on disk space' : '' }, h('span', { class: 'sb-l' }, 'Free'), `${bytes(s.free_bytes)} of ${bytes(s.total_bytes)}`),
    h('span', { class: 'sb grow' }),
    h('span', { class: 'sb' }, h('span', { class: 'sb-l' }, 'HF_ENDPOINT'), h('code', null, origin()), copyBtn(origin(), { label: 'Copy', title: 'Copy the server address' })),
    h('a', { class: 'sb ' + (s.hf_token_set ? '' : 'warn'), href: '#/settings', title: 'Hugging Face token (Settings)' }, icon('lock', 13),
      s.hf_token_set ? 'HF token set' : 'no token: gated models locked'));
}

on('status', (e) => {
  const s = e.detail;
  el.count.textContent = String(s.models + s.datasets);
  renderStatus(s);
  renderXfer();
});
on('jobs', renderXfer);
on('upload', (e) => { el.up.hidden = !e.detail; });

// ---------------------------------------------------------------- router
export function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, query = ''] = raw.split('?');
  const parts = path.split('/').filter(Boolean).map((p) => { try { return decodeURIComponent(p); } catch { return p; } });
  return { name: parts[0] || 'library', parts: parts.slice(1), query: Object.fromEntries(new URLSearchParams(query)), raw: location.hash };
}

let cleanup = null, seq = 0, first = true;

async function render() {
  const mine = ++seq;
  if (cleanup) { try { cleanup(); } catch { /* ignore */ } cleanup = null; }
  const route = parseHash();
  for (const a of document.querySelectorAll('#side nav a')) {
    const active = a.dataset.nav === (route.name === 'repo' ? 'library' : route.name);
    if (active) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  el.search.value = route.name === 'library' ? route.query.q || '' : '';
  const page = h('div', { class: 'page' });
  el.main.replaceChildren(page);
  window.scrollTo(0, 0);
  el.main.scrollTop = 0;
  if (!first) el.main.focus({ preventScroll: true });
  first = false;
  const path = VIEWS[route.name];
  if (!path) {
    setTitle('Not found');
    page.append(empty('Page not found', `There is nothing at ${location.hash || '#/'}.`, h('a', { class: 'btn primary', href: '#/library' }, 'Go to the library')));
    return;
  }
  setTitle(NAV.find((n) => n[0] === route.name)?.[1] || '');
  try {
    const mod = await import(path);
    const stop = await mod.default(page, route);
    if (mine !== seq) { if (stop) stop(); return; }
    cleanup = stop || null;
  } catch (e) {
    page.replaceChildren(errorBox(e instanceof TypeError ? new ApiError(0, 'This screen could not be loaded: the server is not answering. Check that the NAS is on.') : e, () => render()));
  }
}

// ---------------------------------------------------------------- polling and keys
async function pollJobs() {
  if (Date.now() - state.jobsAt < 3000) return;                 // the Queue page polls faster and feeds the same state
  try { state.offline = false; setJobs(await api('/jobs', { retry: false })); } catch (e) { if (e.status === 0) { state.offline = true; renderXfer(); } }
}
let lastDone = null;
on('jobs', (e) => {
  const done = e.detail.jobs.filter((j) => j.status === 'done').length;
  if (lastDone !== null && done !== lastDone) { refreshStatus({ quiet: true }).catch(() => {}); emit('library-changed'); }
  lastDone = done;
});

document.addEventListener('keydown', (e) => {
  const t = e.target, typing = t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable);
  if (((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') || (e.key === '/' && !typing && !e.ctrlKey && !e.metaKey && !document.querySelector('dialog[open]'))) {
    e.preventDefault();
    el.search.focus();
    el.search.select();
  } else if (e.key === 'Escape' && t === el.search) el.search.blur();
});

async function boot() {
  buildShell();
  renderStatus(null);
  window.addEventListener('hashchange', render);
  try { await refreshStatus(); } catch (e) { renderStatus(null, true); }
  if (!location.hash) location.replace('#/library');
  render();
  every(pollJobs, 4000);
  every(() => refreshStatus({ quiet: true }).catch(() => renderStatus(state.status, !state.status)), 30000, { immediate: false });
}
boot();
