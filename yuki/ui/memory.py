"""The tray's memory controls: service auto-start, status line, pause, portrait and to-do panels.

Memory is a separate process (``yuki-memory``, docs/MEMORY.md) with its own
lifetime: the tray starts it when it is not running, and leaves it running
when Yuki quits. Everything the tray asks memory goes through the shared
:class:`~yuki.agent.memory.MemoryAccess` on a background thread, and comes back
to the GUI thread as a Qt signal, so a slow or missing memory never freezes
the menu.
"""

from __future__ import annotations

import html
import subprocess
import sys
import math
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtGui import QCursor, QFont, QGuiApplication, QKeyEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QTextBrowser, QToolButton, QVBoxLayout

from yuki.agent.memory import (
    TODO_SOURCES,
    MemoryAccess,
    MemoryUnavailable,
    due_text,
    todo_done,
)
from yuki.ui.glass import GlassWindow, ui_font
from yuki.ui.uilog import UiLog

#: Windows process-creation flags for the detached memory service.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000

#: Size of the "what Yuki knows" panel (without the shadow margin).
KNOWS_WIDTH = 560
KNOWS_HEIGHT = 600

#: Size of the "Today's list" panel (without the shadow margin).
TODAY_WIDTH = 480
TODAY_HEIGHT = 520

#: An open to-do due within this long (or overdue) is listed under "Due soon".
DUE_SOON = timedelta(hours=24)

#: "Quiet until tomorrow" lasts until this hour of the next morning.
TOMORROW_HOUR = 6

#: The status line before the first answer arrives.
STATUS_PENDING = "Memory: checking…"


def quiet_until(status: dict[str, Any] | None, *, now: datetime | None = None) -> datetime | None:
    """When nudges come back on, from ``status["nudges"]`` (``nudge_status()``); ``None`` when not quiet."""
    nudges = (status or {}).get("nudges")
    if not isinstance(nudges, dict):
        return None
    moment = local_moment(nudges.get("quiet_until"))
    now = now or datetime.now()
    return moment if moment is not None and moment > now else None


def quiet_label(moment: datetime, *, now: datetime | None = None) -> str:
    """``16:30`` today, ``tomorrow 06:00``, else ``Fri 06:00``."""
    now = now or datetime.now()
    if moment.date() == now.date():
        return moment.strftime("%H:%M")
    if moment.date() == (now + timedelta(days=1)).date():
        return f"tomorrow {moment.strftime('%H:%M')}"
    return moment.strftime("%a %H:%M")


def minutes_until_tomorrow(now: datetime | None = None) -> int:
    """Minutes from ``now`` until :data:`TOMORROW_HOUR` o'clock next morning.

    Before that hour it is the same calendar day's morning (at 01:00,
    "tomorrow" means the morning ahead, not the one after it).
    """
    now = now or datetime.now()
    target = now.replace(hour=TOMORROW_HOUR, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return max(1, math.ceil((target - now).total_seconds() / 60.0))


def describe_memory_status(status: dict[str, Any] | None, *, installed: bool = True) -> str:
    """The one-line status shown at the top of the tray menu.

    Args:
        status: ``MemoryClient.status()``, or ``None`` when it could not be read.
        installed: Whether the memory API exists at all.
    """
    if status is None:
        return "Memory: unavailable" if installed else "Memory: not installed"
    if not status.get("service_running"):
        return "Memory: not running"
    if status.get("paused"):
        return "Memory: paused"
    quiet = quiet_until(status)
    if quiet is not None:
        return f"Memory: watching · quiet until {quiet_label(quiet)}"
    captures = int(status.get("captures_today") or 0)
    facts = int(status.get("facts_today") or 0)
    return (
        f"Memory: watching · {captures} capture{'' if captures == 1 else 's'} · "
        f"{facts} fact{'' if facts == 1 else 's'} today"
    )


def memory_service_command() -> list[str]:
    """How to start ``yuki-memory`` from the same Python environment as this process.

    Uses ``pythonw.exe`` when it sits next to the interpreter. A venv's
    ``python.exe`` is only a launcher that starts the real console interpreter,
    and that child gets a console of its own even when the launcher is detached:
    a visible terminal window that kills memory if the user closes it.
    ``pythonw.exe`` is the same interpreter without a console.
    """
    windowless = Path(sys.executable).with_name("pythonw.exe")
    interpreter = str(windowless) if windowless.exists() else sys.executable
    return [interpreter, "-m", "yuki.memory.service"]


def spawn_memory_service(popen: Callable[..., Any] = subprocess.Popen) -> int:
    """Start the memory service detached and hidden; return its pid.

    Detached (no console, its own process group) and, where the job Yuki runs
    in allows it, broken away from that job, so closing Yuki -- or the
    terminal that launched it -- does not take memory down with it.

    Raises:
        OSError: When the process could not be started at all.
    """
    command = memory_service_command()
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # SW_HIDE
    base = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    options = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        cwd=str(Path(__file__).resolve().parents[2]),
        startupinfo=startup,
    )
    try:
        process = popen(command, creationflags=base | _CREATE_BREAKAWAY_FROM_JOB, **options)
    except OSError:  # the job does not allow breakaway: start it inside the job
        process = popen(command, creationflags=base, **options)
    return int(process.pid)


