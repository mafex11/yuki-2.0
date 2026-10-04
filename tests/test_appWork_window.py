"""The Work page in the window (docs/research/revamp-2026-10-02/68-build-appWork.md): the page data (memory's merged
list split so each thing shows once, Today with the Day tab's own figures, the natural-date words), the window's ops
(a row's mark, Open chat, the Continue run, Draft a nudge into the ask bar unsent) and the ask bar's note.

Offline: temp databases, stubs and invented data only; no network, no tray, no real memory, nothing on screen. The page
side is checked in a headless browser by scripts/check_live_patch.js (WK1-WK15).
"""

from __future__ import annotations

import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuki.memory.api import MemoryClient
from yuki.memory.store import Store
from yuki.ui import web_data as W
from yuki.ui import web_window

#: Sunday 4 Oct 2026, 15:00 local.
NOW = datetime(2026, 10, 4, 15, 0).timestamp()
M, H, D = 60.0, 3600.0, 86400.0


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds")


def _row(ref, text, source="user", *, due=None, title=None, since=NOW - D, status="open", evidence=None, owner="user"):
    return {"id": ref, "text": text, "source": source, "due": due, "status": status, "evidence": evidence,
            "since": _iso(since), "owner": owner, "label": source, "title": title}


# -- one natural-date format everywhere on the page -------------------------------------------------------------------


def test_dates_are_said_one_way_on_the_page():
    day = datetime.fromtimestamp(NOW).date()
    at = lambda d, h=12: datetime.combine(day + timedelta(days=d), datetime.min.time()).timestamp() + h * H  # noqa: E731
    assert W.week_words(at(0, 9), NOW) == "9 AM"
    assert W.week_words(at(-1), NOW) == "yesterday"
    assert W.week_words(at(-4), NOW) == f"{day - timedelta(days=4):%A}"            # a weekday within the week
    assert W.week_words(at(-9), NOW) == f"{(day - timedelta(days=9)).day} {day - timedelta(days=9):%B}"
    assert W.ahead_words(at(1, 11), False, NOW) == "tomorrow 11 AM"
    assert W.ahead_words(at(3, 16), False, NOW) == f"{day + timedelta(days=3):%A} 4 PM"
    assert W.ahead_words(at(0, 18), False, NOW) == "today 6 PM"
    assert W.ahead_words(at(2), True, NOW) == f"{day + timedelta(days=2):%A}"
    # due words: a weekday within the week either way ("due Monday", "was due Wednesday")
    assert W.due_words(at(3, 0), True, NOW) == (f"due {day + timedelta(days=3):%A}", False)
    assert W.due_words(at(-4, 0), True, NOW) == (f"was due {day - timedelta(days=4):%A}", True)
    assert W.due_words(at(1, 0), True, NOW) == ("due tomorrow", False)
    assert W.due_words(at(-1, 0), True, NOW) == ("was due yesterday", True)


# -- the columns: each thing once ---------------------------------------------------------------------------------------


def _rows():
    tomorrow = (datetime.fromtimestamp(NOW) + timedelta(days=1)).strftime("%Y-%m-%d")
    return [
        _row("todo:1", "Water the invented plants", due=tomorrow),
        _row("commit:2", "Remind you to post the invented letter", "commitment", owner="yuki"),
        _row("commit:3", "Send Dev the planner build", "chat", title="Send Dev the build",
             evidence="the user said on 2026-10-04: \"I'll send Dev the build tonight\""),
        _row("commit:4", "Print the flyers", "call", due=tomorrow, evidence="the user said in a call on 2026-10-03: \"I can print the flyers\""),
        _row("loop:5", "Dev said he would send the measurements (Dev on Signal on 2026-10-02).", "loop", since=NOW - 2 * D),
        _row("loop:6", "The user promised Noor the drum loop (on Signal on 2026-10-03).", "loop"),
        _row("loop:7", "Riya asked the user for a dark mode (Riya in Signal on 2026-10-01).", "loop"),
        _row("loop:8", "Something not judged yet.", "loop"),
        _row("todo:9", "An old one", status="done"),
    ]


BOARD = {"loops": {5: {"kind": "waiting", "who": "Dev", "what": "send the measurements", "where": "Signal",
                       "draft": "message Dev on Signal: any luck with the measurements?"},
                   6: {"kind": "promised", "who": "Noor", "what": "Send Noor the drum loop", "where": "Signal"},
                   7: {"kind": "asked", "who": "Riya", "what": "Add a dark mode", "where": "Signal"}},
         "listed": [], "replies": [], "meetings": [], "ready": True}


