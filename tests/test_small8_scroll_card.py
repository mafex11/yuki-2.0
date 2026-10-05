"""small8 (report 73, release 7, item 1): the done cards of scroll runs that worked said "Didn't work".

Three causes, rebuilt here with invented windows and words:

* Page Down / Page Up in a text document moved the view (line 30 to 186) but its scroll position change was set aside
  as "its content grew" (the only elements under the document are its own scroll bar's buttons, which never move), so
  the window's account said "only things that change on their own changed";
* a key or click whose window read could not decide was still "contradicted" by focus, title and page staying the same
  (a web page drawn where it cannot be read; a status bar's line number);
* three clicks refused before anything was sent (no control at that point) graded the request "failed" although the
  brain's own closing claim said the scroll got done.

Fakes and stand-ins only: no desktop, no network, no real memory, nothing sent.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.agent.finish import (LIKELY, SEEN_REFUSED, VERIFIED, honest_text, outcome_verdict, sayable)
from yuki.agent.loop import action_verdict
from yuki.agent.tools import ToolOutcome
from yuki.perception import tree as T
from yuki.perception.outcome import describe, diff, verdict


# -- 1. the window's account: the same view size is the view moving ---------------------------------------------------


def _doc_window(position: float, *, view: float | None, line: int) -> list[dict[str, Any]]:
    """A text editor: its Document scrolls (no element per line), its vertical scroll bar's parts sit under it and
    never move, and the status bar's read-only text says the line."""
    def el(i: int, role: str, name: str, parent: int, box: tuple[int, int, int, int], **kw: Any) -> dict[str, Any]:
        return {"id": i, "role": role, "name": name, "parent": parent, "depth": 0 if parent < 0 else 1,
                "bounds": box, "center": ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), "states": (),
                "value": None, "is_interactive": False, "is_scrollable": False, **kw}

    return [
        el(0, "Window", "notes.txt - Editor", -1, (0, 0, 1000, 800)),
        el(1, "Document", "Text editor", 0, (0, 40, 980, 760), is_interactive=True, is_scrollable=True,
           scroll=(-1.0, position), scroll_view=((-1.0, view) if view is not None else None)),
        el(2, "ScrollBar", "Vertical", 1, (980, 40, 1000, 760)),
        el(3, "Button", "Line up", 2, (980, 40, 1000, 60)),
        el(4, "Button", "Line down", 2, (980, 740, 1000, 760)),
        el(5, "Button", "Page up", 2, (980, 60, 1000, 100)),
        el(6, "Text", "Status", 0, (0, 760, 1000, 800), value=f"Ln {line}, Col 1"),
    ]


def test_page_down_in_a_document_whose_view_size_holds_is_the_view_moving() -> None:
    before = _doc_window(8.0, view=12.5, line=30)
    after = _doc_window(49.6, view=12.5, line=186)
    change = diff(before, after)                      # a key press: no scroll point
    assert change.scrolled and not change.scroll_aside and change.content_moved
    assert change.scrolled[0][1:] == ("v", 8.0, 49.6)
    ok, why = verdict(change)
    assert ok is True and "its content moved" in why
    assert any("scrolled down from 8% to 50% of the way" in ln for ln in describe(change))


def test_the_live_failure_without_a_view_size_is_kept_as_it_was() -> None:
    # a provider that does not report its view size: the old rule stands (what stayed put says "grew")
    change = diff(_doc_window(8.0, view=None, line=30), _doc_window(49.6, view=None, line=186))
    assert not change.scrolled and change.scroll_aside[0][4] == "grew"
    assert verdict(change)[0] is None


def test_content_that_grew_past_the_view_changes_the_view_size_and_is_still_set_aside() -> None:
    # an infinite feed that loaded more below: the position falls and the view shows a smaller share of the content
    change = diff(_doc_window(100.0, view=20.0, line=30), _doc_window(85.0, view=17.0, line=30))
    assert not change.content_moved and change.scroll_aside[0][4] == "grew"
    # a view size the provider reports as 100% (nothing to scroll) is never read as the same content
    full = diff(_doc_window(10.0, view=100.0, line=30), _doc_window(30.0, view=100.0, line=30))
    assert not full.content_moved


