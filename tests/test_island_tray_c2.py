"""The island and the tray after the 3 Oct feedback (builder appC2), offscreen (QT_QPA_PLATFORM=offscreen: nothing is
shown on screen, no tray icon is added, no focus is taken): a shown island always ends at full strength whatever
sequence of shows, leaves, morphs and hovers ran; it hides only for a real full screen; it is a compact pill until
the pointer rests on it; a call leaves only a short notice; the topmost band is re-asserted on every foreground
change; its logging carries no content; the tray's faces follow the mock's grids; the hover card and the menu.
All data is invented."""

from __future__ import annotations

import os
import random
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from yuki.ui import lattice as L

    L.REDUCED_OVERRIDE[0] = False
    yield app
    L.REDUCED_OVERRIDE[0] = None


def _call(minutes: float, status: str = "recording", session: str = "") -> dict:
    start = datetime.now().astimezone() - timedelta(minutes=minutes)
    return {"status": status, "app": "Discord", "id": 7,
            "session_started_at": session or start.isoformat(timespec="seconds")}


def _island(qapp, log=None):
    from yuki.ui.island import Island

    isl = Island(bindings={"voice": "ctrl+alt+y", "cancel": "alt+shift+x"})
    isl.screen_state = lambda: (5, "accepts")
    isl.log = log
    return isl


def _pump(qapp, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)


# -- 1. the island is always drawn at full strength once shown --------------------------------------------------


def test_a_shown_island_ends_at_full_strength_after_any_sequence(qapp):
    """3 Oct: a bar was painted but invisible while its halo showed (a layered window left at opacity 0). Whatever
    runs (enters cut by leaves, leaves cut by enters, morphs, hovers, the run card), a shown island settles with the
    window at opacity 1 and the panel at the design's opacity, and the halo at full strength."""
    from yuki.ui.island import FinalInfo, RunInfo

    rng = random.Random(20261003)
    isl = _island(qapp)
    run = RunInfo(3, time.monotonic(), steps=2, line="Renaming the exports", request="rename the exports")
    moves = [
        lambda: isl.update(voice="listening"),
        lambda: isl.update(voice="hearing", caption=[("rename", 0.9), ("them", 0.3)]),
        lambda: isl.update(voice="speaking", spoken="Renaming them now"),
        lambda: isl.update(voice="off", run=run),
        lambda: isl.update(run=None, final=FinalInfo("Renamed 12 exports")),
        lambda: isl.update(final=None, question="Which folder?", question_id=3),
        lambda: isl.update(question="", question_id=None),
        lambda: isl.update(voice_error="The microphone was unplugged."),
        lambda: isl.update(voice_error=""),
        lambda: isl.update(fullscreen=True, fullscreen_kind="d3d"),
        lambda: isl.update(fullscreen=False, fullscreen_kind=""),
        lambda: isl.update(call=_call(4)),
        lambda: isl.update(call=None, listening="off"),
        lambda: isl.update(listening="on"),
        lambda: isl.pointer(True, now=True),
        lambda: isl.pointer(False),
        lambda: isl.toggle_card(),
        lambda: isl.window.leave(animate=True),
        lambda: isl.set_resting(not isl.resting),
    ]
    try:
        for _round in range(6):
            for _ in range(14):
                rng.choice(moves)()
                _pump(qapp, rng.choice((0.0, 0.01, 0.05, 0.12, 0.2)))
            isl.refresh()
            _pump(qapp, 0.8)                                  # past every enter, leave, morph and fade
            w = isl.window
            if w.isVisible():
                assert w.windowOpacity() == 1.0
                assert w.shown_strength() == 1.0
                assert abs(w.panel_opacity() - w.design_opacity()) < 1e-6
                assert not w._leaving
                if w.halo_win.isVisible():
                    assert w.halo_win.strength() == 1.0
            assert w.windowOpacity() == 1.0                   # never anything else, shown or not
    finally:
        isl.stop()


