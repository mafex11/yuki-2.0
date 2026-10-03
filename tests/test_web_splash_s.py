"""The window's startup splash and Settings -> About (06a planet, planet-splash.js), offline: the tray asks for the
splash on the first window of a Yuki run only; the window puts it in the page's address (still with reduced motion),
answers the page's ready with whether a full-screen app is in front, and sends the version; the native window loads
the app icon at its DPI sizes. The page's own behaviour (plays, fades into the page, click / Esc skip, still frame,
covered, About) is checked in the headless shell by scripts/check_splash.js (never a visible window, invented data).
The real memory, ui.json and voice.json are never touched."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuki.ui import web_window, webview2

ROOT = Path(__file__).resolve().parent.parent
WEB = web_window.WEB_DIR


class _Host:
    def __init__(self) -> None:
        self.posted: list[dict] = []
        self.hwnd = 0
        self.marks: dict = {}
        self.o = SimpleNamespace(placement="offscreen")

    def post(self, message) -> None:
        self.posted.append(json.loads(json.dumps(message, default=str)))

    def kinds(self, kind: str) -> list[dict]:
        return [m for m in self.posted if m.get("kind") == kind]


def _window(tmp_path, monkeypatch, **kw):
    win = web_window.WebWindow(db_path=tmp_path / "mem" / "memory.db", ui_path=tmp_path / "ui.json", connect=False,
                               fixture=web_window.load_fixture(), **kw)
    host = _Host()
    win.host = host
    return win, host


def test_the_splash_knob_follows_the_ask_and_reduced_motion():
    assert web_window.splash_query(False, {"reduce_motion": False}) == ""
    assert web_window.splash_query(True, {"reduce_motion": False}) == "splash=play"
    assert web_window.splash_query(True, {"reduce_motion": True}) == "splash=still"


def test_the_tray_asks_for_the_splash_on_its_first_window_only(tmp_path, monkeypatch):
    from yuki.ui import ui_settings, window_host

    launched: list[list[str]] = []

    class _Popen:
        def __init__(self, cmd, **kw):
            launched.append(cmd)
            self.pid = 7

        def poll(self):
            return 0                                                # the window was closed again

    host = SimpleNamespace(proc=None, server=None, start=lambda: None, _log=lambda *a, **k: None, _splash_shown=False)
    monkeypatch.setattr(window_host.subprocess, "Popen", _Popen)
    monkeypatch.setattr(window_host, "_allow_foreground", lambda pid: None)
    monkeypatch.setattr(ui_settings, "settings_path", lambda: tmp_path / "ui.json")
    monkeypatch.setattr(window_host.WindowHost, "_web_ok", True)
    host.window_engine = lambda: window_host.WindowHost.window_engine(host)
    open_window = window_host.WindowHost.open_window
    assert open_window(host, "home") and launched[-1][-3:] == ["--page", "home", "--splash"]
    assert open_window(host) and "--splash" not in launched[-1]          # opened again later: never again
    assert open_window(host, "me") and "--splash" not in launched[-1]
    assert window_host.WindowHost._splash_shown is False                 # a new tray run starts with it due


def test_the_qt_window_never_gets_the_flag(tmp_path, monkeypatch):
    from yuki.ui import ui_settings, window_host

    launched: list[list[str]] = []
    monkeypatch.setattr(window_host.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd) or SimpleNamespace(pid=1, poll=lambda: 0))
    monkeypatch.setattr(window_host, "_allow_foreground", lambda pid: None)
    ui = tmp_path / "ui.json"
    ui.write_text(json.dumps({"window_engine": "qt"}), encoding="utf-8")
    monkeypatch.setattr(ui_settings, "settings_path", lambda: ui)
    host = SimpleNamespace(proc=None, server=None, start=lambda: None, _log=lambda *a, **k: None, _splash_shown=False)
    host.window_engine = lambda: window_host.WindowHost.window_engine(host)
    assert window_host.WindowHost.open_window(host) and "--splash" not in launched[-1]


def test_ready_answers_whether_the_splash_may_play(tmp_path, monkeypatch):
    win, host = _window(tmp_path, monkeypatch, splash=True)
    monkeypatch.setattr(win, "cover_now", lambda: False)
    win.on_message({"op": "ready"})
    assert host.kinds("splash") == [{"kind": "splash", "on": True}]
    assert host.posted[0]["kind"] == "splash"                            # before anything else: the page waits on it

    win2, host2 = _window(tmp_path, monkeypatch, splash=True)
    monkeypatch.setattr(win2, "cover_now", lambda: True)                 # a full-screen game in front
    win2.on_message({"op": "ready"})
    assert host2.kinds("splash") == [{"kind": "splash", "on": False}]

    win3, host3 = _window(tmp_path, monkeypatch)                         # not the first window of the run
    win3.on_message({"op": "ready"})
    assert not host3.kinds("splash")


def test_cover_now_is_windows_answer_and_never_this_window(monkeypatch):
    win = web_window.WebWindow(connect=False, fixture={"schema": "yuki-window/1"})
    win.host = SimpleNamespace(hwnd=0)

    def fake(state: int, fg: int):
        def q(ref):
            ref._obj.value = state
            return 0
        monkeypatch.setattr(web_window.ctypes, "windll", SimpleNamespace(
            shell32=SimpleNamespace(SHQueryUserNotificationState=q),
            user32=SimpleNamespace(GetForegroundWindow=lambda: fg)))

    fake(2, 55)                    # QUNS_BUSY: a full-screen app, and another window in front
    assert win.cover_now() is True
    fake(5, 55)                    # QUNS_ACCEPTS_NOTIFICATIONS: nothing full screen
    assert win.cover_now() is False
    win.host = SimpleNamespace(hwnd=55)
    fake(3, 55)                    # full screen, but it is this window in front
    assert win.cover_now() is False


def test_the_boot_carries_the_version(tmp_path, monkeypatch):
    import time

    from yuki import __version__

    win, host = _window(tmp_path, monkeypatch)
    win.on_message({"op": "ready"})
    end = time.time() + 5
    while not host.kinds("boot") and time.time() < end:
        time.sleep(0.02)
    assert host.kinds("boot")[0]["version"] == __version__


def test_main_puts_the_splash_in_the_address_and_gives_the_window_the_icon(monkeypatch, tmp_path):
    from yuki.ui import ui_settings

    seen: list = []

    class _FakeHost:
        def __init__(self, options):
            seen.append(options)
            self.o = options
            self.hwnd = 0
            self.marks = options.marks
            self.on_closing = None

        def run(self):
            return 0

    monkeypatch.setattr(webview2, "runtime_version", lambda loader=None: "154.0.0.0")
    monkeypatch.setattr(webview2, "loader_path", lambda: None)
    monkeypatch.setattr(web_window, "WebView2Host", _FakeHost)
    monkeypatch.setattr(ui_settings, "load", lambda *a, **k: ui_settings.UiSettings())
    monkeypatch.setattr(web_window, "appearance", lambda: {"accent": "green", "reduce_motion": False})
    args = ["--fixture", "--offscreen", "--no-connect", "--db", str(tmp_path / "memory.db")]
    assert web_window.main(args + ["--splash", "--query", "blink=0"]) == 0
    assert seen[-1].start_url.endswith("/index.html?blink=0&splash=play")
    assert seen[-1].icon == web_window.APP_ICON and seen[-1].icon.exists()
    assert web_window.main(args) == 0
    assert "splash" not in seen[-1].start_url
    monkeypatch.setattr(web_window, "appearance", lambda: {"accent": "green", "reduce_motion": True})
    assert web_window.main(args + ["--splash"]) == 0
    assert seen[-1].start_url.endswith("/index.html?splash=still")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows icons")
def test_the_window_loads_the_icon_at_its_dpi_sizes():
    host = webview2.WebView2Host(webview2.HostOptions(folders={}, start_url=f"https://{webview2.APP_HOST}/index.html",
                                                      user_data_dir=Path("."), icon=web_window.APP_ICON))
    big, small = host._load_icons()
    try:
        assert big and small and len(host._icons) == 2
    finally:
        for h in host._icons:
            webview2.ctypes.windll.user32.DestroyIcon(webview2.wintypes.HICON(h))
    none = webview2.WebView2Host(webview2.HostOptions(folders={}, start_url=f"https://{webview2.APP_HOST}/index.html",
                                                      user_data_dir=Path(".")))
    assert none._load_icons() == (None, None)


def test_the_page_loads_the_splash_before_the_app_and_about_keeps_it():
    index = (WEB / "index.html").read_text(encoding="utf-8")
    order = [index.index(f'src="{s}"') for s in ("lattice/field.js", "dots.js", "planet-splash.js", "bridge.js", "app.js")]
    assert order == sorted(order)
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "B.on('splash'" in app and "splash.quiet" in app and "splash.paged()" in app
    settings = (WEB / "pages" / "settings.js").read_text(encoding="utf-8")
    assert "'about'" in settings and "keep: ''" in settings and "about-replay" in settings
    # the copies are the gallery's component and its one dependency, unchanged in what they draw
    assert "window.PlanetSplash = { mount, still, REF, duration: 1.2 }" in (WEB / "planet-splash.js").read_text(encoding="utf-8")
    assert "window.YukiDots = {" in (WEB / "dots.js").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_splash_plays_fades_skips_and_about_replays_in_headless_chrome():
    """scripts/check_splash.js: the real index.html in Playwright's headless shell (never a visible window, a
    throwaway profile), a stub host, the invented fixture."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import web_shots

        exe = web_shots.headless_shell()
    except (SystemExit, ImportError):
        pytest.skip("chrome-headless-shell is not installed")
    r = subprocess.run(["node", str(ROOT / "scripts" / "check_splash.js"), "--chrome", str(exe)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
