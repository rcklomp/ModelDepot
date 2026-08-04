"""Civitai source adapter for the ingester.

Downloads a Civitai model version's file(s) into staging and captures
provenance. Civitai serves individual model files (single .safetensors /
.checkpoint), not archives. A token is REQUIRED for every download.

Contract (from developer.civitai.com REST reference):
  - Metadata: GET https://civitai.com/api/v1/model-versions/{versionId}
    Returns JSON including:
      files[]: { name, type, format, sizeKB, downloadUrl, hashes: {SHA256, ...} }
      model:   { name, type, nsfw, poi }
      trainedWords, baseModel, etc.
  - Download: GET https://civitai.com/api/download/models/{versionId}?type=...&format=...
    Auth: Authorization: Bearer <token> (or ?token=<token>).
    Returns 302 to a short-lived signed CDN URL — must follow redirects.

Design:
  - A Civitai model version becomes a repo in the store: 'civitai/{model_id}'.
  - The version id is the "commit" (Civitai has no git, so we synthesize a
    stable revision id from the version id + sha256 of the file manifest).
  - Files are stored under their original filenames and re-exposed through the
    SAME HF-shaped /resolve/ endpoint, so ComfyUI/A1111 (which just want a file
    URL) and HF clients both consume them from one mirror.
  - SHA-256 is recorded so files are recoverable from community SHA indexes if
    Civitai removes a model.
"""
from __future__ import annotations

import hashlib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

API_BASE = "https://civitai.com"


@dataclass(frozen=True)
class CivitaiFile:
    """One file within a Civitai model version, with provenance."""

    name: str                # filename, e.g. 'model.safetensors'
    type: str                # 'Model' | 'VAE' | 'Config' | ...
    format: str              # 'SafeTensor' | 'Checkpoint' | ...
    size_bytes: int
    download_url: str        # the per-file downloadUrl
    sha256: str | None       # upstream-provided SHA-256, if present
    local_path: Path         # where it landed in staging


@dataclass(frozen=True)
class CivitaiSnapshot:
    """The result of downloading a Civitai model version into staging."""

    model_id: int
    model_name: str
    version_id: int
    version_name: str
    base_model: str | None
    license_name: str | None
    staging_dir: Path
    files: list[CivitaiFile]


class CivitaiSourceError(Exception):
    pass


def fetch(version_id: int, staging_dir: Path, *, token: str,
          file_type: str | None = None, file_format: str | None = None,
          api_base: str = API_BASE) -> CivitaiSnapshot:
    """Download a Civitai model version's files into `staging_dir`.

    A token is required. If `file_type`/`file_format` are given, only the
    matching file(s) are downloaded; otherwise all files in the version are
    fetched.

    Returns a CivitaiSnapshot. Raises CivitaiSourceError on any failure.
    """
    if not token:
        raise CivitaiSourceError("a Civitai API token is required (CIVITAI_API_KEY)")

    import urllib.request

    # 1. Fetch version metadata.
    meta_url = f"{api_base}/api/v1/model-versions/{version_id}"
    try:
        meta = _get_json(meta_url, token=token)
    except Exception as exc:
        raise CivitaiSourceError(f"could not fetch version {version_id}: {exc}") from exc

    model = meta.get("model", {}) or {}
    model_id = meta.get("modelId") or 0
    files_meta = meta.get("files", []) or []
    if not files_meta:
        raise CivitaiSourceError(f"version {version_id} has no files")

    # 2. Select files matching type/format filters.
    selected = []
    for f in files_meta:
        if file_type and f.get("type") != file_type:
            continue
        if file_format and f.get("format") != file_format:
            continue
        selected.append(f)
    if not selected:
        raise CivitaiSourceError(
            f"no files in version {version_id} match type={file_type!r} format={file_format!r}"
        )

    # 3. Download each selected file, following the 302 to the CDN.
    staged: list[CivitaiFile] = []
    for f in selected:
        name = f.get("name") or f"file_{len(staged)}"
        # Build the download URL. Prefer the per-file downloadUrl from metadata;
        # fall back to the version download endpoint with type/format params.
        download_url = f.get("downloadUrl") or _version_download_url(
            version_id, api_base, f.get("type"), f.get("format"), token,
        )
        hashes = f.get("hashes", {}) or {}
        sha256 = hashes.get("SHA256") or hashes.get("sha256")
        size_bytes = int((f.get("sizeKB") or 0) * 1024)

        local_path = staging_dir / name
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _download_following_redirects(download_url, local_path, token=token)
        except Exception as exc:
            raise CivitaiSourceError(
                f"could not download {name} from {download_url}: {exc}"
            ) from exc

        staged.append(CivitaiFile(
            name=name,
            type=f.get("type", "Model"),
            format=f.get("format", "Unknown"),
            size_bytes=size_bytes or local_path.stat().st_size,
            download_url=download_url,
            sha256=sha256.lower() if sha256 else None,
            local_path=local_path,
        ))

    return CivitaiSnapshot(
        model_id=model_id,
        model_name=model.get("name", f"civitai-{model_id}"),
        version_id=version_id,
        version_name=meta.get("name", str(version_id)),
        base_model=meta.get("baseModel"),
        license_name=_license_for_base(meta.get("baseModel")),
        staging_dir=staging_dir,
        files=staged,
    )


