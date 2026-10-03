"""The first-run setup dialog: four short steps in one dark window, in the Lattice design (phase E15).

1. AI model (required): Anthropic API key (default) or AWS Bedrock + region, optional Test.
2. Voice (optional): Deepgram and ElevenLabs keys, or "Skip voice for now".
3. Privacy: an explicit on/off for ambient memory and for call memory.
   Leaving this step saves everything (keys, backend, consent).
4. Download: the memory search model, with a progress bar, or "Download later".

Every write goes through a parameter (``env``, ``local_dir``, ``memory_dir``) so
tests and trial runs point at scratch locations. Slow work (the key test, the
download) runs on plain threads and reports back through queued Qt signals.

The look comes from the Lattice kit (:mod:`yuki.ui.lattice`, painting from the one token source): the graphite
field, bone text, and one live colour (the user's accent, mint by default) only for live things (the current step,
a key that tested fine, the download's head, keyboard focus). Primary keys are lit bone with dark ink. Content sits
in crop-marked clearings (no cards, no shadows); the face is the brand mark in the header; the steps are a
dot-matrix track, the step number is Doto and the download runs along a row of dots. Errors are said in words, in
the brightest ink, never in red.
"""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFontMetricsF, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import yuki.onboarding as ob
from yuki.onboarding import consent as consent_mod
from yuki.onboarding import prefs
from yuki.onboarding.envvars import UserEnv
from yuki.ui import lattice as L
from yuki.ui import tokens as _tokens

# -- look (Lattice: the one token source, yuki/ui/web/tokens.json via yuki.ui.tokens and the yuki.ui.lattice kit) ---
# The wizard runs before any ui.json exists, so the live colour is normally the default accent (mint); L.live()
# follows the user's accent when one is set (a second run after a key was removed).

_C = _tokens.COLOR
_LIVE = _tokens.ACCENTS[_tokens.DEFAULT_ACCENT]["live"]
BG = _C["bg"]
TEXT = _C["text"]
TEXT_2 = _C["text2"]
MUTED = _C["text3"]
LINE = _C["hair2"]
RING = _LIVE             # Lattice: the focus ring is the live colour
ACCENT_LED = _LIVE       # the current step only
ACCENT = TEXT_2          # links: ink, underlined (the live colour is for live states)
GOOD = TEXT_2            # "Key works." in words, after a live check dot
BAD = _C["text"]         # an error in words, in the brightest ink (Lattice has one colour, and it means live)
UI_FAMILY = _tokens.TYPE["family"]["ui"]

#: Keyboard focus moves (Tab, Shift+Tab, a shortcut, arrows in a group): these show the live focus ring; a click
#: does not (the design's ``:focus-visible``).
_KEYBOARD_FOCUS = (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                   Qt.FocusReason.ShortcutFocusReason, Qt.FocusReason.OtherFocusReason)


def _style() -> str:
    """The few things a style sheet does best (label inks, the plain inputs); fonts are set per widget from the kit
    so the variable weights hold, and everything else paints itself."""
    live, sel = L.css(L.live_alpha(0.55)), L.css(L.live_alpha(0.28))
    return f"""
QDialog {{ background: {BG}; }}
QWidget {{ color: {TEXT}; }}
QLabel {{ background: transparent; }}
QLabel#body {{ color: {TEXT_2}; }}
QLabel#hint {{ color: {MUTED}; }}
QLabel#label {{ color: {MUTED}; }}
QLabel#bad {{ color: {BAD}; }}
QLineEdit {{ background: transparent; border: 1px solid {LINE}; border-radius: 12px; padding: 0 12px; color: {TEXT};
            selection-background-color: {sel}; selection-color: {TEXT}; }}
QLineEdit:focus {{ border: 1px solid {live}; }}
QLineEdit#bare, QLineEdit#bare:focus {{ border: none; padding: 0; background: transparent; }}
"""


