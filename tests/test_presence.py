"""Offline checks of the observation cards (yuki.memory.presence): the measured signals, the checks on the
models' answers, the per-kind budgets and the API fields the cards need.

Temp databases, synthetic data and stub models only; nothing touches the desktop or a real model.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from yuki.core.attention import DEFAULT_RULES, PRESENCE_CHANNELS, Attention
from yuki.memory.api import MemoryClient
from yuki.memory.nudges import NudgeConfig, NudgeWorker, observe
from yuki.memory.presence import (Look, _call_sessions, channel_budgets, card_feedback_lines, recurrence)
from yuki.memory.store import NUDGE_REACTIONS, NewNudge, NudgeCheckin, Store, TimelineRow

DAY = datetime(2026, 9, 28)
CFG = NudgeConfig()


def at(hhmm: str) -> float:
    h, m = (int(x) for x in hhmm.split(":"))
    return (DAY + timedelta(hours=h, minutes=m)).timestamp()


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def fact(store: Store, when: float, text: str, vec: np.ndarray | None = None, **kw) -> int:
    fid = store.add_journal(when, None, kw.pop("app", "Google Chrome"), text, kw.pop("importance", 6),
                            vector=vec, embed_model="test" if vec is not None else None, **kw)
    with store._lock:   # memory learned it when it happened (a live journal writes within minutes)
        store._conn.execute("UPDATE journal SET created_at=? WHERE id=?", (when + 60.0, fid))
    return fid


def unit(*xs: float) -> np.ndarray:
    v = np.asarray(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def card(store: Store, when: float, text: str, trigger: str, reaction: str | None = None,
         kind: str = "notice") -> int:
    _cid, nid = store.record_checkin(NudgeCheckin(at=when, trigger=trigger, outcome=kind, model="m"),
                                     NewNudge(kind=kind, text=text, trigger=trigger, inputs={"offer": "Open it."}))
    if reaction:
        store.react_to_nudge(nid, reaction, at=when + 20)
    return nid


# -- the measured signals ---------------------------------------------------------------------------


def test_recurrence_needs_similar_fresh_facts_over_minutes():
    F = SimpleNamespace
    same = [F(id=1, at=at("19:52")), F(id=2, at=at("20:27")), F(id=3, at=at("20:46"))]
    other = [F(id=4, at=at("20:40"))]
    vecs = {1: unit(1, 0.05, 0), 2: unit(1, 0.1, 0), 3: unit(1, 0, 0.1), 4: unit(0, 1, 0)}
    groups = recurrence(same + other, vecs, now=at("20:50"))
    assert [[f.id for f in g] for g in groups] == [[1, 2, 3]]
    # too close together (a burst, not something coming back), or no longer fresh: nothing
    burst = [F(id=1, at=at("20:45")), F(id=2, at=at("20:46")), F(id=3, at=at("20:47"))]
    assert recurrence(burst, vecs, now=at("20:50")) == []
    assert recurrence(same, vecs, now=at("21:30")) == []
    # below the cosine it is not the same thing
    far = {1: unit(1, 0, 0), 2: unit(0.6, 0.8, 0), 3: unit(0.6, 0, 0.8)}
    assert recurrence(same, far, now=at("20:50")) == []


def test_call_rows_close_together_are_one_session():
    C = SimpleNamespace
    rows = [C(id=34, app="Discord", detected_at=at("21:37"), ended_at=at("21:57")),
            C(id=35, app="Discord", detected_at=at("22:01"), ended_at=at("23:00")),
            C(id=36, app="Discord", detected_at=at("23:06"), ended_at=at("23:59")),
            C(id=40, app="Zen", detected_at=at("11:00"), ended_at=at("11:05")),
            C(id=41, app="Discord", detected_at=at("09:00"), ended_at=None)]
    sessions = _call_sessions(rows)
    assert [[c.id for c in s] for s in sessions] == [[40], [34, 35, 36]]


# -- the per-kind budgets and the cards' own feedback --------------------------------------------------


def test_less_of_these_halves_only_that_kind_and_do_it_takes_one_back(store):
    base = {"stuck": 3, "recall": 3}
    card(store, at("10:00"), "a", "stuck", "less")
    assert channel_budgets(store, at("12:00"), base) == {"stuck": 2, "recall": 3}
    card(store, at("10:30"), "b", "stuck", "less")
    card(store, at("11:00"), "c", "stuck", "less")
    assert channel_budgets(store, at("12:00"), base)["stuck"] == 0      # three presses: that kind rests
    card(store, at("11:30"), "d", "stuck", "accepted")
    assert channel_budgets(store, at("12:00"), base)["stuck"] == 1
    lines = card_feedback_lines(store, lambda v, n: str(v)[:n], at("12:00"))
    assert any("Less of these" in ln and "notice (stuck)" in ln for ln in lines)
    assert any("Do it" in ln for ln in lines)
    assert {"accepted", "less"} <= set(NUDGE_REACTIONS)


def test_attention_history_comes_from_the_stored_cards_and_spreads_them():
    now = [at("12:00")]
    att = Attention(clock=lambda: now[0])
    assert set(PRESENCE_CHANNELS) <= set(DEFAULT_RULES)
    att.load("presence", [at("11:40")])
    d = att.check("presence", now=now[0], facts={})
    assert not d.allowed and d.reason == "cooldown"                     # 25 minutes apart
    att.load("presence", [at("11:05"), at("11:30")])
    assert att.check("presence", now=now[0], facts={}).reason == "budget"   # two an hour
    att.load("stuck", [at("09:00"), at("10:00"), at("11:00")])
    assert att.check("stuck", now=now[0], facts={}).reason == "budget"


def test_a_kind_brought_to_zero_is_closed(store):
    for h in ("09:00", "09:30", "10:00"):
        card(store, at(h), "x", "recall", "less")
    w = worker_for(store, Stub({}, {}))
    att = w.presence._budgets(at("12:00"), CFG)
    assert w.presence.budgets["recall"] == 0 and w.presence.budgets["stuck"] == DEFAULT_RULES["stuck"].budget
    assert att.rule("recall").budget == 1   # the framework's own rule never reads 0 as "unlimited"


# -- the checks on the models' answers -------------------------------------------------------------------


class Stub:
    def __init__(self, decision: dict, card: dict):
        self.decision, self.card, self.calls = decision, card, []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **request):
        name = request["tool_choice"]["name"]
        self.calls.append(name)
        base = {"reason": "stub", "mentions": [], "evidence": [], "offer": ""}
        data = {"observe_decision": {**base, "say": "none", "detail": "", **self.decision},
                "write_notice": {**base, "say": "none", "text": None, **self.card}}.get(name, {})
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=data)],
                               stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_creation_input_tokens=0,
                                                     cache_read_input_tokens=0))


def worker_for(store: Store, stub: Stub) -> NudgeWorker:
    return NudgeWorker(store, config=CFG, client=stub, log_dir=Path(tempfile.gettempdir()) / "yuki-presence-tests",
                       locked_fn=lambda: False, media_fn=lambda: [], render_fn=lambda: [],
                       event_name="Local\\YukiNudgeReadyTest", clock=lambda: at("21:00"))


def present(store: Store, start: float, end: float, app: str = "Google Chrome") -> None:
    t = start
    while t < end:
        e = min(end, t + 300.0)
        store.save_timeline(TimelineRow(None, t, e, "present", app.lower(), app, active_s=e - t))
        t = e


def stuck_look(ids: list[int]) -> Look:
    return Look("stuck", f"stuck:J{ids[0]}", trigger="stuck - test", lines=[], ids={f"J{i}": 0.0 for i in ids},
                required={f"J{i}" for i in ids})


def test_a_card_must_rest_on_the_thing_that_woke_the_look_and_offered_ids(store):
    present(store, at("19:00"), at("21:00"))
    ids = [fact(store, at(h), f"Deploy failed ({h})") for h in ("19:52", "20:27", "20:46")]
    other = fact(store, at("20:10"), "The user viewed a page.")
    view = observe(store, at("21:00"), CFG)
    cases = [
        ({"say": "notice", "detail": "deploy", "evidence": ["J999999"], "offer": "Open the run."}, "suppressed:ungrounded"),
        ({"say": "notice", "detail": "deploy", "evidence": [f"J{other}"], "offer": "Open the run."}, "suppressed:no_content"),
        ({"say": "notice", "detail": "deploy", "evidence": [f"J{ids[0]}"], "offer": ""}, "suppressed:no_offer"),
    ]
    for decision, outcome in cases:
        stub = Stub(decision, {})
        w = worker_for(store, stub)
        look = stuck_look(ids)
        look.ids[f"J{other}"] = at("20:10")
        checkin = NudgeCheckin(at=at("21:00"), trigger="stuck", outcome="error")
        out = w.presence.decide(at("21:00"), CFG, view, [], look, checkin)
        assert out["outcome"] == outcome and stub.calls == ["observe_decision"], (decision, out["outcome"])


def test_a_grounded_look_becomes_a_notice_card_with_its_offer(store):
    present(store, at("19:00"), at("21:00"))
    ids = [fact(store, at(h), f"Deploy failed ({h})") for h in ("19:52", "20:27", "20:46")]
    stub = Stub({"say": "notice", "detail": "deploy-beta failed 3 times", "evidence": [f"J{i}" for i in ids],
                 "offer": "Open the failed run."},
                {"say": "notice", "text": "Deploy-beta failed three times since 19:52. Want me to pull the log?",
                 "offer": "Open the latest failed Deploy run and tell me why deploy-beta failed.",
                 "evidence": [f"J{ids[-1]}"]})
    w = worker_for(store, stub)
    view = observe(store, at("21:00"), CFG)
    result = w.presence.observe(at("21:00"), CFG, view, [], stuck_look(ids))
    assert result["outcome"] == "notice" and stub.calls == ["observe_decision", "write_notice"]
    n = store.nudge(result["nudge_id"])
    assert n.kind == "notice" and n.trigger == "stuck" and n.ref == f"stuck:J{ids[0]}"
    assert n.inputs["offer"].startswith("Open the latest failed Deploy run")
    row = MemoryClient._nudge_dict(n)   # what pending_nudges hands the UI
    assert row["offer"] == n.inputs["offer"] and row["trigger"] == "stuck" and row["kind"] == "notice"
    assert "offer on the card" in (n.reason or "")   # a Reply "yes" reaches the agent with the offer in the why
    # looked at once: the same recurrence is not looked at again without more of it
    assert w.presence._looked(f"stuck:J{ids[0]}", at("21:01"), 3600.0)


def test_a_notice_with_nothing_next_is_not_shown(store):
    present(store, at("19:00"), at("20:00"))
    done = fact(store, at("19:50"), "The user merged PR #2804.")
    stub = Stub({"say": "notice", "detail": "merged", "evidence": [f"J{done}"], "offer": ""}, {})
    w = worker_for(store, stub)
    look = Look("notice", f"notice:J{done}", trigger="notice - test", ids={f"J{done}": at("19:50")},
                required={f"J{done}"})
    out = w.presence.decide(at("20:00"), CFG, observe(store, at("20:00"), CFG), [], look,
                            NudgeCheckin(at=at("20:00"), trigger="notice", outcome="error"))
    assert out["outcome"] == "suppressed:nothing_next"


def test_a_missed_card_stays_in_the_history_and_is_never_pending(store):
    nid = card(store, at("10:00"), "Something", "recall")
    store.expire_nudges(at("10:31"), ("notice",))
    client = MemoryClient(store)
    assert all(n["id"] != nid for n in client.pending_nudges())
    import time as _time

    rows = client.recent_nudges(hours=(_time.time() - at("09:00")) / 3600.0)
    missed = [r for r in rows if r["id"] == nid]
    assert missed and missed[0].get("missed") and missed[0]["offer"] == "Open it."


def test_an_id_list_sent_as_one_string_is_still_read_as_a_list():
    from yuki.memory.nudges import _clean_ids

    assert _clean_ids('J1630", "J1631", "W7') == ["J1630", "J1631", "W7"]   # seen from Sonnet on 28 Sept
    assert _clean_ids('["J5", "NOW"]') == ["J5", "NOW"] and _clean_ids(["[J1]", " W2 "]) == ["J1", "W2"]
    assert _clean_ids("NOW") == ["NOW"] and _clean_ids(None) == []
