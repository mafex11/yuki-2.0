"""The UI's own settings: ``%LOCALAPPDATA%\\Yuki\\ui.json`` (the glass, motion, appearance, the window's place).

Written by the Yuki window's Settings page (Appearance section); read by the window
and by yuki-ui, which applies a change live to the overlay, the cards, the voice
pill and the strip when the window tells it over the pipe (``ui_settings``), or at
its next start. Unknown keys are kept, so an older and a newer build can share the file.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

#: Glass effect choices, in the order the Settings page shows them.
GLASS_MODES = ("off", "light", "full")
#: The default glass (measured 2026-09-26, docs/UI-window.md "Phase 2 measurements").
DEFAULT_GLASS = "light"
#: The window's background: plain black / white (2026-09-27), or "see-through" (DWM Acrylic of the desktop).
BACKGROUNDS = ("solid", "see-through")
DEFAULT_BACKGROUND = "solid"


def settings_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki" / "ui.json"


@dataclass(frozen=True)
class UiSettings:
    glass: str = DEFAULT_GLASS          # off | light | full
    bend: float = 1.0                   # "Glass bending": 0..2
    blur: float = 1.0                   # "Blur": 0..2
    reduce_motion: bool = False
    appearance: str = "system"          # system | light | dark
    window: list[int] | None = None     # x, y, w, h of the window when it last closed
    maximized: bool = False
    background: str = DEFAULT_BACKGROUND   # solid (black / white) | see-through (DWM's blur of the desktop)
    transparency: float = 1.0           # "Transparency": 0..2, how see-through every glass is (1 = as designed)
    open_hotkey: str = ""               # "Open Yuki anywhere": the overlay's shortcut ("" = the built-in default)
    quiet_last: list[str] | None = None  # the quiet hours to bring back when they are turned on again

    def policy(self) -> Any:
        from yuki.ui.lens import GlassPolicy

        return GlassPolicy(self.glass, self.bend, self.blur, self.reduce_motion, self.background == "see-through",
                           self.transparency)


def _clean(raw: dict[str, Any]) -> UiSettings:
    d = UiSettings()
    glass = str(raw.get("glass", d.glass)).lower()
    appearance = str(raw.get("appearance", d.appearance)).lower()
    background = str(raw.get("background", d.background)).lower()

    def num(key: str, default: float) -> float:
        try:
            return min(2.0, max(0.0, float(raw.get(key, default))))
        except (TypeError, ValueError):
            return default

    win = raw.get("window")
    win = [int(v) for v in win][:4] if isinstance(win, list) and len(win) >= 4 and all(
        isinstance(v, (int, float)) for v in win[:4]) else None
    hotkey = " ".join(str(raw.get("open_hotkey") or "").split()).lower()
    last = raw.get("quiet_last")
    last = [str(v) for v in last][:2] if isinstance(last, list) and len(last) >= 2 else None
    return UiSettings(glass if glass in GLASS_MODES else d.glass, num("bend", 1.0), num("blur", 1.0),
                      bool(raw.get("reduce_motion", False)),
                      appearance if appearance in ("system", "light", "dark") else "system", win,
                      bool(raw.get("maximized", False)),
                      background if background in BACKGROUNDS else d.background, num("transparency", 1.0),
                      hotkey, last)


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
    old.update(asdict(settings))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(old, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def update(path: Path | None = None, **changes: Any) -> UiSettings:
    """Change some settings and save; returns the result (unknown names are ignored)."""
    names = {f.name for f in fields(UiSettings)}
    current = load(path)
    new = _clean({**asdict(current), **{k: v for k, v in changes.items() if k in names}})
    save(new, path)
    return new


def apply_policy(settings: UiSettings | None = None) -> UiSettings:
    """Load (or take) the settings and make them this process's glass policy."""
    from yuki.ui.lens import set_policy

    settings = settings or load()
    set_policy(settings.policy())
    return settings


__all__ = ["UiSettings", "GLASS_MODES", "DEFAULT_GLASS", "BACKGROUNDS", "DEFAULT_BACKGROUND", "settings_path", "load", "save", "update", "apply_policy",
           "replace"]