def test_memorys_list_is_split_so_each_thing_shows_once():
    cols = W.work_columns(_rows(), BOARD, [], NOW)
    lst = {t["ref"] for t in cols["list"]}
    said = {p["ref"] for p in cols["said"]}
    assert lst == {"todo:1", "commit:2", "loop:7", "loop:8"}                         # yours, Yuki's own, asked, unjudged
    assert said == {"commit:3", "commit:4", "loop:6"}                                 # told me, said on a call, promised
    assert [w["ref"] for w in cols["waiting"]] == ["loop:5"]
    assert not lst & said and not lst & {"loop:5"} and not said & {"loop:5"}
    w = cols["waiting"][0]
    assert w["who"] == "Dev" and w["what"] == "send the measurements" and w["draft"].startswith("message Dev") and w["day"]
    s3 = next(p for p in cols["said"] if p["ref"] == "commit:3")
    assert s3["quote"] == "I'll send Dev the build tonight" and s3["where"] == "said to me" and s3["title"] == "Send Dev the build"
    s4 = next(p for p in cols["said"] if p["ref"] == "commit:4")
    assert s4["where"] == "on a call" and s4["due"] == "due tomorrow"
    s6 = next(p for p in cols["said"] if p["ref"] == "loop:6")
    assert s6["title"] == "Send Noor the drum loop" and s6["who"] == "Noor" and not s6["quote"]
    meta = {t["ref"]: t["meta"] for t in cols["list"]}
    assert meta["loop:7"].startswith("Riya asked in Signal, ") and meta["commit:2"] == "I said I’d do it"
    assert meta["loop:8"].startswith("I noticed it") and meta["todo:1"].startswith("added ")
    assert next(t for t in cols["list"] if t["ref"] == "todo:1")["tomorrow"] is True
    assert [t["ref"] for t in cols["done"]] == ["todo:9"] and cols["done"][0]["back"] is True


def test_a_promise_moved_to_your_list_leaves_you_said_youd_and_a_kept_one_stays_struck():
    board = dict(BOARD, listed=["commit:3", "loop:6"])
    kept = [{"ref": "commit:10", "text": "Send Riya the link", "title": None, "closed_at": NOW - 600, "since": NOW - 3 * H}]
    cols = W.work_columns(_rows(), board, kept, NOW)
    assert {"commit:3", "loop:6"} <= {t["ref"] for t in cols["list"]}
    assert {p["ref"] for p in cols["said"]} == {"commit:4", "commit:10"}
    k = next(p for p in cols["said"] if p["ref"] == "commit:10")
    assert k["closed"].startswith("closed itself: I saw it done ")
    moved = next(t for t in cols["list"] if t["ref"] == "commit:3")
    assert moved["moved"] and moved["meta"].startswith("you told me")


def test_the_add_field_quote_reader_takes_only_memorys_own_format():
    assert W._quote_of('the user said in a call on 2026-10-03: "I can print the flyers"') == "I can print the flyers"
    assert W._quote_of("asked by Riya in Signal") == ""
    assert W._quote_of(None) == ""


# -- Today: the Day tab's own figures ------------------------------------------------------------------------------------


class _Day:
    def __init__(self):
        self.totals = {"present_s": 5 * H, "fullscreen_s": 1 * H}
        lane = SimpleNamespace(key="code", label="VS Code", present_s=4 * H, fullscreen_s=0.0)
        self.lanes = [lane]
        seg = lambda s, e, lane="code": SimpleNamespace(start=s, end=e, lane=lane, state="present", present_s=e - s, fullscreen=False)  # noqa: E731
        self.segments = [seg(NOW - 3 * H, NOW - 2 * H), seg(NOW - 2 * H + 20, NOW - 1.5 * H)]


class _TodayMem:
    def __init__(self, threads=None, timeline=None):
        self._threads = threads
        self._timeline = timeline or []

    def work_threads_for(self, spans):
        return self._threads if self._threads is not None else [None for _ in spans]

    def day_timeline(self, since, until):
        return [r for r in self._timeline if r["end"] > since and r["start"] < until]


