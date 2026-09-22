"""``yuki-ui``: the tray app that owns the overlay, the strip and the two lanes.

:class:`YukiUi` is the only place that knows how a stream of agent events turns
into a visible thing. It holds no opinion about what a request *means*: it shows a
card per request, and when the worker starts using tools it steps aside for the
status strip. Every decision it makes is about who is busy and what is on screen.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Sequence

from PySide6.QtCore import QObject, QUrl, Qt
from PySide6.QtGui import QAction, QColor, QDesktopServices, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from yuki.config import MODEL_ALIASES, Settings
from yuki.ui.glass import ACCENT
from yuki.ui.hotkey import HotkeyListener, hotkey_bindings
from yuki.ui.overlay import Overlay, ReplyCard
from yuki.ui.runtime import WORKER, AgentRuntime
from yuki.ui.status import StatusStrip, describe_tool
from yuki.ui.uilog import UiLog

#: Name of the mutex that keeps one Yuki UI per session.
MUTEX_NAME = "Local\\YukiUiSingleInstance"

_ERROR_ALREADY_EXISTS = 183


def single_instance_handle(name: str = MUTEX_NAME) -> int | None:
    """Claim the single-instance mutex.

    Args:
        name: Kernel object name.

    Returns:
        The handle to hold for the process lifetime, or None if another instance
        already owns it.
    """
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None
    if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


def tray_icon() -> QIcon:
    """A small snowflake glyph, drawn rather than shipped as a file."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor(16, 17, 20, 235))
    painter.setPen(QPen(QColor(255, 255, 255, 40), 2.0))
    painter.drawRoundedRect(3, 3, 58, 58, 16, 16)
    pen = QPen(ACCENT, 5.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    centre, arm = 32, 17
    painter.drawLine(centre, centre - arm, centre, centre + arm)
    for dx, dy in ((arm, arm), (arm, -arm)):
        painter.drawLine(
            int(centre - dx * 0.87), int(centre - dy * 0.5), int(centre + dx * 0.87), int(centre + dy * 0.5)
        )
    painter.end()
    return QIcon(pixmap)


class YukiUi(QObject):
    """Wires the hotkeys, the overlay, the strip and the runtime together.

    Args:
        settings: Shared settings for both agents.
        ui_log: Where UI events go.
        runtime: The agent runtime. One is built from ``settings`` if omitted.
    """

    def __init__(
        self,
        settings: Settings,
        ui_log: UiLog,
        runtime: AgentRuntime | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.ui_log = ui_log
        self.overlay = Overlay()
        self.strip = StatusStrip()
        self.runtime = runtime or AgentRuntime(settings, ui_log)

        #: request id -> the card showing it.
        self.cards: dict[int, ReplyCard] = {}
        #: Worker requests that have already taken over the screen with the strip.
        self._acting: set[int] = set()
        #: Which lane is waiting for an answer, and for which request.
        self._question: tuple[str, int] | None = None

        self.overlay.submitted.connect(self._on_submitted)
        self.overlay.dismissed.connect(lambda: self.ui_log.event("overlay", state="hidden"))

        runtime_signals = self.runtime
        runtime_signals.started.connect(self._on_started)
        runtime_signals.tool_called.connect(self._on_tool_called)
        runtime_signals.asked.connect(self._on_asked)
        runtime_signals.finished.connect(self._on_finished)
        runtime_signals.failed.connect(self._on_failed)
        runtime_signals.queued.connect(self._on_queued)

        self.bindings = hotkey_bindings(settings)
        self.hotkeys = HotkeyListener(self.bindings)
        self.hotkeys.pressed.connect(self._on_hotkey)
        self.hotkeys.failed.connect(
            lambda action, reason: self.ui_log.event("hotkey_failed", action=action, reason=reason)
        )

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the lanes and the hotkey listener."""
        self.runtime.start()
        self.hotkeys.start()
        self.ui_log.event("start", hotkeys=self.bindings, model=self.settings.model)

    def stop(self) -> None:
        """Shut everything down in the right order."""
        self.ui_log.event("stop")
        self.hotkeys.stop()
        self.runtime.stop()
        self.overlay.hide()
        self.strip.hide()

    # -- input -------------------------------------------------------------

    def _on_hotkey(self, action: str) -> None:
        """React to a global hotkey."""
        self.ui_log.event("hotkey", action=action)
        if action == "cancel":
            self.runtime.cancel_worker()
            return
        self.overlay.toggle()
        if self.overlay.isVisible() and not self.overlay.closing:
            self.ui_log.event("overlay", state="shown", via="hotkey")

    def show_overlay(self) -> None:
        """Open the overlay (tray menu, or another instance asking for it)."""
        self.overlay.open()
        self.ui_log.event("overlay", state="shown", via="menu")

    def _on_submitted(self, text: str) -> None:
        """The user pressed Enter: either an answer to a question, or a request."""
        if self._question is not None:
            lane_name, request_id = self._question
            self._question = None
            self.runtime.answer(lane_name, text)
            card = self.overlay.add_card(text)
            self.cards[request_id] = card
            self.overlay.expand_input()
            return
        lane_name, request_id = self.runtime.submit(text)
        self.cards[request_id] = self.overlay.add_card(text)
        self.overlay.expand_input()
        self.ui_log.event("submit", id=request_id, lane=lane_name, text=text)

    # -- runtime events ----------------------------------------------------

    def _on_started(self, lane_name: str, request_id: int, request: str) -> None:
        card = self.cards.get(request_id)
        if card is None:
            self.cards[request_id] = self.overlay.add_card(request)

    def _on_tool_called(self, lane_name: str, request_id: int, name: str, tool_input: dict) -> None:
        """Show the step. For the worker, the first one takes the screen over."""
        line = describe_tool(name, tool_input)
        if lane_name == WORKER:
            if request_id not in self._acting:
                self._acting.add(request_id)
                if self.overlay.isVisible():
                    self.overlay.dismiss()
                self.ui_log.event("task_mode", id=request_id, tool=name)
            self.strip.show_step(line)
            return
        card = self.cards.get(request_id)
        if card is not None:
            card.set_body(line)

    def _on_asked(self, lane_name: str, request_id: int, question: str) -> None:
        """A lane needs an answer: bring the overlay back with the question."""
        self.ui_log.event("ask_user", id=request_id, lane=lane_name, question=question)
        self.strip.dismiss()
        self._question = (lane_name, request_id)
        card = self.cards.get(request_id)
        if card is None:
            self.cards[request_id] = self.overlay.add_card("", question, tone="question")
        else:
            card.set_body(question, tone="question")
        self.overlay.open()

    def _on_finished(self, lane_name: str, request_id: int, text: str) -> None:
        self._settle(lane_name, request_id, text, tone="reply")

    def _on_failed(self, lane_name: str, request_id: int, text: str) -> None:
        self._settle(lane_name, request_id, text, tone="error")

    def _settle(self, lane_name: str, request_id: int, text: str, *, tone: str) -> None:
        """Put a closing message where the user is actually looking."""
        self._acting.discard(request_id)
        if self._question is not None and self._question[1] == request_id:
            self._question = None
        card = self.cards.pop(request_id, None)
        if card is not None:
            card.set_body(text, tone="error" if tone == "error" else "reply")
        if self.overlay.isVisible() and not self.overlay.closing:
            if card is None:
                self.overlay.add_card("", text, tone="error" if tone == "error" else "reply")
            self.overlay.expand_input()
        else:
            self.strip.show_final(text, tone=tone)
        self.ui_log.event("settle", id=request_id, lane=lane_name, tone=tone, text=text)

    def _on_queued(self, request_id: int, request: str, waiting: int) -> None:
        """A front-desk request turned out to need hands and went to the worker."""
        card = self.overlay.add_card(
            request, f"Queued — {waiting} ahead of it" if waiting > 1 else "Queued — up next"
        )
        self.cards[request_id] = card

    # -- tray --------------------------------------------------------------

    def toggle_model(self) -> str:
        """Switch between the two configured models.

        Returns:
            The model id now in force.
        """
        aliases = list(MODEL_ALIASES)
        current = next(
            (alias for alias in aliases if MODEL_ALIASES[alias] == self.settings.model), aliases[0]
        )
        nxt = aliases[(aliases.index(current) + 1) % len(aliases)]
        return self.runtime.set_model(nxt)

    def open_logs(self) -> None:
        """Open the session log folder in the file manager."""
        self.ui_log.event("open_logs", path=str(self.settings.sessions_dir))
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.settings.sessions_dir)))


def build_tray(ui: YukiUi, app: QApplication) -> QSystemTrayIcon:
    """Create the tray icon and its menu.

    Args:
        ui: The controller the menu acts on.
        app: The application, so Quit can end it.

    Returns:
        The tray icon, which the caller must keep a reference to.
    """
    tray = QSystemTrayIcon(tray_icon(), app)
    menu = QMenu()

    show = QAction("Show", menu)
    show.triggered.connect(ui.show_overlay)
    menu.addAction(show)

    model = QAction("", menu)

    def refresh_model_label() -> None:
        model.setText(f"Model: {ui.settings.model.split('.')[-1]}  (click to switch)")

    def switch_model() -> None:
        ui.toggle_model()
        refresh_model_label()
        tray.setToolTip(f"Yuki — {ui.settings.model}")

    refresh_model_label()
    model.triggered.connect(switch_model)
    menu.addAction(model)

    logs = QAction("Open logs folder", menu)
    logs.triggered.connect(ui.open_logs)
    menu.addAction(logs)

    menu.addSeparator()
    quit_action = QAction("Quit", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.setToolTip(f"Yuki — {ui.settings.model}")
    tray.activated.connect(
        lambda reason: ui.show_overlay()
        if reason == QSystemTrayIcon.ActivationReason.Trigger
        else None
    )
    tray.show()
    return tray


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Yuki desktop shell.

    Args:
        argv: Unused; present for the console-script entry point.

    Returns:
        Process exit code. ``1`` if another instance is already running.
    """
    del argv
    handle = single_instance_handle()
    if handle is None:
        print("yuki-ui is already running (press the hotkey to show it)", file=sys.stderr)
        return 1

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication([])
    app.setApplicationName("Yuki")
    app.setQuitOnLastWindowClosed(False)

    settings = Settings()
    ui_log = UiLog(sessions_dir=settings.sessions_dir, session_id=None)
    ui = YukiUi(settings, ui_log)
    tray = build_tray(ui, app)
    ui.start()

    try:
        code = app.exec()
    finally:
        ui.stop()
        tray.hide()
        ui_log.close()
    return int(code)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