class MemoryControl(QObject):
    """What the tray menu does with memory, off the GUI thread.

    Args:
        memory: The shared memory access (the lanes use the same one).
        ui_log: Where every memory action is recorded.
        spawn: Starts the service and returns its pid (replaced offline).

    Signals:
        status_changed: ``(status dict or None, error or None)`` after every
            status read.
        knows_ready: ``(portrait or None, know-how lines, meta line)`` for the
            "what Yuki knows" window.
    """

    status_changed = Signal(object, object)
    knows_ready = Signal(object, object, str)
    #: ``(to-do rows or None, meta line)`` for the "Today's list" panel.
    today_ready = Signal(object, str)
    #: ``(event name, fields)``: UI log records from the worker threads, written
    #: on the GUI thread (the session file has one writer).
    _logged = Signal(str, object)

    def __init__(
        self,
        memory: MemoryAccess,
        ui_log: UiLog,
        *,
        spawn: Callable[[], int] = spawn_memory_service,
    ) -> None:
        super().__init__()
        self.memory = memory
        self.ui_log = ui_log
        self._spawn = spawn
        self._status_busy = threading.Lock()
        #: The last status read, for the menu to show while a new one loads.
        self.last_status: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._logged.connect(self._write_log)

    def _event(self, name: str, **fields: Any) -> None:
        """Record a UI event from any thread; it is written on the GUI thread."""
        self._logged.emit(name, fields)

    def _write_log(self, name: str, fields: object) -> None:
        self.ui_log.event(name, **(fields if isinstance(fields, dict) else {}))

    def _background(self, name: str, work: Callable[[], None]) -> threading.Thread:
        def run() -> None:
            try:
                work()
            except Exception as exc:  # a tray action must never take the UI down
                self._event("memory_error", action=name, error=f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=run, name=f"yuki-ui-memory-{name}", daemon=True)
        thread.start()
        return thread

    # -- status ------------------------------------------------------------

    def _read_status(self) -> tuple[dict[str, Any] | None, str | None]:
        """``status()``, with ``nudge_status()`` under ``"nudges"`` when memory has it."""
        try:
            status, error = self.memory.status(), None
        except MemoryUnavailable as exc:
            status, error = None, str(exc)
        if status is not None:
            try:
                status["nudges"] = self.memory.nudge_status()
            except MemoryUnavailable:
                pass  # an older memory, or a slow one: the line just leaves quiet out
        self.last_status, self.last_error = status, error
        return status, error

    def refresh_status(self) -> threading.Thread | None:
        """Read the status in the background; emits :attr:`status_changed`.

        A read already in flight is not doubled (the menu can be opened twice
        while memory is slow).
        """
        if not self._status_busy.acquire(blocking=False):
            return None

        def work() -> None:
            try:
                status, error = self._read_status()
                self.status_changed.emit(status, error)
            finally:
                self._status_busy.release()

        return self._background("status", work)

    # -- service -----------------------------------------------------------

    def ensure_service(self) -> threading.Thread:
        """Start ``yuki-memory`` when memory reports it is not running."""

        def work() -> None:
            if not self.memory.installed:
                self._event("memory_service", state="not_installed")
                return
            status, error = self._read_status()
            if status is None:
                self._event("memory_service", state="unknown", error=error)
                self.status_changed.emit(None, error)
                return
            if status.get("service_running"):
                self._event("memory_service", state="running")
                self.status_changed.emit(status, None)
                return
            try:
                pid = self._spawn()
            except OSError as exc:
                self._event(
                    "memory_service", state="spawn_failed", error=f"{type(exc).__name__}: {exc}"
                )
                return
            self._event(
                "memory_service", state="spawned", pid=pid, command=memory_service_command()
            )
            self.status_changed.emit({**status, "service_running": True, "starting": True}, None)

        return self._background("ensure_service", work)

    # -- menu actions ------------------------------------------------------

    def set_paused(self, paused: bool) -> threading.Thread:
        """Pause or resume capture, then re-read the status."""

        def work() -> None:
            try:
                self.memory.set_paused(paused)
                self._event("memory_pause", paused=paused)
            except MemoryUnavailable as exc:
                self._event("memory_pause", paused=paused, error=str(exc))
            status, error = self._read_status()
            self.status_changed.emit(status, error)

        return self._background("set_paused", work)

    def refresh_portrait(self) -> threading.Thread:
        """Ask memory to rebuild the portrait now."""

        def work() -> None:
            self._event("memory_refresh_portrait", state="started")
            try:
                self.memory.refresh_portrait()
                self._event("memory_refresh_portrait", state="done")
            except MemoryUnavailable as exc:
                self._event("memory_refresh_portrait", state="failed", error=str(exc))

        return self._background("refresh_portrait", work)

    def fetch_knows(self) -> threading.Thread:
        """Fetch the portrait and saved know-how; emits :attr:`knows_ready`."""

        def work() -> None:
            portrait: str | None = None
            knowhow: list[str] = []
            notes: list[str] = []
            try:
                portrait, _ = self.memory.portrait()
            except MemoryUnavailable as exc:
                notes.append(f"portrait unavailable: {exc}")
            try:
                knowhow = self.memory.knowhow_all()
            except MemoryUnavailable as exc:
                notes.append(f"know-how unavailable: {exc}")
            status = self.last_status or {}
            updated = _stamp(status.get("portrait_updated_at"))
            meta = f"Portrait updated {updated}" if updated else ""
            if notes and portrait is None and not knowhow:
                meta = "; ".join(notes)
            self._event(
                "memory_show", portrait_chars=len(portrait or ""), knowhow=len(knowhow), notes=notes
            )
            self.knows_ready.emit(portrait, knowhow, meta)

        return self._background("fetch_knows", work)

    def fetch_today(self) -> threading.Thread:
        """Fetch the to-do list, done ones included; emits :attr:`today_ready`."""

        def work() -> None:
            try:
                rows: list[dict[str, Any]] | None = self.memory.todos(include_done=True)
                meta = ""
            except MemoryUnavailable as exc:
                rows, meta = None, f"To-do list unavailable: {exc}"
            self._event("today_list", rows=None if rows is None else len(rows), error=meta or None)
            self.today_ready.emit(rows, meta)

        return self._background("fetch_today", work)


