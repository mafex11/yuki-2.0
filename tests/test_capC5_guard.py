"""capC5 (report 78 §7.2, §7.4; report 102): the replay guard and the anchors of run_skill.

A learned way is replayed against its recorded path (``yuki.memory.trace_replay.way_path``): a step whose observed
state leaves the path - a different app, window or site than the recording, a dialog the recordings never showed, the
control missing or ambiguous - stops the run and hands over ("I stopped at step N: ..."); a timestamp or an unread
count in a title and a moved window never stop a good run. Deviations are counted per way and rank it lower, never
out. The user's takeover stops the run and is not counted against the way. Anchors in order: a proven AutomationId,
the role and name, a pick by digest within its container, and last the RuntimeId of the very control - only within
one session.

Offline: a stand-in desk (windows, trees, a dispatcher) and temp databases; invented apps, names and sites.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from test_c_practice import vec
from test_capC2_links import NOW, store  # noqa: F401 - the store fixture
from test_capC4_demos import answer, chat_demo, demos, learner, taskrow
from test_capC4_skills import Desk, el, item_of, memory_for
from yuki.memory import practice as P
from yuki.memory import trace_replay as R
from yuki.voice import skills as S


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    for name, value in (("FIRST_FIND_WAIT_S", 0.0), ("FIND_WAIT_S", 0.0), ("AFTER_OPEN_WAIT_S", 0.0),
                        ("FOCUS_WAIT_S", 0.0), ("EXPECT_WAIT_S", 0.0), ("END_WAIT_S", 0.0), ("POLL_S", 0.0)):
        monkeypatch.setattr(S, name, value)
    S._RIDS.clear()
    R.set_use_learned(True)
    yield
    R.set_use_learned(None)
    S._RIDS.clear()


class Desk5(Desk):
    """The C4 desk with Windows' owner facts (a dialog in front the window list leaves out) and a judge that records
    what it was shown."""

    def __init__(self, windows: dict[int, dict[str, Any]], trees: dict[int, list[dict[str, Any]]], front: int, *,
                 path: dict[str, Any] | None = None, memory: Any = None, session: str = "s1") -> None:
        if memory is None:
            client = SimpleNamespace(way_path=lambda pid: path or {"steps": [], "recorded": False})
            memory = SimpleNamespace(call=lambda what, fn, timeout_s=1.0: fn(client), session_id=session)
        super().__init__(windows, trees, front, memory=memory)
        self.fg_owner = 0
        self.judged: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def overview_text(self) -> tuple[str, dict[str, Any], float]:
        text, payload, ms = super().overview_text()
        payload["foreground_owner_hwnd"] = self.fg_owner
        return text, payload, ms

    def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        # report 58's account as the real dispatcher gives it: the window in front after the action, and whether it
        # changed (second verification N5 reads it)
        before = self.front
        out = super().dispatch(name, args)
        out.payload["details"].update(foreground_changed=self.front != before, foreground_hwnd=self.front)
        return out

    def authorize(self, tool: str, tool_input: dict[str, Any], target: dict[str, Any], earlier: list[str],
                  force: bool = False) -> None:
        self.judged.append((tool, dict(tool_input), dict(target)))
        return None


def rec(app: str = "sheets", *, same: bool = False, dialog: bool = False, host: str = "") -> dict[str, Any]:
    return {"known": True, "app": app, "host": host, "same_window": same, "dialog": dialog}


def path_of(*steps: dict[str, Any], dialog_apps: list[str] = ()) -> dict[str, Any]:
    return {"steps": list(steps), "dialog_apps": list(dialog_apps), "recorded": True}


def btn(name: str) -> dict[str, Any]:
    return {"do": "click", "role": "Button", "name": name}


def way(*steps: dict[str, Any], apps: tuple[str, ...] = ("sheets",), **over: Any) -> dict[str, Any]:
    return item_of([{"run": list(steps), "end": {"kind": "selected", "app": apps[0]}}], apps=list(apps), trace=True,
                   demo=True, **over)


def sheets_desk(path: dict[str, Any], *, export_in: tuple[int, ...] = (100,), extra: dict[int, dict] | None = None,
                trees: dict[int, list[dict[str, Any]]] | None = None) -> Desk5:
    windows = {100: {"title": "(3) Reports - Sheets", "process_name": "sheets", "bounds": [0, 0, 800, 600]},
               200: {"title": "Notes", "process_name": "notesy"}, **(extra or {})}
    base = {100: [el(1, "Button", "Reports")], 200: [], 300: []}
    for h in export_in:
        base.setdefault(h, []).append(el(5 + h, "Button", "Export"))
    base.update(trees or {})
    return Desk5(windows, base, 100, path=path)


def clicks(desk: Desk) -> list[int]:
    return [c[1]["element"] for c in desk.calls if c[0] == "click"]


def run(desk: Desk, item: dict[str, Any], words: list[str] | None = None) -> tuple[bool, str, dict[str, Any]]:
    return S.SkillRunner(desk).run({"steps": []}, item=item, words=words or ["export the reports"])


# -- the replay guard: stops and hands over ------------------------------------------------------------------------

def test_a_good_run_on_its_recorded_path_runs_every_step():
    desk = sheets_desk(path_of(rec(), rec(same=True)))
    ok, report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert ok and facts["verified"] is True and clicks(desk) == [1, 105]


def test_a_different_window_of_the_same_app_stops_and_hands_over():
    desk = sheets_desk(path_of(rec(), rec(same=True)), export_in=(300,),
                       extra={300: {"title": "Budget - Sheets", "process_name": "sheets"}})
    desk.effects[1] = lambda: setattr(desk, "front", 300)       # another sheets window comes to the front
    ok, report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert not ok and facts["deviated_at"] == 2 and facts["deviation"] == "window"
    assert report.startswith('I stopped at step 2: Button "Export" was in another Sheets window')
    assert facts["handover"].startswith("I stopped at step 2:") and "skill_context" in facts
    assert clicks(desk) == [1]                                  # nothing clicked in the other window


def test_a_different_app_stops_and_hands_over():
    desk = sheets_desk(path_of(rec(), rec("sheets")), export_in=(200,))
    desk.effects[1] = lambda: setattr(desk, "front", 200)
    ok, report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert not ok and facts["deviation"] == "app"
    assert 'I stopped at step 2: Button "Export" was only in Notesy, not in Sheets as when you did it' in report
    assert clicks(desk) == [1]


def test_a_different_site_stops_but_the_same_site_with_another_address_does_not():
    doc = lambda url: {"id": 90, "role": "Document", "name": "", "value": url}   # noqa: E731
    path = path_of(rec(host="mail.example.com"), rec(same=True, host="mail.example.com"))
    other = sheets_desk(path, trees={100: [el(1, "Button", "Reports"), el(105, "Button", "Export"),
                                           doc("https://other.example.org/inbox")]})
    ok, report, facts = run(other, way(btn("Reports"), btn("Export")))
    assert not ok and facts["deviation"] == "page" and facts["deviated_at"] == 1
    assert "a different site was open (other.example.org, not mail.example.com as when you did it)" in report
    assert clicks(other) == []
    same = sheets_desk(path, trees={100: [el(1, "Button", "Reports"), el(105, "Button", "Export"),
                                          doc("https://WWW.Mail.example.com/inbox?at=10:42&unread=4")]})
    ok, _report, facts = run(same, way(btn("Reports"), btn("Export")))
    assert ok and facts["verified"] is True


def test_a_dialog_the_recordings_never_showed_stops_one_they_showed_does_not():
    def with_dialog(path: dict[str, Any]) -> Desk5:
        desk = sheets_desk(path, export_in=(101,), trees={100: [el(1, "Button", "Reports")]})
        desk.effects[1] = lambda: (desk.windows.__setitem__(101, {"title": "Unsaved changes", "process_name": "sheets",
                                                                  "owner_hwnd": 100}), setattr(desk, "front", 101))
        return desk

    desk = with_dialog(path_of(rec(dialog=False), rec(same=True)))
    ok, report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert not ok and facts["deviation"] == "dialog" and facts["deviated_at"] == 1
    assert 'I stopped at step 1: a dialog came up ("Unsaved changes") that never came up when you did it' in report
    assert clicks(desk) == [1]
    desk = with_dialog(path_of(rec(dialog=True), rec(same=True)))
    ok, _report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert ok and clicks(desk) == [1, 106]                     # the next step acts inside the dialog it expected


def test_a_dialog_in_front_that_the_window_list_leaves_out_is_seen():
    desk = sheets_desk(path_of(rec(dialog=False), rec(same=True)))

    def unlisted() -> None:
        desk.front, desk.fg_owner = 555, 100                    # Windows' owner fact; the list leaves it out
    desk.effects[1] = unlisted
    ok, report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert not ok and facts["deviation"] == "dialog" and "a dialog came up" in report


def test_a_way_with_no_recording_takes_a_dialog_only_when_the_next_step_acts_inside_it():
    desk = sheets_desk({"steps": [], "recorded": False}, export_in=(101,), trees={100: [el(1, "Button", "Reports")]})
    desk.effects[1] = lambda: desk.windows.__setitem__(101, {"title": "Export options", "process_name": "sheets",
                                                             "owner_hwnd": 100})
    ok, _r, _f = run(desk, way(btn("Reports"), btn("Export")))
    assert ok and clicks(desk) == [1, 106]
    desk = sheets_desk({"steps": [], "recorded": False})
    desk.effects[1] = lambda: desk.windows.__setitem__(101, {"title": "Sign in", "process_name": "sheets",
                                                             "owner_hwnd": 100})
    ok, report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert not ok and facts["deviation"] == "dialog" and "the way never goes through" in report
    assert clicks(desk) == [1]


def test_the_target_missing_or_ambiguous_stops_and_hands_over():
    desk = sheets_desk(path_of(rec(), rec(same=True)), export_in=())
    ok, report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert not ok and facts["deviation"] == "not_found"
    assert report.startswith('I stopped at step 2: Button "Export" was not on the screen.')
    desk = sheets_desk(path_of(rec(), rec(same=True)),
                       trees={100: [el(1, "Button", "Reports"), el(7, "Button", "Export"), el(8, "Button", "Export")]})
    ok, report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert not ok and facts["deviation"] == "not_found" and "2 controls fit" in report and clicks(desk) == [1]


# -- harmless differences never stop a good run --------------------------------------------------------------------

def test_a_timestamp_or_unread_count_in_a_title_and_a_moved_window_do_not_stop_a_run():
    desk = sheets_desk(path_of(rec(), rec(same=True)))

    def churn() -> None:
        desk.windows[100].update(title="(12) Reports - Sheets - saved 10:42:07", bounds=[300, 200, 1100, 800])
        desk.windows[200].update(title="Notes (5 unread) 10:42")
    desk.effects[1] = churn
    ok, report, facts = run(desk, way(btn("Reports"), btn("Export")))
    assert ok and facts["verified"] is True and clicks(desk) == [1, 105]


# -- the path itself, from the user's recording --------------------------------------------------------------------

def _chat_way(store: Any) -> P.Playbook:
    t1, s1 = chat_demo(store, NOW)
    t2, s2 = chat_demo(store, NOW + 86400)
    ans = lambda s: answer(s, slots={s[2]: "channel", s[4]: "message"})   # noqa: E731
    lrn = learner(store, [ans(s1), ans(s2)])
    lrn.learn_task(taskrow(store, t1), now=NOW + 120)
    lrn.learn_task(taskrow(store, t2), now=NOW + 86400 + 120)
    [o] = demos(store)
    return o


def test_the_recorded_path_comes_from_the_users_own_rows_one_per_step(store):
    o = _chat_way(store)
    path = R.way_path(store, o.id)
    assert path["recorded"] and len(path["steps"]) == len(R.way_steps(o)) == 6
    assert all(s["known"] and s["app"] == "chatter" for s in path["steps"])
    # the switch row moves to the app; the next keys stay in its window (no window change recorded between them)
    assert [s["same_window"] for s in path["steps"]] == [False, True, True, True, True, True]
    assert not any(s["dialog"] for s in path["steps"]) and path["dialog_apps"] == []
    # a way not learned from the trace has no recorded path: the runner then holds it to the apps it names
    pid = P.add_playbook(store, {"kind": "procedure", "intent": "pause the video", "apps": ["browser"],
                                 "body": {"steps": ["press k"]}}, events=[{"type": "observed", "run_key": "r"}])
    assert R.way_path(store, pid) == {"steps": [], "dialog_apps": [], "recorded": False}
    assert R.way_path(store, 999999)["recorded"] is False


def test_yukis_own_rows_never_count_as_the_users_recorded_steps(store):
    o = _chat_way(store)
    actions = o.body["trace"]["actions"]
    store._write(lambda conn: conn.execute("UPDATE ui_actions SET source = 'yuki' WHERE id = ?", (actions[1],)))
    path = R.way_path(store, o.id)
    assert path["steps"][1] == {"known": False}               # not the user's: nothing known of it
    # and the run checks such a step only against the apps the way names
    runner = S.SkillRunner(SimpleNamespace(memory=memory_for(store)))
    item = {"id": o.id, "trace": True, "apps": ["chatter"]}
    steps = [S.way_step(i + 1, s) for i, s in enumerate(R.way_steps(o))]
    exp = runner.expectations(item, steps, 0)
    assert exp[1].recorded is False and exp[1].apps == frozenset({"chatter"}) and exp[0].recorded is True


# -- deviations per way: lower, never out --------------------------------------------------------------------------

def _hand_run(store: Any, rid: str, pid: int, outcome: str, at: float) -> None:
    P.record_run(store, {"source": "hand", "session_id": "s", "request_id": rid, "at": at, "ended_at": at + 5,
                         "outcome": outcome, "cited": [pid], "steps": [{"tool": "click", "kind": "action", "ok": True,
                                                                         "at": at}]}, now=at + 5)


def test_a_way_that_keeps_deviating_ranks_lower_never_out_and_a_good_run_clears_it(store):
    ev = [{"type": "observed", "run_key": "r1"}, {"type": "succeeded", "run_key": "r1"}]
    a = P.add_playbook(store, {"kind": "procedure", "intent": "export the weekly reports", "apps": ["sheets"],
                               "body": {"steps": ["Reports", "Export"]}}, events=ev,
                       vector=vec("export the weekly reports"))
    b = P.add_playbook(store, {"kind": "procedure", "intent": "export the weekly reports now", "apps": ["sheets"],
                               "body": {"steps": ["Export"]}}, events=ev,
                       vector=vec("export the weekly reports now"))
    q = vec("export the weekly reports")
    assert [w["id"] for w in P.relevant(store, query_vec=q, apps_in_play=["sheets"], now=NOW)["ways"]][0] == a
    for k in range(3):
        _hand_run(store, f"d{k}", a, "handed_over", NOW - 3000 + k * 100)
    assert R.deviations(store, [a, b], now=NOW) == {a: 3}
    ways = P.relevant(store, query_vec=q, apps_in_play=["sheets"], now=NOW)["ways"]
    assert [w["id"] for w in ways] == [b, a]                  # lower, still there
    assert ways[1]["deviations"] == 3 and "ranked lower, still allowed" in ways[1]["why"]
    assert R.deviation_factor(3) == R.deviation_factor(9) == pytest.approx(0.7 ** 3)
    _hand_run(store, "good", a, "final", NOW - 100)           # a verified run since: the count starts again
    assert R.deviations(store, [a], now=NOW) == {}
    assert [w["id"] for w in P.relevant(store, query_vec=q, apps_in_play=["sheets"], now=NOW)["ways"]][0] == a


# -- takeover (report 63) ------------------------------------------------------------------------------------------

class Watch:
    def __init__(self, on_takeover: Any) -> None:
        self.fired: str | None = None
        self.started = self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def test_the_users_takeover_stops_the_run_at_once_and_is_not_counted_against_the_way():
    desk = sheets_desk(path_of(rec(), rec(same=True)))
    made: list[Watch] = []

    def factory(on_takeover: Any) -> Watch:
        made.append(Watch(on_takeover))
        return made[-1]

    desk.effects[1] = lambda: setattr(made[0], "fired", "keys")   # the user starts typing after step 1
    ok, report, facts = S.SkillRunner(desk, takeover=factory).run({"steps": []}, item=way(btn("Reports"),
                                                                                          btn("Export")),
                                                                   words=["export the reports"])
    assert not ok and clicks(desk) == [1] and made[0].started and made[0].stopped
    assert facts["took_over_at"] == 2 and facts["deviated_at"] is None and facts["deviation"] is None
    assert facts["settled"] is True and "skill_context" not in facts   # nothing handed over: it is theirs
    assert report.startswith("I stopped at step 2: you took over.")
    steps = S.trace_steps(facts["results"], 9)
    assert steps[-1]["tool"] == S.TAKEOVER_TOOL == R.TAKEOVER_TOOL


# -- anchors, in order ---------------------------------------------------------------------------------------------

def test_a_proven_automation_id_comes_first_then_the_role_and_name():
    tree = [el(1, "Button", "Export", aid="btnExportCsv"), el(2, "Button", "Export", aid="btnExportPdf")]
    desk = Desk5({100: {"title": "Sheets", "process_name": "sheets"}}, {100: tree}, 100)
    item = way({**btn("Export"), "aid": "btnExportPdf"})
    ok, _r, facts = run(desk, item)
    assert ok and clicks(desk) == [2]                          # the id told the two apart
    # the id's control must carry the name too (a generic id shared by other controls is no identity)
    desk = Desk5({100: {"title": "Sheets", "process_name": "sheets"}},
                 {100: [el(1, "Button", "Print", aid="btnExportPdf"), el(2, "Button", "Export")]}, 100)
    ok, _r, _f = run(desk, item)
    assert ok and clicks(desk) == [2]                          # role and name decided
    # no id: role and name
    desk = Desk5({100: {"title": "Sheets", "process_name": "sheets"}}, {100: [el(4, "Button", "Export")]}, 100)
    ok, _r, _f = run(desk, way(btn("Export")))
    assert ok and clicks(desk) == [4]


def test_a_pick_by_digest_stays_inside_its_container(store):
    from yuki.memory import trace_demos as TD

    digest = TD.name_digest(store, "tunes", "ListItem", "Road Trip")[:P.PICK_DIGEST_CHARS]
    box = TD.container_digest(store, "tunes", "List", "lstPlaylists", "")[:P.PICK_DIGEST_CHARS]
    tree = [el(1, "List", "Playlists", aid="lstPlaylists"), el(10, "ListItem", "Focus Beats", parent=1),
            el(11, "ListItem", "Road Trip", parent=1),
            el(2, "List", "Recent", aid="lstRecent"), el(20, "ListItem", "Road Trip", parent=2)]
    desk = Desk5({100: {"title": "Tunes", "process_name": "tunes"}}, {100: tree}, 100, memory=memory_for(store))
    pick = {"do": "pick", "in": {"role": "List", "digest": box}, "role": "ListItem", "app": "tunes", "item": digest}
    ok, _r, _f = run(desk, way(pick, apps=("tunes",)))
    assert ok and clicks(desk) == [11]                         # never the same name in the other list
    # without the container's identity, the same name in two lists is not clear: it stops
    loose = Desk5({100: {"title": "Tunes", "process_name": "tunes"}}, {100: tree}, 100, memory=memory_for(store))
    ok, report, _f = run(loose, way({**pick, "in": {"role": "List"}}, apps=("tunes",)))
    assert not ok and "2 items match" in report and clicks(loose) == []


def _rid(i: int, name: str, rid: list[int]) -> dict[str, Any]:
    return {**el(i, "ListItem", name, parent=1), "runtime_id": rid}


def test_the_runtime_id_finds_the_very_control_again_only_within_one_session():
    def chat_desk(name: str, *, session: str, hwnd: int = 100) -> Desk5:
        tree = [el(1, "List", "Chats"), _rid(11, name, [42, 7, 3]), _rid(12, "Other chat", [42, 7, 4])]
        return Desk5({hwnd: {"title": "Chatter", "process_name": "chatter"}}, {hwnd: tree}, hwnd, session=session)

    step = {"do": "click", "role": "ListItem", "name": "Team room"}
    first = chat_desk("Team room", session="s1")
    ok, _r, _f = run(first, way(step, apps=("chatter",)))
    assert ok and clicks(first) == [11]
    # the same session, the same window: the unread count changed its name; its RuntimeId finds it again
    again = chat_desk("Team room, 3 unread", session="s1")
    again.memory = first.memory
    runner = S.SkillRunner(again)
    runner._session = S.session_of(first)                      # the very same voice session's hands
    ok, report, facts = runner.run({"steps": []}, item=way(step, apps=("chatter",)), words=["open the team room"])
    assert ok and clicks(again) == [11] and "an earlier step of this session" in facts["results"][0].why
    # another session (RuntimeIds do not survive a restart): never re-found by it; it stops instead
    other = chat_desk("Team room, 3 unread", session="s2")
    ok, report, facts = run(other, way(step, apps=("chatter",)))
    assert not ok and facts["deviation"] == "not_found" and clicks(other) == []
    # the same session but another window of the app (restarted: a new window): not re-found either
    moved = chat_desk("Team room, 3 unread", session="s1", hwnd=900)
    runner = S.SkillRunner(moved)
    runner._session = S.session_of(first)
    ok, _r, facts = runner.run({"steps": []}, item=way(step, apps=("chatter",)), words=["open the team room"])
    assert not ok and clicks(moved) == []


def test_runtime_ids_kept_are_bounded_and_expire():
    step = S.SkillStep(i=1, do="click", role="Button", name="Export")
    S.remember_rid("s", step, 100, "sheets", {"runtime_id": [1, 2], "role": "Button"}, now=NOW)
    hit = {"id": 5, "runtime_id": [1, 2], "role": "Button"}
    assert S.recall_rid("s", step, 100, "sheets", [hit], now=NOW + 60) == hit
    assert S.recall_rid("s", step, 100, "sheets", [hit], now=NOW + S.RID_KEEP_S + 1) is None
    assert S.recall_rid("other", step, 100, "sheets", [hit], now=NOW + 60) is None
    assert S.recall_rid("s", step, 100, "notesy", [hit], now=NOW + 60) is None
    for k in range(S.RID_KEEP_N + 20):
        S.remember_rid("s", S.SkillStep(i=1, do="click", role="Button", name=f"b{k}"), 100, "sheets",
                       {"runtime_id": [k], "role": "Button"}, now=NOW + k)
    assert len(S._RIDS) == S.RID_KEEP_N


# -- each step verified by report 58's before/after account ----------------------------------------------------------

def test_the_real_hands_run_a_way_through_a_dispatcher_with_the_before_after_account_on():
    from yuki.agent.tools import Dispatcher

    class Hands:   # the real hands are a plain object (kept by a weak reference)
        def __init__(self, backend: Any) -> None:
            self.backend = backend
            self._dispatcher = Dispatcher(backend, max_tree_elements=300, tree_timeout_s=0.8, verify_outcomes=False)

    hands = Hands(object())
    disp = S.skill_dispatcher(hands)
    assert disp is not hands._dispatcher and disp._verify_choice is True     # the talker's quick hands keep theirs
    assert (disp.max_tree_elements, disp.tree_timeout_s) == (S.SKILL_TREE_ELEMENTS, S.SKILL_TREE_TIMEOUT_S)
    assert S.skill_dispatcher(hands) is disp                                 # made once per set of hands
    assert S.skill_dispatcher(Desk5({}, {}, 0)) is not None                  # a stand-in desk: its own, as before
