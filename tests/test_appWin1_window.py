"""The user's feedback round 3, window side (builder appWin1; docs/research/revamp-2026-10-02/62-build-appWin1.md):
W1 Work threads you can close, dismiss, reopen and continue (the next-step request, the places to bring back); W2
session titles and summaries; Home's "In front" tile as the current work thread; W6 "maybe misheard" as what was
heard and Yuki's guess; W3 Correct from the user's own words (correct_item, its Undo, "fixing" then "fixed").

Offline: temp databases and invented data only; no network, no tray, no real memory. The page side is checked in
a headless browser by scripts/check_live_patch.js.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

from yuki.memory.api import MemoryClient
from yuki.memory.store import NewEpisode, Store, WorkOp
from yuki.ui import web_data as W
from yuki.ui import web_window

NOW = time.time()
M, H = 60.0, 3600.0


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def _thread(store, title, state, *, at=None, app=None, open_items=()):
    [op] = store.apply_work([WorkOp("starts", None, title=title, state=state, at=at or NOW, seen_at=at or NOW,
                                    open_items=list(open_items), app=app,
                                    vector=np.ones(4, dtype=np.float32), embed_model="stub")])
    return op.result_id


def _touch(store, tid, at, app):
    store.apply_work([WorkOp("continues", tid, state="Still on it.", at=at, seen_at=at, app=app)])


def _card(store, tid, shown, nxt):
    store.change_work_threads([{"action": "present", "id": tid, "shown": shown, "next": nxt}], origin="test")


# -- W1: Work's threads ------------------------------------------------------------------------------------------------


def test_work_shows_the_card_and_next_step_and_folds_closed_threads_away(store):
    client = MemoryClient(store)
    a = _thread(store, "Seed-swap app", "The user wired the form.", open_items=["fix uploads"])
    b = _thread(store, "Garden planner", "The user drew the beds.", at=NOW - H)
    c = _thread(store, "A cooking video", "The user watched a video.", at=NOW - 2 * H)
    _card(store, a, "You wired the form; photo uploads still fail.", "Fix the photo upload.")
    client.close_thread(f"thread:{b}", "done")
    client.close_thread(f"thread:{c}", "dismiss")
    data = W.build_work(client, week={"days": [], "totals": {}}, now=NOW)
    assert [t["ref"] for t in data["threads"]] == [f"thread:{a}"]                    # closed ones leave the list
    t = data["threads"][0]
    assert t["state"] == "You wired the form; photo uploads still fail." and t["next"] == "Fix the photo upload."
    closed = {x["ref"]: x for x in data["closed"]}
    assert closed[f"thread:{b}"]["status"] == "done" and closed[f"thread:{b}"]["by_you"] is True
    assert closed[f"thread:{c}"]["status"] == "dismissed"
    assert data["can"] == {"close": True, "cont": True, "correct": True}
    # Reopen brings it back to the list
    client.close_thread(f"thread:{c}", "reopen")
    again = W.build_work(client, week={"days": [], "totals": {}}, now=NOW)
    assert f"thread:{c}" in [x["ref"] for x in again["threads"]] and f"thread:{c}" not in [x["ref"] for x in again["closed"]]


def test_a_thread_closed_long_ago_is_not_in_the_closed_fold(store):
    client = MemoryClient(store)
    old = _thread(store, "Old thing", "Done long ago.", at=NOW - 40 * 86400)
    client.close_thread(f"thread:{old}", "done")
    data = W.build_work(client, week={"days": [], "totals": {}}, now=NOW + 30 * 86400)
    assert data["closed"] == []


def test_the_window_closes_reopens_and_continues_a_thread(store, tmp_path):
    client = MemoryClient(store)
    cap = store.upsert_thread("Chrome", "Seed swap - staging", "https://seeds.example/staging", process="chrome.exe")
    [op] = store.apply_work([WorkOp("starts", None, title="Seed-swap app", state="The user wired the form.", at=NOW,
                                    seen_at=NOW, capture_thread_id=cap, vector=np.ones(4, dtype=np.float32),
                                    embed_model="stub")])
    tid = op.result_id
    _card(store, tid, "You wired the form; uploads fail.", "Fix the photo upload.")
    win = web_window.WebWindow(db_path=tmp_path / "x.db", connect=False)
    win.memory = client
    sent = []
    win._tray = lambda op, wait_s=0, **kw: (sent.append((op, kw)) or {"ok": True})
    assert win.a_close_thread({"ref": f"thread:{tid}", "how": "done"}) == {"status": "done", "how": "done"}
    assert win.a_close_thread({"ref": f"thread:{tid}", "how": "finish"})["ok"] is False
    assert win.a_close_thread({"ref": f"thread:{tid}", "how": "reopen"})["status"] == "active"
    cont = win.a_thread_continue({"ref": f"thread:{tid}"})["cont"]
    assert cont["next"] == "Fix the photo upload." and cont["ask"].startswith("Help me continue Seed-swap app.")
    assert cont["places"] and all("url" not in p for p in cont["places"])          # the page never gets URLs
    assert cont["places"][0]["page"] is True
    # Ask Yuki: memory's request, with the ticked places, as the user's request; Edit first: the ask bar
    win.a_continue({"ref": f"thread:{tid}", "how": "ask", "places": [0]})
    op_, kw = sent[-1]
    assert op_ == "submit" and kw["text"].startswith("Help me continue Seed-swap app. Where it stands:")
    assert "Next: Fix the photo upload." in kw["text"] and "First bring back what I had open for it:" in kw["text"]
    assert "https://seeds.example/staging" in kw["text"] and "left off" not in kw["text"]
    win.a_continue({"ref": f"thread:{tid}", "how": "edit", "places": []})
    assert sent[-1][0] == "askbar" and "First bring back" not in sent[-1][1]["text"]
    assert win.a_thread_continue({"ref": "thread:999"})["ok"] is False


def test_open_page_opens_only_memorys_own_web_address(monkeypatch, tmp_path):
    opened = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    class Mem:
        def thread_continue(self, ref):
            return {"places": [{"kind": "window", "app": "VS Code", "title": "x.ts", "url": None},
                               {"kind": "page", "app": "Chrome", "title": "Guide", "url": "https://docs.example/a"},
                               {"kind": "page", "app": "Files", "title": "local", "url": "file:///C:/x"}]}

    win = web_window.WebWindow(db_path=tmp_path / "x.db", connect=False)
    win.memory = Mem()
    win._grant_foreground = lambda: None
    assert win.a_open_page({"ref": "thread:1", "place": 1}) == {} and opened == ["https://docs.example/a"]
    assert win.a_open_page({"ref": "thread:1", "place": 0})["ok"] is False
    assert win.a_open_page({"ref": "thread:1", "place": 2})["ok"] is False          # http and https only
    assert win.a_open_page({"ref": "thread:1", "place": "1"})["ok"] is False
    assert len(opened) == 1


def test_continue_text_names_the_ticked_places_once():
    cont = {"ask": "Help me continue X. Next: Y.", "places": [
        {"title": "x.ts", "app": "VS Code", "url": None}, {"title": "Guide", "app": "Chrome", "url": "https://d.example/g"}]}
    assert web_window.continue_text(cont, []) == "Help me continue X. Next: Y."
    text = web_window.continue_text(cont, [1, 0, 1, 7, True, "0"])
    assert text == ("Help me continue X. Next: Y. First bring back what I had open for it: “Guide” in Chrome "
                    "(https://d.example/g); “x.ts” in VS Code.")


# -- Home: the current work thread ---------------------------------------------------------------------------------------


def test_home_shows_the_thread_you_are_on_with_its_app_time_and_next_step(store):
    client = MemoryClient(store)
    tid = _thread(store, "Yuki revamp", "The user built the window.", at=NOW - 80 * M, app="T3 Code")
    other = _thread(store, "Budget", "The user typed numbers.", at=NOW - 5 * H, app="Calc")
    for k in range(1, 8):
        _touch(store, tid, NOW - 80 * M + k * 10 * M, "T3 Code")
    _card(store, tid, "You built the window; the misheard list is next.", "Wire the misheard buttons.")
    front = {"app": "T3 Code", "since_at": NOW - 30 * M}
    t = W.current_thread(client, NOW, front)
    assert t["ref"] == f"thread:{tid}" and t["title"] == "Yuki revamp" and t["app"] == "T3 Code"
    assert t["next"] == "Wire the misheard buttons." and t["in_front"] is True
    # its stretch, to now: the page counts from since_at (no minutes in the data, so it doesn't change every minute)
    assert t["min"] is None and abs(t["since_at"] - (NOW - 80 * M)) < 2
    # another app in front now: the stretch ends at the last touch
    t2 = W.current_thread(client, NOW, {"app": "Discord", "since_at": NOW - 2 * M})
    assert t2["in_front"] is False and 69 <= t2["min"] <= 71
    # nothing touched lately: no tile; a closed thread is never the current one
    assert W.current_thread(client, NOW + 3 * H, None) is None
    client.close_thread(f"thread:{tid}", "done")
    assert W.current_thread(client, NOW, front) is None
    assert other and "work_threads" in W.PAGE_TABLES["home"]


def test_the_tile_counts_from_the_threads_own_start_not_from_when_the_app_came_in_front(store):
    client = MemoryClient(store)
    a = _thread(store, "Seed-swap app", "The user wired the form.", at=NOW - 150 * M, app="VS Code")
    _touch(store, a, NOW - 100 * M, "VS Code")
    b = _thread(store, "Garden planner", "The user drew the beds.", at=NOW - 20 * M, app="VS Code")
    _touch(store, b, NOW - 1 * M, "VS Code")
    t = W.current_thread(client, NOW, {"app": "VS Code", "since_at": NOW - 150 * M})
    assert t["ref"] == f"thread:{b}" and abs(t["since_at"] - (NOW - 20 * M)) < 2      # about 20 minutes, not 2h 30m


def test_a_thread_just_closed_gives_way_to_the_open_one_touched_before_it(store):
    client = MemoryClient(store)
    y = _thread(store, "Garden planner", "The user drew the beds.", at=NOW - 5 * M, app="Figma")
    x = _thread(store, "Seed-swap app", "The user wired the form.", at=NOW - 1 * M, app="VS Code")
    client.close_thread(f"thread:{x}", "done")
    t = W.current_thread(client, NOW, None)
    assert t is not None and t["ref"] == f"thread:{y}" and t["app"] == "Figma"
    assert t["in_front"] is False and t["min"] == 0


def test_a_long_untouched_thread_you_dismiss_still_reaches_the_closed_fold(store):
    client = MemoryClient(store)
    old = _thread(store, "Old paused idea", "The user sketched it.", at=NOW - 10 * 86400)
    for k in range(W.CLOSED_SHOWN * 3 + 2):                                       # many closed threads touched later
        tid = _thread(store, f"Done thing {k}", "Finished.", at=NOW - 5 * 86400 + k * 60)
        client.close_thread(f"thread:{tid}", "done")
    time.sleep(0.05)                                                              # a later closed_at than all of them
    client.close_thread(f"thread:{old}", "dismiss")                               # closed last, touched longest ago
    data = W.build_work(client, week={"days": [], "totals": {}}, now=time.time())
    assert data["closed"][0]["ref"] == f"thread:{old}" and data["closed"][0]["status"] == "dismissed"
    assert len(data["closed"]) == W.CLOSED_SHOWN


def test_home_has_no_in_front_tile_any_more(store):
    client = MemoryClient(store)
    _thread(store, "Yuki revamp", "The user built the window.", at=NOW - 5 * M, app="T3 Code")
    d = W.build_home(client, requests_csv=None, now=NOW)
    assert "front" not in d["now"] and d["now"]["thread"]["title"] == "Yuki revamp"


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
    assert win.a_correct_item({"ref": "thread:7", "words": "  that was  last week "}) == {"correction_id": 41, "ref": "thread:7"}
    assert mem.calls[-1] == ("correct", "thread:7", "that was last week") and "work" in refreshed
    assert win.a_correct_item({"ref": "thread:7", "words": "  "})["ok"] is False
    assert win.a_correct_item({"ref": "rule:1", "words": "x"})["error"] == "That can't be corrected here."
    view = W.pending_view(win.shown_pending(), ("thread:",))
    assert view["thread:7"]["state"] == "fixing" and view["thread:7"]["cid"] == 41 and view["thread:7"]["words"] == "that was last week"
    # the pass lands: the row says "Fixed" a while (this window only; the shared file no longer has it)
    mem.status[41] = "done"
    win._check_pending()
    assert "thread:7" not in win.pending and win.fixed["thread:7"]["state"] == "fixed"
    assert W.pending_view(win.shown_pending(), ("thread:",))["thread:7"]["state"] == "fixed"
    assert W.pending_view(win.shown_pending(), ("thread:",), now=time.time() + 3600) == {}
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
    assert all("next" in t for t in work["threads"]) and work["closed"] and work["can"]["close"]
    assert "front" not in home["now"] and home["now"]["thread"]["ref"].startswith("thread:")
    titles = [s["title"] for s in day["sessions"]]
    assert len(set(titles)) == len(titles) and not any(t.endswith("…") for t in titles)
    assert any(s.get("summary") and s.get("ref") for s in day["sessions"])
    assert all(m.get("heard") and m.get("guess") for m in me["misheard"]["items"]) and me["can"]["correct"]
    assert all(k in data["replies"] for k in ("close_thread", "thread_continue", "correct_item", "cancel_correction", "open_page"))