class KnowsWindow(GlassWindow):
    """A small read-only glass panel showing the portrait and the saved know-how.

    Args:
        title: The panel's heading.
        size: ``(width, height)`` without the shadow margin.
    """

    def __init__(
        self, *, title: str = "What Yuki knows", size: tuple[int, int] = (KNOWS_WIDTH, KNOWS_HEIGHT)
    ) -> None:
        super().__init__(activates=True, radius=16)
        self.setObjectName("knows")
        margin = self.SHADOW + 18
        layout = QVBoxLayout(self)
        layout.setContentsMargins(margin, margin - 4, margin, margin)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel(title)
        title.setFont(ui_font(13, weight=QFont.Weight.DemiBold))
        title.setStyleSheet("color: rgba(238,240,245,255); background: transparent;")
        header.addWidget(title)
        header.addStretch(1)
        close = QToolButton()
        close.setText("✕")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(
            "QToolButton { color: rgba(238,240,245,150); background: transparent;"
            " border: none; padding: 2px 6px; font-size: 13px; }"
            "QToolButton:hover { color: rgba(238,240,245,255); }"
        )
        close.clicked.connect(self.fade_out)
        header.addWidget(close)
        layout.addLayout(header)

        self.meta = QLabel("")
        self.meta.setFont(ui_font(9))
        self.meta.setStyleSheet("color: rgba(238,240,245,150); background: transparent;")
        self.meta.setWordWrap(True)
        layout.addWidget(self.meta)

        self.body = QTextBrowser()
        self.body.setReadOnly(True)
        self.body.setOpenLinks(False)
        self.body.setFont(ui_font(11))
        self.body.setFrameShape(QTextBrowser.Shape.NoFrame)
        self.body.setStyleSheet(
            "QTextBrowser { background: transparent; color: rgba(238,240,245,235);"
            " border: none; selection-background-color: rgba(126,180,255,90); }"
            "QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,50);"
            " border-radius: 3px; min-height: 24px; }"
            "QScrollBar::handle:vertical:hover { background: rgba(255,255,255,90); }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }"
        )
        layout.addWidget(self.body, 1)
        self.resize(size[0] + 2 * self.SHADOW, size[1] + 2 * self.SHADOW)
        self.show_loading()

    def focus_target(self) -> QTextBrowser:  # noqa: D102 - GlassWindow override
        return self.body

    def show_loading(self) -> None:
        """Placeholder while memory is being read."""
        self.meta.setText("")
        self.body.setHtml(_paragraphs("Reading memory…", dim=True))

    def show_knows(self, portrait: str | None, knowhow: list[str], meta: str = "") -> None:
        """Fill the panel.

        Args:
            portrait: The rendered portrait text, shown as written (line breaks kept).
            knowhow: Saved know-how lines.
            meta: One dim line under the title (when the portrait was updated).
        """
        self.meta.setText(meta)
        parts = [
            _paragraphs(portrait)
            if portrait
            else _paragraphs("No portrait yet. It is written from the journal overnight, "
                             "or when you choose Refresh portrait now.", dim=True)
        ]
        if knowhow:
            items = "".join(f"<li>{html.escape(line)}</li>" for line in knowhow)
            parts.append(
                "<p style='color: rgba(126,180,255,255); margin-top: 14px;'>"
                "Know-how saved on this PC</p>"
                f"<ul style='margin-left: -18px;'>{items}</ul>"
            )
        self.body.setHtml("".join(parts))

    def anchor(self) -> QPoint:
        """Centred on the screen under the cursor."""
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else self.geometry()
        return QPoint(
            area.left() + (area.width() - self.width()) // 2,
            area.top() + (area.height() - self.height()) // 2,
        )

    def open(self) -> None:
        """Show (or bring back) the panel."""
        self.fade_in(self.anchor())

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: D102 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            self.fade_out()
            return
        super().keyPressEvent(event)


