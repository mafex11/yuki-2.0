"""The Lattice window in HTML (Phase E3/E4), offline: the bundle (strict CSP, no inline code, no markup from data,
no infinite animation, the fixture invented), the bridge (snapshot-first paint, the tray's events as chat diffs,
actions and their replies), the settings ops (each writes the file the tray reads, in a temp folder), the engine
setting and its fall back to the Qt window, the offer button and Undo through the nudge controller, and the page
builders over stand-in memories. A real WebView2 window opens only with YUKI_WEBVIEW2_TEST=1 (off-screen, never
activated, closed by itself). The real memory, ui.json, voice.json, privacy file and policy are never touched."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuki.ui import web_data, web_window, webview2

ROOT = Path(__file__).resolve().parent.parent
WEB = web_window.WEB_DIR


# -- the bundle ------------------------------------------------------------------------------------------------------


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self.links: list[str] = []
        self.csp = ""
        self.inline_script = False
        self.style_tags = self.style_attrs = 0
        self.handlers: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.style_attrs += "style" in a
        self.handlers += [k for k in a if k.startswith("on")]
        if tag == "script":
            self.scripts.append(a.get("src") or "")
            self._in_script = True
        elif tag == "link":
            self.links.append(a.get("href") or "")
        elif tag == "style":
            self.style_tags += 1
        elif tag == "meta" and a.get("http-equiv", "").lower() == "content-security-policy":
            self.csp = a.get("content", "")

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script and data.strip():
            self.inline_script = True


def _page(name: str) -> _Page:
    p = _Page()
    p.feed((WEB / name).read_text(encoding="utf-8"))
    return p


def _js_files() -> list[Path]:
    return [WEB / "lattice" / "field.js", WEB / "bridge.js", WEB / "ui.js", WEB / "app.js"] + sorted((WEB / "pages").glob("*.js"))


def test_the_fixture_is_invented_and_covers_every_page():
    data = web_window.load_fixture()
    assert data["schema"] == "yuki-window/1" and "Invented" in data["note"]
    assert set(data["pages"]) == {"home", "work", "day", "me", "playbook", "activity", "settings"}
    assert data["pages"]["home"]["offers"] and data["pages"]["me"]["people"] and data["chat"]["items"]
    text = json.dumps(data, ensure_ascii=False)
    for name in ("Hemanth", "quix", "Mafex", "Jatin", "Discord", "Eastvale", "Corvana", "Varun", "VALORANT", "Kian"):
        assert name not in text, name


def test_the_fixture_file_is_what_its_generator_writes():
    sys.path.insert(0, str(ROOT / "scripts"))
    import make_web_fixture

    data = make_web_fixture.build()
    assert web_window.load_fixture() == json.loads(json.dumps(data, ensure_ascii=False))


@pytest.mark.parametrize("name", ["index.html", "preview.html"])
def test_the_pages_run_under_a_strict_csp_with_no_inline_code(name):
    p = _page(name)
    csp = {d.split()[0]: d.split()[1:] for d in p.csp.split(";") if d.strip()}
    assert csp["default-src"] == ["'none'"] and csp["script-src"] == ["'self'"] and csp["style-src"] == ["'self'"]
    assert csp["connect-src"] == ["'none'"] and csp["frame-src"] == ["'none'"] and csp["object-src"] == ["'none'"]
    assert csp["base-uri"] == ["'none'"] and csp["form-action"] == ["'none'"]
    assert not p.inline_script and p.style_tags == 0 and p.style_attrs == 0 and p.handlers == []


def test_every_asset_is_there_and_the_real_page_never_loads_the_fixture():
    real, preview = _page("index.html"), _page("preview.html")
    assert "fixtures/window.js" not in real.scripts and "fixtures/window.js" in preview.scripts
    assert [s for s in preview.scripts if s != "fixtures/window.js"] == real.scripts
    assert real.scripts[0] == "lattice/field.js" and real.scripts[-1] == "app.js"   # pages register before the app
    for ref in real.scripts + real.links + preview.scripts:
        assert (WEB / ref).exists(), ref
    assert {f"pages/{n}.js" for n in ("home", "work", "me", "playbook", "activity", "settings", "drawer")} <= set(real.scripts)


def test_no_infinite_animation_no_markup_from_data_no_network():
    css = (WEB / "lattice.css").read_text(encoding="utf-8")
    rules = "".join(part.split("*/", 1)[-1] for part in css.split("/*"))   # the stylesheet without its comments
    assert "infinite" not in rules and "animation: rec" not in rules     # idle CPU (25-build-E1-E2-E5.md §2)
    for js in _js_files():
        source = js.read_text(encoding="utf-8")
        for bad in ("eval(", "fetch(", "XMLHttpRequest", "new Function", "document.write", "outerHTML", "insertAdjacentHTML"):
            assert bad not in source, (js.name, bad)
        if js.name != "ui.js":
            assert "innerHTML" not in source, js.name               # the only markup: field.js's static icon table
    ui = (WEB / "ui.js").read_text(encoding="utf-8")
    assert ui.count("innerHTML") == 1 and "t.innerHTML = L.icon(name, ghost)" in ui


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_every_script_parses():
    r = subprocess.run(["node", str(ROOT / "scripts" / "check_web_js.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_only_our_origin_may_load_and_web_links_go_to_the_browser():
    ok, ext = webview2.is_allowed_navigation, webview2.is_external_link
    assert ok("https://app.yuki.example/index.html?state=listening")
    for uri in ("http://app.yuki.example/index.html", "https://app.yuki.example.evil.com/", "file:///C:/x.html",
                "https://fonts.yuki.example/Doto.ttf", "about:blank", "javascript:alert(1)", "https://localhost:8080/"):
        assert not ok(uri), uri
    assert ext("https://example.org/page") and not ext("https://app.yuki.example/x") and not ext("javascript:x")


# -- the bridge ------------------------------------------------------------------------------------------------------


class _Host:
    """WebView2Host's thread-safe surface, recording what the page would get."""

    def __init__(self) -> None:
        self.posted: list[dict] = []
        self.marks: dict = {"main": 100.0}
        self.browser_pid = 4242
        self.runtime = "154.0.0.0"
        self.hwnd = 0
        self.o = SimpleNamespace(placement="offscreen")
        self.invoked: list = []

    def post(self, message) -> None:
        self.posted.append(json.loads(json.dumps(message, default=str)))

    def invoke(self, fn) -> None:
        self.invoked.append(fn)

    def bring_forward(self) -> None:
        pass

    def close(self) -> None:
        pass

    def capture(self, path, done=None) -> bool:
        return True

    def kinds(self, kind: str) -> list[dict]:
        return [m for m in self.posted if m.get("kind") == kind]


