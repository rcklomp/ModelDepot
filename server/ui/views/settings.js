import { h, api, askToken, copyBtn, emit, errorBox, loading, refreshStatus, setTitle, themePref, toast, tokens } from '../lib.js';

export default async function settings(page) {
  setTitle('Settings');
  const body = h('div', null, loading());
  page.append(h('div', { class: 'page-head' }, h('h1', null, 'Settings')), body);
  let alive = true;

  async function load() {
    try { const s = await api('/settings'); if (alive) draw(s); } catch (e) { if (alive) body.replaceChildren(errorBox(e, load)); }
  }

  async function save(changes, done) {
    try {
      const s = await api('/settings', { method: 'PUT', body: changes });
      toast(done || 'Saved');
      refreshStatus({ quiet: true }).catch(() => {});
      if (alive) draw(s, true);
    } catch (e) { toast(e.detail || e.message, { kind: 'error' }); }
  }

  function tokenCard(title, key, isSet, fromEnv, text) {
    const input = h('input', { type: 'password', id: 'tok-' + key, autocomplete: 'new-password', spellcheck: 'false', placeholder: isSet ? 'A token is stored: type a new one to replace it' : 'Paste the token here' });
    return h('section', { class: 'card' }, h('h2', null, title),
      h('p', { class: 'status-line' }, h('span', { class: 'dot ' + (isSet ? 'ok' : '') }), isSet ? (fromEnv ? 'A token is set from the server\'s environment and is used first.' : 'A token is stored.') : 'No token is set.'),
      h('p', { class: 'muted' }, text, ' The token is stored on the NAS and is never shown again, not even here.'),
      h('form', { class: 'row gap wrap', onsubmit: (e) => { e.preventDefault(); if (input.value.trim()) save({ [key]: input.value.trim() }, 'Token saved'); } },
        h('label', { class: 'sr', for: 'tok-' + key }, title), input, h('button', { class: 'btn primary', type: 'submit' }, 'Save'),
        h('button', { class: 'btn', type: 'button', disabled: !isSet || fromEnv, title: fromEnv ? 'This one comes from the server environment: remove it there' : '', onclick: () => save({ [key]: '' }, 'Token removed') }, 'Clear')));
  }

  function draw(s, keepScroll) {
    const win = h('input', { type: 'text', id: 'win', value: s.download_window, placeholder: '01:00-07:00', pattern: '([01]\\d|2[0-3]):[0-5]\\d-([01]\\d|2[0-3]):[0-5]\\d|', spellcheck: 'false', autocomplete: 'off', 'aria-describedby': 'win-h' });
    const conc = h('input', { type: 'number', id: 'conc', min: '1', max: '4', value: String(s.max_concurrent) });
    const theme = h('select', { id: 'theme', onchange: () => { themePref.set(theme.value); emit('theme'); } },
      [['auto', 'Follow my system'], ['dark', 'Dark'], ['light', 'Light']].map(([v, t]) => h('option', { value: v, selected: v === themePref.get() }, t)));
    const adminCard = () => {
      const has = !!tokens.admin;
      return h('section', { class: 'card' }, h('h2', null, 'Admin token'),
        h('p', { class: 'status-line' }, h('span', { class: 'dot ' + (has ? 'ok' : '') }), has ? 'This browser has an admin token.' : 'This browser has no admin token yet.'),
        h('p', null, 'Downloading, deleting and changing settings need the admin token. The server creates it the first time it starts. It is in this file on the server:'),
        h('p', { class: 'row gap-s wrap' }, h('code', null, s.admin_token_file), copyBtn(s.admin_token_file, { label: 'Copy path' })),
        h('p', { class: 'muted' }, 'It was also printed once in the container log on that first start. This page asks for it the first time you change something, and keeps it in this browser only.'),
        h('div', { class: 'row gap' }, has ? null : h('button', { class: 'btn primary', type: 'button', onclick: async () => { try { await askToken('admin'); toast('Admin token saved in this browser'); draw(s); } catch { /* cancelled */ } } }, 'Enter the admin token'),
          h('button', { class: 'btn', type: 'button', onclick: () => { tokens.clear(); toast('Signed out: the tokens were forgotten by this browser'); draw(s); } }, 'Sign out')),
        h('p', { class: 'muted' }, 'Sign out makes this browser forget the admin token and the read token. Nothing changes on the server.'));
    };
    const cards = h('div', { class: 'cards' },
      tokenCard('Hugging Face token', 'hf_token', s.hf_token_set, s.hf_token_from_env, 'It lets the server download gated and private repositories with your account.'),
      tokenCard('Civitai API key', 'civitai_token', s.civitai_token_set, s.civitai_token_from_env, 'Civitai only lets signed-in accounts download, so this is needed for every Civitai download.'),
      h('section', { class: 'card' }, h('h2', null, 'Downloads'),
        h('form', { onsubmit: (e) => { e.preventDefault(); save({ download_window: win.value.trim(), max_concurrent: Math.max(1, Math.min(4, parseInt(conc.value, 10) || 1)) }, 'Download settings saved'); } },
          h('div', { class: 'field' }, h('label', { for: 'win' }, 'Download window'), win,
            h('span', { class: 'muted', id: 'win-h' }, 'Downloads only start between these times, as HH:MM-HH:MM on the server clock (for example 01:00-07:00, also across midnight). Empty means any time. Running downloads are not interrupted.')),
          h('div', { class: 'field' }, h('label', { for: 'conc' }, 'Downloads at a time'), conc, h('span', { class: 'muted' }, '1 to 4. One is kind to a small NAS.')),
          h('button', { class: 'btn primary', type: 'submit' }, 'Save'))),
      adminCard(),
      h('section', { class: 'card' }, h('h2', null, 'Appearance'), h('div', { class: 'field' }, h('label', { for: 'theme' }, 'Theme'), theme)));
    const y = keepScroll ? document.getElementById('main').scrollTop : 0;
    body.replaceChildren(cards);
    document.getElementById('main').scrollTop = y;
  }
  load();
  return () => { alive = false; };
}
