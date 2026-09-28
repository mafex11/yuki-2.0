"""The Horizon x Field Unit redesign (2026-09-28): the skyline's data and its picked moment, the waves that
stop when hidden, the accent setting, the bundled fonts, the GUI stall watchdog, the encrypted page snapshot
and the placeholders that keep their size. Offscreen only (rule 7), temporary files only."""

from __future__ import annotations

import os
import time
from datetime import date, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

import pytest  # noqa: E402


def _gui_less_app() -> bool:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    app = QCoreApplication.instance()
    return app is not None and not isinstance(app, QApplication)


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    if _gui_less_app():
        pytest.skip("GUI-less QCoreApplication in this process: run this file on its own")
    app = QApplication.instance() or QApplication([])
    from yuki.ui.window.theme import app_font

    app.setFont(app_font())
    return app


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def _day():
    """A day: Warp 20:00-21:00 (full), Chrome 21:00-21:30, away 21:30-22:00, nothing recorded 02:00-04:00, the
    PC off 05:00-06:00, a Discord call 20:10-23:00 still going."""
    from yuki.ui.window.data import Band, DayView, Lane, Segment, Session

    since = datetime(2026, 9, 26).timestamp()
    h = lambda x: since + x * 3600  # noqa: E731
    lanes = [Lane("warp", "Warp", 3600), Lane("chrome", "Chrome", 1800)]
    segs = [Segment(h(20), h(21), "warp", "present", 3600, host=None),
            Segment(h(21), h(21.5), "chrome", "present", 1800, host="www.youtube.com"),
            Segment(h(21.5), h(22), "away", "away", 0)]
    call = Band("call", h(20) + 600, h(23), "Discord call", "Discord", h(20) + 600, h(23), "", [], 9000.0, True)
    gap = Band("gap", h(2), h(4), "Not recorded")
    off = Band("off", h(5), h(6), "PC off")
    sess = Session(h(20), h(21.5), "warp", "Coding the skyline", 5400, [("Warp", 3600), ("Chrome", 1800)],
                   ["20:00-21:30: coding"], [{"importance": 5, "by_yuki": False, "at": h(20.5), "nature": "fact",
                                              "text": "The user fixed the call band."}])
    totals = {"present_s": 5400, "away_s": 1800, "switches": 3, "call_s": 10200, "off_s": 3600, "asleep_s": 0,
              "fullscreen_s": 0, "gap_s": 7200}
    return DayView(date(2026, 9, 26), since, since + 86400, since + 86400, lanes, segs, [call], [off], [gap], [sess],
                   totals, "A long evening.", [], [], h(20), h(22))


# -- the skyline's data -----------------------------------------------------------------------------------


def test_skyline_maps_minutes_at_the_pc_into_quarter_hours():
    from yuki.ui.window.skyline import BUCKET_S, BUCKETS, skyline

    day = _day()
    d = skyline(day)
    assert len(d.present) == len(d.away) == len(d.near) == len(d.far) == BUCKETS
    i20, i21, i2130 = 20 * 4, 21 * 4, 21 * 4 + 2
    assert all(abs(d.present[i] - BUCKET_S) < 1e-6 for i in range(i20, i21))          # a full hour at the PC
    assert all(abs(d.present[i] - BUCKET_S) < 1e-6 for i in range(i21, i2130))       # Chrome's half hour
    assert all(d.away[i] == BUCKET_S and d.present[i] == 0 for i in range(i2130, 22 * 4))
    assert sum(d.present) == pytest.approx(5400) and sum(d.away) == pytest.approx(1800)
    assert d.present[10] == 0 and d.near[10] == 0                                       # nothing at 02:30
    # the hill behind carries away time too, the ridge does not
    assert d.far[i2130 + 1] > 0 and d.near[i2130 + 1] < d.far[i2130 + 1]
    # gaps say why, in words; calls are spans with their length
    kinds = {(g.kind, g.label) for g in d.gaps}
    assert ("gap", "nothing recorded") in kinds and ("off", "PC off") in kinds
    assert [(round(c.start_h, 2), round(c.end_h, 2), c.label) for c in d.calls] == [(20.17, 23.0,
                                                                                      "Discord call, 2h 50m")]
    # a peak carries a flag: the app that held it and the session's span
    assert d.peaks and d.peaks[0].label == "Warp" and d.peaks[0].sub == "20:00 to 21:30"


