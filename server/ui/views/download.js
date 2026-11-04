import { h, icon, api, qs, badge, bytes, num, seg, empty, loading, refreshStatus, setTitle, state, ApiError } from '../lib.js';
import { buildPicker, versionField } from './picker.js';

const HF_SORTS = [['downloads', 'Most downloaded'], ['likes', 'Most liked'], ['last_modified', 'Recently updated']];
const CIV_SORTS = [['Highest Rated', 'Highest rated'], ['Most Downloaded', 'Most downloaded'], ['Newest', 'Newest']];
const CIV_TYPES = [['', 'Any type'], ['Checkpoint', 'Checkpoint'], ['LORA', 'LoRA'], ['VAE', 'VAE'], ['TextualInversion', 'Textual inversion'], ['Controlnet', 'ControlNet']];
const FORMATS = [['', 'Any format'], ['gguf', 'GGUF'], ['safetensors', 'safetensors'], ['mlx', 'MLX']];

// Kept while you move between pages, so coming back from the Queue does not lose the search.
const memo = { src: 'hf', type: 'model', q: '', sort: 'downloads', fmt: '', cq: '', csort: 'Highest Rated', ctype: '', nsfw: false, results: null, err: null, sel: null, version: 'main' };

/** "https://huggingface.co/datasets/org/name/tree/dev", "datasets/org/name" or "org/name" -> {id, type, rev} */
export function parseRepo(text) {
  let t = String(text).trim();
  if (!t) return null;
  const url = /^(?:https?:\/\/)?(?:www\.)?(?:huggingface\.co|hf\.co)\/(.+)$/i.exec(t);
  if (url) t = url[1];
  const parts = t.split(/[?#]/)[0].split('/').filter(Boolean);
  let type = 'model';
  if (parts[0] === 'datasets') { type = 'dataset'; parts.shift(); } else if (parts[0] === 'models') parts.shift();
  else if (parts[0] === 'spaces') return { error: 'Spaces are not supported by this server.' };
  const marks = ['tree', 'blob', 'resolve', 'commit'];
  const cut = parts.findIndex((p, i) => i >= 1 && marks.includes(p));
  const idParts = cut >= 0 ? parts.slice(0, cut) : parts.slice(0, 2);
  const id = idParts.join('/');
  if (!/^[\w.-]+(\/[\w.-]+)?$/.test(id)) return { error: 'That does not look like a repository id. Use org/name or a huggingface.co address.' };
  const rev = url && cut >= 0 && marks.slice(0, 3).includes(parts[cut]) ? parts[cut + 1] : null;
  return { id, type, rev: rev || null };
}

/** The server's own sentence; for upstream trouble (502) a reassurance, or the way to the token setting. */
function upstreamError(e, retry) {
  const soft = e instanceof ApiError && (e.status === 502 || e.status === 0);
  return h('div', { class: 'state error', role: 'alert' }, h('p', null, icon('warn'), ' ', e.detail || e.message),
    soft ? h('p', { class: 'muted' }, /Settings/.test(e.detail) ? h('a', { href: '#/settings' }, 'Open Settings') : 'The server may be offline or the site may be down. Your library and everything else still work.') : null,
    h('button', { class: 'btn', type: 'button', onclick: retry }, icon('retry'), 'Try again'));
}

export default async function download(page, route) {
  setTitle('Download');
  const q = route.query;
  if (q.id) {
    const p = q.src === 'civitai' ? { id: q.id, type: 'model' } : parseRepo(q.id);
    if (p && !p.error) {
      Object.assign(memo, { src: q.src === 'civitai' ? 'civitai' : 'hf', type: q.type === 'dataset' || p.type === 'dataset' ? 'dataset' : 'model', version: p.rev || 'main' });
      memo.sel = { source: memo.src, type: memo.type, id: p.id };
    }
  }
  let alive = true, searchTicket = 0, pickTicket = 0, settings = null;
  const controls = h('div', { class: 'dl-controls' });
  const banner = h('div');
  const list = h('section', { class: 'dl-list', 'aria-label': 'Search results' });
  const pick = h('section', { class: 'dl-pick', 'aria-label': 'What to download' });
  const grid = h('div', { class: 'dl-grid' }, list, pick);
  page.append(h('div', { class: 'page-head' }, h('h1', null, 'Download')), controls, banner, grid);

  api('/settings').then((s) => { settings = s; if (alive) drawBanner(); }).catch(() => {});
  refreshStatus({ quiet: true }).catch(() => {});

  // ---------------------------------------------------------------- controls
  function drawControls() {
    const civ = memo.src === 'civitai';
    const search = h('input', { type: 'search', id: 'dl-q', placeholder: civ ? 'Search Civitai' : memo.type === 'dataset' ? 'Search datasets on Hugging Face' : 'Search models on Hugging Face',
      'aria-label': 'Search', value: civ ? memo.cq : memo.q, spellcheck: 'false', autocomplete: 'off' });
    const sel = (opts, val, label) => { const s = h('select', { 'aria-label': label }, opts.map(([v, t]) => h('option', { value: v, selected: v === val }, t))); return s; };
    const sort = sel(civ ? CIV_SORTS : HF_SORTS, civ ? memo.csort : memo.sort, 'Sort by');
    const fmt = civ ? sel(CIV_TYPES, memo.ctype, 'Kind of model') : memo.type === 'model' ? sel(FORMATS, memo.fmt, 'File format') : null;
    const nsfw = civ ? h('label', { class: 'check inline' }, h('input', { type: 'checkbox', checked: memo.nsfw, onchange: (e) => { memo.nsfw = e.target.checked; } }), 'Show NSFW') : null;
    const form = h('form', { class: 'dl-form', onsubmit: (e) => {
      e.preventDefault();
      if (civ) Object.assign(memo, { cq: search.value.trim(), csort: sort.value, ctype: fmt.value }); else Object.assign(memo, { q: search.value.trim(), sort: sort.value, fmt: fmt ? fmt.value : '' });
      runSearch();
    } }, search, h('button', { class: 'btn primary', type: 'submit' }, icon('search', 15), 'Search'), sort, fmt, nsfw);
    const paste = h('input', { type: 'text', id: 'dl-paste', placeholder: 'org/name  or  https://huggingface.co/org/name', spellcheck: 'false', autocomplete: 'off' });
    const pasteErr = h('p', { class: 'err', role: 'alert', hidden: true });
    const pasteForm = civ ? null : h('form', { class: 'dl-form paste', onsubmit: (e) => {
      e.preventDefault();
      const p = parseRepo(paste.value);
      if (!p) return;
      if (p.error) { pasteErr.textContent = p.error; pasteErr.hidden = false; return; }
      pasteErr.hidden = true;
      if (memo.type !== p.type) { memo.type = p.type; memo.results = null; runSearch(); }
      memo.version = p.rev || 'main';
      memo.sel = { source: 'hf', type: p.type, id: p.id };
      drawControls(); drawList(); loadPicker();
    } }, h('label', { for: 'dl-paste' }, 'or paste a repository id / Hugging Face URL'), paste, h('button', { class: 'btn', type: 'submit' }, 'Open'), pasteErr);
    controls.replaceChildren(
      h('div', { class: 'row gap wrap' },
        seg([['hf', 'Hugging Face'], ['civitai', 'Civitai']], memo.src, (v) => { memo.src = v; memo.sel = null; memo.results = null; memo.err = null; drawControls(); drawList(); drawPick(); runSearch(); controls.querySelector(`[data-v="${v}"]`).focus(); }, 'Source'),
        civ ? null : seg([['model', 'Models'], ['dataset', 'Datasets']], memo.type, (v) => { memo.type = v; memo.sel = null; memo.results = null; drawControls(); drawList(); drawPick(); runSearch(); controls.querySelector(`[data-v="${v}"]`).focus(); }, 'What to look for')),
      ...[form, pasteForm].filter(Boolean));
    drawBanner();
  }
  function drawBanner() {
    banner.replaceChildren();
    if (memo.src === 'civitai' && settings && !settings.civitai_token_set) banner.append(h('p', { class: 'callout warn' }, icon('lock', 15), h('span', null, 'Civitai needs an API key (Settings) before it lets you download. You can still search. ', h('a', { href: '#/settings' }, 'Add the key'), '.')));
  }

  // ---------------------------------------------------------------- results
  async function runSearch() {
    const my = ++searchTicket;
    memo.err = null;
    list.replaceChildren(loading('Searching…'));
    const civ = memo.src === 'civitai';
    try {
      const r = await api('/search?' + qs(civ ? { source: 'civitai', q: memo.cq, sort: memo.csort, filter: memo.ctype, nsfw: memo.nsfw, limit: 30 }
        : { source: 'hf', type: memo.type, q: memo.q, sort: memo.sort, filter: memo.type === 'model' ? memo.fmt : '', limit: 30 }));
      if (my !== searchTicket) return;
      memo.results = r.results;
    } catch (e) {
      if (my !== searchTicket) return;
      memo.results = null;
      memo.err = e;
    }
    if (alive) drawList();
  }

  const isSel = (r) => memo.sel && memo.sel.id === r.id && memo.sel.source === r.source;
  function drawList() {
    const civ = memo.src === 'civitai';
    list.replaceChildren();
    if (memo.err) { list.append(upstreamError(memo.err, runSearch)); return; }
    if (!memo.results) { list.append(loading('Searching…')); return; }
    if (!memo.results.length) { list.append(empty('Nothing found', 'Try fewer words, another format, or paste the repository address above.')); return; }
    list.append(h('ul', { class: 'results' }, memo.results.map((r) => h('li', null, h('button', { type: 'button', class: 'res', 'aria-current': isSel(r) ? 'true' : null, onclick: () => {
      memo.sel = { source: r.source, type: r.type, id: r.id };
      memo.version = 'main';
      drawList(); loadPicker();
    } },
    civ ? [h('span', { class: 'res-id' }, r.name || r.id), h('span', { class: 'res-meta' }, badge(r.civitai_type || 'model', 'b-civitai'), [r.version, r.base_model, r.size ? bytes(r.size) : ''].filter(Boolean).join(' · '),
      r.nsfw ? badge('NSFW', 'b-warn') : null), h('span', { class: 'res-meta' }, `by ${r.author || 'unknown'} · ↓ ${num(r.downloads)}`)]
      : [h('span', { class: 'res-id' }, r.id, r.gated ? h('span', { class: 'lock', title: 'Gated: needs an approved Hugging Face account' }, icon('lock', 13)) : null),
        h('span', { class: 'res-meta' }, h('span', null, `↓ ${num(r.downloads)}`), h('span', null, `♥ ${num(r.likes)}`), r.task ? h('span', { class: 'muted' }, r.task) : null, r.formats.map((f) => badge(f))),
        r.in_library ? h('span', { class: 'res-meta' }, badge('Already in library', 'b-ok')) : null])))));
  }

  // ---------------------------------------------------------------- picker
  function drawPick() {
    grid.classList.toggle('has-picker', !!memo.sel);
    if (!memo.sel) pick.replaceChildren(empty('Pick something on the left', 'The files of the repository you choose appear here, grouped so you can take just one quantization or just the weights.'));
  }

  async function loadPicker() {
    const my = ++pickTicket;
    const s = memo.sel;
    if (!s) return drawPick();
    grid.classList.add('has-picker');
    pick.replaceChildren(backBtn(), loading('Reading the file list…'));
    try {
      const [d] = await Promise.all([api('/upstream/files?' + qs({ source: s.source, type: s.type, id: s.id, revision: s.source === 'hf' ? memo.version : '' })),
        refreshStatus({ quiet: true }).catch(() => {})]);
      if (my !== pickTicket || !alive) return;
      const p = buildPicker(d, { version: memo.version, hfTokenSet: !!(state.status && state.status.hf_token_set),
        civitaiTokenSet: !(settings && !settings.civitai_token_set), onVersion: (v) => { memo.version = v; loadPicker(); } });
      pick.replaceChildren(backBtn(), p.el);
    } catch (e) {
      if (my !== pickTicket || !alive) return;
      pick.replaceChildren(...[backBtn(), s.source === 'hf' ? versionField(memo.version, (v) => { memo.version = v; loadPicker(); }) : null, upstreamError(e, loadPicker)].filter(Boolean));
    }
  }
  const backBtn = () => h('button', { class: 'btn sm back-list', type: 'button', onclick: () => { memo.sel = null; grid.classList.remove('has-picker'); drawList(); drawPick(); } }, icon('back', 14), 'Results');

  drawControls();
  drawList();
  drawPick();
  if (memo.sel) loadPicker();
  if (!memo.results && !memo.err) runSearch();
  return () => { alive = false; };
}
