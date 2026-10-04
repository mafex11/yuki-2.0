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

#: The window's pages, as its rail names them (``yuki/ui/web/app.js`` PAGES), plus ``todos`` (Home, scrolled to the
#: to-do list) and ``chat`` (the conversation drawer). The 4 Oct appTodo build: the old Qt names (today, yuki, status)
#: sent the thinker to the wrong page and told it the to-dos were on Me; they are still taken (:data:`LEGACY`).
PAGES: tuple[str, ...] = ("home", "todos", "work", "me", "playbook", "activity", "chat", "settings")
#: The Qt window's names, still accepted from an older caller (passed on as given: both windows map them).
LEGACY: tuple[str, ...] = ("today", "yuki", "status")
#: A short map of Yuki's own window, for the tool schemas: which page shows what, so a question about Yuki's own data
#: is answered by reading that data and placing it here, never by guessing (the 4 Oct live test: "where is my to-do
#: list?" got "inside the Work tab thread card", a place that never existed), with the worked example of that case.
#: Keep it short: the thinker's tool block has a measured budget (tests/test_d_brain.py).
PAGE_WORDS = ("home: needs them, offers, Your list (open to-dos), running; todos: Home at Your list; work: every "
              "to-do, today's sessions; me: people, routines, facts, misheard; playbook: lessons, rules; activity: "
              "what you did, calls; chat; settings. Asked about it, read the data and place it here, never guess "
              "(where's my list: todo list, then todos, say where).")
#: Where the window opened, said in the report the model answers from (``todos``: the words it should say).
PLACES = {"home": "Home", "todos": "Home, at the user's to-do list (under Your list; every to-do is also on Work)",
          "work": "Work (every to-do, today's sessions)", "me": "Me", "playbook": "Playbook", "activity": "Activity",
          "chat": "the chat", "settings": "Settings"}
#: The new names for the Qt window (``yuki.ui.window``, the fallback without WebView2), which knows only its own.
QT_PAGES = {"home": "today", "todos": "today", "work": "today", "playbook": "yuki", "activity": "status"}


def qt_page(page: str) -> str:
    """``page`` as the Qt window names it."""
    return QT_PAGES.get(page, page)


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
    page = str(page or "").strip().casefold() or "home"
    if page not in PAGES and page not in LEGACY:
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
    return True, f"Yuki's window is open on {PLACES.get(page) or f'its {page} page'}, in front"


__all__ = ["PAGES", "PAGE_WORDS", "PLACES", "open_page", "qt_page", "register"]