def _wait(pred, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _cipher(folder: Path):
    from yuki.memory.crypto import FieldCipher, load_or_create_key

    folder.mkdir(parents=True, exist_ok=True)
    return FieldCipher(load_or_create_key(folder / "memory.key"))


def _window(tmp_path, monkeypatch, **kw) -> tuple[web_window.WebWindow, _Host]:
    win = web_window.WebWindow(db_path=tmp_path / "mem" / "memory.db", ui_path=tmp_path / "ui.json", connect=False,
                               policy_path=tmp_path / "autonomy.json", **kw)
    monkeypatch.setattr(win, "_open_memory", lambda: None)   # no store in these tests
    host = _Host()
    win.host = host
    return win, host


def test_ready_paints_first_from_the_encrypted_snapshot(tmp_path, monkeypatch):
    from yuki.ui.window import snapshot

    cipher = _cipher(tmp_path / "mem")
    home = {"needs": [{"kind": "question", "ref": "q:1", "title": "Invented question?"}], "offers": [], "away": []}
    assert snapshot.save(snapshot.path_for(tmp_path / "ui.json"), cipher, {"home": home, "shell": {"user": {"name": "Sam"}},
                                                                            "nonsense": {}},
                         db=str(tmp_path / "mem" / "memory.db"), pages=snapshot.WEB_PAGES, fmt=snapshot.WEB_FORMAT)
    win, host = _window(tmp_path, monkeypatch)
    win.start()
    assert win._snapshot_done.wait(5)
    win.on_message({"op": "ready", "page": "home"})
    assert _wait(lambda: host.kinds("boot"))
    boot = host.kinds("boot")[0]
    assert boot["source"] == "snapshot" and boot["pages"]["home"] == home and "nonsense" not in boot["pages"]
    assert boot["shell"]["user"]["name"] == "Sam" and boot["shell"]["connected"] is False
    assert set(boot["appearance"]) >= {"accent", "reduce_motion"}
    # fresh data replaces it in place; only pages built from memory are saved for the next opening
    win.send_page("work", {"threads": []})
    assert host.kinds("data")[-1] == {"kind": "data", "page": "work", "data": {"threads": []}, "source": "memory"}
    win.close()
    again = snapshot.load(snapshot.path_for(tmp_path / "ui.json"), cipher, db=str(tmp_path / "mem" / "memory.db"),
                          fmt=snapshot.WEB_FORMAT)
    assert again == {"work": {"threads": []}}


def test_the_two_window_engines_never_read_each_others_snapshot(tmp_path):
    import pickle
    import zlib

    from yuki.ui.window import snapshot

    cipher = _cipher(tmp_path / "mem")
    path, db = tmp_path / "window-snapshot.bin", "x.db"
    assert snapshot.save(path, cipher, {"home": {"a": 1}}, db=db, pages=snapshot.WEB_PAGES, fmt=snapshot.WEB_FORMAT)
    assert snapshot.load(path, cipher, db=db) == {}                           # the Qt window ignores the web one
    assert snapshot.load(path, cipher, db="other.db", fmt=snapshot.WEB_FORMAT) == {}
    assert snapshot.save(path, cipher, {"me": {"b": 2}}, db=db)               # Qt pages: the web window reads nothing
    assert snapshot.load(path, cipher, db=db, fmt=snapshot.WEB_FORMAT) == {}
    raw = pickle.dumps({"format": snapshot.WEB_FORMAT, "db": db, "at": time.time(), "day": "2000-01-01",
                        "views": {"home": {}, "day": {}, "me": {"c": 3}}})
    path.write_bytes(cipher.encrypt_bytes(zlib.compress(raw)))
    assert snapshot.load(path, cipher, db=db, fmt=snapshot.WEB_FORMAT) == {"me": {"c": 3}}   # yesterday's day/home
    path.write_bytes(b"garbage")
    assert snapshot.load(path, cipher, db=db, fmt=snapshot.WEB_FORMAT) == {}


def test_the_fixture_mode_boots_and_answers_every_action(tmp_path, monkeypatch):
    win, host = _window(tmp_path, monkeypatch, fixture=web_window.load_fixture())
    win.on_message({"op": "ready"})
    assert _wait(lambda: host.kinds("boot"))
    boot = host.kinds("boot")[0]
    assert boot["source"] == "fixture" and set(boot["pages"]) >= {"home", "settings"}
    call = boot["shell"]["call"]["started_at"]
    assert abs((time.time() - call) - (46 * 60 + 4)) < 120                    # the fixture's times moved to now
    for action in web_window.WebWindow.ACTIONS:
        win.on_message({"op": "act", "id": action, "action": action})
    win.on_message({"op": "act", "id": "x", "action": "format_disk"})
    assert _wait(lambda: len(host.kinds("reply")) == len(web_window.WebWindow.ACTIONS) + 1)
    replies = {r["id"]: r for r in host.kinds("reply")}
    assert replies["x"]["ok"] is False and "unknown" in replies["x"]["error"]
    assert replies["offer"]["result"]["undo"] is True


def test_without_the_tray_its_actions_say_so_plainly(tmp_path, monkeypatch):
    win, host = _window(tmp_path, monkeypatch)
    for action, fields in (("offer", {"card": "4"}), ("offer_undo", {"card": "4"}), ("stop", {"job": 3}),
                           ("steer", {"job": 3, "text": "also the other one"}), ("submit", {"text": "hi"}), ("askbar", {})):
        win.on_message({"op": "act", "id": action, "action": action, **fields})
    assert _wait(lambda: len(host.kinds("reply")) == 6)
    for r in host.kinds("reply"):
        assert r["ok"] is False and "isn't running" in r["error"], r


def test_the_trays_events_become_chat_diffs_runs_and_live_state(tmp_path, monkeypatch):
    from yuki.ui.chat_model import ChatModel

    win, host = _window(tmp_path, monkeypatch)
    win.booted = True
    tray = ChatModel()
    tray.apply({"type": "user", "job": 31, "text": "Rename the invoices", "at": 1000.0})
    tray.apply({"type": "started", "job": 31, "lane": "worker", "at": 1000.5})
    win._on_tray_event({"ev": "snapshot", "snapshot": tray.snapshot(), "state": {"voice_state": "listening", "wake": True,
                                                                                "conversation": True}})
    assert host.kinds("chat")[-1]["reset"] is True and len(host.kinds("chat")[-1]["items"]) == 2
    win._on_tray_event({"ev": "chat", "event": {"type": "tool_called", "job": 31, "name": "files",
                                               "label": "Renaming a file, invoice-0312.pdf", "at": 1002.0}})
    diff = host.kinds("chat")[-1]
    assert diff["what"] == "changed" and diff["item"]["steps"][0]["label"].startswith("Renaming") and diff["item"]["running"]
    assert _wait(lambda: host.kinds("live") and host.kinds("live")[-1]["shell"].get("runs"))
    run = host.kinds("live")[-1]["shell"]["runs"][0]
    assert run["jobs"] == [31] and run["title"] == "“Rename the invoices”" and run["line"].startswith("Step 1")
    assert host.kinds("live")[-1]["shell"]["voice"] == {"state": "listening", "wake": True, "conversation": True,
                                                         "quiet_until": 0}
    win._on_tray_event({"ev": "focus", "page": "chat"})
    assert host.kinds("focus")[-1] == {"kind": "focus", "page": "home", "thread": True} and host.invoked
    win._on_tray_event({"ev": "permissions", "permissions": {"kinds": []}})
    assert host.kinds("permissions")


# -- settings ops (each writes the file the tray and memory read; here in a temp folder) ----------------------------


def _act(win: web_window.WebWindow, host: _Host, action: str, **fields) -> dict:
    n = len(host.kinds("reply"))
    win.on_message({"op": "act", "id": f"{action}-{n}", "action": action, **fields})
    assert _wait(lambda: len(host.kinds("reply")) > n)
    return host.kinds("reply")[-1]


def test_the_offers_toggle_and_check_ins_write_the_privacy_file(tmp_path, monkeypatch):
    from yuki.ui.window import settings_io

    privacy = tmp_path / "privacy.toml"
    privacy.write_text("# the user's file\n[nudges]\nenabled = true\n", encoding="utf-8")
    monkeypatch.setattr(settings_io, "privacy_path", lambda: privacy)
    win, host = _window(tmp_path, monkeypatch)
    assert _act(win, host, "offers", enabled=False)["ok"]
    assert tomllib.loads(privacy.read_text(encoding="utf-8"))["nudges"]["offers"] == "off"
    assert _act(win, host, "offers", enabled=True)["ok"]
    assert _act(win, host, "coach", enabled=False)["ok"] and _act(win, host, "questions", enabled=False)["ok"]
    parsed = tomllib.loads(privacy.read_text(encoding="utf-8"))["nudges"]
    assert parsed == {"enabled": False, "offers": "yuki_only", "questions_enabled": False}
    assert privacy.read_text(encoding="utf-8").startswith("# the user's file")   # comments kept


def test_autonomy_and_app_access_are_stored_inside_the_users_rules(tmp_path, monkeypatch):
    from yuki.ui import policy
    from yuki.ui.window import settings_io

    privacy = tmp_path / "privacy.toml"
    monkeypatch.setattr(settings_io, "privacy_path", lambda: privacy)
    win, host = _window(tmp_path, monkeypatch)
    r = _act(win, host, "permissions_set", kind="files", level="ask")
    assert r["ok"] and next(k for k in r["permissions"]["kinds"] if k["key"] == "files")["level"] == "ask"
    refused = _act(win, host, "permissions_set", kind="send_messages", level="do_and_tell")
    assert refused["ok"] is False and policy.load(tmp_path / "autonomy.json")["send_messages"] == "ask"
    assert _act(win, host, "app_act", app="Signal", act=False)["ok"]
    assert policy.app_acts(tmp_path / "autonomy.json") == {"signal": False}
    assert policy.load(tmp_path / "autonomy.json")["files"] == "ask"            # the levels survive
    assert _act(win, host, "app_look", app="Banking app", look=False)["ok"]
    assert settings_io.blocked("apps", privacy) == ["Banking app"]
    assert _act(win, host, "app_look", app="banking APP", look=True)["ok"]
    assert settings_io.blocked("apps", privacy) == []


def test_accent_motion_and_voice_are_saved_where_the_tray_reads_them(tmp_path, monkeypatch):
    from yuki.ui import ui_settings
    from yuki.ui.window import settings_io

    voice = tmp_path / "voice.json"
    monkeypatch.setattr(settings_io, "voice_prefs_path", lambda: voice)
    win, host = _window(tmp_path, monkeypatch)
    assert _act(win, host, "accent", accent="orange")["ok"] and _act(win, host, "motion", reduce=True)["ok"]
    saved = ui_settings.load(tmp_path / "ui.json")
    assert saved.accent == "orange" and saved.reduce_motion is True
    assert _act(win, host, "accent", accent="pink")["ok"] is False
    r = _act(win, host, "voice", end_tone=False, tts_provider="deepgram", mic="USB mic")
    assert r["ok"] and r["offline"] is True
    assert json.loads(voice.read_text(encoding="utf-8")) == {"end_tone": False, "tts_provider": "deepgram", "mic_device": "USB mic"}


# -- the engine setting and the fall back to the Qt window ----------------------------------------------------------


def test_the_engine_setting_defaults_to_the_web_window(tmp_path):
    from yuki.ui import ui_settings

    assert ui_settings.load(tmp_path / "none.json").window_engine == "web"
    p = tmp_path / "ui.json"
    p.write_text(json.dumps({"window_engine": "qt", "accent": "orange"}), encoding="utf-8")
    assert ui_settings.load(p).window_engine == "qt"
    p.write_text(json.dumps({"window_engine": "electron"}), encoding="utf-8")
    assert ui_settings.load(p).window_engine == "web"
    assert ui_settings.update(p, window_engine="qt", web_window=[10, 20, 1440, 900]).web_window == [10, 20, 1440, 900]


def test_the_tray_opens_the_web_window_and_falls_back_to_qt(tmp_path, monkeypatch):
    from yuki.ui import ui_settings, window_host

    launched: list[list[str]] = []

    class _Popen:
        def __init__(self, cmd, **kw):
            launched.append(cmd)
            self.pid = 7

        def poll(self):
            return 0

    host = SimpleNamespace(proc=None, server=None, start=lambda: None, _log=lambda *a, **k: None)
    monkeypatch.setattr(window_host.subprocess, "Popen", _Popen)
    monkeypatch.setattr(window_host, "_allow_foreground", lambda pid: None)
    ui = tmp_path / "ui.json"
    monkeypatch.setattr(ui_settings, "settings_path", lambda: ui)
    monkeypatch.setattr(window_host.WindowHost, "_web_ok", None)
    monkeypatch.setattr(window_host, "web_runtime_available", lambda: True)
    open_window = window_host.WindowHost.open_window
    host.window_engine = lambda: window_host.WindowHost.window_engine(host)
    assert open_window(host, "me") and launched[-1][-4:] == ["-m", "yuki.ui.web_window", "--page", "me"]
    ui.write_text(json.dumps({"window_engine": "qt"}), encoding="utf-8")
    assert open_window(host) and launched[-1][-2:] == ["-m", "yuki.ui.window"]
    ui.write_text(json.dumps({"window_engine": "web"}), encoding="utf-8")
    monkeypatch.setattr(window_host.WindowHost, "_web_ok", None)
    monkeypatch.setattr(window_host, "web_runtime_available", lambda: False)   # no WebView2 runtime on this PC
    assert open_window(host) and launched[-1][-2:] == ["-m", "yuki.ui.window"]


def test_the_web_window_opens_the_qt_window_itself_when_webview2_is_missing(monkeypatch):
    calls: list = []
    monkeypatch.setattr(web_window, "single_instance", lambda: 99)
    monkeypatch.setattr(web_window, "release_instance", lambda h: calls.append(("released", h)))
    monkeypatch.setattr(webview2, "runtime_version", lambda loader=None: None)
    monkeypatch.setattr(web_window, "run_qt_window", lambda page, db: calls.append(("qt", page)) or 0)
    assert web_window.main(["--page", "yuki"]) == 0
    assert calls == [("released", 99), ("qt", "playbook")]
    assert web_window.main(["--page", "me", "--no-fallback"]) == 3


def test_pages_from_the_agents_tool_map_to_the_new_pages():
    assert web_window.page_name("today") == ("home", False) and web_window.page_name("chat") == ("home", True)
    assert web_window.page_name("yuki") == ("playbook", False) and web_window.page_name("status") == ("activity", False)
    assert web_window.page_name("settings") == ("settings", False) and web_window.page_name("nope") == ("home", False)
    from yuki.memory.store import default_db_path

    assert web_window.default_db_path() == default_db_path()   # the same rule, without importing the store


# -- an offer's button and its Undo, through the nudge controller ---------------------------------------------------


class _Nudges:
    """NudgeController's offer surface: accept_offer / undo_offer run memory off the GUI thread and keep the answer
    on the card's one dict (``_offer_result`` / ``_undo_result``), as the real controller's _on_acted / _on_undone."""

    def __init__(self) -> None:
        self.delivered: dict[str, dict] = {}
        self.showing, self.queue = None, []
        self.memory_calls: list[tuple] = []

    def record_delivery(self, nudge, via, *, seen_at=None):
        return self.delivered.setdefault(str(nudge["id"]), dict(nudge))

    def accept_offer(self, nudge, via="card"):
        stored = self.record_delivery(nudge, via)
        if stored.get("_offer_state"):
            return None
        stored["_offer_state"] = "running"

        def work():
            self.memory_calls.append(("offer_act", stored["id"]))
            stored.update(_offer_state="done", outcome="done",
                          _offer_result={"ok": True, "do": "todo_add", "state": "done", "message": "Added to your list.", "undo": True})
        t = threading.Thread(target=work)
        t.start()
        return t

    def undo_offer(self, nudge):
        stored = self.record_delivery(nudge, "window")
        if stored.get("_offer_state") != "done":
            return None

        def work():
            self.memory_calls.append(("offer_undo", stored["id"]))
            stored.update(_offer_state="undone", outcome="undone", _undo_result={"ok": True, "state": "undone"})
        t = threading.Thread(target=work)
        t.start()
        return t


def _tray_host(tmp_path):
    from PySide6.QtCore import QCoreApplication

    QCoreApplication.instance() or QCoreApplication([])
    from yuki.ui.window_host import WindowHost

    card = {"id": 41, "kind": "notice", "trigger": "thread", "text": "Next: the watering schedule.",
            "offer": {"do": "todo_add", "label": "Add to list", "steps": ["Add it"], "wont": "Nothing else."}}
    nudges = _Nudges()
    nudges.delivered["41"] = dict(card)
    sig = SimpleNamespace(connect=lambda fn: None)
    runtime = SimpleNamespace(**{n: sig for n in ("started", "tool_called", "tool_finished", "said", "asked", "finished",
                                                  "failed", "summarized", "handed_off")}, memory=None)
    voice = SimpleNamespace(**{n: sig for n in ("chat_event", "state_changed", "conversation_changed", "wake_changed",
                                                "devices_changed")})
    ui = SimpleNamespace(runtime=runtime, voice=voice, nudges=nudges, ui_log=SimpleNamespace(event=lambda *a, **k: None))
    host = WindowHost(ui)
    host._on_gui = lambda fn: fn()   # the GUI-thread hop, inline (no event loop in this test)
    return host, nudges


def test_the_offer_button_and_undo_run_through_the_nudge_controller(tmp_path):
    host, nudges = _tray_host(tmp_path)
    r = host._handle({"op": "offer", "card": 41})
    assert r == {"result": {"ok": True, "do": "todo_add", "state": "done", "message": "Added to your list.", "undo": True}}
    again = host._handle({"op": "offer", "card": 41})                         # a second press runs nothing
    assert again["result"]["ok"] is False and again["result"]["why"] == "already"
    undo = host._handle({"op": "offer_undo", "card": 41})
    assert undo == {"result": {"ok": True, "state": "undone"}}
    assert host._handle({"op": "offer_undo", "card": 41})["result"]["ok"] is False
    assert nudges.memory_calls == [("offer_act", 41), ("offer_undo", 41)]
    assert host._handle({"op": "offer", "card": 999})["ok"] is False
    assert host._handle({"op": "offer"})["ok"] is False


def test_the_controller_keeps_what_the_press_did_on_the_cards_dict():
    from yuki.ui.nudges import NudgeController

    sig = SimpleNamespace(emit=lambda *a: None)
    card = SimpleNamespace(isVisible=lambda: False, nudge=None)
    stub = SimpleNamespace(ui_log=SimpleNamespace(event=lambda *a, **k: None), ask_requested=sig, offer_state=sig,
                           card=card, _show_result=lambda *a: None)
    stored = {"id": 5, "offer": {"do": "todo_add", "label": "Add to list"}}
    NudgeController._on_acted(stub, stored, {"ok": True, "do": "todo_add", "state": "done", "message": "Added to your list.",
                                             "undo": True, "todo": "todo:3"}, "window")
    assert stored["_offer_state"] == "done" and stored["_outcome_at"] > 0
    assert stored["_offer_result"] == {"ok": True, "do": "todo_add", "state": "done", "message": "Added to your list.",
                                       "undo": True}
    NudgeController._on_undone(stub, stored, {"ok": True, "state": "undone"})
    assert stored["_undo_result"] == {"ok": True, "state": "undone"} and stored["outcome"] == "undone"


def test_an_offer_card_shows_will_wont_its_button_and_the_undo_window():
    now = time.time()
    n = {"id": 41, "kind": "notice", "trigger": "thread", "text": "Next: the watering schedule.",
         "shown_at": "2026-10-03T21:27:00",
         "offer": {"do": "todo_add", "label": " Add  to list", "steps": ["Add “Write the schedule”", " due Mon "],
                   "wont": "Won't open anything.", "due": "2026-10-05"}}
    c = web_data.offer_card(n, now)
    assert c["label"] == "Add to list" and c["will"] == "Add “Write the schedule”; due Mon" and c["wont"] == "Won't open anything."
    assert c["open"] and c["undo"] and c["state"] is None and c["title"] == "Your work" and ["due", "2026-10-05"] in c["chips"]
    done = web_data.offer_card({**n, "outcome": "done", "outcome_at": now - 60}, now)
    assert not done["open"] and done["state"] == "done" and abs(done["undo_until"] - (now - 60 + 600)) < 1
    assert web_data.offer_card({**n, "offer": {**n["offer"], "do": "draft"}, "outcome": "done", "outcome_at": now}, now)["undo_until"] is None
    assert not web_data.offer_card({**n, "reaction": "less"}, now)["open"]


# -- the page builders over stand-in memories --------------------------------------------------------------------------


class _Memory:
    """A stand-in MemoryClient with invented data (only the methods the builders call)."""

    def __init__(self, *, index: bool = True) -> None:
        self.now = time.time()
        if index:
            self.people_list = lambda limit=40: [
                {"ref": "person:2", "name": "Riya", "relation": "friend", "aliases": ["riyaclimbs"], "usual": "Signal DM",
                 "last_contact": self.now - 600, "routes": [
                     {"label": "Riya", "app": "Signal", "kind": "dm", "mine": 78, "theirs": 47, "last_mine": self.now - 600,
                      "last_theirs": self.now - 700, "preferred": True},
                     {"label": "Crew", "app": "Telegram", "kind": "group", "mine": 2, "theirs": 9, "last_mine": None,
                      "last_theirs": self.now - 90000, "preferred": False}]}]
            self.contact = lambda ref, near=None: {"found": True, "name": "Riya", "facts": [{"text": "Climbs on Tuesdays."}, "Tests the app."],
                                                   "routes": [{"label": "Riya", "app": "Signal", "mine": 78, "theirs": 47,
                                                               "samples": ["the user's own private message"]}]}

    def status(self):
        return {"paused": False, "service_running": True, "portrait_updated_at": None}

    def call_status(self):
        return {"call": {"app": "Signal", "status": "recording", "session_started_at": self.now - 120}, "recording": True}

    def questions(self, statuses=None, limit=50):
        return [{"id": "question:4", "question": "Is the call at 10?", "evidence": "It moved once.", "status": "open",
                 "at": "2026-10-02T12:19:00", "asked_at": None}]

    def call_audio_waiting(self):
        return {"calls": 2, "minutes": 94.0}

    def recent_nudges(self, hours=12):
        offer = {"do": "todo_add", "label": "Add to list", "steps": ["Add it"], "wont": "Nothing else."}
        return [{"id": 41, "kind": "notice", "trigger": "thread", "text": "Next: the schedule.", "shown_at": self.now - 300,
                 "reaction": "shown", "offer": offer, "reason": "you stopped mid-way"},
                {"id": 39, "kind": "notice", "trigger": "thread", "text": "Order seed trays.", "shown_at": self.now - 900,
                 "reaction": "accepted", "outcome": "done", "outcome_at": self.now - 400, "offer": offer}]

    def day_timeline(self, since, until):
        return [{"start": self.now - 1500, "end": self.now - 60, "state": "present", "app": "VS Code", "process": "code",
                 "title": "beds.ts", "active_s": 1300, "passive_s": 100, "fullscreen": False, "meeting": False, "withheld": False}]

    def portrait_facts_with_evidence(self, evidence_per_fact=6):
        return [{"id": 22, "ref": "fact:22", "kind": "person", "subject": "Dev", "text": "Dev is the user's brother.",
                 "summary": "Your brother", "title": None, "aspect": None, "origin": "user", "confidence": 0.9,
                 "since": self.now - 86400, "evidence": []}]

    def journal_entries(self, since, until=None):
        return []

    def todos(self, include_done=False):
        return []

    def rules(self):
        return [{"ref": "rule:4", "id": 4, "kind": "preference", "text": "Keep it short.", "quote": "", "at": self.now - 86400 * 3,
                 "origin": "user"}]

    def activity(self, since=None, until=None, group_by="site", limit=15):
        return {"totals": {"present_s": 3600}, "items": [{"label": "VS Code", "present_s": 2400, "visits": 9},
                                                         {"label": "Signal", "present_s": 20, "visits": 1}]}

    def calls_in(self, since=None, until=None, limit=30):
        return [{"app": "Signal", "start": self.now - 7200, "end": self.now - 3600, "ongoing": False, "seconds": 3600,
                 "speech_s": 900, "recorded": True, "gists": [{"call_id": 1, "gist": "Planned the meetup."}],
                 "people": [{"name": "Noor", "how": "on screen"}]}]


def _csv(tmp_path: Path) -> Path:
    import csv
    import io
    from datetime import datetime

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["timestamp", "session", "lane", "request", "wall_s", "tool_calls", "cost_usd", "outcome"])
    w.writerow([datetime.fromtimestamp(time.time() - 1200).isoformat(), "s", "worker", "open my budget", 3.2, "open_app=1 done=1", 0.03, "final"])
    w.writerow([datetime.fromtimestamp(time.time() - 600).isoformat(), "s", "worker", "pause it", 12, "click=2", 0.06, "error"])
    p = tmp_path / "requests.csv"
    p.write_text(buf.getvalue(), encoding="utf-8")
    return p


