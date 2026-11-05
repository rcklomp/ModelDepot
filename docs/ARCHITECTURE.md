# Architecture

One Python process (FastAPI, one uvicorn worker) in one container. Download jobs run as short-lived child processes.

```
 browser ──────────────► GUI (static files, /)  ──► /api/hub/*   library, search, picker, jobs, settings, web upload   (server/hub/)
 hf / transformers / vLLM / mlx / datasets ─────► /api/models|datasets/..  and  /{org}/{repo}/resolve/..   (server/adapters/hf_adapter.py)
 hf upload / HfApi.upload_folder ───────────────► create, preupload, LFS batch + PUT, commit            (server/adapters/hf_upload.py)
                                                          │
                                  Repository core (server/repository.py): refs, manifests, blobs, events, locks
                                                          │
                                  Filesystem storage under one root (MIRROR_ROOT=/data)
 download job  ─ python -m server.hub.runner ─►  ingester (HF / Civitai) ─► staging/ ─► verify ─► blobs/ ─► manifest ─► ref
 upload (Hub protocol or browser) ─► staging/uploads/ ─► hash once ─► [store lock] blobs/ ─► manifest ─► ref   (server/hub/upload.py)
```

## The store (plain files, rsync-portable)

```
<root>/blobs/sha256/ab/abcdef...      content-addressed files, immutable; identical files are stored once
<root>/repos/<org>/<name>/            models:   refs.json  revisions/<commit>/manifest.json
<root>/datasets/<org>/<name>/         datasets: same layout
<root>/staging/                       partial downloads (kept for resume) and uploads (staging/uploads/), never served
<root>/jobs/  <root>/config/          the download queue; settings, admin token, the wanted list
<root>/events.jsonl                   append-only audit log      <root>/.store-id   identity of this store
```

A repository's core id is `org/name` (models) or `datasets/org/name` (datasets). Principles: blobs are immutable, a revision never changes what it holds (it only gains files of its own commit, see DESIGN.md), only refs move;
derived state is rebuildable; every write that must survive a power cut is fsync'd (`server/fsutil.py`).

## Concurrency rule that matters

An ingest stores blobs one by one and publishes its manifest at the end, so while it runs its blobs look "unreferenced". `server/locks.py` makes the ingest hold a
shared lock (so does an upload commit, for its whole store-and-publish step; it waits if a GC is running) and GC / delete-repo an exclusive one that never waits; GC also never touches a blob younger than an hour. (Before 2026-11-04 a nightly GC could delete
the files of a running download.)

## What is deliberately not here

Spaces; branches and pull requests (uploads go to `main` only); the Xet protocol (clients set `HF_HUB_DISABLE_XET=1`); any knowledge of the inference engines that use it. The rig, Franken and Ollama code that
used to live here is in the colibri repository (`tools/hot-expert/rig_console/`).
