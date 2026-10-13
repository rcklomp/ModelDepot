"""One list of the models on this machine: the mirror's and Ollama's together (2026-10-13).

The console used to show two lists that did not know about each other: "In Ollama now" (remove only) and the mirror
catalog (load only, matched to Ollama by a name guess that never matched). Here every model appears ONCE:

* a mirror GGUF (one file, or one shard set) -- with the Ollama models made from it, if any;
* an Ollama model that came from elsewhere (`ollama pull`) -- on its own row;
* a mirror repo without GGUF (safetensors) -- listed, with why Ollama cannot use it.

Linking an Ollama model to its mirror file: Ollama re-packs the GGUF on import, so the digests differ, but the model
layer is the same bytes plus Ollama's own small layers (template, parameters): an Ollama model is the mirror file
when its size exceeds the file's by 0..LINK_SLACK bytes (measured 2026-10-13: +377 B and +583 B); of several such
files, the closest. Vision projector
files (`mmproj-*`) are not models and are never offered.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any

from . import catalog

LINK_SLACK = 64 * 1024
OLLAMA_URL = os.environ.get("CONSOLE_OLLAMA_URL", "http://127.0.0.1:11434")
_SHARD = re.compile(r"^(.+)-(\d{5})-of-(\d{5})\.gguf$", re.I)
_QUANT = re.compile(r"[-.]((?:UD-)?(?:I?Q\d[A-Z0-9_]*|F16|BF16|F32))(?:-\d{5}-of-\d{5})?\.gguf$", re.I)


def ollama_models() -> tuple[list[dict[str, Any]], str | None]:
    """Ollama's models with byte sizes, or ([], why) when Ollama does not answer."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as r:
            data = json.load(r)
    except Exception as e:  # noqa: BLE001 -- any failure means "Ollama not reachable"
        return [], f"Ollama does not answer at {OLLAMA_URL} ({type(e).__name__})"
    out = []
    for m in data.get("models", []):
        d = m.get("details") or {}
        out.append({"name": m.get("name") or m.get("model"), "size": int(m.get("size") or 0),
                    "quant": d.get("quantization_level"), "params": d.get("parameter_size")})
    return out, None


def gguf_targets(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The loadable GGUFs of one repo: single files and whole shard sets; mmproj files left out."""
    singles, shards = [], {}
    for f in files:
        name = f["path"].rsplit("/", 1)[-1]
        if not name.lower().endswith(".gguf") or name.lower().startswith("mmproj"):
            continue
        m = _SHARD.match(f["path"])
        if m:
            shards.setdefault(m.group(1), {"total": int(m.group(3)), "files": []})["files"].append(f)
        else:
            singles.append({"file": f["path"], "label": name, "size": f["size"], "shards": 1, "complete": True})
    for prefix, g in shards.items():
        fs = sorted(g["files"], key=lambda x: x["path"])
        singles.append({"file": fs[0]["path"], "label": prefix.rsplit("/", 1)[-1], "size": sum(x["size"] for x in fs),
                        "shards": g["total"], "complete": len(fs) == g["total"]})
    for t in singles:
        q = _QUANT.search(t["file"])
        t["quant"] = q.group(1).upper() if q else None
    return singles


def build(repo) -> dict[str, Any]:
    ollama, ollama_error = ollama_models()
    rows, mirror_rows = [], []
    for item in catalog.list_catalog(repo):
        detail = catalog.model_detail(repo, item["repo_id"]) or {}
        revs = detail.get("revisions") or []
        files = revs[0]["files"] if revs else []
        targets = gguf_targets(files)
        if not targets:
            rows.append({"kind": "mirror", "repo_id": item["repo_id"], "label": item["repo_id"].split("/")[-1],
                         "file": None, "size": item["total_size_bytes"], "quant": None, "ollama": [],
                         "can_add": False, "note": f"{item['type']} files: Ollama runs GGUF only"})
            continue
        for t in targets:
            mirror_rows.append({"kind": "mirror", "repo_id": item["repo_id"], "label": t["label"], "file": t["file"],
                                "size": t["size"], "quant": t["quant"], "shards": t["shards"], "ollama": [],
                                "can_add": t["complete"],
                                "note": None if t["complete"] else "incomplete shard set in the mirror"})
    # each Ollama model goes to the ONE mirror file it is closest above (its own small layers on top), if any
    linked: set[str] = set()
    for o in ollama:
        fits = [r for r in mirror_rows if 0 <= o["size"] - r["size"] <= LINK_SLACK]
        if fits:
            min(fits, key=lambda r: o["size"] - r["size"])["ollama"].append(o["name"])
            linked.add(o["name"])
    rows += mirror_rows
    for o in ollama:
        if o["name"] not in linked:
            rows.append({"kind": "ollama", "repo_id": None, "label": o["name"], "file": None, "size": o["size"],
                         "quant": o["quant"], "ollama": [o["name"]], "can_add": False,
                         "note": "pulled into Ollama directly; not in the mirror"})
    rows.sort(key=lambda r: (r["label"] or "").lower())
    return {"rows": rows, "ollama_error": ollama_error}
