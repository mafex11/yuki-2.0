"""Yuki's memory (docs/MEMORY.md): encrypted store, journal worker, local embeddings.

Import the pieces from their modules; this package imports nothing itself, so
``import yuki.memory.secret_filter`` or ``yuki.memory.acting`` (the agent's
logger and marker) stays cheap - no store, numpy, fastembed or Bedrock client.
The store's names below resolve on first use:

    from yuki.memory.store import Store          # the database (watcher + readers)
    from yuki.memory.journal import JournalWorker
    from yuki.memory.embed import get_embedder
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "JournalEntry",
    "PendingCapture",
    "Store",
    "ThreadInfo",
    "default_db_path",
    "default_memory_dir",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from yuki.memory import store

        return getattr(store, name)
    raise AttributeError(f"module 'yuki.memory' has no attribute {name!r}")
