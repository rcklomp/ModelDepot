# Design

## Goal

A self-hosted, censorship-resilient local repository for AI model artifacts
that inference engines consume as if it were Hugging Face. The whole point is
resilience: if `huggingface.co` is geo-blocked, rate-limited, or taken down,
machines on the LAN keep working because the artifacts are already local and
resolve against the mirror.

## The key insight

Hugging Face's own client libraries (`huggingface_hub`, `transformers`,
`diffusers`, vLLM, TGI, MLX, Ollama's `hf.co/...` puller) all honour a single
environment variable: `HF_ENDPOINT`. Point it at any server that speaks enough
of the HF Hub HTTP API and clients treat that server as `huggingface.co`.

The entire HF compatibility surface reduces to four GET routes plus three
response headers (verified against `huggingface_hub` source):

- `GET /api/models/{repo_id}` → metadata JSON (`sha`, `siblings`, `private`, `gated`).
- `GET /api/models/{repo_id}/tree/{rev}/{path?}` → file listing.
- `GET /{repo_id}/resolve/{rev}/{filename}` → `200`/`206` + bytes, with headers
  `X-Repo-Commit`, `ETag` (or `X-Linked-Etag`), `Content-Length` (or
  `X-Linked-Size`). Honor `Range` for resumability.
- Optional: `/commits/{rev}`, `/refs`, `POST /paths-info/{rev}`.

Client config is just:

```
HF_ENDPOINT=https://mirror.lan
HF_HUB_DISABLE_XET=1     # avoid the proprietary Xet chunk protocol
```

This is a **verified minimum compatibility surface for current HF clients**, not
a permanent contract. HF has changed APIs before; the adapter absorbs that
churn — the core does not.

## Architectural inversion: artifact repository, not an HF mirror

The design is layered so the storage layer never knows what Hugging Face is:

```
Engines (HF_ENDPOINT=mirror)
  → Caddy (TLS, HTTP/2, compression, Range, cache)
    → Protocol adapters (hf first; later civitai, ollama, oci, fs)
      → Repository Core (pure domain logic)
        → StorageBackend interface (FilesystemStorage only; S3 later)
          → Content-addressable blob store
```

HF is merely the first protocol adapter. Adding Civitai / Ollama / OCI does not
touch storage, manifests, or the repository core.

## Core principles

- **Storage is the source of truth.** Blobs + manifests + refs define everything.
- **Blobs are immutable.** The blob store contains only immutable blobs — no
  sidecar files, no metadata. Identical content is stored once (dedup).
- **A revision never changes what it holds.** A published revision is never
  overwritten and none of its files changes or disappears. A revision names one
  fixed commit, so files of that commit that arrive later (the same repository
  pulled in parts) are ADDED to it (`RepositoryCore.publish_or_extend`, under one
  lock per repository); a path whose content would differ is refused. To update a
  model, publish a new revision and move a reference.
- **References are mutable.** `refs.json` is the only live-pointer state.
- **Protocol adapters are disposable.** If HF changes its API, only
  `hf_adapter.py` changes.
- **Every operation is crash-safe.** Interrupted ingests leave only `staging/`
  dirt; never a half-visible repo. Atomic publish = manifest appears at its
  final path in one filesystem rename.
- **All derived state is rebuildable.** `index/`, refcounts, and the event log
  are derived / append-only. Deleting the index never loses data.
- **Plain files are preferred over databases.** The store is rsync-portable.

## Content-addressable storage

Blobs are stored by SHA-256 in a sharded layout:

```
blobs/sha256/ab/abcdef0123...
```

This gives deduplication, corruption detection, and rsync-friendliness, and
makes the repository resemble an OCI registry — a useful future capability
without any storage-format change. SHA-256 is the canonical identity; an HF
ETag is merely namespaced adapter metadata.

Reference counts are **derived** during `repo gc` (walking all manifests) and
optionally cached in the rebuildable index — not stored as blob sidecars. This
keeps the blob store to immutable blobs only.

## Transactional ingest

```
1. download   → staging/{txid}/                       (temp, never served)
2. verify sha256 → reject on mismatch                 (emit IntegrityFailed)
3. move to blobs/ → dedup                             (emit BlobStored)
4. generate manifest (schema 1, sorted, namespaced)   (emit RevisionImported)
5. atomic publish → revisions/{commit}/               (emit RevisionPublished)
6. update ref → refs.json: main → new commit          (emit ReferenceUpdated)
```

Atomic publish = the manifest appears at its final path in one filesystem op.
The ref update is the only mutation of live state. Rollback =
`repo refs set <repo> main <old_commit>`.

## Future: OCI

The repository already resembles an OCI registry (content-addressed blobs,
immutable revisions, refs). An OCI adapter is a realistic future addition for
replication, verification, and dedup via existing OCI tooling — without any
storage-format change.

## Trade-offs

- **More upfront work than `pip install olah`.** In exchange, you own every
  layer and the design is not coupled to another project's lifecycle.
- **No Xet support.** Avoided via `HF_HUB_DISABLE_XET=1`. If Xet ever becomes
  mandatory for public downloads, the adapter will need to grow.
- **LM Studio is a documented exception.** It has its own catalog; we serve it
  via local-file import rather than emulating its proprietary catalog.
