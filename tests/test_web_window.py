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
    for name in ("Mafex", "Jatin", "Discord", "Eastvale", "Corvana", "VALORANT", "Kian"):
        assert name not in text, name
    # Real contacts' names are checked by digest, so the guard itself does not spell them out (review 34 H7):
    # sha256 of the casefolded word, first 16 hex digits.
    import hashlib

    words = {"".join(ch if ch.isalnum() else " " for ch in text).casefold()}
    words = {w for chunk in words for w in chunk.split()}
    real = {"4a911b18f788b638", "85c13293783e9420", "f4482939d91a358c"}
    assert not {hashlib.sha256(w.encode()).hexdigest()[:16] for w in words} & real


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
    # endless animations only on what exists while something is live (the design's rec dot and step spinner), and
    # paused while a full-screen app covers the window: an idle page never redraws (25-build-E1-E2-E5.md §2)
    endless = [line.split("{")[0].strip() for line in rules.splitlines() if "infinite" in line]
    assert endless == [".recdot", ".spin"], endless
    assert "html.quiet .recdot, html.quiet .spin { animation: none !important; }" in rules
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


def test_the_window_talks_to_the_tray_over_the_authenticated_pipe(tmp_path, monkeypatch):
    """The real pipe (HMAC both ways, the key from memory's own key file): hello, the tray's snapshot as the page's
    chat, an op and its reply, the people op as Me's source."""
    from yuki.ui import window_ipc
    from yuki.ui.chat_model import ChatModel

    cipher = _cipher(tmp_path / "mem")
    address = window_ipc.pipe_name(tmp_path / "mem" / "memory.db")
    tray = ChatModel()
    tray.apply({"type": "user", "job": 5, "text": "Open the forum", "at": 1000.0})
    seen: list[dict] = []

    def handle(req):
        seen.append(req)
        if req["op"] == "hello":
            server.send_to(req["_peer"], {"ev": "snapshot", "snapshot": tray.snapshot(), "state": {"voice_state": "off"}})
            return {"version": window_ipc.VERSION, "ops": list(window_ipc.OPS)}
        if req["op"] == "people":
            return {"available": True, "people": [{"ref": "person:2", "name": "Riya", "routes": []}]}
        if req["op"] == "steer":
            return {"job": req["job"], "delivered": True}
        return {"ok": False, "error": "not in this test"}

    server = window_ipc.WindowServer(window_ipc.auth_key(cipher), address, handle).start()
    try:
        win, host = _window(tmp_path, monkeypatch)
        win.cipher, win.want_connect, win.booted = cipher, True, True
        win._connect()
        assert win.client is not None and win.shell["connected"] is True and "offer" in win.ops
        assert _wait(lambda: host.kinds("chat") and host.kinds("chat")[-1].get("reset"))
        assert host.kinds("chat")[-1]["items"][0]["text"] == "Open the forum"
        r = _act(win, host, "steer", job=5, text="also the other one")
        assert r["ok"] and r["delivered"] is True
        assert win._people() == ("index", [web_data.person_from_index({"ref": "person:2", "name": "Riya", "routes": []})])
        assert [q["op"] for q in seen][:1] == ["hello"] and any(q["op"] == "people" for q in seen)
    finally:
        if win.client is not None:
            win.client.close()
        server.stop()


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
    # "In front" became the current work thread (4 Oct); this stand-in has no threads of work, so no tile
    assert "front" not in d["now"] and d["now"]["thread"] is None and d["now"]["call"]["recording"] is True
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
    assert [f["h"] for f in card["facts"]] == ["Climbs on Tuesdays.", "Tests the app."] and "samples" not in card["routes"][0]
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
    assert by["“open my budget”"]["because"] == "because"
    card = by["Order seed trays."]
    assert card["result"] == "done" and card["where"] == "Card · Your work"
    # no Undo anywhere on the Activity tab (the 3 Oct ask A), and every row belongs to a group
    assert not any("undo" in r or "cant_undo" in r for r in d["rows"])
    assert {r["group"] for r in d["rows"]} == {g["id"] for g in d["groups"]}
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
                     "started_at": 100.0, "status": "running", "step": 1}]   # appPT: the step, no total
    m.apply({"type": "finished", "job": 3, "text": "The forum's up.", "at": 104.0})
    assert web_data.runs_of(m) == []


