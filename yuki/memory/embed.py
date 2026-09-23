"""Local text embeddings for the journal (no cloud).

Model: ``sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`` (fastembed's
quantised ONNX build, 384 dimensions, ~50 languages). Chosen over
``BAAI/bge-small-en-v1.5`` because the user reads and watches Japanese content
and may ask in Japanese: the model is trained so a sentence and its translation
land close together, so a Japanese query finds an English fact about the same
thing. Measured on this PC (Ryzen 7 2700, 2026-09-23), on 5 facts x 5 mixed
English/Japanese queries:

=========================================  =====  ======  ==========  =========  =======
model                                      disk   load    1 text      batch/txt  correct
=========================================  =====  ======  ==========  =========  =======
paraphrase-multilingual-MiniLM-L12-v2 (Q)  267MB  1.2 s   ~6 ms       ~14 ms     4/5
BAAI/bge-small-en-v1.5 (Q)                 67MB   2.0 s   ~6-90 ms    ~80 ms     1/5 (fails on Japanese)
minishlab/potion-multilingual-128M         547MB  2.3 s   0.2 ms      0.2 ms     4/5, weaker margins
=========================================  =====  ======  ==========  =========  =======

(the one "miss" was an ambiguous query that also matched a music stream).

The model files are downloaded once into ``%LOCALAPPDATA%\\Yuki\\memory\\models``
with ``huggingface_hub.snapshot_download(local_dir=...)``: the default
fastembed cache relies on symlinks, which a non-admin Windows account without
Developer Mode cannot create.

Vectors are float32 and L2-normalised, so cosine similarity is a dot product.

Public API::

    MODEL_NAME, DIM
    Embedder(model_name=MODEL_NAME, models_dir=None)
        .embed(texts: Sequence[str]) -> np.ndarray       # (n, DIM) float32, normalised
        .embed_one(text: str) -> np.ndarray              # (DIM,)
        .stats() -> dict                                 # load_ms, model_bytes, calls, avg_ms
    get_embedder() -> Embedder                           # process-wide lazy singleton
    cosine_top_k(query, matrix, k) -> list[tuple[int, float]]
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any
from collections.abc import Sequence

import numpy as np

from yuki.memory.store import default_memory_dir

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DIM = 384


def _dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


class Embedder:
    """Lazily loaded fastembed model; safe to share between threads."""

    def __init__(self, model_name: str = MODEL_NAME, models_dir: Path | None = None) -> None:
        self.model_name = model_name
        self.models_dir = Path(models_dir) if models_dir else default_memory_dir() / "models"
        self._model: Any = None
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self.load_ms: float | None = None
        self.download_ms: float | None = None
        self.model_bytes: int | None = None
        self.calls = 0
        self.texts = 0
        self.total_ms = 0.0

    @property
    def model_dir(self) -> Path:
        return self.models_dir / self.model_name.replace("/", "__")

    def _ensure_loaded(self) -> Any:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            from fastembed import TextEmbedding

            desc = next(
                (m for m in TextEmbedding.list_supported_models() if m["model"] == self.model_name), None
            )
            if desc is None:
                raise ValueError(f"fastembed does not know {self.model_name!r}")
            target = self.model_dir
            model_file = target / desc["model_file"]
            if not model_file.exists():
                from huggingface_hub import snapshot_download

                t0 = time.perf_counter()
                snapshot_download(desc["sources"]["hf"], local_dir=str(target))
                self.download_ms = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            self._model = TextEmbedding(self.model_name, specific_model_path=str(target))
            self.load_ms = (time.perf_counter() - t0) * 1000
            self.model_bytes = _dir_bytes(target)
            return self._model

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Embed ``texts`` -> ``(n, DIM)`` float32 L2-normalised."""
        items = [t or "" for t in texts]
        if not items:
            return np.zeros((0, DIM), dtype=np.float32)
        model = self._ensure_loaded()
        t0 = time.perf_counter()
        with self._run_lock:
            vecs = np.asarray(list(model.embed(items)), dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        vecs = vecs / norms
        self.calls += 1
        self.texts += len(items)
        self.total_ms += (time.perf_counter() - t0) * 1000
        return vecs

    def embed_one(self, text: str) -> np.ndarray:
        """Embed one text -> ``(DIM,)``."""
        return self.embed([text])[0]

    def stats(self) -> dict[str, Any]:
        """Load time, size on disk and per-text timing so far."""
        return {
            "model": self.model_name,
            "loaded": self._model is not None,
            "download_ms": self.download_ms,
            "load_ms": self.load_ms,
            "model_bytes": self.model_bytes,
            "calls": self.calls,
            "texts": self.texts,
            "avg_ms_per_text": (self.total_ms / self.texts) if self.texts else None,
        }


_singleton: Embedder | None = None
_singleton_lock = threading.Lock()


def get_embedder() -> Embedder:
    """The process-wide embedder (the model loads on first use, not here)."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Embedder()
        return _singleton


def cosine_top_k(query: np.ndarray, matrix: np.ndarray, k: int) -> list[tuple[int, float]]:
    """Indices and cosine scores of the ``k`` rows of ``matrix`` closest to ``query``."""
    if matrix.size == 0 or k <= 0:
        return []
    q = np.asarray(query, dtype=np.float32).ravel()
    q = q / (np.linalg.norm(q) or 1.0)
    m = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1)
    norms[norms == 0] = 1.0
    scores = (m @ q) / norms
    order = np.argsort(-scores)[:k]
    return [(int(i), float(scores[i])) for i in order]
