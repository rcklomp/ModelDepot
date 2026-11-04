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

This is **not** only a Hugging Face mirror. It is an artifact repository with an HF
adapter, for models and datasets. The storage layer has no knowledge of Hugging Face; HF is the first
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

A standalone application for a NAS (it has nothing to do with any inference engine or GPU rig; those are just clients).
Today: the Hugging Face routes for models and datasets (`hf download`, `snapshot_download`, `datasets.load_dataset` work against it), an ingester (`repo ingest-hf`, `ingest-civitai`, `ingest-local`) and integrity / GC jobs, all from the command line.
Being built, in this order: a browser GUI served by the same process, one Docker image
for the Synology DS713+, then uploading your own models and datasets. The old rig console that used to live here is `tools/hot-expert/rig_console/` in
the colibri repository (the tag `archive/rig-console` keeps the last version of this repository that carried it).

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