def test_continue_puts_a_plan_from_memorys_own_fields_in_the_ask_bar():
    # an older memory (no thread_continue): the same concrete shape memory's ask has, never "where I left off" (W1)
    text = web_window.plan_text({"title": "Garden planner", "state": "Half built.", "open": ["Write the schedule", "Test"]})
    assert text == "Help me continue Garden planner. Where it stands: Half built. Next: Write the schedule"
    assert web_window.plan_text({"title": "X"}) == "Help me continue X."
    assert "left off" not in text


def test_shift_times_moves_only_times():
    d = {"at": 2_000_000_000, "started_at": 2_000_000_001, "count": 2_000_000_002, "nested": [{"until": 2_000_000_003}],
         "since": 5, "label": "x"}
    s = web_data.shift_times(d, 10)
    assert s == {"at": 2_000_000_010, "started_at": 2_000_000_011, "count": 2_000_000_002, "nested": [{"until": 2_000_000_013}],
                 "since": 5, "label": "x"}


# -- live refresh: the open page follows memory (42-window-live-refresh.md) ----------------------------------------


class _Server:
    """WindowServer's broadcast surface, recording what every window would get."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def broadcast(self, event: dict) -> int:
        self.sent.append(json.loads(json.dumps(event)))
        return 1

    def send_to(self, peer, event) -> bool:
        self.sent.append(json.loads(json.dumps(event)))
        return True


def test_the_tray_forwards_nudge_and_call_events_once_per_change(tmp_path):
    host, _nudges = _tray_host(tmp_path)
    host.server = _Server()
    recording = {"call": {"id": 7, "app": "Signal", "status": "recording", "session_started_at": "2026-10-03T10:00:00"},
                 "recording": True, "waiting": {"calls": 0, "minutes": 0}}
    for _ in range(4):                                     # the watcher's polls bring the same status every 10-20 s
        host.call_status(recording)
    host.call_status({**recording, "call": {**recording["call"], "others": 2}})   # nothing the window shows moved
    host.call_status({"call": None, "recording": False, "waiting": {"calls": 1, "minutes": 12.0}})
    calls = [e for e in host.server.sent if e["ev"] == "call"]
    assert [c["status"]["recording"] for c in calls] == [True, False]
    rows = [{"id": 51, "kind": "question", "text": "Invented question?"}]
    for _ in range(3):                                     # NudgeWatcher.arrived on every 60 s poll
        host.cards_arrived(rows, "poll")
    host.cards_arrived([], "poll")
    host.cards_arrived(rows + [{"id": 52, "kind": "notice"}], "event")
    host.card_written("nudge_ack")
    host.card_written("nudge_received")                    # not a write memory made: no refresh
    topics = [(e["topic"], e.get("why")) for e in host.server.sent if e["ev"] == "memory"]
    assert topics == [("cards", "arrived"), ("cards", "arrived"), ("cards", "nudge_ack")]


class _Live:
    """A window with its real worker and refresh clock, over stand-ins: ``build`` counts and returns invented data,
    ``_stamps`` reads ``self.stamps``; ``shown`` plays Windows' "can it be seen" answer."""

    def __init__(self, tmp_path, monkeypatch, *, memory=None, real_build=False, open_page="home") -> None:
        monkeypatch.setattr(web_window, "DEBOUNCE_S", 0.05)
        monkeypatch.setattr(web_window, "MAX_WAIT_S", 0.3)
        monkeypatch.setattr(web_window, "MIN_GAP_S", {})
        monkeypatch.setattr(web_window, "DEFAULT_GAP_S", 0.0)
        self.win, self.host = _window(tmp_path, monkeypatch)
        win = self.win
        win.memory = memory if memory is not None else SimpleNamespace(close=lambda: None)
        win.open_page = open_page
        self.built: list[str] = []
        self.stamp_reads: list[str] = []
        self.stamps: dict[str, str] = {}
        self.version: dict[str, int] = {}
        self.shown = True
        monkeypatch.setattr(win, "window_shown", lambda: self.shown)
        if not real_build:
            def build(name):
                self.built.append(name)
                if name == "shell":
                    return {"memory_line": "memory on", "needs": 0}
                return {"page": name, "v": self.version.get(name, 0)}
            monkeypatch.setattr(win, "build", build)
        else:
            real = win.build
            monkeypatch.setattr(win, "build", lambda name: (self.built.append(name), real(name))[1])
        monkeypatch.setattr(win, "_stamps", lambda name: (self.stamp_reads.append(name), self.stamps.get(name))[1])
        monkeypatch.setattr(win, "_media", lambda: None)
        win.booted = True
        win.ready.set()
        threading.Thread(target=win._worker, daemon=True).start()
        threading.Thread(target=win._scheduler, daemon=True).start()

    def settle(self, s: float = 0.4) -> None:
        time.sleep(s)

    def data(self, page: str | None = None) -> list[dict]:
        return [m for m in self.host.kinds("data") if page is None or m["page"] == page]

    def close(self) -> None:
        self.win.close()


