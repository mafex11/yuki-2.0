"""The Lattice kit for Yuki's native surfaces (the island, the run card, the corner cards, the ask bar, the tray).

Everything here paints from the one token source (:mod:`yuki.ui.tokens`, generated from ``yuki/ui/web/tokens.json``)
and copies the Lattice design's CSS and ``field.js`` constants (``docs/ui-mockups/revamp-2026-10/3-lattice``, gitignored):

* colours (the graphite field, bone text, the one live colour that follows the user's accent);
* type: Instrument Sans for the UI, JetBrains Mono for labels and times, Doto (``ROND`` 100) for timers and counters;
* motion: the design's ease ``cubic-bezier(.2,.7,.1,1)``, its durations, the 30 fps cap, the 1.5 DPR cap and
  "reduce motion" (the app's setting or Windows' own "Show animations" switch: one still frame, no blinks);
* :class:`Loop`: **one** shared 33 ms timer for every animated dot surface. It runs only while a subscriber says it
  has more to draw and stops the moment everything has settled (idle = no timer, no repaint), like the design's loop;
* the dot sprites (16 brightness levels x 3 tones, pre-rendered once per size and blitted per cell);
* the 5x5 dot icons, keys (``.key`` / ``.pri`` / ``.ghost`` / ``.stop``, ``sm`` / ``xs``), chips (``.chip``,
  ``.mem``, ``.rec``), the ``kbd`` hint, crop marks and :class:`Blinker` (the rec dot's and the caret's 2-step blink,
  one timer, only while one is on screen).

Pure presentation: nothing here decides anything.
"""

from __future__ import annotations

import ctypes
import time
from typing import Any, Callable

from PySide6.QtCore import QEasingCurve, QObject, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QWidget

from yuki.ui import tokens as T

# -- colours --------------------------------------------------------------------------------------------


def qcolor(value: Any) -> QColor:
    """``#RRGGBB`` or ``rgba(r,g,b,a)`` (the token file's two forms) as a QColor."""
    text = str(value or "").strip()
    if text.startswith("rgba(") and text.endswith(")"):
        parts = [p.strip() for p in text[5:-1].split(",")]
        r, g, b = (int(float(x)) for x in parts[:3])
        a = float(parts[3]) if len(parts) > 3 else 1.0
        c = QColor(r, g, b)
        c.setAlphaF(max(0.0, min(1.0, a)))
        return c
    return QColor(text)


_C = T.COLOR
BG = qcolor(_C["bg"])
RAISE = qcolor(_C["raise"])
RAISE2 = qcolor(_C["raise2"])
DOT = qcolor(_C["dot"])
DOT_FACE = qcolor(_C["dotFace"])
LIT = qcolor(_C["lit"])
TEXT = qcolor(_C["text"])
TEXT2 = qcolor(_C["text2"])
TEXT3 = qcolor(_C["text3"])
HAIR = qcolor(_C["hair"])
HAIR2 = qcolor(_C["hair2"])
MARK = qcolor(_C["mark"])
BORDER = qcolor(_C["surfaceBorder"])          # #2A2D31: the island, the cards
PANEL_BORDER = qcolor(_C["panelBorder"])      # #34373C: toasts, the hover card
MID = qcolor(_C["midTone"])
#: Hover border of a key (the design's ``#3A3E43``) and the ask bar's border (``#2E3135``): literal in its CSS.
KEY_HOVER_BORDER = QColor("#3A3E43")
ASKBAR_BORDER = QColor("#2E3135")


def _accent() -> dict[str, Any]:
    try:
        from yuki.ui import ui_settings

        name = ui_settings.current().accent
    except Exception:
        name = T.DEFAULT_ACCENT
    return T.ACCENTS.get(name) or T.ACCENTS[T.DEFAULT_ACCENT]


def live() -> QColor:
    """The one live colour (the user's accent; mint by default): listening, the working head, rec, needs-you, Stop."""
    return qcolor(_accent()["live"])


def live_ink() -> QColor:
    return qcolor(_accent()["liveInk"])


def live_alpha(alpha: float) -> QColor:
    """The live colour at ``alpha`` (the design's ``rgba(111,235,196,.45)`` borders and its ``.12`` fill)."""
    c = live()
    c.setAlphaF(alpha)
    return c


def css(c: QColor) -> str:
    """A QColor as a style-sheet colour."""
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})"


# -- motion ---------------------------------------------------------------------------------------------

MOTION = T.MOTION
DUR: dict[str, int] = dict(MOTION["durationMs"])
FPS: int = int(MOTION["fpsCap"])
FRAME_MS: int = int(round(1000.0 / FPS))
DPR_CAP: float = float(MOTION["dprCap"])


def ease_curve() -> QEasingCurve:
    """The design's ease, ``cubic-bezier(.2,.7,.1,1)`` (tokens ``motion.ease``)."""
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(0.2, 0.7), QPointF(0.1, 1.0), QPointF(1.0, 1.0))
    return curve


_EASE = None


def ease(x: float) -> float:
    """The design's ease at progress ``x`` (0..1)."""
    global _EASE
    if _EASE is None:
        _EASE = ease_curve()
    return float(_EASE.valueForProgress(max(0.0, min(1.0, x))))