def _dot_icon(name: str, color: QColor, px: int = 16, dots: int = 10) -> QIcon:
    """A 5x5 dot icon (``dots`` px) centred in a ``px`` icon, drawn at 2x so it stays crisp."""
    pm = QPixmap(px * 2, px * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    off = (px - dots) / 2
    L.paint_icon(p, QRectF(off, off, dots, dots), name, color)
    p.end()
    return QIcon(pm)


class _Ring:
    """Keyboard focus draws a ring in the live colour; a click never does (mixed into the painted controls)."""

    _ring_on = False

    def focusInEvent(self, e: Any) -> None:  # noqa: N802 - Qt naming
        self._ring_on = e.reason() in _KEYBOARD_FOCUS
        super().focusInEvent(e)  # type: ignore[misc]
        self.update()  # type: ignore[attr-defined]

    def focusOutEvent(self, e: Any) -> None:  # noqa: N802 - Qt naming
        self._ring_on = False
        super().focusOutEvent(e)  # type: ignore[misc]
        self.update()  # type: ignore[attr-defined]

    def ring(self) -> bool:
        return self._ring_on and self.hasFocus()  # type: ignore[attr-defined]


class _Key(_Ring, L.Key):
    """A Lattice key (``.key`` / ``.pri`` / ``.ghost``) that takes Tab focus again (a wizard is driven by keyboard
    too), refits when its words change ("Next" -> "Save and continue") and dims a disabled primary key to a raised
    one, so a lit key always means "you can go on"."""

    def __init__(self, text: str = "", *, variant: str = "", size: str = "", icon: str | None = None) -> None:
        super().__init__(text, variant=variant, size=size, icon=icon)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        super().setText(text)
        self._apply()
        self.update()

    def colors(self, hover: bool) -> tuple[QColor, QColor, QColor]:  # noqa: D102
        if self.variant == "pri" and not self.isEnabled():
            return L.RAISE, L.HAIR2, L.TEXT2
        return super().colors(hover)

    def changeEvent(self, e: Any) -> None:  # noqa: N802 - repaint on enable / disable
        super().changeEvent(e)
        if e.type() == QEvent.Type.EnabledChange:
            self.update()

    def paintEvent(self, e: object) -> None:  # noqa: D102
        super().paintEvent(e)
        if self.ring():
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            radius = L.KEY_SIZES.get(self.size_name, L.KEY_SIZES[""])[3]
            p.setPen(QPen(L.live(), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75), radius - 0.75, radius - 0.75)
            p.end()


class _Radio(_Ring, QRadioButton):
    """A radio in the Lattice look (the design's ``.chk``, round): a 14 px ring in hair-2; chosen, a lit ring, a
    field-coloured gap and a lit centre. Words 14 px, bright when chosen."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setFont(L.ui_font(14, 500))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def sizeHint(self) -> QSize:  # noqa: D102
        fm = QFontMetricsF(self.font())
        return QSize(int(round(2 + 14 + 10 + fm.horizontalAdvance(self.text()) + 4)), 26)

    def minimumSizeHint(self) -> QSize:  # noqa: D102
        return self.sizeHint()

    def hitButton(self, pos: Any) -> bool:  # noqa: N802 - the whole row picks it
        return self.rect().contains(pos)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        hover = self.underMouse()
        box = QRectF(2, self.height() / 2 - 7, 14, 14)
        p.setPen(Qt.PenStyle.NoPen)
        if self.isChecked():
            p.setBrush(L.LIT)
            p.drawEllipse(box)
            p.setBrush(L.BG)
            p.drawEllipse(box.adjusted(1, 1, -1, -1))
            p.setBrush(L.LIT)
            p.drawEllipse(box.adjusted(4, 4, -4, -4))
        else:
            p.setPen(QPen(L.TEXT3 if hover else L.HAIR2, 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(box.adjusted(0.5, 0.5, -0.5, -0.5))
        if self.ring():
            p.setPen(QPen(L.live(), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(box.adjusted(-2.5, -2.5, 2.5, 2.5))
        p.setPen(L.TEXT if (self.isChecked() or hover) else L.TEXT2)
        p.setFont(self.font())
        p.drawText(QRectF(2 + 14 + 10, 0, self.width(), self.height()), Qt.AlignmentFlag.AlignVCenter, self.text())
        p.end()


class _Clearing(QFrame):
    """A Lattice clearing: no fill, no border, no shadow, four crop marks at the corners (tokens ``card``)."""

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        L.paint_crop_marks(p, QRectF(self.rect()), L.MARK, float(_tokens.CARD["cropMarkPx"]))
        p.end()


class _DotRule(QWidget):
    """The design's ``.dotrule``: a row of 1 px hair-2 dots every 6 px, where another design would draw a line."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(3)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(L.HAIR2)
        x = 3.0
        while x < self.width():
            p.drawEllipse(QPointF(x, 1.5), 1.0, 1.0)
            x += 6.0
        p.end()


class _Seg(QWidget):
    """The design's ``.seg``: a 1 px hair-2 frame (radius 10) on the field, 3 px padding, its keys 2 px apart."""

    def __init__(self) -> None:
        super().__init__()
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(3, 3, 3, 3)
        self.row.setSpacing(2)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(L.HAIR2, 1.0))
        p.setBrush(L.BG)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 9.5, 9.5)
        p.end()


