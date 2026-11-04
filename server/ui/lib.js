// Shared helpers for every screen: DOM builder, API client with token handling, formatting, toasts, dialogs.
// Everything that comes from the network is untrusted: it only ever reaches the page through h() (text nodes), never innerHTML.

// ---------------------------------------------------------------- DOM builder
const NS = 'http://www.w3.org/2000/svg';
const BOOL_PROPS = new Set(['disabled', 'checked', 'hidden', 'required', 'readOnly', 'selected', 'open', 'multiple', 'autofocus']);

export function safeUrl(u, base) {
  try { const x = new URL(u, base); return x.protocol === 'http:' || x.protocol === 'https:' ? x.href : null; } catch { return null; }
}

function add(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false || k === true) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}

/** h('div', {class:'x', onclick: fn}, 'text', child, [more]) */
export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs && (typeof attrs !== 'object' || attrs instanceof Node || Array.isArray(attrs))) { kids.unshift(attrs); attrs = null; }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'style' && typeof v === 'object') for (const [p, val] of Object.entries(v)) el.style.setProperty(p, val);
    else if (k === 'href') { const s = String(v); if (s.startsWith('#') || s.startsWith('/')) el.setAttribute('href', s); else { const u = safeUrl(s); if (u) el.setAttribute('href', u); } }
    else if (BOOL_PROPS.has(k)) el[k] = !!v;
    else if (k === 'value') el.value = v;
    else if (/^(inner|outer)HTML$/i.test(k) || k.startsWith('on')) throw new Error('not allowed: ' + k);
    else el.setAttribute(k, v === true ? '' : v);
  }
  add(el, kids);
  return el;
}

const ICONS = {
  library: 'M4 4h4v16H4zM10 4h4v16h-4zM16 5l4 1-3.5 14-4-1z', download: 'M12 4v11M7 11l5 5 5-5M5 20h14', queue: 'M4 6h16M4 12h16M4 18h10',
  storage: 'M4 7c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 7v10c0 1.7 3.6 3 8 3s8-1.3 8-3V7M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3',
  connect: 'M9 15l6-6M10 6.5l1.5-1.5a4 4 0 015.6 5.6L15.5 12M14 17.5L12.5 19a4 4 0 01-5.6-5.6L8.5 12',
  settings: 'M4 7h10M18 7h2M4 17h2M10 17h10M14 4v6M6 14v6', search: 'M11 4a7 7 0 100 14 7 7 0 000-14zM20 20l-4-4',
  copy: 'M9 9h11v11H9zM5 15V4h10', lock: 'M6 11h12v9H6zM8 11V8a4 4 0 018 0v3', trash: 'M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13',
  pause: 'M8 5v14M16 5v14', play: 'M7 5l12 7-12 7z', retry: 'M20 12a8 8 0 11-2.5-5.8M20 4v5h-5', x: 'M6 6l12 12M18 6L6 18',
  check: 'M5 12l5 5 9-10', ext: 'M14 4h6v6M20 4l-9 9M18 14v6H4V6h6', warn: 'M12 4l9 16H3zM12 10v4M12 17v.5', back: 'M15 6l-6 6 6 6',
  arrowdown: 'M12 5v14M6 13l6 6 6-6', upload: 'M12 19V6M7 10l5-5 5 5M5 20h14',
};

export function icon(name, size = 16) {
  const s = document.createElementNS(NS, 'svg');
  for (const [k, v] of Object.entries({ viewBox: '0 0 24 24', width: size, height: size, fill: 'none', stroke: 'currentColor', 'stroke-width': 2,
    'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', class: 'icon' })) s.setAttribute(k, v);
  const p = document.createElementNS(NS, 'path');
  p.setAttribute('d', ICONS[name] || '');
  s.append(p);
  return s;
}

export const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== '' && v != null && v !== false)).toString();

