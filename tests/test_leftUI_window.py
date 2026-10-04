"""The window's leftovers of 4 Oct (docs/research/revamp-2026-10-02/69-build-leftUI.md): Settings' takeover distance
(saved through the window, read by the takeover watch as a task starts) and Home's Your list following Work's split
(a promise you made or someone owing you never shows as your to-do on Home).

Offline: temp files, a temp database, stubs and invented data only; no network, no tray, no real memory, nothing on
screen. The page side is checked in a headless browser by scripts/check_live_patch.js (TK1, H1, T1-T4, N2).
"""

from __future__ import annotations

import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

from yuki.memory.api import MemoryClient
from yuki.memory.store import Store
from yuki.ui import web_data as W
from yuki.ui import web_window

NOW = datetime(2026, 10, 4, 15, 0).timestamp()
M, H, D = 60.0, 3600.0, 86400.0


def _wait(pred, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class _Host:
    def __init__(self) -> None:
        self.posted: list[dict] = []

    def post(self, msg: dict) -> None:
        self.posted.append(msg)

    def kinds(self, kind: str) -> list[dict]:
        return [m for m in self.posted if m.get("kind") == kind]


def _act(win, host, action: str, **fields) -> dict:
    n = len(host.kinds("reply"))
    win.on_message({"op": "act", "id": f"{action}-{n}", "action": action, **fields})
    assert _wait(lambda: len(host.kinds("reply")) > n)
    return host.kinds("reply")[-1]


# -- 1. Settings: the takeover distance ----------------------------------------------------------------------------------


def test_settings_shows_the_takeover_distance_in_plain_words(tmp_path):
    from yuki.ui import ui_settings

    v = W.takeover_view(40)
    assert v["px"] == 40 and v["label"] == ui_settings.TAKEOVER_MOVE_LABEL and v["hint"]
    assert [c["px"] for c in v["choices"]] == list(ui_settings.TAKEOVER_MOVE_CHOICES)
    assert all(c["words"] and not any(ch.isdigit() for ch in c["words"]) for c in v["choices"])   # words, no numbers
    own = W.takeover_view(50)                          # set by hand in ui.json: shown as itself, in its place
    assert [c["px"] for c in own["choices"]] == [24, 40, 50, 64, 100, 160]
    assert next(c for c in own["choices"] if c["px"] == 50)["words"] == ui_settings.TAKEOVER_MOVE_OWN
    path = tmp_path / "ui.json"
    ui_settings.update(path, takeover_move_px=100)
    class _Bare:   # a memory that can say nothing (each read falls back)
        status = activity = call_prefs = None

    d = W.build_settings(_Bare(), policy_path=tmp_path / "autonomy.json", ui_path=path)
    assert d["takeover"]["px"] == 100


def test_a_saved_distance_reaches_the_takeover_watch_without_a_restart(tmp_path, monkeypatch):
    from yuki.actions.leases import TakeoverThread
    from yuki.ui import ui_settings

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))       # the settings file the watch reads, a temp one
    path = ui_settings.settings_path()
    assert path.parent.parent == tmp_path
    win = web_window.WebWindow(db_path=tmp_path / "mem" / "memory.db", ui_path=path, connect=False,
                               policy_path=tmp_path / "autonomy.json")
    monkeypatch.setattr(win, "_open_memory", lambda: None)
    host = _Host()
    win.host = host
    assert TakeoverThread(lambda kind: None).move_px == 40                     # nothing saved: the default
    r = _act(win, host, "takeover_move", px=160)
    assert r["ok"] and r["px"] == 160
    assert ui_settings.load(path).takeover_move_px == 160
    # the next task's watch (made as each task starts acting, as runtime.takeover_watch does) uses it
    assert TakeoverThread(lambda kind: None).move_px == 160
    assert _act(win, host, "takeover_move", px=3)["px"] == 12                 # kept within bounds
    assert TakeoverThread(lambda kind: None).move_px == 12
    assert _act(win, host, "takeover_move", px="far")["ok"] is False          # not a distance: nothing saved
    assert ui_settings.load(path).takeover_move_px == 12
    assert "takeover_move" in win.SETTINGS_ACTIONS                             # the page's data is read again


# -- 2. Home's Your list follows Work's split ----------------------------------------------------------------------------


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds")


def _row(ref, text, source="user", *, due=None, title=None, since=NOW - D, status="open"):
    return {"id": ref, "text": text, "source": source, "due": due, "status": status, "evidence": None,
            "since": _iso(since), "owner": "yuki" if source == "commitment" else "user", "label": source, "title": title}