def test_home_is_the_inbox_built_from_memory(tmp_path):
    m = _Memory()
    d = web_data.build_home(m, requests_csv=_csv(tmp_path), people=[{"name": "Riya"}])
    from datetime import datetime

    label = web_data.day_words(datetime.fromisoformat("2026-10-02T12:19:00").timestamp())
    assert d["needs"] == [{"kind": "question", "ref": "question:4", "at_label": label, "title": "Is the call at 10?",
                           "why": "It moved once."}]
    assert [o["card"] for o in d["offers"]] == ["41"] and d["offers"][0]["label"] == "Add to list"
    assert d["now"]["front"]["app"] == "VS Code" and d["now"]["call"]["recording"] is True
    assert [r["kind"] for r in d["running"]] == ["call", "waiting"] and "2 calls" in d["running"][1]["title"]
    kinds = {a["kind"]: a for a in d["away"]}
    assert kinds["card"]["changed"][0]["undo"]["card"] == "39"                   # ran 7 min ago: Undo still open
    titles = [a["title"] for a in d["away"] if a["kind"] == "request"]
    assert "“pause it”" in titles and "“open my budget”" in titles
    assert d["people"] == [{"name": "Riya"}]


def test_me_uses_the_people_index_and_falls_back_to_the_portrait():
    source, people = web_data.people_now(_Memory())
    assert source == "index" and people[0]["name"] == "Riya" and people[0]["count"] == 136
    assert people[0]["route"] == "Signal" and people[0]["prov"] == "Seen in Signal and Telegram" and "rhythm" not in people[0]
    card = web_data.contact_card(_Memory().contact("person:2"))
    assert card["facts"] == ["Climbs on Tuesdays.", "Tests the app."] and "samples" not in card["routes"][0]
    assert "private message" not in json.dumps(card)                            # the user's messages never reach the page
    source, people = web_data.people_now(_Memory(index=False))
    assert source == "portrait" and people[0]["name"] == "Dev" and people[0]["routes"] == []
    assert people[0]["fact"]["ref"] == "fact:22"
    me = web_data.build_me(_Memory(), pending={"fact:22": {"kind": "correct", "words": "My brother Dev"}})
    assert me["source"] == "index" and me["rules"][0]["prov"].startswith("You told me") and me["pending"]


