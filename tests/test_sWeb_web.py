"""sWeb: `meet.open`, `youtube.home` and `youtube.video` (PLAN.md S-2; D8, D16, D17, D19; B.2; map-skills S2.1,
S2.2).

Invented data only, a temp database. The census, the tab selection, the focus, the browser launch and the reads of the
window in front are stand-ins (the real ones are made unreachable): nothing touches the desktop or the real memory.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from _sweb_world import NOW, SECRETS, Clock, open_store
from yuki.memory.api import MemoryClient
from yuki.perception.tabs import Candidate
from yuki.skills import connection as C
from yuki.skills import donecheck as D
from yuki.skills import engine as E
from yuki.skills import registry as R
from yuki.skills import web_route as W
from yuki.skills.resolve import Live

ZEN = 900
SERVICE = "Google Meet"


def _boom(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("a test reached the real desktop")


@pytest.fixture(autouse=True)
def guard(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from yuki.actions import launch, tabs
    from yuki.memory import gamegate
    from yuki.perception import windows

    for mod, name in ((launch, "focus_window"), (launch, "open_url"), (launch, "launch_app"), (tabs, "tab_action"),
                      (windows, "list_windows")):
        monkeypatch.setattr(mod, name, _boom)
    state: dict[str, Any] = {"game": "", "front": (0, ""), "windows": [(ZEN, 4321)], "census": None, "selected": [],
                             "select_ok": {}, "focused": [], "opened": [], "selected_now": None, "urls": None,
                             "board": None, "refusals": []}
    monkeypatch.setattr(gamegate, "game_in_front", lambda: state["game"])

    def refused(action: str, hwnd: int | None = None) -> bool:
        blocked = bool(state["game"]) or action in state.get("refuse", ())
        if blocked:
            state["refusals"].append(action)
        return blocked

    monkeypatch.setattr(W, "_refused", refused)
    monkeypatch.setattr(W, "_front", lambda: state["front"])
    monkeypatch.setattr(W, "_app_windows", lambda process: list(state["windows"]))

    def census(store: Any, service: str, windows: Any, log: Any) -> dict[str, Any]:
        state["census_calls"] = state.get("census_calls", 0) + 1
        log("tab_census", service=service, windows=len(windows), tabs=(state["census"] or {}).get("tabs"))
        return state["census"] or {"candidates": [], "decision": "none", "chosen": [], "tabs": 0}

    monkeypatch.setattr(W, "_census", census)

    def select(hwnd: int, tab_id: str) -> Any:
        state["selected"].append((hwnd, tab_id))
        ok = state["select_ok"].get(tab_id, True)
        if ok:
            state["selected_now"] = [tab_id]
        return SimpleNamespace(ok=ok, details={})

    monkeypatch.setattr(W, "_select_tab", select)

    def focus(hwnd: int) -> Any:
        state["focused"].append(hwnd)
        state["front"] = (hwnd, "zen.exe")
        return SimpleNamespace(ok=True, details={})

    monkeypatch.setattr(W, "_focus", focus)
    monkeypatch.setattr(W, "_selected_tabs", lambda hwnd: state["selected_now"])

    def open_url(url: str, app: str | None) -> Any:
        state["opened"].append((url, app))
        return SimpleNamespace(ok=True, details={})

    monkeypatch.setattr(W, "_open_url", open_url)
    monkeypatch.setattr(W, "_front_urls", lambda hwnd: state["urls"])
    monkeypatch.setattr(W, "_board_now", lambda wait_s=0.15: state["board"])
    return state


@pytest.fixture()
def client(tmp_path):
    C.clear_cache()
    store = open_store(tmp_path)
    c = MemoryClient(store)
    R.seed(store, C.load())
    yield c
    c.close()


def _ident(client: Any, **ident: Any) -> None:
    client.store.set_nudge_state(f"site_tabs:{SERVICE}", json.dumps({"process": "zen", **ident}))


def _cand(tab_id: str, *, match: str = "runtime_id", group: int = 0, index: int = 1, score: int = 1) -> Candidate:
    return Candidate(hwnd=ZEN, tab_id=tab_id, group=group, index=index, match=match, score=score)


def _census(*cands: Candidate, decision: str | None = None, tabs: int = 9) -> dict[str, Any]:
    chosen = [cands[0]] if cands else []
    if decision == "ask":
        chosen = list(cands)
    return {"candidates": list(cands), "decision": decision or ("one" if cands else "none"), "chosen": chosen,
            "tabs": tabs, "windows": 1}


def _ctx(client: Any, *, words: list[str], origin: str = "voice", log: list | None = None,
         game: Any = None) -> E.RunContext:
    logs = log if log is not None else []
    clock = Clock()
    return E.RunContext(origin=origin, words=words, memory=client, recheck="inline", game=game or (lambda: ""),
                        log=lambda event, **f: logs.append((event, f)), clock=clock.now, sleep=clock.sleep,
                        probes={"foreground": lambda ctx, args: (True, {"stub": True}),
                                "page_at": lambda ctx, args: (True, {"stub": True}),
                                "moment": lambda ctx, args: (None, {"stub": True}),
                                "media_session": lambda ctx, args: (True, {"stub": True})})


def _run(client: Any, key: str, slots: dict[str, Any], ctx: E.RunContext) -> E.SkillResult:
    return E.resolve_and_run(key, slots, ctx.words, ctx, live=Live())


# -- meet.open ---------------------------------------------------------------------------------------------------------


def test_open_my_meeting_selects_the_meet_tab_by_identity_then_brings_zen_forward(client, guard) -> None:
    _ident(client, runtime_ids=[{"id": "42.7.1", "pid": 4321, "at": NOW}])
    guard["census"] = _census(_cand("42.7.1"))
    logs: list = []
    got = _run(client, "meet.open", {}, _ctx(client, words=["open my meeting"], log=logs))
    assert got.outcome == "done" and got.done_line == "Your Meet tab is up in Zen.", got
    assert guard["selected"] == [(ZEN, "42.7.1")] and guard["focused"] == [ZEN]
    assert guard["opened"] == []                                 # no link opened, nothing typed, no Join click
    assert any(e == "tab_census" for e, _f in logs)
    assert json.dumps(logs, default=str).count("42.7.1") == 0     # the tab's id stays in RAM


def test_zens_tab_strip_unreadable_is_said_honestly_and_nothing_is_done(client, guard) -> None:
    _ident(client, names={"abc123": {"n": 3, "at": NOW}})
    guard["census"] = {"candidates": [], "decision": "none", "chosen": [], "tabs": 0, "diagnostics": [{}]}
    client.store._write(lambda conn: conn.execute(
        "UPDATE skills SET told_ciphertext = ? WHERE key = 'meet.open'",
        (client.store._enc(json.dumps({"link": "https://meet.google.com/aaa-bbbb-ccc"})),)))
    got = _run(client, "meet.open", {}, _ctx(client, words=["open my meeting"]))
    assert got.outcome == "asked" and got.ask == W.LINE_MEET_UNREADABLE and not got.done
    assert guard["selected"] == [] and guard["focused"] == [] and guard["opened"] == []   # not even the told link


def test_no_meet_tab_says_so_or_opens_the_link_the_user_told(client, guard) -> None:
    _ident(client, names={"abc123": {"n": 3, "at": NOW}})
    guard["census"] = _census(tabs=7)
    got = _run(client, "meet.open", {}, _ctx(client, words=["open my meeting"]))
    assert got.outcome == "asked" and got.ask == "There's no Meet tab open in Zen." and guard["focused"] == []
    client.store._write(lambda conn: conn.execute(
        "UPDATE skills SET told_ciphertext = ? WHERE key = 'meet.open'",
        (client.store._enc(json.dumps({"link": "https://meet.google.com/aaa-bbbb-ccc"})),)))
    opened: list = []
    ctx = _ctx(client, words=["open my meeting"])
    ctx.verbs = {"open_url": lambda call: opened.append(dict(call.args)) or E.RouteResult(ran=True, outcome="opened")}
    got = _run(client, "meet.open", {}, ctx)
    assert got.outcome == "done" and got.route == "A:told_link:open_url"
    assert opened == [{"url": "https://meet.google.com/aaa-bbbb-ccc", "app": "Zen"}]


def test_two_equal_meet_tabs_are_a_question_and_the_answer_picks_along_the_strip(client, guard) -> None:
    _ident(client, names={"abc123": {"n": 3, "at": NOW}})
    guard["census"] = _census(_cand("42.7.9", match="name", index=5, score=3),
                              _cand("42.7.2", match="name", index=2, score=3), decision="ask")
    got = _run(client, "meet.open", {}, _ctx(client, words=["open my meeting"]))
    assert got.outcome == "asked" and got.options == ["1", "2"] and guard["selected"] == []
    assert got.ask == "I see 2 Meet tabs in Zen. Which one, counting along the tab list: the first or the second?"
    got = _run(client, "meet.open", {"pick": 2}, _ctx(client, words=["the second one", "2"]))
    assert got.outcome == "done" and guard["selected"] == [(ZEN, "42.7.9")]   # index 5 is second along the strip


def test_a_candidate_that_wont_select_gives_way_to_the_next_and_none_is_said(client, guard) -> None:
    _ident(client, runtime_ids=[{"id": "42.7.1", "pid": 4321, "at": NOW}], names={"abc": {"n": 2}})
    guard["census"] = _census(_cand("42.7.1"), _cand("42.7.3", match="name", index=3))
    guard["select_ok"] = {"42.7.1": False}
    got = _run(client, "meet.open", {}, _ctx(client, words=["open my meeting"]))
    assert got.outcome == "done" and guard["selected"] == [(ZEN, "42.7.1"), (ZEN, "42.7.3")]
    guard["selected"].clear()
    guard["focused"].clear()
    guard["select_ok"] = {"42.7.1": False, "42.7.3": False}
    got = _run(client, "meet.open", {}, _ctx(client, words=["open my meeting"]))
    assert got.outcome == "asked" and got.ask == W.LINE_MEET_NO_ID and guard["focused"] == []


def test_meet_tab_never_learned_or_zen_closed_is_said_plainly(client, guard) -> None:
    got = _run(client, "meet.open", {}, _ctx(client, words=["open my meeting"]))
    assert got.outcome == "asked" and got.ask == W.LINE_MEET_UNKNOWN and not guard.get("census_calls")
    _ident(client, names={"abc123": {"n": 3}})
    guard["windows"] = []
    got = _run(client, "meet.open", {}, _ctx(client, words=["open my meeting"]))
    assert got.outcome == "asked" and got.ask == W.LINE_ZEN_CLOSED and not guard.get("census_calls")


def test_the_meet_done_check_needs_zen_in_front_on_that_very_tab(client, guard) -> None:
    res = E.RouteResult(ran=True, facts_ram={"hwnd": ZEN, "tab_id": "42.7.1"})
    ctx = D.ProbeContext(since=NOW, result=res)
    guard["front"] = (ZEN, "zen.exe")
    guard["selected_now"] = ["42.7.1"]
    assert W.probe_meet_shown(ctx, {"process": "zen.exe"})[0] is True
    guard["selected_now"] = ["42.7.4"]
    assert W.probe_meet_shown(ctx, {"process": "zen.exe"})[0] is False
    guard["front"] = (77, "code.exe")
    assert W.probe_meet_shown(ctx, {"process": "zen.exe"})[0] is False
    guard["front"] = (ZEN, "zen.exe")
    guard["selected_now"] = None
    assert W.probe_meet_shown(ctx, {"process": "zen.exe"})[0] is None      # unreadable: never a "done"


def test_a_game_in_front_holds_meet_open_and_a_refused_focus_is_never_followed(client, guard) -> None:
    _ident(client, runtime_ids=[{"id": "42.7.1", "pid": 4321, "at": NOW}])
    guard["census"] = _census(_cand("42.7.1"))
    guard["game"] = "skyrunner"
    got = _run(client, "meet.open", {}, _ctx(client, words=["open my meeting"], game=lambda: "skyrunner"))
    assert got.outcome == "held_game" and got.say == "You're in a game: after the round?"
    assert not guard.get("census_calls") and guard["selected"] == [] and guard["focused"] == []
    guard["game"] = ""
    guard["refuse"] = ("focus",)                  # the game came up right before Zen would come forward
    got = _run(client, "meet.open", {}, _ctx(client, words=["open my meeting"]))
    assert got.outcome == "held_game" and guard["focused"] == [] and got.handoff is None


# -- the browser ------------------------------------------------------------------------------------------------------


def test_the_browser_rule_named_then_in_front_then_recent_then_usual_then_most() -> None:
    m = [{"key": "arc", "name": "Arc"}, {"key": "chrome", "name": "Google Chrome"}]
    rule = W.browser_rule
    assert rule(named="chrome", said=True, front="arc", recent="arc", pref=None, measured=m) == ("chrome", "words")
    assert rule(named="chrome", said=False, front="arc", recent=None, pref="chrome", measured=m) == ("arc", "front")
    assert rule(named=None, said=False, front="code", recent="chrome", pref=None, measured=m) == ("chrome", "recent")
    assert rule(named="arc", said=False, front="code", recent=None, pref="arc", measured=m) == ("arc", "pref")
    assert rule(named=None, said=False, front="code", recent=None, pref=None, measured=m) == ("arc", "most")
    shown = [{"key": "arc", "site_last": 5.0}, {"key": "chrome", "site_last": 9.0}]
    assert rule(named=None, said=False, front=None, recent=None, pref=None, measured=shown) == ("chrome", "site")
    assert rule(named=None, said=False, front=None, recent=None, pref=None, measured=[]) == ("", "default")


def _timeline(store: Any, process: str, app: str, host: str | None, start: float, secs: float, *,
              by_yuki: int = 0) -> None:
    store._write(lambda conn: conn.execute(
        "INSERT INTO timeline(started_at, ended_at, state, process, app, host, page_key, active_s, updated_at, by_yuki)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)", (start, start + secs, "present", process, app, host,
                                         store.cipher.digest("p", process, str(start)), secs, start + secs, by_yuki)))


def _chrome(store: Any, start: float, secs: float = 600) -> None:
    for k, host in enumerate(("docs.example.org", "news.example.org", "shop.example.org")):
        _timeline(store, "chrome", "Google Chrome", host, start + k, secs)


def test_a_browser_is_measured_from_the_users_own_page_rows_never_a_list(client) -> None:
    s = client.store
    _timeline(s, "arc", "Arc", "www.youtube.com", NOW - 3 * 86400, 7200)          # showed YouTube: a browser
    _chrome(s, NOW - 2 * 86400)                                                     # three hosts: a browser
    _timeline(s, "code", "Visual Studio Code", None, NOW - 3600, 9000)              # no page: not a browser
    for k in range(5):                                                              # one site in its own window:
        _timeline(s, "chatter", "Chatter", "app.chatter.example", NOW - 7200 + k, 9000)   # a chat app, not a browser
    _timeline(s, "quillbrowse", "Quill", "a.example.org", NOW - 600, 9999, by_yuki=1)  # Yuki's own: never counts
    _timeline(s, "quillbrowse", "Quill", "b.example.org", NOW - 500, 9999, by_yuki=1)
    _timeline(s, "quillbrowse", "Quill", "c.example.org", NOW - 400, 9999, by_yuki=1)
    _timeline(s, "edgy", "Edgy", "news.example.org", NOW - 20 * 86400, 99999)      # too old
    got = W.measured_browsers(s, now=NOW, site_hosts=W.site_hosts("youtube"))
    assert [b["key"] for b in got] == ["arc", "chrome"] and got[1]["name"] == "Google Chrome"
    assert got[0]["site_last"] is not None and got[1]["site_last"] is None and got[1]["hosts"] == 3
    _timeline(s, "chrome", "Google Chrome", "x.example.org", NOW - 900, 60)
    assert W.last_in_front(s, ["arc", "chrome"], now=NOW) == "chrome"
    assert W.last_in_front(s, ["arc", "chrome"], now=NOW + 3600) is None              # older than 30 minutes
    # with nothing in front or recent and no usual: the browser YouTube was last shown in
    assert W.browser_rule(named=None, said=False, front="code", recent=None, pref=None, measured=got) == ("arc",
                                                                                                         "site")


def test_youtube_home_opens_home_in_the_browser_in_use_and_nothing_else(client, guard) -> None:
    s = client.store
    now = __import__("time").time()
    _timeline(s, "arc", "Arc", "www.youtube.com", now - 3 * 86400, 7200)
    _chrome(s, now - 2 * 86400)
    _timeline(s, "chrome", "Google Chrome", "x.example.org", now - 900, 60)          # in front 15 min ago
    guard["front"] = (55, "Code.exe")
    got = _run(client, "youtube.home", {}, _ctx(client, words=["put something on YouTube"]))
    assert got.outcome == "done" and got.done_line == "YouTube's up in Google Chrome.", got
    assert guard["opened"] == [("https://www.youtube.com/", "Google Chrome")]


def test_a_browser_named_in_their_words_wins_and_is_offered_as_their_usual(client, guard) -> None:
    s = client.store
    now = __import__("time").time()
    _chrome(s, now - 2 * 86400)
    _timeline(s, "chrome", "Google Chrome", "x.example.org", now - 600, 60)
    _timeline(s, "arc", "Arc", "www.youtube.com", now - 3 * 86400, 7200)
    guard["front"] = (56, "chrome.exe")
    got = _run(client, "youtube.home", {"browser": "Arc"}, _ctx(client, words=["YouTube in Arc please"]))
    assert got.outcome == "done" and got.done_line == "YouTube's up." and guard["opened"][-1] == (
        "https://www.youtube.com/", "Arc")
    assert got.ask_keep == {"slot": "browser", "pref": "youtube.home:browser", "option": "arc"}
    # with no browser in front or recent, their usual (the preference) is used, and named
    from yuki.skills import prefs

    prefs.keep_usual(s, "youtube.home:browser", "arc", "yes")
    guard["front"] = (57, "Code.exe")
    s._write(lambda conn: conn.execute("DELETE FROM timeline WHERE process = 'chrome'"))
    got = _run(client, "youtube.home", {}, _ctx(client, words=["YouTube"]))
    assert got.outcome == "done" and guard["opened"][-1] == ("https://www.youtube.com/", "Arc")
    assert got.done_line == "YouTube's up in Arc."


def test_youtube_video_opens_only_the_picked_videos_own_link(client, guard) -> None:
    assert W.video_link("abcDEF12345") == ("https://www.youtube.com/watch?v=abcDEF12345", "abcDEF12345")
    assert W.video_link("https://www.youtube.com/shorts/Zx9") == ("https://www.youtube.com/shorts/Zx9", "Zx9")
    assert W.video_link("https://www.example.org/watch?v=nope") is None
    assert W.video_link("a b") is None and W.video_link("") is None
    from yuki.skills.connection import load

    skill = load().get("youtube.video")
    route = skill.routes[0]
    ctx = _ctx(client, words=["the Quillon one"])
    call = E.RouteCall(skill, route, {"site": "youtube"}, {"video": {"name": "x", "ref": "abcDEF12345"}}, ctx, 0.0)
    guard["front"] = (58, "Code.exe")
    got = W.route_youtube_video(call)
    assert got.ran is True and got.facts_ram["video_id"] == "abcDEF12345"
    assert guard["opened"] == [("https://www.youtube.com/watch?v=abcDEF12345", None)]   # no browser known: default
    # the done-check: the very video in front; another video is a "no"
    guard["front"] = (58, "arc.exe")
    pctx = D.ProbeContext(since=NOW, result=got)
    guard["urls"] = ["https://www.youtube.com/watch?v=abcDEF12345&t=3"]
    assert W.probe_video_shown(pctx, {"site": "youtube"})[0] is True
    guard["urls"] = ["https://www.youtube.com/watch?v=other999"]
    assert W.probe_video_shown(pctx, {"site": "youtube"})[0] is False
    guard["urls"] = None
    assert W.probe_video_shown(pctx, {"site": "youtube"})[0] is None


def test_a_video_named_only_in_words_goes_to_the_thinker_and_nothing_auto_plays(client, guard) -> None:
    got = _run(client, "youtube.video", {"video": "the new Quillon one"},
               _ctx(client, words=["play the new Quillon one"]))
    assert got.outcome == "handed_over" and guard["opened"] == []
    assert got.handoff["done"]["call"] == "video_shown"
    # nothing names a video: never filled from taste (no video slot taste), so nothing plays unasked
    got = _run(client, "youtube.video", {}, _ctx(client, words=["play a video"]))
    assert got.outcome == "handed_over" and guard["opened"] == [] and "video" not in got.values


def test_the_youtube_recheck_reads_memorys_own_view_of_the_window_in_front(client, guard) -> None:
    pctx = D.ProbeContext(since=NOW)
    assert W.probe_page_seen(pctx, {"site": "youtube", "kind": "home"})[0] is None
    guard["board"] = {"windows": [{"foreground": True, "read_at": NOW + 1, "url": "https://www.youtube.com/"}]}
    assert W.probe_page_seen(pctx, {"site": "youtube", "kind": "home"})[0] is True
    guard["board"] = {"windows": [{"foreground": True, "read_at": NOW + 1, "url": "https://www.youtube.com/watch?v=x"}]}
    assert W.probe_page_seen(pctx, {"site": "youtube", "kind": "home"})[0] is False
    guard["board"] = {"windows": [{"foreground": True, "read_at": NOW - 5, "url": "https://www.youtube.com/"}]}
    assert W.probe_page_seen(pctx, {"site": "youtube", "kind": "home"})[0] is None    # read before the run: stale


def test_a_game_in_front_holds_youtube(client, guard) -> None:
    guard["game"] = "skyrunner"
    for key in ("youtube.home", "youtube.video"):
        got = _run(client, key, {}, _ctx(client, words=["YouTube"], game=lambda: "skyrunner"))
        assert got.outcome == "held_game"
    assert guard["opened"] == []


def test_logs_are_content_free(client, guard) -> None:
    s = client.store
    now = __import__("time").time()
    _timeline(s, "quillbrowse", "Thistle Hollow Browser", "www.youtube.com", now - 600, 60)
    guard["front"] = (59, "Code.exe")
    logs: list = []
    got = _run(client, "youtube.home", {}, _ctx(client, words=["YouTube"], log=logs))
    assert got.outcome == "done"
    text = json.dumps(logs, default=str)
    for secret in SECRETS:
        assert secret not in text
