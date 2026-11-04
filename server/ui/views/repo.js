import { h, icon, api, qs, badge, bytes, date, dateTime, copyBtn, codeBlock, confirmDialog, infoDialog, empty, errorBox, loading, tabs, toast, go,
  origin, plural, refreshStatus, safeUrl, setTitle, sourceName, state, debounce } from '../lib.js';

const SHOW = 300;

export default async function repo(page, route) {
  const type = route.parts[0], id = route.parts.slice(1).join('/');
  if (!['model', 'dataset'].includes(type) || !id) {
    page.append(empty('That address is not a repository', 'Repository pages look like #/repo/model/org/name.', h('a', { class: 'btn primary', href: '#/library' }, 'Go to the library')));
    return;
  }
  setTitle(id);
  page.append(loading('Loading ' + id + '…'));
  let d;
  try {
    d = await api('/repo?' + qs({ type, id }));
  } catch (e) {
    page.replaceChildren(h('a', { class: 'back', href: '#/library' }, icon('back', 14), 'Library'),
      e.status === 404 ? empty('Not in the library', `${id} has not been downloaded onto this server.`,
        h('a', { class: 'btn primary', href: `#/download?src=hf&type=${type}&id=${encodeURIComponent(id)}` }, icon('download'), 'Download it')) : errorBox(e, () => location.reload()));
    return;
  }
  const status = state.status || (await refreshStatus({ quiet: true }).catch(() => null));
  d.revisions.sort((a, b) => String(b.fetched_at).localeCompare(String(a.fetched_at)));          // the server lists them by commit id, which says nothing about age
  const latest = d.revisions.find((r) => r.commit === d.commit) || d.revisions[0] || { files: [] };

  const srcUrl = safeUrl(d.source_ref || '') || (d.source === 'huggingface' ? `https://huggingface.co/${type === 'dataset' ? 'datasets/' : ''}${id}` : null);
  const meta = (k, v) => h('div', { class: 'meta-i' }, h('dt', null, k), h('dd', null, v));
  const head = h('header', { class: 'repo-head' },
    h('a', { class: 'back', href: '#/library' }, icon('back', 14), 'Library'),
    h('div', { class: 'repo-title' }, h('h1', null, id), badge(type, 'b-' + type)),
    h('dl', { class: 'meta' },
      meta('Size', bytes(d.size_bytes)), meta('Files', String(d.files)),
      meta('Commit', h('span', { class: 'row gap-s' }, h('code', { title: d.commit }, d.commit.slice(0, 8)), copyBtn(d.commit, { label: '', title: 'Copy the full commit id' }))),
      meta('Licence', d.license || 'not stated'), meta('Updated', dateTime(d.updated)),
      meta('Source', srcUrl ? h('a', { href: srcUrl, target: '_blank', rel: 'noopener noreferrer' }, sourceName(d.source), ' ', icon('ext', 12)) : sourceName(d.source))));

  const panel = h('div', { class: 'tab-panel', role: 'tabpanel' });
  let currentRev = latest.commit;

  // ---- Files
  function filesPanel() {
    const wrap = h('div');
    const filter = h('input', { type: 'search', placeholder: 'Search files', 'aria-label': 'Search the files', spellcheck: 'false' });
    const revSel = d.revisions.length > 1 ? h('select', { 'aria-label': 'Revision', onchange: () => { currentRev = revSel.value; draw(); } },
      d.revisions.map((r) => h('option', { value: r.commit, selected: r.commit === currentRev }, `${r.commit.slice(0, 8)} · ${date(r.fetched_at)}${r.commit === d.commit ? ' (current)' : ''}`))) : null;
    const list = h('div', { class: 'files' });
    let showAll = false;
    function draw() {
      const rev = d.revisions.find((r) => r.commit === currentRev) || latest;
      const q = filter.value.trim().toLowerCase();
      const rows = rev.files.filter((f) => !q || f.path.toLowerCase().includes(q)).sort((a, b) => a.path.localeCompare(b.path));
      list.replaceChildren();
      if (!rows.length) { list.append(h('p', { class: 'muted pad' }, q ? `No file name contains “${filter.value.trim()}”.` : 'This revision has no files.')); return; }
      list.append(h('div', { class: 'frow head', 'aria-hidden': 'true' }, h('span', null, `Path (${plural(rows.length, 'file')})`), h('span', { class: 'r' }, 'Size'), h('span', null, 'SHA-256')));
      for (const f of showAll ? rows : rows.slice(0, SHOW)) {
        const cut = f.path.lastIndexOf('/');
        list.append(h('div', { class: 'frow' },
          h('span', { class: 'path' }, cut >= 0 ? h('span', { class: 'dir' }, f.path.slice(0, cut + 1)) : null, f.path.slice(cut + 1)),
          h('span', { class: 'r', 'data-l': 'Size' }, bytes(f.size)),
          h('span', { class: 'sha' }, h('code', { title: f.sha256 }, f.sha256.slice(0, 12)), copyBtn(f.sha256, { label: '', title: 'Copy the full SHA-256' }))));
      }
      if (!showAll && rows.length > SHOW) list.append(h('button', { class: 'btn', type: 'button', onclick: () => { showAll = true; draw(); } }, `Show all ${rows.length} files`));
    }
    filter.addEventListener('input', debounce(draw, 120));
    draw();
    wrap.append(h('div', { class: 'toolbar' }, filter, revSel), list);
    return wrap;
  }

  // ---- README
  async function readmePanel() {
    if (!d.readme) return empty('No README', 'This repository has no README.md. The files are still listed under Files.');
    const { renderMarkdown } = await import('../markdown.js');
    return renderMarkdown(d.readme);
  }

  // ---- Revisions
  function revisionsPanel() {
    const refsOf = (c) => Object.entries(d.refs || {}).filter(([, v]) => v === c).map(([k]) => badge(k, 'b-ref'));
    return h('div', { class: 'rev-list' }, d.revisions.map((r) => h('div', { class: 'rev' },
      h('code', { title: r.commit }, r.commit.slice(0, 12)), copyBtn(r.commit, { label: '', title: 'Copy the full commit id' }),
      r.commit === d.commit ? badge('current', 'b-ok') : null, refsOf(r.commit),
      h('span', { class: 'grow' }),
      h('span', { class: 'muted' }, `${dateTime(r.fetched_at)} · ${plural(r.files.length, 'file')} · ${bytes(r.files.reduce((a, f) => a + f.size, 0))}`),
      h('button', { class: 'btn sm', type: 'button', onclick: () => { currentRev = r.commit; tabBar.querySelector('[data-key=files]').click(); } }, 'Show files'))));
  }

  // ---- Use
  function usePanel() {
    const o = origin(), pub = id, ds = type === 'dataset';
    const env = [`export HF_ENDPOINT=${o}`, 'export HF_HUB_DISABLE_XET=1'];
    if (status && status.read_token_required) env.push('export HF_TOKEN=<read token>');
    const gguf = latest.files.map((f) => f.path).filter((p) => /\.gguf$/i.test(p) && !/(^|\/)mmproj/i.test(p)).sort()[0];
    const enc = (p) => p.split('/').map(encodeURIComponent).join('/');
    return h('div', { class: 'use' },
      h('p', { class: 'muted' }, 'Run these on any machine on your network. Nothing is fetched from the internet: the files come from this server.'),
      codeBlock(env.join('\n'), 'Point the tools at this server (once per terminal)'),
      status && status.read_token_required ? h('p', { class: 'muted' }, 'This server asks for a read token: replace ', h('code', null, '<read token>'), ' with it.') : null,
      codeBlock(`hf download ${pub}${ds ? ' --repo-type dataset' : ''}`, 'Download with the command line'),
      codeBlock(ds ? `from datasets import load_dataset\n\nds = load_dataset("${pub}")` : `from transformers import AutoModel\n\nmodel = AutoModel.from_pretrained("${pub}")`, 'Or use it from Python'),
      gguf ? codeBlock(`llama-server --model-url ${o}/${enc(pub)}/resolve/main/${enc(gguf)}`, 'llama.cpp (first GGUF file in this repository)') : null);
  }

  let tabBar, shown = ['files', 'readme', 'revisions', 'use'].includes(route.query.tab) ? route.query.tab : 'files';
  async function select(key) {
    shown = key;
    history.replaceState(null, '', `#/repo/${type}/${id.split('/').map(encodeURIComponent).join('/')}${key === 'files' ? '' : '?tab=' + key}`);
    panel.replaceChildren(loading());
    const content = key === 'files' ? filesPanel() : key === 'readme' ? await readmePanel() : key === 'revisions' ? revisionsPanel() : usePanel();
    if (shown === key) panel.replaceChildren(content);
  }
  tabBar = tabs([['files', 'Files', latest.files.length], ['readme', 'README'], ['revisions', 'Revisions', d.revisions.length], ['use', 'Use']], shown, select, 'Repository sections');

  // ---- danger zone
  const verifyBtn = h('button', { class: 'btn', type: 'button', onclick: async () => {
    try { await api('/ops/verify', { method: 'POST', body: { type, id } }); toast('Integrity check started', { link: ['Open the queue', '#/queue'] }); go('#/queue'); }
    catch (e) { toast(e.detail || e.message, { kind: 'error' }); }
  } }, icon('check', 15), 'Check integrity');
  const delBtn = h('button', { class: 'btn danger', type: 'button', onclick: deleteRepo }, icon('trash', 15), 'Delete from library…');

  async function deleteRepo() {
    let prev;
    try { prev = await api('/repo/delete-preview?' + qs({ type, id })); } catch (e) { toast(e.detail || e.message, { kind: 'error' }); return; }
    const ok = await confirmDialog({ title: `Delete ${id}?`, confirm: 'Delete', danger: true, typeToConfirm: prev.frees_bytes > 1e9 ? id : null,
      body: [h('p', null, `Deleting frees ${bytes(prev.frees_bytes)}. ${bytes(prev.shared_bytes)} are shared with other repositories and stay.`),
        h('p', { class: 'muted' }, 'Machines on your network will no longer find it here. You can download it again from Hugging Face later.')] });
    if (!ok) return;
    try {
      const r = await api('/repo/delete', { method: 'POST', body: { type, id } });
      toast(`Deleted ${id}. Freed ${bytes(r.freed_bytes)}.`);
      refreshStatus({ quiet: true }).catch(() => {});
      go('#/library');
    } catch (e) {
      if (e.status === 409) infoDialog('It cannot be deleted right now', h('p', null, e.detail));
      else toast(e.detail || e.message, { kind: 'error' });
    }
  }

  const danger = h('section', { class: 'danger-zone' }, h('h2', null, 'Danger zone'),
    h('div', { class: 'dz-row' }, h('div', null, h('b', null, 'Check integrity'), h('p', { class: 'muted' }, 'Reads every file of this repository again and compares its checksum. Runs as a job in the queue.')), verifyBtn),
    h('div', { class: 'dz-row' }, h('div', null, h('b', null, 'Delete from library'), h('p', { class: 'muted' }, 'Removes this repository from the server. Files that other repositories also use are kept.')), delBtn));

  page.replaceChildren(head, tabBar, panel, danger);
  select(shown);
}
