"""The first-run setup dialog: four short steps in one dark window (Horizon x Field Unit, as the app).

1. AI model (required): Anthropic API key (default) or AWS Bedrock + region, optional Test.
2. Voice (optional): Deepgram and ElevenLabs keys, or "Skip voice for now".
3. Privacy: an explicit on/off for ambient memory and for call memory.
   Leaving this step saves everything (keys, backend, consent).
4. Download: the memory search model, with a progress bar, or "Download later".

Every write goes through a parameter (``env``, ``local_dir``, ``memory_dir``) so
tests and trial runs point at scratch locations. Slow work (the key test, the
download) runs on plain threads and reports back through queued Qt signals.
Colours and the UI family come from the one token source (:mod:`yuki.ui.tokens`, plain Python, no Qt) rather
than from ``yuki.ui.window.theme``, so this window does not depend on the Qt theme. The steps are four small
hardware keys across the top; the current one's LED is lit in the live colour (the default accent: the wizard
runs before any setting).
"""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
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
from yuki.ui import tokens as _tokens
from yuki.onboarding import prefs
from yuki.onboarding.envvars import UserEnv

# -- look (Lattice: the one token source, yuki/ui/web/tokens.json via yuki.ui.tokens) -------------------------
# The palette only; the wizard's own re-skin is Phase E15. It runs before any ui.json exists: default accent.

_C = _tokens.COLOR
_LIVE = _tokens.ACCENTS[_tokens.DEFAULT_ACCENT]["live"]
_LIVE_RGB = tuple(int(_LIVE[i:i + 2], 16) for i in (1, 3, 5))
BG = _C["bg"]
PANEL = _C["panel"]
FIELD = _C["raise"]
LINE = _C["hair2"]
KEY = _C["raise"]
KEY_ON = _C["raise2"]
MUTED_BG = _C["raise"]
TEXT = _C["text"]
TEXT_2 = _C["text2"]
MUTED = _C["text3"]
FAINT = _C["mark"]
LED_OFF = _C["dot"]
PRIMARY = _C["lit"]
PRIMARY_FG = _C["bg"]
RING = _LIVE             # Lattice: the focus ring is the live colour
ACCENT_LED = _LIVE       # the current step's LED only
ACCENT = TEXT_2          # links: ink, underlined (the live colour is for live states)
GOOD = TEXT_2            # "Key works." in words, no colour
BAD = _C["text"]         # an error in words, in the brightest ink (Lattice has one colour, and it means live)
SELECTION = f"rgba({_LIVE_RGB[0]},{_LIVE_RGB[1]},{_LIVE_RGB[2]},0.28)"
UI_FAMILY = _tokens.TYPE["family"]["ui"]
FONT_FAMILIES = [UI_FAMILY, "IBM Plex Sans", "Segoe UI", "Nirmala UI"]
FONT = ", ".join(f'"{f}"' for f in FONT_FAMILIES)   # the bundled UI face (yuki.ui.fonts), loaded by the wizard
FONT_DISPLAY = FONT

STYLE = f"""
QDialog {{ background: {BG}; }}
QWidget {{ color: {TEXT}; font-family: {FONT}; font-size: 13px; }}
QLabel#title {{ font-family: {FONT_DISPLAY}; font-size: 32px; font-weight: 300; }}
QLabel#body {{ color: {TEXT_2}; font-size: 14px; }}
QLabel#hint {{ color: {MUTED}; font-size: 13px; }}
QLabel#section {{ font-family: {FONT_DISPLAY}; font-weight: 400; font-size: 17px; }}
QLabel#bad {{ color: {BAD}; }}
QFrame#card {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 18px; }}
QLineEdit {{ background: {FIELD}; border: 1px solid {LINE}; border-radius: 14px; padding: 7px 14px;
            selection-background-color: {SELECTION}; }}
QLineEdit:focus {{ border: 1px solid {RING}; }}
QPushButton {{ background: transparent; color: {TEXT_2}; border: 1px solid {LINE}; border-radius: 17px;
              padding: 6px 16px; min-height: 20px; font-size: 13px; }}
QPushButton:hover {{ background: {KEY}; color: {TEXT}; }}
QPushButton:disabled {{ color: {FAINT}; background: transparent; }}
QPushButton#primary {{ background: {PRIMARY}; border: 1px solid {PRIMARY}; color: {PRIMARY_FG}; font-weight: 500; }}
QPushButton#primary:hover {{ background: #FFFFFF; border-color: #FFFFFF; }}
QPushButton#primary:disabled {{ background: {FAINT}; border-color: transparent; color: {KEY_ON}; }}
QPushButton#flat {{ background: transparent; border: none; color: {MUTED}; }}
QPushButton#flat:hover {{ color: {TEXT}; background: {KEY}; }}
QPushButton#small {{ padding: 5px 12px; font-size: 13px; }}
QPushButton#choice {{ padding: 6px 16px; min-width: 64px; }}
QPushButton#choice:checked {{ background: {PRIMARY}; border: 1px solid {PRIMARY}; color: {PRIMARY_FG}; font-weight: 500; }}
QRadioButton {{ spacing: 10px; font-weight: 500; }}
QRadioButton::indicator {{ width: 14px; height: 14px; border-radius: 8px; border: 1px solid {MUTED}; background: {FIELD}; }}
QRadioButton::indicator:checked {{ width: 6px; height: 6px; border: 5px solid {PRIMARY}; background: {BG}; }}
QProgressBar {{ background: {KEY}; border: 1px solid {LINE}; border-radius: 4px; height: 8px; max-height: 8px;
               text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {TEXT_2}; border-radius: 3px; }}
"""


