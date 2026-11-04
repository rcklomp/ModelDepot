import { h, icon, api, bytes, plural, pct, confirmDialog, errorBox, loading, go, refreshStatus, setTitle, toast, ApiError } from '../lib.js';

export default async function storage(page) {
  setTitle('Storage');
  const body = h('div', null, loading('Reading the disk…'));
  page.append(h('div', { class: 'page-head' }, h('h1', null, 'Storage')), body);
  let alive = true;
  const gcOut = h('div', { class: 'gc-out', 'aria-live': 'polite' });

  async function load(initial = true) {
    try {
      const [s, lib] = await Promise.all([api('/status'), api('/library').catch(() => null)]);
      if (alive) draw(s, lib && lib.items);
    } catch (e) {
      if (alive && initial) body.replaceChildren(errorBox(e, load));
    }
  }

  function draw(s, items) {
    const lib = s.stored.bytes, other = Math.max(0, s.total_bytes - s.free_bytes - lib);
    const freeFrac = s.total_bytes ? s.free_bytes / s.total_bytes : 1;
    const level = freeFrac < 0.05 ? 'bad' : freeFrac < 0.1 ? 'warn' : '';
    const seg = (cls, v, label) => h('div', { class: 'dseg ' + cls, style: { width: Math.max(pct(v, s.total_bytes), v > 0 ? 0.4 : 0) + '%' }, title: `${label}: ${bytes(v)}` });
    const key = (cls, label, v) => h('li', null, h('span', { class: 'swatch ' + cls }), h('span', null, label), h('b', null, bytes(v)));
    const sum = items ? items.reduce((a, i) => a + i.size_bytes, 0) : null;

    const disk = h('section', { class: 'card' }, h('h2', null, 'Disk'),
      h('div', { class: 'dbar', role: 'img', 'aria-label': `Disk of ${bytes(s.total_bytes)}: library ${bytes(lib)}, other files ${bytes(other)}, free ${bytes(s.free_bytes)}` },
        seg('lib', lib, 'Library'), seg('other', other, 'Everything else'), seg('free ' + level, s.free_bytes, 'Free')),
      h('ul', { class: 'legend' }, key('lib', 'Library', lib), key('other', 'Other files on this disk', other), key('free ' + level, 'Free', s.free_bytes)),
      level ? h('p', { class: 'callout ' + level, role: 'status' }, icon('warn', 15), level === 'bad' ? 'The disk is almost full. New downloads may fail.' : 'Free space is getting low.') : null);

    const facts = h('section', { class: 'card' }, h('h2', null, 'The library'),
      h('dl', { class: 'facts' },
        h('div', null, h('dt', null, 'Models'), h('dd', null, String(s.models))),
        h('div', null, h('dt', null, 'Datasets'), h('dd', null, String(s.datasets))),
        h('div', null, h('dt', null, 'Files stored'), h('dd', null, String(s.stored.blobs))),
        h('div', { class: 'wide' }, h('dt', null, 'Store id'), h('dd', null, h('code', null, s.store_id || 'not set'))),
        h('div', { class: 'wide' }, h('dt', null, 'Store path'), h('dd', null, h('code', null, s.store_path)))),
      h('p', { class: 'muted' }, sum != null && sum > lib
        ? `The repositories add up to ${bytes(sum)}, but the library only takes ${bytes(lib)} on disk: a file that is in several repositories, or in several versions of one, is stored once. Deleting one repository frees only the files no other repository uses.`
        : sum != null && sum < lib
          ? `The newest version of each repository adds up to ${bytes(sum)}; the library takes ${bytes(lib)} because older versions are kept too. A file that several repositories or versions share is stored only once.`
          : 'A file that is in several repositories, or in several versions of one, is stored once. That is why disk use can be smaller than the sum of the repositories, and why deleting one repository frees only the files nothing else uses.'));

    // ---- integrity
    const verifyBtn = h('button', { class: 'btn', type: 'button', onclick: async () => {
      const ok = await confirmDialog({ title: 'Check the integrity of everything?', confirm: 'Start the check',
        body: [h('p', null, `This reads every file in the library again (${bytes(lib)}) and compares it with the checksum recorded when it was stored.`),
          h('p', { class: 'muted' }, 'On a NAS that can take from several minutes to hours. The library stays usable but a little slower. It runs as a job in the Queue and you can leave the page.')] });
      if (!ok) return;
      try { await api('/ops/verify', { method: 'POST', body: {} }); toast('Integrity check started', { link: ['Open the queue', '#/queue'] }); go('#/queue'); }
      catch (e) { toast(e.detail || e.message, { kind: 'error' }); }
    } }, icon('check', 15), 'Check integrity of everything');

    // ---- free unused space
    const gcBtn = h('button', { class: 'btn', type: 'button', onclick: preview }, icon('trash', 15), 'Look for unused space');
    async function preview() {
      gcBtn.disabled = true;
      gcOut.replaceChildren(loading('Looking for unreferenced files…'));
      try {
        const r = await api('/ops/gc', { method: 'POST', body: { dry_run: true } });
        if (!alive) return;
        if (!r.blobs) { gcOut.replaceChildren(h('p', { class: 'callout ok' }, icon('check', 15), `Nothing to free: ${plural(r.checked, 'stored file')} checked, and every one belongs to a repository.`)); return; }
        gcOut.replaceChildren(h('div', { class: 'callout' }, h('p', null, h('b', null, `${plural(r.blobs, 'unreferenced blob')}, ${bytes(r.bytes)}`), ` can be freed (${plural(r.checked, 'stored file')} checked).`),
          h('button', { class: 'btn danger', type: 'button', onclick: () => run(r) }, icon('trash', 15), `Free ${bytes(r.bytes)}`)));
      } catch (e) {
        gcOut.replaceChildren(h('p', { class: 'callout bad', role: 'alert' }, e instanceof ApiError ? e.detail : e.message));
      } finally { gcBtn.disabled = false; }
    }
    async function run(r) {
      if (!(await confirmDialog({ title: `Free ${bytes(r.bytes)}?`, confirm: 'Delete the unused files', danger: true,
        body: [h('p', null, `${plural(r.blobs, 'file')} that no repository uses will be deleted for good.`)] }))) return;
      try {
        const out = await api('/ops/gc', { method: 'POST', body: { dry_run: false } });
        gcOut.replaceChildren(h('p', { class: 'callout ok' }, icon('check', 15), `Deleted ${plural(out.blobs, 'unreferenced file')}: ${bytes(out.bytes)} freed.`));
        refreshStatus({ quiet: true }).catch(() => {});
        load(false);
      } catch (e) { gcOut.replaceChildren(h('p', { class: 'callout bad', role: 'alert' }, e.detail || e.message)); }
    }

    const tools = h('section', { class: 'card' }, h('h2', null, 'Maintenance'),
      h('div', { class: 'dz-row' }, h('div', null, h('b', null, 'Check integrity of everything'),
        h('p', { class: 'muted' }, 'Reads every file once and compares its checksum. It takes a while on a NAS, because every byte of the library is read.')), verifyBtn),
      h('div', { class: 'dz-row stack' }, h('div', null, h('b', null, 'Free unused space'),
        h('p', { class: 'muted' }, 'Files no repository uses any more (left over after a deleted repository or a failed download) can be removed. You see what would go first, then decide. A download in progress is never touched: cleaning refuses to run while one is being stored, and files stored in the last hour are skipped.')),
        gcBtn), gcOut);

    body.replaceChildren(h('div', { class: 'cards' }, disk, facts, tools));
  }

  load();
  return () => { alive = false; };
}
