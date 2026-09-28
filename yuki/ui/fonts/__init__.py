"""The fonts Yuki ships, loaded into Qt's font database once per process, and the fallbacks for the rest.

* **IBM Plex Sans** (IBM, SIL Open Font License 1.1, ``IBMPlexSans-OFL.txt``): the UI face everywhere (the
  window, every floating surface, the tray menu, the first-run wizard). One variable file carries every
  weight (the ``wght`` axis) and width; no italic is used, so none is shipped. Picked by the user on
  2026-09-28 from Geist, IBM Plex Sans, Manrope and Onest (docs/ui-mockups/horizon-field/fonts/).
* **Doto** (SIL OFL 1.1, ``Doto-OFL.txt``): the dot-matrix displays.

Plex has no Devanagari, so Nirmala UI (a Windows built-in) follows it as a fallback family and Hindi words
never show as boxes; Segoe UI and Segoe UI Symbol catch the rest. :func:`load_fonts` runs at startup in the
tray and the window process (through :func:`yuki.ui.window.theme.app_font`, which both call right after
creating the application) and in the first-run wizard.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: The UI family, as IBMPlexSans.ttf names itself.
UI_FAMILY = "IBM Plex Sans"
#: The dot-matrix family, as Doto.ttf names itself.
DOT_FAMILY = "Doto"
#: Families tried after the UI family, per glyph (Devanagari, then symbols).
FALLBACKS = ("Segoe UI", "Nirmala UI", "Segoe UI Symbol")
#: The files bundled here (in yuki/ui/fonts/, and in packaging/yuki.spec's datas).
FILES = ("IBMPlexSans.ttf", "Doto.ttf")

_LOADED: dict[str, bool] = {}


def font_dir() -> Path:
    """Where the fonts sit: next to this module, in the source tree and in the PyInstaller folder alike."""
    here = Path(__file__).resolve().parent
    if (here / "Doto.ttf").exists():
        return here
    base = Path(getattr(sys, "_MEIPASS", "") or here)
    return base / "yuki" / "ui" / "fonts"


def load_fonts() -> bool:
    """Add Plex and Doto to Qt's font database and register the fallback families (once per process; needs a
    QGuiApplication). True when both are available."""
    if _LOADED:
        return all(_LOADED.values())
    from PySide6.QtGui import QFont, QFontDatabase

    for name in FILES:
        path = font_dir() / name
        try:
            fid = QFontDatabase.addApplicationFont(str(path)) if path.exists() else -1
        except Exception:
            fid = -1
        _LOADED[name] = fid >= 0
    for family in (UI_FAMILY, "Segoe UI Variable", "Segoe UI Variable Text", "Segoe UI Variable Display",
                   "Segoe UI"):
        QFont.insertSubstitutions(family, [f for f in FALLBACKS if f != family])
    families = set(QFontDatabase.families())
    _LOADED["IBMPlexSans.ttf"] = _LOADED.get("IBMPlexSans.ttf", False) and UI_FAMILY in families
    _LOADED["Doto.ttf"] = _LOADED.get("Doto.ttf", False) and DOT_FAMILY in families
    return all(_LOADED.values())


def dot_available() -> bool:
    return bool(_LOADED.get("Doto.ttf"))


def ui_available() -> bool:
    return bool(_LOADED.get("IBMPlexSans.ttf"))
