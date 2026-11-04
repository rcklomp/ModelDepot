import { h, icon, api, badge, bytes, speed, eta, dateTime, pct, plural, confirmDialog, empty, errorBox, every, loading, setJobs, setTitle, sourceName, toast } from '../lib.js';

function chipOf(j) {
  if (j.status === 'queued') return ['Waiting', 'wait'];
  if (j.status === 'paused') return ['Paused', 'pause'];
  if (j.status === 'done') return ['Done', 'ok'];
  if (j.status === 'failed') return ['Failed', 'bad'];
  if (j.phase === 'storing') return [`Storing ${j.files_done} of ${j.files_total} files`, 'run'];
  if (j.phase === 'hashing') return ['Verifying', 'run'];
  if (j.phase === 'downloading') return ['Downloading', 'run'];
  return ['Preparing', 'run'];
}

function statsOf(j) {
  const of = j.bytes_total ? `${bytes(j.bytes_done)} of ${bytes(j.bytes_total)}` : bytes(j.bytes_done);
  const files = j.files_total ? plural(j.files_total, 'file') : '';
  if (j.status === 'queued') return [j.bytes_total ? `${bytes(j.bytes_total)} to download` : '', files].filter(Boolean).join(' · ');
  if (j.status === 'paused') return `${of} kept on disk`;
  if (j.status === 'done') {
    if (j.kind === 'verify') return `Checked ${plural((j.result && j.result.checked) || j.files_total, 'file')}: no damaged or missing file. Finished ${dateTime(j.finished)}`;
    return `${bytes(j.bytes_total || j.bytes_done)} stored · ${plural((j.result && j.result.files) || j.files_total, 'file')} · finished ${dateTime(j.finished)}`;
  }
  if (j.status === 'failed') return j.kind === 'verify' ? `Checked ${j.files_done} of ${j.files_total} files` : of;
  if (j.phase === 'hashing') return `${j.files_done} of ${j.files_total} files · ${of}`;
  if (j.phase === 'storing') return `${of} downloaded, now storing`;
  return [of, j.speed_bps > 0 ? speed(j.speed_bps) : '', eta(j.eta_s)].filter(Boolean).join(' · ');
}

function jobCard(j, act) {
  const title = h('span', { class: 'job-title' });
  const chip = h('span', { class: 'chip' });
  const note = h('span', { class: 'muted job-note' });
  const fill = h('div', { class: 'fill' });
  const bar = h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-label': 'Progress' }, fill);
  const stats = h('p', { class: 'job-stats' });
  const msg = h('p', { class: 'job-msg', role: 'alert', hidden: true });
  const logBody = h('pre');
  const log = h('details', { class: 'job-log', hidden: true }, h('summary', null, 'Details'), logBody);
  const actions = h('div', { class: 'job-actions' });
  const kind = h('span');
  const el = h('article', { class: 'job' }, h('div', { class: 'job-top' }, title, kind, h('span', { class: 'grow' }), note, chip), bar, stats, msg, log, actions);
  let key = '', busy = false;

  const btn = (label, ic, fn, cls = '') => h('button', { type: 'button', class: 'btn sm ' + cls, onclick: async (e) => {
    if (busy) return;
    const self = e.currentTarget;
    busy = true;
    self.disabled = true;
    try { await fn(); } finally { busy = false; self.disabled = false; }
  } }, ic && icon(ic, 14), label);

  function update(job) {
    j = job;
    const [text, cls] = chipOf(job);
    title.textContent = job.kind === 'verify' ? `Integrity check: ${job.label}` : job.label;
    chip.textContent = text;
    chip.className = 'chip ' + cls;
    kind.replaceChildren(...(job.kind === 'verify' ? [badge('Check', 'b-ref')] : [badge(job.spec.type === 'dataset' ? 'dataset' : 'model', 'b-' + (job.spec.type || 'model')), badge(sourceName(job.spec.source), 'b-ref')]));
    note.textContent = job.status === 'queued' || job.status === 'paused' ? job.note || '' : '';
    const done = job.status === 'done';
    const p = done ? 100 : pct(job.bytes_done, job.bytes_total);
    bar.className = 'progress ' + cls + (job.status === 'running' && !job.bytes_total && job.phase !== 'storing' ? ' indet' : '');
    fill.style.width = p.toFixed(1) + '%';
    bar.setAttribute('aria-valuenow', String(Math.round(p)));
    stats.textContent = statsOf(job);
    const failedMsg = job.status === 'failed' ? job.message || 'The job failed.' : '';
    msg.hidden = !failedMsg;
    msg.textContent = failedMsg;
    const lines = job.status === 'failed' ? [...(job.log || []), ...((job.result && job.result.bad) || [])] : [];
    log.hidden = !lines.length;
    logBody.textContent = lines.join('\n');
    const k = job.status + job.kind;
    if (k === key) return;
    key = k;
    const rm = (label, confirmText) => btn(label, 'x', async () => {
      if (confirmText && !(await confirmDialog({ title: 'Remove this download?', confirm: 'Remove', danger: true, body: h('p', null, confirmText) }))) return;
      await act(j, 'remove');
    }, 'ghost');
    const partial = 'This discards the partial download. You can start it again later, but the part already fetched has to be fetched again.';
    actions.replaceChildren();
    if (job.kind === 'verify') actions.append(job.status === 'running' ? rm('Cancel check') : rm('Clear'));
    else if (job.status === 'done') actions.append(rm('Clear'));
    else if (job.status === 'paused') actions.append(btn('Resume', 'play', () => act(j, 'resume'), 'primary'), rm('Remove', partial));
    else if (job.status === 'failed') actions.append(btn('Retry', 'retry', () => act(j, 'resume'), 'primary'), rm('Remove', partial));
    else actions.append(btn('Pause', 'pause', () => act(j, 'pause')), rm('Remove', partial));
  }
  update(j);
  return { el, update };
}