def test_skyline_stops_at_now_on_today():
    from yuki.ui.window.skyline import skyline

    day = _day()
    d = skyline(day, now=day.since + 21 * 3600 + 60)
    assert d.last == 21 * 4


# -- a picked moment ----------------------------------------------------------------------------------------


def test_a_moment_belongs_to_its_session_else_its_hour():
    from yuki.ui.window.skyline import apps_line, call_moment, moment_at

    day = _day()
    m = moment_at(day, day.since + 20.25 * 3600)
    assert m.kind == "session" and m.title == "Coding the skyline" and m.lead == "Warp"
    assert m.noticed == ["Fixed the call band."]           # second person, as the window speaks
    assert apps_line(m) == "Warp 1h 00m, Chrome 30m, Discord call ongoing, from 20:00 to 21:30"
    hour = moment_at(day, day.since + 21.75 * 3600)       # after the session: its hour
    assert hour.kind == "hour" and (hour.start, hour.end) == (day.since + 21 * 3600, day.since + 22 * 3600)
    assert hour.title == "Mostly Chrome, 30 of the 60 minutes"
    nothing = moment_at(day, day.since + 3 * 3600)
    assert nothing.kind == "empty" and nothing.title == "Nothing was recorded then"
    off = moment_at(day, day.since + 5.5 * 3600)
    assert off.title == "The PC was off"
    c = call_moment(day, 0)
    assert c.kind == "call" and c.title.startswith("Discord call, 2h 50m end to end, still going")


