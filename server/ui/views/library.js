import { h, icon, api, badge, bytes, date, errorBox, empty, loading, on, plural, repoHref, sourceName, tabs, toast, debounce } from '../lib.js';

const SORTS = [['name', 'Name'], ['size', 'Size (largest first)'], ['updated', 'Recently updated']];

export default async function library(page, route) {
  const st = { type: ['model', 'dataset'].includes(route.query.type) ? route.query.type : 'all', sort: 'name', q: route.query.q || '', items: [] };
  const body = h('div');
  page.append(h('div', { class: 'page-head' }, h('h1', null, 'Library')), body);
  let alive = true;

  async function load() {
    body.replaceChildren(loading('Loading the library…'));
    try {
      const [lib, wanted] = await Promise.all([api('/library'), api('/wanted').catch(() => ({ items: [] }))]);
      if (!alive) return;
      st.items = lib.items;
      draw(wanted.items);
    } catch (e) {
      if (alive) body.replaceChildren(errorBox(e, load));
    }
  }

  function draw(wantedItems) {
    const list = h('div', { class: 'lib-list' });
    const counts = { all: st.items.length, model: st.items.filter((i) => i.type === 'model').length, dataset: st.items.filter((i) => i.type === 'dataset').length };
    if (!st.items.length) {
      body.replaceChildren(...[wantedStrip(wantedItems), empty('Your library is empty',
        'Nothing has been downloaded onto this server yet. Download a model or dataset from Hugging Face once, and every machine on your network can then fetch it from here.',
        h('a', { class: 'btn primary', href: '#/download' }, icon('download'), 'Go to Download'))].filter(Boolean));
      return;
    }
    const filter = h('input', { type: 'search', placeholder: 'Filter by name, format or licence', 'aria-label': 'Filter the library', value: st.q, spellcheck: 'false',
      oninput: debounce(() => { st.q = filter.value; const q = st.q.trim(); history.replaceState(null, '', '#/library' + (q ? '?q=' + encodeURIComponent(q) : '')); renderList(); }, 120) });
    const sort = h('select', { 'aria-label': 'Sort by', onchange: () => { st.sort = sort.value; renderList(); } }, SORTS.map(([v, t]) => h('option', { value: v }, t)));
    const tabBar = tabs([['all', 'All', counts.all], ['model', 'Models', counts.model], ['dataset', 'Datasets', counts.dataset]], st.type, (k) => { st.type = k; renderList(); }, 'Show');

    function renderList() {
      const q = st.q.trim().toLowerCase();
      let rows = st.items.filter((i) => (st.type === 'all' || i.type === st.type)
        && (!q || [i.id, i.license, sourceName(i.source), ...i.formats].some((s) => s && String(s).toLowerCase().includes(q))));
      const key = { name: (a, b) => a.id.toLowerCase().localeCompare(b.id.toLowerCase()), size: (a, b) => b.size_bytes - a.size_bytes,
        updated: (a, b) => String(b.updated || '').localeCompare(String(a.updated || '')) };
      rows = rows.sort(key[st.sort]);
      list.replaceChildren();
      if (!rows.length) {
        list.append(empty('Nothing matches', q ? `No ${st.type === 'all' ? 'repository' : st.type} matches “${st.q.trim()}”.` : `There are no ${st.type}s in the library yet.`,
          q && h('button', { class: 'btn', onclick: () => { filter.value = ''; st.q = ''; history.replaceState(null, '', '#/library'); renderList(); filter.focus(); } }, 'Clear the filter'),
          h('a', { class: 'btn primary', href: '#/download' }, 'Download something')));
        return;
      }
      list.append(h('div', { class: 'lrow head', 'aria-hidden': 'true' }, h('span', null, 'Repository'), h('span', null, 'Formats'), h('span', { class: 'r' }, 'Size'),
        h('span', { class: 'r' }, 'Files'), h('span', null, 'Updated'), h('span', null, 'Source')),
      ...rows.map((i) => h('a', { class: 'lrow item', href: repoHref(i.type, i.id) },
        h('span', { class: 'rid' }, h('span', { class: 'name' }, i.id), badge(i.type, 'b-' + i.type)),
        h('span', { class: 'fmts' }, i.formats.map((f) => badge(f))),
        h('span', { class: 'r', 'data-l': 'Size' }, bytes(i.size_bytes)), h('span', { class: 'r', 'data-l': 'Files' }, plural(i.files, 'file')),
        h('span', { 'data-l': 'Updated' }, date(i.updated)), h('span', { class: 'muted' }, sourceName(i.source)))));
    }
    body.replaceChildren(...[wantedStrip(wantedItems), h('div', { class: 'toolbar' }, tabBar, h('span', { class: 'grow' }), filter, sort), list].filter(Boolean));
    renderList();
  }

  function wantedStrip(items) {
    if (!items || !items.length) return null;
    const box = h('section', { class: 'wanted', 'aria-label': 'Requested by machines on your network' });
    let all = false;
    const paint = () => {
      box.replaceChildren();
      const shown = all ? items : items.slice(0, 3);
      for (const w of shown) {
        const row = h('div', { class: 'wanted-row' }, icon('download', 15),
          h('span', null, 'Machines on your network asked for ', h('b', null, w.id), ` (${plural(w.count, 'time')}) `, h('span', { class: 'muted' }, w.type === 'dataset' ? 'dataset' : '')),
          h('span', { class: 'grow' }),
          h('a', { class: 'btn sm primary', href: `#/download?src=hf&type=${w.type}&id=${encodeURIComponent(w.id)}` }, 'Download'),
          h('button', { class: 'btn sm', type: 'button', onclick: async () => {
            try { await api('/wanted/dismiss', { method: 'POST', body: { type: w.type, id: w.id } }); items.splice(items.indexOf(w), 1); if (!items.length) box.remove(); else paint(); }
            catch (e) { toast(e.detail || e.message, { kind: 'error' }); }
          } }, 'Dismiss'));
        box.append(row);
      }
      if (items.length > 3) box.append(h('button', { class: 'linklike', type: 'button', onclick: () => { all = !all; paint(); } }, all ? 'Show fewer' : `Show all ${items.length}`));
    };
    paint();
    return box;
  }

  const off = on('library-changed', () => { if (alive) load(); });
  load();
  return () => { alive = false; off(); };
}
