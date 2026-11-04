# API Compatibility

Exactly which Hugging Face endpoints and headers the HF adapter implements, which
are tested, and which are intentionally omitted. This is the **verified minimum
compatibility surface for current HF clients** — not a permanent contract. If HF
changes its API, only `server/adapters/hf_adapter.py` changes.

## How clients reach the mirror

```bash
export HF_ENDPOINT=http://<mirror>:<port>
export HF_HUB_DISABLE_XET=1
```

`huggingface_hub` (and every engine built on it) reads `HF_ENDPOINT` at import
time and routes all resolution and downloads there.

## Implemented endpoints

All GET (the mirror is read-only; ingest happens via the CLI).

| Endpoint | Status | Purpose |
|---|---|---|
| `GET /api/models/{repo_id}` | ✅ implemented + tested | Model metadata (`sha`, `siblings`, `private`, `gated`) |
| `GET /api/models/{org}/{repo}/revision/{rev}` | ✅ implemented + tested with the real client | Metadata at a revision: what `snapshot_download`, `hf download` and `model_info(revision=...)` call. Missing until 2026-11-04: whole-repo downloads failed with a 404. |
| `GET /api/models/{org}/{repo}/tree/{rev}/{path?}` | ✅ implemented + tested with the real client | File listing. Without `?recursive` the direct children of `path` (files and folders); with `?recursive=true` every file below it. Until 2026-11-04 `recursive` was ignored, so files in sub-folders were silently skipped by `snapshot_download`. |
| `GET /{repo_id}/resolve/{rev}/{filename}` | ✅ implemented + tested (GET + HEAD) | Blob download — the load-bearing route |
| `GET /api/models/{org}/{repo}/refs` | ✅ implemented + tested | Branch/tag references |
| `GET /api/models/{org}/{repo}/commits/{rev}` | ✅ implemented + tested | Commit history (single entry per revision) |
| `POST /api/models/{org}/{repo}/paths-info/{rev}` | ✅ implemented | Batch file metadata |

## The resolve route contract (the critical one)

`GET /{repo_id}/resolve/{rev}/{filename}` must return these for `huggingface_hub`
to accept the file (verified against `huggingface_hub` source —
`file_download.py`, `_get_metadata_or_catch_error`):

| Header | Required | Source |
|---|---|---|
| `X-Repo-Commit` | **yes** — HF raises `FileMetadataError` without it | manifest commit |
| `ETag` (or `X-Linked-Etag`) | **yes** — used as the cache key | upstream HF etag (lfs.sha256 / git blob_id) |
| `Content-Length` (or `X-Linked-Size`) | **yes** | file size |
| `Accept-Ranges: bytes` | for resumability | set |
| `Content-Range` | on `206` Range responses | computed |

Behavior:
- `GET` → `200` + streamed bytes (full file) or `206` + ranged bytes (with `Range`).
- `HEAD` → same headers, no body (this is what `get_hf_file_metadata` issues first).
- `Range` requests honored with `206` + `Content-Range` for resumable downloads.
- No redirect is required — `huggingface_hub` accepts a direct `200` body. The
  `Location` header is optional; if absent, the request URL is used as the
  download location.

## Adapter capability matrix

```
HF:         resolve ✓  tree ✓  refs ✓  commits ✓  upload ✗  search ✗
Civitai:    (planned)  resolve ✓  tree ✓  refs ✗  commits ✗  upload ✗  search ✗
Filesystem: (planned)  resolve ✓  tree ✓  refs ✗  commits ✗  upload ✗  search ✗
OCI:        (future)   resolve ✓  tree ✓  refs ✓  commits ✓  upload ✓  search ✗
```

## Datasets

Everything above exists twice: `/api/datasets/{org}/{repo}/...` and `/datasets/{org}/{repo}/resolve/{rev}/{file}` serve dataset repositories, so
`hf download ... --repo-type dataset`, `snapshot_download(repo_type="dataset")`, `HfFileSystem` and `datasets.load_dataset("org/name")` work
(tested with the real clients in `tests/compat/test_datasets.py`). Legacy single-segment names (`squad`, `gpt2`) work for both types.
Storage: datasets live in `<root>/datasets/<org>/<name>` (models stay in `<root>/repos/<org>/<name>`); inside the core a dataset's id is
`datasets/<org>/<name>` (`datasets/_user/squad` for a single-segment name); `repo list` prints that form. A MODEL organisation called
`datasets` cannot be served (the URL is a dataset); the Hub reserves the name too. Ingest: `repo ingest-hf <id> --type dataset`,
`repo ingest-local <id> <dir> --type dataset`.

## Cache headers

`resolve/<40-hex commit>/...` is `immutable` for a year; `resolve/main/...` (any branch or tag) is `no-cache`, because a ref moves when a new revision is published.

## Authentication

`MIRROR_AUTH_TOKEN`, when set, is required as `Authorization: Bearer <token>` on every Hub route (`/api/models`, `/api/datasets`, `/datasets/...`, `.../resolve/...`): clients set `HF_TOKEN`. `/healthz` stays open. (Until 2026-11-04 the variable was read and never checked.)

## Dates

`lastModified`, `createdAt` and commit dates are `YYYY-MM-DDTHH:MM:SS.mmmZ`: `huggingface_hub` parses only that shape.

## Intentionally omitted

- **Xet chunk protocol.** Avoided via `HF_HUB_DISABLE_XET=1`. If Xet ever
  becomes mandatory for public downloads, the adapter will need to grow.
- **Spaces** (`/api/spaces/...`, `/spaces/...`). Never: a Space is an app, not a stored artifact.
- **Write/upload endpoints** (`POST /api/models/...`, LFS upload). The mirror is
  read-only; ingest happens through the CLI on a trusted box.
- **Search** (`/api/models?search=...`). Out of scope; use `repo list` locally.
- **LM Studio's internal catalog.** Not an HF API; LM Studio consumes the mirror
  via local-file import (see CLIENT_SETUP.md).

## Engine coverage (validated in `tests/engines/test_matrix.py`)

| Engine | Consumption | Status |
|---|---|---|
| transformers / diffusers | `HF_ENDPOINT` via huggingface_hub | ✅ tested (real hf client) |
| vLLM | `HF_ENDPOINT` + `--model` | ✅ covered by hf_hub test |
| TGI | `HF_ENDPOINT` | ✅ covered by hf_hub test |
| MLX (`mlx_lm`) | `HF_ENDPOINT` | ✅ covered by hf_hub test |
| `huggingface_hub` CLI | `HF_ENDPOINT` | ✅ tested (real hf client) |
| Ollama | `hf.co/<repo>:<file>` + `HF_ENDPOINT` | ✅ tested (resolve flow) |
| llama.cpp | `--model-url <resolve URL>` | ✅ tested (direct stream + Range) |
| LM Studio | local-file import | ✅ documented exception |
