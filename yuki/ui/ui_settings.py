"""The UI's own settings: ``%LOCALAPPDATA%\\Yuki\\ui.json`` (motion, the window's place, the overlay's shortcut,
the accent colour, which window a tray click opens: ``window_engine``).

Written by the Yuki window's Settings page; read by the window and by yuki-ui,
which applies a change live when the window tells it over the pipe
(``ui_settings``), or at its next start. Unknown keys are kept, so an older and a
newer build can share the file; the Liquid Glass keys retired on 2026-09-27
(glass, bending, blur, transparency, light/dark, background) are ignored when
read and dropped on the next save.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

from yuki.ui import tokens as _tokens

#: Keys of the retired glass Appearance settings: ignored on load, removed on save.
RETIRED = frozenset({"glass", "bend", "blur", "transparency", "appearance", "background"})


def settings_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki" / "ui.json"


@dataclass(frozen=True)
class UiSettings:
    reduce_motion: bool = False
    window: list[int] | None = None     # x, y, w, h of the window when it last closed
    maximized: bool = False
    open_hotkey: str = ""               # "Open Yuki anywhere": the overlay's shortcut ("" = the built-in default)
    quiet_last: list[str] | None = None  # the quiet hours to bring back when they are turned on again
    accent: str = "green"               # the one accent colour: "green" (olive, default) | "orange"
    window_engine: str = "web"          # what a tray click opens: "web" (the Lattice HTML window) | "qt" (native)
    web_window: list[int] | None = None  # the HTML window's x, y, w, h (physical px) when it last closed
    web_maximized: bool = False
    island_resting: bool = True         # Settings → Island: the tiny resting pill when idle (False: nothing on screen)
    #: appTakeover: how far (px) the mouse must move while Yuki works for Yuki to pause (you took over); see
    #: :data:`TAKEOVER_MOVE_LABEL`. Kept within 12..400 (``yuki.actions.leases.takeover_move_px``).
    takeover_move_px: int = 40


#: The plain-words label and hint for ``takeover_move_px`` on the Settings page (appTakeover).
TAKEOVER_MOVE_LABEL = "Pause when you move the mouse this far"
TAKEOVER_MOVE_HINT = ("While I work, a key or a click always pauses me so you can take over, and so does moving the "
                      "mouse this far. Pick a bigger move if your mouse drifts on its own.")
TAKEOVER_MOVE_CHOICES: tuple[int, ...] = (24, 40, 64, 100, 160)
#: Each choice in plain words, as the Settings page offers it (the window's Settings → While I work).
TAKEOVER_MOVE_WORDS: dict[int, str] = {24: "A nudge", 40: "A short move (the usual)", 64: "A clear move",
                                       100: "A long move", 160: "A big sweep"}
#: A distance set by hand in ui.json that is none of the choices: shown as it is, in these words.
TAKEOVER_MOVE_OWN = "Your own setting"

#: Settings → Privacy and pause → "Record how I work" (report 78; phase C0 showed it off and locked, phase C1 of
#: 7 Oct 2026 unlocked it after the user's decision D1). Saved in the privacy file (``[trace] record_actions``,
#: ``leave_out``), which the memory service reads within a second. Off for a new install.
RECORD_LABEL = "Record how I work"
RECORD_HINT = ("I note which button or field you use in each app, your shortcuts and what changed, so I can learn to "
               "do things your way. Never the letters you type, never what you copy, never your screen, never while a "
               "game runs.")
RECORD_ON = "On. Pausing memory from the tray pauses this too. What I note is kept for 30 days."
RECORD_OFF = "Off. Nothing about how you work is noted."
#: Kept for older callers (C0): the switch no longer waits.
RECORD_WAITING = RECORD_OFF
RECORD_LEAVE_OUT_LABEL = "Leave these apps out"
RECORD_LEAVE_OUT_HINT = "Nothing you do in these apps is noted."


#: The window engines a settings file may hold (``window_engine``); anything else reads as "web".
WINDOW_ENGINES: tuple[str, ...] = ("web", "qt")

#: The accent names a settings file may hold (the one token source, ``yuki/ui/web/tokens.json`` ``accent``;
#: "green" is the Lattice mint; the colours are in :data:`yuki.ui.window.theme.ACCENTS`).
ACCENT_NAMES: tuple[str, ...] = tuple(_tokens.ACCENTS)


def _clean(raw: dict[str, Any]) -> UiSettings:
    win = raw.get("window")
    win = [int(v) for v in win][:4] if isinstance(win, list) and len(win) >= 4 and all(
        isinstance(v, (int, float)) for v in win[:4]) else None
    hotkey = " ".join(str(raw.get("open_hotkey") or "").split()).lower()
    last = raw.get("quiet_last")
    last = [str(v) for v in last][:2] if isinstance(last, list) and len(last) >= 2 else None
    accent = str(raw.get("accent") or "green").strip().lower()
    engine = str(raw.get("window_engine") or "web").strip().lower()
    web = raw.get("web_window")
    web = [int(v) for v in web][:4] if isinstance(web, list) and len(web) >= 4 and all(
        isinstance(v, (int, float)) for v in web[:4]) else None
    try:
        move = max(12, min(400, int(raw.get("takeover_move_px", 40))))
    except (TypeError, ValueError):
        move = 40
    return UiSettings(bool(raw.get("reduce_motion", False)), win, bool(raw.get("maximized", False)), hotkey, last,
                      accent if accent in ACCENT_NAMES else "green", engine if engine in WINDOW_ENGINES else "web",
                      web, bool(raw.get("web_maximized", False)), bool(raw.get("island_resting", True)),
                      takeover_move_px=move)


def load(path: Path | None = None) -> UiSettings:
    try:
        raw = json.loads((path or settings_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return UiSettings()
    return _clean(raw if isinstance(raw, dict) else {})


def save(settings: UiSettings, path: Path | None = None) -> None:
    path = path or settings_path()
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        old = old if isinstance(old, dict) else {}
    except (OSError, ValueError):
        old = {}
    old = {k: v for k, v in old.items() if k not in RETIRED}
    old.update(asdict(settings))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(old, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def update(path: Path | None = None, **changes: Any) -> UiSettings:
    """Change some settings and save; returns the result (unknown names are ignored)."""
    names = {f.name for f in fields(UiSettings)}
    current_ = load(path)
    new = _clean({**asdict(current_), **{k: v for k, v in changes.items() if k in names}})
    save(new, path)
    if path is None:
        _CURRENT[0] = new
    return new


#: This process's settings, read once (``reload`` after the window says they changed).
_CURRENT: list[UiSettings | None] = [None]


def current() -> UiSettings:
    """The settings as this process last read them (read on first use; a broken file reads as defaults)."""
    if _CURRENT[0] is None:
        try:
            _CURRENT[0] = load()
        except Exception:
            _CURRENT[0] = UiSettings()
    return _CURRENT[0]


def reload() -> UiSettings:
    _CURRENT[0] = None
    return current()


__all__ = ["UiSettings", "RETIRED", "ACCENT_NAMES", "WINDOW_ENGINES", "settings_path", "load", "save", "update", "current", "reload", "replace"]