class _Mem:
    """An invented merged list and judged board (memory's todos() and work_board() shapes)."""

    def __init__(self, *, old_board: bool = False):
        self.old_board = old_board
        self.board_calls: list[dict] = []
        self.rows = [
            _row("todo:1", "Water the invented plants", since=NOW - 2 * H),
            _row("todo:2", "Pay the invented bill", due=_iso(NOW - D)),
            _row("commit:3", "Find the invented manual", "commitment", since=NOW - 3 * H),       # Yuki's own promise
            _row("commit:4", "Send Ola the invented notes", "chat", since=NOW - H),            # you said you'd
            _row("commit:5", "Bring the invented cake", "call", since=NOW - 5 * H),            # you said you'd
            _row("loop:6", "Ola said she would send the invented map", "loop", since=NOW - 4 * H),   # waiting on
            _row("loop:7", "Ola asked for the invented photos", "loop", since=NOW - 6 * H),          # asked of you
            _row("loop:8", "You promised Ben the invented tune", "loop", since=NOW - 7 * H),         # promised
            _row("loop:9", "Not judged yet, invented", "loop", since=NOW - 8 * H),
            _row("commit:10", "Book the invented room", "chat", since=NOW - 9 * H),            # moved to your list
        ]

    def todos(self, include_done=False):
        return [r for r in self.rows if include_done or r["status"] == "open"]

    def work_board(self, now=None, **kw):
        if self.old_board and kw:
            raise TypeError("an older memory: no replies keyword")
        self.board_calls.append(kw)
        return {"loops": {6: {"kind": "waiting", "who": "Ola", "what": "send the invented map"},
                          7: {"kind": "asked", "who": "Ola", "what": "Send Ola the invented photos"},
                          8: {"kind": "promised", "who": "Ben", "what": "Send Ben the invented tune"}},
                "listed": ["commit:10"], "replies": [], "ready": True}

    def work_kept(self, now=None):
        return []

    def end_todo(self, ref, how="done"):
        return ref

    def reopen_todo(self, ref):
        return ref

    def correct_item(self, ref, words):
        return {"ok": True}


def test_home_lists_exactly_works_your_list_in_its_order():
    mem = _Mem()
    home = W.build_home(mem, requests_csv=None, now=NOW, call_status={})["todos"]
    work = W.build_work(mem, week={"days": [], "totals": {}}, now=NOW)
    refs = [t["ref"] for t in home["open"]]
    assert refs == [t["ref"] for t in work["todos"]["open"]]
    assert set(refs) == {"todo:1", "todo:2", "commit:3", "loop:7", "loop:9", "commit:10"}
    # a promise you made and someone owing you are never your to-do on Home
    assert not {"commit:4", "commit:5", "loop:6", "loop:8"} & set(refs)
    assert home["n"] == len(refs) == work["todos"]["n"] and home["shown"] == W.HOME_TODOS == 5
    assert refs[0] == "todo:2"                          # the list's own order: late first
    # Done / Not needed / Correct / Undo stay as they were: the same buttons, Undo only on a to-do you added
    assert home["can"] == {"end": True, "undo": True, "correct": True, "add": False}
    assert [t["undo"] for t in home["open"] if t["ref"].startswith("todo:")] == [True, True]
    assert not any(t["undo"] for t in home["open"] if not t["ref"].startswith("todo:"))
    # Home reads the board without the chats (Work reads them for Needs your reply)
    assert {"replies": False} in mem.board_calls


def test_home_follows_work_with_an_older_memory_or_none_judged():
    old = _Mem(old_board=True)
    assert [t["ref"] for t in W.build_home(old, requests_csv=None, now=NOW, call_status={})["todos"]["open"]] == \
        [t["ref"] for t in W.build_work(old, week={"days": [], "totals": {}}, now=NOW)["todos"]["open"]]

    class _NoBoard(_Mem):
        work_board = None

    bare = _NoBoard()
    home = W.build_home(bare, requests_csv=None, now=NOW, call_status={})["todos"]
    # nothing judged yet: Work keeps your to-dos, Yuki's promises and every loop on Your list, and so does Home
    assert {"commit:4", "commit:5"}.isdisjoint(t["ref"] for t in home["open"])
    assert {"loop:6", "loop:7", "loop:8", "loop:9"} <= {t["ref"] for t in home["open"]}


def test_home_rebuilds_when_the_board_changes():
    assert "work_items" in W.PAGE_TABLES["home"]


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def test_home_over_a_real_memory_leaves_your_promises_to_work(store):
    m = MemoryClient(store)
    m.add_todo("Invented thing")
    store.add_conversation_fact("commitment", "The user will send the invented deck", owner="user", origin="model",
                                quote="I'll send the deck", title="Send the deck")
    now = time.time()
    home = W.build_home(m, requests_csv=None, now=now)["todos"]
    work = W.build_work(m, week={"days": [], "totals": {}}, now=now)
    assert [t["text"] for t in home["open"]] == [t["text"] for t in work["todos"]["open"]] == ["Invented thing"]
    assert [p["title"] for p in work["said"]] == ["Send the deck"]
    assert m.work_board(now, replies=False)["replies"] == []