def _windows_animations_off() -> bool:
    """Windows' "Show animations in Windows" switched off (``SPI_GETCLIENTAREAANIMATION``): the OS's reduced motion."""
    try:
        value = ctypes.c_int(1)
        ok = ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0)
        return bool(ok) and not value.value
    except Exception:
        return False


#: A test or a still render may pin reduced motion on or off (None: the settings and Windows decide).
REDUCED_OVERRIDE: list[bool | None] = [None]


def reduced() -> bool:
    """Reduce motion: the app's setting (``ui.json``) or Windows' own switch; one still frame, no blinks."""
    if REDUCED_OVERRIDE[0] is not None:
        return bool(REDUCED_OVERRIDE[0])
    try:
        from yuki.ui import ui_settings

        if ui_settings.current().reduce_motion:
            return True
    except Exception:
        pass
    return _windows_animations_off()


# -- type -----------------------------------------------------------------------------------------------


def ui_font(px: float, weight: float = 400.0) -> QFont:
    """Instrument Sans (the UI face) at ``px`` pixels and a true variable weight (400-700)."""
    from yuki.ui.window.theme import ensure_fonts, family

    ensure_fonts()
    name = family()
    f = QFont(name)
    from yuki.ui.fonts import FALLBACKS

    f.setFamilies([name, *[x for x in FALLBACKS if x != name]])
    f.setPixelSize(max(1, int(round(px))))
    f.setVariableAxis(QFont.Tag("wght"), float(weight))
    f.setWeight(QFont.Weight(int(weight)))
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def mono_font(px: float, weight: float = 500.0) -> QFont:
    """JetBrains Mono (labels, times, provenance, costs)."""
    from yuki.ui.window.theme import mono_font as _mono

    f = _mono(px, weight)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def doto_font(px: float, weight: float = 800.0, spacing_em: float = 0.02) -> QFont:
    """Doto with ``ROND`` 100 (the design's ``.doto``): timers, counters, step numbers."""
    from yuki.ui.window.theme import ensure_fonts

    ensure_fonts()
    f = QFont("Doto")
    f.setFamilies(["Doto", "Consolas"])
    f.setPixelSize(max(1, int(round(px))))
    f.setVariableAxis(QFont.Tag("wght"), float(weight))
    f.setVariableAxis(QFont.Tag("ROND"), 100.0)
    f.setWeight(QFont.Weight(int(min(900, weight))))
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, px * spacing_em)
    return f


def font_px(f: QFont) -> float:
    return float(f.pixelSize() if f.pixelSize() > 0 else f.pointSizeF() * 4 / 3)


# -- the one loop ---------------------------------------------------------------------------------------


def stop_timer(timer: QTimer | None) -> None:
    """Stop a shared timer from a hide: at interpreter exit Qt deletes the timers before the widgets it hides."""
    from shiboken6 import isValid

    if timer is not None and isValid(timer):
        timer.stop()


class Loop(QObject):
    """One timer for every animated dot surface (the design's ``Loop``): at most 30 frames a second.

    A subscriber is ``fn(t) -> bool`` (``t``: seconds on one clock); it returns True while it has more to draw.
    The timer runs only while at least one wants more, so a settled screen costs nothing. :meth:`add` kicks it."""

    _shared: "Loop | None" = None

    def __init__(self) -> None:
        super().__init__()
        self.subs: dict[int, Callable[[float], bool]] = {}
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self.tick)
        #: Perf counters (frames, total ms inside frames, the worst frame), for the report's numbers.
        self.ticks = 0
        self.ms = 0.0
        self.max_ms = 0.0
        self.draws = 0

    @classmethod
    def shared(cls) -> "Loop":
        if cls._shared is None:
            cls._shared = Loop()
        return cls._shared

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def add(self, fn: Callable[[float], bool]) -> None:
        self.subs[id(fn)] = fn
        if not self._timer.isActive():
            self._timer.start()
            QTimer.singleShot(0, self.tick)   # the first frame now, as the design's kick

    def remove(self, fn: Callable[[float], bool]) -> None:
        self.subs.pop(id(fn), None)
        if not self.subs:
            stop_timer(self._timer)

    def tick(self) -> None:
        t0 = time.perf_counter()
        t = now()
        more = False
        for fn in list(self.subs.values()):
            try:
                if fn(t):
                    more = True
            except RuntimeError:   # a widget gone underneath (C++ side deleted)
                self.subs = {k: v for k, v in self.subs.items() if v is not fn}
        dt = (time.perf_counter() - t0) * 1000.0
        self.ticks += 1
        self.ms += dt
        self.max_ms = max(self.max_ms, dt)
        if not more or reduced():
            self._timer.stop()


#: The animation clock (seconds). A still render pins it (:func:`set_clock`) so a frame is reproducible.
_CLOCK: list[Callable[[], float] | None] = [None]


def now() -> float:
    fn = _CLOCK[0]
    return fn() if fn is not None else time.monotonic()


def set_clock(fn: Callable[[], float] | None) -> None:
    """Pin the animation clock (tests and side-by-side renders); ``None`` goes back to the real one."""
    _CLOCK[0] = fn