function syncChildren(parent, wanted) {
  wanted.forEach((node, i) => { if (parent.children[i] !== node) parent.insertBefore(node, parent.children[i] || null); });
  while (parent.children.length > wanted.length) parent.lastChild.remove();
}

export default async function queue(page) {
  setTitle('Queue');
  const head = h('p', { class: 'queue-line muted' });
  const offline = h('p', { class: 'callout bad', role: 'status', hidden: true }, 'The server cannot be reached right now. Trying again…');
  const activeList = h('div', { class: 'jobs' });
  const doneList = h('div', { class: 'jobs' });
  const doneCount = h('span', { class: 'count' });
  const clearAll = h('button', { class: 'btn sm', type: 'button' }, 'Clear all');
  const finished = h('details', { class: 'finished', hidden: true }, h('summary', null, 'Finished', doneCount), h('div', { class: 'finished-bar' }, clearAll), doneList);
  const emptyEl = empty('Nothing is downloading', 'Pick a model or dataset on the Download page and it will show up here with its progress.', h('a', { class: 'btn primary', href: '#/download' }, icon('download'), 'Go to Download'));
  const body = h('div', { class: 'queue-body' }, loading('Loading the queue…'));
  page.append(h('div', { class: 'page-head' }, h('h1', null, 'Queue')), head, offline, body);

  const cards = new Map();
  let alive = true, first = true, current = [];

  async function act(job, action) {
    try { await api(`/jobs/${encodeURIComponent(job.id)}/${action}`, { method: 'POST' }); }
    catch (e) { toast(e.detail || e.message, { kind: 'error' }); }
    await poll();
  }
  clearAll.addEventListener('click', async () => {
    for (const j of current.filter((x) => x.status === 'done')) await api(`/jobs/${encodeURIComponent(j.id)}/remove`, { method: 'POST' }).catch(() => {});
    await poll();
  });

  function render(data) {
    current = data.jobs;
    const w = data.window ? data.window.replace('-', '–') : '';
    head.replaceChildren(w ? `Download window ${w}` : 'Downloads start immediately', ` · ${data.max_concurrent} at a time · `, h('a', { href: '#/settings' }, 'Change in Settings'));
    const seen = new Set();
    for (const j of data.jobs) {
      seen.add(j.id);
      const c = cards.get(j.id);
      if (c) c.update(j); else cards.set(j.id, jobCard(j, act));
    }
    for (const id of [...cards.keys()]) if (!seen.has(id)) cards.delete(id);
    const active = data.jobs.filter((j) => j.status !== 'done'), done = data.jobs.filter((j) => j.status === 'done');
    syncChildren(activeList, active.map((j) => cards.get(j.id).el));
    syncChildren(doneList, done.map((j) => cards.get(j.id).el));
    doneCount.textContent = String(done.length);
    finished.hidden = !done.length;
    const want = [active.length ? activeList : null, finished.hidden ? null : finished, data.jobs.length ? null : emptyEl].filter(Boolean);
    syncChildren(body, want);
  }

  async function poll() {
    try {
      const data = await api('/jobs');
      if (!alive) return;
      offline.hidden = true;
      setJobs(data);
      render(data);
      first = false;
    } catch (e) {
      if (!alive) return;
      if (first) body.replaceChildren(errorBox(e, () => { body.replaceChildren(loading()); poll(); }));
      else offline.hidden = false;
    }
  }
  const stop = every(poll, 2000);
  return () => { alive = false; stop(); };
}
