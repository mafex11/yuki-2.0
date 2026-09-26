"""Yuki's design system: Liquid Glass adapted to Windows 11, in code.

One module holds every visual decision the Yuki window and the restyled
surfaces (overlay, nudge and question cards, voice pill, status strip) make:
colour tokens for light and dark, the type scale, spacing, corner radii, the
glass material levels and motion. Widgets never invent a colour or a size;
they ask the :class:`Theme` for a role.

The material
------------
Liquid Glass is a translucent sheet that sits *on* the content behind it:

* **Backdrop.** On a real desktop the window asks DWM for a system backdrop
  (``DWMWA_SYSTEMBACKDROP_TYPE``: Acrylic for the window, Mica as the calm
  alternative, see :mod:`yuki.ui.window.backdrop`); DWM blurs what is behind
  at no cost to this process. Offscreen, or when Windows' transparency effects
  are off, the same look is painted: a blurred copy of a backdrop image under a
  tint (:func:`yuki.ui.window.glass.paint_backdrop`), or a plain tinted
  gradient.
* **Glass levels** (:class:`GlassLevel`), from quiet to prominent: ``inset``
  (rows inside a panel), ``regular`` (panels), ``raised`` (floating controls,
  cards, popovers) and ``lens`` (the selected pill: accent-tinted, brightest
  edge). Each level is a fill, a top sheen, a specular edge (bright at the top
  left, a faint rim at the bottom right), a hairline outline for definition and
  a soft shadow.
* **Shape.** Very round: panels 24 px, cards 20 px, rows 14 px, every control a
  pill (radius = half its height). The window itself keeps Windows 11's own
  corners and frame so snapping, the taskbar and Alt+Tab behave as usual.

Type is Segoe UI Variable (Display optical size for titles, Text for body),
falling back to Segoe UI. Everything is in logical pixels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from PySide6.QtGui import QColor, QFont, QFontDatabase

# -- spacing, radii, motion -------------------------------------------------------

#: The 4-px spacing scale.
SPACE = {"xxs": 2, "xs": 4, "s": 8, "m": 12, "l": 16, "xl": 24, "xxl": 32, "xxxl": 48}

#: Corner radii. Controls are pills: their radius is half their height.
RADIUS = {"window": 12, "panel": 24, "card": 20, "row": 14, "chip": 9, "tooltip": 16}

#: Control heights (pills).
CONTROL_H = {"small": 26, "regular": 32, "large": 40, "input": 44}

#: Motion (milliseconds). Glass moves softly: a short spring-like ease out.
MOTION = {"fast": 120, "regular": 180, "slow": 260}

#: Window geometry (logical px).
WINDOW_SIZE = (1280, 840)
SIDEBAR_W = 216


def rgba(r: int, g: int, b: int, a: float = 1.0) -> QColor:
    """A colour from 0-255 channels and a 0-1 alpha."""
    return QColor(r, g, b, round(255 * a))


def hexa(value: str, a: float = 1.0) -> QColor:
    """A colour from ``#rrggbb`` and a 0-1 alpha."""
    c = QColor(value)
    c.setAlphaF(a)
    return c


def css(c: QColor) -> str:
    """``rgba(r,g,b,a)`` for a Qt style sheet."""
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})"


@dataclass(frozen=True)
class GlassLevel:
    """One level of the glass material (see the module docstring)."""

    fill: QColor
    sheen: QColor            # top-down highlight inside the shape (fades out by ~45%)
    edge: QColor             # specular edge, top left
    rim: QColor              # faint rim light, bottom right
    outline: QColor          # hairline outside the edge, for definition
    shadow: QColor           # shadow colour (alpha = strongest point)
    shadow_blur: int         # px
    shadow_y: int            # px


