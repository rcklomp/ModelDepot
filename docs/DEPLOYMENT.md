# Deployment

The mirror runs as a small HTTP service that inference engines on your LAN
reach via `HF_ENDPOINT`. There are two paths: **no-Docker** (for the DS713+ and
any box without Docker) and **Docker Compose** (for newer Synology, mini PCs,
NUCs). Both point at the same store directory, which can live on an external
SSD, a NAS share, or internal disk.

> **Offline-first note.** Once a model is ingested, the mirror needs no
> internet. Ingest from a connection with good bandwidth (e.g. while traveling),
> then deploy the store on a NAS at home with a limited link — clients load
> entirely from the LAN. See *Offline workflow* below.

## Quick reference: what goes where

| Component | Where |
|---|---|
| Store (blobs, manifests, refs) | A directory you own: `/volume1/llm-mirror`, `/Volumes/MySSD/llm-mirror`, etc. |
| Server (FastAPI + uvicorn) | A host on your LAN: the NAS, a mini PC, a NUC |
| Clients | Any machine on the LAN, setting `HF_ENDPOINT` |

The store and the server are decoupled: you can move the store between drives
(rsync) and point any server at it by setting `MIRROR_ROOT`.

---

## Path A — Synology without Docker (DS713+ and similar)

For the DS713+ (Intel Atom D2700, 1 GB RAM, no Docker support) and any DSM 6/7
box where you don't want Docker. Uses the NAS's built-in Python in a venv, and
installs a boot service that survives DSM upgrades.

### One-time install

Copy the repo onto the NAS (USB, download, or git), then:

```bash
sudo ./scripts/run-on-synology.sh /volume1/llm-mirror
# optionally set a custom port:
sudo ./scripts/run-on-synology.sh /volume1/llm-mirror 8000
```

The script:
1. Finds Python ≥ 3.8 (the built-in `/usr/local/bin/python3` on DSM 7.1).
2. Creates a venv at `/volume1/@appdata/llm-repo/venv` (gets pip without a C
   compiler).
3. Installs the server with **bare uvicorn** (not `[standard]`, which would
   need a C compiler for `uvloop`/`httptools`).
4. Writes `/usr/local/etc/rc.d/S99llm-repo.sh` — a SysV service that starts the
   mirror on every boot. This directory is preserved across DSM updates.
5. Starts it immediately.

### Managing the service

```bash
/usr/local/etc/rc.d/S99llm-repo.sh start
/usr/local/etc/rc.d/S99llm-repo.sh stop
/usr/local/etc/rc.d/S99llm-repo.sh restart
/usr/local/etc/rc.d/S99llm-repo.sh status
tail -f /volume1/llm-mirror/log/server.log
```

### Resource notes for the DS713+

- **One worker only.** The service runs `uvicorn --workers 1`. The Atom D2700
  is a low-clock dual-core; multiple workers would just multiply RAM (~50 MB
  each) without throughput gain for an I/O-bound blob server.
- **~50 MB RSS idle**, leaving ample headroom in 1 GB.
- **`MIRROR_INDEX_BACKEND=none`** is set by default on Synology — the SQLite
  index is a cache and not worth the RAM/IO on this hardware; directory walks
  are fast enough. Override with `MIRROR_INDEX_BACKEND=sqlite` if you have tens
  of thousands of files.
- **TLS**: front with DSM's built-in reverse proxy
  (Control Panel → Login Portal → Advanced → Reverse Proxy:
  `https://<host>:443` → `http://localhost:8000`). Needed only if an engine
  rejects plain HTTP.

### Updating the code

Re-run the same command — it copies fresh code into `/volume1/@appdata/llm-repo`
and restarts the service. The store is untouched.

---

## Path B — Docker Compose (newer Synology, mini PC, NUC)

For hosts with Docker (DS224+/DS423+ with Container Manager, Intel/AMD mini
PCs, NUCs). Caddy fronts the shim for TLS/HTTP-2/compression/Range.

```bash
cp .env.example .env
# edit .env: set MIRROR_ROOT to your store path, MIRROR_HOST to your hostname
docker compose up -d
```

This runs two containers:
- `shim` — the FastAPI server (`server.main:app` via uvicorn).
- `caddy` — reverse proxy on 80/443, TLS with automatic certs (or internal CA
  for LAN-only hostnames), HTTP/2, compression, Range pass-through.

Logs: `docker compose logs -f shim`. The store is a bind-mounted volume, so
moving it is just changing `MIRROR_ROOT` in `.env`.

---

## Path C — Bare metal (Linux/macOS, any always-on box)

For a mini PC or NUC where you'd rather not use Docker:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
export MIRROR_ROOT=/srv/llm-mirror
uvicorn server.main:app --host 0.0.0.0 --port 8000 --workers 1
```

For a systemd unit on Linux:

```ini
[Unit]
Description=LLM Repository mirror
After=network.target

[Service]
Type=simple
User=ronald
Environment=MIRROR_ROOT=/srv/llm-mirror
Environment=MIRROR_INDEX_BACKEND=sqlite
WorkingDirectory=/home/ronald/LLM_repo
ExecStart=/home/ronald/LLM_repo/.venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8000 --workers 1
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

---

## Offline workflow (ingest while you have bandwidth, serve at home)

This is the key pattern when the NAS has a limited internet connection:

1. **On a machine with good bandwidth** (laptop at a café, a VPS), ingest the
   models you want into a store on a fast local drive:
   ```bash
   export MIRROR_ROOT=/Volumes/FastSSD/llm-mirror
   repo ingest-hf microsoft/Phi-3-mini-4k-instruct
   repo ingest-hf mistralai/Mistral-7B-Instruct-v0.2
   repo ingest-hf meta-llama/Llama-3.2-1B
   ```
