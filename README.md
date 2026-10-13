# LLM Repository

A general-purpose **local AI artifact repository** with a Hugging Face
compatibility adapter. Inference engines on your LAN treat it as
`huggingface.co` by setting one environment variable:

```bash
export HF_ENDPOINT=https://mirror.lan
export HF_HUB_DISABLE_XET=1     # avoid the proprietary Xet chunk protocol
```

## Why

Censorship / takedown resilience. If Hugging Face is geo-blocked,
rate-limited, or taken down, your machines keep working because they already
pulled what they need and resolve against your mirror. Storage is plain files
on storage you own (external SSD, NAS share) — portable via rsync.

This is **not** a Hugging Face mirror. It is an artifact repository with an HF
adapter. The storage layer has no knowledge of Hugging Face; HF is the first
protocol adapter. Civitai, Ollama, OCI, and filesystem adapters follow without
touching storage.

## Core principles

- Storage is the source of truth.
- Blobs are immutable. Revisions are immutable. References are mutable.
- Protocol adapters are disposable.
- Every operation is crash-safe.
- All derived state is rebuildable.
- Plain files are preferred over databases.

## Status

Early. Milestone 1 (Repository Core) and Milestone 2 (HF compatibility) are the
current focus. See the implementation plan and `docs/` for detail.

## Quick start (dev)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
pytest                              # unit + contract tests
uvicorn server.main:app --reload    # serve the shim
```

## Quick start (Docker, primary deployment)

```bash
cp .env.example .env               # set MIRROR_ROOT, MIRROR_HOST
docker compose up -d
```

## Quick start (Synology DS713+, no Docker)

```bash
./scripts/run-on-synology.sh
```

## The model console on the rig "rome" (port 8002, since 2026-10-13)

`llm-console.service` runs `console.main` from this checkout (`~/LLM_repo`, a git checkout of Gitea `ronald/LLM_repo`;
deploy = push to it, then restart the service). It listens on 127.0.0.1 only: from the Mac,
`ssh -L 8002:127.0.0.1:8002 rome` and open http://localhost:8002.

- **Franken engine** (top of the page): which model serves Open WebUI on port 8081, VRAM per card, and Serve / Stop.
  The console only queues one of four fixed actions through colibri's `tools/hot-expert/franken_service.sh`; a cron
  job (every minute) runs it with colibri's `serve_alt.sh` outside this service's cgroup. A switch takes minutes.
- **Ollama / mirror catalog:** downloads into the mirror, and "Load into Ollama" -- Ollama only; Open WebUI does not
  use Ollama and the Franken engine cannot load these.
- **Guard** (`console/guard.py`): Host allow-list, and changing requests need `X-Console-Request: 1` plus a same-origin
  Origin (CSRF / DNS rebinding through the tunnel).

## Layout

```
server/      HTTP shim (FastAPI): config, storage, blobstore, repository core,
             manifests, refs, events, index, adapters/
ingester/    CLI + transactional ingest (HF first, Civitai later) + jobs
scripts/     deployment helpers (Synology, rsync replication)
tests/       unit, contract (HF header contract), engines (real-client compat)
docs/        DESIGN, API_COMPATIBILITY, FAILURE_MODES, PERFORMANCE, ...
```

See `docs/DESIGN.md` for the architectural rationale and trade-offs.