def test_unchanged_page_data_is_not_posted_again(tmp_path, monkeypatch):
    win, host = _window(tmp_path, monkeypatch)
    win.booted = True
    home = {"needs": [], "offers": [], "away": [], "since": 1000.0}
    assert win.send_page("home", home) is True
    assert win.send_page("home", dict(home)) is False                     # the same again: not sent
    assert win.send_page("home", {**home, "since": 1060.0}) is False      # only the moving "since": not sent
    assert win.send_page("home", {**home, "needs": [{"ref": "q:1"}]}) is True
    assert win.send_page("day", {"buckets": [1], "now_min": 600}) is True
    assert win.send_page("day", {"buckets": [1], "now_min": 601}) is False   # the now marker is the page's tick
    assert win.send_page("settings", {"voice": {}, "usage": {"mb": 80}}) is True
    assert win.send_page("settings", {"voice": {}, "usage": {"mb": 81}}) is False
    assert [m["page"] for m in host.kinds("data")] == ["home", "home", "day", "settings"]
    # what the snapshot painted counts as sent: fresh data equal to it is not posted at all
    win2, host2 = _window(tmp_path / "b", monkeypatch)
    win2.pages = {"work": {"threads": [{"ref": "thread:1"}]}}
    win2._snapshot_done.set()
    win2._boot_when_ready()
    assert win2.send_page("work", {"threads": [{"ref": "thread:1"}]}) is False and not host2.kinds("data")


def test_nothing_is_built_or_posted_while_minimised_and_the_open_page_catches_up_once_on_restore(tmp_path, monkeypatch):
    from yuki.ui.chat_model import ChatModel

    live = _Live(tmp_path, monkeypatch)
    win, host = live.win, live.host
    try:
        win.model = ChatModel()
        win.model.subscribe(win._on_chat_change)
        live.shown = False                                                   # minimised
        for i in range(5):
            win.topic("cards")
            win._on_tray_event({"ev": "chat", "event": {"type": "user", "job": 40 + i, "text": "Invented", "at": 1000.0 + i}})
            win._on_tray_event({"ev": "state", "voice_state": "listening"})
        win.send_page("work", {"threads": []})                               # a build finished while hidden
        live.settle()
        assert live.built == [] and not host.kinds("data") and not host.kinds("chat") and not host.kinds("live")
        assert "home" in win.dirty and "shell" in win.dirty
        live.shown = True                                                    # restored: the host's hook
        win.on_shown("restore")
        live.settle()
        assert sorted(live.built) == ["home", "shell"]                       # the open page (and the rail), once
        chat = host.kinds("chat")
        assert len(chat) == 1 and chat[0]["reset"] and len(chat[0]["items"]) == len(win.model.items) > 5
        assert len(host.kinds("live")) >= 1 and [m["page"] for m in live.data()].count("work") == 1
        assert [m["page"] for m in live.data()].count("home") == 1
    finally:
        live.close()


