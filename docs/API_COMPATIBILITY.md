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

All GET: this is what a client needs to download. The write routes (publishing your own repos) are under [Uploading](#uploading).

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
HF:         resolve ✓  tree ✓  refs ✓  commits ✓  upload ✓  search ✗
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
The admin token passes the same gate (it is the stronger credential, and `huggingface_hub` sends one token only: whoever publishes needs to get through). The read token never writes.

## Dates

`lastModified`, `createdAt` and commit dates are `YYYY-MM-DDTHH:MM:SS.mmmZ`: `huggingface_hub` parses only that shape.

## Intentionally omitted

- **Xet chunk protocol.** Avoided via `HF_HUB_DISABLE_XET=1`. If Xet ever
  becomes mandatory for public downloads, the adapter will need to grow.
- **Spaces** (`/api/spaces/...`, `/spaces/...`). Never: a Space is an app, not a stored artifact.
- **Pull requests, other branches, `copyFile`, resumable/multipart LFS, Xet upload, private repos, Spaces**: see [Uploading](#uploading).
- **Search** (`/api/models?search=...`). Out of scope; use `repo list` locally.
- **LM Studio's internal catalog.** Not an HF API; LM Studio consumes the mirror
  via local-file import (see CLIENT_SETUP.md).

## Uploading

The Hub's write protocol, so people can publish their own models and datasets with the REAL client: `HfApi.create_repo`, `upload_file`,
`upload_folder` (with `delete_patterns`), `delete_file`, `delete_folder`, `whoami`, `repo_exists`, and the `hf upload` CLI, for
`repo_type="model"` and `"dataset"`. Tested against a live server with the real `huggingface_hub` in `tests/compat/test_upload.py`.
The code is `server/adapters/hf_upload.py` (routes) and `server/hub/upload.py` (the commit logic, shared with the GUI's web upload).

Client side: `HF_ENDPOINT=http://<mirror>:<port>`, `HF_HUB_DISABLE_XET=1` (Xet upload is not implemented; without the variable a client that
has `hf_xet` installed asks for a Xet write token and fails with `Network error ... 404 ... /xet-write-token/main`), and `HF_TOKEN=<the ADMIN token>` (`<store>/config/admin-token` or `MIRROR_ADMIN_TOKEN`).

| Endpoint | What it does |
|---|---|
| `GET /api/whoami-v2` | `{type:"user", id:"admin", name:<MIRROR_NAMESPACE, default "local">, fullname, auth:{type:"access_token", accessToken:{role:"write"}}}` for the admin token; 401 for anything else (the read token included) |
| `POST /api/repos/create` `{type, name, organization?}` | creates an EMPTY repository (no revision yet). `organization` defaults to the namespace. 409 if it exists (the body carries `url`, which is what `exist_ok=True` reads), 400 for `space`, for a private repo and for a bad name |
| `POST /api/validate-yaml` | the client checks a README.md's front matter here before it uploads one; always `{errors:[], warnings:[]}` |
| `POST /api/{models\|datasets}/{org}/{repo}/preupload/main` | per file `uploadMode`: `"lfs"` when the file is bigger than 1 MiB or ends in `.safetensors .gguf .bin .pt .pth .ckpt .onnx .parquet .arrow .h5 .npz .zip .tar .gz .zst`, else `"regular"`; `shouldIgnore:false`; `oid` = the id of the file already at that path (sha256 for LFS, git blob sha1 for regular), so the client skips unchanged files; `commitOid` = the head or `null` |
| `POST [/datasets]/{org}/{repo}.git/info/lfs/objects/batch` | git-LFS batch (`application/vnd.git-lfs+json`), `transfer:"basic"`: per object `actions.upload` and `actions.verify` with absolute URLs on THIS server (scheme and host from the request, or `X-Forwarded-Proto/Host`); no `actions` when the blob is already stored |
| `PUT /_lfs/{oid}?size=&exp=&sig=` | the file's bytes, streamed to `<store>/staging/uploads/<oid>.part`, size checked, renamed to `<oid>`. One PUT per file |
| `POST /_lfs/{oid}/verify?size=&exp=&sig=` | hashes the staged file once; 422 (and the bytes are deleted) when it is not the announced sha256 |
| `POST /api/{models\|datasets}/{org}/{repo}/commit/main` | NDJSON commit: `header`, `file` (base64 or utf-8), `lfsFile`, `deletedFile`, `deletedFolder`. Answers `{success, commitOid, commitUrl, hookOutput}` |

How a commit becomes a revision: the new revision is the files of the current head with the commit's changes applied, in order; regular files
are hashed and stored, LFS files are moved from staging into the blob store (they were hashed at verify; one not verified is hashed once at
commit); the manifest has `source.kind = "upload"` and each file's etag is the sha256 (LFS) or the git blob sha1 (regular), as on the Hub; the
commit id is a sha1 over parent, summary, the (path, sha256) list and a timestamp; the revision is published and `main` moved. All of that runs
under the store lock (`ingest_lock`, see ARCHITECTURE.md), so a garbage collection cannot delete the blobs between "stored" and "published";
a GC that is running makes the commit wait. Commits to one repository are serialised, so two concurrent uploads never both start from the same head.
A commit that changes nothing is a success that returns the current head and publishes no new revision. `parentCommit` that is not the head is 409.
The commit summary and description are not part of the manifest (it has no field for them); they are in `events.jsonl` (`UploadCommitted`).

The signed URLs: the client does not send its Authorization header to the LFS PUT (the Hub's real URL points at S3), so each URL carries an
HMAC over (purpose, oid, size, expiry) keyed with the admin token, valid for one hour. `/_lfs/...` is not behind the read gate, so it works
with or without `MIRROR_AUTH_TOKEN`; the admin token in a header opens it too. Staged data nobody committed is swept after 2 days
(web-upload sessions after 7).

Every write route needs the admin token: missing or wrong gives 401 `{"error": ...}` (with `X-Error-Message`); `MIRROR_READ_ONLY=1` gives 403
on all of them (checked first). `MIRROR_AUTH_TOKEN` (the read token) can read, and never writes.

**Not supported** (answered with a clear 400, not silently ignored): branches other than `main` and pull requests (`revision=...`, `create_pr=True`),
`copyFile` (upload the file again), private repositories (`private=True`: the whole mirror is private when `MIRROR_AUTH_TOKEN` is set), Spaces,
the `kernel` repo type, resumable or multipart LFS (one PUT per file, so a failed 50 GB upload starts over), Xet (`HF_HUB_DISABLE_XET=1`),
`gitIgnore` handling (every file is accepted), deleting a repository through the Hub API (use the GUI), `upload_large_folder`.
A new repository's name follows the Hub's shape (letters, digits, `-_.`, at most 96, no `--` or `..`; the namespaces `api datasets spaces models` are reserved).
A repository that was created and never uploaded to exists (`repo_exists` is true, `model_info().sha` is `null`, its file list is empty) but the GUI library shows repositories with content only.

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