def test_an_enter_cut_short_is_settled_once_after_it_should_have_ended(qapp):
    from yuki.ui.island import SETTLE_CHECK_MS

    events = []
    isl = _island(qapp, log=lambda e, **f: events.append((e, f)))
    try:
        isl.update(voice="listening")                         # an animated enter starts at strength 0 ...
        assert isl.window._fade < 1.0
        isl.window._move.stop()                               # ... and its animation never finishes
        _pump(qapp, SETTLE_CHECK_MS / 1000 + 0.15)
        assert isl.window.isVisible() and isl.window.shown_strength() == 1.0
        assert any(e == "island_settled" for e, _f in events)
    finally:
        isl.stop()


# -- 2. always on top ---------------------------------------------------------------------------------------------


def test_the_topmost_band_is_reasserted_on_every_foreground_change(qapp, monkeypatch):
    import yuki.ui.focus as focus
    import yuki.ui.island as island_mod

    events, calls = [], []
    isl = _island(qapp, log=lambda e, **f: events.append((e, f)))
    try:
        isl.update(voice="hearing")
        isl.pointer(True, now=True)                           # open: the halo is up too
        _pump(qapp, 0.5)
        assert isl.window.isVisible() and isl.window.halo_win.isVisible()
        monkeypatch.setattr(island_mod, "_is_windows", lambda: True)
        monkeypatch.setattr(island_mod, "window_facts", lambda h: island_mod.WindowFacts())
        monkeypatch.setattr(island_mod, "monitor_device", lambda h: "")
        monkeypatch.setattr(island_mod, "window_title", lambda h: "")
        monkeypatch.setattr(focus, "assert_topmost", lambda h: (calls.append(h) or (len(calls) > 2, True)))
        isl.on_foreground(1234)
        isl.on_foreground(5678)
        halo, win = int(isl.window.halo_win.winId()), int(isl.window.winId())
        assert calls == [halo, win, halo, win]                # the halo first, so the island sits above it
        lost = [f for e, f in events if e == "island_topmost"]
        assert lost and lost[0]["was_topmost"] is False       # a lost band is logged
    finally:
        monkeypatch.undo()
        isl.stop()


# -- 3. hide only for a real full screen ----------------------------------------------------------------------------


def test_only_a_real_full_screen_counts():
    from yuki.ui.island import WindowFacts, fullscreen_kind

    borderless_max = WindowFacts(covers=True, zoomed=True, caption=False)    # Arc maximised on its monitor
    borderless_full = WindowFacts(covers=True, zoomed=False, caption=False)  # a borderless game, an F11 browser
    framed_big = WindowFacts(covers=True, zoomed=False, caption=True)
    small = WindowFacts(covers=False)
    assert fullscreen_kind(2, borderless_max) == ""
    assert fullscreen_kind(2, borderless_full) == "window"
    assert fullscreen_kind(2, framed_big) == "" and fullscreen_kind(2, small) == ""
    assert fullscreen_kind(3, small) == "d3d"                 # an exclusive full-screen game
    assert fullscreen_kind(4, small) == "presentation"
    assert fullscreen_kind(1, small) == "locked"
    assert fullscreen_kind(5, borderless_full) == "" and fullscreen_kind(0, WindowFacts()) == ""


def test_the_island_stays_over_a_maximised_borderless_app_and_leaves_for_a_full_screen(qapp, monkeypatch):
    import yuki.ui.island as island_mod

    isl = _island(qapp)
    facts = {"f": island_mod.WindowFacts(covers=True, zoomed=True)}
    monkeypatch.setattr(island_mod, "window_facts", lambda h: facts["f"])
    try:
        isl.screen_state = lambda: (2, "busy")
        isl.update(voice="listening")
        isl.on_foreground(42)
        assert isl.state == "listening"                       # maximised Arc: still there
        facts["f"] = island_mod.WindowFacts(covers=True, zoomed=False)
        isl.on_foreground(42)
        assert isl.state == "game-quiet" and isl.inputs.fullscreen_kind == "window"
        assert isl.current_spec().why_hidden == "full screen: window"
    finally:
        monkeypatch.undo()
        isl.stop()