@dataclass(frozen=True)
class Theme:
    """Every colour role, for one appearance (light or dark)."""

    name: str
    dark: bool
    # the window: tint laid over the (DWM or painted) backdrop, and the opaque fallback
    window_tint: QColor
    fallback_top: QColor
    fallback_bottom: QColor
    # text
    text: QColor
    text_2: QColor           # secondary
    text_3: QColor           # muted: captions, axis labels
    text_on_accent: QColor
    # accents
    accent: QColor
    accent_soft: QColor
    kinds: dict[str, QColor]
    status: dict[str, QColor]
    # data
    series: tuple[QColor, ...]
    series_other: QColor
    grid: QColor
    baseline: QColor
    away: QColor
    off_fill: QColor
    off_hatch: QColor
    unrecorded: QColor
    # glass levels
    glass: dict[str, GlassLevel] = field(default_factory=dict)
    # scene (mockups only): the simulated wallpaper
    wallpaper: tuple[tuple[QColor, float, float, float], ...] = ()
    wallpaper_base: tuple[QColor, QColor] = (QColor(0, 0, 0), QColor(0, 0, 0))

    def level(self, name: str) -> GlassLevel:
        return self.glass[name]

    def kind(self, name: str | None) -> QColor:
        return self.kinds.get(str(name or ""), self.accent)

    def series_color(self, index: int | None) -> QColor:
        if index is None or index < 0 or index >= len(self.series):
            return self.series_other
        return self.series[index]


# The categorical order is the validated reference palette (dataviz skill,
# references/palette.md), checked again against Yuki's glass surfaces:
# light #eef1f6 and dark #23252c both pass every hard gate; four light slots are
# under 3:1, so every lane carries a visible text label (the relief rule).
_SERIES_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948")
_SERIES_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767")

#: Status colours are reserved (never a series) and always ship with an icon + words.
_STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}


def _light() -> Theme:
    return Theme(
        name="light", dark=False,
        window_tint=rgba(246, 247, 251, 0.64),
        fallback_top=hexa("#f4f6fb"), fallback_bottom=hexa("#e9edf5"),
        text=rgba(14, 16, 22), text_2=rgba(14, 16, 22, 0.64), text_3=rgba(14, 16, 22, 0.44),
        text_on_accent=rgba(255, 255, 255),
        accent=hexa("#2f6fe4"), accent_soft=hexa("#2f6fe4", 0.14),
        kinds={
            "praise": hexa("#1f9d5c"), "nudge": hexa("#d9822b"), "reminder": hexa("#2f6fe4"),
            "review": hexa("#7a55d6"), "resume": hexa("#2a8fd6"), "question": hexa("#d64f93"),
            "joke": hexa("#b0469b"), "unclear": hexa("#b7791f"), "you": hexa("#2f6fe4"),
            "call": hexa("#1a9a8a"), "error": hexa("#d03b3b"),
        },
        status={k: hexa(v) for k, v in _STATUS.items()},
        series=tuple(hexa(v) for v in _SERIES_LIGHT), series_other=hexa("#8a8f9c"),
        grid=rgba(14, 16, 22, 0.07), baseline=rgba(14, 16, 22, 0.16),
        away=rgba(14, 16, 22, 0.30), off_fill=rgba(20, 24, 36, 0.07), off_hatch=rgba(20, 24, 36, 0.16),
        unrecorded=rgba(20, 24, 36, 0.04),
        glass={
            "inset": GlassLevel(rgba(255, 255, 255, 0.38), rgba(255, 255, 255, 0.18), rgba(255, 255, 255, 0.85),
                                rgba(255, 255, 255, 0.30), rgba(30, 40, 70, 0.06), rgba(30, 40, 80, 0.0), 0, 0),
            "regular": GlassLevel(rgba(255, 255, 255, 0.52), rgba(255, 255, 255, 0.34), rgba(255, 255, 255, 0.98),
                                  rgba(255, 255, 255, 0.45), rgba(30, 40, 70, 0.08), rgba(30, 44, 90, 0.13), 28, 8),
            "raised": GlassLevel(rgba(255, 255, 255, 0.72), rgba(255, 255, 255, 0.45), rgba(255, 255, 255, 1.0),
                                 rgba(255, 255, 255, 0.55), rgba(30, 40, 70, 0.10), rgba(30, 44, 90, 0.20), 34, 12),
            "lens": GlassLevel(rgba(255, 255, 255, 0.86), rgba(255, 255, 255, 0.60), rgba(255, 255, 255, 1.0),
                               rgba(255, 255, 255, 0.70), rgba(47, 111, 228, 0.22), rgba(47, 90, 180, 0.22), 18, 6),
        },
        wallpaper_base=(hexa("#dfe8f7"), hexa("#f3e9f1")),
        wallpaper=(
            (hexa("#8fb7ff", 0.85), 0.18, 0.22, 0.42), (hexa("#f7b2c8", 0.75), 0.82, 0.18, 0.38),
            (hexa("#ffd59a", 0.65), 0.70, 0.86, 0.40), (hexa("#a9e5d4", 0.70), 0.12, 0.88, 0.36),
            (hexa("#c3b3ff", 0.70), 0.50, 0.50, 0.30),
        ),
    )


