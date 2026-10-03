"""See and stop every run (06 §4.4, A5) and the ``ask`` offer's hand-off, in the tray's own wiring (offscreen).

``YukiUi`` is built bare (``__new__``) with stubbed collaborators - no voice, no window, no memory service, no
lanes - so only its run and offer handling is exercised: the strip appears at request start with Stop, Stop
cancels that request, a stopped run reads "Stopped." (never an error), the stop shortcut is said once, and a card's
``ask`` suggestion lands in the ask bar unsent; what the user then does with it is recorded. Run apart from the
other GUI tests (QT_QPA_PLATFORM=offscreen); nothing is shown on screen.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class Card:
    def __init__(self) -> None:
        self.body = ""

    def set_body(self, text, tone="reply") -> None:
        self.body = text

    def set_meta(self, text) -> None:
        pass


class Overlay:
    def __init__(self) -> None:
        from PySide6.QtWidgets import QPlainTextEdit

        self.visible = False
        self.closing = False
        self.input = QPlainTextEdit()
        self.opened = 0

    def isVisible(self) -> bool:
        return self.visible

    def add_card(self, *a, **k) -> Card:
        return Card()

    def dismiss(self) -> None:
        self.visible = False

    def expand_input(self) -> None:
        pass

    def open(self) -> None:
        self.visible = True
        self.opened += 1
        self.input.clear()

    def nudge_cards(self) -> list:
        return []


class Runtime:
    def __init__(self) -> None:
        self.cancelled: list[int] = []
        self.submitted: list[str] = []
        self.worker_busy = False
        self.worker_running: int | None = None
        self.next_id = 100

    def cancel(self, request_id, reason="cancelled", *, by="") -> str:
        self.cancelled.append(request_id)
        self.by = by
        return "running"

    def cancel_worker(self) -> None:
        if self.worker_running is not None:
            self.cancelled.append(self.worker_running)

    def running_ids(self) -> dict:
        return {"worker": self.worker_running, "front_desk": None}

    def submit(self, request, **kwargs):
        self.submitted.append(request)
        self.next_id += 1
        return "worker", self.next_id


class Nudges:
    def __init__(self) -> None:
        self.outcomes: list[tuple] = []

    def make_way(self) -> None:
        pass

    def try_show(self) -> None:
        pass

    def offer_outcome(self, nudge, outcome) -> None:
        self.outcomes.append((nudge.get("id"), outcome))

    def coach_note_for(self, lane, exclude=None):
        return "", []


def bare_ui(qapp):
    from PySide6.QtCore import QObject

    from yuki.ui.app import YukiUi
    from yuki.ui.status import StatusStrip

    ui = YukiUi.__new__(YukiUi)
    QObject.__init__(ui)
    events: list[tuple] = []
    ui.ui_log = SimpleNamespace(event=lambda name, **f: events.append((name, f)))
    ui.events = events
    ui.strip = StatusStrip()
    ui.strip.stop_clicked.connect(ui._on_strip_stop)   # as YukiUi.__init__ wires it
    ui.overlay = Overlay()
    ui.runtime = Runtime()
    ui.nudges = Nudges()
    ui.voice = SimpleNamespace(owns=lambda rid: False, interrupt=lambda: None)
    ui.window_host = SimpleNamespace(user_message=lambda *a: None, emit=lambda *a: None)
    ui.bindings = {"cancel": "alt+shift+x"}
    ui.cards, ui._settled, ui._acting = {}, {}, set()
    ui._question = None
    ui._runs, ui._stopped, ui._stop_hint_shown = {}, {}, False
    ui._pending_ask, ui._ask_runs = None, {}
    ui._origin_hwnd = None
    ui._nudge_reply = None
    return ui


def test_every_run_shows_on_the_strip_from_its_start_with_stop_and_the_shortcut_once(qapp):
    ui = bare_ui(qapp)
    ui._on_started("front_desk", 1, "what's on my list")
    assert ui.strip.run_id == 1 and not ui.strip.stop_button.isHidden()
    assert ui.strip.label._full == "Thinking… (Alt+Shift+X stops it)"   # said once, on the session's first strip
    assert ui.strip.meta.text() == "0 s"
    ui._on_tool_called("front_desk", 1, "todo", {"action": "list"})
    assert ui.strip.meta.text().endswith("step 1")
    ui._on_finished("front_desk", 1, "Three things on your list.")
    assert ui.strip.run_id is None and ui.strip.stop_button.isHidden()
    assert ui.strip.label._full == "Three things on your list."        # done: the answer
    ui._on_started("worker", 2, "open spotify")
    assert ui.strip.label._full == "Thinking…"                         # the shortcut is not said again


def test_stop_on_the_strip_cancels_that_request_and_it_ends_as_stopped_not_an_error(qapp):
    ui = bare_ui(qapp)
    ui._on_started("worker", 7, "tidy my downloads")
    ui._on_tool_called("worker", 7, "list_files", {"path": "Downloads"})
    ui._on_tool_called("worker", 7, "move_files", {"to": "Archive"})
    assert ui.strip.meta.text().endswith("step 2")
    ui.strip._on_stop()                                                # the pill (never takes focus)
    assert ui.runtime.cancelled == [7] and ui.strip.stop_button.text() == "Stopping…"
    ui._on_failed("worker", 7, "Cancelled by the user.")
    assert ui.strip.label._full == "Stopped." and ui.strip._tone == "final"   # not the red error line
    assert [f["tone"] for n, f in ui.events if n == "settle"] == ["stopped"]


def test_the_cancel_hotkey_marks_the_workers_run_as_stopped(qapp):
    ui = bare_ui(qapp)
    ui._on_started("worker", 9, "rename the files")
    ui.runtime.worker_running = 9
    ui._on_hotkey("cancel")
    assert ui.runtime.cancelled == [9]
    ui._on_failed("worker", 9, "Cancelled by the user.")
    assert ui.strip.label._full == "Stopped."


def test_a_failure_the_user_did_not_ask_for_stays_an_error(qapp):
    ui = bare_ui(qapp)
    ui._on_started("worker", 3, "x")
    ui._on_failed("worker", 3, "Bedrock is down.")
    assert ui.strip.label._full == "Bedrock is down." and ui.strip._tone == "error"


def test_the_workers_run_keeps_the_strip_over_a_quick_question(qapp):
    ui = bare_ui(qapp)
    ui._on_started("worker", 11, "set up the meeting notes")
    ui._on_started("front_desk", 12, "what time is it")
    assert ui.strip.run_id == 11
    ui._on_finished("front_desk", 12, "21:40")
    assert ui.strip.label._full == "21:40"                              # the answer is seen (no overlay open)
    deadline = time.monotonic() + 4.0
    while ui.strip.run_id is None and time.monotonic() < deadline:     # then the worker's run comes back
        qapp.processEvents()
        time.sleep(0.02)
    assert ui.strip.run_id == 11
    ui._on_tool_called("worker", 11, "open_app", {"name": "notepad"})
    assert ui.strip.run_id == 11 and ui.strip.meta.text().endswith("step 1")


def test_an_ask_offer_waits_unsent_in_the_ask_bar_and_its_fate_is_recorded(qapp):
    ui = bare_ui(qapp)
    nudge = {"id": 5, "kind": "notice"}
    text = "Check the T3 Code agent's stream failure and tell me what it was"
    ui._on_offer_ask(nudge, text)
    assert ui.overlay.input.toPlainText() == text and ui.runtime.submitted == []   # nothing ran
    ui._on_submitted(text)                                              # sent unchanged
    request = ui.runtime.submitted[0]
    assert request.startswith("[Yuki's own suggestion from its card N5, sent unchanged by the user]\n" + text)
    rid = ui.runtime.next_id
    ui._on_started("worker", rid, request)
    ui._on_strip_stop(rid)
    ui._on_failed("worker", rid, "Cancelled by the user.")
    assert ui.nudges.outcomes == [(5, "stopped")]                       # stopped within a minute: not wanted
    ui._on_offer_ask({"id": 6}, "Look at the deploy")
    ui._on_overlay_dismissed()
    assert ui.nudges.outcomes[-1] == (6, "abandoned")                   # closed unsent
    ui._on_offer_ask({"id": 8}, "Look at the deploy")
    ui._on_submitted("look at the deploy logs from today")              # changed: the user's own words
    assert ui.runtime.submitted[-1].startswith("look at the deploy logs from today")
    rid = ui.runtime.next_id
    ui._on_started("worker", rid, ui.runtime.submitted[-1])
    ui._on_finished("worker", rid, "The deploy failed on the lockfile.")
    assert ui.nudges.outcomes[-1] == (8, "done")


def test_the_call_pill_counts_from_the_session_not_the_row(qapp):
    from datetime import datetime, timedelta

    from yuki.ui.calls import CallIndicator

    pill = CallIndicator()
    now = datetime.now().astimezone()
    pill.set_call({"status": "recording", "app": "Discord",
                   "detected_at": (now - timedelta(minutes=2)).isoformat(timespec="seconds"),
                   "session_started_at": (now - timedelta(minutes=95)).isoformat(timespec="seconds")})
    assert pill.elapsed(time.time()).startswith("1:35")                 # 95 minutes, not the row's 2
    pill.close()