# -- 4. compact by default, open on hover -----------------------------------------------------------------------


def test_every_state_is_a_compact_pill_until_the_pointer_rests_on_it(qapp):
    from yuki.ui.island import COMPACT_H, HOVER_IN_MS, HOVER_OUT_MS, RunInfo

    isl = _island(qapp)
    try:
        isl.update(run=RunInfo(4, time.monotonic(), steps=2, line="Renaming the exports"))
        spec = isl.window.spec
        assert spec.compact and spec.word == "Working · step 2" and spec.halo is None
        assert isl.window.panel_height(spec, 300) == COMPACT_H + 2 and isl.window.face.pitch == 4
        isl.pointer(True)
        _pump(qapp, HOVER_IN_MS / 1000 * 0.4)
        assert isl.window.spec.compact                        # a pointer passing by doesn't open it
        _pump(qapp, HOVER_IN_MS / 1000 + 0.05)
        full = isl.window.spec
        assert not full.compact and full.keys == ("stop", "chev") and full.halo == "working"
        assert isl.window.face.pitch == 8
        isl.pointer(False)
        _pump(qapp, HOVER_OUT_MS / 1000 * 0.5)
        assert not isl.window.spec.compact                    # it waits 300 ms
        _pump(qapp, HOVER_OUT_MS / 1000 * 0.5 + 0.1)
        assert isl.window.spec.compact
        isl.pointer(True, now=True)                           # a click opens at once
        isl.toggle_card()                                     # the run card opens from the open island ...
        isl.pointer(False)
        _pump(qapp, HOVER_OUT_MS / 1000 + 0.1)
        assert isl.window.spec.body == "run"                  # ... and stays until it is closed
        isl._close_card()
        isl.hovered = False
        isl.refresh()
        assert isl.window.spec.compact
        words = {}
        for name, change in (("Listening", {"run": None, "voice": "listening"}), ("Hearing you", {"voice": "hearing"}),
                             ("Speaking", {"voice": "speaking"}), ("Thinking", {"voice": "thinking"})):
            isl.update(**change)
            words[name] = isl.window.spec.word
        assert words == {k: k for k in words}
    finally:
        isl.stop()


def test_only_a_question_or_a_broken_voice_opens_by_itself(qapp):
    isl = _island(qapp)
    try:
        isl.update(question="Which folder did you mean?", question_id=2)
        assert not isl.window.spec.compact and isl.window.spec.body == "choices"
        isl.update(question="", question_id=None, voice_error="The microphone was unplugged.")
        assert not isl.window.spec.compact and isl.window.spec.keys == ("retry", "settings")
    finally:
        isl.stop()


def test_idle_is_the_click_through_resting_pill_or_nothing(qapp):
    isl = _island(qapp)
    try:
        isl.refresh()
        spec = isl.window.spec
        assert spec.resting and isl.window._click_through and isl.window.face.pitch == 3
        isl.pointer(True, now=True)
        assert not isl.hovered                                # the resting pill never opens
        isl.set_resting(False)
        _pump(qapp, 0.5)
        assert isl.window.spec.hidden and not isl.window.isVisible()
        isl.set_resting(True)
        isl.update(listening="off")                           # switched off: nothing on screen
        _pump(qapp, 0.5)
        assert isl.state == "not-listening" and not isl.window.isVisible()
    finally:
        isl.stop()


# -- 5. calls ----------------------------------------------------------------------------------------------------


def test_a_call_leaves_only_a_short_notice_then_nothing(qapp, monkeypatch):
    import yuki.ui.island as island_mod

    monkeypatch.setattr(island_mod, "CALL_NOTICE_MS", 200)
    isl = _island(qapp)
    try:
        call = _call(1)
        isl.update(call=call)
        spec = isl.window.spec
        assert isl.state == "call-notice" and spec.compact and spec.word == "Recording your mic · Discord"
        assert spec.rec and spec.halo is None
        _pump(qapp, 0.6)
        assert isl.state == "quiet-in-call" and not isl.window.isVisible() and not isl.window.halo_win.isVisible()
        isl.update(call=dict(call, status="recording"))      # the same call session: no second notice
        assert isl.state == "quiet-in-call"
        isl.update(voice="hearing")                           # Yuki hears you in a call: the pill, a still rec dot
        assert isl.window.spec.compact and isl.window.spec.rec
    finally:
        monkeypatch.undo()
        isl.stop()