def test_today_has_the_days_active_and_longest_focus_and_the_threads_with_most_time():
    W._usual_cache.clear()
    sessions = [{"start_at": NOW - 3 * H, "end_at": NOW - 1.5 * H, "title": "Bed sizes screen", "apps": [["VS Code", 80], ["Firefox", 10]]},
                {"start_at": NOW - 1 * H, "end_at": NOW - 0.5 * H, "title": "Budget", "apps": [["Calc", 25]]},
                {"start_at": NOW - 0.4 * H, "end_at": NOW, "title": "Planner again", "apps": [["VS Code", 20]]}]
    th = [{"ref": "thread:7", "title": "Garden planner"}, None, {"ref": "thread:7", "title": "Garden planner"}]
    board = {"meetings": [{"at": NOW + D, "all_day": False, "title": "Seed swap call", "seen": ["Firefox"]}]}
    T = W.work_today(_TodayMem(th), _Day(), sessions, board, NOW)
    assert T["focus"] == 240                                                             # the Day's Active: 5h - 1h
    assert T["longest"]["min"] == 90 and T["longest"]["app"] == "VS Code" and T["longest"]["title"] == "Bed sizes screen"
    assert T["worked"] == [["Garden planner", 110, "thread:7"]]                          # grouped by thread, no Budget
    assert T["coming"][0][1] == "Seed swap call" and T["coming"][0][0].startswith("tomorrow ")
    assert T["coming_seen"] == "seen on your screen in Firefox"
    assert T["usual"] is None                                                            # no history: no usual


def test_your_usual_by_now_is_the_same_weekdays_median_up_to_this_hour():
    W._usual_cache.clear()
    day = datetime.fromtimestamp(NOW).date()
    rows = []
    for k, hours in ((1, 3.0), (2, 4.0), (3, 6.0)):                                       # the same weekday, 1-3 weeks back
        d0 = datetime.combine(day - timedelta(days=7 * k), datetime.min.time()).timestamp()
        rows.append({"start": d0 + 9 * H, "end": d0 + 9 * H + hours * H, "state": "present", "active_s": hours * H,
                     "passive_s": 0.0, "fullscreen": 0})
        rows.append({"start": d0 + 16 * H, "end": d0 + 20 * H, "state": "present", "active_s": 4 * H,      # after 15:00:
                     "passive_s": 0.0, "fullscreen": 0})                                                    # not by now
    d1 = datetime.combine(day - timedelta(days=7), datetime.min.time()).timestamp()
    rows.append({"start": d1 + 13 * H, "end": d1 + 14 * H, "state": "present", "active_s": 3600.0, "passive_s": 0.0,
                 "fullscreen": 1})                                                                          # full screen: not counted
    assert W.usual_by_now(_TodayMem(timeline=rows), NOW) == pytest.approx(240.0)


def test_the_work_page_reads_its_new_tables_for_the_live_refresh():
    assert {"work_items", "messages", "todos", "conversation_facts", "open_loops", "timeline"} <= set(W.PAGE_TABLES["work"])


def test_build_work_over_a_real_memory_has_every_block(store):
    m = MemoryClient(store)
    m.add_todo("Invented thing")
    store.add_conversation_fact("commitment", "The user will send the invented deck", owner="user", origin="model",
                                quote="I'll send the deck", title="Send the deck")
    d = W.build_work(m, week={"days": [], "totals": {}}, now=time.time())
    assert {"todos", "waiting", "said", "replies", "today", "week", "pending", "can"} <= set(d)
    assert d["todos"]["can"]["mark"] and d["todos"]["can"]["reopen"]
    assert [t["text"] for t in d["todos"]["open"]] == ["Invented thing"]
    assert [p["title"] for p in d["said"]] == ["Send the deck"] and d["said"][0]["quote"] == "I'll send the deck"
    assert d["replies"] == {"items": [], "ready": False}


# -- the window's ops ----------------------------------------------------------------------------------------------------


class _Mem:
    def __init__(self):
        self.calls = []
        self.board = {"loops": {5: {"kind": "waiting", "who": "Dev", "what": "x", "draft": "message Dev on Signal: hey?"}}}

    def work_mark(self, key, mark):
        self.calls.append(("mark", key, mark))
        return key.startswith(("chat:", "commit:", "loop:"))

    def work_chat(self, key):
        return {"app": "Signal", "process": "signal", "name": "Dev", "title": "Signal", "url": None} if key == "chat:3" else None

    def work_board(self, now=None):
        return self.board

    def thread_continue(self, ref):
        return {"title": "Garden planner", "places": [{"app": "VS Code", "title": "planner", "url": None},
                                                      {"app": "Firefox", "title": "Docs", "url": "https://example.com/docs"}]} if ref == "thread:7" else None

    def end_todo(self, ref, how="done"):
        self.calls.append(("end", ref, how))
        return ref

    def reopen_item(self, ref, any_age=False):
        self.calls.append(("reopen", ref, any_age))
        return ref

    def add_todo_words(self, words, client=None):
        self.calls.append(("words", words))
        return "todo:9", "2026-10-05"

    def add_todo(self, text, due=None):
        self.calls.append(("add", text, due))
        return "todo:8"


