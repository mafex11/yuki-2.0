"""Saved plans of the one-off memory passes (dry run -> review -> apply exactly that), shared plumbing.

A pass's ``--dry-run --save FILE`` keeps its decisions in one JSON file, encrypted with the memory's key (they quote
memory); ``--apply FILE`` writes exactly those decisions with no new model call, so what was reviewed on a copy is
what is written. Used by :mod:`yuki.memory.sessions`, :mod:`yuki.memory.misheard` and :mod:`yuki.memory.english`
(the threads pass keeps its own, :func:`yuki.memory.workthreads.save_plan`, same format).
"""

from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


def save_plan(store: Any, kind: str, decisions: Sequence[dict[str, Any]], path: Any = None, *,
              now: float | None = None) -> Path:
    """Write ``decisions`` (encrypted) to ``path`` (default: a dated file in the temp folder). Returns the path."""
    path = Path(path) if path else Path(tempfile.gettempdir()) / f"yuki-{kind}-plan-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"kind": f"yuki-{kind}-plan", "made_at": time.time() if now is None else float(now),
                                "count": len(decisions),
                                "decisions_ciphertext": store.cipher.encrypt(json.dumps(list(decisions),
                                                                                        ensure_ascii=False))}),
                    encoding="utf-8")
    return path


def load_plan(store: Any, kind: str, path: Any) -> list[dict[str, Any]]:
    """The decisions :func:`save_plan` kept; raises when the file is another pass's plan or not this memory's key."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("kind") != f"yuki-{kind}-plan":
        raise ValueError(f"{path} is not a saved {kind} plan")
    return [d for d in json.loads(store.cipher.decrypt(data["decisions_ciphertext"])) if isinstance(d, dict)]