// ---------------------------------------------------------------- formatting (decimal units, like Hugging Face)
export function bytes(n) {
  if (n == null || Number.isNaN(+n)) return '–';
  n = +n;
  if (n < 1000) return `${n} B`;
  const u = ['kB', 'MB', 'GB', 'TB', 'PB'];
  let i = -1;
  do { n /= 1000; i++; } while (n >= 1000 && i < u.length - 1);
  return `${n >= 100 ? n.toFixed(0) : n.toFixed(1)} ${u[i]}`;
}
export const speed = (bps) => `${bytes(bps)}/s`;
export function eta(s) {
  if (s == null) return '';
  if (s < 60) return `${Math.max(1, Math.round(s))} s left`;
  if (s < 3600) return `${Math.ceil(s / 60)} min left`;
  return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min left`;
}
const toDate = (v) => (typeof v === 'number' ? new Date(v * 1000) : new Date(v));
export function date(v) {
  if (!v) return '–';
  const d = toDate(v);
  return Number.isNaN(+d) ? String(v) : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}
export function dateTime(v) {
  if (!v) return '–';
  const d = toDate(v);
  return Number.isNaN(+d) ? String(v) : d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
export const num = (n) => new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(n || 0);
export const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + 's'}`;
export const pct = (a, b) => (b > 0 ? Math.min(100, Math.max(0, (a / b) * 100)) : 0);
export const origin = () => location.origin;
export const repoHref = (type, id) => `#/repo/${type}/${id.split('/').map(encodeURIComponent).join('/')}`;
export const sourceName = (s) => ({ huggingface: 'Hugging Face', hf: 'Hugging Face', civitai: 'Civitai', local: 'Local', url: 'URL', ollama: 'Ollama' }[s] || s || '–');

// ---------------------------------------------------------------- small widgets
export function badge(text, cls = '') { return h('span', { class: 'badge ' + (cls || 'b-' + String(text).toLowerCase().replace(/[^a-z0-9]/g, '')) }, text); }

export function loading(msg = 'Loading…') { return h('div', { class: 'state', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), msg); }

export function empty(title, text, ...actions) {
  return h('div', { class: 'state empty' }, h('h2', null, title), text && h('p', null, text), actions.length ? h('div', { class: 'row gap' }, actions) : null);
}

export function errorBox(err, retry) {
  const msg = err instanceof ApiError ? err.detail : (err && err.message) || String(err);
  return h('div', { class: 'state error', role: 'alert' }, h('p', null, icon('warn'), ' ', msg),
    retry && h('button', { class: 'btn', onclick: retry }, icon('retry'), 'Try again'));
}

export function seg(options, value, onchange, label) {
  const root = h('div', { class: 'seg', role: 'group', 'aria-label': label || null });
  for (const [v, text] of options) {
    root.append(h('button', { type: 'button', class: 'seg-b', 'aria-pressed': String(v === value), onclick: () => {
      for (const b of root.children) b.setAttribute('aria-pressed', String(b === btn(v)));
      onchange(v);
    }, 'data-v': v }, text));
  }
  const btn = (v) => [...root.children].find((b) => b.dataset.v === v);
  return root;
}

/** Accessible tab strip; the caller shows the matching panel in onselect. */
export function tabs(items, current, onselect, label) {
  const root = h('div', { class: 'tabs', role: 'tablist', 'aria-label': label || null });
  const select = (key, focus) => {
    for (const b of root.children) { const on = b.dataset.key === key; b.setAttribute('aria-selected', String(on)); b.tabIndex = on ? 0 : -1; if (on && focus) b.focus(); }
    onselect(key);
  };
  for (const [key, text, count] of items) {
    root.append(h('button', { type: 'button', role: 'tab', class: 'tab', 'data-key': key, 'aria-selected': String(key === current), tabindex: key === current ? 0 : -1,
      onclick: () => select(key),
      onkeydown: (e) => {
        const bs = [...root.children], i = bs.findIndex((b) => b.dataset.key === key);
        const j = e.key === 'ArrowRight' ? (i + 1) % bs.length : e.key === 'ArrowLeft' ? (i - 1 + bs.length) % bs.length : e.key === 'Home' ? 0 : e.key === 'End' ? bs.length - 1 : -1;
        if (j >= 0) { e.preventDefault(); select(bs[j].dataset.key, true); }
      } }, text, count != null ? h('span', { class: 'count' }, count) : null));
  }
  return root;
}

export async function copyText(text) {
  try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; } } catch { /* fall through */ }
  // The NAS is usually reached over plain http, where the async clipboard API is not available.
  const ta = h('textarea', { 'aria-hidden': 'true', style: { position: 'fixed', top: '0', left: '0', opacity: '0' } });
  ta.value = text;
  document.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { /* ignore */ }
  ta.remove();
  return ok;
}

export function copyBtn(text, { label = 'Copy', small = true, title } = {}) {
  const b = h('button', { type: 'button', class: 'btn' + (small ? ' sm' : '') + (label ? '' : ' icon-only'), title: title || 'Copy to clipboard',
    'aria-label': label ? null : title || 'Copy' }, icon('copy', 14), label ? h('span', null, label) : null);
  b.addEventListener('click', async () => {
    const ok = await copyText(typeof text === 'function' ? text() : text);
    if (!ok) { toast('Could not copy automatically: select the text and press Ctrl+C.', { kind: 'error' }); return; }
    const span = b.querySelector('span');
    if (span) { span.textContent = 'Copied'; setTimeout(() => { span.textContent = label; }, 1500); }
    else { b.classList.add('copied'); setTimeout(() => b.classList.remove('copied'), 1500); }
  });
  return b;
}

