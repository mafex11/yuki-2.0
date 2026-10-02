"""Voice v3.16's visible bits, offscreen (QT_QPA_PLATFORM=offscreen; nothing is shown on screen): the pill's end
line, the tray icon when Yuki is not listening, and the call pill's "Yuki isn't listening". Run apart from the
non-GUI tests."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_pill_shows_the_end_line_for_three_seconds(app) -> None:
    from yuki.voice.indicator import ENDED_MS, VoicePill
    from yuki.voice.session import ENDED_NOTE

    pill = VoicePill()
    pill.show_state("ended", ENDED_NOTE)
    app.processEvents()
    assert pill.label.text() == ENDED_NOTE and not pill.wave.isVisible()
    assert pill._fade.isActive() and pill._fade.interval() == ENDED_MS == 3000
    pill.show_state("wake", "")     # the wake state that follows does not cut the line short
    assert pill._fade.isActive() and pill.label.text() == ENDED_NOTE
    image = pill.grab().toImage()   # it renders
    assert image.width() > 0 and image.height() > 0
    pill.close()


def test_the_tray_icon_shows_when_yuki_is_not_listening(app) -> None:
    from yuki.ui.app import NOT_LISTENING_LINES, tray_icon

    on = tray_icon(True).pixmap(64, 64).toImage()
    off = tray_icon(False).pixmap(64, 64).toImage()
    assert on != off
    assert set(NOT_LISTENING_LINES) == {"off", "quiet", "call_off"}
    assert all("isn't listening" in line for line in NOT_LISTENING_LINES.values())


def test_the_call_pill_says_yuki_isnt_listening(app) -> None:
    from yuki.ui.calls import NOT_LISTENING_TEXT, CallIndicator

    pill = CallIndicator()
    call = {"app": "Discord", "status": "recording", "session_started_at": None}
    pill.set_call(call)
    assert NOT_LISTENING_TEXT not in pill.label.text()
    pill.set_listening(False)
    assert pill.label.text().endswith(NOT_LISTENING_TEXT)
    pill.set_call(call)             # a fresh status keeps it
    assert pill.label.text().endswith(NOT_LISTENING_TEXT)
    pill.set_listening(True)
    assert NOT_LISTENING_TEXT not in pill.label.text()
    pill.close()