class Blinker(QObject):
    """The design's 2-step blinks (``@keyframes rec { 50% { opacity: .3 } }``, ``steps(2, jump-none)``): the rec dot
    (1.6 s) and the caret (1 s). One timer per period, running only while a widget that blinks is on screen."""

    _by_period: dict[int, "Blinker"] = {}

    def __init__(self, period_ms: int) -> None:
        super().__init__()
        self.period_ms = period_ms
        self.on = True
        self.widgets: list[QWidget] = []
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, period_ms // 2))
        self._timer.timeout.connect(self._flip)

    @classmethod
    def of(cls, period_ms: int) -> "Blinker":
        b = cls._by_period.get(period_ms)
        if b is None:
            b = cls._by_period[period_ms] = Blinker(period_ms)
        return b

    def watch(self, w: QWidget) -> None:
        if w not in self.widgets:
            self.widgets.append(w)
        if not reduced() and not self._timer.isActive():
            self.on = True
            self._timer.start()

    def unwatch(self, w: QWidget) -> None:
        if w in self.widgets:
            self.widgets.remove(w)
        if not self.widgets:
            stop_timer(self._timer)
            self.on = True

    @property
    def active(self) -> bool:
        return self._timer.isActive()

    def _flip(self) -> None:
        self.on = not self.on
        for w in list(self.widgets):
            try:
                w.update()
            except RuntimeError:
                self.widgets.remove(w)


# -- dot sprites ----------------------------------------------------------------------------------------

#: Tones of a lit dot (``field.js`` ``COL``): bone, the live colour, the mid grey.
TONE_LIT, TONE_LIVE, TONE_MID = 0, 1, 2
LEVELS = int(T.FIELD["spriteLevels"])
_SPRITES: dict[tuple, QPixmap] = {}