/** A block of code or commands with a Copy button. */
export function codeBlock(text, title) {
  return h('div', { class: 'codeblock' }, title && h('div', { class: 'codeblock-h' }, title), h('pre', null, h('code', null, text)), copyBtn(text, { label: 'Copy' }));
}

// ---------------------------------------------------------------- toasts and dialogs
export function toast(msg, { kind = 'info', link, ms } = {}) {
  const box = document.getElementById('toasts');
  if (!box) return;
  const t = h('div', { class: 'toast ' + kind, role: kind === 'error' ? 'alert' : null }, h('span', null, msg),
    link && h('a', { href: link[1] }, link[0]), h('button', { class: 'x', type: 'button', 'aria-label': 'Dismiss', onclick: () => t.remove() }, icon('x', 14)));
  box.append(t);
  setTimeout(() => t.remove(), ms || (kind === 'error' ? 9000 : 5500));
  while (box.children.length > 4) box.firstChild.remove();
}

function openDialog(dlg) {
  return new Promise((resolve) => {
    dlg.addEventListener('close', () => { dlg.remove(); resolve(dlg.returnValue); });
    document.body.append(dlg);
    dlg.showModal();
  });
}

// type=button: Enter inside a dialog must trigger the primary button, never Cancel
const cancelBtn = () => h('button', { type: 'button', class: 'btn', onclick: (e) => e.target.closest('dialog').close('cancel') }, 'Cancel');

/** Resolves true when the user confirms. typeToConfirm: the exact text that must be typed first. */
export async function confirmDialog({ title, body, confirm = 'OK', danger = false, typeToConfirm = null }) {
  const input = typeToConfirm ? h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false', 'aria-label': `Type ${typeToConfirm} to confirm` }) : null;
  const ok = h('button', { class: 'btn ' + (danger ? 'danger' : 'primary'), value: 'ok', disabled: !!input }, confirm);
  if (input) input.addEventListener('input', () => { ok.disabled = input.value.trim() !== typeToConfirm; });
  const dlg = h('dialog', { class: 'modal', 'aria-labelledby': 'dlg-t' },
    h('form', { method: 'dialog' }, h('h2', { id: 'dlg-t' }, title), h('div', { class: 'modal-body' }, body),
      input && h('p', null, 'Type ', h('code', null, typeToConfirm), ' to confirm:'), input,
      h('div', { class: 'modal-actions' }, cancelBtn(), ok)));
  return (await openDialog(dlg)) === 'ok';
}

export function infoDialog(title, body) {
  return openDialog(h('dialog', { class: 'modal', 'aria-labelledby': 'dlg-t' },
    h('form', { method: 'dialog' }, h('h2', { id: 'dlg-t' }, title), h('div', { class: 'modal-body' }, body),
      h('div', { class: 'modal-actions' }, h('button', { class: 'btn primary', value: 'ok' }, 'Close')))));
}

// ---------------------------------------------------------------- tokens and the API client
const LS = { admin: 'modeldepot.adminToken', read: 'modeldepot.readToken', theme: 'modeldepot.theme' };
const ls = {
  get: (k) => { try { return localStorage.getItem(k) || ''; } catch { return ''; } },
  set: (k, v) => { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch { /* private mode */ } },
};
export const tokens = {
  get admin() { return ls.get(LS.admin); }, set admin(v) { ls.set(LS.admin, v); },
  get read() { return ls.get(LS.read); }, set read(v) { ls.set(LS.read, v); },
  clear() { ls.set(LS.admin, ''); ls.set(LS.read, ''); },
};
export const themePref = { get: () => ls.get(LS.theme) || 'auto', set: (v) => ls.set(LS.theme, v === 'auto' ? '' : v) };

export class ApiError extends Error {
  constructor(status, detail) { super(detail); this.status = status; this.detail = detail; }
}

function detailOf(data, status) {
  const d = data && data.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join('; ');
  return status === 404 ? 'Not found.' : `The server answered HTTP ${status}.`;
}

const bearer = (t) => (t ? { Authorization: 'Bearer ' + t } : {});