2. **Move the store to the NAS** (USB drive, or rsync over the LAN):
   ```bash
   ./scripts/rsync-to-target.sh /Volumes/FastSSD/llm-mirror /volume1/llm-mirror
   # or directly to the NAS over SSH:
   ./scripts/rsync-to-target.sh /Volumes/FastSSD/llm-mirror nas:/volume1/llm-mirror
   ```
3. **On the NAS**, run `repo verify <repo_id>` to confirm integrity after the
   move (blobs are content-addressed, so this catches any transfer corruption).
4. The mirror now serves these models over the LAN with **zero internet
   required**. Clients set `HF_ENDPOINT=http://<nas>:8000` and load away.

Because blobs are immutable and content-addressed, rsync replication is
bandwidth-efficient (only new blobs transfer) and safely resumable.

---

## Ingesting large models (resume on interrupt)

Big models — a 100+ GiB GGUF in 4 shards, or a multi-GB safetensors repo — take
hours to download and will inevitably hit a network drop or two. The ingester is
built for this; nothing about an interrupted ingest is lost.

### How resume works

- **Per-file resume.** `huggingface_hub` downloads each file via HTTP Range into
  an internal cache under `staging/.cache/huggingface/`, leaving a `.incomplete`
  file if interrupted. Re-running the same command continues from exactly where
  it left off — completed shards are never re-downloaded.
- **Deterministic staging.** Staging lives at `staging/hf-<repo-slug>` (a fixed
  path per repo), so the resume cache is found on re-run. (Same for Civitai:
  `staging/civitai-<version_id>`.)
- **Staging is kept on failure.** If an ingest dies — network drop, Ctrl-C,
  process killed — the staging dir is *not* cleaned up, so the next run resumes.
  It is only removed after a successful publish.
- **Automatic retry.** The download is wrapped in a retry loop: on a transient
  failure (network drop, timeout, HTTP 429/5xx) it backs off (exponential, capped
  at 60s) and retries up to `--retries` times (default 6), resuming each time.
  Fatal errors (404, gated, bad revision) fail immediately without wasting
  retries.

Note: the old `hf_transfer` accelerator is deprecated in `huggingface_hub` 1.x
(its replacement, the Xet backend, is disabled here), so resume-around-retries
is the supported path for large files.

### Babysitting a long download

Every ingest writes structured progress lines to a log file you can tail:

```bash
# In one terminal, kick off the big download:
export MIRROR_ROOT=/Volumes/2TB/llm-mirror
export HF_HUB_DISABLE_XET=1
repo ingest-hf unsloth/DeepSeek-V4-Flash-0731-GGUF --rev main --allow "UD-IQ3_XXS/*"

# In another, watch what's happening:
tail -f /Volumes/2TB/llm-mirror/log/ingest-unsloth-DeepSeek-V4-Flash-0731-GGUF.log
```

Log lines look like:
```
2026-08-04T16:47:37+00:00 attempt=1 repo=... status=downloading
2026-08-04T16:48:10+00:00 attempt=1 repo=... status=retry reason=HfHubHTTPError(503) wait=2s (will resume from HF cache)
2026-08-04T16:50:02+00:00 attempt=2 repo=... status=complete
```

### If it's interrupted (connection drop, you closed the laptop, etc.)

Just run the exact same command again. It resumes:

```bash
repo ingest-hf unsloth/DeepSeek-V4-Flash-0731-GGUF --rev main --allow "UD-IQ3_XXS/*"
```

There's no special "resume" flag — re-running IS resume. You'll see it pick up
from the partial download rather than restart.

### Pulling one quant of a multi-quant repo

Large GGUF repos ship many quants (often 1+ TiB total). Use `--allow` to grab
just the quant directory you want, so you don't accidentally pull everything:

```bash
# Just the UD-IQ3_XXS quant (~97 GiB, 4 shards), not the whole ~1.4 TiB repo:
repo ingest-hf unsloth/DeepSeek-V4-Flash-0731-GGUF --rev main --allow "UD-IQ3_XXS/*"
```

### Tuning for a flaky link

```bash
# More retries (default 6) and a custom progress log:
repo ingest-hf <repo> --retries 12 --progress-log /tmp/deepseek-ingest.log
```

### Cleaning up a failed ingest's staging

If you genuinely want to discard a partial download and start fresh (e.g. you
changed the `--allow` pattern and the cache is stale), remove the staging dir:

```bash
rm -rf $MIRROR_ROOT/staging/hf-<repo-slug>
```

---

## Replication / backup

The store is plain files — replicate it anywhere with rsync:

```bash
# To an external SSD (cold backup):
./scripts/rsync-to-target.sh /volume1/llm-mirror /Volumes/BackupSSD/llm-mirror

# To a second NAS:
./scripts/rsync-to-target.sh /volume1/llm-mirror nas2:/volume1/llm-mirror
```

`--delete` keeps the target a true mirror; `--partial --inplace` make large
blob transfers resumable. See `scripts/rsync-to-target.sh` for details.

## Troubleshooting

- **Engine can't resolve a model**: confirm `HF_ENDPOINT` points at the mirror
  and `HF_HUB_DISABLE_XET=1` is set (Xet is not implemented). Check
  `repo list` shows the repo and `repo refs <repo_id>` shows `main`.
- **Slow first download from the mirror**: the first read of a large blob may
  be disk-bound on the NAS (HDD vs SSD matters a lot — see PERFORMANCE.md).
  Subsequent reads benefit from the OS page cache.
- **Corrupt blob after a move**: `repo verify <repo_id>` re-hashes every blob
  against its manifest sha256 and reports any mismatch.
