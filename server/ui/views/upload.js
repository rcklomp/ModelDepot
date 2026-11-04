// Upload your own model or dataset: files go one request each into a server-side session (PUT, streamed from disk by the browser),
// then the session is published as one new revision. The state lives at module level, so an upload keeps running if you open another page.
import { h, icon, api, qs, bytes, speed, eta, pct, plural, seg, codeBlock, confirmDialog, askToken, tokens, toast, origin, refreshStatus, setTitle, state, emit, repoHref, ApiError } from '../lib.js';

const NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/;
const SHOW = 200;
const U = { type: 'model', id: '', summary: '', replace: false, files: new Map(), skipped: 0, reading: false, phase: 'edit', session: '', queue: [], err: null, result: null,
  run: 0, xhr: null, doneBytes: 0, doneCount: 0, totalBytes: 0, samples: [] };
let view = null, lib = [], ns = '', R = null, timer = 0;

const newSession = () => (crypto.randomUUID ? crypto.randomUUID() : [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, '0')).join(''));
const busy = () => !['edit', 'done'].includes(U.phase);
const list = () => [...U.files.values()].sort((a, b) => a.path.localeCompare(b.path));
const sizeSum = () => list().reduce((a, f) => a + f.size, 0);

// ---------------------------------------------------------------- reading what the user picked or dropped
/** Walk FileSystemEntry objects (from a drop) into [{file, path}]; directories are read in batches until empty. */
export async function walkEntries(entries) {
  const out = [];
  const walk = async (entry) => {
    if (entry.isFile) out.push({ file: await new Promise((ok, no) => entry.file(ok, no)), path: entry.fullPath.replace(/^\//, '') });
    else if (entry.isDirectory) {
      const reader = entry.createReader();
      for (let batch = await new Promise((ok, no) => reader.readEntries(ok, no)); batch.length; batch = await new Promise((ok, no) => reader.readEntries(ok, no))) {
        for (const e of batch) await walk(e);
      }
    }
  };
  for (const e of entries) await walk(e);
  return out;
}

/** Add [{file, path}]. strip: the one folder that was picked is not part of the repository path (its contents become the top level). */
function addFiles(items, strip) {
  for (const { file, path } of items) {
    const p = strip ? path.split('/').slice(1).join('/') : path;
    if (!p) continue;
    if (p.split('/').some((s) => s.startsWith('.'))) { U.skipped++; continue; }
    U.files.set(p, { file, path: p, size: file.size, status: 'wait', sent: 0 });
  }
}

// ---------------------------------------------------------------- the target repository
function target() {
  const raw = U.id.trim();
  if (!raw) return { error: '' };
  const parts = raw.split('/');
  if (parts.length > 2 || parts.some((s) => !NAME.test(s) || s.includes('..') || s.includes('--'))) return { error: 'Use organisation/name or just a name: letters, digits, - _ and . (at most 96 characters each).' };
  const items = lib.filter((i) => i.type === U.type);
  if (parts.length === 2) return { id: raw, exists: items.some((i) => i.id === raw) };
  if (items.some((i) => i.id === raw)) return { id: raw, exists: true };
  return ns ? { id: `${ns}/${raw}`, exists: items.some((i) => i.id === `${ns}/${raw}`), added: true } : { id: raw, exists: false, unknownNs: true };
}

// ---------------------------------------------------------------- sending
function putFile(f) {
  return new Promise((ok, no) => {
    const x = new XMLHttpRequest();
    U.xhr = x;
    x.open('PUT', '/api/hub/upload/file?' + qs({ type: U.type, id: U.id.trim(), path: f.path, session: U.session }));
    if (tokens.admin) x.setRequestHeader('Authorization', 'Bearer ' + tokens.admin);
    x.upload.onprogress = (e) => {
      f.sent = Math.min(e.loaded, f.size);
      const now = performance.now(), sent = U.doneBytes + f.sent;
      U.samples.push([now, sent]);
      while (U.samples.length > 2 && now - U.samples[0][0] > 6000) U.samples.shift();
      schedule();
    };
    x.onload = () => { let d = null; try { d = JSON.parse(x.responseText); } catch { /* not JSON */ } ok({ status: x.status, detail: d && typeof d.detail === 'string' ? d.detail : `The server answered HTTP ${x.status}.` }); };
    x.onerror = () => no(new ApiError(0, 'The connection to the server broke while sending. Check that the NAS is on, then retry.'));
    x.onabort = () => no(new ApiError(-1, 'cancelled'));
    x.send(f.file);                                   // the File itself: the browser streams it from disk, nothing is read into memory
  });
}

function setPhase(p) {
  U.phase = p;
  window[busy() ? 'addEventListener' : 'removeEventListener']('beforeunload', leaveGuard);
  emit('upload', p === 'uploading' || p === 'committing');
  draw();
}
const leaveGuard = (e) => { e.preventDefault(); e.returnValue = ''; };

async function start() {
  const t = target();
  if (U.phase === 'edit') {
    if (U.replace && t.exists && !(await confirmReplace(t.id))) return;
    U.session = newSession();
    U.queue = list();
    for (const f of U.queue) { f.status = 'wait'; f.sent = 0; }
    U.doneBytes = U.doneCount = 0;
    U.totalBytes = sizeSum();
  }
  const mine = ++U.run;
  U.err = null;
  U.samples = [];
  setPhase('uploading');
  try {
    if (!tokens.admin) await askToken('admin');
    for (const f of U.queue) {
      if (f.status === 'done' || f.status === 'skip') continue;
      f.status = 'up'; f.sent = 0;
      paintRows(f);
      let r = await putFile(f);
      if (mine !== U.run) return;
      if (r.status === 401) { await askToken('admin'); r = await putFile(f); if (mine !== U.run) return; }
      if (r.status !== 200) { f.status = 'fail'; U.err = { file: f, msg: r.detail }; return setPhase('failed'); }
      f.status = 'done'; f.sent = f.size; U.doneBytes += f.size; U.doneCount++;
      paintRows(f);
    }
    setPhase('committing');
    const res = await api('/upload/commit', { method: 'POST', body: { type: U.type, id: U.id.trim(), session: U.session, summary: U.summary.trim() || undefined, replace: U.replace } });
    if (mine !== U.run) return;
    U.result = res;
    U.files.clear();
    U.skipped = 0;
    U.session = '';
    setPhase('done');
    refreshStatus({ quiet: true }).catch(() => {});
    emit('library-changed');
    loadLib();
  } catch (e) {
    if (mine !== U.run || e.status === -1) return;
    const f = U.queue.find((x) => x.status === 'up');
    if (f) f.status = 'fail';
    U.err = { file: f || null, msg: e.detail || e.message, commit: U.phase === 'committing' };
    setPhase('failed');
  }
}

async function discard(message) {
  const s = U.session;
  U.run++;
  if (U.xhr) U.xhr.abort();
  U.session = '';
  U.err = null;
  for (const f of list()) { f.status = 'wait'; f.sent = 0; }
  setPhase('edit');
  if (s) await api('/upload/discard', { method: 'POST', body: { session: s } }).catch((e) => toast(e.detail || e.message, { kind: 'error' }));
  toast(message);
}

const confirmReplace = (id) => confirmDialog({ title: `Replace the files of ${id}?`, confirm: 'Publish and replace', danger: true,
  body: [h('p', null, 'The new revision will contain only the files of this upload. Files that are in the repository now but not in this upload are left out of it.'),
    h('p', { class: 'muted' }, 'The previous revision stays in the repository under Revisions.')] });

async function loadLib() {
  try { lib = (await api('/library')).items; } catch { /* the note about existing repositories is optional */ }
  if (view && U.phase === 'edit') refreshEdit();
}

// ---------------------------------------------------------------- drawing
let E = null;                                           // references into the edit form, so typing does not rebuild it

function draw() {
  if (!view) return;
  view.replaceChildren(h('div', { class: 'page-head' }, h('h1', null, 'Upload')),
    h('p', null, 'Put your own model or dataset on this server. Machines on your network can then fetch it like anything else in the library.'),
    U.phase === 'edit' ? editPanel() : U.phase === 'done' ? donePanel() : runPanel(), cliNote());
}

function editPanel() {
  const idIn = h('input', { type: 'text', id: 'up-id', value: U.id, placeholder: 'my-model   or   org/my-model', spellcheck: 'false', autocomplete: 'off', oninput: () => { U.id = idIn.value; refreshEdit(); } });
  const sumIn = h('input', { type: 'text', id: 'up-sum', value: U.summary, maxlength: '200', placeholder: 'Optional: what is in this upload', oninput: () => { U.summary = sumIn.value; } });
  const rep = h('input', { type: 'checkbox', id: 'up-rep', checked: U.replace, onchange: () => { U.replace = rep.checked; refreshEdit(); } });
  const fileIn = h('input', { type: 'file', multiple: true, class: 'sr', tabindex: '-1', 'aria-hidden': 'true', onchange: () => { addFiles([...fileIn.files].map((file) => ({ file, path: file.name })), false); fileIn.value = ''; refreshEdit(); } });
  const dirIn = h('input', { type: 'file', webkitdirectory: true, class: 'sr', tabindex: '-1', 'aria-hidden': 'true', onchange: () => {
    addFiles([...dirIn.files].map((file) => ({ file, path: file.webkitRelativePath || file.name })), true); dirIn.value = ''; refreshEdit(); } });
  const zone = h('div', { class: 'drop' }, icon('upload', 26), h('p', { class: 'drop-t' }, 'Drop files or a folder here'),
    h('p', { class: 'muted' }, 'A folder is read with everything inside it; its own name is not part of the path.'),
    h('div', { class: 'row gap wrap center' }, h('button', { type: 'button', class: 'btn', onclick: () => fileIn.click() }, 'Choose files'),
      h('button', { type: 'button', class: 'btn', onclick: () => dirIn.click() }, 'Choose a folder')), fileIn, dirIn);
  zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('over'));
  zone.addEventListener('drop', async (e) => {
    e.preventDefault();
    zone.classList.remove('over');
    const dt = e.dataTransfer, entries = [...(dt.items || [])].filter((i) => i.kind === 'file').map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null)).filter(Boolean);
    U.reading = true;
    refreshEdit();
    try {
      if (entries.length) addFiles(await walkEntries(entries), entries.length === 1 && entries[0].isDirectory);
      else addFiles([...dt.files].map((file) => ({ file, path: file.name })), false);
    } catch { toast('Could not read that folder. Try “Choose a folder” instead.', { kind: 'error' }); }
    U.reading = false;
    refreshEdit();
  });
  E = { hint: h('p', { class: 'up-hint', 'aria-live': 'polite' }), repNote: h('p', { class: 'callout', hidden: true }), zoneNote: h('p', { class: 'muted' }), list: h('div'), sum: h('p', { class: 'sum' }),
    space: h('p', { class: 'callout bad', role: 'alert', hidden: true }), btn: h('button', { type: 'button', class: 'btn primary big', onclick: start }, icon('upload', 18), h('span')), why: h('p', { class: 'muted hint' }), zone };
  const form = h('div', { class: 'upload' },
    h('section', { class: 'card' }, h('h2', null, 'Where to'),
      h('div', { class: 'field' }, h('label', null, 'Kind'), seg([['model', 'Model'], ['dataset', 'Dataset']], U.type, (v) => { U.type = v; refreshEdit(); }, 'Kind of repository')),
      h('div', { class: 'field' }, h('label', { for: 'up-id' }, 'Repository id'), idIn, E.hint),
      h('div', { class: 'field' }, h('label', { for: 'up-sum' }, 'Commit message'), sumIn)),
    h('section', { class: 'card' }, h('h2', null, 'Files'), zone, E.zoneNote, E.list, E.sum,
      h('label', { class: 'check' }, rep, h('span', null, h('b', null, 'Replace the repository\'s current files'), h('span', { class: 'muted block' },
        'Files that are in the repository now but not in this upload are removed from the new revision (the old revision stays under Revisions). Off: your files are added to the current ones, and the same path is overwritten.'))),
      E.repNote),
    h('div', { class: 'upload-go' }, E.space, E.btn, E.why));
  refreshEdit();
  return form;
}