class StepKeys(QWidget):
    """The four steps as small hardware keys, the current one lit (its LED in the accent), done ones with a
    quiet LED. No numbers: each key says its step."""

    def __init__(self, names: tuple[str, ...]) -> None:
        super().__init__()
        self.names = names
        self.current = 0
        self.setFixedHeight(40)

    def set_current(self, index: int) -> None:
        self.current = index
        self.update()

    def text(self) -> str:   # what it says, for tests and screen readers
        return ", ".join(("now: " if i == self.current else "") + n for i, n in enumerate(self.names))

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        f = QFont(UI_FAMILY)
        f.setFamilies(FONT_FAMILIES)
        f.setPixelSize(12)
        p.setFont(f)
        fm = QFontMetrics(f)
        x = 0.0
        for i, name in enumerate(self.names):
            w = fm.horizontalAdvance(name) + 40
            r = QRectF(x, 2, w, 34)
            on = i == self.current
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(LINE))
            p.drawRoundedRect(r, 17, 17)   # a capsule key
            p.setBrush(QColor(KEY_ON if on else KEY))
            p.drawRoundedRect(r.adjusted(1, 1, -1, -2), 16, 16)
            led = ACCENT_LED if on else (TEXT_2 if i < self.current else LED_OFF)
            p.setBrush(QColor(led))
            p.drawEllipse(QPointF(r.left() + 14, r.center().y() - 0.5), 2.6, 2.6)
            p.setPen(QColor(TEXT if on else MUTED))
            p.drawText(r.adjusted(24, 0, -8, -2), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)
            x += w + 8
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


def _label(text: str, name: str = "body", wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(wrap)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    return label


def _link(kind: str) -> QLabel:
    text, url = LINKS[kind]
    label = QLabel(f'<a href="{url}" style="color:{ACCENT}; text-decoration:underline">{text} &rarr;</a>')
    label.setObjectName("hint")
    label.setOpenExternalLinks(True)
    return label


def _card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(22, 20, 22, 20)
    layout.setSpacing(8)
    return frame, layout


class SecretField(QWidget):
    """A masked line edit with a Show/Hide button."""

    def __init__(self, placeholder: str) -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        self.toggle = QPushButton("Show")
        self.toggle.setObjectName("small")
        self.toggle.setCheckable(True)
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.toggled.connect(self._reveal)
        row.addWidget(self.edit, 1)
        row.addWidget(self.toggle)

    def _reveal(self, shown: bool) -> None:
        self.edit.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)
        self.toggle.setText("Hide" if shown else "Show")

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, value: str) -> None:  # noqa: N802 - Qt naming
        self.edit.setText(value)