def test_activity_is_the_ledger_with_why_result_undo_and_calls(tmp_path):
    d = web_data.build_activity(_Memory(), requests_csv=_csv(tmp_path))
    by = {r["what"]: r for r in d["rows"]}
    assert by["“pause it”"]["status"] == "failed" and by["“open my budget”"]["result"] == "done"
    assert by["“open my budget”"]["because"] == "because" and by["“open my budget”"]["cant_undo"] is True
    card = by["Order seed trays."]
    assert card["undo"]["card"] == "39" and card["result"] == "done" and card["where"] == "Card · Your work"
    assert d["seen"] == [{"app": "VS Code", "min": 40.0, "visits": 9}]               # under 30 s: left out
    assert d["calls"][0]["gists"] == ["Planned the meetup."] and d["calls"][0]["people"] == ["Noor"]
    assert d["counts"]["did_today"] >= 1


def test_the_drawer_gets_every_step_with_its_time_and_the_runs():
    from yuki.ui.chat_model import ChatModel

    m = ChatModel()
    m.apply({"type": "user", "job": 3, "text": "Open the forum", "at": 100.0})
    m.apply({"type": "started", "job": 3, "lane": "worker", "at": 100.2})
    m.apply({"type": "tool_called", "job": 3, "name": "open_app", "label": "Opening an app, Firefox", "at": 101.0})
    m.apply({"type": "tool_finished", "job": 3, "name": "open_app", "ok": True, "summary": "ok", "at": 102.0})
    items = [web_data.chat_item(i, now=103.0) for i in m.items]
    assert items[0]["kind"] == "user" and items[1]["steps"][0]["label"] == "Opening an app, Firefox"
    assert items[1]["steps"][0]["ok"] is True and items[1]["running"] and items[1]["work_line"].startswith("working")
    runs = web_data.runs_of(m, now=103.0)
    assert runs == [{"item": items[1]["id"], "jobs": [3], "title": "“Open the forum”", "line": "Step 1 · Opening an app, Firefox",
                     "started_at": 100.0, "status": "running"}]
    m.apply({"type": "finished", "job": 3, "text": "The forum's up.", "at": 104.0})
    assert web_data.runs_of(m) == []