def test_nothing_is_built_while_a_full_screen_app_covers_the_window(tmp_path, monkeypatch):
    monkeypatch.setattr(web_window, "FALLBACK_S", {"home": 0.15})
    live = _Live(tmp_path, monkeypatch)
    win, host = live.win, live.host
    try:
        win._arm_fallback()
        win.set_covered(True)
        assert host.kinds("quiet")[-1] == {"kind": "quiet", "on": True}     # the page stills its field
        win.topic("cards")
        win.on_call_status({"call": {"id": 3, "status": "recording", "app": "Invented"}, "recording": True})
        live.settle(0.6)                                                     # past the fallback too
        assert live.built == [] and live.stamp_reads == [] and not live.data()
        assert not host.kinds("live")                                        # even the rail's chip waits
        win.set_covered(False)
        live.settle()
        assert host.kinds("quiet")[-1]["on"] is False and "home" in live.built and host.kinds("live")
    finally:
        live.close()


def test_a_burst_of_card_events_builds_the_open_page_once(tmp_path, monkeypatch):
    live = _Live(tmp_path, monkeypatch)
    win = live.win
    monkeypatch.setattr(web_window, "DEBOUNCE_S", 0.3)
    monkeypatch.setattr(web_window, "MAX_WAIT_S", 2.0)
    try:
        for _ in range(20):                                                  # 20 events in about a second
            win._on_tray_event({"ev": "memory", "topic": "cards"})
            time.sleep(0.04)
        live.settle(0.8)
        assert live.built.count("home") == 1 and live.built.count("shell") == 1 and "activity" not in live.built
    finally:
        live.close()


def test_a_forwarded_call_status_patches_rail_and_home_without_reading_memory(tmp_path, monkeypatch):
    class _NoCallRead(_Memory):
        reads = 0

        def call_status(self):
            _NoCallRead.reads += 1
            return super().call_status()

        def call_audio_waiting(self):
            _NoCallRead.reads += 1
            return super().call_audio_waiting()

        def close(self):
            pass

    live = _Live(tmp_path, monkeypatch, memory=_NoCallRead(), real_build=True)
    win, host = live.win, live.host
    try:
        win.mark("home", "want", urgent=True)                                # Home on screen (read from memory once)
        live.settle(0.6)
        assert live.built == ["home"]
        _NoCallRead.reads = 0
        status = {"call": {"id": 9, "app": "Signal", "status": "recording", "session_started_at": "2026-10-03T10:00:00"},
                  "recording": True, "waiting": {"calls": 1, "minutes": 30.0}}
        win._on_tray_event({"ev": "call", "status": status})
        assert host.kinds("live")[-1]["shell"]["call"]["recording"] is True   # the rail's chip, at once
        live.settle(0.3)
        home = live.data("home")[-1]["data"]
        assert home["now"]["call"]["app"] == "Signal" and [r["kind"] for r in home["running"]] == ["call", "waiting"]
        assert _NoCallRead.reads == 0 and live.built == ["home"]              # patched from the status: no build, no read
        assert {"day", "activity"} <= set(win.dirty)                          # pages with call rows: built when opened
        n, m = len(host.kinds("live")), len(live.data("home"))
        win._on_tray_event({"ev": "call", "status": status})                  # the same status: nothing new to draw
        live.settle()
        assert len(host.kinds("live")) == n and len(live.data("home")) == m
        # a build later uses the forwarded status too (no call read)
        win.mark("home", "action", urgent=True)
        live.settle(0.6)
        assert live.built == ["home", "home"] and _NoCallRead.reads == 0
    finally:
        live.close()


def test_a_new_card_marks_home_and_activity_dirty_and_builds_only_the_open_page(tmp_path, monkeypatch):
    live = _Live(tmp_path, monkeypatch)
    win = live.win
    try:
        win.fresh.update({"home", "activity"})
        win.stamps.update(home=time.time(), activity=time.time())
        win._on_tray_event({"ev": "memory", "topic": "cards", "why": "nudge_ack"})
        live.settle()
        assert "home" in live.built and "activity" not in live.built and "activity" in win.dirty
        win.on_message({"op": "want", "page": "activity"})                   # opened: built because it is dirty
        live.settle()
        assert live.built.count("activity") == 1 and "activity" not in win.dirty
    finally:
        live.close()