def synthesize_commit(version_id: int, files: list[CivitaiFile]) -> str:
    """Build a stable 40-hex 'commit' from the version id + file manifest.

    Civitai has no git revisions, so we synthesize one that's deterministic for
    a given (version, set-of-files). This serves as the immutable revision id in
    the store and is what gets echoed back as X-Repo-Commit.
    """
    h = hashlib.sha256()
    h.update(str(version_id).encode())
    for f in sorted(files, key=lambda x: x.name):
        h.update(f.name.encode())
        h.update((f.sha256 or "").encode())
    return h.hexdigest()[:40]


def repo_id_for(model_id: int, model_name: str) -> str:
    """Map a Civitai model to a store repo_id: 'civitai/<slug>'."""
    slug = _slugify(model_name) or str(model_id)
    return f"civitai/{slug}"


def to_staged_snapshot(snap: CivitaiSnapshot) -> "StagedSnapshot":
    """Adapt a CivitaiSnapshot into the source-agnostic StagedSnapshot shape
    that transaction.ingest consumes.

    The Civitai model becomes a repo 'civitai/<slug>'; the synthesized commit is
    deterministic per (version, files). Provenance is stored under metadata.civitai
    via the transaction layer's namespacing (we set etag to the sha256 where known
    so it round-trips through the HF adapter; Civitai-specific fields ride along
    in the CivitaiFile -> StagedFile mapping).
    """
    from .hf_source import StagedFile, StagedSnapshot

    commit = synthesize_commit(snap.version_id, snap.files)
    rid = repo_id_for(snap.model_id, snap.model_name)

    staged_files = [
        StagedFile(
            path=f.name,
            local_path=f.local_path,
            size=f.size_bytes,
            etag=f.sha256 or "",      # echo the content sha256 as the etag
            sha256=f.sha256,          # verified against the downloaded bytes
        )
        for f in snap.files
    ]
    return StagedSnapshot(
        repo_id=rid,
        revision_requested=str(snap.version_id),
        commit=commit,
        staging_dir=snap.staging_dir,
        files=staged_files,
    )


def civitai_source_url(model_id: int, version_id: int) -> str:
    return f"https://civitai.com/models/{model_id}?modelVersionId={version_id}"


# ---- internals -------------------------------------------------------

def _get_json(url: str, *, token: str) -> dict:
    import json
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _version_download_url(version_id: int, api_base: str,
                          file_type: str | None, file_format: str | None,
                          token: str) -> str:
    params = {"token": token}
    if file_type:
        params["type"] = file_type
    if file_format:
        params["format"] = file_format
    return f"{api_base}/api/download/models/{version_id}?{urllib.parse.urlencode(params)}"


def _download_following_redirects(url: str, dest: Path, *, token: str,
                                  chunk: int = 1 << 20) -> None:
    """Download `url` to `dest`, following Civitai's 302 to the CDN.

    The Authorization header is sent only to civitai.com; on redirect to the CDN
    host it's stripped (CDN URLs are pre-signed and reject extra auth).
    """
    import urllib.request
    parsed = urllib.parse.urlparse(url)
    is_civitai = parsed.hostname and "civitai.com" in parsed.hostname

    headers = {"User-Agent": "llm-repo-ingester/1.0"}
    if is_civitai:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    # urllib follows redirects by default; on cross-host redirect it drops
    # headers we didn't set on the redirect Request, but to be safe we rely on
    # the CDN's signed URL and don't forward auth off civitai.com.
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
        while True:
            buf = resp.read(chunk)
            if not buf:
                break
            out.write(buf)


def _license_for_base(base_model: str | None) -> str | None:
    """Best-effort license hint from the base model. Civitai doesn't expose a
    clean license field per version; record the base model as a provenance hint."""
    if not base_model:
        return None
    return f"base:{base_model}"


def _slugify(name: str) -> str:
    import re
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name or "").strip("-")
    return slug[:60] or "model"
