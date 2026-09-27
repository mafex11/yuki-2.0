"""Yuki's design system: shadcn/ui's dark zinc theme, flat, in code.

One module holds every visual decision the Yuki window and yuki-ui's floating
surfaces (overlay, cards, voice pill, call pill, strip, tray menu) make: colour
tokens, the type scale, spacing, corner radii and control sizes. Widgets never
invent a colour or a size; they ask the :class:`Theme` for a role.

The look (2026-09-27, replacing Liquid Glass at the user's request: "just use
simple shadcn ui everywhere"): dark only, flat, opaque.

* **Colour.** shadcn's zinc dark tokens: background ``#09090b``; cards and
  popovers the same colour with a 1 px ``#27272a`` border; muted surfaces and
  hover ``#27272a`` / ``#18181b``; text ``#fafafa``, muted text ``#a1a1aa``; the
  primary button ``#fafafa`` on ``#18181b`` ink; destructive red; a ``#d4d4d8``
  focus ring. No gradients, glow, blur or translucency.
* **Shape.** ``--radius: 0.5rem``: cards 8 px, buttons and inputs 6 px, the
  switch and avatars fully round.
* **Type.** Inter when installed, else Segoe UI Variable (with its real weight
  and optical-size axes), else Segoe UI: 14 px body, 12-13 px muted captions,
  600-weight headings.
* **Spacing.** A 4 px scale; pages and cards use 16 / 24 px.

Everything is in logical pixels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from PySide6.QtGui import QColor, QFont, QFontDatabase

# -- spacing, radii, sizes ---------------------------------------------------------

#: The 4-px spacing scale.
SPACE = {"xxs": 2, "xs": 4, "s": 8, "m": 12, "l": 16, "xl": 24, "xxl": 32, "xxxl": 48}

#: Corner radii (shadcn ``--radius: 0.5rem``: ``rounded-lg`` 8, ``rounded-md`` 6, ``rounded-sm`` 4).
RADIUS = {"window": 8, "panel": 8, "card": 8, "row": 6, "control": 6, "chip": 4, "tooltip": 6}

#: Control heights (shadcn Button ``sm`` 32, default 36, ``lg`` 40; Input 36).
CONTROL_H = {"small": 32, "regular": 36, "large": 40, "input": 36}

#: Motion (milliseconds): short and plain.
MOTION = {"fast": 120, "regular": 150, "slow": 200}

#: Window geometry (logical px).
WINDOW_SIZE = (1280, 840)
SIDEBAR_W = 232


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
    """Every colour role."""

    name: str
    dark: bool
    # shadcn tokens
    bg: QColor               # --background
    card: QColor             # --card / --popover
    border: QColor           # --border / --input
    muted: QColor            # --muted / --secondary / --accent (hover)
    subtle: QColor           # a quieter muted surface (#18181b): hover on a card, insets
    ring: QColor             # --ring
    primary: QColor          # --primary
    primary_fg: QColor       # --primary-foreground
    destructive: QColor      # --destructive (a button's fill)
    danger: QColor           # destructive text (errors in words)
    # text
    text: QColor             # --foreground
    text_2: QColor           # --muted-foreground
    text_3: QColor           # captions a step quieter
    text_on_accent: QColor
    # accents (monochrome: the selection, the spinner, links)
    accent: QColor
    accent_soft: QColor      # text selection, a menu item under the pointer
    kinds: dict[str, QColor]
    status: dict[str, QColor]
    # data (the timeline's lanes)
    series: tuple[QColor, ...]
    series_other: QColor
    grid: QColor
    baseline: QColor
    away: QColor
    off_fill: QColor
    off_hatch: QColor
    unrecorded: QColor
    surfaces: dict[str, Surface] = field(default_factory=dict)

    def level(self, name: str) -> Surface:
        """A surface by role: ``card`` | ``popover`` | ``muted`` | ``input`` | ``selected`` | ``primary`` |
        ``secondary`` | ``sidebar``."""
        return self.surfaces.get(name) or self.surfaces["card"]

    def kind(self, name: str | None) -> QColor:
        return self.kinds.get(str(name or ""), self.text_2)

    def series_color(self, index: int | None) -> QColor:
        if index is None or index < 0 or index >= len(self.series):
            return self.series_other
        return self.series[index]


# The timeline's categorical lane colours: the validated reference palette's dark slots (dataviz skill,
# references/palette.md). Data, not decoration: every lane also carries a text label.
_SERIES_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767")

#: Status colours are reserved (never a series) and always ship with words.
_STATUS = {"good": "#22c55e", "warning": "#f59e0b", "serious": "#f97316", "critical": "#ef4444"}


def _dark() -> Theme:
    bg = hexa("#09090b")
    border = hexa("#27272a")
    muted = hexa("#27272a")
    subtle = hexa("#18181b")
    text, text_2, text_3 = hexa("#fafafa"), hexa("#a1a1aa"), hexa("#8b8b94")
    clear = QColor(0, 0, 0, 0)
    return Theme(
        name="dark", dark=True,
        bg=bg, card=bg, border=border, muted=muted, subtle=subtle, ring=hexa("#d4d4d8"),
        primary=hexa("#fafafa"), primary_fg=hexa("#18181b"), destructive=hexa("#7f1d1d"), danger=hexa("#ef4444"),
        text=text, text_2=text_2, text_3=text_3, text_on_accent=hexa("#18181b"),
        accent=hexa("#fafafa"), accent_soft=hexa("#3f3f46"),
        # monochrome by default; colour only where it carries meaning (an error, a warning, a call on the map)
        kinds={
            "praise": text_2, "nudge": text_2, "reminder": text_2, "review": text_2, "resume": text_2,
            "question": text_2, "joke": text_2, "you": text_2, "unclear": hexa("#f59e0b"),
            "warning": hexa("#f59e0b"), "call": hexa("#14b8a6"), "error": hexa("#ef4444"),
        },
        status={k: hexa(v) for k, v in _STATUS.items()},
        series=tuple(hexa(v) for v in _SERIES_DARK), series_other=hexa("#71717a"),
        grid=border, baseline=hexa("#3f3f46"),
        away=rgba(255, 255, 255, 0.32), off_fill=rgba(255, 255, 255, 0.03), off_hatch=rgba(255, 255, 255, 0.10),
        unrecorded=rgba(255, 255, 255, 0.025),
        surfaces={
            "card": Surface(bg, border),
            "popover": Surface(bg, border),
            "sidebar": Surface(bg, clear),
            "muted": Surface(subtle, clear),
            "input": Surface(bg, border),
            "selected": Surface(muted, clear),
            "secondary": Surface(muted, clear),
            "primary": Surface(hexa("#fafafa"), clear),
        },
    )


def _named(t: Theme) -> Theme:
    from dataclasses import replace

    return replace(t, surfaces={k: replace(v, name=k) for k, v in t.surfaces.items()})


DARK = _named(_dark())


def theme(name: str = "dark") -> Theme:
    """The theme. Yuki is dark only (2026-09-27); the name is accepted for older callers and ignored."""
    del name
    return DARK


# -- type ------------------------------------------------------------------------

#: role -> (pixel size, weight word). shadcn's scale: one 24 px page title, 18 px section titles, 15 px card
#: titles, 14 px body, 13 px muted captions.
TYPE: dict[str, tuple[int, str]] = {
    "large_title": (24, "Semibold"),
    "title": (18, "Semibold"),
    "numeral": (24, "Semibold"),
    "headline": (15, "Semibold"),
    "body": (14, "Regular"),
    "body_strong": (14, "Semibold"),
    "callout": (14, "Regular"),
    "caption": (13, "Regular"),
    "caption_strong": (13, "Semibold"),
    "micro": (12, "Medium"),
    "icon": (16, "Regular"),
    "icon_small": (13, "Regular"),
    "icon_large": (18, "Regular"),
}

_WEIGHTS = {"Light": QFont.Weight.Light, "Regular": QFont.Weight.Normal, "Medium": QFont.Weight.Medium,
            "Semibold": QFont.Weight.DemiBold, "Bold": QFont.Weight.Bold}
#: The weight axis value of each weight word (variable fonts).
_WGHT = {"Light": 300.0, "Regular": 400.0, "Medium": 500.0, "Semibold": 600.0, "Bold": 700.0}


@lru_cache(maxsize=None)
def _families() -> frozenset[str]:
    return frozenset(QFontDatabase.families())


@lru_cache(maxsize=None)
def family() -> str:
    """The UI family: Inter when installed, else Segoe UI Variable, else Segoe UI."""
    families = _families()
    for name in ("Inter", "Inter Variable", "Segoe UI Variable"):
        if name in families:
            return name
    return "Segoe UI"


def variable_font(px: float, weight: str = "Regular") -> QFont:
    """The UI font at ``px`` pixels in a real weight.

    Segoe UI Variable gets its ``wght`` / ``opsz`` axes (measured 2026-09-27 on this PC, Qt 6.11 DirectWrite:
    asking the family for ``DemiBold`` renders Bold, and only the axes give the true Semibold); Inter and Segoe
    UI get the plain weight.
    """
    name = family()
    f = QFont(name)
    f.setPixelSize(max(1, int(round(px))))
    if name == "Segoe UI Variable":
        f.setVariableAxis(QFont.Tag("wght"), _WGHT.get(weight, 400.0))
        f.setVariableAxis(QFont.Tag("opsz"), max(8.0, min(36.0, px * 0.75)))
    f.setWeight(_WEIGHTS.get(weight, QFont.Weight.Normal))
    return f


def rich_font(px: float = 14) -> QFont:
    """The font for rich text (a formatted reply): the family without fixed axes, so a span's
    ``font-weight:600`` picks the real Semibold (fixed axes would pin every span to Regular)."""
    f = QFont(family())
    f.setPixelSize(max(1, int(round(px))))
    return f


def font(role: str) -> QFont:
    """The font for a type role (see :data:`TYPE`); Segoe Fluent Icons for glyphs."""
    size, weight = TYPE[role]
    if role.startswith("icon"):
        f = QFont("Segoe Fluent Icons" if "Segoe Fluent Icons" in _families() else "Segoe MDL2 Assets")
        f.setPixelSize(size)
        return f
    return variable_font(size, weight)


def app_font() -> QFont:
    """The application's default font (every plain Qt widget: menus, inputs). By family, without fixed
    axes: an app font with axes would be inherited by every rich-text label and pin its bold spans to
    Regular (measured 2026-09-27)."""
    return rich_font(14)


#: Segoe Fluent Icons code points used by the window (Windows 11's icon font).
ICON = {
    "today": "\ue823",      # clock
    "me": "\ue77b",         # contact
    "yuki": "\ue99a",       # robot
    "status": "\ue9d9",     # diagnostic
    "chat": "\ue8bd",       # message
    "settings": "\ue713",
    "prev": "\ue76b", "next": "\ue76c", "down": "\ue70d",
    "calendar": "\ue787", "mic": "\ue720", "speaker": "\ue767", "phone": "\ue717",
    "info": "\ue946", "edit": "\ue70f", "delete": "\ue74d", "people": "\ue716",
    "heart": "\ueb51", "bulb": "\uea80", "shield": "\uea18", "globe": "\ue774",
    "power": "\ue7e8", "moon": "\ue708", "question": "\ue897", "check": "\ue73e",
    "close": "\ue711", "send": "\ue724", "search": "\ue721", "more": "\ue712",
    "work": "\ue821", "game": "\ue7fc", "code": "\ue943", "music": "\uec4f",
    "headphones": "\ue7f6", "pause": "\ue769", "play": "\ue768", "cloud": "\ue753",
    "money": "\ue825", "sparkle": "\ue945", "history": "\ue81c", "pin": "\ue718",
    "wave": "\ue9e9", "link": "\ue71b", "warning": "\ue7ba", "lock": "\ue72e",
    "flag": "\ue7c1", "emoji": "\ue76e", "book": "\ue82d", "star": "\ue734",
}