export async function api(path, { method = 'GET', body, retry = true } = {}) {
  const read = method === 'GET';
  let res;
  try {
    res = await fetch('/api/hub' + path, { method, headers: { ...bearer(read ? tokens.read || tokens.admin : tokens.admin), ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined });
  } catch {
    throw new ApiError(0, 'Cannot reach the server. Is the NAS on, and was this page opened from it?');
  }
  if (res.status === 401 && retry) { await askToken(read ? 'read' : 'admin'); return api(path, { method, body, retry: false }); }
  let data = null;
  try { data = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) throw new ApiError(res.status, detailOf(data, res.status));
  return data;
}

const pendingAsk = {};
/** One token dialog at a time per kind, however many requests got a 401. Resolves when a good token is stored. */
export function askToken(kind) {
  if (!pendingAsk[kind]) pendingAsk[kind] = tokenDialog(kind).finally(() => { delete pendingAsk[kind]; });
  return pendingAsk[kind];
}

async function tokenDialog(kind) {
  const admin = kind === 'admin';
  const err = h('p', { class: 'err', role: 'alert', hidden: true });
  const input = h('input', { type: 'password', autocomplete: 'off', name: 'token', id: 'tok-in', required: true, spellcheck: 'false' });
  const where = h('code', null, 'the admin-token file on the server');
  const save = h('button', { class: 'btn primary', value: 'ok' }, 'Check and save');
  const form = h('form', { method: 'dialog' },
    h('h2', { id: 'dlg-t' }, admin ? 'Admin token needed' : 'Access token needed'),
    h('div', { class: 'modal-body' },
      h('p', null, admin ? 'Changes (downloading, deleting, settings) need the admin token.' : 'This library asks for a read token before it shows anything. Ask whoever runs this server for it (the server calls it MIRROR_AUTH_TOKEN).'),
      admin && h('p', { class: 'muted' }, 'It is stored in ', where, '. Settings shows the path, and the server printed it once in the container log on its first start.'),
      h('label', { for: 'tok-in' }, admin ? 'Admin token' : 'Read token'), input, err),
    h('div', { class: 'modal-actions' }, cancelBtn(), save));
  const dlg = h('dialog', { class: 'modal', 'aria-labelledby': 'dlg-t' }, form);
  if (admin) fetch('/api/hub/settings', { headers: tokens.read || tokens.admin ? { Authorization: 'Bearer ' + (tokens.read || tokens.admin) } : {} })
    .then((r) => r.json()).then((s) => { if (s.admin_token_file) where.textContent = s.admin_token_file; }).catch(() => {});
  let good = false;
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const t = input.value.trim();
    save.disabled = true;
    try {
      const r = await fetch('/api/hub/auth/check', { headers: { Authorization: 'Bearer ' + t } });
      const c = await r.json();
      if (admin && !c.admin) throw new Error(c.read && c.read_token_required ? 'That is the read token, not the admin token.' : 'That token was not accepted.');
      if (!admin && !c.read) throw new Error('That token was not accepted.');
      if (c.admin) tokens.admin = t; else tokens.read = t;
      good = true;
      dlg.close('ok');
    } catch (ex) {
      err.textContent = ex.message || 'Could not check the token.';
      err.hidden = false;
      save.disabled = false;
      input.select();
    }
  });
  await openDialog(dlg);
  if (!good) throw new ApiError(401, admin ? 'The admin token is needed for this.' : 'The access token is needed to look at this library.');
}

// ---------------------------------------------------------------- shared state and timers
export const state = { status: null, jobs: null, jobsAt: 0 };
const bus = new EventTarget();
export const on = (name, fn) => { bus.addEventListener(name, fn); return () => bus.removeEventListener(name, fn); };
export const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, { detail }));

export const app = { name: '' };
export function setTitle(t) { document.title = t ? `${t} · ${app.name}` : app.name; }

export async function refreshStatus({ quiet = false } = {}) {
  state.status = await api('/status', { retry: !quiet });
  emit('status', state.status);
  return state.status;
}
export function setJobs(data) {
  state.jobs = data;
  state.jobsAt = Date.now();
  emit('jobs', data);
}

/** Run fn now and then every ms, but only while the tab is visible. Returns a stop function. */
export function every(fn, ms, { immediate = true } = {}) {
  let timer = null, stopped = false, busy = false;
  const tick = async () => { if (busy || stopped) return; busy = true; try { await fn(); } catch { /* the caller shows errors */ } busy = false; };
  const start = () => { if (!timer && !stopped) timer = setInterval(tick, ms); };
  const stop = () => { clearInterval(timer); timer = null; };
  const vis = () => { if (document.visibilityState === 'visible') { tick(); start(); } else stop(); };
  document.addEventListener('visibilitychange', vis);
  if (document.visibilityState === 'visible') { if (immediate) tick(); start(); }
  return () => { stopped = true; stop(); document.removeEventListener('visibilitychange', vis); };
}

export const go = (hash) => { location.hash = hash; };
export const debounce = (fn, ms = 200) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