def _win(tmp_path, mem):
    win = web_window.WebWindow(db_path=tmp_path / "x.db", connect=False)
    win.memory = mem
    refreshed: list[str] = []
    win.refresh = lambda name, *a, **k: refreshed.append(name)
    trayed: list[tuple] = []
    win._tray = lambda op, wait_s=None, **args: trayed.append((op, args)) or {}
    win._due_reader = lambda: None
    return win, refreshed, trayed


def test_a_rows_mark_is_a_closed_name(tmp_path):
    mem = _Mem()
    win, refreshed, _ = _win(tmp_path, mem)
    assert win.a_work_mark({"key": "chat:3", "mark": "no_reply"}) == {} and mem.calls[-1] == ("mark", "chat:3", "no_reply")
    assert win.a_work_mark({"key": "commit:4", "mark": "listed"}) == {} and "home" in refreshed and "work" in refreshed
    assert win.a_work_mark({"key": "chat:3", "mark": None}) == {}
    assert win.a_work_mark({"key": "chat:3", "mark": "sent"})["ok"] is False                       # nothing else
    assert win.a_work_mark({"key": "thread:3", "mark": "listed"})["ok"] is False


def test_open_chat_only_brings_it_forward_and_marks_it_opened(tmp_path, monkeypatch):
    mem = _Mem()
    win, refreshed, trayed = _win(tmp_path, mem)
    seen = []
    monkeypatch.setattr(web_window, "bring_forward", lambda place: seen.append(place) or (True, "chat"))
    assert win.a_work_open_chat({"key": "chat:3"}) == {"app": "Signal", "how": "chat"}
    assert seen[0]["process"] == "signal" and mem.calls[-1] == ("mark", "chat:3", "opened") and trayed == []   # nothing sent
    monkeypatch.setattr(web_window, "bring_forward", lambda place: (False, ""))
    r = win.a_work_open_chat({"key": "chat:3"})
    assert r["ok"] is False and "isn’t open" in r["error"]
    assert win.a_work_open_chat({"key": "chat:99"})["ok"] is False


def test_bring_forward_opens_a_web_chat_only_by_its_own_address(monkeypatch):
    opened = []
    import webbrowser

    monkeypatch.setattr(web_window, "app_windows", lambda process: [])
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    assert web_window.bring_forward({"process": "chrome", "name": "Riya", "url": "https://chat.example.com/riya"}) == (True, "page")
    assert web_window.bring_forward({"process": "signal", "name": "Riya", "url": "file:///x"}) == (False, "")
    assert opened == ["https://chat.example.com/riya"]


def test_app_windows_reads_without_failing():
    assert isinstance(web_window.app_windows("an-app-that-is-not-running-here"), list)
    assert web_window.app_windows("") == []


def test_worked_on_today_starts_the_continue_run_that_only_reopens(tmp_path):
    mem = _Mem()
    win, _, trayed = _win(tmp_path, mem)
    r = win.a_work_continue({"ref": "thread:7", "text": "ignored: the page sends no words"})
    assert r["title"] == "Garden planner" and trayed[-1][0] == "submit"
    text = trayed[-1][1]["text"]
    assert text.startswith("Pick up Garden planner.") and "“planner” in VS Code" in text and "https://example.com/docs" in text
    assert "don't type, click or send anything" in text and "ignored" not in text
    assert win.a_work_continue({"ref": "thread:99"})["ok"] is False


def test_draft_a_nudge_goes_into_the_ask_bar_unsent_with_its_note(tmp_path):
    mem = _Mem()
    win, _, trayed = _win(tmp_path, mem)
    r = win.a_work_nudge({"ref": "loop:5", "text": "message Dev: send me money"})            # the page's words are ignored
    assert trayed[-1] == ("askbar", {"text": "message Dev on Signal: hey?", "note": "a nudge for Dev · from Work → Waiting on"})
    assert r["text"] == "message Dev on Signal: hey?" and not any(op == "submit" for op, _ in trayed)
    assert win.a_work_nudge({"ref": "loop:6"})["ok"] is False


