"""appTodo (4 Oct; docs/research/revamp-2026-10-02/64-build-appTodo.md): the to-do list in the window, open_yuki at the
list, the thinker's map of Yuki's own window, and no threads anywhere in the window.

The live test of 4 Oct: Yuki added seven to-dos by voice, then said the list "lives inside the Work tab thread card",
a place that never existed; the window showed the to-dos nowhere. Now Home has "Your list" and Work the whole list
(Done / Not needed / Correct, Add, the ones done lately), open_yuki's ``todos`` opens Home at the list, the tool block
carries a short map of the window, and the user's decision of 4 Oct removed threads from the window (Work is the list
and today's sessions; Home's Now tile is the current session). Memory keeps its threads for the agent.

Offline: temp databases and invented data only; no network, no tray, no real memory. The page side is checked in a
headless browser by scripts/check_live_patch.js (T1-T9, N1-N2).
"""

from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from yuki.memory.api import MemoryClient
from yuki.memory.store import Store
from yuki.ui import own_window
from yuki.ui import web_data as W
from yuki.ui import web_window

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "yuki" / "ui" / "web"
#: A fixed local moment: Sunday 4 Oct 2026, 10:00.
NOW = datetime(2026, 10, 4, 10, 0).timestamp()
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


def _item(ref, text, source="user", *, due=None, title=None, since=NOW - D, status="open", evidence=None):
    return {"id": ref, "text": text, "source": source, "due": due, "status": status, "evidence": evidence,
            "since": _iso(since), "owner": "user", "label": source, "title": title}


class _Todos:
    """A memory with only a to-do list (and the buttons' methods, recorded)."""

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def todos(self, include_done=False):
        return [r for r in self.rows if include_done or r["status"] == "open"]

    def end_todo(self, ref, how="done"):
        self.calls.append(("end", ref, how))
        return ref if ref.startswith(("todo:", "commit:", "loop:")) else None

    def reopen_todo(self, ref):
        self.calls.append(("reopen", ref))
        return ref if ref == "todo:1" else None

    def add_todo(self, text, due=None):
        self.calls.append(("add", text, due))
        return "todo:9"

    def correct_item(self, ref, words):
        return {"ok": True, "ref": ref, "correction_id": 7, "state": "updating"}


# -- the list's words: due dates in natural words, where an item came from ---------------------------------------------


def test_due_dates_are_said_as_people_say_them():
    day = datetime.fromtimestamp(NOW).date()
    at = lambda d, h=0, m=0: datetime.combine(day + timedelta(days=d), datetime.min.time()).timestamp() + h * H + m * M  # noqa: E731
    assert W.due_words(at(0, 17), False, NOW) == ("due today, 5 PM", False)
    assert W.due_words(at(0, 9, 30), False, NOW) == ("was due today, 9:30 AM", True)    # earlier today: past
    assert W.due_words(at(1), True, NOW) == ("due tomorrow", False)
    assert W.due_words(at(-1), True, NOW) == ("was due yesterday", True)
    assert W.due_words(at(0), True, NOW) == ("due today", False)                        # a day's due: not past today
    assert W.due_words(at(14), True, NOW) == ("due 18 October", False)
    assert W.due_words(None, False, NOW) == ("", False)
    assert W.due_epoch("2026-10-05") == (datetime(2026, 10, 5).timestamp(), True)
    assert W.due_epoch("not a date") == (None, False)


def test_a_row_says_its_words_where_it_came_from_and_whether_undo_can_put_it_back():
    loop = W.todo_view(_item("loop:3", "Riya is waiting for the climbing photos", "loop", title="Photos for Riya",
                             evidence="asked by Riya in Signal on 2026-09-28"), NOW)
    assert loop["title"] == "Photos for Riya" and loop["from"] == "I noticed it" and loop["undo"] is False
    assert loop["evidence"] == "asked by Riya in Signal on 28 September"                 # dates made natural
    mine = W.todo_view(_item("todo:4", "Order  seed trays", since=NOW - 2 * D), NOW)
    assert mine["text"] == "Order seed trays" and mine["from"] == "" and mine["undo"] is True and mine["added"]
    told = W.todo_view(_item("commit:5", "Send take 2", "chat", evidence="the user said on 2026-10-03: \"x\""), NOW)
    assert told["from"] == "you told me" and told["evidence"] is None                  # a commitment's note: not shown
    assert W.todo_view(_item("commit:6", "Find a library", "commitment"), NOW)["from"] == "I said I’d do it"
    assert W.todo_view(_item("commit:7", "Bring seeds", "call"), NOW)["from"] == "said on a call"


