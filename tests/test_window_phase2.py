"""The Yuki window, phase 2: settings writers, the glass, the frameless chrome, pages offscreen, the overlay's
thread, and yuki-ui's translation of the voice side's chat events. Offscreen only (rule 7)."""

from __future__ import annotations

import os
import tomllib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

import pytest  # noqa: E402


def _gui_less_app() -> bool:
    """Another test module made a QCoreApplication (no GUI): painting in this process would crash."""
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    app = QCoreApplication.instance()
    return app is not None and not isinstance(app, QApplication)


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    if _gui_less_app():
        pytest.skip("GUI-less QCoreApplication in this process: covered by test_gui_tests_in_their_own_process")
    return QApplication.instance() or QApplication([])


def test_gui_tests_in_their_own_process():
    """When the voice tests' QCoreApplication came first, this file's GUI tests run again in a fresh process."""
    if not _gui_less_app():
        pytest.skip("the GUI tests ran in this process")
    import subprocess
    import sys

    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", __file__, "-k",
                           "not own_process"], capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]


# -- settings files -------------------------------------------------------------------------------------

PRIVACY = """# Yuki memory - privacy rules (defaults).
version = 1

[apps]
# Never read windows of these processes.
blocked_processes = [
    "1Password.exe",
    "KeePass.exe",
]

[user]
# Your own blocklist.
blocked_apps = []
blocked_domains = []

[meetings]
names = ["Zoom"]
"""


def test_privacy_key_changes_in_place_and_keeps_comments(tmp_path):
    from yuki.ui.window.settings_io import blocked, set_blocked, set_toml_key, update_privacy

    path = tmp_path / "privacy.toml"
    path.write_text(PRIVACY, encoding="utf-8")
    set_blocked("apps", ["discord.exe", "Discord.exe", " Steam "], path)   # de-duplicated, trimmed
    update_privacy("nudges", "quiet_start", "23:30", path)                 # a table that was missing
    update_privacy("nudges", "enabled", False, path)
    update_privacy("apps", "blocked_processes", ["1Password.exe"], path)   # a multi-line array replaced
    text = path.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert data["user"]["blocked_apps"] == ["discord.exe", "Steam"]
    assert data["nudges"] == {"quiet_start": "23:30", "enabled": False}
    assert data["apps"]["blocked_processes"] == ["1Password.exe"]
    assert data["meetings"]["names"] == ["Zoom"] and data["version"] == 1
    assert "# Your own blocklist." in text and "# Never read windows" in text
    assert blocked("apps", path) == ["discord.exe", "Steam"]
    # a quote in a value stays a valid TOML string
    assert tomllib.loads(set_toml_key("", "user", "blocked_domains", ['a"b.com']))["user"]["blocked_domains"] == \
        ['a"b.com']


def test_voice_prefs_written_directly_only_as_a_merge(tmp_path):
    import json

    from yuki.ui.window.settings_io import write_voice_prefs

    path = tmp_path / "voice.json"
    path.write_text(json.dumps({"tts_voice": "abc", "mic_device": "Mic 1"}), encoding="utf-8")
    prefs = write_voice_prefs({"wake": False, "mic": None, "speaker": "Headphones"}, path)
    assert prefs == {"tts_voice": "abc", "wake": False, "speaker_device": "Headphones"}


def test_ui_settings_round_trip_clamps_and_keeps_unknown_keys(tmp_path):
    import json

    from yuki.ui import ui_settings

    path = tmp_path / "ui.json"
    path.write_text(json.dumps({"future_key": 3, "glass": "FULL", "bend": 7}), encoding="utf-8")
    s = ui_settings.load(path)
    assert (s.glass, s.bend, s.blur, s.appearance) == ("full", 2.0, 1.0, "system")
    s = ui_settings.update(path, glass="nonsense", blur=0.4, reduce_motion=True, not_a_setting=1)
    assert (s.glass, s.blur, s.reduce_motion) == (ui_settings.DEFAULT_GLASS, 0.4, True)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["future_key"] == 3 and "not_a_setting" not in raw
    pol = s.policy()
    # 2026-09-27: the window's background is plain, so page glass is a cached GPU picture only in Full (and
    # not See-through); Light and Full both bend what floats over the app's own content
    # every page's glass is a cached picture (drawn on a change, then composited): on the GPU only in Full
    assert pol.refracts("regular") and ui_settings.UiSettings(glass="off").policy().refracts("regular")
    assert pol.gpu == (s.glass == "full") and ui_settings.UiSettings(glass="full").policy().gpu
    assert not ui_settings.UiSettings(glass="full", background="see-through").policy().gpu
    assert ui_settings.UiSettings(glass="light").policy().floats_refract
    assert not ui_settings.UiSettings(glass="off").policy().floats_refract
    assert ui_settings.update(path, background="nonsense").background == "solid"


# -- the frameless chrome --------------------------------------------------------------------------------


