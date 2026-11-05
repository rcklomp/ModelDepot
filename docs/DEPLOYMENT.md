# Deployment (Synology DS713+ with Docker, or any Docker host)

The whole application is **one container on one port**: the Hugging Face-compatible API that your AI tools use, the browser GUI, and
the download worker. Everything worth keeping lives in **one folder** that you mount into the container (`/data`): the models and
datasets, the download queue, your settings and the admin token. Move or back up that folder and you have moved the application.

Tested on 2026-11-04: the image builds for `linux/amd64`, starts, passes its health check, serves the GUI with its security headers, serves
`snapshot_download` to the real Hugging Face client, takes a 20 MB `hf upload` and returns it byte-identical, and downloads from huggingface.co
through the GUI's queue (Docker Desktop on a Mac, emulating amd64). **Not
tested on the NAS itself** (a DS713+ has an old kernel and an old Docker): the first start there is the real test; see Troubleshooting.

## 0. If an older version is already running on the NAS

On 2026-11-04 the NAS was running version 0.1.0 of this application as a DSM package with its own Python (renamed ModelDepot by the owner's Hermes Agent), on
**port 8010** (the Hugging Face API only: no GUI, no datasets, no uploads) and **port 8011** (the early "Rig Model Manager" console, which is not part of this
application any more; it listed jobs and inventory on the LAN without a token). This version replaces both. To move over:

1. Stop the package (both ports), then **copy its data folder** (`rsync -a --partial <old folder>/ /volume1/docker/modeldepot/`) and keep the old one until you have checked the new one.
2. Set `DATA_DIR` in `.env` to the copy and start the container (section 1). **The address stays http://\<nas\>:8090** (README, `.env.example`, everything below). Only if
   machines of yours really use `HF_ENDPOINT=http://<nas>:8010`, also publish the old port next to it, so nothing has to change on them: create `docker-compose.yml`'s sibling
   `docker-compose.override.yml` with `services: { modeldepot: { ports: ["8010:8000"] } }` (Compose adds it to the 8090 mapping), then `docker compose up -d`. Do not replace 8090 by 8010.
3. Everything in the old store shows up in the library. Checked on a store written by that version's code: no existing file was changed, and the real Hugging Face client
   downloads every repo. New next to the old files: `.store-id`, `config/`, `jobs/`, `datasets/`.

Repos that version imported with `repo ingest-local` gave **wrong files** to `snapshot_download` (every file of the repo came back with the same bytes, because they all
carried one ETag). This version serves them correctly with no change to the store; clients that downloaded them earlier should delete their cached copy of those repos.

## 1. Get the code and the image onto the NAS

Over ssh on the NAS (the NAS needs `git` and Docker Compose; if it lacks them use B). The DS713+ has only the older standalone **`docker-compose`** (v1, no `docker compose` plugin, checked 2026-11-04): where this document says `docker compose ...`, type `docker-compose ...` there; the files are the same and an override file is merged the same way:

```bash
# A. build on the NAS
git clone http://klompnas3971.lan:3000/ronald/ModelDepot.git modeldepot && cd modeldepot     # or copy the folder over
cp .env.example .env && nano .env            # set DATA_DIR=/volume1/docker/modeldepot (create the folder first)
docker compose up -d --build
```

```bash
# B. build on your Mac, ship the image (no build tools needed on the NAS)
docker build --platform linux/amd64 -t modeldepot:latest .
docker save modeldepot:latest | ssh <nas> 'docker load'
# then on the NAS, in a folder with docker-compose.yml and .env:  docker compose up -d
```

Open **http://\<nas\>:8090** (not 8000: your NAS already uses that for the restic backup server; change `PORT` in `.env` if 8090 is taken too). Memory use on the NAS is not measured; the application is one small Python process plus one more while a download runs, and
nothing else (no database, no Node).

## 2. First start

- The server makes an **admin token** and writes it to `DATA_DIR/config/admin-token` (and once to `docker logs modeldepot`). The GUI asks for it the first time you
  change something (download, delete, settings) and remembers it in that browser. Browsing the library needs no token. To choose your own, set `MIRROR_ADMIN_TOKEN` in `.env`.
- Put your **Hugging Face token** in Settings if you want gated models (Llama, Gemma...). It is stored in `DATA_DIR/config/settings.json` (mode 600) and never shown again.
- If the data folder is **not mounted** (a USB disk missing, a typo in `DATA_DIR`) the server refuses to create it on the system disk and says why in `docker logs`.
  An unexpectedly empty library right after an update is the other sign: check `store_id` in the GUI's Storage page against what you wrote down.

## 3. Using it from other machines

The GUI's **Connect** page shows the exact lines for your server. In short, on each machine that should download from the NAS:

```bash
export HF_ENDPOINT=http://<nas>:8090
export HF_HUB_DISABLE_XET=1
hf download Qwen/Qwen3-0.6B            # or transformers, vLLM, mlx_lm, datasets.load_dataset ...
```

If you set `MIRROR_AUTH_TOKEN`, clients also need `HF_TOKEN=<that token>`. See `CLIENT_SETUP.md` for engine-specific notes.
A model that is not in the library gives a 404 and shows up in the GUI's **Wanted** strip with a Download button.

## 4. Download behaviour

- Downloads run **one at a time** (Settings: up to 4). Add a **download window** (for example `01:00-07:00`) to keep a big download off your daytime internet.
- Pause keeps the partial files; Resume (or a restart of the container) continues where it stopped. Remove discards the partial download.
- A download is refused up front when the volume cannot hold it.
- **A single file over 50 GB works.** The Hugging Face client itself refuses to start one without Xet ("The file is too large to be downloaded using the regular download method ... Install `hf_xet`"); this application does not use Xet, so it lifts that limit and downloads by plain HTTP with resume. Do not install `hf_xet` in the image to get around the message: it is removed on purpose. If a repo failed with that message before 2026-11-05, update the image (section 5) and press Retry: the files that had arrived are kept.
- Every file is checked against Hugging Face's sha256 where one is published, then stored by its own sha256 (identical files are stored once).

## 5. Updating, backing up, TLS

- **Update:** pull the new code, `docker compose up -d --build`. The data folder is untouched.
- **Which version is running:** the footer of every GUI page says `Version x.y.z` (bottom right); the same number is in `GET /api/hub/status` and `/openapi.json` (`info.version`). After an update it must be the new one: an old number means the old image is still running.
- **Back up / copy:** the data folder is plain files: `rsync -a --partial --inplace /volume1/docker/modeldepot/ other-nas:/volume1/docker/modeldepot/` (`scripts/rsync-to-target.sh` wraps this). Stop the container or avoid copying while a download is being stored.
- **Integrity:** Storage -> "Check integrity" re-hashes every file and lists any that are damaged.
- **TLS** (only if a tool insists on https): use DSM's reverse proxy (Control Panel -> Login Portal -> Advanced -> Reverse Proxy, `https://<nas>:443` -> `http://localhost:8090`).
- Do **not** publish port 8090 to the internet: the Hugging Face routes are open on your LAN by default.

## Troubleshooting

- **`Operation not permitted` / odd permission errors in the logs on an old DSM:** the Docker there predates the image's glibc. In `docker-compose.yml` uncomment `security_opt: seccomp:unconfined`.
- **`Permission denied` writing to the data folder:** the folder belongs to a NAS user; set `user: "<uid>:<gid>"` (see `id <user>`) in `docker-compose.yml`.
- **Download window never opens:** the container's clock zone is UTC unless `TZ` is set in `.env`.
- **A model downloads but the engine cannot find it:** the engine must use the same `HF_ENDPOINT`; `curl -sI http://<nas>:8090/<org>/<name>/resolve/main/config.json` should answer 200.
- **The older no-Docker install script** (`run-on-synology.sh`) was removed on 2026-11-04: it ran as root without a login, started no GUI or download worker, and its non-root option aborted. It is in the git history.