def test_continue_puts_a_plan_from_memorys_own_fields_in_the_ask_bar():
    text = web_window.plan_text({"title": "Garden planner", "state": "Half built.", "open": ["Write the schedule", "Test"]})
    assert text.startswith("Pick up “Garden planner” where I left off") and "Half built." in text and "Next: Write the schedule" in text
    assert web_window.plan_text({"title": "X"}).count(":") == 1


def test_shift_times_moves_only_times():
    d = {"at": 2_000_000_000, "started_at": 2_000_000_001, "count": 2_000_000_002, "nested": [{"until": 2_000_000_003}],
         "since": 5, "label": "x"}
    s = web_data.shift_times(d, 10)
    assert s == {"at": 2_000_000_010, "started_at": 2_000_000_011, "count": 2_000_000_002, "nested": [{"until": 2_000_000_013}],
                 "since": 5, "label": "x"}


# -- a real window (opt-in) ------------------------------------------------------------------------------------------


@pytest.mark.skipif(os.environ.get("YUKI_WEBVIEW2_TEST") != "1", reason="opens a real (off-screen) WebView2 window")
def test_a_real_offscreen_window_paints_the_pages_without_taking_focus(tmp_path):
    import ctypes

    metrics = tmp_path / "m.json"
    before = ctypes.windll.user32.GetForegroundWindow()
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen([sys.executable, "-m", "yuki.ui.web_window", "--fixture", "--offscreen", "--profile",
                             str(tmp_path / "profile"), "--t0", repr(time.time()), "--metrics", str(metrics),
                             "--exit-after", "0.5"], env=env)
    try:
        proc.wait(60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
    m = json.loads(metrics.read_text(encoding="utf-8"))
    assert m["page"]["raf"] and m["page"]["visibility"] == "visible" and all(m["page"]["fonts"])
    assert m["took_focus"] is False and ctypes.windll.user32.GetForegroundWindow() == before
    assert 0 < m["derived"]["first_paint"] < 10
