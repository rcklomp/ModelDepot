"""Versioned, deterministic manifests.

A manifest describes one revision of a repository. It is written atomically
(temp file + rename). Once published, an entry never changes or goes away; the only
later change is that files of the same commit that arrive afterwards are added
(RepositoryCore.publish_or_extend). The on-disk form is JSON with schema
versioning so it can evolve.

Design rules enforced here:
  - schema_version is always present (currently 1).
  - files[] are sorted by path before serialisation (deterministic output —
    friendly to git, rsync, diff, and debugging).
  - adapter-specific metadata is namespaced under `metadata` (e.g. metadata.hf)
    rather than living at the top level, so adding adapters never reshapes the
    core schema.
  - SHA-256 is the canonical identity (blob ref + checksums.sha256). ETag and
    other source-specific fields are compatibility metadata, not identity.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class License:
    name: str
    url: str | None = None


@dataclass(frozen=True)
class Source:
    kind: str                 # 'huggingface' | 'civitai' | 'ollama' | 'url'
    ref: str                  # original URL / identifier


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    blob: str                 # 'sha256:<digest>'
    checksums: dict[str, str] = field(default_factory=dict)  # {sha256: <hex>}
    metadata: dict[str, Any] = field(default_factory=dict)   # namespaced: {hf: {...}}


@dataclass(frozen=True)
class RevisionInfo:
    commit: str
    fetched_at: str           # ISO-8601 UTC
    license: License | None = None


@dataclass
class Manifest:
    schema_version: int
    repo_id: str
    source: Source
    revision: RevisionInfo
    files: list[FileEntry]

    def to_dict(self) -> dict[str, Any]:
        files = sorted((asdict(f) for f in self.files), key=lambda f: f["path"])
        return {
            "schema_version": self.schema_version,
            "repo_id": self.repo_id,
            "source": asdict(self.source),
            "revision": asdict(self.revision),
            "files": files,
        }


def dump_json(manifest: Manifest) -> str:
    """Serialise a manifest deterministically (sorted keys, sorted files)."""
    return json.dumps(manifest.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)


def write_atomic(manifest: Manifest, dest: Path) -> None:
    """Write a manifest to `dest` via a temp file + atomic rename."""
    from .fsutil import atomic_write_text
    atomic_write_text(dest, dump_json(manifest) + "\n")


def read(path: Path) -> Manifest:
    """Read and validate a manifest from disk."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return from_dict(data)


def from_dict(data: dict[str, Any]) -> Manifest:
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ManifestError(
            f"unsupported schema_version: got {version!r}, expected {SCHEMA_VERSION}"
        )
    try:
        source = Source(**data["source"])
        rev_data = data["revision"]
        license_field = rev_data.get("license")
        revision = RevisionInfo(
            commit=rev_data["commit"],
            fetched_at=rev_data["fetched_at"],
            license=License(**license_field) if license_field else None,
        )
        files = [FileEntry(**f) for f in data.get("files", [])]
    except (KeyError, TypeError) as exc:
        raise ManifestError(f"malformed manifest: {exc}") from exc
    return Manifest(
        schema_version=version,
        repo_id=data["repo_id"],
        source=source,
        revision=revision,
        files=files,
    )


class ManifestError(Exception):
    pass
