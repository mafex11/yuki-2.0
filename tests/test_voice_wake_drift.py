"""A drifted name end never replays old audio (live 4 Oct: the spotter put the name's end 7-39 s before its batch)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QObject

from yuki.config import Settings


def _session(tmp: Path) -> Any:
    from yuki.voice.session import VoiceSession

    settings = Settings(log_dir=tmp / "sessions", requests_csv=tmp / "r.csv", voice_wake=False)
    session = VoiceSession(settings, QObject(), None, prefs_path=tmp / "voice.json")
    # this PC's own state (a full-screen window or game in front, a call, a recent goodbye) makes the wake strict
    # (vGame) and changes where listening starts: keep it out of the test
    session._game_now = lambda: None
    session._call_focus = lambda: False
    session._after_goodbye = lambda: False
    return session


def _starts(session: Any, *calls: dict) -> list[tuple]:
    seen: list[tuple] = []

    class _Thread:
        def __init__(self, target: Any, args: tuple, **_: Any) -> None:
            seen.append(args)

        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return False

    import yuki.voice.session as session_module

    real = session_module.threading.Thread
    session_module.threading.Thread = _Thread
    try:
        for kw in calls:
            session.start_conversation("wake", **kw)
    finally:
        session_module.threading.Thread = real
    return seen


def test_a_name_end_seconds_before_its_batch_is_ignored(tmp_path: Path) -> None:
    from yuki.voice.session import WAKE_PREROLL_S

    seen = _starts(_session(tmp_path), dict(wake_at=30.0, heard_at=30.4, name_end=22.99))
    (_s, _v, listen, _st, _c, cut), = seen
    assert listen == pytest.approx(30.0 - WAKE_PREROLL_S)   # the batch with the name, as when the end is unknown
    assert cut is False                                     # the name may be in the replay


def test_a_plausible_name_end_is_still_used(tmp_path: Path) -> None:
    seen = _starts(_session(tmp_path), dict(wake_at=30.0, heard_at=30.4, name_end=29.6))
    (_s, _v, listen, _st, _c, cut), = seen
    assert listen == 29.6 and cut is True
