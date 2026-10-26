"""The user's feedback round 3, window side (builder appWin1; the design notes):
W2 session titles and summaries; W6 "maybe misheard" as what was heard and Yuki's guess; W3 Correct from the user's
own words (correct_item, its Undo, "fixing" then "fixed"). W1's threads and Home's current-thread tile left the
window with appTodo (4 Oct); their replacements are tested in tests/test_appTodo_window.py.

Offline: temp databases and invented data only; no network, no tray, no real memory. The page side is checked in
a headless browser by scripts/check_live_patch.js.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from yuki.memory.api import MemoryClient
from yuki.memory.store import NewEpisode, Store
from yuki.ui import web_data as W
from yuki.ui import web_window

NOW = time.time()
H = 3600.0


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


# -- W1 (Work's threads) and Home's current-thread tile left the window with appTodo (4 Oct: no threads in the window;
# Work is the to-do list and today's sessions, Home's Now tile the current session): tests/test_appTodo_window.py


# -- W2: sessions ----------------------------------------------------------------------------------------------------------


class _Sess:
    def __init__(self, start, end):
        self.start, self.end = start, end


def test_a_session_takes_its_title_and_summary_from_the_longest_episode_inside_it(store):
    day0 = NOW - 10 * H
    store.commit_episodes(None, day0, [
        NewEpisode(day0, day0 + H, day0, day0 + H, True, "09:00-10:00: the user coded.", {}, title="Garden planner beds",
                   summary="You finished the bed sizes screen."),
        NewEpisode(day0 + H, day0 + 1.2 * H, day0 + H, day0 + 1.2 * H, True, "x", {}, title="Quick forum check",
                   summary="You read the forum."),
        NewEpisode(day0 + 2 * H, day0 + 3 * H, day0 + 2 * H, day0 + 3 * H, True, "12:00-13:00: the user played.", {})])
    client = MemoryClient(store)
    s1, s2, s3 = _Sess(day0, day0 + 1.2 * H), _Sess(day0 + 2 * H, day0 + 3 * H), _Sess(day0 + 5 * H, day0 + 6 * H)
    labels = W.session_labels(client, [s1, s2, s3], day0 - H)
    assert labels[id(s1)]["title"] == "Garden planner beds" and labels[id(s1)]["summary"] == "You finished the bed sizes screen."
    assert labels[id(s1)]["ref"].startswith("episode:")
    # no title written yet: the app shows as the title, but the session can still be corrected (its episode's ref)
    assert labels[id(s2)]["title"] is None and labels[id(s2)]["ref"].startswith("episode:")
    assert id(s3) not in labels                                                     # no episode at all


def test_a_corrected_session_shows_fixed_on_its_new_episode(store, tmp_path):
    day0 = NOW - 10 * H
    store.commit_episodes(None, day0, [NewEpisode(day0, day0 + H, day0, day0 + H, True, "the user coded.", {},
                                                  title="Garden planner beds", summary="You sized the beds.")])
    client = MemoryClient(store)
    [ep] = client.episodes(day0 - H, day0 + 2 * H)
    old_ref = ep["ref"]
    win = web_window.WebWindow(db_path=tmp_path / "x.db", connect=False)
    win.ui_path = tmp_path / "ui.json"
    fake = _FixMemory()
    fake.store = store                                                              # the episode rows, read by id
    win.memory = fake
    win.refresh = lambda *a, **k: None
    win.a_correct_item({"ref": old_ref, "words": "it was the seed-swap app"})
    new_id = store.supersede_episode(ep["id"], "the user coded the seed-swap app.", title="Seed-swap app")
    fake.status[41] = "done"
    win._check_pending()
    assert f"episode:{new_id}" in win.fixed and old_ref not in win.fixed
    assert [e["ref"] for e in client.episodes(day0 - H, day0 + 2 * H)] == [f"episode:{new_id}"]
    assert W.pending_view(win.shown_pending(), ("episode:",))[f"episode:{new_id}"]["state"] == "fixed"


# -- W6: maybe misheard ------------------------------------------------------------------------------------------------------


class _HeardMemory:
    def misheard(self, days=7, limit=8, now=None):
        return {"items": [
            {"id": 9, "ref": "journal:9", "at": NOW - H, "app": "Discord", "text": "The user said hey meant would call.",
             "call": None, "source": {"quote": "raw words in another script", "kind": "call", "where": "Discord call"},
             "heard_in": "Discord call", "channel_known": False, "people": [], "heard": "hey meant", "guess": "Hemanth",
             "why": "Hemanth is the friend you call on Discord.", "guess_is_name": True,
             "shown": "You said Hemanth would call."},
            {"id": 10, "ref": "journal:10", "at": NOW - 2 * H, "app": "Discord", "text": "Said a word.", "call": None,
             "source": {}, "heard_in": "Discord", "channel_known": False, "people": [], "heard": None, "guess": None}],
            "total": 2}

    def resolve_heard(self, ref, verdict, *, correction_id=None):
        return {"ok": True, "correction_id": 5 if verdict == "guess_right" else None}

    def correct_item(self, ref, words):
        return {"ok": True}


def test_misheard_rows_carry_what_was_heard_the_guess_and_why_never_the_transcript():
    m = W.misheard_view(_HeardMemory(), {}, [], NOW)
    first, second = m["items"]
    assert first["heard"] == "hey meant" and first["guess"] == "Hemanth" and first["guess_is_name"] is True
    assert first["why"] == "Hemanth is the friend you call on Discord." and first["text"] == "You said Hemanth would call."
    assert "raw words" not in str(m) and "quote" not in first
    assert "heard" not in second and second["text"] == "Said a word."              # no guess: shown as before
    assert m["direct"] is True and m["correct"] is True


def test_yes_on_a_guess_is_guess_right(tmp_path):
    win = web_window.WebWindow(db_path=tmp_path / "x.db", connect=False)
    win.memory = _HeardMemory()
    assert win.a_resolve_heard({"ref": "journal:9", "verdict": "guess_right"}) == {"correction_id": 5, "verdict": "guess_right"}
    assert win.a_resolve_heard({"ref": "journal:9", "verdict": "maybe"})["ok"] is False


# -- W3: Correct from your own words ------------------------------------------------------------------------------------------


class _FixMemory:
    def __init__(self):
        self.calls, self.status = [], {}

    def correct_item(self, ref, words):
        self.calls.append(("correct", ref, words))
        if ref == "rule:1":
            return {"ok": False, "error": "That can't be corrected here."}
        return {"ok": True, "ref": ref, "correction_id": 41, "state": "updating"}

    def cancel_correction(self, cid):
        self.calls.append(("cancel", cid))
        return {"ok": cid == 41, "id": cid}

    def correction_status(self, cid):
        return {"id": cid, "state": self.status.get(cid, "updating")}


def test_correct_sends_the_users_words_and_shows_fixing_then_fixed(tmp_path):
    win = web_window.WebWindow(db_path=tmp_path / "x.db", connect=False)
    win.ui_path = tmp_path / "ui.json"
    win.memory = mem = _FixMemory()
    refreshed = []
    win.refresh = lambda name, *a, **k: refreshed.append(name)
    assert win.a_correct_item({"ref": "todo:7", "words": "  that was  last week "}) == {"correction_id": 41, "ref": "todo:7"}
    assert mem.calls[-1] == ("correct", "todo:7", "that was last week") and {"home", "work"} <= set(refreshed)
    assert win.a_correct_item({"ref": "todo:7", "words": "  "})["ok"] is False
    assert win.a_correct_item({"ref": "rule:1", "words": "x"})["error"] == "That can't be corrected here."
    view = W.pending_view(win.shown_pending(), ("todo:",))
    assert view["todo:7"]["state"] == "fixing" and view["todo:7"]["cid"] == 41 and view["todo:7"]["words"] == "that was last week"
    # the pass lands: the row says "Fixed" a while (this window only; the shared file no longer has it)
    mem.status[41] = "done"
    win._check_pending()
    assert "todo:7" not in win.pending and win.fixed["todo:7"]["state"] == "fixed"
    assert W.pending_view(win.shown_pending(), ("todo:",))["todo:7"]["state"] == "fixed"
    assert W.pending_view(win.shown_pending(), ("todo:",), now=time.time() + 3600) == {}
    assert {"me", "work", "day", "home"} <= set(refreshed)


def test_undo_cancels_a_correction_that_waits(tmp_path):
    win = web_window.WebWindow(db_path=tmp_path / "x.db", connect=False)
    win.ui_path = tmp_path / "ui.json"
    win.memory = mem = _FixMemory()
    win.refresh = lambda *a, **k: None
    win.a_correct_item({"ref": "episode:3", "words": "it was the Rust exercise"})
    assert win.a_cancel_correction({"ref": "episode:3", "correction_id": 41}) == {} and "episode:3" not in win.pending
    assert mem.calls[-1] == ("cancel", 41)
    late = win.a_cancel_correction({"ref": "episode:3", "correction_id": 40})
    assert late["ok"] is False and late["too_late"] is True
    assert win.a_cancel_correction({"ref": "episode:9"})["ok"] is False             # nothing waiting


def test_an_older_memory_without_correct_item_uses_the_fixed_format(tmp_path):
    class Old:
        def __init__(self):
            self.text = None

        def correct_portrait(self, text):
            self.text = text
            return 12

    win = web_window.WebWindow(db_path=tmp_path / "x.db", connect=False)
    win.ui_path = tmp_path / "ui.json"
    win.memory = old = Old()
    r = win.a_correct_item({"ref": "fact:4", "words": "he is my cousin", "title": "Dev", "text": "Dev is your classmate."})
    assert r == {"correction_id": 12} and "he is my cousin" in old.text


def test_the_fixture_has_the_round_3_shapes():
    data = web_window.load_fixture()
    work, day, home, me = (data["pages"][k] for k in ("work", "day", "home", "me"))
    assert "threads" not in work and "closed" not in work and work["todos"]["open"] and work["today"]
    assert "front" not in home["now"] and "thread" not in home["now"] and home["now"]["session"]["title"]
    titles = [s["title"] for s in day["sessions"]]
    assert len(set(titles)) == len(titles) and not any(t.endswith("…") for t in titles)
    assert any(s.get("summary") and s.get("ref") for s in day["sessions"])
    assert all(m.get("heard") and m.get("guess") for m in me["misheard"]["items"]) and me["can"]["correct"]
    assert all(k in data["replies"] for k in ("correct_item", "cancel_correction", "todo_end", "todo_reopen", "todo_add"))
    assert not any(k in data["replies"] for k in ("close_thread", "thread_continue", "open_page", "continue"))