def _dark() -> Theme:
    return Theme(
        name="dark", dark=True,
        window_tint=rgba(20, 22, 28, 0.66),
        fallback_top=hexa("#1d2028"), fallback_bottom=hexa("#15171d"),
        text=rgba(245, 246, 250), text_2=rgba(245, 246, 250, 0.66), text_3=rgba(245, 246, 250, 0.44),
        text_on_accent=rgba(255, 255, 255),
        accent=hexa("#5d9bff"), accent_soft=hexa("#5d9bff", 0.20),
        kinds={
            "praise": hexa("#76d69e"), "nudge": hexa("#ffbe6e"), "reminder": hexa("#7eb4ff"),
            "review": hexa("#c4a0ff"), "resume": hexa("#8cc8ff"), "question": hexa("#ffa0cd"),
            "joke": hexa("#e59ad6"), "unclear": hexa("#f0c05a"), "you": hexa("#7eb4ff"),
            "call": hexa("#5fd4c2"), "error": hexa("#ff7b7b"),
        },
        status={k: hexa(v) for k, v in _STATUS.items()},
        series=tuple(hexa(v) for v in _SERIES_DARK), series_other=hexa("#7c8190"),
        grid=rgba(255, 255, 255, 0.06), baseline=rgba(255, 255, 255, 0.14),
        away=rgba(255, 255, 255, 0.32), off_fill=rgba(0, 0, 0, 0.26), off_hatch=rgba(255, 255, 255, 0.10),
        unrecorded=rgba(255, 255, 255, 0.03),
        glass={
            "inset": GlassLevel(rgba(255, 255, 255, 0.045), rgba(255, 255, 255, 0.03), rgba(255, 255, 255, 0.16),
                                rgba(255, 255, 255, 0.05), rgba(0, 0, 0, 0.30), rgba(0, 0, 0, 0.0), 0, 0),
            "regular": GlassLevel(rgba(46, 49, 60, 0.46), rgba(255, 255, 255, 0.06), rgba(255, 255, 255, 0.30),
                                  rgba(255, 255, 255, 0.09), rgba(0, 0, 0, 0.45), rgba(0, 0, 0, 0.36), 30, 10),
            "raised": GlassLevel(rgba(62, 66, 80, 0.58), rgba(255, 255, 255, 0.09), rgba(255, 255, 255, 0.38),
                                 rgba(255, 255, 255, 0.12), rgba(0, 0, 0, 0.50), rgba(0, 0, 0, 0.48), 36, 14),
            "lens": GlassLevel(rgba(93, 155, 255, 0.24), rgba(255, 255, 255, 0.12), rgba(255, 255, 255, 0.50),
                               rgba(255, 255, 255, 0.16), rgba(0, 0, 0, 0.50), rgba(0, 0, 0, 0.40), 18, 6),
        },
        wallpaper_base=(hexa("#0b1226"), hexa("#140c24")),
        wallpaper=(
            (hexa("#2c5fd8", 0.85), 0.22, 0.30, 0.45), (hexa("#8a3fd1", 0.70), 0.80, 0.20, 0.40),
            (hexa("#1b8fb8", 0.55), 0.70, 0.85, 0.42), (hexa("#d0508a", 0.45), 0.10, 0.90, 0.34),
            (hexa("#3a3fb0", 0.60), 0.50, 0.55, 0.30),
        ),
    )


