"""The window's page snapshots: the last data each page showed, kept between openings so a reopened window
paints its pages at once and then refreshes them from memory.

The views hold decrypted memory (portrait lines, to-dos, the day's timeline), so the file is encrypted and
authenticated with memory's own field key (AES-256-GCM, :class:`yuki.memory.crypto.FieldCipher`): at rest it
is as private as the database, a copied file is unreadable elsewhere, and a tampered one is rejected before
anything is unpickled. It lives next to ``ui.json`` (``window-snapshot.bin``), is written off the GUI thread
when the window closes, and is read off the GUI thread when it opens. A snapshot from another database, an
older format or a day that is over (Today) is ignored.
"""

from __future__ import annotations

import os
import pickle
import time
import zlib
from datetime import date
from pathlib import Path
from typing import Any

#: Bump when a view's shape changes: an older file is ignored.
FORMAT = 2
#: Pages worth keeping (chat is live; its history is read afresh).
PAGES = ("today", "me", "yuki", "status", "settings")
#: A snapshot older than this is not shown (it would only mislead for a moment).
MAX_AGE_S = 3 * 86400


def path_for(ui_path: Path | None) -> Path:
    from yuki.ui import ui_settings

    return (ui_path or ui_settings.settings_path()).parent / "window-snapshot.bin"


def save(path: Path, cipher: Any, views: dict[str, Any], *, db: str) -> bool:
    """Write the views (the pages in :data:`PAGES` only). False when it could not."""
    keep = {k: v for k, v in views.items() if k in PAGES and v is not None}
    if not keep:
        return False
    try:
        raw = pickle.dumps({"format": FORMAT, "db": db, "at": time.time(), "day": date.today().isoformat(),
                            "views": keep}, protocol=pickle.HIGHEST_PROTOCOL)
        blob = cipher.encrypt_bytes(zlib.compress(raw, 3))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def load(path: Path, cipher: Any, *, db: str) -> dict[str, Any]:
    """The views of the last snapshot for this database ({} when none, stale, foreign or unreadable)."""
    try:
        blob = path.read_bytes()
        raw = zlib.decompress(cipher.decrypt_bytes(blob))   # authenticated before anything is unpickled
        data = pickle.loads(raw)  # noqa: S301 - our own file, authenticated by memory's key
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("db") != db:
        return {}
    if time.time() - float(data.get("at") or 0) > MAX_AGE_S:
        return {}
    views = dict(data.get("views") or {})
    today_view = views.get("today")
    if today_view is not None and getattr(today_view, "day", None) != date.today():
        views.pop("today", None)   # yesterday's Today would say the wrong day
    return views