function refreshEdit() {
  if (!E || !view || U.phase !== 'edit') return;
  const t = target(), files = list(), total = sizeSum();
  E.hint.className = 'up-hint' + (t.error ? ' err' : ' muted');
  E.hint.textContent = t.error ? t.error : !U.id.trim() ? 'A bare name is fine: the server adds its own organisation (usually “local”).'
    : t.unknownNs ? `Published as ${t.id}, with the server's own organisation in front (usually “local”).` : t.added ? `Will be published as ${t.id}.` : t.exists ? `${t.id} is in the library.` : `${t.id} will be created.`;
  E.repNote.hidden = !(t.exists && t.id);
  E.repNote.replaceChildren(icon('warn', 15), h('span', null, U.replace ? `This adds a new revision to ${t.id}, and the files that are not in this upload are left out of it.` : `This adds a new revision to ${t.id}.`));
  E.zoneNote.textContent = U.reading ? 'Reading the folder…' : U.skipped ? `Skipped ${plural(U.skipped, 'hidden file')} (names starting with a dot, like .DS_Store): the server refuses them.` : '';
  E.zone.classList.toggle('busy', U.reading);
  const rows = files.slice(0, SHOW);
  E.list.replaceChildren(...(files.length ? [h('div', { class: 'files' },
    h('div', { class: 'urow head', 'aria-hidden': 'true' }, h('span', null, 'Path'), h('span', { class: 'r' }, 'Size'), h('span')),
    ...rows.map((f) => h('div', { class: 'urow' }, h('span', { class: 'path' }, f.path), h('span', { class: 'r' }, bytes(f.size)),
      h('button', { type: 'button', class: 'btn sm ghost', 'aria-label': 'Remove ' + f.path, title: 'Remove from this upload', onclick: () => { U.files.delete(f.path); refreshEdit(); } }, icon('x', 13)))),
    files.length > SHOW ? h('p', { class: 'muted pad' }, `… and ${files.length - SHOW} more files.`) : null)] : []));
  E.sum.replaceChildren(...(files.length ? [`${plural(files.length, 'file')} · ${bytes(total)} `, h('button', { type: 'button', class: 'linklike', onclick: () => { U.files.clear(); U.skipped = 0; refreshEdit(); } }, 'Remove all')] : []));
  const free = state.status ? state.status.free_bytes : null;
  const tooBig = free != null && total > free;
  E.space.hidden = !tooBig;
  E.space.replaceChildren(icon('warn', 15), ` Not enough free space (needs ${bytes(total)}, ${bytes(free)} free).`);
  const why = !U.id.trim() ? 'Enter a repository id.' : t.error ? t.error : !files.length ? 'Add some files first.' : tooBig ? '' : null;
  E.btn.disabled = why !== null || U.reading;
  E.btn.lastChild.textContent = files.length ? `Upload · ${plural(files.length, 'file')} · ${bytes(total)}` : 'Upload';
  E.why.textContent = why || '';
  E.why.hidden = !why;
  if (E.cli) E.cli.replaceChildren(codeBlock(cliText()));
}

