"""The fonts Yuki ships, loaded into Qt's font database once per process, and the fallbacks for the rest.

The Lattice design (2026-10-03; ``yuki/ui/web/tokens.json`` ``type``) uses three faces, all SIL Open Font
License 1.1 with their licence beside them:

* **Instrument Sans** (``InstrumentSans-OFL.txt``): the UI face everywhere (the window, every floating
  surface, the tray menu, the first-run wizard). One variable file, weights 400-700.
* **JetBrains Mono** (``JetBrainsMono-OFL.txt``): labels, times, provenance and costs. Loaded the first time a
  surface asks for it (:func:`load_mono`), so a process that shows no data type never loads it.
* **Doto** (``Doto-OFL.txt``): the dot-matrix displays.

**IBM Plex Sans** (``IBMPlexSans-OFL.txt``, the round-2 face) stays bundled as the fallback UI face: it is loaded
only when Instrument Sans cannot be, so the tray holds the same number of faces as before.

Neither face has Devanagari, so Nirmala UI (a Windows built-in) follows as a fallback family and Hindi words
never show as boxes; Segoe UI and Segoe UI Symbol catch the rest. :func:`load_fonts` runs at startup in the
tray and the window process (through :func:`yuki.ui.window.theme.app_font`, which both call right after
creating the application) and in the first-run wizard. The HTML window loads the same files through
``tokens.css`` (``https://fonts.yuki.example/``, mapped to this folder).
"""

from __future__ import annotations

import sys
from pathlib import Path

#: The UI family, as InstrumentSans.ttf names itself.
UI_FAMILY = "Instrument Sans"
#: The data family, as JetBrainsMono.ttf names itself.
MONO_FAMILY = "JetBrains Mono"
#: The dot-matrix family, as Doto.ttf names itself.
DOT_FAMILY = "Doto"
#: The fallback UI family (round 2), loaded only when Instrument Sans is not.
FALLBACK_UI_FAMILY = "IBM Plex Sans"
#: Families tried after the UI family, per glyph (Devanagari, then symbols).
FALLBACKS = ("Segoe UI", "Nirmala UI", "Segoe UI Symbol")
#: The files bundled here (in yuki/ui/fonts/, and in packaging/yuki.spec's datas), by family.
FILES = {UI_FAMILY: "InstrumentSans.ttf", MONO_FAMILY: "JetBrainsMono.ttf", DOT_FAMILY: "Doto.ttf",
         FALLBACK_UI_FAMILY: "IBMPlexSans.ttf"}

#: family -> whether it is in this process's font database (absent: not tried yet).
_LOADED: dict[str, bool] = {}


def font_dir() -> Path:
    """Where the fonts sit: next to this module, in the source tree and in the PyInstaller folder alike."""
    here = Path(__file__).resolve().parent
    if (here / "Doto.ttf").exists():
        return here
    base = Path(getattr(sys, "_MEIPASS", "") or here)
    return base / "yuki" / "ui" / "fonts"


def _add(family: str) -> bool:
    """Add one bundled family (once); True when Qt then lists it."""
    if family in _LOADED:
        return _LOADED[family]
    from PySide6.QtGui import QFontDatabase

    path = font_dir() / FILES[family]
    try:
        fid = QFontDatabase.addApplicationFont(str(path)) if path.exists() else -1
    except Exception:
        fid = -1
    _LOADED[family] = fid >= 0 and family in QFontDatabase.applicationFontFamilies(fid)
    return _LOADED[family]


def load_fonts() -> bool:
    """Add the UI face and Doto to Qt's font database and register the fallback families (once per process;
    needs a QGuiApplication). True when both are available (Plex standing in for Instrument Sans counts)."""
    if UI_FAMILY in _LOADED and DOT_FAMILY in _LOADED:
        return ui_available() and dot_available()
    from PySide6.QtGui import QFont

    ui = _add(UI_FAMILY) or _add(FALLBACK_UI_FAMILY)
    dot = _add(DOT_FAMILY)
    for family in (UI_FAMILY, FALLBACK_UI_FAMILY, "Segoe UI Variable", "Segoe UI Variable Text",
                   "Segoe UI Variable Display", "Segoe UI"):
        QFont.insertSubstitutions(family, [f for f in FALLBACKS if f != family])
    return ui and dot


def load_mono() -> bool:
    """Add JetBrains Mono (once). True the first time it is added (callers refresh their family cache then)."""
    if MONO_FAMILY in _LOADED:
        return False
    return _add(MONO_FAMILY)


def dot_available() -> bool:
    return bool(_LOADED.get(DOT_FAMILY))


def ui_available() -> bool:
    return bool(_LOADED.get(UI_FAMILY) or _LOADED.get(FALLBACK_UI_FAMILY))


def mono_available() -> bool:
    return bool(_LOADED.get(MONO_FAMILY))