def test_a_finished_request_refreshes_the_open_activity_page_and_marks_home_stale(tmp_path, monkeypatch):
    from yuki.ui.chat_model import ChatModel

    live = _Live(tmp_path, monkeypatch, open_page="activity")
    win = live.win
    try:
        win.model = ChatModel()
        for ev in ({"type": "user", "job": 5, "text": "Invented request", "at": 1000.0},
                   {"type": "started", "job": 5, "lane": "worker", "at": 1000.1},
                   {"type": "finished", "job": 5, "text": "Done.", "at": 1002.0}):
            win._on_tray_event({"ev": "chat", "event": ev})
        live.settle()
        assert live.built == ["activity"] and "home" in win.dirty
        assert {"work", "playbook"} <= set(win.maybe)                         # a run may have moved these: stamps later
        win._on_tray_event({"ev": "chat", "event": {"type": "tool_finished", "job": 5, "name": "remember_rule", "ok": True}})
        assert {"me", "playbook"} <= set(win.dirty)
    finally:
        live.close()


def test_every_memory_tool_the_agent_has_is_in_the_topic_table():
    from yuki.agent.tools import MEMORY_TOOLS

    assert set(web_window.TOOL_TOPICS) == set(MEMORY_TOOLS) - {"memory"}     # a renamed tool fails here
    for topic in web_window.TOOL_TOPICS.values():
        assert topic in web_window.TOPIC_PAGES or topic in web_window.CHECK_TOPICS


def test_want_rebuilds_a_dirty_page_inside_the_45s_window(tmp_path, monkeypatch):
    live = _Live(tmp_path, monkeypatch, open_page="home")
    win = live.win
    try:
        win.fresh.add("me")
        win.stamps["me"] = time.time()                                       # built 0 s ago
        win.on_message({"op": "want", "page": "me"})
        live.settle()
        assert "me" not in live.built and live.stamp_reads == []             # fresh and clean: nothing
        win.open_page = "home"
        win.topic("rules")                                                   # a rule changed while Home was open
        win.on_message({"op": "want", "page": "me"})
        live.settle()
        assert live.built.count("me") == 1
    finally:
        live.close()


def test_the_fallback_reads_only_stamps_and_builds_only_when_a_stamp_moved(tmp_path, monkeypatch):
    monkeypatch.setattr(web_window, "FALLBACK_S", {"day": 0.2})
    live = _Live(tmp_path, monkeypatch, open_page="day")
    win = live.win
    try:
        live.stamps["day"] = "s1"
        win.fresh.add("day")
        win.seen_stamps["day"] = "s1"
        win._arm_fallback()
        live.settle(0.7)
        assert live.stamp_reads.count("day") >= 2 and live.built == []       # stamps read, nothing moved: no build
        live.stamps["day"] = "s2"                                            # the timeline moved
        live.settle(0.5)
        assert live.built == ["day"]
        win.client, win.call_status = object(), {"recording": True}          # a call records: no fallback reads
        reads = len(live.stamp_reads)
        win._arm_fallback()
        live.settle(0.6)
        assert len(live.stamp_reads) == reads
    finally:
        win.client = None
        live.close()


def test_a_restore_with_the_tray_down_makes_one_connection_not_three(tmp_path, monkeypatch):
    """Review F1: on_shown, the page's shown want and the woken scheduler each used to connect."""
    live = _Live(tmp_path, monkeypatch, open_page="home")
    win = live.win
    attempts: list[str] = []

    def connect_once():
        attempts.append(threading.current_thread().name)
        time.sleep(0.05)
        win.client = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(win, "_connect_once", connect_once)
    try:
        win._scheduler_on = True
        live.shown = False                                                   # minimised while the tray restarted
        win.cipher, win.want_connect = object(), True
        live.settle(0.2)
        assert attempts == []                                                # nothing while hidden
        live.shown = True
        win.on_shown("restore")
        win.on_message({"op": "want", "page": "home", "shown": True})
        live.settle(0.4)
        assert len(attempts) == 1, attempts
        # an older connection's goodbye arriving late leaves the current one alone
        current = win.client
        win._on_tray_closed(SimpleNamespace())
        assert win.client is current
    finally:
        win.client = None
        live.close()


