"""Normalized model shape shared across HF and Civitai search results.

The UI is source-agnostic: it renders one ModelResult type regardless of where
the model came from. Each source adapter (hf.py, civitai.py) converts its raw
upstream response into this shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class QuantOption:
    """A selectable file or quant group within a model (pre-download choice)."""

    label: str            # e.g. "Q4_K_M" or "model.safetensors"
    path: str             # repo-relative path or civitai file name
    size_bytes: int
    shard_count: int = 1  # >1 if this is a sharded quant (sum of shards)


@dataclass
class ModelResult:
    source: str                  # "hf" | "civitai"
    id: str                      # repo_id (hf) or "<model_id>/<version_id>" (civitai)
    name: str
    author: str
    type: str                    # "gguf" | "mlx" | "safetensors" | "checkpoint" | ...
    downloads: int = 0
    likes: int = 0
    description: str = ""
    url: str = ""                # upstream page URL
    nsfw: bool = False
    quant_options: list[QuantOption] = field(default_factory=list)
    total_size_bytes: int = 0    # sum of all files (best-effort)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "id": self.id,
            "name": self.name,
            "author": self.author,
            "type": self.type,
            "downloads": self.downloads,
            "likes": self.likes,
            "description": self.description[:500],
            "url": self.url,
            "nsfw": self.nsfw,
            "quant_options": [
                {"label": q.label, "path": q.path, "size_bytes": q.size_bytes,
                 "shard_count": q.shard_count}
                for q in self.quant_options
            ],
            "total_size_bytes": self.total_size_bytes,
        }


@dataclass
class SearchPage:
    results: list[ModelResult]
    next_cursor: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [r.to_dict() for r in self.results],
            "next_cursor": self.next_cursor,
        }
