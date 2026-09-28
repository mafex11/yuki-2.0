"""Yuki's design system, "Horizon x Field Unit", in code.

One module holds every visual decision the Yuki window and yuki-ui's floating surfaces (overlay, cards,
voice pill, call pill, strip, tray menu) and the first-run wizard make: colour tokens, the type scale,
spacing, corner radii and control sizes. Widgets never invent a colour or a size; they ask the
:class:`Theme` for a role. The approved mockups are ``docs/ui-mockups/drafts/mix/`` (gitignored: they hold
personal data); ``mix.css`` there is the source of these tokens.

The look (2026-09-28, replacing flat shadcn at the user's request):

* **Horizon's palette.** Navy-charcoal: the window ``#0B0E13``, flat panels ``#10141B`` with a 1 px
  ``#1E2632`` line, inset "glass" displays ``#06090D``, ink ``#E6E9EE`` / ``#AEB6C2`` / ``#6F7886``. Floating
  dialogs are the same panel colour with 14 px corners and a soft shadow.
* **Field Unit's hardware.** Dot-matrix displays in Doto (bundled, SIL OFL: :mod:`yuki.ui.fonts`), keys
  with a 2 px bottom edge and a status LED.
* **One accent**, olive green ``#8FAF5A`` by default or orange ``#FF5B14`` (Settings > General > Accent),
  used only on dot-matrix displays, live and recording states, the current or selected moment, the
  selected key and the speaking wave. No amber or yellow anywhere, no decorative dashes, no gradients,
  glass, blur or light mode.
* **Type.** Segoe UI Variable (Display, light or regular, for titles; Text for the rest), else Segoe UI;
  Nirmala UI as the fallback family so Devanagari never shows as boxes.

Everything is in logical pixels.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache

from PySide6.QtGui import QColor, QFont, QFontDatabase

# -- spacing, radii, sizes ---------------------------------------------------------

#: The 4-px spacing scale.
SPACE = {"xxs": 2, "xs": 4, "s": 8, "m": 12, "l": 16, "xl": 24, "xxl": 32, "xxxl": 48}

#: Corner radii: page modules 12, displays and keys 8, pill buttons fully round, floating dialogs 14.
RADIUS = {"window": 8, "panel": 12, "card": 12, "display": 8, "key": 8, "row": 8, "control": 16, "field": 8,
          "chip": 6, "tooltip": 8, "floating": 14, "bubble": 10}

#: Control heights (the mockup's pill buttons are 30-32 px tall).
CONTROL_H = {"small": 30, "regular": 32, "large": 36, "input": 34}

#: Motion (milliseconds): short and plain.
MOTION = {"fast": 120, "regular": 150, "slow": 200}

#: Window geometry (logical px). The sidebar is a column of 60 px hardware keys.
WINDOW_SIZE = (1280, 840)
SIDEBAR_W = 96

#: Page padding (mockup ``main``: 34 36 40 32).
PAGE_PAD = (32, 34, 36, 40)

#: The accent choices (Settings > General > Accent). Olive green is the default (the user, 2026-09-28:
#: "forest green or olive green", calm and earthy, never neon); orange is the Field Unit original.
ACCENTS: dict[str, str] = {"green": "#8FAF5A", "orange": "#FF5B14"}
DEFAULT_ACCENT = "green"

#: Horizon x Field Unit tokens (mix.css).
PALETTE = {
    "bg": "#0B0E13", "panel": "#10141B", "key": "#131821", "key_on": "#19212D", "line": "#1E2632",
    "line2": "#161D27", "glass": "#06090D", "glass_border": "#1A222E", "ink": "#E6E9EE", "ink2": "#AEB6C2",
    "mute": "#6F7886", "faint": "#3E4858", "led_off": "#283140",
    # floating dialogs (surfaces.html)
    "float_border": "#222A36", "float_sep": "#1B222C", "bubble": "#161C26", "menu_ink": "#D0D5DC",
    # the skyline (today.html)
    "cool": "#8C9BB0", "sky_far": "#131A24", "sky_near": "#222D3C", "ridge": "#6A7B92", "sky_base": "#26303E",
    "sky_gap": "#3A4556", "tick": "#56606E", "peak": "#7D899B", "call_track": "#171E28",
    # errors are said in words, in a quiet red (never amber)
    "danger": "#E5736B", "destructive": "#3A1A1D",
}


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
class Surface:
    """A flat surface: its fill and its 1 px border (transparent: none)."""

    fill: QColor
    border: QColor
    name: str = ""


@dataclass(frozen=True)
class Theme:
    """Every colour role. The older shadcn role names stay (``card``, ``muted``, ``text_2``...) so every widget
    keeps asking by role; the Horizon x Field Unit roles are added beside them."""

    name: str
    dark: bool
    accent_name: str
    # base roles
    bg: QColor               # the window
    card: QColor             # a page module (panel)
    border: QColor           # a module's line
    muted: QColor            # hover and a raised key face
    subtle: QColor           # a key face at rest
    ring: QColor             # the focus ring
    primary: QColor          # the primary pill button (light)
    primary_fg: QColor       # its ink
    destructive: QColor      # a destructive button's fill
    danger: QColor           # an error in words
    # text
    text: QColor             # ink
    text_2: QColor           # secondary ink
    text_3: QColor           # captions, sources
    text_on_accent: QColor
    # the one accent: displays, live and recording states, the selected moment and key, the speaking wave
    accent: QColor
    accent_soft: QColor      # text selection
    kinds: dict[str, QColor]
    status: dict[str, QColor]
    # data (kept for older callers; the skyline is monochrome)
    series: tuple[QColor, ...]
    series_other: QColor
    grid: QColor
    baseline: QColor
    away: QColor
    off_fill: QColor
    off_hatch: QColor
    unrecorded: QColor
    # Horizon x Field Unit
    panel: QColor = field(default_factory=lambda: hexa(PALETTE["panel"]))
    key: QColor = field(default_factory=lambda: hexa(PALETTE["key"]))
    key_on: QColor = field(default_factory=lambda: hexa(PALETTE["key_on"]))
    line2: QColor = field(default_factory=lambda: hexa(PALETTE["line2"]))
    glass: QColor = field(default_factory=lambda: hexa(PALETTE["glass"]))
    glass_border: QColor = field(default_factory=lambda: hexa(PALETTE["glass_border"]))
    faint: QColor = field(default_factory=lambda: hexa(PALETTE["faint"]))
    led_off: QColor = field(default_factory=lambda: hexa(PALETTE["led_off"]))
    float_border: QColor = field(default_factory=lambda: hexa(PALETTE["float_border"]))
    float_sep: QColor = field(default_factory=lambda: hexa(PALETTE["float_sep"]))
    bubble: QColor = field(default_factory=lambda: hexa(PALETTE["bubble"]))
    menu_ink: QColor = field(default_factory=lambda: hexa(PALETTE["menu_ink"]))
    cool: QColor = field(default_factory=lambda: hexa(PALETTE["cool"]))
    sky: dict[str, QColor] = field(default_factory=dict)
    surfaces: dict[str, Surface] = field(default_factory=dict)

    def level(self, name: str) -> Surface:
        """A surface by role: ``card`` | ``popover`` | ``muted`` | ``input`` | ``selected`` | ``primary`` |
        ``secondary`` | ``sidebar`` | ``display`` | ``key`` | ``key_on``."""
        return self.surfaces.get(name) or self.surfaces["card"]

    def kind(self, name: str | None) -> QColor:
        return self.kinds.get(str(name or ""), self.text_2)

    def series_color(self, index: int | None) -> QColor:
        if index is None or index < 0 or index >= len(self.series):
            return self.series_other
        return self.series[index]

    def accent_alpha(self, a: float) -> QColor:
        c = QColor(self.accent)
        c.setAlphaF(a)
        return c


def _make(accent: str) -> Theme:
    P = {k: hexa(v) for k, v in PALETTE.items()}
    acc = hexa(ACCENTS.get(accent, ACCENTS[DEFAULT_ACCENT]))
    clear = QColor(0, 0, 0, 0)
    text, text_2, text_3 = P["ink"], P["ink2"], P["mute"]
    grey_series = tuple(hexa(v) for v in ("#AEB6C2", "#8C9BB0", "#6F7886", "#56606E", "#3E4858", "#6A7B92",
                                          "#7D899B", "#283140"))
    t = Theme(
        name="dark", dark=True, accent_name=accent if accent in ACCENTS else DEFAULT_ACCENT,
        bg=P["bg"], card=P["panel"], border=P["line"], muted=P["key_on"], subtle=P["key"],
        ring=hexa(PALETTE["ink2"], 0.45),
        primary=P["ink"], primary_fg=P["bg"], destructive=P["destructive"], danger=P["danger"],
        text=text, text_2=text_2, text_3=text_3, text_on_accent=P["bg"],
        accent=acc, accent_soft=hexa("#2A3444"),
        # monochrome: the kind of a card is said in words; only an error has a colour of its own
        kinds={
            "praise": text_2, "nudge": text_2, "reminder": text_2, "review": text_2, "resume": text_2,
            "question": text_2, "joke": text_2, "you": text_2, "unclear": text_2, "warning": text_2,
            "call": P["cool"], "error": P["danger"],
        },
        status={"good": text_2, "warning": text, "serious": text, "critical": P["danger"]},
        series=grey_series, series_other=text_3,
        grid=P["line2"], baseline=P["faint"],
        away=hexa(PALETTE["ink"], 0.28), off_fill=hexa(PALETTE["ink"], 0.03), off_hatch=hexa(PALETTE["ink"], 0.08),
        unrecorded=hexa(PALETTE["ink"], 0.025),
        sky={k: P[k] for k in ("sky_far", "sky_near", "ridge", "sky_base", "sky_gap", "tick", "peak",
                               "call_track", "cool")},
        surfaces={
            "card": Surface(P["panel"], P["line"]),
            "popover": Surface(P["panel"], P["float_border"]),
            "sidebar": Surface(P["bg"], clear),
            "muted": Surface(P["glass"], P["glass_border"]),
            "input": Surface(P["glass"], P["line"]),
            "selected": Surface(P["key_on"], clear),
            "secondary": Surface(P["key"], P["line"]),
            "primary": Surface(P["ink"], clear),
            "display": Surface(P["glass"], P["glass_border"]),
            "key": Surface(P["key"], P["line"]),
            "key_on": Surface(P["key_on"], P["line"]),
        },
    )
    return replace(t, surfaces={k: replace(v, name=k) for k, v in t.surfaces.items()})


@lru_cache(maxsize=None)
def _cached(accent: str) -> Theme:
    return _make(accent)


def accent_name(value: str | None = None) -> str:
    """A valid accent name: ``value`` when it is one, else this process's setting (``ui.json`` ``accent``)."""
    if value in ACCENTS:
        return str(value)
    try:
        from yuki.ui import ui_settings

        chosen = ui_settings.current().accent
    except Exception:
        chosen = DEFAULT_ACCENT
    return chosen if chosen in ACCENTS else DEFAULT_ACCENT


