"""A small on-disk cache of Yuki's own voice for short lines she says often (docs/VOICE.md v3.14).

"On it.", "Mm-hm.", "Paused.", "Anytime.": the same few words, said again and again, each paying the voice's first
byte (~1.0-1.3 s for Gemini Leda) and, as a reply's first piece, the spoken-line check (~0.9 s). A cache, never a
router: it is keyed by the exact words (marks included), the voice, its model and the reply's style, it only
replays audio the voice itself made for those exact words, and nothing about what is said changes.

* The audio (24 kHz mono 16-bit PCM, edges already trimmed) of a line of at most :data:`MAX_WORDS` words and
  :data:`MAX_CHARS` characters is stored once it was fully fetched, never interrupted, and heard.
* The spoken-line check's own ``SAY`` for such a line as the first piece of a reply is remembered with it, only
  for lines the check would let through unchecked anywhere else (at most ``speechguard.FREE_WORDS`` words): the
  one thing it looks for in a short first piece is a sentence's tail said alone, which is the words themselves.
  A ``DROP`` is never remembered: such a line is checked again every time.
* At most :data:`MAX_ENTRIES` lines, the least recently used let go. Files under ``%LOCALAPPDATA%\\Yuki\\voice\\
  tts-cache`` (the user's own voice data; offline runs pass their own folder). Nothing is logged but sizes.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

MAX_WORDS = 6
MAX_CHARS = 60
MAX_ENTRIES = 300


def default_dir() -> Path:
    from yuki.core.paths import data_dir

    return data_dir() / "voice" / "tts-cache"


class TTSCache:
    """The cache (thread-safe). ``get``, ``put``, ``preload`` and ``flush`` touch the disk and run off the voice loop;
    ``approved`` and ``approve`` only touch memory (offline 29 Sept the first ``approved`` read the index on the loop:
    a 724 ms stall), so the index is loaded ahead (``preload``) and an approval is written with the next ``put``."""

    def __init__(self, root: Path | None = None, *, max_entries: int = MAX_ENTRIES) -> None:
        self.root = Path(root) if root is not None else default_dir()
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._index: dict[str, dict[str, Any]] | None = None
        self._dirty = False
        self.stats = {"hits": 0, "misses": 0, "stored": 0, "approved_hits": 0}

    # -- keys ----------------------------------------------------------------------------------

    @staticmethod
    def key(provider: str, model: str, voice: str, style: str | None, text: str) -> str:
        raw = json.dumps([provider, model, voice, style or "", text], ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def cacheable(text: str) -> bool:
        words = (text or "").split()
        return 0 < len(words) <= MAX_WORDS and len(text) <= MAX_CHARS

    # -- the index -------------------------------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._index is None:
            try:
                self._index = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._index = {}
        return self._index

    def _save(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.root / "index.json.tmp"
            tmp.write_text(json.dumps(self._index or {}), encoding="utf-8")
            os.replace(tmp, self.root / "index.json")
        except OSError:
            pass

    # -- use -------------------------------------------------------------------------------------

    def get(self, key: str) -> bytes | None:
        with self._lock:
            entry = self._load().get(key)
            if entry is None:
                self.stats["misses"] += 1
                return None
            try:
                pcm = (self.root / f"{key}.pcm").read_bytes()
            except OSError:
                self._load().pop(key, None)
                self.stats["misses"] += 1
                return None
            entry["used"] = time.time()
            self.stats["hits"] += 1
            return pcm

    def preload(self) -> None:
        """Read the index now (off the voice loop, when the conversation is built)."""
        with self._lock:
            self._load()

    def approved(self, key: str) -> bool:
        """The spoken-line check said SAY to these exact words as a reply's first piece before (memory only: an
        index not loaded yet knows nothing)."""
        with self._lock:
            ok = self._index is not None and bool((self._index.get(key) or {}).get("approved"))
            if ok:
                self.stats["approved_hits"] += 1
            return ok

    def put(self, key: str, pcm: bytes) -> None:
        if not pcm:
            return
        with self._lock:
            index = self._load()
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                tmp = self.root / f"{key}.pcm.tmp"
                tmp.write_bytes(pcm)
                os.replace(tmp, self.root / f"{key}.pcm")
            except OSError:
                return
            entry = index.setdefault(key, {})
            entry.update(bytes=len(pcm), used=time.time())
            self.stats["stored"] += 1
            self._dirty = False   # saved just below, approvals with it
            if len(index) > self.max_entries:
                for old in sorted(index, key=lambda k: index[k].get("used", 0))[:len(index) - self.max_entries]:
                    index.pop(old, None)
                    try:
                        (self.root / f"{old}.pcm").unlink()
                    except OSError:
                        pass
            self._save()

    def approve(self, key: str) -> None:
        """Remember the check's SAY (memory only; written with the next ``put`` or ``flush``)."""
        with self._lock:
            if self._index is None:
                return
            entry = self._index.setdefault(key, {})   # its audio usually lands just after (``put``)
            if not entry.get("approved"):
                entry["approved"] = True
                self._dirty = True

    def flush(self) -> None:
        with self._lock:
            if self._index is not None and self._dirty:
                self._save()
                self._dirty = False


__all__ = ["TTSCache", "default_dir", "MAX_WORDS", "MAX_CHARS"]