# -- 1b. logging without content ------------------------------------------------------------------------------------


def test_the_island_log_says_what_it_does_without_any_content(qapp):
    from yuki.ui.island import RunInfo

    events = []
    isl = _island(qapp, log=lambda e, **f: events.append((e, f)))
    secret = "message Priyanka the launch passcode"
    try:
        isl.update(run=RunInfo(9, time.monotonic(), steps=1, line=secret, request=secret))
        isl.update(question=secret, question_id=9)
        isl.update(question="", question_id=None, run=None, voice="speaking", spoken=secret)
        _pump(qapp, 0.5)
        names = {e for e, _f in events}
        assert {"island", "island_shown"} <= names
        snap = next(f for e, f in events if e == "island")
        for key in ("state", "shown", "visible", "strength", "window_opacity", "rect", "monitor", "fullscreen",
                    "topmost", "why_hidden"):
            assert key in snap
        assert all(secret not in repr(f) and "Priyanka" not in repr(f) for _e, f in events)
    finally:
        isl.stop()


# -- 6. the tray -------------------------------------------------------------------------------------------------


def _lit(img, x, y) -> int:
    return img.pixelColor(x, y).alpha()


def test_the_tray_faces_follow_the_mocks_grids(qapp):
    from yuki.ui.tray import tray_face_image, tray_frames

    r16 = tray_face_image("resting", 16)
    assert r16.width() == 16 and _lit(r16, 1, 4) > 200 and _lit(r16, 2, 5) > 200   # eye dot at (1,4), 2 px
    assert _lit(r16, 3, 4) == 0                                                       # 1 px gap, whole pixels
    assert _lit(r16, 7, 0) == 0                                                       # row 0 empty: open eyes
    r20 = tray_face_image("resting", 20)
    assert _lit(r20, 6, 6) > 200 and _lit(r20, 5, 5) < 255                            # a 3 px round dot
    c24 = tray_face_image("call", 24)
    assert c24.pixelColor(22, 2).green() > 200                                        # the mint rec badge
    off = tray_face_image("off", 16)
    assert _lit(off, 13, 0) > 0 and _lit(off, 14, 1) == 0                             # a hollow ring
    light = tray_face_image("resting", 16, theme="light")
    assert light.pixelColor(1, 4).red() < 60                                          # dark ink on a light bar
    assert tray_frames("hearing") == tray_frames("speaking") == tray_frames("working") == 4
    assert tray_frames("resting") == tray_frames("needs-you") == 1
    frames = {bytes(tray_face_image("speaking", 20, k).constBits()) for k in range(4)}
    assert len(frames) == 3                                  # the mock: frames 1 and 3 are the same half-open mouth