class OnOff(QWidget):
    """An explicit On / Off choice: nothing is picked until the user picks."""

    changed = Signal()

    def __init__(self, on_text: str = "Turn on", off_text: str = "Keep off") -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 4, 0, 0)
        row.setSpacing(8)
        self.on = QPushButton(on_text)
        self.off = QPushButton(off_text)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for button in (self.on, self.off):
            button.setObjectName("choice")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.group.addButton(button)
            row.addWidget(button)
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
        try:   # the bundled UI face (Instrument Sans) and the Devanagari fallback, before the style names them
            from yuki.ui.fonts import load_fonts

            load_fonts()
        except Exception:
            pass
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(640)
        self.resize(640, 700)   # Plex wraps a little wider than Segoe: room for the privacy text

        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 26, 32, 22)
        outer.setSpacing(6)
        self.steps_label = StepKeys(STEP_NAMES)
        self.title = _label("", "title")
        self.subtitle = _label("", "body")
        outer.addWidget(self.steps_label)
        outer.addSpacing(14)
        outer.addWidget(self.title)
        outer.addWidget(self.subtitle)
        outer.addSpacing(12)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._model_page())
        self.stack.addWidget(self._voice_page())
        self.stack.addWidget(self._privacy_page())
        self.stack.addWidget(self._download_page())
        outer.addWidget(self.stack, 1)

        self.error = _label("", "bad")
        self.error.hide()
        outer.addWidget(self.error)

        nav = QHBoxLayout()
        nav.setSpacing(10)
        self.back_btn = QPushButton("Back")
        self.back_btn.setObjectName("flat")
        self.skip_btn = QPushButton("")
        self.skip_btn.setObjectName("flat")
        self.next_btn = QPushButton("Next")
        self.next_btn.setObjectName("primary")
        self.next_btn.setDefault(True)
        for button in (self.back_btn, self.skip_btn, self.next_btn):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(self.back)
        self.skip_btn.clicked.connect(self.skip)
        self.next_btn.clicked.connect(self.next)
        nav.addWidget(self.back_btn)
        nav.addStretch(1)
        nav.addWidget(self.skip_btn)
        nav.addWidget(self.next_btn)
        outer.addLayout(nav)

        self._go(0)

    # -- pages -------------------------------------------------------------------------------

    def _model_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        card, box = _card()
        self.anthropic_radio = QRadioButton("Anthropic API key")
        self.bedrock_radio = QRadioButton("AWS Bedrock (advanced)")
        self.anthropic_radio.setChecked(True)
        self.backend_group = QButtonGroup(page)
        self.backend_group.addButton(self.anthropic_radio)
        self.backend_group.addButton(self.bedrock_radio)
        box.addWidget(self.anthropic_radio)
        box.addWidget(_label("One key from Anthropic. Best for most people.", "hint"))
        box.addSpacing(4)
        box.addWidget(self.bedrock_radio)
        box.addWidget(_label("Use Claude through your own AWS account.", "hint"))
        col.addWidget(card)

        card, box = _card()
        self.key_title = _label("", "section")
        box.addWidget(self.key_title)
        self.model_key = SecretField("Paste your key here")
        box.addWidget(self.model_key)
        self.region_row = QWidget()
        region = QHBoxLayout(self.region_row)
        region.setContentsMargins(0, 0, 0, 0)
        region.addWidget(_label("AWS region", "hint", wrap=False))
        self.region = QLineEdit(ob.DEFAULT_REGION)
        self.region.setMaximumWidth(180)
        region.addWidget(self.region)
        region.addStretch(1)
        box.addWidget(self.region_row)
        row = QHBoxLayout()
        self.key_link = QLabel()
        self.key_link.setObjectName("hint")
        self.key_link.setOpenExternalLinks(True)
        row.addWidget(self.key_link, 1)
        self.test_btn = QPushButton("Test")
        self.test_btn.setObjectName("small")
        self.test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.test_btn.clicked.connect(self.test_key)
        row.addWidget(self.test_btn)
        box.addLayout(row)
        self.test_result = _label("", "hint")
        self.test_result.hide()
        box.addWidget(self.test_result)
        box.addWidget(_label("Saved only in your Windows account settings, never in a file.", "hint"))
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
        col.setSpacing(12)
        for attr, kind, name, why in (
            ("deepgram_key", "deepgram", "Deepgram", "Hears you: turns your voice into text."),
            ("google_key", "google", "Google Gemini", "Gives Yuki its voice, with laughs and sighs. Without it "
             "Yuki uses a plainer one."),
        ):
            card, box = _card()
            box.addWidget(_label(name, "section"))
            box.addWidget(_label(why, "hint"))
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
        col.setSpacing(12)
        card, box = _card()
        box.addWidget(_label("Remember what I'm doing", "section"))
        box.addWidget(_label(
            "Yuki reads the window in front (chats, pages, documents) and keeps short notes, so it can help "
            "with what you're doing. Notes stay on this PC, encrypted. To write them, the text goes to the AI "
            "you picked; nothing else leaves the PC. It never reads password managers, banking or sign-in "
            "pages, private windows, or apps and sites you block. You can pause it any time from the tray.",
            "hint"))
        self.ambient_choice = OnOff()
        box.addWidget(self.ambient_choice)
        col.addWidget(card)

        card, box = _card()
        box.addWidget(_label("Remember what I say on calls", "section"))
        box.addWidget(_label(
            "Only your own microphone, never the other people, and Yuki asks on each call first. Your voice is "
            "kept encrypted on this PC until it's turned into text there later, then deleted. The AI you "
            "picked writes the notes.",
            "hint"))
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
        col.setSpacing(12)
        card, box = _card()
        box.addWidget(_label("Memory search model", "section"))
        box.addWidget(_label("A few hundred MB, downloaded once. It lets Yuki find things in its memory. "
                             "If you skip it, Yuki downloads it by itself the first time it's needed.", "hint"))
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(12)
        box.addSpacing(6)
        box.addWidget(self.bar)
        self.download_status = _label("", "hint")
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
        self.test_result.setStyleSheet(f"color: {GOOD if ok else BAD};")
        self.test_result.setText(("✓  " if ok else "") + message)
        self.test_result.show()

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
        else:
            self.download_state = "failed"
            self.skipped.add("download")
            self.download_status.setText(f"Couldn't download it right now ({why}). "
                                         "Yuki will try again by itself later.")
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
