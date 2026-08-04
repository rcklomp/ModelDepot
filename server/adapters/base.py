"""Protocol adapter base.

Adapters translate an external protocol (Hugging Face Hub HTTP, Civitai, Ollama,
OCI, ...) into calls on the Repository Core. They are intentionally thin: HTTP
parsing and response shaping live here; business logic lives in the core.

Each adapter declares a capability matrix rather than assuming uniform features,
so a caller (or the contract tests) can ask 'does this adapter support refs?'
without probing. Adapters are disposable: if Hugging Face changes its API next
year, only this directory changes — storage, manifests, refs, events are
untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Capabilities:
    """What a protocol adapter can serve. Booleans, not implementations."""

    resolve: bool = True    # serve a blob by path within a revision
    tree: bool = True       # list files in a revision
    refs: bool = False      # expose branch/tag references
    commits: bool = False   # expose commit history
    upload: bool = False    # accept uploads (v1 is read-only everywhere)
    search: bool = False    # search across repos


class ProtocolAdapter(Protocol):
    """A protocol adapter mounts routes onto a FastAPI app for its protocol.

    Concrete adapters (HfAdapter, and later CivitaiAdapter, etc.) implement
    `capabilities` and `mount(app)`. They hold a reference to the Repository
    Core; they do not touch storage directly.
    """

    @property
    def name(self) -> str: ...

    @property
    def capabilities(self) -> Capabilities: ...

    def mount(self, app) -> None:
        """Register this adapter's routes on the given FastAPI app."""
        ...
