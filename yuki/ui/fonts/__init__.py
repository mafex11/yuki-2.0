"""The fonts Yuki ships: Doto (dot-matrix displays, SIL Open Font License 1.1, ``Doto-OFL.txt``), loaded into
Qt's font database once per process, and the fallback families for everything else.

Segoe UI Variable is a Windows built-in; where it is missing Segoe UI stands in. Neither has Devanagari, so
Nirmala UI (also built in) is added as a fallback family: Hindi words in a chat or a portrait line never show
as boxes. :func:`load_fonts` runs at startup in both the tray and the window process (through
:func:`yuki.ui.window.theme.app_font`, which both call right after creating the application).
"""

from __future__ import annotations

import sys
from pathlib import Path

#: The dot-matrix family, as Doto.ttf names itself.
DOT_FAMILY = "Doto"
#: Families tried after the UI family, per glyph (Devanagari, then symbols).
FALLBACKS = ("Segoe UI", "Nirmala UI", "Segoe UI Symbol")

_LOADED: list[bool] = []


def font_dir() -> Path:
    """Where Doto.ttf sits: next to this module, in the source tree and in the PyInstaller folder alike."""
    here = Path(__file__).resolve().parent
    if (here / "Doto.ttf").exists():
        return here
    base = Path(getattr(sys, "_MEIPASS", "") or here)
    return base / "yuki" / "ui" / "fonts"


def load_fonts() -> bool:
    """Add Doto to Qt's font database and register the fallback families (once per process; needs a
    QGuiApplication). True when Doto is available."""
    if _LOADED:
        return _LOADED[0]
    from PySide6.QtGui import QFont, QFontDatabase

    ok = False
    path = font_dir() / "Doto.ttf"
    try:
        if path.exists():
            ok = QFontDatabase.addApplicationFont(str(path)) >= 0
    except Exception:
        ok = False
    for family in ("Segoe UI Variable", "Segoe UI Variable Text", "Segoe UI Variable Display", "Segoe UI"):
        QFont.insertSubstitutions(family, [f for f in FALLBACKS if f != family])
    _LOADED.append(ok and DOT_FAMILY in QFontDatabase.families())
    return _LOADED[0]


def dot_available() -> bool:
    return bool(_LOADED and _LOADED[0])