def sprites(size: int, max_r: float, min_r: float, crisp: bool, dpr: float = 1.0) -> QPixmap:
    """The design's ``makeSprites``: one sheet of ``LEVELS`` x 3 dots (lit, live, mid), ``size`` device pixels per
    cell, radius ``min_r + (max_r - min_r) * v ** 0.8`` and alpha ``0.2 + 0.8 v``; ``crisp`` adds the faint 0.12 v
    halo the big face cells have. Cached per size and colour."""
    tones = (LIT, live(), MID)
    key = (size, round(max_r, 3), round(min_r, 3), crisp, round(dpr, 3), tuple(c.rgb() for c in tones))
    sheet = _SPRITES.get(key)
    if sheet is not None:
        return sheet
    img = QImage(size * LEVELS, size * 3, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    for row, c in enumerate(tones):
        for lv in range(LEVELS):
            v = (lv + 1) / LEVELS
            r = min_r + (max_r - min_r) * v ** 0.8
            cx, cy = lv * size + size / 2, row * size + size / 2
            if crisp:
                halo = QColor(c)
                halo.setAlphaF(0.12 * v)
                p.setBrush(halo)
                rr = min(size / 2, r + 1.2 * dpr)
                p.drawEllipse(QPointF(cx, cy), rr, rr)
            body = QColor(c)
            body.setAlphaF(0.2 + 0.8 * v)
            p.setBrush(body)
            p.drawEllipse(QPointF(cx, cy), r, r)
    p.end()
    sheet = QPixmap.fromImage(img)
    if len(_SPRITES) > 24:
        _SPRITES.clear()
    _SPRITES[key] = sheet
    return sheet


def dpr_of(widget: QWidget | None) -> float:
    """The widget's device pixel ratio, capped at the design's 1.5."""
    try:
        d = float(widget.devicePixelRatioF()) if widget is not None else 1.0
    except Exception:
        d = 1.0
    return max(1.0, min(DPR_CAP, d))


# -- dot icons (field.js ICONS) ---------------------------------------------------------------------------

ICONS: dict[str, str] = {
    "home": "00100 01110 11111 01010 01110", "work": "11111 00000 11111 00000 11100", "me": "01110 01110 00000 11111 11111",
    "playbook": "10000 11000 11100 11110 11111", "activity": "00000 00010 10101 01000 00000",
    "settings": "01110 10001 10101 10001 01110", "chat": "11111 10001 10001 11111 01000",
    "stop": "00000 01110 01110 01110 00000", "mic": "01110 01110 01110 00100 01110",
    "check": "00000 00001 00010 10100 01000", "x": "10001 01010 00100 01010 10001", "swap": "00010 11111 00000 11111 01000",
    "chev": "01000 00100 00010 00100 01000", "plus": "00100 00100 11111 00100 00100",
    "memory": "01010 10101 01010 10101 01010", "lesson": "01110 10001 10001 01110 00100",
    "pin": "01110 01110 11111 00100 00100", "search": "01110 10001 10001 01110 00001",
    "day": "11111 10101 11111 10101 11111", "music": "00111 00101 00100 11100 11100", "call": "01110 10001 10001 11011 11011",
    "rec": "01110 11111 11111 11111 01110", "key": "11111 10001 10101 10001 11111", "up": "00100 01110 10101 00100 00100",
    "window": "11111 11111 10001 10001 11111", "copy": "11100 10111 11101 00101 00111", "todo": "10111 00000 10111 00000 10111",
    "eye": "00000 01110 11011 01110 00000", "hand": "01010 01010 11111 11111 01110", "undo": "00100 01000 11111 01001 00101",
    "front": "11111 10001 10001 10001 11111", "pause": "00000 01010 01010 01010 00000", "bolt": "00010 00100 01110 00100 01000",
    "dash": "00000 00000 01110 00000 00000", "dot": "00000 00000 00100 00000 00000", "retry": "01110 10001 10101 10001 01111",
    "play": "01000 01100 01110 01100 01000", "restart": "01110 10001 10111 10000 01110", "edit": "00011 00111 01110 11100 11000",
    "person": "01110 01110 00100 01110 10101",
}


def paint_icon(p: QPainter, rect: QRectF, name: str, color: QColor, *, ghost: bool = False,
               rotate: float = 0.0) -> None:
    """A 5x5 dot icon in ``rect`` (the design's 10x10 viewBox: lit dots r .82, ghost dots r .5 at 22%)."""
    rows = (ICONS.get(name) or ICONS["dot"]).split(" ")
    s = min(rect.width(), rect.height()) / 10.0
    ox = rect.x() + (rect.width() - 10 * s) / 2
    oy = rect.y() + (rect.height() - 10 * s) / 2
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    if rotate:
        c = rect.center()
        p.translate(c)
        p.rotate(rotate)
        p.translate(-c)
    g = QColor(color)
    g.setAlphaF(color.alphaF() * 0.22)
    for j in range(5):
        for i in range(5):
            on = rows[j][i] == "1"
            if on:
                p.setBrush(color)
                p.drawEllipse(QPointF(ox + (i * 2 + 1) * s, oy + (j * 2 + 1) * s), 0.82 * s, 0.82 * s)
            elif ghost:
                p.setBrush(g)
                p.drawEllipse(QPointF(ox + (i * 2 + 1) * s, oy + (j * 2 + 1) * s), 0.5 * s, 0.5 * s)
    p.restore()


class Icon(QWidget):
    """A dot icon as a widget (``.ic``: 13 px; 10 in a chip; 12 on Stop). ``spin``: the running step's spinner
    (``spin 1.2s steps(8)``), turned by the shared :class:`Blinker`-like step timer."""

    def __init__(self, name: str, size: int = 13, color: QColor | None = None, parent: QWidget | None = None, *,
                 ghost: bool = False) -> None:
        super().__init__(parent)
        self.name, self.color, self.ghost = name, color, ghost
        self.angle = 0.0
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_color(self, color: QColor | None) -> None:
        self.color = color
        self.update()

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        paint_icon(p, QRectF(self.rect()), self.name, self.color or TEXT, ghost=self.ghost, rotate=self.angle)
        p.end()


class Spinner(Icon):
    """The running step's mark: the ``retry`` icon turning in 8 steps per 1.2 s (the design's ``.spin``)."""

    _timer: QTimer | None = None
    _all: list["Spinner"] = []

    def __init__(self, size: int = 11, parent: QWidget | None = None) -> None:
        super().__init__("retry", size, live(), parent)

    @classmethod
    def _step(cls) -> None:
        for s in list(cls._all):
            try:
                s.angle = (s.angle + 45.0) % 360.0
                s.update()
            except RuntimeError:
                cls._all.remove(s)

    def showEvent(self, e: object) -> None:  # noqa: D102 - turns only while on screen
        super().showEvent(e)
        if self not in Spinner._all:
            Spinner._all.append(self)
        if Spinner._timer is None:
            Spinner._timer = QTimer()
            Spinner._timer.setInterval(150)    # 1.2 s / 8 steps
            Spinner._timer.timeout.connect(Spinner._step)
        if not reduced() and not Spinner._timer.isActive():
            Spinner._timer.start()

    def hideEvent(self, e: object) -> None:  # noqa: D102
        super().hideEvent(e)
        if self in Spinner._all:
            Spinner._all.remove(self)
        if not Spinner._all:
            stop_timer(Spinner._timer)


# -- keys, chips, kbd ------------------------------------------------------------------------------------

#: ``.key`` sizes: height, side padding, font px, radius (``sm`` 26 / 10 / 12.5, ``xs`` 22 / 8 / 11.5 / 6).
KEY_SIZES: dict[str, tuple[int, int, float, int]] = {"": (30, 12, 13.0, 8), "sm": (26, 10, 12.5, 8),
                                                     "xs": (22, 8, 11.5, 6)}


class Key(QPushButton):
    """A Lattice key (the design's ``.key``): ``variant`` ``""`` (raised), ``pri`` (lit bone, dark ink), ``ghost``
    (words only) or ``stop`` (the live outline); ``size`` ``""`` / ``sm`` / ``xs``; an optional dot icon before the
    words and a ``kbd`` hint after them. Never takes the keyboard (the surfaces never take focus)."""

    def __init__(self, text: str = "", *, variant: str = "", size: str = "", icon: str | None = None,
                 kbd: str = "", parent: QWidget | None = None, count: str = "") -> None:
        super().__init__(text, parent)
        self.variant, self.size_name, self.icon_name, self.kbd, self.count = variant, size, icon, kbd, count
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._apply()

    def _apply(self) -> None:
        h, _pad, px, _r = KEY_SIZES.get(self.size_name, KEY_SIZES[""])
        self.setFont(ui_font(px, 600 if self.variant == "pri" else 500))
        self.setFixedHeight(h)
        self.setFixedWidth(self.sizeHint().width())

    def set_parts(self, text: str | None = None, *, icon: str | None = "", kbd: str | None = None,
                  variant: str | None = None, count: str | None = None) -> None:
        if text is not None:
            self.setText(text)
        if icon != "":
            self.icon_name = icon
        if kbd is not None:
            self.kbd = kbd
        if variant is not None:
            self.variant = variant
        if count is not None:
            self.count = count
        self._apply()
        self.update()

    def _pad(self) -> int:
        _h, pad, _px, _r = KEY_SIZES.get(self.size_name, KEY_SIZES[""])
        return 8 if self.variant == "ghost" else pad

    def _icon_px(self) -> int:
        return 12 if self.variant == "stop" else 13

    def sizeHint(self) -> QSize:  # noqa: D102
        fm = QFontMetricsF(self.font())
        w = 2 * self._pad() + fm.horizontalAdvance(self.text())
        parts = 1 if self.text() else 0
        if self.icon_name:
            w += self._icon_px()
            parts += 1
        if self.count:
            w += max(9.0, QFontMetricsF(doto_font(14)).horizontalAdvance(self.count)) + 2
            parts += 1
        if self.kbd:
            w += QFontMetricsF(mono_font(10.5)).horizontalAdvance(self.kbd) + 10 + 2 + 2
            parts += 1
        w += 7 * max(0, parts - 1)
        h, _pad, _px, _r = KEY_SIZES.get(self.size_name, KEY_SIZES[""])
        return QSize(int(round(w + 2)), h)

    def colors(self, hover: bool) -> tuple[QColor, QColor, QColor]:
        """``(fill, border, ink)`` for the variant (and hover)."""
        v = self.variant
        if v == "pri":
            return (QColor("#FFFFFF") if hover else LIT), LIT, QColor(_C["bg"])
        if v == "ghost":
            return (RAISE if hover else QColor(0, 0, 0, 0)), QColor(0, 0, 0, 0), (TEXT if hover else TEXT2)
        if v == "stop":
            return live_alpha(0.12), live_alpha(0.45), live()
        return (RAISE2 if hover else RAISE), (KEY_HOVER_BORDER if hover else HAIR2), TEXT

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        hover = self.underMouse() and self.isEnabled()
        fill, border, ink = self.colors(hover)
        if not self.isEnabled():
            ink = QColor(ink)
            ink.setAlphaF(ink.alphaF() * 0.55)
        _h, _pad, _px, radius = KEY_SIZES.get(self.size_name, KEY_SIZES[""])
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, radius - 0.5, radius - 0.5)
        p.fillPath(path, fill)
        if border.alpha():
            p.setPen(QPen(border, 1.0))
            p.drawPath(path)
        x = float(self._pad()) + 1
        cy = self.height() / 2
        if self.icon_name:
            s = self._icon_px()
            paint_icon(p, QRectF(x, cy - s / 2, s, s), self.icon_name, ink)
            x += s + 7
        if self.text():
            p.setPen(ink)
            p.setFont(self.font())
            fm = QFontMetricsF(self.font())
            tw = fm.horizontalAdvance(self.text())
            p.drawText(QRectF(x, 0, tw + 2, self.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       self.text())
            x += tw + 7
        if self.count:
            f = doto_font(14)
            p.setFont(f)
            p.setPen(ink)
            cw = max(9.0, QFontMetricsF(f).horizontalAdvance(self.count))
            p.drawText(QRectF(x + 2, 0, cw, self.height()), Qt.AlignmentFlag.AlignCenter, self.count)
            x += cw + 2 + 7
        if self.kbd:
            paint_kbd(p, QPointF(x + 2, cy), self.kbd)
        p.end()


class Check(QPushButton):
    """The design's ``.chk``: a 14 px box (radius 4, hair-2 border; checked: lit with a 3 px field-coloured inset)
    and 12.5 px words in text-2. Checkable; never takes the keyboard."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(ui_font(12.5))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._fit()

    def _fit(self) -> None:
        fm = QFontMetricsF(self.font())
        self.setFixedSize(int(round(2 + 14 + 8 + fm.horizontalAdvance(self.text()) + 2 + 1)), 26)

    def setText(self, text: str) -> None:  # noqa: D102
        super().setText(text)
        self._fit()

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        box = QRectF(2.5, (self.height() - 14) / 2 + 0.5, 13, 13)
        if self.isChecked():   # a lit border, a 3 px field-coloured inset, a lit centre
            outer = QRectF(2, (self.height() - 14) / 2, 14, 14)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(LIT)
            p.drawRoundedRect(outer, 4, 4)
            p.setBrush(BG)
            p.drawRoundedRect(outer.adjusted(1, 1, -1, -1), 3, 3)
            p.setBrush(LIT)
            p.drawRect(outer.adjusted(4, 4, -4, -4))
        else:
            p.setPen(QPen(HAIR2, 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(box, 3.5, 3.5)
        p.setPen(TEXT if self.underMouse() else TEXT2)
        p.setFont(self.font())
        p.drawText(QRectF(2 + 14 + 8, 0, self.width(), self.height()), Qt.AlignmentFlag.AlignVCenter, self.text())
        p.end()


def kbd_size(text: str) -> QSize:
    fm = QFontMetricsF(mono_font(10.5))
    return QSize(int(round(fm.horizontalAdvance(text) + 10 + 2)), int(round(10.5 + 6 + 2)))


def paint_kbd(p: QPainter, left_center: QPointF, text: str) -> QRectF:
    """The ``.kbd`` hint: mono 10.5 px, text-3, a 1 px hair-2 border, radius 4, padding 3 x 5. Returns its box."""
    size = kbd_size(text)
    r = QRectF(left_center.x(), left_center.y() - size.height() / 2, size.width(), size.height())
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(QPen(HAIR2, 1.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 3.5, 3.5)
    p.setPen(TEXT3)
    p.setFont(mono_font(10.5))
    p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
    p.restore()
    return r


class Kbd(QWidget):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.text = text
        self.setFixedSize(kbd_size(text))

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        paint_kbd(p, QPointF(0, self.height() / 2), self.text)
        p.end()


class Chip(QWidget):
    """A chip (the design's ``.chip``): 24 px, a pill, a 1 px hair-2 border (dashed for ``mem``), mono 11.5 px in
    text-2, an optional 10 px dot icon. ``bold`` is a first part in bright text (``.chip.ev b``)."""

    def __init__(self, text: str = "", *, icon: str | None = None, kind: str = "", bold: str = "",
                 parent: QWidget | None = None, max_width: int = 0) -> None:
        super().__init__(parent)
        self.text, self.icon_name, self.kind, self.bold, self.max_width = text, icon, kind, bold, max_width
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._fit()

    def set_text(self, text: str, bold: str | None = None) -> None:
        self.text = text
        if bold is not None:
            self.bold = bold
        self._fit()
        self.update()

    def _fit(self) -> None:
        f = mono_font(11.5)
        fm = QFontMetricsF(f)
        w = 18.0 + fm.horizontalAdvance(self.text) + (fm.horizontalAdvance(self.bold) + (fm.horizontalAdvance(" ")
                                                                                        if self.text else 0)
                                                    if self.bold else 0)
        if self.icon_name:
            w += 10 + 6
        w += 2
        if self.max_width:
            w = min(w, self.max_width)
        self.setFixedSize(int(round(w)), 24)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        pen = QPen(live_alpha(0.4) if self.kind == "rec" else HAIR2, 1.0)
        if self.kind == "mem":
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([3.0, 3.0])
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        x = 10.0
        ink = live() if self.kind == "rec" else TEXT2
        if self.icon_name:
            paint_icon(p, QRectF(x, 7, 10, 10), self.icon_name, ink)
            x += 16
        f = mono_font(11.5)
        p.setFont(f)
        fm = QFontMetricsF(f)
        room = self.width() - x - 9
        if self.bold:
            p.setPen(TEXT)
            bw = fm.horizontalAdvance(self.bold)
            p.drawText(QRectF(x, 0, bw + 1, self.height()), Qt.AlignmentFlag.AlignVCenter, self.bold)
            x += bw + (fm.horizontalAdvance(" ") if self.text else 0)
            room = self.width() - x - 9
        p.setPen(ink)
        text = fm.elidedText(self.text, Qt.TextElideMode.ElideRight, room + 1)
        p.drawText(QRectF(x, 0, room + 2, self.height()), Qt.AlignmentFlag.AlignVCenter, text)
        p.end()


class RecChip(QWidget):
    """The rec chip (``.chip.rec``): the blinking live dot and the call's running time in Doto 15 px, live colour.
    The time is set from outside (one clock, the call session's); the dot blinks through :class:`Blinker`."""

    def __init__(self, parent: QWidget | None = None, *, dot: int = 7, px: float = 15.0, border: bool = True) -> None:
        super().__init__(parent)
        self.text = "00:00"
        self.dot, self.px, self.border = dot, px, border
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._fit()

    def set_time(self, text: str) -> None:
        if text != self.text:
            self.text = text
            self._fit()
            self.update()

    def _fit(self) -> None:
        fm = QFontMetricsF(doto_font(self.px, 800, 0.04))
        w = (9 + self.dot + 6 + fm.horizontalAdvance(self.text) + 9 + 2) if self.border else (
            self.dot + 8 + fm.horizontalAdvance(self.text) + 2)
        self.setFixedSize(int(round(w)), 24 if self.border else int(self.px + 8))

    def showEvent(self, e: object) -> None:  # noqa: D102
        super().showEvent(e)
        Blinker.of(1600).watch(self)

    def hideEvent(self, e: object) -> None:  # noqa: D102
        super().hideEvent(e)
        Blinker.of(1600).unwatch(self)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = live()
        x = 0.0
        if self.border:
            r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            p.setPen(QPen(live_alpha(0.4), 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
            x = 9.0
        dot = QColor(c)
        dot.setAlphaF(1.0 if Blinker.of(1600).on else 0.3)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(dot)
        p.drawEllipse(QRectF(x, self.height() / 2 - self.dot / 2, self.dot, self.dot))
        x += self.dot + 6
        f = doto_font(self.px, 800, 0.04)
        p.setFont(f)
        p.setPen(c)
        p.drawText(QRectF(x, 0, self.width() - x, self.height()), Qt.AlignmentFlag.AlignVCenter, self.text)
        p.end()


def paint_crop_marks(p: QPainter, rect: QRectF, color: QColor = MARK, size: float = 9.0) -> None:
    """The clearings' frame: four corners of two 1 px hairlines, ``size`` long (tokens ``card.cropMarkPx``)."""
    p.save()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    x0, y0, x1, y1 = rect.left(), rect.top(), rect.right(), rect.bottom()
    for (x, y, sx, sy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        hx = x if sx > 0 else x - size
        vy = y if sy > 0 else y - size
        p.drawRect(QRectF(hx, y if sy > 0 else y - 1, size, 1))
        p.drawRect(QRectF(x if sx > 0 else x - 1, vy, 1, size))
    p.restore()


class Label(QLabel):
    """A plain-text label in a Lattice face and colour (``elide``: one line, cut with an ellipsis)."""

    def __init__(self, text: str = "", *, font: QFont | None = None, color: QColor | None = None,
                 parent: QWidget | None = None, wrap: bool = False, elide: bool = False, rich: bool = False) -> None:
        super().__init__(parent)
        self._full = text
        self._elide = elide
        self.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
        if font is not None:
            self.setFont(font)
        self.set_color(color or TEXT)
        self.setWordWrap(wrap)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        if elide:
            self.setMinimumWidth(10)
            self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def set_color(self, color: QColor) -> None:
        self.setStyleSheet(f"color: {css(color)}; background: transparent;")

    def full_text(self) -> str:
        return self._full

    def setText(self, text: str) -> None:  # noqa: D102
        self._full = text
        if self._elide:
            super().setText(self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, max(10, self.width())))
        else:
            super().setText(text)

    def resizeEvent(self, e: object) -> None:  # noqa: D102
        super().resizeEvent(e)
        if self._elide:
            super().setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight,
                                                          max(10, self.width())))


class Span:
    """A run of a :class:`Para`'s text drawn differently: a colour, a weight, the caption's dotted underline (an
    unsure word) or a different face (``font``)."""

    __slots__ = ("start", "length", "color", "weight", "dotted", "font")

    def __init__(self, start: int, length: int, *, color: QColor | None = None, weight: float | None = None,
                 dotted: bool = False, font: QFont | None = None) -> None:
        self.start, self.length, self.color, self.weight, self.dotted, self.font = start, length, color, weight, \
            dotted, font


class Para(QWidget):
    """Wrapped text with the design's CSS line height (``line-height: 1.45`` etc.: half-leading above and below
    each line), optional :class:`Span` runs and the caption's caret. Height follows the width (``heightForWidth``),
    so a layout gives it room the way the browser does. ``max_lines`` with ``elide`` cuts the last line with an
    ellipsis (one-line labels)."""

    def __init__(self, text: str = "", *, font: QFont | None = None, color: QColor | None = None,
                 line_height: float = 1.45, parent: QWidget | None = None, max_lines: int = 0,
                 caret: bool = False, elide_left: bool = False) -> None:
        super().__init__(parent)
        self._text = text
        self._spans: list[Span] = []
        self._font = font or ui_font(14)
        self._color = color or TEXT
        self._lh = float(line_height)
        self._max_lines = max_lines
        self._elide_left = elide_left
        self.caret = caret
        self._cache: tuple[int, list[tuple[Any, float]]] | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        sp = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    # -- content ---------------------------------------------------------------------------------------

    def text(self) -> str:
        return self._text

    def set_text(self, text: str, spans: list[Span] | None = None) -> None:
        if text == self._text and (spans or []) == self._spans:
            return
        self._text, self._spans = text, list(spans or [])
        self._cache = None
        self.updateGeometry()
        self.update()

    def set_color(self, color: QColor) -> None:
        self._color = color
        self.update()

    def set_font(self, font: QFont) -> None:
        self._font = font
        self._cache = None
        self.updateGeometry()
        self.update()

    def set_caret(self, on: bool) -> None:
        if on != self.caret:
            self.caret = on
            if on and self.isVisible():
                Blinker.of(1000).watch(self)
            elif not on:
                Blinker.of(1000).unwatch(self)
            self.update()

    def showEvent(self, e: object) -> None:  # noqa: D102
        super().showEvent(e)
        if self.caret:
            Blinker.of(1000).watch(self)

    def hideEvent(self, e: object) -> None:  # noqa: D102
        super().hideEvent(e)
        Blinker.of(1000).unwatch(self)

    # -- layout ----------------------------------------------------------------------------------------

    def line_px(self) -> float:
        return font_px(self._font) * self._lh

    def _formats(self) -> list[Any]:
        from PySide6.QtGui import QTextCharFormat, QTextLayout

        out = []
        for s in self._spans:
            fmt = QTextCharFormat()
            if s.font is not None:
                fmt.setFont(s.font)
            elif s.weight is not None:
                f = QFont(self._font)
                f.setVariableAxis(QFont.Tag("wght"), float(s.weight))
                f.setWeight(QFont.Weight(int(s.weight)))
                fmt.setFont(f)
            if s.color is not None:
                fmt.setForeground(s.color)
            r = QTextLayout.FormatRange()
            r.start, r.length, r.format = s.start, s.length, fmt
            out.append(r)
        return out

    def laid_chars(self, width: int) -> int:
        """How many characters of the text the lines at ``width`` hold (fewer than its length when ``max_lines`` cut
        it), so a caller can end a cut text with a visible ellipsis."""
        lines = self._lines(max(1, int(width)))
        if not lines:
            return 0
        last = lines[-1][0]
        return int(last.textStart() + last.textLength())

    def _lines(self, width: int) -> list[tuple[Any, float]]:
        """The laid-out lines for ``width``: ``(QTextLine, top)`` (cached per width)."""
        if self._cache is not None and self._cache[0] == width:
            return self._cache[1]
        from PySide6.QtGui import QTextLayout, QTextOption

        text = self._text
        if self._max_lines == 1:
            fm = QFontMetricsF(self._font)
            text = fm.elidedText(text, Qt.TextElideMode.ElideLeft if self._elide_left else Qt.TextElideMode.ElideRight,
                                 max(1, width - (14 if self.caret else 0)))
        layout = QTextLayout(text, self._font)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WordWrap if self._max_lines != 1 else QTextOption.WrapMode.NoWrap)
        layout.setTextOption(opt)
        if self._spans and text == self._text:
            layout.setFormats(self._formats())
        lh = self.line_px()
        fm = QFontMetricsF(self._font)
        content = fm.ascent() + fm.descent()
        lines: list[tuple[Any, float]] = []
        layout.beginLayout()
        y = 0.0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(max(1, width - (11 if self.caret else 0)))
            top = y + (lh - content) / 2
            line.setPosition(QPointF(0, top))
            lines.append((line, y))
            y += lh
            if self._max_lines and len(lines) >= self._max_lines:
                break
        layout.endLayout()
        self._layout_keep = layout
        self._cache = (width, lines)
        return lines

    def hasHeightForWidth(self) -> bool:  # noqa: D102
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: D102
        n = max(1, len(self._lines(max(1, width)))) if self._text or self.caret else 1
        return int(round(n * self.line_px()))

    def natural_width(self) -> float:
        fm = QFontMetricsF(self._font)
        w = fm.horizontalAdvance(self._text)
        for s in self._spans:
            if s.weight is not None or s.font is not None:
                w += fm.averageCharWidth() * 0.05 * s.length
        return w + (11 if self.caret else 0)

    def sizeHint(self) -> QSize:  # noqa: D102
        w = int(round(self.natural_width())) + 1
        return QSize(w, self.heightForWidth(w))

    def minimumSizeHint(self) -> QSize:  # noqa: D102
        return QSize(10, int(round(self.line_px())))

    def resizeEvent(self, e: object) -> None:  # noqa: D102
        super().resizeEvent(e)
        self._cache = None

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.setPen(self._color)
        lines = self._lines(self.width())
        for line, _top in lines:
            line.draw(p, QPointF(0, 0))
        dotted = [s for s in self._spans if s.dotted]
        if dotted and lines:
            c = QColor(TEXT3)
            c.setAlphaF(0.7)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            for s in dotted:
                for line, _top in lines:
                    a, b = line.textStart(), line.textStart() + line.textLength()
                    lo, hi = max(a, s.start), min(b, s.start + s.length)
                    if lo >= hi:
                        continue
                    x0, _ = line.cursorToX(lo)
                    x1, _ = line.cursorToX(hi)
                    base = line.position().y() + line.ascent() + 4.0
                    x = float(x0) + 0.5
                    while x < float(x1):
                        p.drawEllipse(QPointF(x, base), 0.75, 0.75)
                        x += 3.0
        if self.caret and Blinker.of(1000).on:
            if lines:
                line, _top = lines[-1]
                end = line.textStart() + line.textLength()
                x, _ = line.cursorToX(end)
                base = line.position().y() + line.ascent()
                x = float(x) + (3.0 if self._text else 0.0)
            else:
                fm = QFontMetricsF(self._font)
                x, base = 0.0, (self.line_px() - (fm.ascent() + fm.descent())) / 2 + fm.ascent()
            h = font_px(self._font)
            p.fillRect(QRectF(x, base + 2 - h, 8, h), live())
        p.end()


__all__ = [
    "ASKBAR_BORDER", "BG", "BORDER", "Blinker", "Chip", "DOT", "DOT_FACE", "DUR", "FPS", "FRAME_MS", "HAIR", "HAIR2",
    "Para", "Span",
    "ICONS", "Icon", "Kbd", "Key", "LIT", "Label", "Loop", "MARK", "MID", "PANEL_BORDER", "RAISE", "RAISE2",
    "RecChip", "Spinner", "TEXT", "TEXT2", "TEXT3", "css", "doto_font", "dpr_of", "ease", "ease_curve", "live",
    "live_alpha", "live_ink", "mono_font", "now", "paint_crop_marks", "paint_icon", "paint_kbd", "qcolor", "reduced",
    "set_clock", "sprites", "ui_font",
]