class _SegKey(_Ring, QPushButton):
    """One choice in a ``.seg``: 26 px, words in text-2; chosen, the raised fill with a hair-2 inset, bright words
    and its small LED lit bone (unchosen, the LED is an unlit dot)."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setCheckable(True)
        self.setFont(L.ui_font(13, 500))
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFixedHeight(26)
        fm = QFontMetricsF(self.font())
        self.setFixedWidth(int(round(12 + 5 + 8 + fm.horizontalAdvance(text) + 14)))

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        on, hover = self.isChecked(), self.underMouse()
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if on or hover:
            p.setPen(QPen(L.HAIR2, 1.0) if on else Qt.PenStyle.NoPen)
            p.setBrush(L.RAISE2 if on else L.RAISE)
            p.drawRoundedRect(r, 6.5, 6.5)
        if self.ring():
            p.setPen(QPen(L.live(), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(L.LIT if on else L.DOT_FACE)
        p.drawEllipse(QPointF(12 + 2.5, self.height() / 2), 2.5, 2.5)
        p.setPen(L.TEXT if (on or hover) else L.TEXT2)
        p.setFont(self.font())
        p.drawText(QRectF(12 + 5 + 8, 0, self.width(), self.height()), Qt.AlignmentFlag.AlignVCenter, self.text())
        p.end()


class _DotBar(QProgressBar):
    """The download's progress as a row of dots (the design's ``.bar``): done dots lit bone, the head in the live
    colour while it runs, the rest unlit. Busy (no size known yet): a short live comet runs along the row on the
    shared Lattice loop, only while it is on screen (one still frame with reduced motion)."""

    PITCH = 7.0
    RADIUS = 1.7

    def __init__(self) -> None:
        super().__init__()
        self._fn = self._frame

    def setRange(self, lo: int, hi: int) -> None:  # noqa: N802 - Qt naming
        super().setRange(lo, hi)
        self._kick()

    def _busy(self) -> bool:
        return self.maximum() == self.minimum()

    def _kick(self) -> None:
        if self._busy() and self.isVisible() and not L.reduced():
            L.Loop.shared().add(self._fn)
        self.update()

    def _frame(self, _t: float) -> bool:
        if not self.isVisible() or not self._busy():
            return False
        self.update()
        return True

    def showEvent(self, e: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(e)
        self._kick()

    def hideEvent(self, e: Any) -> None:  # noqa: N802 - Qt naming
        super().hideEvent(e)
        try:
            L.Loop.shared().remove(self._fn)
        except RuntimeError:   # the shared loop's timer is already gone (the app shutting down)
            pass

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        n = max(1, int((self.width() - 2 * self.RADIUS) // self.PITCH) + 1)
        x0, cy = self.RADIUS + 0.5, self.height() / 2
        live = L.live()
        if self._busy():
            tail = 6
            head = 0.0 if L.reduced() else (L.now() * 22.0) % (n + tail)
        else:
            span = max(1, self.maximum() - self.minimum())
            frac = (self.value() - self.minimum()) / span
            lit = int(round(frac * n))
        for k in range(n):
            c = QColor(L.DOT)
            if self._busy():
                d = head - k
                if 0 <= d < tail:
                    c = QColor(live)
                    c.setAlphaF(1.0 - d / tail * 0.8)
            elif k < lit:
                c = QColor(live) if (k == lit - 1 and lit < n) else QColor(L.LIT)
            p.setBrush(c)
            p.drawEllipse(QPointF(x0 + k * self.PITCH, cy), self.RADIUS, self.RADIUS)
        p.end()


class _Result(QLabel):
    """The key test's answer, in words: a live check before it when the key works; an error in the brightest ink,
    with no icon and no colour."""

    def __init__(self) -> None:
        super().__init__("")
        self.ok = False
        self.setWordWrap(True)
        self.setFont(L.ui_font(13, 500))
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)

    def set_result(self, ok: bool, message: str) -> None:
        self.ok = bool(ok)
        self.setContentsMargins(20 if self.ok else 0, 0, 0, 0)
        self.setStyleSheet(f"color: {GOOD if self.ok else BAD}; background: transparent;")
        self.setText(message)

    def paintEvent(self, e: Any) -> None:  # noqa: D102
        super().paintEvent(e)
        if self.ok:
            p = QPainter(self)
            cy = self.fontMetrics().height() / 2
            L.paint_icon(p, QRectF(0, cy - 6, 12, 12), "check", L.live())
            p.end()


def _face(pitch: int, parent: QWidget) -> QWidget:
    """The face (the Lattice brand mark) at ``pitch`` px per cell, for the header. Hiding it while the app shuts down
    (the shared loop's timer already gone) is quiet."""
    from yuki.ui.dots import FaceWidget

    class _Face(FaceWidget):
        def hideEvent(self, e: Any) -> None:  # noqa: N802 - Qt naming
            try:
                super().hideEvent(e)
            except RuntimeError:
                pass

    return _Face(pitch, parent)


class StepKeys(QWidget):
    """The four steps as a dot-matrix track (the design's working figure): one segment of dots per step, done ones
    lit bone, the current one in the live colour, the ones ahead unlit; each step's name under its segment in mono
    (bright for the current one)."""

    PITCH = 6.0

    def __init__(self, names: tuple[str, ...]) -> None:
        super().__init__()
        self.names = names
        self.current = 0
        self.setFixedHeight(30)

    def set_current(self, index: int) -> None:
        self.current = index
        self.update()

    def text(self) -> str:   # what it says, for tests and screen readers
        return ", ".join(("now: " if i == self.current else "") + n for i, n in enumerate(self.names))

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        f = L.mono_font(11)
        p.setFont(f)
        n, gap = len(self.names), 14.0
        seg = (self.width() - gap * (n - 1)) / max(1, n)
        dots = max(1, int((seg - 3) // self.PITCH) + 1)
        live = L.live()
        for i, name in enumerate(self.names):
            x0 = i * (seg + gap)
            on, done = i == self.current, i < self.current
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(live if on else L.LIT if done else L.DOT_FACE)
            r = 1.6 if (on or done) else 1.3
            for k in range(dots):
                p.drawEllipse(QPointF(x0 + 1.5 + k * self.PITCH, 3.0), r, r)
            p.setPen(L.TEXT if on else L.TEXT2 if done else L.TEXT3)
            p.drawText(QRectF(x0, 10, seg, self.height() - 10),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
        p.end()


LINKS = {
    "anthropic": ("Get a key at console.anthropic.com", "https://console.anthropic.com/settings/keys"),
    "bedrock": ("Bedrock API keys in the AWS console", "https://console.aws.amazon.com/bedrock/home#/api-keys"),
    "deepgram": ("Get a Deepgram key", "https://console.deepgram.com/"),
    "elevenlabs": ("Get an ElevenLabs key", "https://elevenlabs.io/app/settings/api-keys"),
    "google": ("Get a Gemini API key", "https://aistudio.google.com/apikey"),
}

STEP_NAMES = ("AI model", "Voice", "Privacy", "Download")


def default_downloader(progress: Callable[[int, int], None]) -> Any:
    """Fetch the memory search model; ``progress(done, total)`` when the embedder reports it."""
    from yuki.memory import embed

    try:   # the "Yuki" wake-word model too (~18 MB); voice works without it only by hotkey
        from yuki.voice.wake import ensure_model

        ensure_model(progress=progress)
    except Exception:  # noqa: BLE001 - the doctor reports a missing wake model; memory comes first
        pass
    if hasattr(embed, "ensure_downloaded"):
        return embed.ensure_downloaded(progress=progress)
    embedder = embed.Embedder(idle_unload_s=None)
    embedder._ensure_loaded()  # noqa: SLF001 - fallback until ensure_downloaded lands
    embedder.unload()
    return embedder.model_dir


def default_key_checker(backend: str, key: str, region: str) -> tuple[bool, str]:
    from yuki.onboarding.checks import check_model_key

    return check_model_key(backend, key, region)


class _Bridge(QObject):
    """Queued signals from worker threads back to the dialog."""

    tested = Signal(int, bool, str)
    progress = Signal(object, object)
    downloaded = Signal(bool, str)


def _emit(signal: Any, *args: Any) -> None:
    """Emit from a worker thread; quiet when the dialog is already gone ("Download later")."""
    try:
        signal.emit(*args)
    except RuntimeError:
        pass


#: Each label kind's face (Lattice type: Instrument Sans for words, JetBrains Mono for labels).
_FACES: dict[str, Callable[[], Any]] = {
    "title": lambda: L.ui_font(26, 600),
    "body": lambda: L.ui_font(14),
    "hint": lambda: L.ui_font(12.5),
    "section": lambda: L.ui_font(16, 600),
    "bad": lambda: L.ui_font(13, 500),
    "label": lambda: L.mono_font(11),
    "brand": lambda: L.ui_font(15, 600),
}


def _label(text: str, name: str = "body", wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(wrap)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    label.setFont(_FACES.get(name, _FACES["body"])())
    return label


class _Para(L.Para):
    """:class:`yuki.ui.lattice.Para` whose minimum height is all its lines at its width, so a window made smaller
    stops at the text instead of cutting the consent paragraphs short."""

    def minimumSizeHint(self) -> QSize:  # noqa: D102
        w = self.width() if self.width() > 80 else 540
        return QSize(10, self.heightForWidth(w))

    def resizeEvent(self, e: Any) -> None:  # noqa: D102
        super().resizeEvent(e)
        if e.size().width() != e.oldSize().width():
            self.updateGeometry()


def _para(text: str, px: float = 13.0, color: QColor | None = None) -> L.Para:
    """A paragraph with the design's line height (1.5): the explanations under each clearing's title."""
    return _Para(text, font=L.ui_font(px), color=color or L.TEXT2, line_height=1.5)


def _link(kind: str) -> QLabel:
    text, url = LINKS[kind]
    label = QLabel(f'<a href="{url}" style="color:{ACCENT}; text-decoration:underline">{text} &rarr;</a>')
    label.setObjectName("hint")
    label.setFont(_FACES["hint"]())
    label.setOpenExternalLinks(True)
    return label


def _card() -> tuple[QFrame, QVBoxLayout]:
    """A clearing (crop marks, no box) and its column, padded as the design's corner cards (16 x 18)."""
    frame = _Clearing()
    frame.setObjectName("card")
    pad_v, pad_h = _tokens.CARD["paddingPx"]
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(pad_h, pad_v, pad_h, pad_v)
    layout.setSpacing(8)
    return frame, layout


def _placeholder_ink(edit: QLineEdit) -> None:
    pal = edit.palette()
    pal.setColor(QPalette.ColorRole.PlaceholderText, L.TEXT3)
    edit.setPalette(pal)


class SecretField(QWidget):
    """A masked key input with a Show/Hide key inside it: the design's ``.inp`` (42 px, a 1 px hair-2 border,
    radius 12, the live border while it has the keyboard), the key in mono so 0/O and 1/l read apart."""

    def __init__(self, placeholder: str) -> None:
        super().__init__()
        self.setFixedHeight(42)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 0, 8, 0)
        row.setSpacing(8)
        self.edit = QLineEdit()
        self.edit.setObjectName("bare")
        self.edit.setFont(L.mono_font(12.5, 400))
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        _placeholder_ink(self.edit)
        for action in self.edit.findChildren(QAction):   # the clear button: a dot x, not the platform's icon
            if action.objectName() == "_q_qlineeditclearaction":
                action.setIcon(_dot_icon("x", L.TEXT3, 16, 9))
        self.edit.installEventFilter(self)
        self.toggle = _Key("Show", variant="ghost", size="xs")
        self.toggle.setCheckable(True)
        self.toggle.toggled.connect(self._reveal)
        row.addWidget(self.edit, 1)
        row.addWidget(self.toggle)

    def eventFilter(self, obj: Any, event: Any) -> bool:  # noqa: N802 - the border follows the edit's focus
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.update()
        return False

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - the whole box is the field (``cursor: text``)
        self.edit.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(L.live_alpha(0.55) if self.edit.hasFocus() else L.HAIR2, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 11.5, 11.5)
        p.end()

    def _reveal(self, shown: bool) -> None:
        self.edit.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)
        self.toggle.setText("Hide" if shown else "Show")

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, value: str) -> None:  # noqa: N802 - Qt naming
        self.edit.setText(value)


class OnOff(QWidget):
    """An explicit On / Off choice: nothing is picked until the user picks (a ``.seg`` with both LEDs unlit)."""

    changed = Signal()

    def __init__(self, on_text: str = "Turn on", off_text: str = "Keep off") -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 6, 0, 0)
        row.setSpacing(8)
        self.seg = _Seg()
        self.on = _SegKey(on_text)
        self.off = _SegKey(off_text)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for button in (self.on, self.off):
            self.group.addButton(button)
            self.seg.row.addWidget(button)
        row.addWidget(self.seg)
        row.addStretch(1)
        self.group.buttonToggled.connect(lambda *_: self.changed.emit())

    def value(self) -> bool | None:
        if self.on.isChecked():
            return True
        if self.off.isChecked():
            return False
        return None

    def set_value(self, value: bool) -> None:
        (self.on if value else self.off).setChecked(True)


class OnboardingDialog(QDialog):
    """The setup window. ``completed`` is True once everything was saved."""

    def __init__(self, env: UserEnv | None = None, local_dir: str | Path | None = None,
                 memory_dir: str | Path | None = None,
                 downloader: Callable[[Callable[[int, int], None]], Any] | None = None,
                 key_checker: Callable[[str, str, str], tuple[bool, str]] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.env = env or UserEnv()
        self.local_dir = local_dir
        self.memory_dir = memory_dir
        self.downloader = downloader or default_downloader
        self.key_checker = key_checker or default_key_checker
        self.completed = False
        self.saved = False
        self.skipped: set[str] = set()
        self.consent: dict[str, bool] = {}
        self.download_state = "idle"   # idle | running | done | failed
        self._test_token = 0
        self._bridge = _Bridge()
        self._bridge.tested.connect(self._on_tested)
        self._bridge.progress.connect(self._on_progress)
        self._bridge.downloaded.connect(self._on_downloaded)

        self.setWindowTitle("Set up Yuki")
        try:   # the bundled faces (Instrument Sans, JetBrains Mono, Doto) and the Devanagari fallback
            from yuki.ui.fonts import load_fonts

            load_fonts()
        except Exception:
            pass
        self.setStyleSheet(_style())
        self.setFont(L.ui_font(13))
        self.setMinimumWidth(640)
        self.resize(640, 720)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 22, 32, 20)
        outer.setSpacing(0)

        head = QHBoxLayout()   # the brand mark: the face, small, and what this window is
        head.setSpacing(14)
        self.face = _face(6, self)
        self.face.set_state("idle")
        head.addWidget(self.face, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(_label("Set up Yuki", "brand", wrap=False), 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        outer.addLayout(head)
        outer.addSpacing(18)

        self.steps_label = StepKeys(STEP_NAMES)
        outer.addWidget(self.steps_label)
        outer.addSpacing(26)

        titles = QHBoxLayout()   # the step number in Doto beside the step's label, title and line
        titles.setSpacing(18)
        self.step_num = QLabel("01")
        self.step_num.setFont(L.doto_font(46, 900))
        self.step_num.setStyleSheet(f"color: {TEXT}; background: transparent;")
        self.step_num.setFixedWidth(int(QFontMetricsF(self.step_num.font()).horizontalAdvance("00")) + 6)
        self.step_num.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        titles.addWidget(self.step_num, 0, Qt.AlignmentFlag.AlignTop)
        words = QVBoxLayout()
        words.setSpacing(3)
        self.step_caption = _label("", "label", wrap=False)
        self.title = _label("", "title")
        self.subtitle = _label("", "body")
        words.addWidget(self.step_caption)
        words.addWidget(self.title)
        words.addWidget(self.subtitle)
        titles.addLayout(words, 1)
        outer.addLayout(titles)
        outer.addSpacing(20)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._model_page())
        self.stack.addWidget(self._voice_page())
        self.stack.addWidget(self._privacy_page())
        self.stack.addWidget(self._download_page())
        outer.addWidget(self.stack, 1)

        self.error = _label("", "bad")
        self.error.hide()
        outer.addWidget(self.error)
        outer.addSpacing(14)
        outer.addWidget(_DotRule())
        outer.addSpacing(14)

        nav = QHBoxLayout()
        nav.setSpacing(8)
        self.back_btn = _Key("Back", variant="ghost")
        self.skip_btn = _Key("", variant="ghost")
        self.next_btn = _Key("Next", variant="pri")
        self.next_btn.setDefault(True)
        self.back_btn.clicked.connect(self.back)
        self.skip_btn.clicked.connect(self.skip)
        self.next_btn.clicked.connect(self.next)
        nav.addWidget(self.back_btn)
        nav.addStretch(1)
        nav.addWidget(self.skip_btn)
        nav.addWidget(self.next_btn)
        outer.addLayout(nav)

        self._go(0)

    def _mood(self, state: str) -> None:
        """The header face's expression (look only): thinking while a test or the download runs, a check when done."""
        face = getattr(self, "face", None)
        if face is not None:
            face.set_state(state)

    # -- pages -------------------------------------------------------------------------------

    def _model_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)
        card, box = _card()
        box.setSpacing(2)
        self.anthropic_radio = _Radio("Anthropic API key")
        self.bedrock_radio = _Radio("AWS Bedrock (advanced)")
        self.anthropic_radio.setChecked(True)
        self.backend_group = QButtonGroup(page)
        self.backend_group.addButton(self.anthropic_radio)
        self.backend_group.addButton(self.bedrock_radio)
        for radio, hint in ((self.anthropic_radio, "One key from Anthropic. Best for most people."),
                            (self.bedrock_radio, "Use Claude through your own AWS account.")):
            box.addWidget(radio)
            line = _label(hint, "hint")
            line.setContentsMargins(26, 0, 0, 0)   # under the words, past the ring
            box.addWidget(line)
            if radio is self.anthropic_radio:
                box.addSpacing(10)
        col.addWidget(card)

        card, box = _card()
        box.setSpacing(10)
        self.key_title = _label("", "section")
        box.addWidget(self.key_title)
        self.model_key = SecretField("Paste your key here")
        box.addWidget(self.model_key)
        self.region_row = QWidget()
        region = QHBoxLayout(self.region_row)
        region.setContentsMargins(0, 0, 0, 0)
        region.setSpacing(12)
        region.addWidget(_label("AWS region", "label", wrap=False))
        self.region = QLineEdit(ob.DEFAULT_REGION)
        self.region.setFont(L.mono_font(12.5, 400))
        self.region.setFixedHeight(38)
        self.region.setMaximumWidth(180)
        _placeholder_ink(self.region)
        region.addWidget(self.region)
        region.addStretch(1)
        box.addWidget(self.region_row)
        row = QHBoxLayout()
        row.setSpacing(12)
        self.key_link = QLabel()
        self.key_link.setObjectName("hint")
        self.key_link.setFont(_FACES["hint"]())
        self.key_link.setOpenExternalLinks(True)
        row.addWidget(self.key_link, 1)
        self.test_btn = _Key("Test", size="sm")
        self.test_btn.clicked.connect(self.test_key)
        row.addWidget(self.test_btn)
        box.addLayout(row)
        self.test_result = _Result()
        self.test_result.hide()
        box.addWidget(self.test_result)
        box.addWidget(_label("Saved only in your Windows account settings, never in a file.", "label"))
        col.addWidget(card)
        col.addStretch(1)

        self.backend_group.buttonToggled.connect(lambda *_: self._backend_changed())
        self.model_key.edit.textChanged.connect(self._model_edited)
        self.region.textChanged.connect(self._model_edited)
        self._backend_changed()
        return page

    def _voice_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)
        for attr, kind, name, why in (
            ("deepgram_key", "deepgram", "Deepgram", "Hears you: turns your voice into text."),
            ("google_key", "google", "Google Gemini", "Gives Yuki its voice, with laughs and sighs. Without it "
             "Yuki uses a plainer one."),
        ):
            card, box = _card()
            box.setSpacing(8)
            box.addWidget(_label(name, "section"))
            box.addWidget(_para(why))
            box.addSpacing(2)
            field = SecretField(f"{name} key (optional)")
            setattr(self, attr, field)
            box.addWidget(field)
            box.addWidget(_link(kind))
            col.addWidget(card)
        col.addStretch(1)
        return page

    def _privacy_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)
        card, box = _card()
        box.addWidget(_label("Remember what I'm doing", "section"))
        box.addWidget(_para(
            "Yuki reads the window in front (chats, pages, documents) and keeps short notes, so it can help "
            "with what you're doing. Notes stay on this PC, encrypted. To write them, the text goes to the AI "
            "you picked; nothing else leaves the PC. It never reads password managers, banking or sign-in "
            "pages, private windows, or apps and sites you block. You can pause it any time from the tray."))
        self.ambient_choice = OnOff()
        box.addWidget(self.ambient_choice)
        col.addWidget(card)

        card, box = _card()
        box.addWidget(_label("Remember what I say on calls", "section"))
        box.addWidget(_para(
            "Only your own microphone, never the other people, and Yuki asks on each call first. Your voice is "
            "kept encrypted on this PC until it's turned into text there later, then deleted. The AI you "
            "picked writes the notes."))
        self.calls_choice = OnOff()
        box.addWidget(self.calls_choice)
        col.addWidget(card)
        col.addStretch(1)
        self.ambient_choice.changed.connect(self._refresh_nav)
        self.calls_choice.changed.connect(self._refresh_nav)
        return page

    def _download_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)
        card, box = _card()
        box.addWidget(_label("Memory search model", "section"))
        box.addWidget(_para("A few hundred MB, downloaded once. It lets Yuki find things in its memory. "
                            "If you skip it, Yuki downloads it by itself the first time it's needed."))
        self.bar = _DotBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        box.addSpacing(10)
        box.addWidget(self.bar)
        box.addSpacing(2)
        self.download_status = _label("", "label")
        self.download_status.setFont(L.mono_font(11.5))
        self.download_status.setStyleSheet(f"color: {TEXT_2}; background: transparent;")
        box.addWidget(self.download_status)
        col.addWidget(card)
        col.addStretch(1)
        return page

    # -- navigation --------------------------------------------------------------------------

    TITLES = (
        ("Welcome to Yuki", "First, connect Yuki to Claude, the AI it thinks with."),
        ("Talk to Yuki", "Optional. Both services have free plans, and you can add them later."),
        ("Your privacy", "Choose what Yuki may remember. You can change this later."),
        ("Last step", "Getting Yuki's memory ready."),
    )

    def _go(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.steps_label.set_current(index)
        self.step_num.setText(f"{index + 1:02d}")
        self.step_caption.setText(f"step {index + 1} of {len(STEP_NAMES)}")
        self.title.setText(self.TITLES[index][0])
        self.subtitle.setText(self.TITLES[index][1])
        self._show_error("")
        if index == 3 and self.download_state == "idle":
            self.start_download()
        self._refresh_nav()

    @property
    def step(self) -> int:
        return self.stack.currentIndex()

    def _refresh_nav(self) -> None:
        if not hasattr(self, "next_btn"):   # still building the pages
            return
        step = self.step
        self.back_btn.setText("Cancel" if step == 0 else "Back")
        self.back_btn.setVisible(step < 3)
        self.skip_btn.setVisible(step in (1, 3))
        self.skip_btn.setText("Skip voice for now" if step == 1 else "Download later")
        if step == 3:
            self.skip_btn.setVisible(self.download_state == "running")
        if step == 0:
            self.next_btn.setText("Next")
            self.next_btn.setEnabled(self._model_ready())
        elif step == 1:
            self.next_btn.setText("Next")
            self.next_btn.setEnabled(True)
        elif step == 2:
            self.next_btn.setText("Save and continue")
            self.next_btn.setEnabled(self.ambient_choice.value() is not None
                                     and self.calls_choice.value() is not None)
        else:
            self.next_btn.setText("Finish")
            self.next_btn.setEnabled(self.download_state in ("done", "failed"))

    def back(self) -> None:
        if self.step == 0:
            self.reject()
        elif self.step < 3:
            self._go(self.step - 1)

    def skip(self) -> None:
        if self.step == 1:
            self.deepgram_key.setText("")
            self.google_key.setText("")
            self._go(2)
        elif self.step == 3:
            self.skipped.add("download")
            self.finish()

    def next(self) -> None:
        if self.step == 0 and self._model_ready():
            self._go(1)
        elif self.step == 1:
            self._go(2)
        elif self.step == 2:
            if self.save():
                self._go(3)
        elif self.step == 3:
            self.finish()

    def _show_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.setVisible(bool(text))

    # -- step 1 ------------------------------------------------------------------------------

    @property
    def backend(self) -> str:
        return "bedrock" if self.bedrock_radio.isChecked() else "anthropic"

    def _backend_changed(self) -> None:
        bedrock = self.backend == "bedrock"
        self.region_row.setVisible(bedrock)
        self.key_title.setText("Bedrock API key" if bedrock else "Anthropic API key")
        self.model_key.edit.setPlaceholderText("Paste your Bedrock API key" if bedrock else "sk-ant-...")
        text, url = LINKS[self.backend]
        self.key_link.setText(f'<a href="{url}" style="color:{ACCENT}; text-decoration:underline">{text} &rarr;</a>')
        self._model_edited()

    def _model_edited(self) -> None:
        self._test_token += 1          # a result for the old key is stale
        self.test_result.hide()
        if self.download_state != "running":
            self._mood("idle")
        self.test_btn.setEnabled(bool(self.model_key.text()))
        self.test_btn.setText("Test")
        self._refresh_nav()

    def _model_ready(self) -> bool:
        if not self.model_key.text():
            return False
        return self.backend != "bedrock" or bool(self.region.text().strip())

    def test_key(self) -> threading.Thread | None:
        """Run one tiny request with the pasted key on a worker thread."""
        if not self.model_key.text():
            return None
        self._test_token += 1
        token = self._test_token
        backend, key, region = self.backend, self.model_key.text(), self.region.text().strip()
        self.test_btn.setEnabled(False)
        self.test_btn.setText("Testing...")
        self.test_result.hide()
        self._mood("thinking")
        bridge, checker = self._bridge, self.key_checker

        def work() -> None:
            try:
                ok, message = checker(backend, key, region)
            except Exception as exc:  # noqa: BLE001
                ok, message = False, f"Something went wrong ({type(exc).__name__})."
            _emit(bridge.tested, token, bool(ok), str(message))

        thread = threading.Thread(target=work, name="onboarding-key-test", daemon=True)
        thread.start()
        return thread

    def _on_tested(self, token: int, ok: bool, message: str) -> None:
        if token != self._test_token:
            return
        self.test_btn.setEnabled(True)
        self.test_btn.setText("Test")
        self.test_result.set_result(ok, message)   # a live check before the words when it works; no colour if not
        self.test_result.show()
        self._mood("idle")

    # -- step 3: save everything --------------------------------------------------------------

    def save(self) -> bool:
        """Save keys (user env vars), the backend choice and consent. False (with a message) on failure."""
        ambient, calls = self.ambient_choice.value(), self.calls_choice.value()
        if ambient is None or calls is None or not self._model_ready():
            return False
        try:
            if self.backend == "bedrock":
                self.env.set(ob.BEDROCK_KEY, self.model_key.text())
                self.env.set(ob.AWS_REGION, self.region.text().strip())
            else:
                self.env.set(ob.ANTHROPIC_KEY, self.model_key.text())
            voice = [(ob.DEEPGRAM_KEY, self.deepgram_key.text()), (ob.GOOGLE_KEY, self.google_key.text())]
            for name, value in voice:
                if value:
                    self.env.set(name, value)
            if not any(value for _name, value in voice):
                self.skipped.add("voice")
            else:
                self.skipped.discard("voice")
            self.env.commit()
        except (OSError, ValueError) as exc:
            self._show_error(f"Couldn't save the keys in Windows settings ({type(exc).__name__}).")
            return False
        try:
            prefs.save_backend_choice(self.backend, self.local_dir)
            self.consent = consent_mod.save_consent(ambient, calls, self.memory_dir)
        except (OSError, ValueError) as exc:
            self._show_error(f"Couldn't save your choices ({type(exc).__name__}).")
            return False
        self.saved = True
        return True

    # -- step 4: download ---------------------------------------------------------------------

    def start_download(self) -> threading.Thread:
        self.download_state = "running"
        self.bar.setRange(0, 0)   # busy until the first progress report
        self.download_status.setText("Downloading...")
        self._mood("thinking")
        bridge, downloader = self._bridge, self.downloader

        def progress(done: int, total: int) -> None:
            _emit(bridge.progress, done, total)

        def work() -> None:
            try:
                downloader(progress)
            except Exception as exc:  # noqa: BLE001
                _emit(bridge.downloaded, False, type(exc).__name__)
                return
            _emit(bridge.downloaded, True, "")

        thread = threading.Thread(target=work, name="onboarding-download", daemon=True)
        thread.start()
        self._refresh_nav()
        return thread

    def _on_progress(self, done: Any, total: Any) -> None:
        try:
            done, total = int(done), int(total)
        except (TypeError, ValueError):
            return
        if total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(max(0, min(1000, round(1000 * done / total))))
            self.download_status.setText(f"Downloading... {done / 1e6:.0f} of {total / 1e6:.0f} MB")

    def _on_downloaded(self, ok: bool, why: str) -> None:
        if self.download_state != "running":
            return
        self.bar.setRange(0, 1000)
        if ok:
            self.download_state = "done"
            self.bar.setValue(1000)
            self.download_status.setText("Done. Yuki is ready.")
            self._mood("done-flash")
        else:
            self.download_state = "failed"
            self.skipped.add("download")
            self.download_status.setText(f"Couldn't download it right now ({why}). "
                                         "Yuki will try again by itself later.")
            self.download_status.setFont(_FACES["bad"]())   # said plainly, in words and the brightest ink
            self.download_status.setStyleSheet(f"color: {BAD}; background: transparent;")
            self._mood("idle")
        self._refresh_nav()

    # -- finishing ----------------------------------------------------------------------------

    def finish(self) -> None:
        if not self.saved:
            return
        if self.download_state != "done":
            self.skipped.add("download")
        ob.write_marker(self.backend, sorted(self.skipped), self.consent.get("ambient_memory", False),
                        self.consent.get("call_memory", False), self.local_dir)
        self.completed = True
        self.accept()

    def reject(self) -> None:  # the window's close button and Esc
        if self.saved and not self.completed:
            self.finish()      # everything is saved: closing the last step is "Download later"
            return
        super().reject()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        try:  # a dark title bar to match (DWMWA_USE_IMMERSIVE_DARK_MODE)
            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(int(self.winId()), 20, ctypes.byref(value),
                                                       ctypes.sizeof(value))
        except Exception:  # noqa: BLE001 - cosmetic
            pass


__all__ = ["OnboardingDialog", "default_downloader", "default_key_checker"]