class TodayWindow(KnowsWindow):
    """The read-only "Today's list" panel: due soon, open, and done today."""

    def __init__(self) -> None:
        super().__init__(title="Today's list", size=(TODAY_WIDTH, TODAY_HEIGHT))
        self.setObjectName("today")

    def show_loading(self) -> None:
        """Placeholder while the list is being read."""
        self.meta.setText("")
        self.body.setHtml(_paragraphs("Reading the to-do list…", dim=True))

    def show_today(
        self, rows: list[dict[str, Any]] | None, meta: str = "", *, now: datetime | None = None
    ) -> None:
        """Fill the panel.

        Args:
            rows: ``todos(include_done=True)``; ``None`` when it could not be read.
            meta: One dim line under the title.
            now: The time the groups are worked out against.
        """
        now = now or datetime.now()
        if rows is None:
            self.meta.setText(meta)
            self.body.setHtml(_paragraphs(meta or "The to-do list could not be read.", dim=True))
            return
        groups = today_groups(rows, now=now)
        open_count = len(groups["due_soon"]) + len(groups["open"])
        self.meta.setText(
            meta
            or f"{open_count} open · {len(groups['done_today'])} done today · {now.strftime('%a %d %b, %H:%M')}"
        )
        self.body.setHtml(today_html(groups, now=now))