def theme(name: str = "dark", *, accent: str | None = None) -> Theme:
    """The theme with the chosen accent (``accent``, else the user's setting). Yuki is dark only; ``name`` is
    accepted for older callers and ignored."""
    del name
    return _cached(accent_name(accent))


#: The default theme (for module-level callers; the live accent comes from :func:`theme`).
DARK = _cached(DEFAULT_ACCENT)


# -- type ------------------------------------------------------------------------

#: role -> (pixel size, weight word, display cut). Titles are Segoe UI Variable Display, light or regular.
TYPE: dict[str, tuple[int, str, bool]] = {
    "large_title": (36, "Light", True),
    "title": (18, "Regular", True),
    "numeral": (24, "Light", True),
    "headline": (18, "Regular", True),
    "lede": (15, "Regular", False),
    "body": (14, "Regular", False),
    "body_strong": (14, "Semibold", False),
    "callout": (14, "Regular", False),
    "caption": (13, "Regular", False),
    "caption_strong": (13, "Semibold", False),
    "micro": (12, "Regular", False),
    "icon": (16, "Regular", False),
    "icon_small": (12, "Regular", False),
    "icon_large": (18, "Regular", False),
}

_WEIGHTS = {"Light": QFont.Weight.Light, "Regular": QFont.Weight.Normal, "Medium": QFont.Weight.Medium,
            "Semibold": QFont.Weight.DemiBold, "Bold": QFont.Weight.Bold}