def test_hit_test_edges_caption_and_snap_layout_button():
    from yuki.ui.window import chrome

    def cap(x, y):
        return y < 44 and x < 1100

    def maxb(x, y):
        return 1180 <= x < 1226 and y < 32

    ht = lambda x, y, maxed=False: chrome.hit_test(x, y, 1280, 840, maximized=maxed, caption=cap, max_button=maxb)  # noqa: E731
    assert ht(2, 2) == chrome.HTTOPLEFT and ht(1278, 838) == chrome.HTBOTTOMRIGHT
    assert ht(640, 1) == chrome.HTTOP and ht(1, 400) == chrome.HTLEFT and ht(1279, 400) == chrome.HTRIGHT
    assert ht(640, 20) == chrome.HTCAPTION
    assert ht(1200, 10) == chrome.HTMAXBUTTON        # Windows 11 shows snap layouts on hover
    assert ht(640, 400) == chrome.HTCLIENT
    assert ht(640, 1, True) == chrome.HTCAPTION       # maximised: no resize edges
    assert ht(-1, 5) == 0


# -- the Light glass (CPU) ---------------------------------------------------------------------------------


def test_light_glass_bends_the_rim_and_reuses_its_displacement_map(qapp):
    import numpy as np
    from PySide6.QtCore import QRectF, QSize

    from yuki.ui import lens
    from yuki.ui.window.proof import pattern
    from yuki.ui.window.refract import Shape
    from yuki.ui.window.theme import theme

    t = theme("light")
    base = pattern(QSize(300, 200), 1.0)
    shape = Shape(QRectF(40, 40, 200, 120), 24, t.optics["regular"], 1.0, None, None, False)
    lens.BANDS.clear()
    first = lens.render_light(base, [[shape]], shadows=False)
    misses = lens.BANDS.misses
    second = lens.render_light(base, [[shape]], shadows=False)
    assert lens.BANDS.misses == misses and lens.BANDS.hits >= 1      # computed once per shape size

    def arr(img):
        b = np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.bytesPerLine() // 4, 4)
        return b[:, : img.width(), :3].astype(int)

    a, b0, c = arr(first), arr(base.convertToFormat(first.format())), arr(second)
    assert (a == c).all()                                              # same input, same picture
    assert (a[5:30, 5:30] == b0[5:30, 5:30]).all()                     # outside the glass: untouched
    assert np.abs(a[44:60, 60:220] - b0[44:60, 60:220]).mean() > 10    # the rim bends and lights
    inner, under = a[90:110, 120:160].mean(), b0[90:110, 120:160].mean()
    assert inner > under                                               # light glass never darker than its tone floor


def test_glass_setting_decides_what_is_refracted_and_what_is_painted(qapp):
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter

    from yuki.ui import lens
    from yuki.ui.window.glass import paint_glass
    from yuki.ui.window.refract import COLLECTOR
    from yuki.ui.window.theme import theme

    t = theme("dark")
    img = QImage(200, 100, QImage.Format.Format_ARGB32_Premultiplied)
    saved = lens.policy()
    try:
        both = {"regular", "control"}   # recorded for the page's cached picture in every mode; the bubble never
        for mode, expect in (("full", both), ("light", both), ("off", both)):
            lens.set_policy(lens.GlassPolicy(mode))
            COLLECTOR.begin(t)
            p = QPainter(img)
            paint_glass(p, QRectF(10, 10, 180, 80), 20, t.level("regular"))
            paint_glass(p, QRectF(20, 20, 60, 26), 13, t.level("control"))
            paint_glass(p, QRectF(100, 20, 60, 26), 13, t.level("control"), painted=True)   # a chat bubble
            p.end()
            shapes = COLLECTOR.end()
            got = {s.level.name for s in shapes}   # optics are scaled by the settings; the level says which
            assert got == expect, mode
    finally:
        lens.set_policy(saved)


# -- pages, offscreen --------------------------------------------------------------------------------------


def test_every_page_builds_and_paints_on_an_empty_memory(qapp, tmp_path):
    from datetime import date

    from yuki.memory.api import MemoryClient
    from yuki.ui.chat_model import ChatModel
    from yuki.ui.window import data as D
    from yuki.ui.window.pages import chat, me, settings, status, today, yuki_page
    from yuki.ui.window.phase2 import demo_thread, grab
    from yuki.ui.window.theme import theme

    memory = MemoryClient.open(tmp_path / "memory.db")
    try:
        t = theme("light")
        model = ChatModel()
        demo_thread(model)
        widgets = {
            "today": today.build(t, D.build_day(memory, date.today()), today=True),
            "me": me.build(t, D.build_me(memory)),
            "yuki": yuki_page.build(t, D.build_yuki(memory)),
            "status": status.build(t, D.build_status(memory)),
            "settings": settings.build(t, D.build_settings(memory), advanced=True),
            "chat": chat.build(t, model, connected=False),
        }
        for key, w in widgets.items():
            w.resize(1000, 800)
            w.show()
            qapp.processEvents()
            img = grab(w)
            assert img.width() == 1000 and not img.isNull(), key
            w.close()
    finally:
        memory.close()


