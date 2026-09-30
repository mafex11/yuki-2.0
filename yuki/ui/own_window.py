"""Yuki's own window, for the brain and the voice (v3.15): open it on one of its pages.

Live 2026-09-30 "open the Yuki dashboard" got "I don't see a Yuki dashboard": nothing told Yuki its own window
exists, and neither the thinker nor the talker had a way to open it. The tray (yuki-ui) registers the opener it
already uses for a tray click (:meth:`yuki.ui.window_host.WindowHost.open_window`); the thinker's ``open_yuki``
tool and the talker's ``open_yuki`` hand call :func:`open_page` from their own threads. The page is the model's
choice from :data:`PAGES` (a schema enum), never read from the user's words here.

No Qt here: importing this from the agent or the voice costs nothing.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

#: The window's pages, as its sidebar names them (``yuki.ui.window.live.PAGES``), in sidebar order.
PAGES: tuple[str, ...] = ("today", "me", "yuki", "status", "chat", "settings")
#: What each page is, for the tool schemas (one line each).
PAGE_WORDS = ("today: the dashboard (the day, its cards, what is on now); me: what Yuki knows about the user "
              "(portrait, people, to-dos); yuki: Yuki's own page (what it has learned, its rules and know-how); "
              "status: health, costs and what is running; chat: the conversation with Yuki; settings")

_lock = threading.Lock()
_opener: Callable[[str], bool] | None = None


def register(opener: Callable[[str], bool] | None) -> None:
    """The tray's way to open the window on a page (``None``: unregister). ``opener(page)`` returns whether the
    window was brought forward or started; it may be called from any thread."""
    global _opener
    with _lock:
        _opener = opener


def open_page(page: str) -> tuple[bool, str]:
    """Open Yuki's window on ``page`` (one of :data:`PAGES`): ``(ok, report)``. Never raises."""
    page = str(page or "").strip().casefold() or "today"
    if page not in PAGES:
        return False, f"Yuki's window has no page {page!r}; its pages are {', '.join(PAGES)}"
    with _lock:
        opener = _opener
    if opener is None:
        return False, "Yuki's window cannot be opened from here (the tray is not running in this process)"
    try:
        ok = bool(opener(page))
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return False, f"opening Yuki's window failed: {type(exc).__name__}: {exc}"
    if not ok:
        return False, "Yuki's window did not open"
    return True, f"Yuki's window is open on its {page} page, in front"


__all__ = ["PAGES", "PAGE_WORDS", "open_page", "register"]
