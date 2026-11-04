# Deployment (Synology DS713+ with Docker, or any Docker host)

The whole application is **one container on one port**: the Hugging Face-compatible API that your AI tools use, the browser GUI, and
the download worker. Everything worth keeping lives in **one folder** that you mount into the container (`/data`): the models and
datasets, the download queue, your settings and the admin token. Move or back up that folder and you have moved the application.

Tested on 2026-11-04: the image builds for `linux/amd64`, starts, passes its health check, serves `snapshot_download` to the real
Hugging Face client, and downloads from huggingface.co through the GUI's queue (Docker Desktop on a Mac, emulating amd64). **Not
tested on the NAS itself** (a DS713+ has an old kernel and an old Docker): the first start there is the real test; see Troubleshooting.

## 1. Get the code and the image onto the NAS

Over ssh on the NAS (the NAS needs `git` and `docker compose`; if it lacks them use B):

```bash
# A. build on the NAS
git clone http://klompnas3971.lan:3000/ronald/LLM_repo.git llm-repo && cd llm-repo     # or copy the folder over
cp .env.example .env && nano .env            # set DATA_DIR=/volume1/docker/llm-repo (create the folder first)
docker compose up -d --build
```

```bash
# B. build on your Mac, ship the image (no build tools needed on the NAS)
docker build --platform linux/amd64 -t llm-repo:latest .
docker save llm-repo:latest | ssh <nas> 'docker load'
# then on the NAS, in a folder with docker-compose.yml and .env:  docker compose up -d
```

Open **http://\<nas\>:8000**. Memory use on the NAS is not measured; the application is one small Python process plus one more while a download runs, and
nothing else (no database, no Node).

## 2. First start

- The server makes an **admin token** and writes it to `DATA_DIR/config/admin-token` (and once to `docker logs llm-repo`). The GUI asks for it the first time you
  change something (download, delete, settings) and remembers it in that browser. Browsing the library needs no token. To choose your own, set `MIRROR_ADMIN_TOKEN` in `.env`.
- Put your **Hugging Face token** in Settings if you want gated models (Llama, Gemma...). It is stored in `DATA_DIR/config/settings.json` (mode 600) and never shown again.
- If the data folder is **not mounted** (a USB disk missing, a typo in `DATA_DIR`) the server refuses to create it on the system disk and says why in `docker logs`.
  An unexpectedly empty library right after an update is the other sign: check `store_id` in the GUI's Storage page against what you wrote down.

## 3. Using it from other machines

The GUI's **Connect** page shows the exact lines for your server. In short, on each machine that should download from the NAS:

```bash
export HF_ENDPOINT=http://<nas>:8000
export HF_HUB_DISABLE_XET=1
hf download Qwen/Qwen3-0.6B            # or transformers, vLLM, mlx_lm, datasets.load_dataset ...
```

If you set `MIRROR_AUTH_TOKEN`, clients also need `HF_TOKEN=<that token>`. See `CLIENT_SETUP.md` for engine-specific notes.
A model that is not in the library gives a 404 and shows up in the GUI's **Wanted** strip with a Download button.

## 4. Download behaviour

- Downloads run **one at a time** (Settings: up to 4). Add a **download window** (for example `01:00-07:00`) to keep a big download off your daytime internet.
- Pause keeps the partial files; Resume (or a restart of the container) continues where it stopped. Remove discards the partial download.
- A download is refused up front when the volume cannot hold it.
- Every file is checked against Hugging Face's sha256 where one is published, then stored by its own sha256 (identical files are stored once).

## 5. Updating, backing up, TLS

- **Update:** pull the new code, `docker compose up -d --build`. The data folder is untouched.
- **Back up / copy:** the data folder is plain files: `rsync -a --partial --inplace /volume1/docker/llm-repo/ other-nas:/volume1/docker/llm-repo/` (`scripts/rsync-to-target.sh` wraps this). Stop the container or avoid copying while a download is being stored.
- **Integrity:** Storage -> "Check integrity" re-hashes every file and lists any that are damaged.
- **TLS** (only if a tool insists on https): use DSM's reverse proxy (Control Panel -> Login Portal -> Advanced -> Reverse Proxy, `https://<nas>:443` -> `http://localhost:8000`).
- Do **not** publish port 8000 to the internet: the Hugging Face routes are open on your LAN by default.

## Troubleshooting

- **`Operation not permitted` / odd permission errors in the logs on an old DSM:** the Docker there predates the image's glibc. In `docker-compose.yml` uncomment `security_opt: seccomp:unconfined`.
- **`Permission denied` writing to the data folder:** the folder belongs to a NAS user; set `user: "<uid>:<gid>"` (see `id <user>`) in `docker-compose.yml`.
- **Download window never opens:** the container's clock zone is UTC unless `TZ` is set in `.env`.
- **A model downloads but the engine cannot find it:** the engine must use the same `HF_ENDPOINT`; `curl -sI http://<nas>:8000/<org>/<name>/resolve/main/config.json` should answer 200.
- **The older no-Docker install script** (`run-on-synology.sh`) was removed on 2026-11-04: it ran as root without a login, started no GUI or download worker, and its non-root option aborted. It is in the git history.
