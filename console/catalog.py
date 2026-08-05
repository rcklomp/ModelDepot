"""Browse the local mirror (what's already ingested).

Unlike search (upstream, needs network), catalog reads manifests/refs from the
shared store directly — fully offline. Supports filtering by type/size/quant
and a text query over repo_id + file paths.
"""
from __future__ import annotations

import re
from typing import Any

from server import manifests
from server.repository import RepositoryCore


def list_catalog(repo: RepositoryCore, *, q: str = "", type_filter: str = "",
                 sort: str = "name") -> list[dict[str, Any]]:
    """Return a list of local repos with summary metadata for the UI."""
    items = []
    for repo_id in repo.list_repos():
        try:
            refs = repo.list_references(repo_id)
            revs = repo.list_revisions(repo_id)
            # Read the 'main' ref's manifest for type/size summary; fall back to
            # the latest revision.
            commit = refs.get("main") or (revs[0] if revs else "")
            if not commit:
                continue
            m = repo.get_manifest(repo_id, commit)
        except Exception:
            continue  # skip broken repos rather than 500 the whole catalog

        files = m.files
        type_ = _infer_type([f.path for f in files])
        total_size = sum(f.size for f in files)
        quants = _extract_quants([f.path for f in files], type_)

        items.append({
            "repo_id": repo_id,
            "name": repo_id.split("/")[-1],
            "author": repo_id.split("/")[0] if "/" in repo_id else "",
            "source": m.source.kind,
            "type": type_,
            "commit": commit,
            "revision_count": len(revs),
            "file_count": len(files),
            "total_size_bytes": total_size,
            "quants": quants,
            "license": (m.revision.license.name if m.revision.license else None),
            "fetched_at": m.revision.fetched_at,
        })

    # Filter
    if q:
        ql = q.lower()
        items = [i for i in items
                 if ql in i["repo_id"].lower() or any(ql in q.lower() for q in i["quants"])]
    if type_filter:
        items = [i for i in items if i["type"] == type_filter]

    # Sort
    sort_keys = {
        "name": lambda i: i["repo_id"].lower(),
        "size": lambda i: -i["total_size_bytes"],
        "date": lambda i: i.get("fetched_at") or "",
    }
    items.sort(key=sort_keys.get(sort, sort_keys["name"]))
    return items


def model_detail(repo: RepositoryCore, repo_id: str) -> dict[str, Any] | None:
    """Full detail for one local repo: revisions, refs, files with sizes + provenance."""
    try:
        refs = repo.list_references(repo_id)
        revs = repo.list_revisions(repo_id)
    except Exception:
        return None
    # A repo that doesn't exist has no refs AND no revisions.
    if not refs and not revs:
        return None

    revisions = []
    for commit in revs:
        try:
            m = repo.get_manifest(repo_id, commit)
        except Exception:
            continue
        revisions.append({
            "commit": commit,
            "fetched_at": m.revision.fetched_at,
            "license": (m.revision.license.name if m.revision.license else None),
            "files": [
                {
                    "path": f.path, "size": f.size,
                    "blob": f.blob,
                    "metadata": f.metadata,
                }
                for f in m.files
            ],
        })
    return {
        "repo_id": repo_id,
        "refs": refs,
        "revisions": revisions,
    }


def _infer_type(paths: list[str]) -> str:
    exts = {p.rsplit(".", 1)[-1].lower() for p in paths if "." in p}
    if "gguf" in exts:
        return "gguf"
    if "safetensors" in exts:
        return "safetensors"
    if "bin" in exts or "pt" in exts:
        return "pytorch"
    if "ckpt" in exts:
        return "checkpoint"
    return "model"


def _extract_quants(paths: list[str], type_: str) -> list[str]:
    """Pull quant labels from GGUF filenames (e.g. 'Q4_K_M', 'UD-IQ3_XXS')."""
    if type_ != "gguf":
        return []
    out = set()
    for p in paths:
        m = re.search(r"[-.]([A-Z0-9_]+)(?:-[0-9]{5}-of-[0-9]{5})?\.gguf$", p, re.I)
        if m:
            out.add(m.group(1))
    return sorted(out)
