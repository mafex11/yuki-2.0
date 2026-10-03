"""Calls on both sides in the UI (report 61b), offscreen (QT_QPA_PLATFORM=offscreen: nothing is shown on screen, no
tray icon is added, no focus is taken, no microphone or speaker is touched).

Since 3 Oct 2026 a call keeps the user's mic (headphones only) and the call app's own sound, and a game's voice chat
is a session of its own with its own track (report 60). The consent cards, the island, the tray, both windows'
words and Stop are built from ``call_status()``'s ``mic``, ``tracks``, ``sessions``, ``kind`` and ``pending_card``.
The apps here are invented ("Parley" a call app, "Frontline" a game): nothing in the UI knows any app by name, the
words come from the data (handbook rule 3)."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

CALL_APP, GAME_APP = "Parley", "Frontline"


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from yuki.ui import lattice as L

    L.REDUCED_OVERRIDE[0] = False
    yield app
    L.REDUCED_OVERRIDE[0] = None


def _iso(minutes_ago: float) -> str:
    return (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def _row(row_id: int, app: str, kind: str, status: str = "recording", **extra) -> dict:
    """A ``call_status()["call"]`` row (the fields the API gives)."""
    return {"id": row_id, "app": app, "kind": kind, "status": status, "asking": False, "consent": "always",
            "headphones": True, "detected_at": _iso(3), "session_started_at": _iso(12), "recording_since": _iso(3),
            "ask_timeout_s": 120, "can_remember_choice": True, **extra}


def _session(row: dict, *, ended: bool = False) -> dict:
    return {"id": row["id"], "app": row["app"], "kind": row["kind"], "status": row["status"],
            "asking": bool(row.get("asking")), "ended": ended}


def _track(row: dict, state: str = "recording", reason: str | None = None) -> dict:
    return {"call_id": row["id"], "app": row["app"], "process": f"{row['app'].lower()}.exe",
            "track_kind": row["kind"], "state": state, "reason": reason, "speech_s": 12.5}


def _mic(state: str = "recording", call_id: int | None = 1, reason: str | None = None) -> dict:
    return {"state": state, "call_id": call_id, "reason": reason, "speech_s": 40.0}


def _status(call: dict | None, rows: list[dict], mic: dict, tracks: list[dict], *, recording: bool = True,
            pending: dict | None = None) -> dict:
    sessions = [_session(r) for r in rows] + ([_session(pending, ended=True)] if pending else [])
    return {"call": call, "recording": recording, "waiting": {"calls": 0, "minutes": 0.0}, "sessions": sessions,
            "mic": mic, "tracks": tracks, "pending_card": pending}


CALL = _row(1, CALL_APP, "call")
GAME = _row(2, GAME_APP, "game")


def call_only() -> dict:
    return _status(CALL, [CALL], _mic(call_id=1), [_track(CALL)])


def game_only() -> dict:
    return _status(GAME, [GAME], _mic(call_id=2), [_track(GAME)])


def call_and_game() -> dict:
    return _status(CALL, [CALL, GAME], _mic(call_id=1), [_track(CALL), _track(GAME)])


def mic_waits_for_headphones() -> dict:
    call = dict(CALL, status="waiting_headphones", headphones=False, form_factor="speakers")
    return _status(call, [call], _mic("paused", 1, "not_headphones"), [_track(call)])


class _Memory:
    """A stand-in for the shared memory access: records every call the controller makes."""

    installed = True

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def call_method(self, name: str, *args, timeout_s: float | None = None):
        del timeout_s
        self.calls.append((name, *args))
        return True


def _controller(memory: _Memory):
    from yuki.ui.calls import CallController

    events: list[tuple] = []
    ui_log = SimpleNamespace(event=lambda name, **fields: events.append((name, fields)))
    ctl = CallController(memory, ui_log, screen_state=lambda: (5, "accepts"))
    return ctl, events


def _pump(qapp, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)


# -- the words: what is kept, from mic and tracks --------------------------------------------------------------------


def test_the_indicator_says_what_is_kept_for_a_call_a_game_both_and_no_headphones():
    from yuki.ui.call_words import call_rec, kept, live_view

    assert kept(call_only())["line"] == f"Recording · {CALL_APP} + you"
    assert kept(game_only())["line"] == f"Recording · {GAME_APP} voice + you"
    both = kept(call_and_game())
    assert both["line"] == f"Recording · {CALL_APP} + {GAME_APP} voice + you"        # both, briefly
    assert both["short_line"] == f"Recording · {CALL_APP} + {GAME_APP} + you"        # the narrow pill's form
    assert both["sentence"] == f"Your mic, {CALL_APP}’s sound and {GAME_APP}’s voice chat."
    waits = kept(mic_waits_for_headphones())
    assert waits["line"] == f"Recording · {CALL_APP} (your mic waits for headphones)"
    assert waits["sentence"] == f"{CALL_APP}’s sound. Your mic waits for headphones."
    # the call's own sound switched off in the privacy file: the mic only, said so
    off = _status(CALL, [CALL], _mic(call_id=1), [_track(CALL, "off", "record_others_off")])
    assert kept(off)["line"] == f"Recording · you on {CALL_APP}" and kept(off)["sentence"] == "Your mic."
    # nothing captured while it waits for headphones and the call's sound is off: no rec, no "Recording"
    idle = dict(CALL, status="waiting_headphones")
    view = live_view(_status(idle, [idle], _mic("paused", 1, "not_headphones"),
                             [_track(idle, "off", "record_others_off")], recording=False))
    assert view is not None and view["line"] == f"Waiting for headphones · {CALL_APP}" and not call_rec(view)
    # an app track that is still opening counts (it records within a poll)
    opening = _status(CALL, [CALL], _mic(call_id=1), [_track(CALL, "starting")])
    assert kept(opening)["line"] == f"Recording · {CALL_APP} + you"
    # no game ever reads as a call
    assert "call" not in kept(game_only())["line"] and live_view(game_only())["where"] == f"{GAME_APP} voice chat"


def test_the_call_card_says_both_sides_and_its_headphones_note_is_true(qapp):
    from yuki.ui.call_words import card_target, consent_text
    from yuki.ui.calls import CallConsentCard

    asking = dict(CALL, status="asking", asking=True, consent=None, headphones=False, form_factor="speakers")
    target = card_target(_status(asking, [asking], _mic("asking", None), [_track(asking, "asking")],
                                 recording=False))
    question, detail, note = consent_text(target)
    assert question == f"You’re on a {CALL_APP} call."
    assert detail == (f"Keep a private timeline of this call? Your mic and {CALL_APP}’s sound, turned into text later "
                      "on this PC.")
    assert note == (f"Your mic waits for headphones (your sound is on speakers now); {CALL_APP}’s sound is kept "
                    "either way.")
    assert "never theirs" not in detail and "your side" not in detail.lower()
    # the call's own sound is off: the card never promises it
    off = card_target(_status(asking, [asking], _mic("asking", None), [_track(asking, "off", "record_others_off")],
                              recording=False))
    q2, d2, n2 = consent_text(off)
    assert off["keeps_sound"] is False and "Only your mic" in d2 and f"{CALL_APP}’s sound" not in d2
    assert n2.startswith("Only with headphones, so other people’s voices aren’t picked up.")
    card = CallConsentCard()
    try:
        card.set_call(target)
        assert card.question.text() == question and card.detail.text() == detail and card.note.text() == note
        assert card.yes_button.text() == "Record this call" and card.no_button.text() == "Not this call"
        assert card.head.text().startswith(f"Call · {CALL_APP} · ")
        assert card.always_button.text() == f"Always for {CALL_APP}"
    finally:
        card.close()


def test_the_game_card_is_about_its_voice_chat_never_a_call(qapp):
    from yuki.ui.call_words import card_target
    from yuki.ui.calls import CallConsentCard

    asking = dict(GAME, status="asking", asking=True, consent=None)
    target = card_target(_status(asking, [asking], _mic("asking", None), [_track(asking, "asking")],
                                 recording=False))
    card = CallConsentCard()
    got = []
    card.answered.connect(lambda row, answer: got.append((row["id"], answer)))
    try:
        card.set_call(target)
        assert card.question.text() == f"Remember {GAME_APP} voice chat?"
        assert "call" not in card.question.text().lower()
        assert card.detail.text() == ("Your mic and the game’s voice chat, as their own track, turned into text "
                                      "after you play, on this PC.")
        assert card.head.text().startswith(f"Game voice · {GAME_APP} · ")
        assert (card.yes_button.text(), card.no_button.text()) == ("Record it", "Not this time")
        assert card.always_button.text() == f"Always for {GAME_APP}"
        card.always_button.setChecked(True)
        card.no_button.click()                              # Not this time + Always = never (as for a call)
        assert got == [(2, "never")]
    finally:
        card.close()


# -- the cards: ask_later never, pending_card shown and answered -----------------------------------------------------


def test_ask_later_never_shows_a_card(qapp):
    from yuki.ui.island import Island, on_call_status

    memory = _Memory()
    ctl, _events = _controller(memory)
    later = dict(GAME, status="ask_later", asking=False, consent=None)
    isl = Island(bindings={})
    isl.screen_state = lambda: (5, "accepts")
    try:
        # memory never makes a waiting game the call: it is only in sessions
        status = _status(None, [later], _mic("asking", None), [_track(later, "asking")], recording=False)
        ctl.offer(status)
        on_call_status(isl, status)
        assert not ctl.card_up and isl.inputs.call_asking == "" and isl.inputs.call is None
        # and were it ever handed over as the call (or as a card), still no card: it waits until it can be seen
        ctl.offer(dict(status, call=dict(later, asking=True)))
        assert not ctl.card_up
        ctl.offer(dict(status, pending_card={**later, "asking": True}))
        assert not ctl.card_up and memory.calls == []
    finally:
        ctl.stop()
        isl.stop()


def test_a_games_card_after_its_session_is_shown_and_answered_once(qapp):
    from yuki.ui.island import Island, on_call_status
    from yuki.ui.tray import TrayWatch

    memory = _Memory()
    ctl, events = _controller(memory)
    pending = {"id": 31, "app": GAME_APP, "kind": "game", "status": "asking", "asking": True, "consent": None,
               "detected_at": _iso(40), "ended_at": _iso(1), "ask_timeout_s": 120, "can_remember_choice": True}
    status = _status(None, [], _mic("off", None), [], recording=False, pending=pending)
    isl = Island(bindings={})
    isl.screen_state = lambda: (5, "accepts")
    tray = SimpleNamespace(tip="", setIcon=lambda i: None, setToolTip=lambda t: setattr(tray, "tip", t))
    try:
        ctl.offer(status)
        on_call_status(isl, status)
        assert ctl.card_up and ctl.card.call["id"] == 31 and ctl.card.call["ended"] is True
        card = ctl.card
        assert card.question.text() == f"Remember {GAME_APP} voice chat?"
        assert "Nothing was kept from the game that just ended" in card.detail.text()
        assert f"Always for {GAME_APP} holds for the next one" in card.detail.text()
        assert (card.yes_button.text(), card.no_button.text()) == ("Yes", "No")
        assert card.head.text().startswith(f"Game voice · {GAME_APP} · ")
        assert any(name == "call_consent_shown" and f.get("ended") and f.get("kind") == "game" for name, f in events)
        # the tray's face and card: a card waits, about the game's voice chat
        watch = TrayWatch(tray, isl, waiting=lambda: 0)
        watch.refresh()
        assert watch.state == "call-ask"
        assert watch.card_info().line == f"{GAME_APP} voice chat · not recording"
        # answered through the same path as every card: answer_call(id, answer), once
        card.always_button.setChecked(True)
        card.yes_button.click()
        ctl.wait()
        assert memory.calls == [("answer_call", 31, "always")]
        ctl.offer(status)                                   # a late fetch before memory took it: not shown again
        assert not ctl.card_up
        ctl.offer(_status(None, [], _mic("off", None), [], recording=False))
        assert not ctl.card_up and memory.calls == [("answer_call", 31, "always")]
    finally:
        ctl.stop()
        isl.stop()


def test_a_live_card_gives_way_to_the_next_rows_card(qapp):
    memory = _Memory()
    ctl, _events = _controller(memory)
    asking = dict(CALL, status="asking", asking=True, consent=None)
    pending = {"id": 31, "app": GAME_APP, "kind": "game", "status": "asking", "asking": True, "consent": None,
               "detected_at": _iso(40), "ended_at": _iso(1)}
    try:
        ctl.offer(_status(asking, [asking], _mic("asking", None), [_track(asking, "asking")], recording=False,
                          pending=pending))
        assert ctl.card_up and ctl.card.call["id"] == 1          # the live call's card first
        ctl.offer(_status(None, [], _mic("off", None), [], recording=False, pending=pending))
        assert ctl.card_up and ctl.card.call["id"] == 31         # answered elsewhere: the game's card takes its place
        assert ctl.card.question.text() == f"Remember {GAME_APP} voice chat?"
    finally:
        ctl.stop()


# -- the island and the tray: what is kept, from call_status fixtures --------------------------------------------------


def test_the_island_says_what_is_kept_and_again_when_a_game_joins(qapp, monkeypatch):
    import yuki.ui.island as island_mod
    from yuki.ui.call_words import kept
    from yuki.ui.island import Island, on_call_status

    monkeypatch.setattr(island_mod, "CALL_NOTICE_MS", 200)
    isl = Island(bindings={})
    isl.screen_state = lambda: (5, "accepts")
    try:
        on_call_status(isl, call_only())
        spec = isl.window.spec
        assert isl.state == "call-notice" and spec.word == f"Recording · {CALL_APP} + you" and spec.rec
        _pump(qapp, 0.5)
        assert isl.state == "quiet-in-call"
        on_call_status(isl, call_only())                     # the same session: no second notice
        assert isl.state == "quiet-in-call"
        on_call_status(isl, call_and_game())                 # a game joins the call: said again, both
        spec = isl.window.spec
        both = kept(call_and_game())
        assert isl.state == "call-notice" and spec.word in (both["short_line"], both["short"])
        assert spec.title == f"Recording · {CALL_APP} + {GAME_APP} voice + you"   # the whole line on hover
        _pump(qapp, 0.5)
        on_call_status(isl, mic_waits_for_headphones())      # its own session: what is kept, the mic waiting
        assert isl.state == "call-notice" and isl.window.spec.rec
        assert isl.window.spec.title == f"Recording · {CALL_APP} (your mic waits for headphones)"
        _pump(qapp, 0.5)
        on_call_status(isl, {"call": None, "recording": False})
        assert isl.inputs.call is None and isl.state != "quiet-in-call"
    finally:
        monkeypatch.undo()
        isl.stop()


def test_the_tray_card_says_what_is_kept(qapp):
    from yuki.ui.island import Island, on_call_status
    from yuki.ui.tray import TrayWatch

    isl = Island(bindings={})
    isl.screen_state = lambda: (5, "accepts")
    tray = SimpleNamespace(tip="", setIcon=lambda i: None, setToolTip=lambda t: setattr(tray, "tip", t))
    try:
        watch = TrayWatch(tray, isl, waiting=lambda: 0)
        on_call_status(isl, call_and_game())
        watch.refresh()
        info = watch.card_info()
        assert watch.state == "call" and watch.badge == "rec"
        assert info.line.startswith(f"On {CALL_APP} · 12:") and info.timer and info.timer in info.line
        assert info.sub == f"Recording · {CALL_APP} + {GAME_APP} voice + you"
        assert f"Recording · {CALL_APP} + {GAME_APP} voice + you" in tray.tip
        on_call_status(isl, game_only())
        info = watch.card_info()
        assert info.line.startswith(f"{GAME_APP} voice chat · ") and info.sub == f"Recording · {GAME_APP} voice + you"
        on_call_status(isl, mic_waits_for_headphones())
        assert watch.card_info().sub == f"Recording · {CALL_APP} (your mic waits for headphones)"
        assert watch.state == "call" and watch.badge == "rec"   # the call's own sound is kept: the rec dot stays
        on_call_status(isl, game_only())
        isl.screen_state = lambda: (3, "d3d_full_screen")       # in the game: the tray carries it
        isl.on_foreground(0)
        info = watch.card_info()
        assert watch.state == "game" and info.sub.startswith(f"Recording · {GAME_APP} voice + you · ")
        for text in (info.line, info.sub, tray.tip):
            assert "your side" not in text.lower() and "only your mic" not in text.lower()
    finally:
        isl.stop()


# -- Stop stays reachable whenever anything is kept --------------------------------------------------------------------


class _Voice:
    def __init__(self) -> None:
        self.wake_enabled, self.call_active, self.call_off = True, False, False

    @property
    def listening_state(self) -> str:
        return "call_off" if self.call_off else "on"

    def set_wake(self, on: bool) -> None:
        self.wake_enabled = bool(on)

    def set_call_off(self, off: bool) -> None:
        self.call_off = bool(off)


def test_stop_is_reachable_while_only_an_app_track_records(qapp):
    from yuki.ui import web_data as W
    from yuki.ui.call_words import call_rec, live_view
    from yuki.ui.island import Island, on_call_status, spec_for
    from yuki.ui.tray import build_menu

    # 1. no headphones: the mic waits, the call's own sound records (the row says waiting_headphones)
    # 2. a new call's card is up (the call is not consented), the game's voice chat records, the mic is paused for it
    asking = dict(CALL, status="asking", asking=True, consent=None)
    card_up = _status(asking, [asking, GAME], _mic("paused", 2, "call_not_consented"),
                      [_track(asking, "asking"), _track(GAME)])
    for status in (mic_waits_for_headphones(), card_up):
        assert status["mic"]["state"] != "recording" and status["recording"] is True
        memory = _Memory()
        ctl, _events = _controller(memory)
        isl = Island(bindings={})
        isl.screen_state = lambda: (5, "accepts")
        try:
            ctl.offer(status)
            on_call_status(isl, status)
            assert ctl.recording                                       # the tray menu's Stop follows this
            assert spec_for("listening", isl.inputs).rec               # the island's rec dot
            voice = _Voice()
            ui = SimpleNamespace(voice=voice, calls=ctl, window_host=SimpleNamespace(open_window=lambda page=None: None))
            menu = build_menu(ui, SimpleNamespace(quit=lambda: None))
            pause = next(a for a in menu.actions() if a.text().split("\t")[0] == "Pause listening for this call")
            menu.aboutToShow.emit()
            assert pause.isVisible() and pause.isEnabled()
            pause.trigger()
            ctl.wait()
            assert ("stop_call",) in memory.calls                      # the notes stop: every track, every row
            # the window: Home's Running row (its Stop remembering) and the Qt Status page's Stop
            shell = W.call_view(status)
            _now, running = W.home_call(shell, None)
            row = next(r for r in running if r["kind"] == "call")
            assert row["recording"] is True and row["title"].startswith("Remembering ")
            assert shell["recording"] is True and call_rec(live_view(status))
        finally:
            ctl.stop()
            isl.stop()
    # the row the indicators follow while the call's card is up: the game being recorded, not the asked call
    view = live_view(card_up)
    assert view["id"] == GAME["id"] and view["line"] == f"Recording · {GAME_APP} voice"


# -- the windows ---------------------------------------------------------------------------------------------------------


def test_the_window_follows_what_is_kept():
    from yuki.ui import web_data as W
    from yuki.ui.window_host import call_signature

    # the tray forwards a status when what the window shows moved: a game joining moves it, speech seconds don't
    a, b = call_only(), call_and_game()
    assert call_signature(a) != call_signature(b)
    grown = call_only()
    grown["tracks"][0]["speech_s"], grown["mic"]["speech_s"] = 99.0, 120.0
    assert call_signature(a) == call_signature(grown)
    game = W.call_view(game_only())
    assert game["label"] == f"Game voice · {GAME_APP}" and game["where"] == f"{GAME_APP} voice chat"
    assert game["status_words"] == f"Recording · {GAME_APP} voice + you"
    assert game["title"] == f"Remembering {GAME_APP} voice chat"
    both = W.call_view(call_and_game())
    assert both["title"] == f"Remembering the {CALL_APP} call and {GAME_APP} voice chat"
    now_call, running = W.home_call(both, {"calls": 0})
    assert now_call["note"].startswith(f"Your mic, {CALL_APP}’s sound and {GAME_APP}’s voice chat.")
    assert running[0]["detail"].startswith(f"Your mic, {CALL_APP}’s sound and {GAME_APP}’s voice chat.")
    rows = W.call_rows([{"recorded": True, "live": True, "app": GAME_APP, "kind": "game", "consent": "always",
                         "start_at": 1.8e9, "min": 12},
                        {"recorded": True, "live": False, "app": CALL_APP, "kind": "call", "consent": "yes",
                         "start_at": 1.8e9 - 3600, "min": 40, "gists": ["x"]}], now=1.8e9 + 60)
    assert rows[0]["what"] == f"Recording {GAME_APP} voice chat" and rows[0]["why"] == f"rule: always for {GAME_APP} voice chat"
    assert rows[1]["what"] == f"Recorded a 40m {CALL_APP} call" and rows[1]["why"] == "you said yes on the call’s card"
    # Settings says what a recorded call keeps from the privacy file's [calls] knobs (read only)
    assert W.call_knobs({}) == {"record_others": True, "game_voice": True}
    assert W.call_knobs({"calls": {"record_others": False}}) == {"record_others": False, "game_voice": True}
    assert W.call_knobs({"calls": {"game_voice": False}}) == {"record_others": True, "game_voice": False}