def test_the_list_is_due_first_then_the_newest_and_the_done_ones_come_apart():
    rows = [_item("todo:1", "undated old", since=NOW - 3 * D), _item("todo:2", "undated new", since=NOW - H),
            _item("todo:3", "due tomorrow", due=_iso(NOW + D)), _item("todo:4", "overdue", due="2026-10-01"),
            *[_item(f"todo:{10 + i}", f"done {i}", status="done") for i in range(W.TODO_DONE_SHOWN + 3)]]
    open_, done = W.todo_lists(_Todos(rows), NOW, include_done=True)
    assert [v["ref"] for v in open_] == ["todo:4", "todo:3", "todo:2", "todo:1"]
    assert open_[0]["over"] and len(done) == W.TODO_DONE_SHOWN and all(v["status"] == "done" for v in done)
    assert W.todo_lists(_Todos(rows), NOW)[1] == []                                     # done ones only when asked
    assert W.todo_moment(open_, NOW) == pytest.approx(NOW + D)                          # a timed due passing
    assert W.todo_lists(object(), NOW) == ([], [])                                      # memory can't say: empty


# -- Home's "Your list" and Work's "To-dos", from memory -----------------------------------------------------------------


def test_a_to_do_added_by_voice_is_on_home_and_work_at_once(store):
    client = MemoryClient(store)
    for i in range(7):                                                                  # the live test: seven by voice
        client.add_todo(f"Invented to-do {i}", (datetime.fromtimestamp(time.time()) + timedelta(days=i)).strftime("%Y-%m-%d"))
    home = W.build_home(client, requests_csv=None)
    assert home["todos"]["n"] == 7 and len(home["todos"]["open"]) == W.HOME_TODOS
    assert home["todos"]["open"][0]["text"] == "Invented to-do 0" and home["todos"]["open"][0]["due"] == "due today"
    assert home["todos"]["can"] == {"end": True, "undo": True, "correct": True, "add": True}
    work = W.build_work(client, week={"days": [], "totals": {}})
    assert len(work["todos"]["open"]) == 7 and work["todos"]["done"] == [] and isinstance(work["sessions"], list)
    assert "threads" not in work and "closed" not in work and work["can"] == {"correct": True}
    client.end_todo("todo:1", "done")
    again = W.build_work(client, week={"days": [], "totals": {}})
    assert [v["ref"] for v in again["todos"]["done"]] == ["todo:1"] and again["todos"]["n"] == 6


def test_home_and_work_read_the_to_do_tables_for_their_live_refresh():
    for page in ("home", "work"):
        assert {"todos", "conversation_facts", "open_loops"} <= set(W.PAGE_TABLES[page])
        assert "work_threads" not in W.PAGE_TABLES[page]                                  # nothing shows threads now
    assert {"home", "work"} <= set(web_window.TOPIC_PAGES["todos"])                       # the agent's todo tool
    assert web_window.TOOL_TOPICS["todo"] == "todos"


def test_work_lists_todays_sessions_with_their_moments():
    class _Lane:
        key, label = "code", "VS Code"

    class _Sess:
        start, end, lane, title, present_s, apps, title_full = NOW - 2 * H, NOW - H, "code", "x", 3000, [("VS Code", 3000)], ""

    class _View:
        since, lanes, segments, sessions = NOW - 10 * H, [_Lane()], [], [_Sess()]

    class _Mem:
        def episodes(self, since, until, limit=200):
            return [{"start": _iso(NOW - 2 * H), "end": _iso(NOW - H), "title": "Invented beds screen", "summary": "You built it.",
                     "ref": "episode:5"}]

    [s] = W.day_sessions(_Mem(), _View())
    assert s["start_at"] == round(NOW - 2 * H, 1) and s["end_at"] == round(NOW - H, 1)
    assert s["title"] == "Invented beds screen" and s["ref"] == "episode:5" and s["app"] == "VS Code"


# -- Home's Now tile: what you are doing now, from the current session ------------------------------------------------------


class _Eps:
    def __init__(self, eps):
        self.eps = eps

    def episodes(self, since, until, limit=50):
        return self.eps


def _ep(start, end, title, summary=None, ref="episode:1"):
    return {"start": _iso(start), "end": _iso(end), "title": title, "summary": summary, "ref": ref}


