import { h, api, codeBlock, origin, setTitle, state, tabs, refreshStatus } from '../lib.js';

const TABS = [['unix', 'macOS / Linux'], ['win', 'Windows (PowerShell)'], ['docker', 'Docker / compose'], ['python', 'Python']];

export default async function connect(page, route) {
  setTitle('Connect');
  const status = state.status || (await refreshStatus({ quiet: true }).catch(() => null));
  let sample = { id: 'org/name', type: 'model', real: false };
  api('/library?sort=size').then((l) => { const i = l.items[l.items.length - 1]; if (i) { sample = { id: i.id, type: i.type, real: true }; draw(); } }).catch(() => {});

  const st = { tab: TABS.some((t) => t[0] === route.query.tab) ? route.query.tab : 'unix', token: !!(status && status.read_token_required), url: origin() };
  const out = h('div');
  const urlIn = h('input', { type: 'text', id: 'c-url', value: st.url, spellcheck: 'false', autocomplete: 'off', oninput: () => { st.url = urlIn.value.trim().replace(/\/+$/, '') || origin(); draw(); } });
  const tokIn = h('input', { type: 'checkbox', checked: st.token, onchange: () => { st.token = tokIn.checked; draw(); } });
  const local = /^(localhost|127\.|\[?::1)/.test(location.hostname);

  page.append(h('div', { class: 'page-head' }, h('h1', null, 'Connect a machine')), h('div', { class: 'connect' },
    h('p', null, 'Any machine on your network can fetch models and datasets from this server instead of the internet. Point its Hugging Face tools at this address, once.'),
    h('div', { class: 'card' },
      h('div', { class: 'field' }, h('label', { for: 'c-url' }, 'Server address'), urlIn,
        local ? h('span', { class: 'warn-t' }, `You opened this page as ${location.host}. Other machines cannot reach “${location.hostname}”: replace it with this server's name or IP address.`)
          : h('span', { class: 'muted' }, 'This is the address you used to open this page.')),
      h('label', { class: 'check' }, tokIn, h('span', null, 'My server requires a token', status && status.read_token_required ? h('span', { class: 'muted' }, ' (it does)') : ''))),
    tabs(TABS, st.tab, (k) => { st.tab = k; history.replaceState(null, '', '#/connect' + (k === 'unix' ? '' : '?tab=' + k)); draw(); }, 'Platform'),
    out,
    h('p', { class: 'muted' }, 'Nothing leaves your network: these machines only talk to this server. Only the server contacts Hugging Face, and only when you start a download on the Download page.')));

  function draw() {
    const u = st.url, t = st.token, id = sample.id, ds = sample.type === 'dataset';
    const dl = `hf download ${id}${ds ? ' --repo-type dataset' : ''}`;
    const note = sample.real ? 'the smallest repository in your library' : 'replace org/name with a repository from your library';
    const lines = {
      unix: [['For this terminal', [`export HF_ENDPOINT=${u}`, 'export HF_HUB_DISABLE_XET=1', t && 'export HF_TOKEN=<read token>']],
        ['Keep it for every new terminal (zsh; use ~/.bashrc for bash)', [`echo 'export HF_ENDPOINT=${u}' >> ~/.zshrc`, `echo 'export HF_HUB_DISABLE_XET=1' >> ~/.zshrc`, t && `echo 'export HF_TOKEN=<read token>' >> ~/.zshrc`]],
        ['Test it', [`curl -sI ${u}/healthz`, '', `# fetch ${note}`, dl]]],
      win: [['For this PowerShell window', [`$env:HF_ENDPOINT = "${u}"`, '$env:HF_HUB_DISABLE_XET = "1"', t && '$env:HF_TOKEN = "<read token>"']],
        ['Keep it for your account', [`[Environment]::SetEnvironmentVariable("HF_ENDPOINT", "${u}", "User")`, '[Environment]::SetEnvironmentVariable("HF_HUB_DISABLE_XET", "1", "User")', t && '[Environment]::SetEnvironmentVariable("HF_TOKEN", "<read token>", "User")']],
        ['Test it', [`curl.exe -sI ${u}/healthz`, '', `# fetch ${note}`, dl]]],
      docker: [['docker run', [`docker run --rm \\`, `  -e HF_ENDPOINT=${u} \\`, '  -e HF_HUB_DISABLE_XET=1 \\', t && '  -e HF_TOKEN=<read token> \\', '  your-image']],
        ['docker-compose.yml', ['services:', '  app:', '    image: your-image', '    environment:', `      HF_ENDPOINT: "${u}"`, '      HF_HUB_DISABLE_XET: "1"', t && '      HF_TOKEN: "<read token>"']],
        ['Test it', [`docker run --rm curlimages/curl -sI ${u}/healthz`]]],
      python: [['At the very top of your script (before importing huggingface_hub, transformers or datasets)', ['import os', '', `os.environ["HF_ENDPOINT"] = "${u}"`, 'os.environ["HF_HUB_DISABLE_XET"] = "1"', t && 'os.environ["HF_TOKEN"] = "<read token>"']],
        ['Then use the libraries as usual', ['from huggingface_hub import snapshot_download', '', `path = snapshot_download("${id}"${ds ? ', repo_type="dataset"' : ''})`, 'print(path)']]],
    }[st.tab];
    out.replaceChildren(...lines.map(([title, ls]) => codeBlock(ls.filter((l) => l !== false && l != null).join('\n'), title)),
      ...(t ? [h('p', { class: 'muted' }, 'Replace ', h('code', null, '<read token>'), ' with the read token of this server. The administrator set it in MIRROR_AUTH_TOKEN.')] : []),
      ...(st.tab === 'docker' ? [h('p', { class: 'muted' }, 'Inside a container, “localhost” is the container itself: use the NAS name or IP address.')] : []));
  }
  draw();
}
