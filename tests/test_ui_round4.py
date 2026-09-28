"""UI round 4 (2026-09-27): chat timing and streaming, formatted replies, plain display text, the masonry
layout, the question split, the user's own overlay shortcut; and the flat shadcn look that replaced glass (step
times that add up, plain step labels, no lone first letter, retired glass settings, flat dark surfaces)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# -- chat: the fold, the timing, streaming ----------------------------------------------------------------


def test_a_plain_reply_has_no_fold_and_times_from_the_message():
    from yuki.ui.chat_model import ChatModel

    m = ChatModel()
    m.apply({"type": "user", "text": "hi", "job": 1, "at": 100.0})
    m.apply({"type": "started", "job": 1, "at": 100.2})
    m.apply({"type": "tool_called", "job": 1, "name": "done", "label": "Wrapping up", "at": 104.9})
    m.apply({"type": "tool_finished", "job": 1, "name": "done", "ok": True, "summary": "", "at": 104.9})
    m.apply({"type": "finished", "job": 1, "text": "Hey.", "at": 105.0})
    item = m.items[-1]
    assert item.real_steps() == []
    assert item.work_seconds() == pytest.approx(5.0)


def test_the_reply_streams_and_the_thinking_shows_until_it_does():
    from yuki.ui.chat_model import ChatModel

    m = ChatModel()
    m.apply({"type": "user", "text": "summarise my day", "job": 2, "at": 10.0})
    m.apply({"type": "partial", "job": 2, "what": "thinking", "text": "Reading the timeline\nCounting the calls",
             "at": 11.0})
    item = m.items[-1]
    assert item.thinking == "Counting the calls" and item.text == ""
    m.apply({"type": "partial", "job": 2, "what": "reply", "text": "A", "at": 11.9})   # a lone first letter
    assert item.text == "" and item.thinking == "Counting the calls"
    m.apply({"type": "partial", "job": 2, "what": "reply", "text": "A calm morning", "at": 12.0})
    assert item.text == "A calm morning" and item.status == "streaming" and item.thinking == ""
    m.apply({"type": "partial", "job": 2, "what": "reply", "text": "A calm morning.", "at": 12.2})
    assert item.text == "A calm morning."
    # the turn went on working: the words shown so far are dropped until the next reply
    m.apply({"type": "tool_called", "job": 2, "name": "recall", "label": "Checking memory", "at": 12.5})
    assert item.text == "" and item.status == "running"
    m.apply({"type": "finished", "job": 2, "text": "A calm morning, one call.", "at": 14.0})
    assert item.text == "A calm morning, one call." and item.status == "done"


def test_stream_partial_reads_the_done_message_and_thinking():
    from yuki.agent import stream_partial as sp

    state: dict = {}
    assert sp.stream_partial(SimpleNamespace(type="content_block_start",
                                             content_block=SimpleNamespace(type="tool_use", name="done")),
                             state) is None
    part = sp.stream_partial(SimpleNamespace(type="input_json", snapshot={"message": "Hello there"}), state)
    assert part is not None and part.what == "reply" and part.text == "Hello there"
    # too soon after the last one, or no longer: nothing new
    assert sp.stream_partial(SimpleNamespace(type="input_json", snapshot={"message": "Hello there!"}), state) is None
    # another tool's input never reads as the reply
    sp.stream_partial(SimpleNamespace(type="content_block_start",
                                      content_block=SimpleNamespace(type="tool_use", name="click")), state)
    state["reply_at"] = 0.0
    assert sp.stream_partial(SimpleNamespace(type="input_json", snapshot={"message": "x" * 40}), state) is None
    think = sp.stream_partial(SimpleNamespace(type="thinking", snapshot="Looking at the day"), {})
    assert think is not None and think.what == "thinking"


# -- formatted replies, plain display text ---------------------------------------------------------------


def test_markdown_renders_lists_bold_code_and_links(qapp):
    from yuki.ui.markdown import to_html
    from yuki.ui.window.theme import theme

    html = to_html("Short summary.\n\n## Steps\n1. Run `uv sync`\n2. **Then** test\n\n- a [link](https://x.y)",
                   theme("dark"))
    assert "Short summary." in html and "font-weight:700" in html
    assert "1." in html and "2." in html and "•" in html
    assert "Cascadia" in html and 'href="https://x.y"' in html


def test_plain_drops_dashes_asides_and_machine_names():
    from yuki.ui.window.data import plain

    assert plain("Focus it with focus_window (expected hwnd 3) — then click.") == \
        "Focus it with focus window, then click."


def test_question_split():
    from yuki.ui.nudges import split_question

    assert split_question("Who's Priya to you? She's on the invoice.") == ("Who's Priya to you?",
                                                                            "She's on the invoice.")
    assert split_question("Who's Priya to you?") == ("Who's Priya to you?", "")


# -- layout ------------------------------------------------------------------------------------------------


def test_masonry_cards_keep_their_own_height_and_balance(qapp):
    from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

    from yuki.ui.window.widgets import Masonry

    host = QWidget()
    lay = Masonry(host, columns=2, spacing=10, min_col=100)
    cards = []
    for h in (40, 300, 60, 50):
        w = QWidget(host)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        lab = QLabel("x")
        lab.setFixedHeight(h)
        v.addWidget(lab)
        lay.addWidget(w)
        cards.append(w)
    host.resize(420, 600)
    host.show()
    qapp.processEvents()
    heights = [c.height() for c in cards]
    assert heights == [40, 300, 60, 50]            # never stretched to a neighbour's height
    assert cards[1].x() > cards[0].x()             # side by side
    assert cards[2].x() == cards[0].x() and cards[3].x() == cards[0].x()   # the short column takes the next ones


# -- the user's overlay shortcut ------------------------------------------------------------------------------


def test_user_hotkey_from_ui_json(tmp_path, monkeypatch):
    from yuki.ui import ui_settings
    from yuki.ui.app import user_bindings

    path = tmp_path / "ui.json"
    monkeypatch.setattr(ui_settings, "settings_path", lambda: path)
    monkeypatch.delenv("YUKI_HOTKEY", raising=False)
    base = {"toggle": "alt+shift", "cancel": "alt+shift+x", "voice": "ctrl+alt+y"}
    assert user_bindings(base) == base
    ui_settings.update(open_hotkey="ctrl+space")
    assert user_bindings(base)["toggle"] == "ctrl+space"
    monkeypatch.setenv("YUKI_HOTKEY", "alt+q")      # a try-out override still wins
    assert user_bindings(base) == base


# -- step times, step labels, streaming -------------------------------------------------------------------


def test_step_times_add_up_to_the_wait_with_a_writing_tail():
    from yuki.ui.chat_model import ChatModel, fmt_seconds

    m = ChatModel()
    m.apply({"type": "user", "text": "what happened on the call?", "job": 3, "at": 100.0})
    m.apply({"type": "started", "job": 3, "at": 100.1})
    m.apply({"type": "tool_called", "job": 3, "name": "calls", "label": "Checking your calls", "at": 111.0})
    m.apply({"type": "tool_finished", "job": 3, "name": "calls", "ok": True, "summary": "2 calls", "at": 111.1})
    m.apply({"type": "tool_called", "job": 3, "name": "call_timeline", "label": "Going through the call",
             "at": 122.0})
    m.apply({"type": "tool_finished", "job": 3, "name": "call_timeline", "ok": True, "summary": "6 blocks",
             "at": 122.05})
    item = m.items[-1]
    times, tail = item.step_times(now=130.0)
    first, second = item.real_steps()
    assert times[first.key] == pytest.approx(11.1)      # the model's thinking before it counts
    assert times[second.key] == pytest.approx(10.95)
    assert tail == (pytest.approx(7.95), True)          # still writing the reply
    assert sum(times.values()) + tail[0] == pytest.approx(item.work_seconds(130.0))
    m.apply({"type": "finished", "job": 3, "text": "Two things happened.", "at": 131.0})
    times, tail = item.step_times()
    assert tail == (pytest.approx(8.95), False)
    assert fmt_seconds(0.05) == "<1 s" and fmt_seconds(4.2) == "4 s" and fmt_seconds(65) == "1 min 5 s"


def test_step_labels_are_plain_words():
    from datetime import date, timedelta

    from yuki.ui.chat_model import ChatModel
    from yuki.ui.status import describe_result, describe_tool

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    assert describe_tool("calls", {"since": yesterday, "until": yesterday}) == "Checking your calls yesterday"
    assert describe_tool("calls", {"since": "2026-01-05"}) == "Checking your calls since 5 Jan"
    assert "call:23" not in describe_tool("call_timeline", {"call": "call:23"})   # a record id means nothing
    assert describe_tool("launch_app", {"name": "spotify"}).endswith("spotify")
    better = describe_result("call_timeline", None, {"call": {"app": "Discord", "start": f"{yesterday}T22:09:00"}})
    assert better == "Reading yesterday's 22:09 Discord call"
    assert describe_result("calls", None, {"calls": []}) == ""
    m = ChatModel()   # the result's better line replaces the step's
    m.apply({"type": "user", "text": "x", "job": 4, "at": 1.0})
    m.apply({"type": "tool_called", "job": 4, "name": "call_timeline", "label": "Going through the call…", "at": 2.0})
    m.apply({"type": "tool_finished", "job": 4, "name": "call_timeline", "ok": True, "summary": "", "at": 2.1})
    m.apply({"type": "tool_label", "job": 4, "name": "call_timeline", "label": better, "at": 2.1})
    assert m.items[-1].real_steps()[0].label == better


def test_the_tool_result_carries_its_payload_for_the_label():
    from yuki.log import ToolResult

    r = ToolResult("call_timeline", True, "6 blocks", payload={"call": {"app": "Discord"}})
    assert r == ToolResult("call_timeline", True, "6 blocks") and "payload" not in repr(r)


# -- the flat look (2026-09-27: glass removed) -------------------------------------------------------------


def test_retired_glass_settings_are_ignored_and_dropped(tmp_path):
    import json

    from yuki.ui import ui_settings

    path = tmp_path / "ui.json"
    path.write_text(json.dumps({"glass": "full", "bend": 2.0, "transparency": 1.9, "background": "see-through",
                                "appearance": "light", "reduce_motion": True, "future_key": 1}), encoding="utf-8")
    s = ui_settings.load(path)
    assert s.reduce_motion is True and not hasattr(s, "glass")
    ui_settings.update(path, open_hotkey="ctrl+space")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert not ui_settings.RETIRED & set(raw)
    assert raw["future_key"] == 1 and raw["open_hotkey"] == "ctrl+space"


def test_the_theme_is_horizon_field_unit_dark():
    from yuki.ui.window.theme import ACCENTS, RADIUS, theme

    t = theme("light", accent="green")   # dark only: any name gives the one theme
    # round 2: near-black charcoal, panels one small step up from it
    assert t.dark and t.bg.name() == "#07080a" and t.card.name() == "#0c0e11" and t.border.name() == "#181b21"
    assert t.text.name() == "#e4e6e9" and t.text_2.name() == "#a4aab3" and t.text_3.name() == "#6b717b"
    assert t.primary.name() == "#e4e6e9" and t.primary_fg.name() == "#07080a"
    assert t.accent.name() == ACCENTS["green"].lower() == "#748f42"
    assert theme(accent="orange").accent.name() == "#e4571c"
    # low contrast between panel and window: no channel more than 8 apart
    assert max(abs(a - b) for a, b in zip(t.card.getRgb()[:3], t.bg.getRgb()[:3])) <= 8
    for name, surface in t.surfaces.items():   # opaque: nothing see-through anywhere
        assert surface.fill.alpha() == 255, name
    # no amber or yellow anywhere: every kind colour is monochrome, the cool call grey or the error red
    for kind, c in t.kinds.items():
        assert c.name() in {t.text_2.name(), t.cool.name(), t.danger.name()}, kind
    assert 14 <= RADIUS["field"] <= RADIUS["display"] <= RADIUS["panel"] <= RADIUS["floating"] <= 20


def test_surfaces_paint_opaque_with_a_border(qapp):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QImage

    from yuki.ui.nudges import NudgeCard

    card = NudgeCard(linger_ms=1000)
    card.set_nudge({"id": 1, "kind": "nudge", "text": "Stretch?", "at": None})
    card.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    card.show()
    img = QImage(card.size(), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    card.render(img)
    inner = card.panel_rect().center()
    assert img.pixelColor(inner).alpha() == 255 and img.pixelColor(inner).name() == "#0c0e11"
    edge = img.pixelColor(QPoint(card.panel_rect().left() + 60, card.panel_rect().top()))
    assert edge.name() == "#1b1f26"
    assert card.title.text() == "" and card.wave.kind == "checkin"   # a plain check-in: its wave says it
    card.close()