def test_the_now_tile_is_the_app_in_front_titled_by_the_current_session():
    front = {"app": "VS Code", "title": "beds.ts", "since_at": NOW - 40 * M}
    mem = _Eps([_ep(NOW - 3 * H, NOW - 2 * H, "Morning forum", ref="episode:1"),
                _ep(NOW - 50 * M, NOW - 5 * M, "Watering schedule screen", "You started the schedule.", "episode:2"),
                _ep(NOW - 70 * M, NOW - 5 * M, "A longer window over it", ref="episode:3")])
    t = W.now_session(mem, NOW, front)
    # the newest that ended while the app was in front; of two ending together, the finer one
    assert t == {"app": "VS Code", "since_at": NOW - 40 * M, "title": "Watering schedule screen",
                 "summary": "You started the schedule.", "ref": "episode:2"}
    # the app came to the front after that episode ended: not this stretch's; the window's own title instead
    late = W.now_session(mem, NOW, {"app": "Discord", "title": "general", "since_at": NOW - 2 * M})
    assert late["title"] == "general" and late["ref"] is None and late["app"] == "Discord"
    assert W.now_session(_Eps([]), NOW, {"app": "Firefox", "title": "", "since_at": NOW})["title"] is None
    assert W.now_session(mem, NOW, None) is None                                          # nothing in front: no tile


def test_home_has_no_thread_tile_and_goals_carry_no_thread(store):
    d = W.build_home(MemoryClient(store), requests_csv=None)
    assert "thread" not in d["now"] and "session" in d["now"]
    assert not hasattr(W, "current_thread") and not hasattr(W, "closed_threads") and not hasattr(W, "thread_goals")
    assert "thread" not in W.goal_view({"id": "G1", "text": "x", "status": "open", "thread": "W3"})


# -- the window's ops: Done / Not needed / Undo / Add; the thread ops are gone ------------------------------------------


def _win(tmp_path, mem):
    win = web_window.WebWindow(db_path=tmp_path / "x.db", connect=False)
    win.ui_path = tmp_path / "ui.json"
    win.memory = mem
    refreshed: list[str] = []
    win.refresh = lambda name, *a, **k: refreshed.append(name)
    return win, refreshed


def test_done_not_needed_undo_and_add_go_straight_to_memory(tmp_path):
    mem = _Todos([])
    win, refreshed = _win(tmp_path, mem)
    r = win.a_todo_end({"ref": "todo:1", "how": "done"})
    assert r["ref"] == "todo:1" and r["undo"] is True and r["until"] > time.time() and mem.calls[-1] == ("end", "todo:1", "done")
    assert {"home", "work", "activity"} <= set(refreshed)
    r = win.a_todo_end({"ref": "commit:4", "how": "not_needed"})
    assert r["undo"] is False and "until" not in r and mem.calls[-1] == ("end", "commit:4", "not_needed")
    assert win.a_todo_end({"ref": "todo:1", "how": "wrong"})["ok"] is False             # only the two buttons' choices
    assert win.a_todo_end({"ref": "thread:1", "how": "done"})["ok"] is False           # nothing open with that id
    assert win.a_todo_reopen({"ref": "todo:1"}) == {"ref": "todo:1"}
    assert win.a_todo_reopen({"ref": "todo:2"})["ok"] is False
    assert win.a_todo_add({"text": "  Invented  new to-do ", "day": "2026-10-06"}) == {"ref": "todo:9"}
    assert mem.calls[-1] == ("add", "Invented new to-do", "2026-10-06")
    win.a_todo_add({"text": "Another", "day": "2026-10-06", "time": "17:30"})
    assert mem.calls[-1] == ("add", "Another", "2026-10-06 17:30")
    assert win.a_todo_add({"text": "x", "day": "6 Oct"})["ok"] is False                 # a machine date only
    assert win.a_todo_add({"text": "  "})["ok"] is False


def test_a_to_do_corrected_refreshes_home_and_work(tmp_path):
    win, refreshed = _win(tmp_path, _Todos([]))
    assert win.a_correct_item({"ref": "loop:3", "words": "she got them already"}) == {"correction_id": 7, "ref": "loop:3"}
    assert {"home", "work"} <= set(refreshed)
    assert "thread" not in web_window.WebWindow.REF_PAGES


