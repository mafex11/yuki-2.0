"""The Yuki window, phase 2: settings writers, the frameless chrome, pages offscreen (nothing clipped in a
narrow window), the overlay's thread, and yuki-ui's translation of the voice side's chat events. Offscreen only
(rule 7)."""

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


def test_ui_settings_round_trip_keeps_unknown_keys(tmp_path):
    import json

    from yuki.ui import ui_settings

    path = tmp_path / "ui.json"
    path.write_text(json.dumps({"future_key": 3, "window": [1, 2, 3, 4, 5], "open_hotkey": "  Ctrl+Space "}),
                    encoding="utf-8")
    s = ui_settings.load(path)
    assert (s.window, s.open_hotkey, s.reduce_motion) == ([1, 2, 3, 4], "ctrl+space", False)
    s = ui_settings.update(path, reduce_motion=True, not_a_setting=1)
    assert s.reduce_motion is True
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["future_key"] == 3 and "not_a_setting" not in raw


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


# -- pages, offscreen --------------------------------------------------------------------------------------


def test_every_page_builds_and_paints_on_an_empty_memory(qapp, tmp_path):
    from datetime import date

    from yuki.memory.api import MemoryClient
    from yuki.ui.chat_model import ChatModel
    from yuki.ui.window import data as D
    from yuki.ui.window.pages import chat, me, settings, status, today, yuki_page
    from yuki.ui.window.shots import demo_thread, grab
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


def test_pages_never_ask_for_more_width_than_the_smallest_window(qapp, tmp_path):
    """The window's smallest size (900 px) leaves the pages 668 px: every page fits, so nothing is clipped at
    the edges (the cards go to one column instead, and text wraps inside them)."""
    from datetime import date

    from yuki.memory.api import MemoryClient
    from yuki.ui.chat_model import ChatModel
    from yuki.ui.window import data as D
    from yuki.ui.window.live import LiveWindow
    from yuki.ui.window.pages import chat, me, settings, status, today, yuki_page
    from yuki.ui.window.shots import demo_thread
    from yuki.ui.window.theme import theme

    memory = MemoryClient.open(tmp_path / "memory.db")
    try:
        t = theme()
        win = LiveWindow(t)
        room = 900 - win.sidebar.width()
        model = ChatModel()
        demo_thread(model)
        pages = {
            "today": today.build(t, D.build_day(memory, date.today()), today=True),
            "me": me.build(t, D.build_me(memory), progressive=False),
            "yuki": yuki_page.build(t, D.build_yuki(memory)),
            "status": status.build(t, D.build_status(memory)),
            "settings": settings.build(t, D.build_settings(memory), advanced=True),
            "chat": chat.build(t, model, connected=False),
        }
        for key, w in pages.items():
            assert w.minimumSizeHint().width() <= room, (key, w.minimumSizeHint().width(), room)
        win.close()
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