def test_a_scroll_elsewhere_with_the_same_view_size_is_still_elsewhere() -> None:
    before, after = _doc_window(10.0, view=12.5, line=30), _doc_window(40.0, view=12.5, line=30)
    change = diff(before, after, scroll=True, point=(500, 790), axes="v")   # made over the status bar, not the text
    assert not change.content_moved and change.scroll_aside[0][4] == "elsewhere"


def test_the_reader_keeps_the_view_size_of_a_scrolling_element() -> None:
    props = {T._P_SCROLL_H_VIEW_SIZE: -1, T._P_SCROLL_V_VIEW_SIZE: 12.5049, T._P_SCROLL_H_PERCENT: -1,
             T._P_SCROLL_V_PERCENT: 8.0}
    get = props.get
    assert T._scroll_position(get, (T._P_SCROLL_H_VIEW_SIZE, T._P_SCROLL_V_VIEW_SIZE)) == (-1.0, 12.5)
    assert T._scroll_position(get) == (-1.0, 8.0)
    assert T._scroll_position({}.get, (T._P_SCROLL_H_VIEW_SIZE, T._P_SCROLL_V_VIEW_SIZE)) is None
    assert T._P_SCROLL_V_VIEW_SIZE in T._OPTIONAL_PROPERTIES and T._P_SCROLL_H_VIEW_SIZE in T._OPTIONAL_PROPERTIES
    assert T.UIElement.__dataclass_fields__["scroll_view"].default is None


# -- 2. a key or click the window read could not decide is never contradicted ---------------------------------------


def _press(screen_change: dict[str, Any], **details: Any) -> ToolOutcome:
    facts = {"foreground_changed": False, "focus_changed": False, "foreground_title_changed": False,
             "page_changed": False, "window_state_changed": False}
    return ToolOutcome(name="press", ok=True, summary="pressed", content=[],
                       payload={"details": {**facts, "screen_change": screen_change, **details}})


def test_a_page_key_in_a_page_drawn_out_of_reach_is_not_confirmed_never_contradicted() -> None:
    why = "the window draws its own interface, so a change inside it cannot be seen"
    assert action_verdict("press", _press({"verified": None, "why": why})) == (None, why)


def test_a_click_that_changed_only_a_status_line_is_not_confirmed_never_contradicted() -> None:
    why = "only things that change on their own changed (a read-only text, a progress value, ...)"
    outcome = ToolOutcome(name="click", ok=True, summary="clicked", content=[],
                          payload={"details": {"focus_changed": False, "page_changed": False,
                                               "foreground_changed": False, "foreground_title_changed": False,
                                               "screen_change": {"verified": None, "why": why}}})
    assert action_verdict("click", outcome) == (None, why)


def test_a_window_read_that_saw_nothing_change_still_contradicts() -> None:
    nothing = "nothing visible changed in the window"
    out = _press({"verified": False, "why": nothing}, verified=False)
    assert action_verdict("press", out) == (False, nothing)
    # and with no window read at all, focus/title/page watched and unchanged is still a contradiction
    bare = ToolOutcome(name="press", ok=True, summary="pressed", content=[],
                       payload={"details": {"focus_changed": False, "page_changed": False}})
    assert action_verdict("press", bare)[0] is False


def test_a_page_key_whose_view_moved_is_verified() -> None:
    out = _press({"verified": True, "why": "the window changed: its content moved, 3 changed"}, verified=True)
    assert action_verdict("press", out)[0] is True


# -- 3. steps refused before they ran ---------------------------------------------------------------------------------


def _act(tool: str, *, ok: bool = True, verdict_: bool | None = True, app: str = "editor", **extra: Any) -> dict:
    return {"kind": "action", "tool": tool, "ok": ok, "verdict": verdict_, "why": "" if ok else "it failed",
            "app": app, "input": extra.pop("input", {}), **extra}


def _scroll_run() -> list[dict[str, Any]]:
    """The live scroll run: a verified scroll, then clicks to put the caret by the line number, all refused but one
    the status bar alone showed (not confirmed), and a command that exited cleanly."""
    return [_act("open_url"), _act("scroll", input={"element": 2, "dy": -10}),
            _act("click", ok=False, verdict_=False, refused=True, input={"x": 520, "y": 160}),
            _act("run_powershell", verdict_=None),
            _act("click", ok=False, verdict_=False, refused=True, input={"x": 600, "y": 145}),
            _act("click", verdict_=None, input={"element": 2}),
            _act("click", ok=False, verdict_=False, refused=True, input={"x": 958, "y": 140})]