LIGHT = _light()
DARK = _dark()


def theme(name: str) -> Theme:
    """``"light"`` or ``"dark"``."""
    return DARK if name == "dark" else LIGHT


# -- type ------------------------------------------------------------------------

#: role -> (pixel size, weight word, optical size). Calm hierarchy: one large
#: title per page, section titles, body, and small captions; nothing in between.
TYPE: dict[str, tuple[int, str, str]] = {
    "large_title": (30, "Semibold", "Display"),
    "title": (20, "Semibold", "Display"),
    "numeral": (24, "Semibold", "Display"),
    "headline": (15, "Semibold", "Text"),
    "body": (14, "Regular", "Text"),
    "body_strong": (14, "Semibold", "Text"),
    "callout": (13, "Regular", "Text"),
    "caption": (12, "Regular", "Text"),
    "caption_strong": (12, "Semibold", "Text"),
    "micro": (11, "Semibold", "Small"),
    "icon": (16, "Regular", ""),
    "icon_small": (13, "Regular", ""),
    "icon_large": (20, "Regular", ""),
}

_WEIGHTS = {"Light": QFont.Weight.Light, "Regular": QFont.Weight.Normal, "Semibold": QFont.Weight.DemiBold,
            "Bold": QFont.Weight.Bold}


@lru_cache(maxsize=None)
def _families() -> frozenset[str]:
    return frozenset(QFontDatabase.families())


def font(role: str) -> QFont:
    """The font for a type role (see :data:`TYPE`).

    Windows' own font database lists "Segoe UI Variable Display" / "... Text" as
    families; FreeType (the offscreen platform) lists one "Segoe UI Variable"
    family whose styles carry the optical size ("Semibold Display"). Both are
    handled; Segoe UI is the fallback.
    """
    size, weight, optical = TYPE[role]
    families = _families()
    if role.startswith("icon"):
        f = QFont("Segoe Fluent Icons" if "Segoe Fluent Icons" in families else "Segoe MDL2 Assets")
        f.setPixelSize(size)
        return f
    native = f"Segoe UI Variable {optical}"
    if native in families:
        f = QFont(native)
        f.setWeight(_WEIGHTS[weight])
    elif "Segoe UI Variable" in families:
        f = QFont("Segoe UI Variable")
        style = f"{weight} {optical}" if weight != "Regular" else f"Regular {optical}"
        if style in QFontDatabase.styles("Segoe UI Variable"):
            f.setStyleName(style)
        else:
            f.setWeight(_WEIGHTS[weight])
    else:
        f = QFont("Segoe UI")
        f.setWeight(_WEIGHTS[weight])
    f.setPixelSize(size)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


#: Segoe Fluent Icons code points used by the window (Windows 11's icon font).
ICON = {
    "today": "",      # clock
    "me": "",         # contact
    "yuki": "",       # robot
    "status": "",     # diagnostic
    "chat": "",       # message
    "settings": "",
    "prev": "", "next": "", "down": "",
    "calendar": "", "mic": "", "speaker": "", "phone": "",
    "info": "", "edit": "", "delete": "", "people": "",
    "heart": "", "bulb": "", "shield": "", "globe": "",
    "power": "", "moon": "", "question": "", "check": "",
    "close": "", "send": "", "search": "", "more": "",
    "work": "", "game": "", "code": "", "music": "",
    "headphones": "", "pause": "", "play": "", "cloud": "",
    "money": "", "sparkle": "", "history": "", "pin": "",
    "wave": "", "link": "", "warning": "", "lock": "",
    "flag": "", "emoji": "", "book": "", "star": "",
}