def test_the_tray_going_away_mid_call_clears_the_chip_and_rereads_from_memory(tmp_path, monkeypatch):
    """Review F2: a stale 'recording' chip stayed for good and switched the fallback off."""
    live = _Live(tmp_path, monkeypatch, open_page="home")
    win = live.win
    try:
        tray = SimpleNamespace(close=lambda: None)
        win.client = tray
        win.on_call_status({"call": {"id": 4, "status": "recording", "app": "Invented"}, "recording": True})
        assert win.shell["call"]["recording"] is True and win._recording() is True
        live.settle()
        live.built.clear()
        win._on_tray_closed(tray)
        assert win.shell["call"] is None and win.client is None and win._recording() is False
        assert live.host.kinds("live")[-1]["shell"]["call"] is None             # the chip goes at once
        live.settle(0.5)
        assert "shell" in live.built and "home" in live.built                   # read from memory again
    finally:
        live.close()


def test_a_failed_build_of_the_open_page_keeps_its_fallback(tmp_path, monkeypatch):
    """Review F7: one exception used to switch the open page's fallback off until you navigated away."""
    monkeypatch.setattr(web_window, "FALLBACK_S", {"day": 0.2})
    live = _Live(tmp_path, monkeypatch, open_page="day")
    win = live.win
    tries: list[str] = []

    def build(name):
        tries.append(name)
        raise RuntimeError("memory busy (invented)")
    monkeypatch.setattr(win, "build", build)
    try:
        win._arm_fallback()
        live.settle(0.9)
        assert tries.count("day") >= 2, tries                                 # tried again on the next fallback
    finally:
        live.close()


def test_a_build_you_asked_for_sends_the_volatile_fields_events_do_not(tmp_path, monkeypatch):
    """Settings' 'Yuki right now' and Home's 'since' stay out of the hash for events and the fallback, but a revisit
    (or your own action) shows them current."""
    win, host = _window(tmp_path, monkeypatch)
    win.booted = True
    assert win.send_page("settings", {"voice": {}, "usage": {"mb": 80}}) is True
    assert win.send_page("settings", {"voice": {}, "usage": {"mb": 81}}) is False             # an event: not sent
    assert win.send_page("settings", {"voice": {}, "usage": {"mb": 82}}, asked=True) is True  # you opened it
    assert win.send_page("settings", {"voice": {}, "usage": {"mb": 82}}, asked=True) is False # exactly the same
    assert win.send_page("home", {"needs": [], "since": 1.0}) is True
    assert win.send_page("home", {"needs": [], "since": 2.0}, asked=True) is True
    assert [m["data"].get("usage", {}).get("mb") for m in host.kinds("data") if m["page"] == "settings"] == [80, 82]


def test_tray_focus_refreshes_the_open_page_not_home(tmp_path, monkeypatch):
    win, host = _window(tmp_path, monkeypatch)
    win.memory, win.booted = object(), True
    marked: list[tuple] = []
    monkeypatch.setattr(win, "mark", lambda name, reason, **kw: marked.append((name, reason, kw.get("check", False))))
    win.on_message({"op": "want", "page": "playbook"})
    assert win.open_page == "playbook" and marked[-1] == ("playbook", "want", False)   # never built yet
    win.fresh.add("playbook")
    win.stamps["playbook"] = time.time() - 30
    marked.clear()
    win._on_tray_event({"ev": "focus"})
    assert marked == [("playbook", "action", False)]                       # not Home
    assert host.kinds("focus")[-1] == {"kind": "focus"}
    marked.clear()
    win.come_forward()                                                       # a second start: the same
    assert [m[0] for m in marked] == ["playbook"]
    marked.clear()
    win.on_message({"op": "want", "page": "playbook", "again": True})        # the same tab clicked again: a check
    assert marked == [("playbook", "want", True)]
    marked.clear()
    win._on_tray_event({"ev": "focus", "page": "me"})                        # another page: it asks itself (want)
    assert marked == [] and host.kinds("focus")[-1]["page"] == "me"


def test_settings_follows_its_own_controls_and_the_trays_voice_state(tmp_path, monkeypatch):
    live = _Live(tmp_path, monkeypatch, open_page="settings")
    win, host = live.win, live.host
    try:
        r = _act(win, host, "motion", reduce=True)                           # writes the test's own ui.json
        assert r["ok"]
        live.settle()
        assert live.built.count("settings") == 1
        win._on_tray_event({"ev": "state", "voice_state": "listening"})      # not shown on Settings: nothing
        win._on_tray_event({"ev": "state", "voice_state": "thinking"})
        live.settle()
        assert live.built.count("settings") == 1
        win._on_tray_event({"ev": "state", "mic": "Invented USB mic"})        # shown there: rebuilt once
        live.settle()
        assert live.built.count("settings") == 2
    finally:
        live.close()