def test_refused_side_clicks_never_make_a_scroll_that_worked_a_failure_when_the_claim_says_done() -> None:
    run = [s for s in _scroll_run() if not (s["tool"] == "click" and s["ok"])]   # the clicks were all refused
    for claims in ("done", "checked", "answer"):
        v = outcome_verdict(run, claims=claims)
        assert v["level"] == LIKELY and v["refused"] == 3, claims
        assert v["seen"] == "the command ran without an error" and v["label"] == "Likely done"   # its own weak check
        assert sayable(v) and honest_text("You're about 8% down, near line 30.", v, claims)[1] is None


def test_never_checked_past_a_refused_step() -> None:
    run = [_act("scroll"), _act("click", ok=False, verdict_=False, refused=True)]
    v = outcome_verdict(run, claims="checked")
    assert v["level"] == LIKELY != VERIFIED and v["seen"] == SEEN_REFUSED


def test_a_brain_that_says_it_tried_keeps_the_refused_step_as_the_failure() -> None:
    run = [s for s in _scroll_run() if not (s["tool"] == "click" and s["ok"])]
    for claims in ("tried", None):
        v = outcome_verdict(run, claims=claims)
        assert v["level"] == "failed" and v["refused"] == 0, claims


def test_a_step_that_ran_and_failed_still_decides() -> None:
    # the one click that ran is not confirmed: the request is not confirmed, never "likely" past it
    v = outcome_verdict(_scroll_run(), claims="done")
    assert v["level"] == "unverified" and v["refused"] == 3
    contradicted = [_act("scroll"), _act("press", verdict_=False, input={"key": "end"}),
                    _act("click", ok=False, verdict_=False, refused=True)]
    assert outcome_verdict(contradicted, claims="done")["level"] == "failed"


def test_nothing_reached_the_screen_or_a_refused_effect_is_never_set_aside() -> None:
    only_refused = [_act("click", ok=False, verdict_=False, refused=True)]
    assert outcome_verdict(only_refused, claims="done")["level"] == "failed"
    # a send or a close the user asked for, refused: it did not happen, whatever the claim
    send = _act("type_text", ok=False, verdict_=False, refused=True, input={"text": "hi", "_about": {"sends": True}})
    close = _act("window_action", ok=False, verdict_=False, refused=True, input={"action": "close"})
    for refused in (send, close):
        v = outcome_verdict([_act("focus_window"), refused], claims="done")
        assert v["level"] == "failed" and v["refused"] == 0


def test_review_f1_a_refused_attempt_after_only_a_focus_or_an_opening_stays_failed() -> None:
    """A focus (or an opening), then the very step asked for refused: nothing of it happened, whatever the claim, and
    the claim check still sees "done" below likely (it asks once)."""
    from yuki.agent.finish import claim_gap

    cases = [[_act("focus_window"), _act("scroll", ok=False, verdict_=False, refused=True, input={"dy": -5})],
             [_act("launch_app"), _act("type_text", ok=False, verdict_=False, refused=True, input={"text": "notes"})],
             [_act("focus_window"), _act("press", ok=False, verdict_=False, refused=True, input={"key": "pagedown"})]]
    for run in cases:
        for claims in ("done", "checked", "answer", "tried"):
            v = outcome_verdict(run, claims=claims)
            assert v["level"] == "failed" and v["refused"] == 0 and v["label"] == "Didn’t work", (run[1]["tool"], claims)
    assert claim_gap("done", outcome_verdict(cases[0], claims="done")) is not None


def test_review_f1_a_refused_content_step_needs_the_same_kind_to_have_worked_before_it() -> None:
    # a click worked, then the typing was refused: the text was never typed
    typed = [_act("click", input={"element": 3}), _act("type_text", ok=False, verdict_=False, refused=True)]
    assert outcome_verdict(typed, claims="done")["level"] == "failed"
    # a scroll worked, then another scroll was refused: the view was reached by the first
    scrolled = [_act("scroll"), _act("scroll", ok=False, verdict_=False, refused=True, input={"dy": 3})]
    assert outcome_verdict(scrolled, claims="done")["level"] == LIKELY
    # keys are one kind: a Page Down that worked covers a refused hotkey
    keys = [_act("press", input={"key": "pagedown"}), _act("hotkey", ok=False, verdict_=False, refused=True)]
    assert outcome_verdict(keys, claims="done")["level"] == LIKELY


