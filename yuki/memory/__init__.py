"""Yuki's memory (docs/MEMORY.md): encrypted store, journal worker, local embeddings.

Import the pieces from their modules; this package deliberately imports only the
store, so ``import yuki.memory`` stays cheap (no fastembed/ONNX, no Bedrock client):

    from yuki.memory.store import Store          # the database (watcher + readers)
    from yuki.memory.journal import JournalWorker
    from yuki.memory.embed import get_embedder
"""

from yuki.memory.store import (
    JournalEntry,
    PendingCapture,
    Store,
    ThreadInfo,
    default_db_path,
    default_memory_dir,
)

__all__ = [
    "JournalEntry",
    "PendingCapture",
    "Store",
    "ThreadInfo",
    "default_db_path",
    "default_memory_dir",
]
