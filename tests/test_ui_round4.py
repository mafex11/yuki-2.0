"""UI round 4 (2026-09-27): chat timing and streaming, formatted replies, plain display text, the masonry
layout, the question split, the user's own overlay shortcut and the glass settings."""

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
    m.apply({"type": "partial", "job": 2, "what": "reply", "text": "A calm", "at": 12.0})
    assert item.text == "A calm" and item.status == "streaming" and item.thinking == ""
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


# -- glass settings ---------------------------------------------------------------------------------------


def test_transparency_scales_the_glass():
    from yuki.ui import lens
    from yuki.ui.window.theme import theme

    t = theme("dark")
    lvl = t.level("regular")
    saved = lens.policy()
    try:
        lens.set_policy(lens.GlassPolicy("light", transparency=0.0))
        dense = lens.scaled_level(lvl).fill.alphaF()
        lens.set_policy(lens.GlassPolicy("light", transparency=2.0))
        clear = lens.scaled_level(lvl).fill.alphaF()
        lens.set_policy(lens.GlassPolicy("full"))
        full_h = lens.scaled_optics(t.optics["raised"]).height
        lens.set_policy(lens.GlassPolicy("light"))
        light_h = lens.scaled_optics(t.optics["raised"]).height
    finally:
        lens.set_policy(saved)
    assert dense > lvl.fill.alphaF() > clear
    assert full_h > light_h > 0                     # Full bends the most