def today_groups(rows: list[dict[str, Any]], *, now: datetime) -> dict[str, list[dict[str, Any]]]:
    """Sort to-do rows for the panel.

    ``due_soon``: open, due within :data:`DUE_SOON` or overdue, soonest first.
    ``open``: the other open ones, in memory's order. ``done_today``: done,
    with ``done_at`` (else ``since``) today; a done row with neither readable
    is kept, since memory chose to return it.
    """
    groups: dict[str, list[dict[str, Any]]] = {"due_soon": [], "open": [], "done_today": []}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if todo_done(row):
            when = local_moment(row.get("done_at")) or local_moment(row.get("since"))
            if when is None or when.date() == now.date():
                groups["done_today"].append(row)
            continue
        due = local_moment(row.get("due"))
        if due is not None and due - now <= DUE_SOON:
            groups["due_soon"].append(row)
        else:
            groups["open"].append(row)
    groups["due_soon"].sort(key=lambda row: local_moment(row.get("due")) or now)
    return groups


def today_html(groups: dict[str, list[dict[str, Any]]], *, now: datetime) -> str:
    """The panel's body: one section per non-empty group, each item with its source and evidence."""
    sections = (("due_soon", "Due soon"), ("open", "Open"), ("done_today", "Done today"))
    parts: list[str] = []
    for key, heading in sections:
        rows = groups.get(key) or []
        if not rows:
            continue
        parts.append(
            "<p style='color: rgba(126,180,255,255); margin-top: 12px; margin-bottom: 4px;'>"
            f"{heading}</p>"
        )
        for row in rows:
            parts.append(_todo_html(row, done=key == "done_today", now=now))
    if not parts:
        return _paragraphs(
            "Nothing on the list. Open loops memory notices, things Yuki promises and "
            "things you ask Yuki to add show up here.",
            dim=True,
        )
    return "".join(parts)


def _todo_html(row: dict[str, Any], *, done: bool, now: datetime) -> str:
    """One to-do: its text, then a dim line of due / source / since, then its evidence."""
    text = html.escape(" ".join(str(row.get("text") or "").split()))
    colour = "rgba(238,240,245,150)" if done else "rgba(238,240,245,240)"
    notes: list[str] = []
    due = local_moment(row.get("due"))
    if row.get("due") and not done:
        overdue = due is not None and due < now
        label = html.escape(f"{'overdue' if overdue else 'due'} {due_text(row.get('due'))}")
        notes.append(f"<span style='color: rgba(255,190,110,230);'>{label}</span>" if overdue else label)
    source = row.get("source")
    if source:
        notes.append(html.escape(TODO_SOURCES.get(str(source), str(source))))
    since = _stamp(row.get("done_at") if done and row.get("done_at") else row.get("since"))
    if since:
        notes.append(html.escape(("done " if done else "since ") + since))
    evidence = " ".join(str(row.get("evidence") or "").split())
    if len(evidence) > 220:
        evidence = evidence[:219].rstrip() + "…"
    lines = [f"<div style='color: {colour}; margin-top: 8px;'>{'✓ ' if done else ''}{text}</div>"]
    if notes:
        lines.append(
            "<div style='color: rgba(238,240,245,140); font-size: 9pt;'>" + " · ".join(notes) + "</div>"
        )
    if evidence:
        lines.append(
            "<div style='color: rgba(238,240,245,115); font-size: 9pt; font-style: italic;'>"
            f"{html.escape(evidence)}</div>"
        )
    return "".join(lines)


def _paragraphs(text: str, *, dim: bool = False) -> str:
    """Plain text as HTML paragraphs, line breaks kept, nothing interpreted."""
    colour = "rgba(238,240,245,150)" if dim else "rgba(238,240,245,235)"
    blocks = [block for block in text.strip().split("\n\n") if block.strip()]
    return "".join(
        f"<p style='color: {colour}; margin-bottom: 8px;'>"
        + html.escape(block.strip()).replace("\n", "<br>")
        + "</p>"
        for block in blocks
    )


def _stamp(value: Any) -> str:
    """``Tue 22 Sep, 21:14`` from epoch seconds, a datetime or an ISO string."""
    moment = local_moment(value)
    if moment is None:
        return value.strip() if isinstance(value, str) else ""
    return moment.strftime("%a %d %b, %H:%M")


def local_moment(value: Any) -> datetime | None:
    """A naive local datetime from epoch seconds, a datetime or an ISO string; else ``None``."""
    moment: datetime | None = None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        try:
            moment = datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    if moment is None:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment
