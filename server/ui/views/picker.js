// The right-hand panel of the Download page: what to take from one upstream repository, and the Download button.
import { h, icon, api, badge, bytes, plural, toast, state, setJobs, debounce } from '../lib.js';

const SHOW_FILES = 400;

/** The branch/tag/commit box: changing it reloads the listing. */
export function versionField(value, onVersion) {
  const input = h('input', { type: 'text', id: 'pk-ver', value, spellcheck: 'false', autocomplete: 'off',
    onchange: () => onVersion(input.value.trim() || 'main'), onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); } } });
  return h('div', { class: 'field inline' }, h('label', { for: 'pk-ver' }, 'Version'), input, h('span', { class: 'muted' }, 'branch, tag or commit; main by default'));
}

/**
 * d: the answer of GET /upstream/files. ctx: { version, onVersion(v), hfTokenSet, civitaiTokenSet }.
 * The selection is either "picked" (quick choice + option cards + the extras box) or "custom" (the user ticked single files).
 */
export function buildPicker(d, ctx) {
  const civ = d.source === 'civitai';
  const sizeOf = new Map(d.files.map((f) => [f.path, f.size]));
  const groups = d.groups || [];
  const optById = new Map(groups.flatMap((g) => g.options.map((o) => [o.id, o])));
  const quickOpts = (d.quick || []).filter((q) => q.files.length || q.id === 'all');
  const extraFiles = (d.extras && d.extras.files) || [];
  const P = { quick: null, opts: new Set(), extras: !civ && extraFiles.length > 0, custom: null };

  const exclusive = groups.find((g) => g.exclusive);
  if (exclusive) {
    const ok = exclusive.options.filter((o) => o.complete !== false);
    const pick = ok.find((o) => /^Q4_K_M$/i.test(o.label)) || ok.find((o) => o.id === 'ext:.safetensors') || ok[0];
    if (pick) P.opts.add(pick.id);
  } else P.quick = 'all';

  const selection = () => {
    if (P.custom) return P.custom;
    const s = new Set();
    if (P.quick) for (const p of quickOpts.find((q) => q.id === P.quick).files) s.add(p);
    else for (const id of P.opts) for (const p of optById.get(id).files) s.add(p);
    if (P.extras) for (const p of extraFiles) s.add(p);
    return s;
  };

  // Civitai can only filter by file kind (type + format), not by name: work out what can be asked for.
  const kindOf = new Map();
  if (civ) for (const o of groups.flatMap((g) => g.options)) for (const p of o.files) kindOf.set(p, [o.file_type || '', o.file_format || '']);
  function civitaiFilter(sel) {
    if (sel.size === d.files.length) return { ok: true };
    const kinds = new Set([...sel].map((p) => (kindOf.get(p) || []).join('|')));
    if (kinds.size === 1) {
      const k = [...kinds][0];
      const same = d.files.filter((f) => (kindOf.get(f.path) || []).join('|') === k).map((f) => f.path);
      if (same.length === sel.size && same.every((p) => sel.has(p))) { const [t, f] = k.split('|'); return { ok: true, file_type: t || null, file_format: f || null }; }
    }
    return { ok: false };
  }

  // ---- header
  const needToken = civ ? !ctx.civitaiTokenSet : d.gated && !ctx.hfTokenSet;
  const head = h('div', { class: 'pk-head' },
    h('div', { class: 'row gap wrap' }, h('h2', { class: 'pk-id' }, civ ? d.name || 'Civitai version ' + d.id : d.id),
      badge(civ ? 'Civitai' : d.type, civ ? 'b-civitai' : 'b-' + d.type), d.license ? badge(d.license, 'b-lic') : null,
      d.gated ? h('span', { class: 'badge b-warn' }, icon('lock', 12), 'Gated') : null,
      d.in_library ? h('a', { class: 'badge b-ok', href: `#/repo/${d.type}/${d.id}` }, 'Already in library') : null),
    h('p', { class: 'muted' }, `${plural(d.file_count, 'file')} · ${bytes(d.total_size)} in the whole repository`, d.commit && !civ ? ` · commit ${d.commit.slice(0, 8)}` : ''),
    civ ? null : versionField(ctx.version, ctx.onVersion),
    needToken ? h('div', { class: 'callout warn', role: 'note' }, icon('lock', 15), h('span', null,
      civ ? 'Civitai needs an API key before it lets you download. ' : 'This repository is gated: Hugging Face only lets an approved account download it. ',
      h('a', { href: '#/settings' }, civ ? 'Add a Civitai key in Settings' : 'Add a Hugging Face token in Settings'), '.')) : null);

  // ---- quick choices
  const quickBtns = quickOpts.map((q) => {
    const b = h('button', { type: 'button', class: 'btn quick', onclick: () => { P.custom = null; P.opts = new Set(); P.quick = P.quick === q.id ? null : q.id; refresh(); } },
      q.label, h('span', { class: 'muted' }, bytes(q.size)));
    return [b, q.id];
  });
  const cfgWarn = h('p', { class: 'callout warn', hidden: true }, icon('warn', 15), (quickOpts.find((q) => q.id === 'config_only') || {}).note || '');
  const quickRow = quickOpts.length > 1 || groups.length ? h('div', null, h('div', { class: 'lbl' }, 'Quick choices'), h('div', { class: 'row gap wrap' }, quickBtns.map((x) => x[0])), cfgWarn) : null;

  // ---- groups of options
  const cards = [];
  const groupEls = groups.map((g) => {
    const wrap = h('fieldset', { class: 'grp' }, h('legend', null, g.title, h('span', { class: 'muted' }, g.exclusive ? ' · choose one' : ' · choose any')));
    const grid = h('div', { class: 'opts' });
    for (const o of g.options) {
      const dis = o.complete === false;
      const input = h('input', { type: g.exclusive ? 'radio' : 'checkbox', name: 'g-' + g.key, disabled: dis, onchange: () => {
        P.custom = null; P.quick = null;
        if (g.exclusive) { for (const x of g.options) P.opts.delete(x.id); P.opts.add(o.id); } else if (input.checked) P.opts.add(o.id); else P.opts.delete(o.id);
        refresh();
      } });
      const card = h('label', { class: 'opt' + (dis ? ' disabled' : '') }, input,
        h('span', { class: 'opt-main' }, h('b', null, o.label), h('span', { class: 'opt-note' + (dis ? ' warn' : '') }, o.note)),
        h('span', { class: 'opt-size' }, bytes(o.size)));
      cards.push({ input, opt: o, card });
      grid.append(card);
    }
    wrap.append(grid);
    return wrap;
  });

  // ---- extras
  const extrasBox = h('input', { type: 'checkbox', onchange: () => { P.extras = extrasBox.checked; if (P.custom) { for (const p of extraFiles) extrasBox.checked ? P.custom.add(p) : P.custom.delete(p); } refresh(); } });
  const extrasEl = !civ && extraFiles.length ? h('label', { class: 'check' }, extrasBox,
    h('span', null, 'Include README, config and tokenizer files ', h('span', { class: 'muted' }, `(${plural(extraFiles.length, 'file')}, ${bytes(d.extras.size)})`))) : null;

  // ---- choose files yourself
  const fileBoxes = [];
  const fileList = h('div', { class: 'filepick' });
  const fileQ = h('input', { type: 'search', placeholder: 'Search files', 'aria-label': 'Search the files of this repository', spellcheck: 'false', oninput: debounce(drawFiles, 120) });
  const visible = () => { const q = fileQ.value.trim().toLowerCase(); return d.files.filter((f) => !q || f.path.toLowerCase().includes(q)); };
  function drawFiles() {
    const rows = visible();
    fileBoxes.length = 0;
    fileList.replaceChildren();
    for (const f of rows.slice(0, SHOW_FILES)) {
      const input = h('input', { type: 'checkbox', onchange: () => { if (!P.custom) P.custom = new Set(selection()); input.checked ? P.custom.add(f.path) : P.custom.delete(f.path); refresh(); } });
      input.checked = selection().has(f.path);
      fileBoxes.push({ input, path: f.path });
      fileList.append(h('label', { class: 'frow pick' }, input, h('span', { class: 'path' }, f.path), h('span', { class: 'r' }, bytes(f.size))));
    }
    if (rows.length > SHOW_FILES) fileList.append(h('p', { class: 'muted pad' }, `Showing ${SHOW_FILES} of ${rows.length} files: search to narrow the list.`));
    if (!rows.length) fileList.append(h('p', { class: 'muted pad' }, 'No file matches.'));
  }
  const customNote = h('p', { class: 'callout', hidden: true }, 'You are choosing single files, so the cards above are not used. ',
    h('button', { type: 'button', class: 'linklike', onclick: () => { P.custom = null; if (!P.quick && !P.opts.size) P.quick = exclusive ? null : 'all'; refresh(); } }, 'Go back to the suggestions'));
  const chooser = h('details', { class: 'chooser', ontoggle: () => { if (chooser.open && !fileList.childElementCount) drawFiles(); } },
    h('summary', null, 'Choose files yourself'),
    h('div', { class: 'toolbar' }, fileQ,
      h('button', { type: 'button', class: 'btn sm', onclick: () => { if (!P.custom) P.custom = new Set(selection()); for (const f of visible()) P.custom.add(f.path); refresh(); } }, 'Select visible'),
      h('button', { type: 'button', class: 'btn sm', onclick: () => { P.custom = new Set(); refresh(); } }, 'Clear')),
    customNote, fileList);

  // ---- summary and the button
  const summary = h('span', { class: 'sum' });
  const spaceMsg = h('p', { class: 'callout bad', role: 'alert', hidden: true });
  const hint = h('p', { class: 'muted hint' });
  const errMsg = h('p', { class: 'callout bad', role: 'alert', hidden: true });
  const btn = h('button', { type: 'button', class: 'btn primary big', onclick: submit }, icon('download', 18), h('span'));

  function refresh() {
    const sel = selection();
    let total = 0;
    for (const p of sel) total += sizeOf.get(p) || 0;
    for (const [b, key] of quickBtns) b.setAttribute('aria-pressed', String(!P.custom && P.quick === key));
    for (const c of cards) { c.input.checked = !P.custom && !P.quick && P.opts.has(c.opt.id); c.card.classList.toggle('on', c.input.checked); }
    if (extrasBox) extrasBox.checked = P.custom ? extraFiles.every((p) => P.custom.has(p)) && extraFiles.length > 0 : P.extras;
    for (const f of fileBoxes) f.input.checked = sel.has(f.path);
    customNote.hidden = !P.custom;
    cfgWarn.hidden = !(P.quick === 'config_only' && !P.custom);
    for (const g of groupEls) g.classList.toggle('muted-grp', !!(P.custom || P.quick));
    summary.textContent = `${plural(sel.size, 'file')} selected · ${bytes(total)}`;
    const free = state.status ? state.status.free_bytes : null;
    const tooBig = free != null && total > free;
    spaceMsg.hidden = !tooBig;
    if (tooBig) spaceMsg.replaceChildren(icon('warn', 15), ` Not enough free space (needs ${bytes(total)}, ${bytes(free)} free). Free some space in Storage or choose less.`);
    const needsChoice = !P.custom && !P.quick && !P.opts.size;
    const civ2 = civ && sel.size ? civitaiFilter(sel) : { ok: true };
    const reason = needToken ? (civ ? 'Add a Civitai API key in Settings first.' : 'Add a Hugging Face token in Settings first.')
      : needsChoice ? 'Choose what to download above.' : !sel.size ? 'Nothing is selected.' : tooBig ? '' : !civ2.ok ? 'Civitai can only filter by file kind (for example Model, SafeTensor): select all files, or only files of one kind.' : null;
    btn.disabled = reason !== null || submitting;
    btn.lastChild.textContent = `Download · ${bytes(total)}`;
    hint.textContent = reason || '';
    hint.hidden = !reason;
    refresh.total = total;
  }

  let submitting = false;
  async function submit() {
    const sel = selection();
    const all = !P.custom && P.quick === 'all';
    const body = civ ? { source: 'civitai', id: d.id, type: 'model', revision: d.revision || d.id, files: null, total_bytes: refresh.total, file_count: sel.size }
      : { source: 'hf', id: d.id, type: d.type, revision: ctx.version || 'main', files: all ? null : [...sel], total_bytes: refresh.total, file_count: sel.size };
    if (civ) { const f = civitaiFilter(sel); body.file_type = f.file_type || null; body.file_format = f.file_format || null; }
    submitting = true; errMsg.hidden = true; refresh();
    try {
      const r = await api('/jobs', { method: 'POST', body });
      toast(r.already_queued ? 'Already in the queue' : 'Queued', { link: ['Open the queue', '#/queue'] });
      api('/jobs').then(setJobs).catch(() => {});
    } catch (e) {
      errMsg.textContent = e.detail || e.message;
      errMsg.hidden = false;
    } finally { submitting = false; refresh(); }
  }

  const el = h('div', { class: 'picker' }, head, quickRow, ...groupEls, extrasEl, chooser,
    h('div', { class: 'pk-foot' }, summary, spaceMsg, errMsg, btn, hint));
  refresh();
  return { el, refresh };
}
