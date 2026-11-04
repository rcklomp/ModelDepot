"""What the Download page can see upstream (huggingface.co, civitai.com): search, and a repo's files grouped into choices.

Network calls are isolated here (and in the runner); everything that decides how files are GROUPED is a pure function over
(path, size) pairs, so it is tested without a network.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

HF_BASE = os.environ.get("MIRROR_UPSTREAM_HF", "https://huggingface.co").rstrip("/")
CIVITAI_BASE = os.environ.get("MIRROR_UPSTREAM_CIVITAI", "https://civitai.com").rstrip("/")

WEIGHT_EXTS = (".gguf", ".safetensors", ".bin", ".pt", ".pth", ".ckpt", ".onnx", ".h5", ".msgpack", ".npz", ".tflite", ".ot", ".pkl")
SUPPORT_EXTS = (".json", ".md", ".txt", ".model", ".jinja", ".tiktoken", ".yaml", ".yml", ".vocab", ".py")
_SHARD = re.compile(r"^(.+)-(\d{5})-of-(\d{5})(\.[^./]+)$")
_QUANT = re.compile(r"[-._]((?:UD-)?(?:I?Q\d[A-Z0-9_]*|F16|BF16|F32))(?:-\d{5}-of-\d{5})?\.gguf$", re.I)


class UpstreamError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def escape_glob(path: str) -> str:
    """allow_patterns are fnmatch globs: make an exact path safe against [ ] * ?"""
    return re.sub(r"([*?\[])", r"[\1]", path)


def patterns_for(paths: list[str]) -> list[str]:
    return [escape_glob(p) for p in paths]


def match_files(entries: list[dict], patterns: list[str] | None) -> list[dict]:
    if not patterns:
        return entries
    return [e for e in entries if any(fnmatch.fnmatch(e["path"], p) for p in patterns)]


# ---------------------------------------------------------------- grouping (pure)

def _size(files: list[dict]) -> int:
    return sum(f["size"] for f in files)


def _opt(oid: str, label: str, files: list[dict], note: str = "", **extra) -> dict:
    return {"id": oid, "label": label, "size": _size(files), "file_count": len(files),
            "files": [f["path"] for f in files], "note": note, **extra}


def describe_tree(kind: str, entries: list[dict]) -> dict:
    """Group a repo's files into the choices the picker shows.

    entries: [{"path": "UD-Q4/model-00001-of-00002.gguf", "size": 123}, ...] (files only).
    Returns {groups, quick, extras, files}. GGUF quants are one choice per (folder, quant); a split quant is ONE choice and is
    marked incomplete when a shard is missing (the UI refuses it: a partial shard set only fails later, inside the engine).
    """
    entries = sorted(({"path": e["path"], "size": int(e.get("size") or 0)} for e in entries), key=lambda e: e["path"])
    ext = lambda p: ("." + p.rsplit(".", 1)[-1].lower()) if "." in p.rsplit("/", 1)[-1] else ""
    base = lambda p: p.rsplit("/", 1)[-1]

    extras = [e for e in entries if ext(e["path"]) in SUPPORT_EXTS and not base(e["path"]).lower().startswith("mmproj")
              and e["size"] <= 20_000_000 and ext(e["path"]) not in WEIGHT_EXTS]
    extra_paths = {e["path"] for e in extras}
    groups: list[dict] = []

    ggufs = [e for e in entries if ext(e["path"]) == ".gguf" and not base(e["path"]).lower().startswith("mmproj")]
    mmproj = [e for e in entries if ext(e["path"]) == ".gguf" and base(e["path"]).lower().startswith("mmproj")]
    if ggufs:
        sets: dict[tuple, dict] = {}
        for e in ggufs:
            d = e["path"].rsplit("/", 1)[0] if "/" in e["path"] else ""
            m = _SHARD.match(base(e["path"]))
            key = (d, m.group(1) if m else base(e["path"]))
            s = sets.setdefault(key, {"files": [], "total": int(m.group(3)) if m else 1, "idx": set()})
            s["files"].append(e)
            s["idx"].add(int(m.group(2)) if m else 1)
        opts = []
        for (d, prefix), s in sets.items():
            q = _QUANT.search(base(s["files"][0]["path"]))
            label = q.group(1).upper() if q else (d.rsplit("/", 1)[-1] if d else prefix.rsplit(".", 1)[0])
            complete = s["idx"] == set(range(1, s["total"] + 1))
            note = (f"All {s['total']} parts included" if complete and s["total"] > 1 else "Single file" if s["total"] == 1
                    else f"Incomplete: {len(s['idx'])} of {s['total']} parts are on the upstream repo")
            opts.append(_opt(f"gguf:{d}/{prefix}", label, s["files"], note, shards=s["total"], complete=complete,
                             folder=d, name=prefix))
        opts.sort(key=lambda o: (o["size"], o["label"]))
        groups.append({"key": "gguf", "title": "GGUF quantizations", "exclusive": True, "options": opts})
    if mmproj:
        groups.append({"key": "mmproj", "title": "Vision projector (optional add-on)", "exclusive": False,
                       "options": [_opt(f"mmproj:{e['path']}", base(e["path"]), [e], "Needed only for image input") for e in mmproj]})

    if kind == "model" and not ggufs:
        by_ext: dict[str, list[dict]] = {}
        for e in entries:
            if ext(e["path"]) in WEIGHT_EXTS and e["path"] not in extra_paths:
                by_ext.setdefault(ext(e["path"]), []).append(e)
        opts = []
        for x, fs in sorted(by_ext.items(), key=lambda kv: -_size(kv[1])):
            # index files (model.safetensors.index.json) are small support files and ride along in `extras`
            opts.append(_opt(f"ext:{x}", f"{x[1:]} weights", fs, f"{len(fs)} file(s)"))
        if opts:
            groups.append({"key": "weights", "title": "Weights", "exclusive": True, "options": opts})
    if kind == "dataset":
        tops: dict[str, list[dict]] = {}
        for e in entries:
            if e["path"] in extra_paths and "/" not in e["path"]:
                continue
            top = e["path"].split("/", 1)[0] if "/" in e["path"] else ""
            tops.setdefault(top, []).append(e)
        opts = [_opt(f"dir:{t}", (t + "/") if t else "Top-level files", fs, f"{len(fs)} file(s)") for t, fs in
                sorted(tops.items(), key=lambda kv: -_size(kv[1]))]
        if len(opts) > 1:
            groups.append({"key": "folders", "title": "Folders", "exclusive": False, "options": opts})

    quick = [_opt("all", "Everything", entries, "Every file in the repository"),
             _opt("config_only", "Config & tokenizer only", extras,
                  "Not a complete model: the small files, no weights")]
    return {"groups": groups, "quick": quick, "extras": _opt("extras", "README, config and tokenizer files", extras),
            "files": entries, "total_size": _size(entries), "file_count": len(entries)}


# ---------------------------------------------------------------- Hugging Face

def _hf_api(token: str):
    from huggingface_hub import HfApi
    return HfApi(endpoint=HF_BASE, token=token or None)


def _wrap(exc: Exception) -> UpstreamError:
    name = type(exc).__name__
    status = getattr(getattr(exc, "response", None), "status_code", None) or 502
    if "Gated" in name:
        return UpstreamError("This repository is gated: it needs a Hugging Face token whose account has been approved (Settings).", 403)
    if "NotFound" in name:
        return UpstreamError("Not found on Hugging Face (a private repository needs a token).", 404)
    return UpstreamError(f"Hugging Face: {exc}", status if isinstance(status, int) else 502)


def hf_search(*, kind: str, q: str = "", sort: str = "downloads", fmt: str = "", limit: int = 30, token: str = "") -> list[dict]:
    api = _hf_api(token)
    sort = sort if sort in ("downloads", "likes", "last_modified", "created_at", "trending_score") else "downloads"
    try:
        if kind == "dataset":
            it = api.list_datasets(search=q or None, sort=sort, limit=limit)
        else:
            it = api.list_models(search=q or None, filter=fmt or None, sort=sort, limit=limit)
        out = []
        for m in it:
            tags = list(getattr(m, "tags", None) or [])
            out.append({"source": "hf", "type": kind, "id": m.id, "author": m.id.split("/")[0] if "/" in m.id else "",
                        "downloads": getattr(m, "downloads", 0) or 0, "likes": getattr(m, "likes", 0) or 0,
                        "updated": (getattr(m, "last_modified", None) or getattr(m, "lastModified", None)).isoformat()
                        if getattr(m, "last_modified", None) else None,
                        "gated": bool(getattr(m, "gated", False)),
                        "formats": [t for t in tags if t in ("gguf", "safetensors", "mlx", "onnx", "pytorch")],
                        "task": getattr(m, "pipeline_tag", None), "url": f"{HF_BASE}/{'datasets/' if kind == 'dataset' else ''}{m.id}"})
            if len(out) >= limit:
                break
        return out
    except Exception as exc:                         # noqa: BLE001 - every upstream failure becomes one clean message
        raise _wrap(exc) from exc


def hf_describe(*, kind: str, repo_id: str, revision: str = "main", token: str = "") -> dict:
    api = _hf_api(token)
    try:
        info = api.repo_info(repo_id, revision=revision, repo_type=kind, token=token or None)
        entries = [{"path": f.path, "size": getattr(f, "size", 0) or 0}
                   for f in api.list_repo_tree(repo_id, recursive=True, revision=info.sha, repo_type=kind, token=token or None)
                   if hasattr(f, "size")]
    except Exception as exc:                         # noqa: BLE001
        raise _wrap(exc) from exc
    card = getattr(info, "card_data", None)
    lic = (getattr(card, "license", None) if card else None) or None
    return {"source": "hf", "type": kind, "id": repo_id, "revision": revision, "commit": info.sha,
            "gated": bool(getattr(info, "gated", False)), "license": lic if isinstance(lic, str) else None,
            **describe_tree(kind, entries)}


# ---------------------------------------------------------------- Civitai

def _civitai_get(path: str, params: dict | None, token: str):
    url = CIVITAI_BASE + path + (("?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})) if params else "")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "llm-repo/1.0",
                                               **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise UpstreamError(f"Civitai answered HTTP {e.code}", e.code if e.code in (401, 403, 404) else 502) from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise UpstreamError(f"Civitai is not reachable ({e})") from e


def civitai_search(*, q: str = "", model_type: str = "", sort: str = "Highest Rated", nsfw: bool = False, limit: int = 30,
                   token: str = "") -> list[dict]:
    body = _civitai_get("/api/v1/models", {"query": q, "types": model_type, "sort": sort, "nsfw": "true" if nsfw else "false",
                                           "limit": min(limit, 100)}, token)
    out = []
    for item in (body.get("items") or []) if isinstance(body, dict) else []:
        versions = item.get("modelVersions") or []
        v = versions[0] if versions else {}
        out.append({"source": "civitai", "type": "model", "id": str(v.get("id") or item.get("id")), "name": item.get("name", ""),
                    "author": (item.get("creator") or {}).get("username", ""), "civitai_type": item.get("type", ""),
                    "downloads": (item.get("stats") or {}).get("downloadCount", 0) or 0,
                    "likes": (item.get("stats") or {}).get("thumbsUpCount", 0) or 0, "nsfw": bool(item.get("nsfw")),
                    "version": v.get("name", ""), "base_model": v.get("baseModel", ""),
                    "size": sum(int((f.get("sizeKB") or 0) * 1024) for f in v.get("files") or []),
                    "url": f"{CIVITAI_BASE}/models/{item.get('id')}"})
    return out


def civitai_describe(*, version_id: int, token: str = "") -> dict:
    body = _civitai_get(f"/api/v1/model-versions/{int(version_id)}", None, token)
    files = [{"path": f.get("name", ""), "size": int((f.get("sizeKB") or 0) * 1024), "type": f.get("type", ""),
              "format": (f.get("metadata") or {}).get("format", "")} for f in body.get("files") or []]
    opts = [_opt(f"file:{f['path']}", f["path"], [f], f"{f['type']} {f['format']}".strip(), file_type=f["type"], file_format=f["format"])
            for f in files]
    return {"source": "civitai", "type": "model", "id": str(version_id), "revision": str(version_id), "commit": str(version_id),
            "gated": False, "license": None, "name": (body.get("model") or {}).get("name", ""),
            "groups": [{"key": "files", "title": "Files", "exclusive": False, "options": opts}] if len(opts) > 1 else [],
            "quick": [_opt("all", "All files of this version", [{"path": f["path"], "size": f["size"]} for f in files])],
            "extras": _opt("extras", "", []), "files": [{"path": f["path"], "size": f["size"]} for f in files],
            "total_size": sum(f["size"] for f in files), "file_count": len(files), "needs_token": True}