def test_review_f1_a_refused_step_in_another_or_an_unknown_app_or_before_the_work_stays_failed() -> None:
    other_app = [_act("scroll"), _act("click", ok=False, verdict_=False, refused=True, app="browser")]
    unknown = [_act("scroll"), _act("click", ok=False, verdict_=False, refused=True, app="")]
    before = [_act("click", ok=False, verdict_=False, refused=True), _act("scroll")]
    for run in (other_app, unknown, before):
        assert outcome_verdict(run, claims="done")["level"] == "failed"


def test_a_refused_step_a_later_retry_put_right_is_not_counted() -> None:
    run = [_act("click", ok=False, verdict_=False, refused=True, control="button|Play"),
           _act("click", control="button|Play")]
    v = outcome_verdict(run, claims="done")
    assert v["level"] == VERIFIED and v["refused"] == 0


def test_the_done_card_of_the_scroll_run_is_not_didnt_work() -> None:
    from yuki.ui.step_words import level_row, return_view

    run = [s for s in _scroll_run() if s["tool"] in ("open_url", "scroll") or s.get("refused")]
    v = outcome_verdict(run, claims="checked")
    calls = [{"name": "scroll", "input": {"what": "Scrolling the notes down"}, "ok": True},
             {"name": "click", "input": {"x": 520, "y": 160}, "ok": False},
             {"name": "done", "input": {"achieved": "Scrolled your notes down"}, "ok": True}]
    view = return_view("done", "You're about 8% down.", calls, v)
    assert view["head"] != "Didn’t work" and view["icon"] != "x"
    assert level_row(v) == {"text": f"Likely done: {SEEN_REFUSED}", "icon": "check"}
    assert view["rows"][0] == level_row(v)


# -- 4. through the agent: a refusal is marked on the run trace, and the done's claim reaches the verdict -------------


@pytest.fixture()
def no_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """No live Now picture, and no real window lookups for the stub desk's invented window numbers."""
    import yuki.agent.tools as tools
    import yuki.memory.now as now

    monkeypatch.setattr(now, "shared_client", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_is_own_window", lambda hwnd: False)


def test_the_agent_marks_a_refused_step_and_grades_by_the_dones_claim(tmp_path: Path, no_live: None) -> None:
    from test_appTakeover import Brain, _agent, _desk, _response, _use
    from yuki.log.events import Final
    from yuki.voice.offline import ChatPlayerDesk

    finals: dict[str, Any] = {}
    for claims in ("done", "tried"):
        def done(i: int) -> SimpleNamespace:
            return _response(_use("done", i, message="Playing now; the extra click didn't go through.",
                                  about=[], states=[], achieved="Started the music", claims=claims))

        # the player in front, so the refused click is in the app where the press worked (review F1: a refused step
        # in another app, or after only a focus, is never set aside)
        brain = Brain([_response(_use("focus_window", 5, hwnd=ChatPlayerDesk.PLAYER)),
                       _response(_use("press", 0, key="playpause", expect_hwnd=ChatPlayerDesk.PLAYER)),
                       _response(_use("click", 1, button="left")),   # neither an element nor x and y: refused
                       done(2), done(3), done(4)])
        agent = _agent(brain, tmp_path / claims, _desk())
        events = list(agent.run("play the music"))
        final = events[-1]
        assert isinstance(final, Final), events[-3:]
        refused = [s for s in agent._trace if s.get("tool") == "click"]
        assert refused and refused[0]["ok"] is False and refused[0].get("refused") is True
        assert not any(s.get("refused") for s in agent._trace if s.get("tool") == "press")
        finals[claims] = final.outcome
    assert finals["done"]["refused"] == 1 and finals["done"]["level"] != "failed"
    assert finals["tried"]["refused"] == 0 and finals["tried"]["level"] == "failed"


def test_a_tool_error_comes_back_marked_refused() -> None:
    from yuki.agent.tools import Dispatcher
    from yuki.voice.offline import ChatPlayerDesk

    out = Dispatcher(backend=ChatPlayerDesk()).dispatch("click", {"button": "left"})
    assert out.ok is False and out.payload.get("refused") is True