# -- the overlay's live thread ----------------------------------------------------------------------------


def test_overlay_thread_mode_keeps_the_card_interface(qapp):
    from yuki.ui.chat_model import ChatModel
    from yuki.ui.overlay import Overlay, ThreadCard

    model = ChatModel()
    overlay = Overlay()
    overlay.attach_thread(model)
    card = overlay.add_card("open spotify")
    assert isinstance(card, ThreadCard)
    card.set_body("done", tone="reply")
    card.set_meta("3 s")
    nudge = {"id": 7, "kind": "nudge", "text": "Stretch?"}
    ncard = overlay.add_nudge_card(nudge, "Yuki · nudge", accent=None, at=1.0)
    assert overlay.nudge_card(7) is ncard and [i.kind for i in model.items] == ["nudge"]
    ncard.set_reply_state("replying")
    assert model.items[0].reply_state == "replying"
    got = []
    ncard.reply_clicked.connect(got.append)
    overlay._on_thread_reply(nudge)
    assert got == [nudge]
    overlay.fit()
    assert not overlay.thread.isHidden() and overlay.thread.height() > 0


# -- yuki-ui's host: the voice side's events become the thread ------------------------------------------------


class _Sig:
    def __init__(self) -> None:
        self.slots = []

    def connect(self, fn) -> None:
        self.slots.append(fn)

    def emit(self, *a) -> None:
        for fn in self.slots:
            fn(*a)


class _Runtime:
    def __init__(self) -> None:
        for name in ("started", "tool_called", "tool_finished", "said", "asked", "finished", "failed", "summarized",
                     "handed_off"):
            setattr(self, name, _Sig())


class _Voice:
    def __init__(self) -> None:
        for name in ("chat_event", "state_changed", "conversation_changed", "wake_changed", "devices_changed"):
            setattr(self, name, _Sig())
        self.jobs: set[int] = set()

    def owns(self, job: int) -> bool:
        return job in self.jobs


class _Ui:
    def __init__(self) -> None:
        self.runtime = _Runtime()
        self.voice = _Voice()


def test_host_turns_the_voice_sides_events_into_bubbles(qapp):
    from yuki.ui.window_host import WindowHost

    ui = _Ui()
    host = WindowHost(ui)
    v = ui.voice.chat_event.emit
    ui.voice.conversation_changed.emit(True)
    v({"type": "user_utterance", "text": "find the invoice and scroll down", "ts": 100.0, "turn_id": "s-1"})
    v({"type": "job_event", "job_id": 31, "state": "submitted", "summary": "find the invoice"})
    ui.voice.jobs.add(31)
    ui.runtime.started.emit("worker", 31, "full brain request")
    v({"type": "assistant_speech", "turn_id": "s-1", "text": "On it", "final": False, "interrupted": False})
    v({"type": "hand_step", "turn_id": "s-1", "name": "scroll", "args": "down 3", "ok": True, "result": "moved",
       "ms": 250})
    ui.runtime.tool_called.emit("worker", 31, "read_page", {"what": "inbox"})
    ui.runtime.tool_finished.emit("worker", 31, "read_page", True, "3 unread")
    v({"type": "assistant_speech", "turn_id": "s-1", "text": "On it, looking", "final": True, "interrupted": True})
    v({"type": "user_utterance", "text": "stop", "ts": 105.0, "turn_id": "s-2"})
    v({"type": "turn_closed", "turn_id": "s-2", "addressed": "others", "replied": False, "cut_by_user": False})
    ui.runtime.finished.emit("worker", 31, "Found it.")
    ui.runtime.summarized.emit("worker", 31, {"wall_s": 5, "model_calls": 2, "cost_usd": 0.012})
    items = host.model.items
    assert [(i.kind, i.text) for i in items] == [("user", "find the invoice and scroll down"),
                                                 ("assistant", "On it, looking"), ("user", "stop")]
    yuki = items[1]
    assert yuki.status == "interrupted" and yuki.source == "voice"
    assert [s.kind for s in yuki.steps] == ["job", "hand", "tool"]
    assert yuki.steps[0].label == "find the invoice" and yuki.steps[0].ok is True
    assert items[2].status == "others"
    # a typed request: the bubble comes from what the user typed, the answer from the runtime
    host.user_message(40, "what's the time?")
    ui.runtime.started.emit("worker", 40, "what's the time?\n\n[Coach note]")
    ui.runtime.finished.emit("worker", 40, "It's 21:30.")
    ui.runtime.summarized.emit("worker", 40, {"wall_s": 2, "model_calls": 1, "cost_usd": 0.004})
    assert [(i.kind, i.text) for i in host.model.items[-2:]] == [("user", "what's the time?"),
                                                                 ("assistant", "It's 21:30.")]
    assert host.model.items[-1].meta == "0.4¢"