#: The weight axis value of each weight word (variable fonts).
_WGHT = {"Light": 300.0, "Regular": 400.0, "Medium": 500.0, "Semibold": 600.0, "Bold": 700.0}


def ensure_fonts() -> bool:
    """Doto and the fallback families, loaded once per process (:func:`yuki.ui.fonts.load_fonts`)."""
    try:
        from yuki.ui.fonts import load_fonts

        return load_fonts()
    except Exception:
        return False


@lru_cache(maxsize=None)
def _families() -> frozenset[str]:
    return frozenset(QFontDatabase.families())


@lru_cache(maxsize=None)
def family() -> str:
    """The UI family: Segoe UI Variable (Windows 11), else Segoe UI."""
    return "Segoe UI Variable" if "Segoe UI Variable" in _families() else "Segoe UI"


def _fallbacks(name: str) -> list[str]:
    from yuki.ui.fonts import FALLBACKS

    return [name] + [f for f in FALLBACKS if f != name]


def variable_font(px: float, weight: str = "Regular", *, display: bool = False) -> QFont:
    """The UI font at ``px`` pixels in a real weight; ``display`` picks the Display optical size (titles).

    Segoe UI Variable gets its ``wght`` / ``opsz`` axes (measured 2026-09-27 on this PC, Qt 6.11 DirectWrite:
    asking the family for ``DemiBold`` renders Bold, and only the axes give the true Semibold). Nirmala UI
    follows it for Devanagari."""
    name = family()
    f = QFont(name)
    f.setFamilies(_fallbacks(name))
    f.setPixelSize(max(1, int(round(px))))
    if name == "Segoe UI Variable":
        f.setVariableAxis(QFont.Tag("wght"), _WGHT.get(weight, 400.0))
        f.setVariableAxis(QFont.Tag("opsz"), 36.0 if display else max(8.0, min(20.0, px * 0.75)))
    f.setWeight(_WEIGHTS.get(weight, QFont.Weight.Normal))
    return f


