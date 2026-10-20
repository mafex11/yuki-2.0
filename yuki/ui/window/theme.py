"""Yuki's design system in code: the **Lattice** palette and type (2026-10-03), read from the one token source.

One module holds every visual decision the Yuki window and yuki-ui's floating surfaces (overlay, cards,
voice pill, call pill, strip, tray menu) and the first-run wizard make: colour tokens, the type scale,
spacing, corner radii and control sizes. Widgets never invent a colour or a size; they ask the
:class:`Theme` for a role.

**Where the values come from.** ``yuki/ui/web/tokens.json`` (the Lattice design's token file,
docs/ui-mockups/revamp-2026-10/3-lattice/) is the single source; ``python -m yuki.ui.tokens_build`` generates
``yuki/ui/web/tokens.css`` for the HTML window and :mod:`yuki.ui.tokens` for Python, which this module reads.
The role names below are the ones the widgets have always asked for (``card``, ``text_2``, ``key_on``...);
:data:`PALETTE` maps each to a Lattice token, so a widget repaints in Lattice without changing.

The look:

* **Umber and bone** (20 Oct: the website's warm blacks, the user's pick, replacing the graphite ``#0B0B0C``
  series). The field ``#090705`` (panels are the field itself), raised keys ``#120F0C`` / ``#1A1613``, hairlines
  ``#211D1A`` / ``#2F2A26``, resting dots ``#292521``, marks ``#4C4742``, ink ``#F2EFE9`` / ``#B4B0A8`` /
  ``#8A8780``. Floating surfaces are the field with a ``#2F2A26`` border (toasts and the hover card ``#38332E``).
  No gradients, glass, blur, shadows or light mode.
* **One live colour**, ember orange ``#FF6A1A`` (Settings > General > Accent "orange", labelled Ember, the
  default since 20 Oct; "green" is the Lattice mint ``#6FEBC4``, the other choice), and only for something live:
  listening, the working head, recording, needs-you, Stop, the focus ring. Primary keys are lit bone, never
  coloured. Errors are said in words in the brightest ink (Lattice has no red).
* **Type.** Instrument Sans for the UI, JetBrains Mono for labels, times and data, Doto for the dot-matrix
  displays (all bundled, SIL OFL: :mod:`yuki.ui.fonts`); Nirmala UI as the fallback family so Devanagari
  never shows as boxes. Without the bundled files, IBM Plex Sans, Segoe UI Variable, then Segoe UI.

Everything is in logical pixels.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache

from PySide6.QtGui import QColor, QFont, QFontDatabase

from yuki.ui import tokens as T

# -- spacing, radii, sizes ---------------------------------------------------------

#: The 4-px spacing scale.
SPACE = {"xxs": 2, "xs": 4, "s": 8, "m": 12, "l": 16, "xl": 24, "xxl": 32, "xxxl": 48}

#: Corner radii (round 2, the user: pills fully round; squares and rectangles clearly rounded, scaled to their
#: size). Anything pill-shaped (buttons, toggles, segmented choices, keycaps, chips) uses half its height, so
#: it is a capsule at any size; these are the rectangles: modules and dialogs 18-20, displays and keys 16,
#: fields 14, small keys and avatars scale down (:func:`corner`).
RADIUS = {"window": 8, "panel": 18, "card": 18, "display": 16, "key": 16, "row": 12, "control": 999, "field": 14,
          "chip": 999, "tooltip": 12, "floating": 20, "bubble": 16}


def corner(w: float, h: float, radius: float | None = None) -> float:
    """A rectangle's corner radius scaled to its size: ``radius`` (a key's 16 by default) for a large one, down
    to about a third of the short side for a small one, never a capsule."""
    short = min(w, h)
    r = RADIUS["key"] if radius is None else radius
    return max(4.0, min(r, short * 0.3))

#: Control heights (the mockup's pill buttons are 30-32 px tall).
CONTROL_H = {"small": 30, "regular": 32, "large": 36, "input": 34}

#: Motion (milliseconds): short and plain.
MOTION = {"fast": 120, "regular": 150, "slow": 200}

#: Window geometry (logical px). The sidebar is a column of 60 px hardware keys.
WINDOW_SIZE = (1280, 840)
SIDEBAR_W = 96

#: Page padding (mockup ``main``: 34 36 40 32).
PAGE_PAD = (32, 34, 36, 40)

#: The accent choices (Settings > General > Accent): the live colour (``tokens.json`` ``accent``). "orange" is the
#: website's ember orange and the default (20 Oct); "green" is Lattice's signal mint, the other choice.
ACCENTS: dict[str, str] = {name: c["live"] for name, c in T.ACCENTS.items()}
#: Ink on the live colour (a key or chip filled with it).
ACCENT_INK: dict[str, str] = {name: c["liveInk"] for name, c in T.ACCENTS.items()}
#: The accent names as the Settings page words them.
ACCENT_LABELS: dict[str, str] = {name: c["label"] for name, c in T.ACCENTS.items()}
DEFAULT_ACCENT = T.DEFAULT_ACCENT

_C = T.COLOR
#: Every colour role the widgets ask for, as a Lattice token (:mod:`yuki.ui.tokens`). The role names are the
#: older Horizon ones, so no widget had to change; the values are Lattice's.
PALETTE = {
    "bg": _C["bg"], "panel": _C["panel"], "key": _C["raise"], "key_on": _C["raise2"], "line": _C["hair"],
    "line2": _C["raise2"], "glass": _C["bg"], "glass_border": _C["hair"], "ink": _C["text"], "ink2": _C["text2"],
    "mute": _C["text3"], "faint": _C["mark"], "led_off": _C["dot"],
    # floating surfaces: the field with a border (Lattice cards, the island)
    "float_border": _C["surfaceBorder"], "float_sep": _C["hair"], "bubble": _C["raise"], "menu_ink": _C["text"],
    # the skyline and the call band: the dot greys
    "cool": _C["midTone"], "sky_far": _C["raise"], "sky_near": _C["raise2"], "ridge": _C["mark"],
    "sky_base": _C["dot"], "sky_gap": _C["hair2"], "tick": _C["mark"], "peak": _C["text3"], "call_track": _C["raise"],
    # Lattice has one colour, and it means "live": an error is said in words, in the brightest ink
    "danger": _C["text"], "destructive": _C["raise2"],
    # Lattice names for new native code (the island, cards v2)
    "hair2": _C["hair2"], "dot": _C["dot"], "dot_face": _C["dotFace"], "lit": _C["lit"], "mark": _C["mark"],
    "panel_border": _C["panelBorder"],
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
    name = accent if accent in ACCENTS else DEFAULT_ACCENT
    acc = hexa(ACCENTS[name])
    clear = QColor(0, 0, 0, 0)
    text, text_2, text_3 = P["ink"], P["ink2"], P["mute"]
    # the dot greys of the design, lightest to darkest (the skyline is monochrome). (20 Oct: all tokens but the
    # warm grey #6E6B66, which stays on umber as it leans warm like the text greys)
    grey_series = tuple(hexa(v) for v in (_C["text2"], _C["midTone"], _C["text3"], "#6E6B66", _C["mark"],
                                          _C["hair2"], _C["dot"], _C["raise2"]))
    t = Theme(
        name="dark", dark=True, accent_name=name,
        bg=P["bg"], card=P["panel"], border=P["line"], muted=P["key_on"], subtle=P["key"],
        ring=acc,   # Lattice: the focus ring is the live colour
        primary=P["lit"], primary_fg=P["bg"], destructive=P["destructive"], danger=P["danger"],
        text=text, text_2=text_2, text_3=text_3, text_on_accent=hexa(ACCENT_INK[name]),
        accent=acc, accent_soft=hexa(ACCENTS[name], 0.28),
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
        surfaces={   # Lattice: panels are the field; keys and inputs are raised with the brighter hairline
            "card": Surface(P["panel"], P["line"]),
            "popover": Surface(P["bg"], P["float_border"]),
            "sidebar": Surface(P["bg"], clear),
            "muted": Surface(P["glass"], P["glass_border"]),
            "input": Surface(P["key"], P["hair2"]),
            "selected": Surface(P["key_on"], clear),
            "secondary": Surface(P["key"], P["hair2"]),
            "primary": Surface(P["lit"], clear),
            "display": Surface(P["glass"], P["glass_border"]),
            "key": Surface(P["key"], P["hair2"]),
            "key_on": Surface(P["key_on"], P["hair2"]),
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

#: role -> (pixel size, weight word, display cut), at Segoe UI Variable's scale (a wider face is scaled by
#: :data:`FAMILY_SCALE`). Titles Light or Regular; labels Medium ("Semibold" is Plex's Medium, see
#: :data:`FAMILY_WEIGHTS`).
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
#: The UI, data and display families (``tokens.json`` ``type.family``).
UI_FAMILY: str = T.TYPE["family"]["ui"]
MONO_FAMILY: str = T.TYPE["family"]["mono"]
DISPLAY_FAMILY: str = T.TYPE["family"]["display"]
#: Per family: how much smaller to set it than the Segoe scale above (Plex runs wider and has a taller
#: x-height; measured on the Today and Me pages: 0.94 reads the same size and nothing clips). Instrument Sans
#: is set at the design's own sizes (body 14 px).
FAMILY_SCALE: dict[str, float] = {"IBM Plex Sans": 0.94, "Instrument Sans": 1.0}
#: Per family: weight words set lighter or heavier (Plex's Semibold reads as bold; a label is its Medium).
#: Instrument Sans runs 400-700: it has no Light, so Light is its Regular.
FAMILY_WEIGHTS: dict[str, dict[str, float]] = {
    "IBM Plex Sans": {"Semibold": 520.0, "Bold": 600.0, "Light": 300.0},
    "Instrument Sans": {"Light": 400.0},
}


def ensure_fonts() -> bool:
    """Doto and the fallback families, loaded once per process (:func:`yuki.ui.fonts.load_fonts`)."""
    try:
        from yuki.ui.fonts import _LOADED, load_fonts

        before = len(_LOADED)
        ok = load_fonts()
        if len(_LOADED) != before:
            _families.cache_clear()   # the bundled families are in the database now
        return ok
    except Exception:
        return False


@lru_cache(maxsize=None)
def _families() -> frozenset[str]:
    return frozenset(QFontDatabase.families())


#: The UI family in use instead of the built-in (a bundled face once one is picked; the font comparison sets it).
_FAMILY: list[str | None] = [None]


def set_family(name: str | None) -> None:
    """Use ``name`` (a family already in the font database) as the UI family; ``None``: the built-in."""
    _FAMILY[0] = name or None


def family() -> str:
    """The UI family: the chosen one, else the bundled Instrument Sans (Lattice), else IBM Plex Sans, else
    Segoe UI Variable, else Segoe UI."""
    if _FAMILY[0]:
        return _FAMILY[0]
    ensure_fonts()
    fams = _families()
    for name in (UI_FAMILY, "IBM Plex Sans", "Segoe UI Variable"):
        if name in fams:
            return name
    return "Segoe UI"


def mono_font(px: float, weight: float = 500.0) -> QFont:
    """JetBrains Mono, the data face (labels, times, provenance, costs; Lattice 11-12.5 px, weight 500). Loaded
    the first time it is asked for, so a process that never shows data type never loads it."""
    try:
        from yuki.ui.fonts import load_mono

        if load_mono():
            _families.cache_clear()
    except Exception:
        pass
    f = QFont(MONO_FAMILY)
    f.setFamilies([MONO_FAMILY, "Cascadia Mono", "Consolas"])
    f.setPixelSize(max(1, int(round(px))))
    f.setVariableAxis(QFont.Tag("wght"), weight)
    f.setWeight(QFont.Weight(int(weight)))
    return f


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
    f.setPixelSize(max(1, int(round(px * FAMILY_SCALE.get(name, 1.0)))))
    wght = FAMILY_WEIGHTS.get(name, {}).get(weight, _WGHT.get(weight, 400.0))
    f.setVariableAxis(QFont.Tag("wght"), wght)   # a variable face's true weight
    if name == "Segoe UI Variable":
        f.setVariableAxis(QFont.Tag("opsz"), 36.0 if display else max(8.0, min(20.0, px * 0.75)))
    f.setWeight(QFont.Weight(int(wght)) if name in FAMILY_WEIGHTS else _WEIGHTS.get(weight, QFont.Weight.Normal))
    return f


def rich_font(px: float = 14) -> QFont:
    """The font for rich text (a formatted reply): the family without fixed axes, so a span's
    ``font-weight:600`` picks the real Semibold (fixed axes would pin every span to Regular)."""
    name = family()
    f = QFont(name)
    f.setFamilies(_fallbacks(name))
    f.setPixelSize(max(1, int(round(px * FAMILY_SCALE.get(name, 1.0)))))
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