def test_the_hover_card_grows_on_windows_popup_and_folds_after_the_pointer(qapp):
    from PySide6.QtCore import QObject, QRect, Signal

    from yuki.ui.island import RunInfo
    from yuki.ui.tray import TrayWatch

    class FakeTray(QObject):
        popup_open = Signal()
        popup_close = Signal()
        theme_changed = Signal()
        activated = Signal(object)

        def __init__(self):
            super().__init__()
            self.faces, self.tip = [], ""

        def set_face(self, state, frame=0, badge=""):
            self.faces.append((state, frame, badge))

        def setToolTip(self, t):  # noqa: N802
            self.tip = t

        def geometry(self):
            return QRect(1800, 1040, 24, 24)

    isl = _island(qapp)
    voice = SimpleNamespace(wake_enabled=True, set_wake=lambda on: setattr(voice, "wake_enabled", on))
    tray = FakeTray()
    opened = []
    try:
        watch = TrayWatch(tray, isl, voice=voice, open_window=lambda: opened.append(True))
        isl.update(run=RunInfo(4, time.monotonic(), steps=2, line="Renaming the exports",
                               request="rename the exports"))
        assert tray.faces[-1][0] == "working" and watch._frames.isActive()    # 4 frames swap while working
        tray.popup_open.emit()
        card = watch.card
        assert card is not None and card.isVisible()
        assert card.info.line == "Working on: rename the exports" and card.info.sub == "step 2"
        assert card.k_stop.isVisibleTo(card.content) and card.info.task == "Renaming the exports"
        tray.popup_close.emit()
        _pump(qapp, 0.15)
        assert card.isVisible() and not card.closing          # 300 ms grace for the pointer to reach the card
        _pump(qapp, 0.7)
        assert not card.isVisible()
        isl.update(run=None)
        tray.popup_open.emit()
        _pump(qapp, 0.5)
        card.tog.toggled.emit(False)                          # the Listen for "Yuki" switch
        assert voice.wake_enabled is False and card.off_text.isVisibleTo(card.content)
        assert "Call notes still record your mic" in card.off_text.text()
        card.k_open.click()
        assert opened
    finally:
        isl.stop()


def test_the_native_tray_reads_windows_hover_and_click_messages(qapp):
    from PySide6.QtWidgets import QSystemTrayIcon

    from yuki.ui import tray as T

    nt = T.NativeTray()                                       # never shown: no icon is added to the taskbar
    got = []
    nt.popup_open.connect(lambda: got.append("open"))
    nt.popup_close.connect(lambda: got.append("close"))
    nt.activated.connect(lambda r: got.append(r))
    assert nt._on_message(T.TRAY_CALLBACK, 0, T.NIN_POPUPOPEN | (1 << 16))
    assert nt._on_message(T.TRAY_CALLBACK, 0, T.NIN_POPUPCLOSE | (1 << 16))
    assert nt._on_message(T.TRAY_CALLBACK, 0, T.NIN_SELECT | (1 << 16))
    assert got == ["open", "close", QSystemTrayIcon.ActivationReason.Trigger]
    assert not nt.isVisible()


def test_the_menu_has_the_mocks_items_and_pause_only_in_a_call(qapp):
    from PySide6.QtWidgets import QMenu

    from yuki.ui.tray import build_menu

    calls = []
    voice = SimpleNamespace(wake_enabled=True, listening_state="on", call_active=False,
                            set_wake=lambda on: calls.append(("wake", on)),
                            set_call_off=lambda on: calls.append(("call_off", on)))
    ui = SimpleNamespace(voice=voice, window_host=SimpleNamespace(open_window=lambda page=None: calls.append(page)))
    app = SimpleNamespace(quit=lambda: calls.append("quit"))
    menu = build_menu(ui, app)
    assert isinstance(menu, QMenu)
    words = [a.text().split("\t")[0] for a in menu.actions() if not a.isSeparator()]
    assert words == ["Open Yuki", "Listen for “Yuki”", "Pause listening for this call", "Settings", "Quit Yuki"]
    pause = menu.actions()[2]
    assert not pause.isVisible()
    voice.call_active = True
    menu.aboutToShow.emit()
    assert pause.isVisible()
    menu.actions()[1].trigger()
    pause.trigger()
    assert ("wake", False) in calls and ("call_off", True) in calls


def test_natural_times_for_the_last_thing_done():
    from yuki.ui.tray import natural_when

    now = datetime(2026, 10, 3, 21, 30).timestamp()
    assert natural_when(now - 20, now) == "just now"
    assert natural_when(datetime(2026, 10, 3, 9, 24).timestamp(), now) == "9:24 AM"
    assert natural_when(datetime(2026, 10, 2, 21, 24).timestamp(), now) == "yesterday, 9:24 PM"
    assert natural_when(datetime(2026, 9, 25, 2, 0).timestamp(), now) == "25 September, 2:00 AM"