def test_hover_and_click_pick_a_moment_and_the_land_is_drawn_once(qapp):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from yuki.ui.window.pages import today
    from yuki.ui.window.theme import theme

    day = _day()
    page = today.build(theme(accent="green"), day)
    page.resize(1200, 900)
    page.show()
    pump(qapp, 0.05)
    chart, panel = page.chart, page.moment_panel
    seen, pinned = [], []
    chart.moment_changed.connect(seen.append)
    chart.moment_clicked.connect(pinned.append)
    heights = {panel.height(), page.sizeHint().height()}
    for hour in (3.0, 20.3, 21.2, 21.8, 12.0):
        x = chart.x_of(day.since + hour * 3600)
        QApplication.sendEvent(chart, QMouseEvent(QEvent.Type.MouseMove, QPointF(x, 200), QPointF(x, 200),
                                                  Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                                  Qt.KeyboardModifier.NoModifier))
        chart.repaint()
        heights.add(panel.height())
        heights.add(page.sizeHint().height())
    assert len(seen) == 5 and abs(seen[1] - (day.since + 20.3 * 3600)) < 120
    assert chart.land_builds == 1                          # the land is cached: hovering repaints the moment only
    assert len(heights) == 2                               # nothing under the chart moved (fixed-size panels)
    x = chart.x_of(day.since + 20.5 * 3600)
    for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
        QApplication.sendEvent(chart, QMouseEvent(kind, QPointF(x, 200), QPointF(x, 200), Qt.MouseButton.LeftButton,
                                                  Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    assert pinned and chart.pinned == pinned[0] and page.at == pinned[0] and page.selected == 0
    stamp, title, apps, _note = panel.lines()
    assert stamp == "20:00" and title == "Coding the skyline" and apps.endswith("from 20:00 to 21:30")
    chart.resize(900, chart.height())                      # a new size draws the land again, once
    chart.repaint()
    assert chart.land_builds == 2
    page.close()


# -- the waves ------------------------------------------------------------------------------------------------


def test_a_wave_animates_only_while_shown_and_holds_still_with_reduce_motion(qapp, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget

    from yuki.ui import ui_settings
    from yuki.ui.wave import TICKER, Wave

    host = QWidget()
    host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    wave = Wave("speak", parent=host)
    other = Wave("listen", parent=host)
    host.show()
    pump(qapp, 0.25)
    assert wave.animating and other.animating and wave.frames > 2
    assert TICKER.running and len([w for w in TICKER.waves if w in (wave, other)]) == 2   # one shared timer
    host.hide()                                             # the window goes away: both waves stop
    pump(qapp, 0.05)
    frames = wave.frames
    pump(qapp, 0.2)
    assert not wave.animating and not other.animating and wave.frames == frames
    assert wave not in TICKER.waves
    monkeypatch.setattr(ui_settings, "current", lambda: ui_settings.UiSettings(reduce_motion=True))
    host.show()
    pump(qapp, 0.2)
    assert not wave.animating and wave.frames == frames     # a still frame, no timer
    host.close()


# -- the accent -----------------------------------------------------------------------------------------------


def test_the_accent_setting_is_saved_and_repaints_the_window(qapp, tmp_path):
    from yuki.memory.api import MemoryClient
    from yuki.ui import ui_settings
    from yuki.ui.window.app import WindowApp
    from yuki.ui.window.theme import ACCENTS, DEFAULT_ACCENT, accent_name, theme

    assert DEFAULT_ACCENT == "green" and ACCENTS == {"green": "#8FAF5A", "orange": "#FF5B14"}
    path = tmp_path / "ui.json"
    assert ui_settings.load(path).accent == "green"                    # the default
    ui_settings.update(path, accent="orange")
    assert ui_settings.load(path).accent == "orange"
    ui_settings.update(path, accent="blue")                             # not a choice any more: back to green
    assert ui_settings.load(path).accent == "green"
    assert accent_name("orange") == "orange" and theme(accent="orange").accent.name() == "#ff5b14"
    memory = MemoryClient.open(tmp_path / "memory.db")
    try:
        ctrl = WindowApp(memory, db_path=tmp_path / "memory.db", connect=False, ui_path=path)
        ctrl.set_accent("orange")
        assert ui_settings.load(path).accent == "orange"
        assert ctrl.t.accent.name() == "#ff5b14" and ctrl.win.sidebar.t.accent.name() == "#ff5b14"
        assert all(k.t.accent.name() == "#ff5b14" for k in ctrl.win.sidebar.items.values())
        ctrl.set_accent("teal")                                          # ignored
        assert ui_settings.load(path).accent == "orange"
        ctrl.win.close()
    finally:
        memory.close()


# -- fonts ---------------------------------------------------------------------------------------------------


def test_doto_is_bundled_and_devanagari_falls_back_to_nirmala(qapp):
    from PySide6.QtGui import QFont, QFontDatabase, QFontInfo

    from yuki.ui import fonts
    from yuki.ui.window.theme import dot_font, variable_font

    assert (fonts.font_dir() / "Doto.ttf").exists() and (fonts.font_dir() / "Doto-OFL.txt").exists()
    assert "SIL Open Font License" in (fonts.font_dir() / "Doto-OFL.txt").read_text(encoding="utf-8")
    assert fonts.load_fonts() and fonts.load_fonts()                      # once; the second call is free
    assert "Doto" in QFontDatabase.families()
    assert QFontInfo(dot_font(44)).family() == "Doto"
    assert "nirmala ui" in [f.lower() for f in QFont.substitutes("Segoe UI Variable")]
    assert "Nirmala UI" in variable_font(14).families()
    spec = (fonts.font_dir().parents[2] / "packaging" / "yuki.spec").read_text(encoding="utf-8")
    assert "ui/fonts/*.ttf" in spec


# -- the stall watchdog -------------------------------------------------------------------------------------


def test_the_stall_watchdog_logs_a_stuck_gui_thread_with_its_stack(qapp):
    from PySide6.QtCore import QTimer

    from yuki.ui.stall import StallWatchdog, maybe_start

    got: list[dict] = []
    dog = StallWatchdog(sink=got.append, process="test").start()
    try:
        pump(qapp, 0.1)
        assert got == []                                                 # an idle loop is not a stall

        def stuck_here() -> None:
            time.sleep(0.12)

        QTimer.singleShot(0, stuck_here)
        pump(qapp, 0.3)
    finally:
        dog.stop()
    assert len(got) == 1 and 90 <= got[0]["ms"] <= 400, got
    assert any("stuck_here" in line for line in got[0]["stack"])          # where it was stuck
    os.environ.pop("YUKI_STALL_WATCH", None)
    assert maybe_start("x") is None                                     # debug only: off by default


# -- the page snapshot --------------------------------------------------------------------------------------


def test_page_snapshots_are_encrypted_and_only_for_their_database(tmp_path):
    from yuki.memory.crypto import FieldCipher
    from yuki.ui.window import snapshot
    from yuki.ui.window.data import StatusView

    cipher = FieldCipher(os.urandom(32))
    view = StatusView(True, False, 9, 2, None, None, 0.5, None, None, [], None, ("00:30", "08:00"), {}, None, True,
                      "Mic", "Speaker", True, None)
    today_view = _day()   # 26 Sep 2026: not today, so it is left out when read back
    path = tmp_path / "window-snapshot.bin"
    assert snapshot.save(path, cipher, {"status": view, "today": today_view, "chat": [1, 2]}, db="A")
    raw = path.read_bytes()
    assert b"Speaker" not in raw and b"Coding the skyline" not in raw        # encrypted at rest
    got = snapshot.load(path, cipher, db="A")
    assert got["status"] == view and "chat" not in got and "today" not in got
    assert snapshot.load(path, cipher, db="B") == {}                         # another database's snapshot
    assert snapshot.load(path, FieldCipher(os.urandom(32)), db="A") == {}    # another key: unreadable
    path.write_bytes(raw[:-3] + b"xyz")
    assert snapshot.load(path, cipher, db="A") == {}                         # tampered: rejected before unpickling


def test_a_reopened_window_shows_the_snapshot_then_refreshes(qapp, tmp_path):
    from yuki.memory.api import MemoryClient
    from yuki.ui.window.app import WindowApp

    memory = MemoryClient.open(tmp_path / "memory.db")
    try:
        ui = tmp_path / "ui.json"
        first = WindowApp(memory, db_path=tmp_path / "memory.db", page="status", connect=False, ui_path=ui)
        first.show(offscreen=True)
        end = time.monotonic() + 10
        while "status" not in first._views and time.monotonic() < end:
            pump(qapp, 0.02)
        first.win.close()
        assert first.save_snapshot() and first.snapshot_path().exists()
        again = WindowApp(memory, db_path=tmp_path / "memory.db", page="status", connect=False, ui_path=ui)
        again._loading.add("status")        # hold the fresh read back: the page must come from the snapshot
        again.show(offscreen=True)
        end = time.monotonic() + 10
        while "status" not in again.win.pages and time.monotonic() < end:
            pump(qapp, 0.02)
        assert "status" in again.win.pages and "status" in again._stale
        again._loading.discard("status")
        again.load("status")
        end = time.monotonic() + 10
        while "status" in again._stale and time.monotonic() < end:
            pump(qapp, 0.02)
        assert "status" not in again._stale
        again.win.close()
    finally:
        memory.close()


# -- placeholders that keep their size ----------------------------------------------------------------------


def test_a_page_placeholder_sits_in_the_pages_own_slot_and_the_moment_panel_never_resizes(qapp):
    from yuki.ui.window.live import LiveWindow, _Loading
    from yuki.ui.window.pages.today import MOMENT_H, MomentPanel
    from yuki.ui.window.skyline import moment_at
    from yuki.ui.window.theme import theme

    t = theme()
    win = LiveWindow(t, current="today")
    slot = win.slots["today"]
    assert isinstance(slot.layout().itemAt(0).widget(), _Loading)       # the skeleton, in the page's own slot
    assert win.slots["me"].layout().count() == 0                         # other pages cost nothing until shown
    win.show_page("me")
    assert isinstance(win.slots["me"].layout().itemAt(0).widget(), _Loading)
    panel = MomentPanel(t)
    day = _day()
    sizes = set()
    for m in (None, moment_at(day, day.since + 20.2 * 3600), moment_at(day, day.since + 3 * 3600)):
        panel.set_moment(m)
        if m is not None:
            m.title = m.title * (8 if m.kind == "session" else 1)          # a very long title still fits two lines
        sizes.add((panel.height(), panel.sizeHint().height(), panel.minimumHeight()))
    assert {s[0] for s in sizes} == {MOMENT_H}
    win.close()


def test_a_long_thread_paints_its_newest_bubbles_first_and_fills_in_above(qapp):
    from PySide6.QtCore import Qt

    from yuki.ui import chatview
    from yuki.ui.chat_model import ChatModel
    from yuki.ui.window.theme import theme

    model = ChatModel()
    for i in range(90):
        model.apply({"type": "user", "text": f"message {i}", "job": 1000 + i, "source": "window", "at": 1e9 + i * 60})
    view = chatview.ChatThreadView(model, lambda: theme(), look="window")
    view.resize(700, 500)
    view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    view.show()
    assert len(view._widgets) == chatview.FIRST                  # the first paint builds only the newest
    end = time.monotonic() + 5
    while len(view._widgets) < chatview.WINDOW and time.monotonic() < end:
        pump(qapp, 0.05)
    assert len(view._widgets) == chatview.WINDOW                 # the rest filled in above, a few per turn
    bar = view.area.verticalScrollBar()
    assert bar.value() == bar.maximum()                           # still on the newest
    view.close()
