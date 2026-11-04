// A small, safe Markdown renderer. It builds DOM nodes with text nodes only: README text from the network never becomes HTML.
// Supported: front matter (shown folded), headings, paragraphs, fenced code, lists (nested), block quotes, tables, rules,
// **bold**, *italic*, `code`, links (http/https only; images are not loaded, they show as [image: alt]).
import { h, safeUrl } from './lib.js';

function link(href, kids) {
  const u = safeUrl(href);
  return u ? h('a', { href: u, rel: 'noopener noreferrer', target: '_blank' }, kids) : h('span', null, kids);
}

const RULES = [
  [/`([^`\n]+)`/, (m) => h('code', null, m[1])],
  [/\[!\[([^\]]*)\]\([^)]*\)\]\(\s*<?([^)\s>]*)>?[^)]*\)/, (m) => link(m[2], m[1] || 'link')],
  [/!\[([^\]]*)\]\([^)]*\)/, (m) => h('span', { class: 'md-img' }, `[image${m[1] ? ': ' + m[1] : ''}]`)],
  [/\[([^\]]+)\]\(\s*<?([^)\s>]*)>?(?:\s+"[^"]*")?\s*\)/, (m) => link(m[2], inline(m[1]))],
  [/<(https?:\/\/[^>\s]+)>/, (m) => link(m[1], m[1])],
  [/https?:\/\/[^\s<>()[\]]*[^\s<>()[\].,;:!?'"]/, (m) => link(m[0], m[0])],
  [/\*\*(?=\S)([\s\S]+?)\*\*/, (m) => h('strong', null, inline(m[1]))],
  [/__(?=\S)([\s\S]+?)__/, (m) => h('strong', null, inline(m[1]))],
  [/\*(?=[^\s*])([^*]+?)\*/, (m) => h('em', null, inline(m[1]))],
  [/(^|\W)_(?=[^\s_])([^_]+?)_(?=\W|$)/, (m) => [m[1], h('em', null, inline(m[2]))]],
];

function stripHtml(t) {
  return t.replace(/<\/?[a-zA-Z][^>]*>/g, (tag) => (/^<https?:/i.test(tag) ? tag : ''));
}

export function inline(text) {
  const out = [];
  let rest = stripHtml(text);
  while (rest) {
    let best = null;
    for (const [re, fn] of RULES) {
      const m = re.exec(rest);
      if (m && (!best || m.index < best.m.index)) best = { m, fn };
    }
    if (!best) { out.push(rest); break; }
    if (best.m.index) out.push(rest.slice(0, best.m.index));
    out.push(best.fn(best.m));
    rest = rest.slice(best.m.index + best.m[0].length);
  }
  return out;
}

const LI = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const HR = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const cells = (l) => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());

function buildList(items) {
  const mk = (it) => h(it.ordered ? 'ol' : 'ul');
  const root = mk(items[0]);
  const stack = [{ indent: items[0].indent, el: root }];
  let last = null;
  for (const it of items) {
    let top = stack[stack.length - 1];
    if (it.indent > top.indent && last) { const el = mk(it); last.append(el); top = { indent: it.indent, el }; stack.push(top); }
    else while (stack.length > 1 && it.indent < top.indent) { stack.pop(); top = stack[stack.length - 1]; }
    last = h('li', null, inline(it.text));
    top.el.append(last);
  }
  return root;
}

function blocks(lines, into) {
  let i = 0, para = [];
  const flush = () => { if (para.length) { into.append(h('p', null, inline(para.join(' ')))); para = []; } };
  while (i < lines.length) {
    const line = lines[i];
    let m;
    if (!line.trim()) { flush(); i++; continue; }
    if ((m = /^\s*(```+|~~~+)\s*([\w+-]*)/.exec(line))) {
      flush();
      const fence = m[1][0], code = [];
      for (i++; i < lines.length && !new RegExp('^\\s*' + fence + '{3,}\\s*$').test(lines[i]); i++) code.push(lines[i]);
      i++;
      into.append(h('pre', { class: 'md-code' }, h('code', null, code.join('\n'))));
      continue;
    }
    if ((m = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line))) { flush(); into.append(h('h' + Math.min(6, m[1].length + 1), null, inline(m[2]))); i++; continue; }
    if (para.length && /^\s*=+\s*$/.test(line)) { into.append(h('h2', null, inline(para.join(' ')))); para = []; i++; continue; }
    if (HR.test(line)) {
      if (para.length && /^\s*-+\s*$/.test(line)) { into.append(h('h3', null, inline(para.join(' ')))); para = []; } else { flush(); into.append(h('hr')); }
      i++; continue;
    }
    if (/^\s{0,3}>/.test(line)) {
      flush();
      const q = [];
      for (; i < lines.length && /^\s{0,3}>/.test(lines[i]); i++) q.push(lines[i].replace(/^\s{0,3}> ?/, ''));
      const bq = h('blockquote');
      blocks(q, bq);
      into.append(bq);
      continue;
    }
    if (line.includes('|') && i + 1 < lines.length && SEP.test(lines[i + 1]) && lines[i + 1].includes('-')) {
      flush();
      const head = cells(line), rows = [];
      for (i += 2; i < lines.length && lines[i].trim() && lines[i].includes('|'); i++) rows.push(cells(lines[i]));
      into.append(h('div', { class: 'md-table' }, h('table', null, h('thead', null, h('tr', null, head.map((c) => h('th', null, inline(c))))),
        h('tbody', null, rows.map((r) => h('tr', null, head.map((_, k) => h('td', null, inline(r[k] || '')))))))));
      continue;
    }
    if (LI.test(line)) {
      flush();
      const items = [];
      while (i < lines.length) {
        const li = LI.exec(lines[i]);
        if (li) { items.push({ indent: li[1].replace(/\t/g, '    ').length, ordered: /\d/.test(li[2]), text: li[3] }); i++; continue; }
        if (!lines[i].trim()) {
          let j = i;
          while (j < lines.length && !lines[j].trim()) j++;
          if (j < lines.length && LI.test(lines[j])) { i = j; continue; }
          break;
        }
        if (/^\s+\S/.test(lines[i]) && items.length) { items[items.length - 1].text += ' ' + lines[i].trim(); i++; continue; }
        break;
      }
      into.append(buildList(items));
      continue;
    }
    para.push(line.trim());
    i++;
  }
  flush();
}

export function renderMarkdown(src) {
  const root = h('div', { class: 'md' });
  let text = String(src).replace(/\r\n?/g, '\n');
  const fm = /^---\n([\s\S]*?)\n---[ \t]*(\n|$)/.exec(text);
  if (fm) {
    root.append(h('details', { class: 'md-meta' }, h('summary', null, 'Metadata (front matter)'), h('pre', null, h('code', null, fm[1]))));
    text = text.slice(fm[0].length);
  }
  blocks(text.replace(/<!--[\s\S]*?-->/g, '').split('\n'), root);
  return root;
}
