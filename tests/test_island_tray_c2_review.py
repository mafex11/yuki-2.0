"""The island and the tray after appC2's review (offscreen: nothing is shown on screen, no tray icon is added, no
window of the live desktop is touched: every Win32 call that would change a window is replaced by a recorder).

The island and its halo go back on top when they come on screen, not only on a foreground change; full screen is
checked again when the window in front changes its own size; the resting pill's "covered by" is not a hit-test that
skips it; a hover never outlives the resting pill; the menu's Pause skips the call's notes and reads its own flag;
the off card uses the mock's words; the hover card never reaches over the tray icon. All data is invented."""

from __future__ import annotations

import os
import time
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


def _island(log=None):
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


@pytest.fixture
def fake_windows(monkeypatch):
    """Pretend to be on Windows with every window-changing Win32 call recorded instead of made (an offscreen window's
    id is no real window: it must never reach SetWindowPos / SetWindowLong)."""
    import yuki.ui.focus as focus
    import yuki.ui.island as island_mod

    rec = SimpleNamespace(topmost=[], no_activate=[], click_through=[])
    monkeypatch.setattr(island_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(island_mod, "window_facts", lambda h: island_mod.WindowFacts())
    monkeypatch.setattr(island_mod, "monitor_device", lambda h: "")
    monkeypatch.setattr(island_mod, "window_title", lambda h: "")
    monkeypatch.setattr(island_mod.Island, "_covered_by", lambda self: "")
    monkeypatch.setattr(focus, "assert_topmost", lambda h: (rec.topmost.append(h) or (True, True)))
    monkeypatch.setattr(focus, "is_topmost", lambda h: True)
    monkeypatch.setattr(focus, "set_no_activate", lambda h, on: rec.no_activate.append((h, on)) or True)
    monkeypatch.setattr(focus, "set_click_through", lambda h, on: rec.click_through.append((h, on)) or True)
    yield rec
    monkeypatch.undo()


# -- C2-1: on top when it comes on screen ---------------------------------------------------------------------------


def test_the_island_and_its_halo_go_on_top_when_they_come_on_screen(qapp, fake_windows):
    """Hidden, the island is skipped on a foreground change; shown later (the wake word, a run, a hover opening the
    halo) with no foreground change, it must still be put back on top."""
    isl = _island()
    try:
        isl.set_resting(False)
        isl.refresh()
        assert not isl.window.isVisible()
        win, halo = int(isl.window.winId()), int(isl.window.halo_win.winId())
        fake_windows.topmost.clear()
        isl.update(voice="hearing")                           # the pill comes on screen: no foreground change
        assert isl.window.isVisible() and win in fake_windows.topmost
        fake_windows.topmost.clear()
        isl.pointer(True, now=True)                           # the hover opens the bar and brings the halo
        _pump(qapp, 0.05)
        assert isl.window.halo_win.isVisible()
        assert fake_windows.topmost == [halo, win]            # the halo first, so the island sits above it
        fake_windows.topmost.clear()
        isl.update(caption=[("rename", 0.9)])                 # already shown: nothing to re-assert
        assert fake_windows.topmost == []
    finally:
        isl.stop()


# -- C2-2: full screen entered inside one window ----------------------------------------------------------------


def test_only_the_window_in_front_itself_changing_size_counts():
    from yuki.ui.island import reshape_matters

    assert reshape_matters(42, 0, 0, 42)                      # the window itself (OBJID_WINDOW, no child)
    assert not reshape_matters(42, -8, 0, 42)                 # its caret (OBJID_CARET)
    assert not reshape_matters(0, -9, 0, 42)                  # the cursor (OBJID_CURSOR, no window)
    assert not reshape_matters(42, 0, 3, 42)                  # one of its parts
    assert not reshape_matters(77, 0, 0, 42)                  # another window of the same thread
    assert not reshape_matters(42, 0, 0, 0)                   # nothing watched


def test_going_full_screen_inside_the_same_window_hides_and_leaving_shows_again(qapp, monkeypatch):
    """F on a browser video, F11, Alt+Enter: no foreground change, only the window's size. The watch says so once
    (coalesced) and the island checks again; leaving full screen brings it back without switching windows."""
    import yuki.ui.island as island_mod

    isl = _island()
    facts = {"f": island_mod.WindowFacts(covers=False)}
    monkeypatch.setattr(island_mod, "window_facts", lambda h: facts["f"])
    monkeypatch.setattr(island_mod, "RESHAPE_SETTLE_MS", 30)
    try:
        isl.update(voice="listening")
        isl.watch.follow(42)                                  # the window in front (no hook offscreen)
        isl.on_foreground(42)
        assert isl.state == "listening"
        facts["f"] = island_mod.WindowFacts(covers=True, zoomed=False, caption=False)
        isl.screen_state = lambda: (2, "busy")
        seen = []
        isl.watch.reshaped.connect(seen.append)
        for _ in range(5):                                    # a burst of size changes ...
            isl.watch._settle.start()
        _pump(qapp, 0.4)
        assert seen == [42]                                   # ... is one check
        assert isl.state == "game-quiet" and isl.inputs.fullscreen_kind == "window"
        facts["f"] = island_mod.WindowFacts(covers=False)
        isl.screen_state = lambda: (5, "accepts")
        isl.watch.reshaped.emit(42)                           # Esc: the video leaves full screen
        assert isl.state == "listening"
        facts["f"] = island_mod.WindowFacts(covers=True, zoomed=False, caption=False)
        isl.screen_state = lambda: (2, "busy")
        isl.watch.reshaped.emit(77)                           # a window no longer in front: ignored
        assert isl.state == "listening"
    finally:
        monkeypatch.undo()
        isl.stop()


def test_a_foreground_change_follows_the_new_window_for_size_changes(qapp):
    from yuki.ui.island import ForegroundWatch

    w = ForegroundWatch()
    w.follow(42)
    assert w.watched == 42 and w._rehook.isActive()           # hooked on the next loop turn, not in the callback
    w.follow(77)
    assert w.watched == 77
    _pump(qapp, 0.05)
    w.stop()
    assert w.watched == 0 and not w._settle.isActive()


# -- C2-5 / C2-6: the resting pill ----------------------------------------------------------------------------------


def test_the_resting_pill_is_never_reported_as_covered(qapp, monkeypatch):
    import yuki.ui.focus as focus
    import yuki.ui.island as island_mod

    isl = _island()
    try:
        isl.refresh()                                         # idle: the click-through resting pill
        _pump(qapp, 0.5)
        assert isl.window.spec.resting and isl.window._click_through and isl.window.isVisible()
        monkeypatch.setattr(island_mod, "_is_windows", lambda: True)
        monkeypatch.setattr(focus, "is_topmost", lambda h: True)
        assert isl._covered_by() == "n/a"                     # a hit-test skips it: it would name the app beneath
    finally:
        monkeypatch.undo()
        isl.stop()


def test_a_hover_never_outlives_the_resting_pill(qapp):
    """Hover the Listening pill, take the hand off the mouse, Yuki goes idle: the next Listening is the pill again."""
    isl = _island()
    try:
        isl.update(voice="listening")
        isl.pointer(True, now=True)
        assert isl.hovered and not isl.window.spec.compact
        isl.update(voice="off")
        assert isl.window.spec.resting and not isl.hovered
        isl.update(voice="listening")
        assert isl.window.spec.compact and not isl.hovered
        isl.pointer(True)                                     # a hover starting ...
        isl.update(voice="off")                               # ... as the island rests: its timer is dropped
        assert not isl._hover_in.isActive()
        _pump(qapp, 0.3)
        assert not isl.hovered
    finally:
        isl.stop()


# -- C2-3 / C2-9: the menu's Pause, the off card ---------------------------------------------------------------------


class _Voice:
    def __init__(self, *, wake: bool = True, call: bool = False) -> None:
        self.wake_enabled, self.call_active, self.call_off = wake, call, False

    @property
    def listening_state(self) -> str:                         # as the voice session: "off" before "call_off"
        if not self.wake_enabled:
            return "off"
        return "call_off" if self.call_off else "on"

    def set_wake(self, on: bool) -> None:
        self.wake_enabled = bool(on)

    def set_call_off(self, off: bool) -> None:
        self.call_off = bool(off) and self.call_active


def _menu(voice, calls):
    from yuki.ui.tray import build_menu

    ui = SimpleNamespace(voice=voice, calls=calls, window_host=SimpleNamespace(open_window=lambda page=None: None))
    menu = build_menu(ui, SimpleNamespace(quit=lambda: None))
    pause = next(a for a in menu.actions() if a.text().split("\t")[0] in ("Pause listening for this call",
                                                                         "Listen again in this call"))
    return menu, pause


def test_pause_for_this_call_skips_the_calls_notes_even_with_listening_off(qapp):
    stopped = []
    calls = SimpleNamespace(recording=True, stop_call=lambda: stopped.append(True))
    voice = _Voice(wake=False, call=True)
    menu, pause = _menu(voice, calls)
    menu.aboutToShow.emit()
    assert pause.isVisible() and pause.isEnabled()
    pause.trigger()
    assert stopped == [True] and voice.call_off                # the notes stop, the wake word stays off for the call
    calls.recording = False
    menu.aboutToShow.emit()
    assert pause.text().split("\t")[0] == "Listen again in this call"   # read from call_off, not listening_state
    pause.trigger()
    assert not voice.call_off and stopped == [True]           # back on: no second stop


def test_pause_is_greyed_out_only_during_a_call_while_listening_is_off(qapp):
    calls = SimpleNamespace(recording=False, stop_call=lambda: None)
    voice = _Voice(wake=False, call=False)
    menu, pause = _menu(voice, calls)
    menu.aboutToShow.emit()
    assert pause.isVisible() and not pause.isEnabled()        # the off card's words point at something real
    assert pause.text().endswith("\tonly during a call")
    voice.wake_enabled = True
    menu.aboutToShow.emit()
    assert not pause.isVisible()                              # listening on, no call: not offered
    calls.recording = True                                    # call notes recording (the voice may not know yet)
    menu.aboutToShow.emit()
    assert pause.isVisible() and pause.isEnabled() and "\t" not in pause.text()


def test_the_off_card_uses_the_mocks_words(qapp):
    from PySide6.QtCore import QObject, QRect, Signal

    from yuki.ui.tray import TrayWatch

    class FakeTray(QObject):
        popup_open = Signal()
        popup_close = Signal()
        theme_changed = Signal()
        activated = Signal(object)

        def set_face(self, *a, **k):
            pass

        def setToolTip(self, t):  # noqa: N802
            pass

        def geometry(self):
            return QRect(700, 560, 24, 24)

    isl = _island()
    voice = _Voice(wake=False)
    try:
        watch = TrayWatch(FakeTray(), isl, voice=voice, open_window=lambda: None)
        isl.update(listening="off")
        info = watch.card_info()
        assert info.line == "Not listening for “Yuki”" and not info.listen
        voice.call_active = True
        voice.set_call_off(True)
        assert watch.card_info().sub == "Paused for this call"
    finally:
        isl.stop()


# -- C2-4: the hover card never reaches over the tray icon ------------------------------------------------------------


def test_the_hover_card_grows_beside_the_icon_never_over_it(qapp):
    from PySide6.QtCore import QRect, QRectF
    from PySide6.QtGui import QGuiApplication

    from yuki.ui.lattice_cards import TrayHoverCard
    from yuki.ui.tray import TrayCardInfo

    area = QGuiApplication.primaryScreen().geometry()
    card = TrayHoverCard()
    try:
        card.set_info(TrayCardInfo(state="resting", line="Resting", sub="Say “Yuki”", last="Nothing yet"))
        for icon in (QRect(area.right() - 60, area.bottom() - 36, 24, 24),     # a taskbar at the bottom
                     QRect(area.right() - 60, area.top() + 12, 24, 24)):        # a taskbar at the top
            card.open_from(icon)
            assert not card.geometry().intersects(icon)       # the pointer on the icon stays on the icon
            start, _r = card._clip()                          # the clip morph's first box
            assert QRectF(card.rect()).contains(start)
            assert abs(start.height() - icon.height()) < 1e-6
            card._anim.stop()
            card._anim_done()
            card._anim_done_fold()
    finally:
        card.close()
