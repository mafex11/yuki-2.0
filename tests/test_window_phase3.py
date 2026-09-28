"""Phase 3 of the Yuki window (2026-09-27 review, restyled 2026-09-28): no flashing windows, corrections that
show as pending until applied, the call band, to-dos, smooth scrolling, helper steps and the tray's notices.

Offscreen only (rule 7), on temporary databases.
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _shown_windows(qapp) -> list[str]:
    return [type(w).__name__ for w in qapp.topLevelWidgets() if w.isVisible()]


def test_item_row_never_shows_a_parentless_window(qapp):
    """The Me page flashed 10-20 tiny windows: a pill shown before it had a parent is a top-level window."""
    from yuki.ui.window.theme import theme
    from yuki.ui.window.widgets import ItemRow

    before = set(_shown_windows(qapp))
    rows = [ItemRow(theme("dark"), f"fact:{i}", "Elsa", "A friend.", "Seen in Discord · today",
                    evidence=[("26 Sep 10:00", "Discord", "said hi")] if i % 2 else []) for i in range(12)]
    qapp.processEvents()
    assert set(_shown_windows(qapp)) == before
    assert rows[0].why_pill.isHidden() and not rows[1].why_pill.isHidden()   # "Why?" only with evidence


def test_a_sent_correction_shows_the_users_words_as_updating(qapp):
    from yuki.ui.window.theme import theme
    from yuki.ui.window.widgets import ItemRow

    got = []
    row = ItemRow(theme("light"), "fact:7", "Elsa", "Elsa is a colleague at Corvana.", "Seen in Slack · today")
    row.correct.connect(lambda ref, words: got.append((ref, words)))
    row.toggle_composer()
    row.field.submitted.emit("Elsa is my friend from college")
    assert got == [("fact:7", "Elsa is my friend from college")]
    assert "Elsa is my friend from college" in row.text_label.text()
    assert row.source_label.text().startswith("Updating")      # plain words, no chip (round 4)
    assert "Elsa is a colleague" in row.was_label.text()          # the old text is shown as the old text
    late = ItemRow(theme("dark"), "fact:8", "Bro", "x", "y", pending={"kind": "correct", "words": "w", "at": 1,
                                                                        "late": True})
    assert late.source_label.text().startswith("Still updating")


def test_pending_corrections_persist_and_resolve_when_memory_applies_them(qapp, tmp_path, monkeypatch):
    from yuki.memory.api import MemoryClient
    from yuki.ui.window.app import WindowApp
    from yuki.ui.window.data import Item

    memory = MemoryClient.open(tmp_path / "memory.db")
    try:
        ctrl = WindowApp(memory, db_path=tmp_path / "memory.db", connect=False, ui_path=tmp_path / "ui.json")
        item = Item("fact:3", "Elsa", "Elsa is a colleague.", "Elsa is a colleague.", "person", None, "model", 0.0,
                    0.6, "")
        ctrl._record_pending(item, "correct", "Elsa is my friend")
        again = WindowApp(memory, db_path=tmp_path / "memory.db", connect=False, ui_path=tmp_path / "ui.json")
        assert again.pending["fact:3"]["words"] == "Elsa is my friend"     # reopening never shows stale text
        other = WindowApp(memory, db_path=tmp_path / "other.db", connect=False, ui_path=tmp_path / "ui.json")
        assert other.pending == {}                                         # a different database's file is ignored
        # memory applied it: the fact is gone (superseded) -> resolved, the file is removed
        monkeypatch.setattr(memory, "portrait_facts_with_evidence", lambda *a, **k: [])
        again._check_pending()
        deadline = time.time() + 5
        while again.pending and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        assert again.pending == {} and not again._pending_path().exists()
        # memory's own progress (correction_status) decides when it tracks the correction
        monkeypatch.setattr(memory, "portrait_facts_with_evidence", lambda *a, **k: [
            {"ref": "fact:4", "text": "Bro is your cousin."}])
        states = {"state": "updating"}
        monkeypatch.setattr(memory, "correction_status", lambda ref: dict(states))
        item4 = Item("fact:4", "Bro", "Bro is your cousin.", "", "person", None, "model", 0.0, 0.6, "")
        again._record_pending(item4, "correct", "Bro is my brother")
        again.pending["fact:4"]["fid"] = 12

        def settle() -> None:
            again._check_pending()
            end = time.time() + 3
            while getattr(again, "_pending_busy", False) and time.time() < end:
                qapp.processEvents()
                time.sleep(0.02)

        settle()
        assert "fact:4" in again.pending                                   # still updating: kept, words shown
        states["state"] = "done"
        settle()
        assert "fact:4" not in again.pending
        for c in (ctrl, again, other):
            c.win.close()
    finally:
        memory.close()


def test_call_band_hover_and_click_and_the_call_row_in_a_moment(qapp):
    """The skyline's call band (Horizon): hovering it names the call, a click picks it and the panel under the
    chart says its real times; a picked moment lists the call as one of its apps."""
    from datetime import date, datetime

    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from yuki.ui.window.data import Band, DayView, Lane, Segment, Session
    from yuki.ui.window.pages import today
    from yuki.ui.window.skyline import apps_line, moment_at
    from yuki.ui.window.theme import theme

    d = date(2026, 9, 26)
    since = datetime(2026, 9, 26).timestamp()
    s, e = since + 20 * 3600, since + 23 * 3600
    lanes = [Lane("warp", "Warp", 3600, color=0), Lane("chrome", "Chrome", 1800, color=1)]
    segs = [Segment(s, s + 3600, "warp", "present", 3600), Segment(s + 3600, s + 5400, "chrome", "present", 1800)]
    call = Band("call", s + 600, e, "Discord call", "Discord", s + 600, e, "", [], 9000.0, True)
    sess = Session(s, s + 5400, "warp", "Coding", 5400, [("Warp", 3600), ("Chrome", 1800)], ["20:00-21:30: coding"], [])
    day = DayView(d, since, since + 86400, since + 86400, lanes, segs, [call], [], [], [sess], {
        "present_s": 5400, "away_s": 0, "switches": 3, "call_s": 9000, "off_s": 0, "asleep_s": 0, "fullscreen_s": 0,
        "gap_s": 0}, "", [], [], s, e)
    page = today.build(theme("dark"), day, selected=0)
    page.resize(1200, 900)
    page.show()
    qapp.processEvents()
    chart = page.chart
    x, y = chart.x_of(s + 3000), chart.TH
    assert chart.call_at(x, y) == 0 and chart.call_at(x, chart.base - 20) is None
    clicked = []
    chart.call_clicked.connect(clicked.append)
    for kind in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
        QApplication.sendEvent(chart, QMouseEvent(kind, QPointF(x, y), QPointF(x, y), Qt.MouseButton.LeftButton,
                                                  Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    assert clicked == [0] and chart.selected_call == 0
    stamp, title, _apps, _note = page.moment_panel.lines()
    assert title.startswith("Discord call") and "still going" in title and stamp == "20:10"
    # a moment in the session lists the call as a row of its apps, in words (no badge)
    line = apps_line(moment_at(day, s + 1200))
    assert "Warp 1h 00m" in line and "Discord call ongoing" in line and line.endswith("from 20:00 to 21:30")
    page.close()


def test_todos_close_by_id_and_wrong_ones_tell_memory(qapp, tmp_path):
    from yuki.memory.api import MemoryClient
    from yuki.ui.window.app import WindowApp

    memory = MemoryClient.open(tmp_path / "memory.db")
    try:
        ref = memory.add_todo("Laminate the certificates for Bro")
        ctrl = WindowApp(memory, db_path=tmp_path / "memory.db", connect=False, ui_path=tmp_path / "ui.json")
        todo = next(t for t in memory.todos() if t["id"] == ref)
        ctrl.todo_close(todo, "wrong")
        deadline = time.time() + 5
        while memory.todos() and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        assert memory.todos() == []                                         # ended by id through end_todo
        ctrl.win.close()
    finally:
        memory.close()


def test_yuki_page_is_about_this_user_only(qapp, tmp_path):
    from yuki.memory.api import MemoryClient
    from yuki.ui.window import data as D
    from yuki.ui.window.pages import yuki_page
    from yuki.ui.window.theme import theme

    assert D.portrait_section("Work\nx\n\nRelationship\nBe brief. No em-dashes.\n\nPeople\n- a", "Relationship") == \
        "Be brief. No em-dashes."
    assert D.sentences("Be brief. No em-dashes.") == ["Be brief.", "No em-dashes."]
    memory = MemoryClient.open(tmp_path / "memory.db")
    try:
        view = D.build_yuki(memory)
        assert not hasattr(view, "rules")                                  # the rules list left this page
        page = yuki_page.build(theme("light"), view)
        from PySide6.QtWidgets import QLabel

        texts = " ".join(lab.text() for lab in page.findChildren(QLabel))
        assert "Your to-dos" in texts and "How Yuki talks to you" in texts and "Yuki's personality with you" in texts
        assert "Who Yuki is" not in texts and "promises" not in texts.lower()
    finally:
        memory.close()


def test_smooth_scroller_eases_to_the_target_and_glides_after_a_flick(qapp):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication, QLabel

    from yuki.ui.window.widgets import scroll

    area = scroll(QLabel("\n".join(f"line {i}" for i in range(400))))
    area.resize(300, 300)
    area.show()
    qapp.processEvents()
    s = area._smooth
    vp = area.viewport()

    def wheel(dy: int) -> None:
        pos = QPointF(50, 50)
        QApplication.sendEvent(vp, QWheelEvent(pos, QPointF(vp.mapToGlobal(pos.toPoint())), QPoint(0, 0),
                                               QPoint(0, dy), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                               Qt.ScrollPhase.NoScrollPhase, False))

    wheel(-120)
    assert area.verticalScrollBar().value() < 20                       # not a jump: it eases there
    end = time.time() + 1.0
    while time.time() < end:
        qapp.processEvents()
        time.sleep(0.005)
    first = area.verticalScrollBar().value()
    assert 60 <= first <= 140 and not s.timer.isActive()               # ~100 px, then idle (no timer)
    for _ in range(8):                                                 # a touchpad flick: small deltas, fast
        wheel(-16)
        qapp.processEvents()
        time.sleep(0.008)
    lifted = area.verticalScrollBar().value()
    end = time.time() + 1.5
    while time.time() < end:
        qapp.processEvents()
        time.sleep(0.005)
    assert area.verticalScrollBar().value() > lifted                   # it glided on after the fingers lifted
    assert not s.timer.isActive()
    area.close()


def test_helper_steps_show_under_the_response_labelled_as_helpers():
    from yuki.ui.chat_model import ChatModel

    m = ChatModel()
    m.apply({"type": "user", "text": "what did Varun say?", "job": 5})
    m.apply({"type": "helper_step", "job": 5, "helper": 1, "state": "started", "text": "search Slack for Varun"})
    m.apply({"type": "helper_step", "job": 5, "helper": 1, "state": "tool", "text": "recall: 3 facts", "ok": True})
    m.apply({"type": "helper_step", "job": 5, "helper": 1, "state": "finished", "text": "Varun owes you the scope",
             "ok": True})
    reply = [i for i in m.items if i.kind == "assistant"][0]
    head, tool = reply.steps
    assert head.kind == "helper" and head.label.startswith("Helper 1") and head.result == "Varun owes you the scope"
    assert not head.running and tool.depth == 1 and tool.name == "recall"


def test_pills_say_their_state_in_words_with_a_wave(qapp):
    """The call pill: the accent recording dot, a grey wave and a dot-matrix timer; waiting for headphones: no
    dot, no timer, said plainly (no amber). The voice pill: a wave per state, an error in red, no wave."""
    import time as _time

    from yuki.ui.calls import CallIndicator
    from yuki.voice.indicator import VoicePill

    ind = CallIndicator()
    ind.set_call({"app": "Discord", "status": "recording", "detected_at": _time.time() - (37 * 60 + 12)})
    assert "Discord" in ind.label.text() and not ind.dot.isHidden()
    assert ind.elapsed() in ("37:12", "37:13") and ind.timer_label.text in ("37:12", "37:13")
    waiting = CallIndicator()
    waiting.set_call({"app": "Zoom", "status": "waiting_headphones"})
    assert waiting.dot.isHidden() and waiting.timer_label.isHidden()
    assert "245,158,11" not in waiting.label.styleSheet()                 # no amber anywhere
    pill = VoicePill()
    pill.show_state("speaking")
    assert pill.label.text() == "Speaking" and pill.wave.kind == "speak" and "228,230,233" in pill.label.styleSheet()
    pill.show_state("thinking")
    assert pill.wave.kind == "think"
    pill.show_state("error", "The microphone is in use")
    assert pill.label.text().startswith("The microphone") and pill.wave.isHidden()
    assert "229,115,107" in pill.label.styleSheet()                        # the error red
    for w in (ind, waiting, pill):
        w.close()


def test_notice_card_shows_a_runtime_notice(qapp):
    from PySide6.QtCore import Qt

    from yuki.ui.nudges import NoticeCard

    card = NoticeCard(linger_ms=5000)
    card.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    card.show_notice("cost", "You've spent $3.10 on models today, past your $3 alert.")
    assert card.title.text() == "Today's spend" and card.reply_button.isHidden() and card.dismiss_button.text() == "OK"
    card.close()


def test_health_check_panel_lists_checks_with_fixes(qapp):
    from yuki.core.doctor import Check
    from yuki.ui.memory import health_html

    out = health_html([Check("Memory service", "fail", "not running", "Start Yuki from the tray"),
                       Check("GPU", "ok", "RTX 2060")])
    assert "1 problem" in out and "What to do: Start Yuki from the tray" in out
    assert out.index("Memory service") < out.index("GPU")                 # problems first