def test_the_thread_ops_left_the_window():
    for gone in ("close_thread", "thread_continue", "open_page", "continue"):
        assert gone not in web_window.WebWindow.ACTIONS
    for gone in ("a_close_thread", "a_thread_continue", "a_open_page", "a_continue"):
        assert not hasattr(web_window.WebWindow, gone)
    assert not hasattr(web_window, "plan_text") and not hasattr(web_window, "continue_text")
    assert {"todo_end", "todo_reopen", "todo_add", "correct_item"} <= set(web_window.WebWindow.ACTIONS)
    assert "thread_days" in web_window.WebWindow.ACTIONS                                  # the chat drawer's, kept


def test_the_pages_have_no_thread_affordances():
    work = (WEB / "pages" / "work.js").read_text(encoding="utf-8")
    home = (WEB / "pages" / "home.js").read_text(encoding="utf-8")
    css = (WEB / "lattice.css").read_text(encoding="utf-8")
    for text in (work, home):
        for gone in ("thread-close", "thread-reopen", "thread-go", "'continue'", "cont-ask", "nowthread", "Closed lately",
                     "Not a thread", "threadCard"):
            assert gone not in text, gone
    assert "'Threads'" not in work and ".thread " not in css and ".nowthread" not in css


# -- memory: Undo of a Done, Correct on a to-do -----------------------------------------------------------------------------


def test_undo_puts_back_a_to_do_you_added_within_ten_minutes_only(store):
    client = MemoryClient(store)
    ref = client.add_todo("Invented to-do")
    assert client.end_todo(ref, "done") == ref and client.todos() == []
    assert client.reopen_todo(ref) == ref and [t["id"] for t in client.todos()] == [ref]
    assert client.reopen_todo(ref) is None                                                # it is open: nothing to undo
    client.end_todo(ref, "not_needed")
    store._write(lambda c: c.execute("UPDATE todos SET done_at = done_at - 700"))         # 11 minutes ago
    assert client.reopen_todo(ref) is None and client.todos() == []
    assert client.reopen_todo("commit:3") is None and client.reopen_todo("todo:x") is None


def test_correct_on_a_to_do_queues_a_correction_aimed_at_it(store):
    from yuki.memory.corrections import target_candidate

    client = MemoryClient(store)
    ref = client.add_todo("Invented to-do for Friday")
    out = client.correct_item(ref, "it is due Monday, not Friday")
    n = int(ref.split(":")[1])
    assert out["ok"] and out["ref"] == ref and store.correction_target(out["correction_id"]) == f"T{n}"
    assert target_candidate(store, f"T{n}").text == "Invented to-do for Friday"
    client.end_todo(ref, "done")
    assert target_candidate(store, f"T{n}") is None                                       # only an open one
    assert client.correct_item("todo:999", "x")["ok"] is False


# -- open_yuki at the list, and the thinker's map of Yuki's own window ---------------------------------------------------


def test_open_yuki_opens_home_at_the_list_and_says_where():
    opened: list[str] = []
    own_window.register(lambda page: opened.append(page) or True)
    try:
        ok, report = own_window.open_page("todos")
        assert ok and opened == ["todos"] and "to-do list" in report and "Your list" in report and "Home" in report
        assert own_window.open_page("today")[0]                                           # an older caller's name
        assert not own_window.open_page("threads")[0]
    finally:
        own_window.register(None)
    assert web_window.page_name("todos") == ("home", False) and web_window.page_sub("todos") == "list"
    assert web_window.page_sub("home") == "" and own_window.qt_page("todos") == "today"


def test_the_map_is_short_names_the_list_and_never_threads():
    words = own_window.PAGE_WORDS
    assert "Your list" in words and "todos" in words and "todo list" in words and "never guess" in words
    assert "thread" not in words.casefold() and len(words) < 400
    from yuki.agent.tools import TOOL_SCHEMAS

    tool = next(t for t in TOOL_SCHEMAS if t["name"] == "open_yuki")
    assert words in tool["description"] and "todos" in tool["input_schema"]["properties"]["page"]["enum"]


def test_the_fixture_has_the_list_the_now_tile_and_no_threads():
    data = web_window.load_fixture()
    home, work = data["pages"]["home"], data["pages"]["work"]
    assert home["todos"]["open"] and home["todos"]["n"] >= len(home["todos"]["open"]) and home["now"]["session"]["title"]
    assert work["todos"]["open"] and work["todos"]["done"] and work["sessions"]
    assert all("start_at" in s and "end_at" in s for s in work["sessions"])
    text = json.dumps({"home_now": home["now"], "work": work})
    assert "thread:" not in text and '"threads"' not in text