def test_pages_that_change_with_the_clock_are_rebuilt_at_the_next_boundary(tmp_path, monkeypatch):
    now = time.time()
    data = {"rows": [{"undo": {"until": now + 120}}, {"undo": {"until": now - 5}}], "offers": [{"undo_until": now + 60}]}
    assert abs(web_window.next_boundary(data, now) - (now + 60)) < 1e-6
    from datetime import datetime, timedelta

    midnight = (datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()
    assert abs(web_window.next_boundary({"rows": []}, now) - (midnight + 1)) < 1e-6
    live = _Live(tmp_path, monkeypatch, open_page="activity")
    win = live.win
    try:
        live.version["activity"] = 1
        win.mark("activity", "want", urgent=True)
        live.settle(0.2)
        assert live.built == ["activity"]
        win._clock_at["activity"] = time.time() + 0.3                         # an Undo window closes then
        with win._cv:
            win._cv.notify()
        live.settle(0.7)
        assert live.built == ["activity", "activity"]
        assert any(e.get("reason") == "clock" for e in win.events if e["kind"] == "web_page")
    finally:
        live.close()


def test_a_build_carries_when_it_started_and_media_never_lands_on_an_older_home(tmp_path, monkeypatch):
    live = _Live(tmp_path, monkeypatch)
    win = live.win
    try:
        live.version["home"] = 7
        win.mark("home", "want", urgent=True)
        live.settle(0.3)
        first = live.data("home")[-1]
        assert first["built_at"] <= time.time() and first["data"]["v"] == 7
        win._jobs.put(("media", "home", {"status": "playing", "title": "Invented song"}, ()))
        live.settle(0.2)
        last = live.data("home")[-1]["data"]
        assert last["v"] == 7 and last["now"]["playing"]["title"] == "Invented song" and win.playing["title"] == "Invented song"
    finally:
        live.close()


def test_page_refreshes_are_logged_content_free(tmp_path, monkeypatch):
    live = _Live(tmp_path, monkeypatch)
    win = live.win
    try:
        live.version["home"] = 3
        win.topic("cards")
        live.settle()
        live.shown = False
        win.send_page("home", {"page": "home", "v": 99, "secret": "Invented private words"})
        live.shown = True
        win.catch_up("restore")
        live.settle(0.2)
    finally:
        live.close()
    lines = [json.loads(x) for x in (tmp_path / "window-refresh.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines[0]["type"] == "session_start" and all(x["type"] == "ui" for x in lines[1:])   # UiLog's writer and shape
    pages = [x for x in lines if x.get("event") == "web_page"]
    assert pages and {"page", "reason", "topic", "ms", "result"} <= set(pages[0]) and pages[0]["topic"] == "cards"
    assert {x.get("event") for x in lines} >= {"web_page", "web_hold", "web_restore"}
    text = json.dumps(lines)
    assert "Invented private words" not in text and '"v"' not in text


def test_page_stamps_move_on_each_write_and_stay_cheap(tmp_path):
    from yuki.memory.api import MemoryClient, _STAMP_SQL, change_stamps

    m = MemoryClient.open(tmp_path / "memory.db")
    try:
        conn = m.store._conn

        def insert(table: str) -> None:
            cols = [c for c in m.store._query(f"PRAGMA table_info({table})") if c[3] and c[4] is None and not c[5]]
            values = [1.0 if str(c[2]).upper() in ("REAL", "INTEGER") else "x" for c in cols]
            m.store._write(lambda c: c.execute(f"INSERT INTO {table} ({', '.join(x[1] for x in cols)}) VALUES "
                                               f"({', '.join('?' for _ in cols)})", values))

        before = change_stamps(m, list(_STAMP_SQL))
        assert all(v is not None for v in before.values()), before
        for table in _STAMP_SQL:
            prev = change_stamps(m, [table])[table]
            insert(table)
            assert change_stamps(m, [table])[table] != prev, table
            cols = {c[1] for c in m.store._query(f"PRAGMA table_info({table})")}
            if "updated_at" in cols:                                         # an edit, no new row, moves it too
                prev = change_stamps(m, [table])[table]
                m.store._write(lambda c: c.execute(f"UPDATE {table} SET updated_at = updated_at + 5"))
                assert change_stamps(m, [table])[table] != prev, table
        for page, tables in web_data.PAGE_TABLES.items():
            assert set(tables) <= set(_STAMP_SQL), page
            t = time.perf_counter()
            for _ in range(20):
                assert web_data.page_stamps(m, page) is not None
            assert (time.perf_counter() - t) / 20 < 0.005, page               # under 5 ms a check
        assert web_data.page_stamps(m, "settings") is None                    # no stamps: built (and hash-skipped)
        # review F3: Work's week figure and Activity's "Seen today" read the timeline, so it moves their stamps
        work0, act0 = web_data.page_stamps(m, "work"), web_data.page_stamps(m, "activity")
        insert("timeline")
        assert web_data.page_stamps(m, "work") != work0 and web_data.page_stamps(m, "activity") != act0
        del conn
    finally:
        m.close()


def test_the_host_says_when_the_window_is_shown_again_once_per_restore(monkeypatch):
    calls: list[str] = []
    opts = webview2.HostOptions(folders={}, start_url=f"https://{webview2.APP_HOST}/index.html", user_data_dir=Path("."),
                                on_shown=calls.append)
    host = webview2.WebView2Host(opts)

    class _Ctl:
        def __bool__(self):
            return True

        def put_bool(self, *a):
            pass

        def call(self, *a):
            pass
    host._controller = _Ctl()
    monkeypatch.setattr(host, "set_memory_low", lambda low: True)
    host._fit()                                                              # a resize: nothing
    host._fit(minimized=True)
    host._fit()                                                              # restored: once
    host._fit()
    assert calls == ["restore"]
    host._proc(0, webview2.WM_ACTIVATE, 1, 0)                                # brought to the front
    host._proc(0, webview2.WM_ACTIVATE, 0, 0)                                # deactivated: nothing
    host._proc(0, webview2.WM_ACTIVATE, 1 | (1 << 16), 0)                    # activated while minimised: nothing
    assert calls == ["restore", "activate"]


def test_overlay_card_inserts_reach_the_window_thread():
    """The overlay in thread mode adds a card through WindowHost.emit (applied and sent to the open window), not
    straight into the model (42 R2). The overlay's method over a stand-in self: no widget, no QApplication."""
    from yuki.ui.chat_model import ChatModel
    from yuki.ui.overlay import Overlay

    model, sent = ChatModel(), []

    def emit(ev):                                                            # WindowHost.emit: apply, then broadcast
        model.apply(ev)
        sent.append(ev["type"])
    me = SimpleNamespace(thread=object(), nudge_card=lambda _id: None, scroll_to_latest=lambda: None, _thread_emit=emit,
                         _proxy=lambda tone, nudge=None, at=None: ("proxy", tone))
    out = Overlay.add_nudge_card(me, {"id": 61, "kind": "notice", "text": "Invented card."}, "Card", at=time.time())
    assert out == ("proxy", "nudge") and sent == ["nudge"] and any(i.kind == "nudge" for i in model.items)
    # the wiring: the tray passes WindowHost.emit to the overlay
    app = (ROOT / "yuki" / "ui" / "app.py").read_text(encoding="utf-8")
    assert "self.overlay.attach_thread(self.window_host.model, emit=self.window_host.emit)" in app


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_live_patch_keeps_scroll_focus_and_figures_in_headless_chrome():
    """scripts/check_live_patch.js: the real index.html in Playwright's headless shell (never a visible window, a
    throwaway profile), the invented fixture; new data pushed into each open page."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import web_shots

        exe = web_shots.headless_shell()
    except (SystemExit, ImportError):
        pytest.skip("chrome-headless-shell is not installed")
    r = subprocess.run(["node", str(ROOT / "scripts" / "check_live_patch.js"), "--chrome", str(exe)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]


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