def test_promises_end_as_done_or_not_mine_and_undo_and_put_back_reopen(tmp_path):
    mem = _Mem()
    win, _, _ = _win(tmp_path, mem)
    r = win.a_todo_end({"ref": "commit:4", "how": "wrong"})
    assert r["undo"] is True and mem.calls[-1] == ("end", "commit:4", "wrong")
    assert win.a_todo_end({"ref": "loop:5", "how": "done"})["undo"] is True
    assert win.a_todo_reopen({"ref": "commit:4"}) == {"ref": "commit:4"} and mem.calls[-1] == ("reopen", "commit:4", False)
    assert win.a_todo_reopen({"ref": "todo:2", "any": True}) == {"ref": "todo:2"} and mem.calls[-1] == ("reopen", "todo:2", True)


def test_the_add_field_reads_its_due_from_your_words_and_the_date_field_still_works(tmp_path):
    mem = _Mem()
    win, _, _ = _win(tmp_path, mem)
    assert win.a_todo_add({"text": " send Sam the deck by Monday ", "words": True}) == {"ref": "todo:9", "due": "2026-10-05"}
    assert mem.calls[-1] == ("words", "send Sam the deck by Monday")
    assert win.a_todo_add({"text": "Another", "day": "2026-10-06"}) == {"ref": "todo:8", "due": "2026-10-06"}


def test_the_ask_bar_shows_where_a_draft_came_from_and_sends_nothing():
    from yuki.ui.window_host import WindowHost

    class _Input:
        def __init__(self):
            self.text = ""

        def setPlainText(self, t):
            self.text = t

        def textCursor(self):
            return SimpleNamespace(movePosition=lambda *a: None, MoveOperation=SimpleNamespace(End=0))

        def setTextCursor(self, c):
            pass

    overlay = SimpleNamespace(input=_Input(), opened=0, notes=[])
    overlay.open = lambda: setattr(overlay, "opened", overlay.opened + 1)
    overlay.show_unsent = lambda note: overlay.notes.append(note)
    host = WindowHost.__new__(WindowHost)
    host.ui = SimpleNamespace(overlay=overlay)
    host._on_gui = lambda fn: fn()
    host._log = lambda *a, **k: None
    assert host._op_askbar({"text": "message Dev on Signal: hey?", "note": "a nudge for Dev · from Work → Waiting on"}) == {}
    assert overlay.input.text == "message Dev on Signal: hey?" and overlay.notes == ["a nudge for Dev · from Work → Waiting on"]
    host._op_askbar({"text": "a plan"})
    assert overlay.notes == ["a nudge for Dev · from Work → Waiting on"]                             # no note: no card


def test_the_work_ops_are_closed_names_and_reads_never_hold_a_redraw():
    for op in ("work_mark", "work_open_chat", "work_continue", "work_nudge"):
        assert op in web_window.WebWindow.ACTIONS
    app = (Path(web_window.__file__).parent / "web" / "app.js").read_text(encoding="utf-8")
    assert "'work_nudge', 'work_continue'" in app


def test_correct_moves_the_due_date_and_keeps_the_rest_as_your_correction(tmp_path):
    class Client:
        def __init__(self, data):
            self.data = data
            self.messages = SimpleNamespace(create=lambda **k: SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=self.data)]))

    class Mem(_Mem):
        def set_due(self, ref, due):
            self.calls.append(("due", ref, due))
            return ref

        def correct_item(self, ref, words):
            self.calls.append(("correct", ref, words))
            return {"ok": True, "ref": ref, "correction_id": 5}

    mem = Mem()
    win, refreshed, _ = _win(tmp_path, mem)
    win._record_pending = lambda *a, **k: None
    win._due_reader = lambda: Client({"due": "2026-10-05", "rest": ""})
    assert win.a_correct_item({"ref": "todo:3", "words": "it's due Monday", "title": "Send the deck"}) == {"ref": "todo:3", "due": "2026-10-05", "correction_id": None}
    assert mem.calls == [("due", "todo:3", "2026-10-05")] and "work" in refreshed                  # no correction pass
    win._due_reader = lambda: Client({"due": "2026-10-09", "rest": "it's for Asha"})
    r = win.a_correct_item({"ref": "commit:4", "words": "due Friday, and it's for Asha"})
    assert r == {"correction_id": 5, "ref": "commit:4", "due": "2026-10-09"} and mem.calls[-1] == ("correct", "commit:4", "it's for Asha")
    win._due_reader = lambda: Client({"due": "", "rest": "x"})
    assert win.a_correct_item({"ref": "todo:3", "words": "Lena owns this"})["correction_id"] == 5
    assert mem.calls[-1] == ("correct", "todo:3", "Lena owns this")                                # words whole