def rich_font(px: float = 14) -> QFont:
    """The font for rich text (a formatted reply): the family without fixed axes, so a span's
    ``font-weight:600`` picks the real Semibold (fixed axes would pin every span to Regular)."""
    f = QFont(family())
    f.setFamilies(_fallbacks(family()))
    f.setPixelSize(max(1, int(round(px))))
    return f


def font(role: str) -> QFont:
    """The font for a type role (see :data:`TYPE`); Segoe Fluent Icons for glyphs."""
    size, weight, display = TYPE[role]
    if role.startswith("icon"):
        f = QFont("Segoe Fluent Icons" if "Segoe Fluent Icons" in _families() else "Segoe MDL2 Assets")
        f.setPixelSize(size)
        return f
    return variable_font(size, weight, display=display)


def dot_font(px: float, weight: float = 800.0) -> QFont:
    """Doto, the dot-matrix face of the displays (falls back to a monospace face when it is missing)."""
    ensure_fonts()
    f = QFont("Doto")
    f.setFamilies(["Doto", "Consolas"])
    f.setPixelSize(max(1, int(round(px))))
    f.setVariableAxis(QFont.Tag("wght"), weight)
    f.setWeight(QFont.Weight.ExtraBold)
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 103.0)
    return f


def app_font() -> QFont:
    """The application's default font (every plain Qt widget: menus, inputs). By family, without fixed
    axes: an app font with axes would be inherited by every rich-text label and pin its bold spans to
    Regular (measured 2026-09-27). Called right after the application is made, in the tray and the window
    process alike, so it also loads the bundled fonts."""
    ensure_fonts()
    return rich_font(14)


#: Segoe Fluent Icons code points used by the window (Windows 11's icon font).
ICON = {
    "today": "",      # calendar (the Today key)
    "me": "",         # contact
    "yuki": "",       # robot
    "status": "",     # diagnostic
    "chat": "",       # message
    "settings": "",
    "prev": "", "next": "", "down": "", "up": "",
    "calendar": "", "mic": "", "speaker": "", "phone": "", "micro": "",
    "info": "", "edit": "", "delete": "", "people": "",
    "heart": "", "bulb": "", "shield": "", "globe": "",
    "power": "", "moon": "", "question": "", "check": "",
    "close": "", "send": "", "search": "", "more": "",
    "work": "", "game": "", "code": "", "music": "",
    "headphones": "", "pause": "", "play": "", "cloud": "",
    "money": "", "sparkle": "", "history": "", "pin": "",
    "wave": "", "link": "", "warning": "", "lock": "",
    "flag": "", "emoji": "", "book": "", "star": "",
    "keyboard": "", "copy": "", "add": "", "like": "", "palette": "",
    "command": "",
}