function runPanel() {
  const failed = U.phase === 'failed', committing = U.phase === 'committing';
  R = { fill: h('div', { class: 'fill' }), stats: h('p', { class: 'job-stats' }), now: h('p', { class: 'path muted' }), nowFill: h('div', { class: 'fill' }), rows: new Map() };
  const bar = (fill, label) => h('div', { class: 'progress' + (failed ? ' bad' : committing ? ' ok' : ''), role: 'progressbar', 'aria-label': label }, fill);
  const rows = U.queue.slice(0, SHOW).map((f) => {
    const st = h('span', { class: 'r ust' });
    R.rows.set(f.path, st);
    return h('div', { class: 'urow' }, h('span', { class: 'path' }, f.path), h('span', { class: 'r' }, bytes(f.size)), st);
  });
  const panel = h('section', { class: 'card' },
    h('h2', null, committing ? `Publishing ${U.id.trim()}…` : failed ? 'The upload stopped' : `Uploading to ${U.id.trim()}`),
    failed ? h('div', { class: 'callout bad', role: 'alert' }, icon('warn', 15), h('div', null, U.err && U.err.file ? h('p', null, h('b', null, 'Could not send '), h('code', null, U.err.file.path)) : null,
      h('p', null, U.err ? U.err.msg : 'Something went wrong.'), h('p', { class: 'muted' }, 'Nothing is published yet. What was already sent stays on the server until you retry or discard it.'))) : null,
    committing ? h('p', { class: 'row gap' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'All files are on the server. Checking them and publishing the new revision…') : null,
    bar(R.fill, 'Overall progress'), R.stats,
    !failed && !committing ? h('div', null, R.now, bar(R.nowFill, 'Current file')) : null,
    h('div', { class: 'row gap' },
      failed ? [h('button', { type: 'button', class: 'btn primary', onclick: () => { if (U.err && U.err.file) U.err.file.status = 'wait'; start(); } }, icon('retry', 14), U.err && U.err.commit ? 'Try to publish again' : 'Retry'),
        U.err && U.err.file ? h('button', { type: 'button', class: 'btn', onclick: () => { U.err.file.status = 'skip'; U.totalBytes -= U.err.file.size; start(); } }, 'Skip this file') : null,
        h('button', { type: 'button', class: 'btn danger', onclick: () => discard('Discarded: everything sent so far was removed from the server.') }, icon('trash', 14), 'Discard all')]
        : committing ? null : h('button', { type: 'button', class: 'btn danger', onclick: () => discard('Cancelled: what was already sent was removed from the server.') }, 'Cancel')),
    h('div', { class: 'files' }, ...rows, U.queue.length > SHOW ? h('p', { class: 'muted pad' }, `… and ${U.queue.length - SHOW} more files.`) : null));
  for (const f of U.queue.slice(0, SHOW)) paintRows(f);
  paintRun();
  return panel;
}

function paintRows(f) {
  const st = R && R.rows.get(f.path);
  if (!st) return;
  st.className = 'r ust ' + f.status;
  st.textContent = f.status === 'wait' ? 'waiting' : f.status === 'up' ? `${Math.round(pct(f.sent, f.size))}%` : f.status === 'done' ? 'done' : f.status === 'fail' ? 'failed' : 'skipped';
}

function schedule() { if (!timer) timer = setTimeout(() => { timer = 0; paintRun(); }, 150); }

function paintRun() {
  if (!R || !view || !busy()) return;
  const cur = U.queue.find((f) => f.status === 'up'), sent = U.doneBytes + (cur ? cur.sent : 0), s = U.samples;
  const bps = s.length > 1 && s[s.length - 1][0] - s[0][0] > 500 ? ((s[s.length - 1][1] - s[0][1]) / (s[s.length - 1][0] - s[0][0])) * 1000 : 0;
  R.fill.style.width = (U.phase === 'committing' ? 100 : pct(sent, U.totalBytes)).toFixed(1) + '%';
  R.stats.textContent = `${U.doneCount} of ${plural(U.queue.length, 'file')} · ${bytes(sent)} of ${bytes(U.totalBytes)}${bps > 0 ? ` · ${speed(bps)}` : ''}${bps > 0 && U.totalBytes > sent ? ` · ${eta((U.totalBytes - sent) / bps)}` : ''}`;
  if (cur) {
    R.now.textContent = `${cur.path} · ${bytes(cur.sent)} of ${bytes(cur.size)}`;
    R.nowFill.style.width = pct(cur.sent, cur.size).toFixed(1) + '%';
    paintRows(cur);
  }
}

function donePanel() {
  const r = U.result, short = r.commit.slice(0, 8);
  return h('section', { class: 'card' }, h('div', { class: 'callout ok', role: 'status' }, icon('check', 15), h('div', null,
    h('p', null, h('b', null, r.created ? `Published ${r.id} at ${short}` : `Nothing changed: ${r.id} already has exactly these files (at ${short})`)),
    h('p', { class: 'muted' }, `${plural(r.files, 'file')} in the repository.${r.created ? ' Machines on your network can fetch it now.' : ''}`))),
  h('div', { class: 'row gap' }, h('a', { class: 'btn primary', href: repoHref(r.type, r.id) }, 'Open the repository'),
    h('button', { type: 'button', class: 'btn', onclick: () => { U.id = ''; U.summary = ''; U.replace = false; U.result = null; setPhase('edit'); } }, 'Upload more')));
}

const cliText = () => [`export HF_ENDPOINT=${origin()}`, 'export HF_HUB_DISABLE_XET=1', 'export HF_TOKEN=<admin token>', '',
  `hf upload ${U.id.trim() || 'my-model'} ./my-folder .${U.type === 'dataset' ? ' --repo-type dataset' : ''}`].join('\n');

function cliNote() {
  const box = h('div', null, codeBlock(cliText()));
  if (E) E.cli = box;
  return h('section', { class: 'card' }, h('h2', null, 'Prefer the command line?'),
    h('p', { class: 'muted' }, 'This does the same as ', h('code', null, 'hf upload'), '. Writing always needs the admin token.'), box);
}

// ---------------------------------------------------------------- the screen
const stopDrops = (e) => e.preventDefault();               // a file dropped beside the zone must not replace the page

export default async function uploadView(page) {
  setTitle('Upload');
  view = page;
  document.addEventListener('dragover', stopDrops);
  document.addEventListener('drop', stopDrops);
  draw();
  refreshStatus({ quiet: true }).then(() => U.phase === 'edit' && refreshEdit()).catch(() => {});
  loadLib();
  api('/api/hub/status').then((st) => { if (st && st.namespace) { ns = st.namespace; refreshEdit(); } }).catch(() => {});      // the organisation a bare name gets (no sign-in needed)
  return () => { view = null; E = null; R = null; document.removeEventListener('dragover', stopDrops); document.removeEventListener('drop', stopDrops); };
}
