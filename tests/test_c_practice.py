"""Phase C, practice memory (docs/research/revamp-2026-10-02/27-build-C.md): migration 29, the run trace, the
distiller's validation, the post-mortem triggers, confidence and decay, the miner's floors, retrieval by target and
intent, the ledger, the Playbook page API, the know-how intake, the frame and the service worker.

Offline: temp databases, a word-bag stand-in for the embedder, stand-ins for the model calls; nothing touches the live
desktop, the real memory or the network."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from yuki.memory import practice as P
from yuki.memory import practice_learn as L
from yuki.memory import store as st
from yuki.memory.store import Store

NOW = 1_790_900_000.0   # 2026-10-02 (a fixed clock)
DAY = 86400.0


class Words:
    """A stand-in for the shared embedder: a hashed bag of a text's words, so texts sharing words sit near each other.
    Deterministic, no model."""

    model_name = "stub-words"
    _SKIP = {"the", "a", "an", "and", "of", "to", "for", "on", "in", "my", "your", "it", "is", "by"}

    def embed(self, texts: list[str], wait_s: float | None = None) -> Any:
        out = np.zeros((len(texts), 256), dtype=np.float32)
        for i, text in enumerate(texts):
            for w in P._tokens(text):
                if w not in self._SKIP:
                    out[i, sum(map(ord, w)) * 31 % 256] += 1.0
        return out


def vec(text: str) -> np.ndarray:
    return Words().embed([text])[0]


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


class Worker:
    """A stand-in for the portrait worker: its model call returns the queued answers (and records what it was sent)."""

    model = "stub"

    def __init__(self, store: Store, answers: list[Any] | None = None) -> None:
        self.store = store
        self.answers = list(answers or [])
        self.calls: list[dict[str, Any]] = []
        self.embedder = Words()
        self.settings = SimpleNamespace(user_names=["Kian"])
        self.client = None
        self.logged: list[tuple[str, dict[str, Any]]] = []

    def _call(self, **kw: Any) -> Any:
        self.calls.append(kw)
        kw["stats"].cost_usd += 0.004
        kw["stats"].calls += 1
        return self.answers.pop(0)

    def _log(self, type_: str, **fields: Any) -> None:
        self.logged.append((type_, fields))

    def vet_names(self) -> list[str]:
        return ["Kian"]


def step(tool: str, *, ok: bool = True, verdict: bool | None = True, app: str = "discord", target: str = "",
         inp: Any = None, result: str = "", kind: str = "action", expected: bool = False, sig: str | None = None,
         playbook: Any = None) -> dict[str, Any]:
    return {"tool": tool, "kind": kind, "app": app, "target": target, "input": inp if inp is not None else {},
            "ok": ok, "verdict": verdict, "why": "", "expected": expected, "result": result, "ms": 100.0,
            "sig": sig or f"{tool}:{json.dumps(inp, sort_keys=True, default=str)}", "playbook": playbook}


def tag_run(**over: Any) -> dict[str, Any]:
    """The 30 Sep Discord tag, as a run trace: the channel by the switcher, a pasted @name that stayed text, then the
    name typed by keys with the suggestion accepted, then the message."""
    steps = [
        step("hotkey", inp={"keys": ["ctrl", "k"], "expect_hwnd": 2002}, target='window "Friends - Discord"',
             result="pressed ctrl+k; the quick switcher opened"),
        step("type_text", inp={"text": "after-hours", "press_enter": True}, target='Edit "Where would you like to go?"',
             result="typed 14 chars; the channel opened"),
        step("type_text", verdict=None, inp={"text": "@Hemanth when are you coming on", "element": 41},
             target='Edit "Message #after-hours"', result="typed 32 chars via clipboard; no suggestion list"),
        step("type_text", inp={"text": "@Hem", "accept_suggestion": True}, target='Edit "Message #after-hours"',
             result="typed 4 chars by keys; the suggestion Quasar40404 was accepted"),
        step("type_text", inp={"text": " when are you coming on", "press_enter": True},
             target='Edit "Message #after-hours"', result="typed and sent"),
    ]
    run = {"source": "agent", "session_id": "S1", "request_id": "worker-1", "lane": "voice", "at": NOW,
           "ended_at": NOW + 40, "words": "tag Hemanth in my usual channel and ask when he's coming on",
           "reply": "Tagged him.", "outcome": "final", "steps": steps, "cost_usd": 0.12, "model_calls": 5,
           "wall_s": 40.0, "numbers": [2002, 41, 1887]}
    run.update(over)
    return run


# -- migration 29 -------------------------------------------------------------------------------------------------


def test_migration_29_adds_the_practice_tables_and_keeps_everything(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    full = st.MIGRATIONS
    monkeypatch.setattr(st, "MIGRATIONS", full[:28])
    old = Store.open(path)
    kid = old.add_knowhow("spotify", "Focus the window before clicking Pause.")
    assert old._query("PRAGMA user_version")[0][0] == 28
    assert not old._query("SELECT name FROM sqlite_master WHERE name='runs'")
    old.close()
    monkeypatch.setattr(st, "MIGRATIONS", full)
    new = Store.open(path)
    try:
        assert new._query("PRAGMA user_version")[0][0] == len(st.MIGRATIONS) >= 29
        for table in ("runs", "playbooks", "playbook_vec", "playbook_events"):
            assert new._query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        assert [k.id for k in new.knowhow_current()] == [kid]                     # nothing moved or lost
        assert P.live(new) == [] and P.workflows(new)["workflows"] == []         # empty until learned
    finally:
        new.close()


def test_run_traces_share_the_captures_ttl(store):
    P.record_run(store, tag_run(at=time.time() - 40 * DAY, ended_at=time.time() - 40 * DAY))
    P.record_run(store, tag_run(request_id="worker-2", at=time.time(), ended_at=time.time()))
    assert store._query("SELECT count(DISTINCT run_key) FROM runs")[0][0] == 2
    store.prune(30)
    keys = {r[0] for r in store._query("SELECT run_key FROM runs")}
    assert keys == {"agent:S1:worker-2"}


# -- the run trace ---------------------------------------------------------------------------------------------------


def test_the_trace_keeps_every_step_and_never_a_window_number_as_meaning(store):
    run = tag_run()
    run["steps"][1]["input"]["text"] = "my key is sk-ant-" "api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdEFGH-xyz"
    out = P.record_run(store, run)
    assert out["ok"] and out["steps"] == 5 and out["learn"] == "pending"
    head = P.run_header(store, out["run_key"])
    assert head.words.startswith("tag Hemanth") and head.outcome == "final" and head.calls == 5
    sig = head.signals
    assert sig["actions"] == 5 and sig["unverified"] == 1 and sig["trouble_then_success"] is True
    assert {2002, 41, 1887} <= set(sig["numbers"])
    steps = P.run_steps(store, out["run_key"])
    assert [s.tool for s in steps] == ["hotkey", "type_text", "type_text", "type_text", "type_text"]
    assert steps[2].target == 'Edit "Message #after-hours"' and steps[2].verdict is None
    assert "2002" not in (steps[0].input or "") and "element" not in (steps[2].input or "")   # kept apart
    assert "sk-ant" not in (steps[1].input or "")                                             # secret filtered
    raw = store._query("SELECT input_ciphertext FROM runs WHERE step=3")[0][0]
    assert "Hemanth" not in raw                                                               # encrypted at rest
    assert P.record_run(store, run)["learn"] == "kept"                                        # once per run


def test_a_voice_hand_is_a_run_of_one_action(store):
    out = P.record_hand_trace(store, session_id="V1", name="press_keys", args={"keys": ["volumedown"], "times": 3,
                                                                               "say_when_done": "Done."},
                              ok=True, verified=True, report="pressed volumedown x3", ms=120, at=NOW,
                              app="chrome.exe", words="lower your volume")
    head = P.run_header(store, out["run_key"])
    assert head.source == "hand" and head.signals["actions"] == 1 and head.words == "lower your volume"
    s = P.run_steps(store, out["run_key"])[0]
    assert s.tool == "press_keys" and s.app == "chrome" and "say_when_done" not in (s.input or "")


def test_the_agent_loop_records_targets_by_role_and_name_and_the_playbooks_it_cites(tmp_path):
    from yuki.agent.loop import Agent
    from yuki.agent.memory import MemoryAccess
    from yuki.config import Settings
    from yuki.log.events import SessionLogger
    from yuki.memory.api import MemoryClient
    from yuki.voice.offline import BrowserDesk

    client = MemoryClient(Store.open(tmp_path / "m" / "memory.db"))
    client._embedder = Words()
    pid = P.add_playbook(client.store, {"kind": "procedure", "intent": "pause the video in chrome",
                                        "apps": ["chrome"], "body": {"steps": ["press k"]}},
                         events=[{"type": "observed", "run_key": "r0"}], vector=vec("pause the video in chrome"))
    memory = MemoryAccess(open_client=lambda: client)

    def resp(*blocks: Any) -> Any:
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(content=list(blocks), stop_reason="tool_use", usage=usage)

    desk = BrowserDesk(action_delay_s=0)
    calls = [
        resp(SimpleNamespace(type="tool_use", id="l1", name="look_at_window", input={"hwnd": desk.CHROME})),
        resp(SimpleNamespace(type="tool_use", id="c1", name="click",
                             input={"element": 2, "expect_hwnd": desk.CHROME, "playbook": f"pb:{pid}"})),
        resp(SimpleNamespace(type="tool_use", id="d1", name="done", input={"message": "Paused.", "about": []})),
    ]
    fake = SimpleNamespace(messages=SimpleNamespace(create=lambda **p: calls.pop(0)))
    settings = Settings(log_dir=tmp_path / "sessions", stream=False, requests_csv=tmp_path / "r.csv")
    agent = Agent(settings, SessionLogger(settings.sessions_dir, console=None), client=fake, backend=desk,
                  prewarm=False, record_requests=False, memory=memory)
    list(agent.run("pause the video in chrome", effort="low", thinking=False))
    assert memory.flush_turns(10)
    head = P.pending_runs(client.store)[0]
    assert pid in head.injected and pid in head.cited
    steps = P.run_steps(client.store, head.run_key)
    click = next(s for s in steps if s.tool == "click")
    assert click.target.startswith('TabItem "GitHub - yukidev/yuki"') and str(desk.CHROME) not in click.target
    assert click.app == "chrome" and click.cost_usd is not None
    types = [r[0] for r in client.store._query("SELECT type FROM playbook_events WHERE playbook_id=?", (pid,))]
    assert "injected" in types and "cited" in types
    client.close()


# -- the distiller's validation ---------------------------------------------------------------------------------


def _ctx(store: Store, run: dict[str, Any], mode: str = "distill", **over: Any) -> L.Ctx:
    key = P.record_run(store, run)["run_key"]
    head = P.run_header(store, key)
    steps = P.run_steps(store, key)
    ctx = L.Ctx(mode=mode, steps={s.step: s for s in steps}, apps={"discord"},
                numbers=set(head.signals["numbers"]), user_words=over.pop("words", []), run_key=key)
    for k, v in over.items():
        setattr(ctx, k, v)
    return ctx


GOOD_PROC = {"op": "ADD", "kind": "procedure", "intent": "tag someone in a Discord channel", "apps": ["discord"],
             "steps": [{"do": "Ctrl+K, type the channel's name, Enter", "from": [1, 2]},
                       {"do": "type @ and the start of the name with accept_suggestion", "from": [4]},
                       {"do": "type the message with press_enter", "from": [5]}],
             "slots": ["channel", "person", "message"]}
GOOD_PIT = {"op": "ADD", "kind": "pitfall", "intent": "mention someone in Discord", "apps": ["discord"],
            "text": "A pasted @name stays plain text in Discord's message box", "from": [3],
            "instead": "type @ and the start of the name by keys with accept_suggestion",
            "family": "mentioning someone in a chat app"}


def test_distiller_steps_must_rest_on_actions_that_ran_ok(store):
    ctx = _ctx(store, tag_run())
    ok = L.check({"outcome": "worked_after_trouble", "operations": [GOOD_PROC, GOOD_PIT], "used": []}, ctx)
    assert [o["kind"] for o in ok.ops] == ["procedure", "pitfall"] and not ok.rejected
    run = tag_run(request_id="worker-9")
    run["steps"][3]["ok"] = False            # the accepted suggestion failed in this version of the run
    bad = L.check({"operations": [GOOD_PROC], "used": []}, _ctx(store, run))
    assert not bad.ops and "did not run ok" in bad.rejected[0]
    no_src = dict(GOOD_PROC, steps=[{"do": "Ctrl+K", "from": []}])
    assert "cites no trace step" in L.check({"operations": [no_src]}, ctx).rejected[0]
    ghost = dict(GOOD_PROC, steps=[{"do": "Ctrl+K", "from": [9]}])
    assert "does not have" in L.check({"operations": [ghost]}, ctx).rejected[0]


def test_distiller_pitfalls_must_rest_on_failed_or_unverified_actions(store):
    ctx = _ctx(store, tag_run())
    wrong = dict(GOOD_PIT, **{"from": [1]})   # step 1 verified: not a dead end
    out = L.check({"operations": [wrong]}, ctx)
    assert not out.ops and "failed or unverified" in out.rejected[0]
    none = dict(GOOD_PIT, **{"from": []})
    assert not L.check({"operations": [none]}, ctx).ops
    # a read that failed is a dead end too (Warp's tab strip cannot be read); a read that worked is not
    run = tag_run(request_id="worker-7", steps=[
        step("read_tabs", kind="read", ok=False, verdict=None, app="warp", result="could not read the tabs"),
        step("look_at_window", kind="read", ok=True, verdict=None, app="warp"),
        step("hotkey", app="warp", inp={"keys": ["ctrl", "tab"]})])
    rctx = _ctx(store, run)
    rctx.apps = {"warp"}
    pit = {"op": "ADD", "kind": "pitfall", "intent": "read Warp's tabs", "apps": ["warp"],
           "text": "Warp's tab strip cannot be read", "instead": "cycle with ctrl+tab and read the title"}
    assert L.check({"operations": [dict(pit, **{"from": [1]})]}, rctx).ops
    assert not L.check({"operations": [dict(pit, **{"from": [2]})]}, rctx).ops
    # a quote the user never said is dropped, and the operation stands only on its own evidence
    proc = dict(GOOD_PROC, quote="something nobody said")
    got = L.check({"operations": [proc]}, ctx)
    assert got.ops and got.ops[0]["quote"] == "" and "dropped" in got.rejected[0]


def test_validation_rejects_window_numbers_stray_apps_invented_quotes_and_unknown_updates(store):
    ctx = _ctx(store, tag_run(), mode="postmortem", words=["you didn't tag him, it's just text"])
    coords = dict(GOOD_PROC, steps=[{"do": "click at (1887, 11) then Enter", "from": [1]}])
    assert "window ids" in L.check({"operations": [coords]}, ctx).rejected[0]
    stray = dict(GOOD_PIT, apps=["whatsapp"])
    assert "never touched" in L.check({"operations": [stray]}, ctx).rejected[0]
    invented = dict(GOOD_PIT, **{"from": [1], "quote": "always use the DM"})
    assert "not the user's own words" in L.check({"operations": [invented]}, ctx).rejected[0]
    quoted = dict(GOOD_PIT, **{"from": [1], "quote": "it's just text"})   # a correction: the user's words suffice
    assert L.check({"operations": [quoted]}, ctx).ops
    style = {"op": "ADD", "kind": "style", "intent": "how to write to Hemanth", "text": "Hinglish, short"}
    assert "needs the user's own words" in L.check({"operations": [style]}, ctx).rejected[0]
    upd = {"op": "UPDATE", "id": 999, "kind": "pitfall", "intent": "x y z", "text": "t", "from": [3]}
    assert "names no playbook shown" in L.check({"operations": [upd]}, ctx).rejected[0]
    # an UPDATE restates the playbook: one without its intent is refused (seen 3 Oct: an "UPDATE" whose text was an
    # argument for invalidating the row)
    pid = P.add_playbook(store, {"kind": "pitfall", "intent": "mention someone in Discord", "apps": ["discord"],
                                 "body": {"text": "a pasted @name stays text"}})
    ctx.existing = {pid: P.get(store, pid)}
    bare = L.check({"operations": [{"op": "UPDATE", "id": pid, "text": "INVALIDATE: this is wrong", "from": [3]}]}, ctx)
    assert not bare.ops and "intent missing" in bare.rejected[0]


# -- the post-mortem's triggers (measured signals only) -------------------------------------------------------------


def _sig(**over: Any) -> dict[str, Any]:
    base = P.run_signals([step("hotkey")], outcome="final")
    base.update(over)
    return base


@pytest.mark.parametrize("signals, follow, cos, mode", [
    (dict(cancelled=True, outcome="cancelled"), [], [], "postmortem"),
    (dict(bad_same_tool_run=2), [], [], "postmortem"),
    (dict(repeated_unverified=2), [], [], "postmortem"),
    (dict(asks=2), [], [], "postmortem"),
    (dict(breaker=1), [], [], "postmortem"),
    (dict(steers=1), [], [], "postmortem"),
    ({}, [{"after_s": 60.0, "words": "lower your volume again"}], [0.81], "postmortem"),        # repeats it
    ({}, [{"after_s": 60.0, "words": "not that one"}], [0.35], "postmortem"),                   # bears on it
    ({}, [{"after_s": 12.0, "words": "It's not full screen, dude."}], [0.10], "postmortem"),    # fast, any cosine
    ({}, [{"after_s": 60.0, "words": "what did I do today"}], [0.05], None),                    # unrelated, late
    ({}, [{"after_s": 200.0, "words": "lower your volume"}], [0.90], None),                     # out of the window
    ({}, [{"after_s": 5.0, "words": "scroll down"}], [0.99], None),                             # more of it
    (dict(actions=3, ok_actions=3, worked=3), [], [], "distill"),
    (dict(actions=2, ok_actions=2, worked=1, trouble_at=1, trouble_then_success=True), [], [], "distill"),
    (dict(actions=2, ok_actions=2, worked=2), [], [], None),
    (dict(actions=4, ok_actions=0, worked=0, outcome="error"), [], [], None),
])
def test_which_runs_get_a_post_mortem_or_the_distiller(signals, follow, cos, mode):
    got, reasons = L.decide(_sig(**signals), follow, cos)
    assert got == mode
    assert bool(reasons) == (mode is not None)


# -- confidence and decay -----------------------------------------------------------------------------------------


def ev(type_: str, at: float, key: str | None = None) -> P.Event:
    return P.Event(at=at, type=type_, weight=P.WEIGHTS.get(type_, 0.0), run_key=key,
                   day=time.strftime("%Y-%m-%d", time.localtime(at)))


def test_confidence_is_computed_from_the_ledger_and_decays_by_evidence_age():
    one = P.evaluate("procedure", [ev("observed", NOW, "r1")], created_at=NOW, now=NOW)
    assert one["state"] == "candidate" and one["confidence"] == pytest.approx(2 / 3, abs=1e-3)
    two = P.evaluate("procedure", [ev("observed", NOW, "r1"), ev("ran_ok", NOW + 10, "r2")], created_at=NOW, now=NOW)
    assert two["state"] == "active" and two["confidence"] == pytest.approx(3 / 4, abs=1e-3)
    taught = P.evaluate("pitfall", [ev("taught", NOW, "r1")], created_at=NOW, now=NOW)
    assert taught["state"] == "active" and taught["confidence"] == pytest.approx(4 / 5, abs=1e-3)
    # decay: one half-life without evidence halves it and the object is stale (verify first)
    old = P.evaluate("procedure", [ev("observed", NOW, "r1"), ev("ran_ok", NOW, "r2")], created_at=NOW,
                     now=NOW + 61 * DAY)
    assert old["state"] == "stale" and old["confidence"] < 0.75 / 2
    slow = P.evaluate("habit", [ev("observed_day", NOW - d * DAY, "miner") for d in range(3)], created_at=NOW,
                      now=NOW + 61 * DAY)
    assert slow["state"] == "active" and slow["n_days"] == 3    # personal defaults decay slower (180 d)
    # a use alone is no evidence
    used = P.evaluate("procedure", [ev("observed", NOW, "r1"), ev("cited", NOW + 50 * DAY, "r3")], created_at=NOW,
                      now=NOW + 61 * DAY)
    assert used["state"] == "stale" and used["n_used"] == 1


def test_trusted_after_clean_uses_and_retired_after_repeated_trouble():
    events = [ev("observed", NOW, "r0")]
    for i in range(3):
        events += [ev("cited", NOW + i + 1, f"u{i}"), ev("ran_ok", NOW + i + 1, f"u{i}")]
    assert P.evaluate("procedure", events, created_at=NOW, now=NOW + 10)["state"] == "trusted"
    events += [ev("cited", NOW + 20, "u9"), ev("user_corrected", NOW + 20, "u9")]
    assert P.evaluate("procedure", events, created_at=NOW, now=NOW + 30)["state"] == "active"   # trust is lost
    bad = [ev("observed", NOW, "r0"), ev("ran_trouble", NOW + 1, "a"), ev("contradicted", NOW + 2, "b")]
    assert P.evaluate("procedure", bad, created_at=NOW, now=NOW + 3)["state"] == "retired"


def test_the_numbers_retire_an_object_by_code(store):
    pid = P.add_playbook(store, {"kind": "procedure", "intent": "open the tab strip", "body": {"steps": ["a"]}},
                         events=[{"type": "observed", "run_key": "r0", "at": NOW}], now=NOW)
    P.add_events(store, [{"playbook_id": pid, "type": "ran_trouble", "run_key": "a", "at": NOW + 1},
                         {"playbook_id": pid, "type": "contradicted", "run_key": "b", "at": NOW + 2}], now=NOW + 3)
    o = P.get(store, pid)
    assert o.valid_to is not None and o.state == "retired" and P.live(store) == []
    assert "retired_by" in [e.type for e in P.ledger(store, pid)]


# -- retrieval by target and intent ---------------------------------------------------------------------------------


def _seed(store: Store) -> dict[str, int]:
    ids = {}
    ids["tag"] = P.add_playbook(store, {"kind": "procedure", "intent": "tag someone in a Discord channel",
                                        "apps": ["discord"], "body": {"steps": ["Ctrl+K", "@ by keys"]}},
                                events=[{"type": "observed", "run_key": "r1"}],
                                vector=vec("tag someone in a Discord channel"))
    ids["paste"] = P.add_playbook(store, {"kind": "pitfall", "intent": "mention someone in Discord",
                                          "apps": ["discord"], "body": {"text": "a pasted @name stays text",
                                                                        "instead": "type it by keys"}},
                                  events=[{"type": "observed", "run_key": "r1"}],
                                  vector=vec("mention someone in Discord"))
    ids["volume"] = P.add_playbook(store, {"kind": "pitfall", "intent": "lower or raise a volume",
                                           "family": "any volume change: the system, one app or Yuki's own voice",
                                           "body": {"text": "media keys move the system volume",
                                                    "instead": "ask which, or app_volume"}},
                                   events=[{"type": "taught", "run_key": "r2"}],
                                   vector=vec("lower or raise a volume any volume change"))
    ids["warp"] = P.add_playbook(store, {"kind": "procedure", "intent": "switch to a Warp tab by its name",
                                         "apps": ["warp"], "body": {"steps": ["ctrl+shift+] until the title"]}},
                                 events=[{"type": "observed", "run_key": "r3"}],
                                 vector=vec("switch to a Warp tab by its name"))
    return ids


def test_retrieval_by_words_and_apps_in_play_with_a_floor(store):
    ids = _seed(store)
    got = P.relevant(store, query_vec=vec("tag Hemanth in my usual Discord channel"), apps_in_play=["discord"])
    assert [w["id"] for w in got["ways"]] == [ids["tag"]]
    assert ids["paste"] in [p["id"] for p in got["pitfalls"]]        # a pitfall of an app in play always comes
    assert ids["warp"] not in [w["id"] for w in got["ways"]]         # under the floor
    vol = P.relevant(store, query_vec=vec("lower your volume"), apps_in_play=["chrome"])
    assert [p["id"] for p in vol["pitfalls"]] == [ids["volume"]] and vol["ways"] == []
    # the window in front is not the target: Warp open in the background, Discord named in the words
    named = P.apps_named(store, "tag him in Discord", known=["Warp.exe"])
    assert named == ["discord"]
    # the intent is matched without the app names (stored names, whole words), the app by scope
    names = P.app_name_tokens(store, ["Warp.exe"])
    assert P.strip_apps("Go to the LLM OS tab in Warp", names) == "go to the llm os tab in"
    assert P.intent_words(store, "lower your volume", ["Warp.exe"]) == "lower your volume"     # nothing to take out
    assert P.vector_text(store, "switch to a Warp tab by its name", None, ["warp"]) == "switch to a tab by its name"
    # no vector (the embedder is down): no unranked ways, but the pitfalls of the apps in play still come
    blind = P.relevant(store, query_vec=None, apps_in_play=["discord"])
    assert blind["ways"] == [] and [p["id"] for p in blind["pitfalls"]] == [ids["paste"]]
    # a far pitfall of no app in play stays out
    far = P.relevant(store, query_vec=vec("what did I do today"), apps_in_play=["chrome"])
    assert far["pitfalls"] == [] and far["ways"] == []


def test_disabled_and_forgotten_objects_never_reach_a_request(store):
    ids = _seed(store)
    assert P.workflow_edit(store, f"pb:{ids['paste']}", "disable")["state"] == "disabled"
    got = P.relevant(store, query_vec=vec("tag him in Discord"), apps_in_play=["discord"])
    assert ids["paste"] not in [p["id"] for p in got["pitfalls"]]
    P.workflow_edit(store, f"pb:{ids['paste']}", "enable")
    P.workflow_edit(store, f"pb:{ids['tag']}", "forget")
    got = P.relevant(store, query_vec=vec("tag him in Discord channel"), apps_in_play=["discord"])
    assert ids["paste"] in [p["id"] for p in got["pitfalls"]] and got["ways"] == []


# -- the ledger and the learner end to end ------------------------------------------------------------------------


def test_the_distiller_writes_a_procedure_and_a_pitfall_then_a_repeat_is_a_new_version(store):
    P.record_run(store, tag_run())
    worker = Worker(store, [{"outcome": "worked_after_trouble", "operations": [GOOD_PROC, GOOD_PIT], "used": []}])
    learner = L.Learner(worker)
    res = learner.run_once(now=NOW + 200)
    assert len(res) == 1 and res[0].mode == "distill" and len(res[0].written["added"]) == 2
    assert worker.calls[0]["purpose"] == "practice_distill" and worker.calls[0]["model"] == L.LEARN_MODEL
    assert "[3] type_text in discord" in worker.calls[0]["user"] and "2002" not in worker.calls[0]["user"]
    proc = next(o for o in P.live(store) if o.kind == "procedure")
    assert proc.state == "candidate" and proc.steps[0].startswith("Ctrl+K") and proc.apps == ["discord"]
    assert P.run_header(store, "agent:S1:worker-1").learn == "done"
    # the same job again: the distiller's ADD lands near the live procedure: a new version, its ledger moved
    P.record_run(store, tag_run(request_id="worker-2", at=NOW + 300, ended_at=NOW + 330))
    worker.answers = [{"outcome": "worked", "operations": [GOOD_PROC], "used": []}]
    res = learner.run_once(now=NOW + 600)
    assert res[0].written["updated"] and not res[0].written["added"]
    procs = [o for o in P.live(store) if o.kind == "procedure"]
    assert len(procs) == 1 and procs[0].state == "active"           # two independent runs
    assert P.get(store, proc.id).superseded_by == procs[0].id


def test_a_clean_run_that_cited_a_playbook_labels_it_without_a_model(store):
    ids = _seed(store)
    run = tag_run(steps=[step("hotkey", playbook=f"pb:{ids['tag']}")], injected=[ids["tag"], ids["paste"]])
    P.record_run(store, run)
    worker = Worker(store, [])
    res = L.Learner(worker).run_once(now=NOW + 200)
    assert res[0].mode is None and res[0].labeled == [(ids["tag"], "ran_ok")] and not worker.calls
    types = [e.type for e in P.ledger(store, ids["tag"])]
    assert types.count("ran_ok") == 1 and "cited" in types and "injected" in types
    assert P.get(store, ids["tag"]).state == "active"


def test_a_post_mortem_writes_the_family_lesson_with_the_users_words(store):
    run = {"source": "agent", "session_id": "S2", "request_id": "worker-1", "lane": "voice", "at": NOW,
           "ended_at": NOW + 8, "words": "lower your volume", "reply": "Turned it down.", "outcome": "final",
           "steps": [step("hotkey", app="chrome", inp={"keys": ["volumedown"]}, verdict=True,
                          result="pressed volumedown")], "model_calls": 1, "wall_s": 8.0}
    P.record_run(store, run)
    store.add_turn("S2", "worker-1", NOW, "lower your volume", "Turned it down.", ["hotkey volumedown"], "final")
    store.add_turn("S2", "worker-2", NOW + 20, "No. No. No. Not my system's audio. I want you to turn your volume "
                   "down", "Done.", ["app_volume"], "final")
    lesson = {"op": "ADD", "kind": "pitfall", "intent": "lower or raise a volume", "apps": [],
              "text": "the media key lowered the whole system's volume; the user meant Yuki's own voice",
              "instead": "for 'your volume' set Yuki's own volume with app_volume; with no target, ask which",
              "family": "a volume request has three targets: the system, one app, Yuki itself", "from": [1],
              "quote": "Not my system's audio. I want you to turn your volume down"}
    worker = Worker(store, [{"outcome": "corrected", "cause": "system volume, not Yuki's", "operations": [lesson],
                             "used": []}])
    res = L.Learner(worker).run_once(now=NOW + 30)
    assert res[0].mode == "postmortem" and res[0].outcome == "corrected" and res[0].written["added"]
    assert "spoke again" in " ".join(res[0].reasons) or "bear on it" in " ".join(res[0].reasons)
    o = P.live(store, ["pitfall"])[0]
    assert o.family.startswith("a volume request has three targets") and o.state == "active"   # taught
    assert o.apps == ["chrome"]     # no app named: the app its evidence ran in (measured), so it is in play there
    assert "taught" in [e.type for e in P.ledger(store, o.id)]
    assert "No. No. No." in worker.calls[0]["user"]
    # the same lesson with words the user never said is refused
    P.record_run(store, dict(run, request_id="worker-3", at=NOW + 100, ended_at=NOW + 105))
    store.add_turn("S2", "worker-4", NOW + 110, "nope, wrong again", "", [], "final")
    worker.answers = [{"outcome": "corrected", "operations": [dict(lesson, quote="use app_volume always")],
                       "used": []}]
    res = L.Learner(worker).run_once(now=NOW + 130)
    assert not res[0].written["added"] and "not the user's own words" in res[0].rejected[0]


def test_the_learner_holds_runs_past_the_daily_budget(store):
    P.record_run(store, tag_run())
    store._write(lambda c: c.execute("INSERT INTO portrait_runs(at, kind, model, outcome, cost_usd) VALUES"
                                     " (?, 'practice_distill', 'x', 'ok', 0.2)", (NOW + 100,)))
    worker = Worker(store, [])
    res = L.Learner(worker).run_once(now=NOW + 200)
    assert res[0].skipped == "budget" and not worker.calls
    assert P.run_header(store, "agent:S1:worker-1").learn == "held"


def test_post_mortems_have_their_own_budget_and_hitting_a_limit_is_logged_once(store):
    """3 Oct: the shared budget was spent by 19:09 and every troubled run after it was held. A stopped run is still
    post-mortemed from its own pool; when that pool is spent too the run is held, logged once, and not looked at again
    until there is room."""
    def spend(kind: str, usd: float, at: float) -> None:
        store._write(lambda c: c.execute("INSERT INTO portrait_runs(at, kind, model, outcome, cost_usd) VALUES"
                                         " (?, ?, 'x', 'ok', ?)", (at, kind, usd)))

    spend("practice_distill", 0.2, NOW + 100)                         # the shared pool is spent
    P.record_run(store, tag_run(outcome="cancelled", request_id="worker-7"))
    worker = Worker(store, [{"outcome": "cancelled", "operations": [], "used": []}])
    learner = L.Learner(worker)
    [res] = learner.run_once(now=NOW + 200)
    assert res.mode == "postmortem" and res.skipped is None and len(worker.calls) == 1
    assert L.spent_today(store, NOW + 200, "postmortem")[1] == 1 and L.spent_today(store, NOW + 200, "shared")[0] == 0.2
    assert not [t for t, _f in worker.logged if t == "practice_budget"]
    # the post-mortems' own pool spent too: held, the limit logged once
    spend("practice_postmortem", L.POSTMORTEM_BUDGET_USD, NOW + 300)
    for n in (8, 9):
        P.record_run(store, tag_run(outcome="cancelled", request_id=f"worker-{n}", at=NOW + 300 + n,
                                    ended_at=NOW + 340 + n))
    out = learner.run_once(now=NOW + 3600)
    assert [r.skipped for r in out] == ["budget", "budget"] and len(worker.calls) == 1
    hits = [f for t, f in worker.logged if t == "practice_budget"]
    assert len(hits) == 1 and hits[0]["pool"] == "postmortem" and hits[0]["cap_usd"] == L.POSTMORTEM_BUDGET_USD
    assert store._query("SELECT learn_note FROM runs WHERE run_key='agent:S1:worker-8' AND step=0")[0][0].startswith(
        "budget (postmortem)")
    # still spent: the held runs are not decided again (no embedding, no write) and nothing more is logged
    assert learner.run_once(now=NOW + 3700) == [] and len(hits) == 1
    # the next local day the pool has room again and the held runs are learned
    worker.answers = [{"outcome": "cancelled", "operations": [], "used": []} for _ in range(2)]
    out = learner.run_once(now=NOW + DAY)
    assert [r.mode for r in out] == ["postmortem", "postmortem"] and len(worker.calls) == 3


def test_used_outcomes_reach_the_ledger_and_a_correction_counts_against_what_was_followed(store):
    ids = _seed(store)
    run = tag_run(injected=[ids["tag"], ids["warp"], ids["paste"]],
                  steps=[step("hotkey", playbook=f"pb:{ids['tag']}"), step("type_text", ok=False, verdict=False)],
                  steers=["not that way"])
    P.record_run(store, run)
    worker = Worker(store, [{"outcome": "corrected", "operations": [],
                             "used": [{"id": ids["tag"], "followed": True, "how": "trouble"},
                                      {"id": ids["warp"], "followed": True, "how": "ok"},   # shown, never cited
                                      {"id": ids["paste"], "how": "repeated"},              # a bad step in discord
                                      {"id": 777, "how": "ok"}]}])
    res = L.Learner(worker).run_once(now=NOW + 200)
    # corrected: what was cited and what was shown for the app the run acted in count against (fx2: never for it);
    # the Warp procedure, shown but of an app the run never acted in, is left alone
    assert res[0].written["used"] == [(ids["tag"], "user_corrected"), (ids["paste"], "user_corrected")]
    assert P.get(store, ids["tag"]).n_corrected == 1 and P.get(store, ids["warp"]).n_ran_ok == 0
    # an uncited procedure the run followed anyway in its app, with a bad step there, can be contradicted; one of an
    # app the run never acted in cannot
    run2 = tag_run(request_id="worker-5", at=NOW + 300, ended_at=NOW + 310, injected=[ids["tag"], ids["warp"]],
                   steps=[step("type_text", ok=True, verdict=False)], steers=["no"])
    P.record_run(store, run2)
    worker.answers = [{"outcome": "corrected", "operations": [],
                       "used": [{"id": ids["tag"], "how": "contradicted"}, {"id": ids["warp"], "how": "contradicted"}]}]
    res = L.Learner(worker).run_once(now=NOW + 600)
    assert res[0].written["used"] == [(ids["tag"], "contradicted")]


def _slider_case(store: Store, *, injected_extra: list[int] = ()) -> tuple[int, dict[str, Any]]:
    """29-build §3 item 5: a misleading pitfall of Chrome ("use the YouTube player's slider"), shown on a voice hand
    run that turned Chrome's mixer volume down (verified: the wrong target, its own report could not know) when the
    user meant Yuki's own voice; their correction 15 s later."""
    slider = P.add_playbook(store, {"kind": "pitfall", "intent": "lower the video's volume",
                                    "apps": ["chrome.exe"], "body": {"text": "the system keys moved everything",
                                                                     "instead": "drag the YouTube player's slider"}},
                            events=[{"type": "observed", "run_key": "r1", "at": NOW - DAY},
                                    {"type": "observed", "run_key": "r2", "at": NOW - 2 * DAY}],
                            vector=vec("lower the video's volume"), now=NOW)
    got = P.record_hand_trace(store, session_id="S9", name="app_volume", args={"app": "chrome.exe", "change": -20},
                              ok=True, verified=True, report="chrome.exe 80 -> 60", ms=90.0, at=NOW,
                              app="chrome.exe", target="the mixer volume of chrome.exe (that app only)",
                              words="Lower your volume.", request_id="voice-1-h1",
                              injected=[slider, *injected_extra])
    assert got["ok"]
    store.add_turn("S9", "voice-1-h1", NOW, "Lower your volume.", "", ["app_volume"], "final")
    store.add_turn("S9", "voice-2", NOW + 15, "No. No. No. Not my system's audio. I want you to turn your volume down.",
                   "", [], "final")
    lesson = {"op": "ADD", "kind": "pitfall", "intent": "lower or raise a volume", "apps": ["chrome.exe"],
              "text": "turned Chrome's volume down; the user meant Yuki's own voice",
              "instead": "for 'your volume' lower Yuki's own entry", "from": [1],
              "quote": "I want you to turn your volume down"}
    return slider, lesson


@pytest.mark.parametrize("outcome", ["corrected", "worked_after_trouble"])
def test_a_correction_counts_against_the_lesson_shown_never_for_it(store, outcome):
    """fx2 regression (29-build §3 item 5): on the run the user corrected, the learner credited the shown pitfall
    ``avoided`` (ran_ok: 0.618 -> 0.75) because the wrong step verified. A correction must count against what was
    shown or cited, whatever the pass says about it - also when the pass labels the outcome otherwise but writes a lesson
    on the user's own correction words."""
    other = P.add_playbook(store, {"kind": "pitfall", "intent": "switch to a Warp tab", "apps": ["warp"],
                                   "body": {"text": "Warp's tabs cannot be read", "instead": "read the title"}},
                           events=[{"type": "observed", "run_key": "r3", "at": NOW - DAY}], vector=vec("warp tab"),
                           now=NOW)
    slider, lesson = _slider_case(store, injected_extra=[other])
    before = P.get(store, slider).confidence
    worker = Worker(store, [{"outcome": outcome, "cause": "Chrome's volume, not Yuki's", "operations": [lesson],
                             "used": [{"id": slider, "how": "avoided"}, {"id": other, "how": "avoided"}]}])
    res = L.Learner(worker).run_once(now=NOW + 120)
    assert res[0].mode == "postmortem" and res[0].written["added"]
    assert res[0].written["used"] == [(slider, "user_corrected")]        # the Warp lesson: no app the run acted in
    o = P.get(store, slider)
    types = [e.type for e in P.ledger(store, slider)]
    assert "ran_ok" not in types and types.count("user_corrected") == 1
    assert o.n_ran_ok == 0 and o.n_corrected == 1 and o.confidence < before
    assert [e.type for e in P.ledger(store, other)].count("ran_ok") == 0


def test_a_pitfall_kept_clear_of_on_a_run_nobody_corrected_still_counts_for_it(store):
    """The other side: the same hand with no correction (the next words are a new request) keeps the old rule - a
    pitfall of the app it acted in, kept clear of, is credited."""
    slider, _lesson = _slider_case(store)
    worker = Worker(store, [{"outcome": "worked", "cause": "", "operations": [],
                             "used": [{"id": slider, "how": "avoided"}]}])
    res = L.Learner(worker).run_once(now=NOW + 120)
    assert res[0].written["used"] == [(slider, "ran_ok")]


# -- the nightly miner --------------------------------------------------------------------------------------------


def test_miner_candidates_need_three_times_on_two_days():
    tables = {"routes": [
        {"person": "Hemanth", "ref": "contact:1", "margin": 0.89, "close": False, "routes": [
            {"route": "Discord DM Quasar40404", "ref": "route:1", "kind": "dm", "mine": 78, "days": 6,
             "last": NOW, "day_ats": [NOW - d * DAY for d in range(6)]},
            {"route": "WhatsApp Hemanth", "ref": "route:2", "kind": "dm", "mine": 10, "days": 3, "last": NOW - 7 * DAY,
             "day_ats": [NOW - (7 + d) * DAY for d in range(3)]}]},
        {"person": "Once", "ref": "contact:2", "margin": 1.0, "close": False, "routes": [
            {"route": "Discord DM once", "ref": "route:3", "kind": "dm", "mine": 5, "days": 1, "last": NOW}]},
        {"person": "Twice", "ref": "contact:3", "margin": 1.0, "close": False, "routes": [
            {"route": "WhatsApp twice", "ref": "route:4", "kind": "dm", "mine": 2, "days": 2, "last": NOW}]}],
        "sites": [], "terminal": [], "surfaces": []}
    cands = L.candidates(tables)
    assert [c.entity for c in cands] == ["writing to Hemanth"]
    assert cands[0].count == 88 and cands[0].days == 6 and len(cands[0].day_ats) == 9


def test_the_miner_checks_citations_floors_and_answers_and_asks_through_the_questions_pipeline(store, monkeypatch):
    cands = [L.Candidate("c1", "route", "writing to Hemanth", {"Discord DM": "78 sent on 6 days"}, [], 88, 6,
                         day_ats=[NOW - d * DAY for d in range(6)]),
             L.Candidate("c2", "site", "youtube.com", {"chrome": "214 min"}, [], 2, 1)]   # under the floors
    answer = SimpleNamespace(answer="the DM, unless I'm in the VC", id=1, answered_at=NOW, origin="practice",
                             question="DM or WhatsApp?")
    tool = {"operations": [
        {"op": "ADD", "kind": "habit", "intent": "how to reach Hemanth", "text": "Discord DM, 78 sent", "ask": False,
         "default": "Discord DM", "cites": ["c1"], "quote": "the DM, unless I'm in the VC"},
        {"op": "ADD", "kind": "habit", "intent": "which browser for YouTube", "text": "Chrome", "cites": ["c2"]},
        {"op": "ADD", "kind": "habit", "intent": "made up", "text": "x", "cites": ["c9"]},
        {"op": "ADD", "kind": "habit", "intent": "quoted wrong", "text": "x", "cites": ["c1"], "quote": "always DM"}],
        "questions": [{"subject": "Hemanth", "question": "Discord DM or WhatsApp for Hemanth when you don't say?",
                       "cites": ["c1"]}, {"subject": "x", "question": "no cites", "cites": []}]}
    ops, questions, rejected = L.Miner.check(tool, cands, [], [answer])
    assert [o["intent"] for o in ops] == ["how to reach Hemanth"]
    assert len(rejected) == 4 and len(questions) == 1
    worker = Worker(store, [])
    miner = L.Miner(worker)
    written = miner.apply(ops, cands, NOW)
    o = P.get(store, written["added"][0])
    assert o.kind == "habit" and o.state == "active" and o.n_days == 6        # six days of evidence + the answer
    import yuki.memory.questions as Q

    monkeypatch.setattr(Q, "vet_new", lambda store, qs, **kw: list(qs))
    assert miner.ask(questions, cands, NOW) == 1
    q = [q for q in store.questions(("open",)) if q.origin == "practice"][0]
    assert q.question.startswith("Discord DM or WhatsApp")
    assert any(p["id"] == q.ref for p in P.workflows(store)["proposals"])     # a proposal on the Playbook page


def test_the_miner_skips_without_a_model_call_when_nothing_crosses_the_floors(store):
    worker = Worker(store, [])
    res = L.Miner(worker).run(now=NOW)
    assert res.skipped and not worker.calls


def test_the_worker_never_mines_during_a_call_a_game_or_while_the_user_is_busy(store, tmp_path):
    worker = Worker(store, [])
    live = {"fullscreen": False}
    idle = {"s": 9999.0}
    pw = L.PracticeWorker(store, worker, db_path=tmp_path / "memory.db", idle_fn=lambda: idle["s"],
                          live_fn=lambda: live, clock=lambda: NOW)
    live["fullscreen"] = True
    assert pw.blocked() == "fullscreen" and pw.tick().get("miner_waiting") == "fullscreen"
    live["fullscreen"] = False
    idle["s"] = 30.0
    assert pw.blocked() == "busy"
    idle["s"] = 9999.0
    cid = store.start_call(app="Discord", process="discord", via="mic", status="recording", at=NOW - 60)
    assert cid and pw.blocked() == "call"
    store.update_call(cid, status="ended", ended_at=NOW - 3600)
    assert pw.blocked() in (None, "call")   # (a just-ended call may still linger while its app holds the mic)
    (tmp_path / "paused").write_text("x")
    assert pw.tick() == {"paused": True}


def test_the_intake_and_the_miner_wait_while_the_shared_budget_is_spent(store, tmp_path):
    """Every practice call but the user's own Playbook edits is inside a daily pool, so the day's total is bounded."""
    store.add_knowhow("spotify", "Focus the Spotify window first before clicking Pause.")
    store._write(lambda c: c.execute("INSERT INTO portrait_runs(at, kind, model, outcome, cost_usd) VALUES"
                                     " (?, 'practice_distill', 'x', 'ok', ?)", (NOW - 60, L.DAILY_BUDGET_USD)))
    worker = Worker(store, [])
    pw = L.PracticeWorker(store, worker, db_path=tmp_path / "memory.db", idle_fn=lambda: 9999.0,
                          live_fn=lambda: {}, clock=lambda: NOW)
    out = pw.tick()
    assert "intake" not in out and "miner" not in out and not worker.calls
    assert L.Intake(worker).waiting()                                    # the row waits for a day with room
    hits = [f for t, f in worker.logged if t == "practice_budget"]
    assert len(hits) == 1 and hits[0]["pool"] == "shared"
    pw.tick()
    assert len([t for t, _f in worker.logged if t == "practice_budget"]) == 1   # logged once a day


# -- the Playbook page ------------------------------------------------------------------------------------------------


def test_workflows_lists_procedures_lessons_and_proposals_with_uses_and_confidence(store):
    ids = _seed(store)
    page = P.workflows(store)
    assert page["available"] is True
    by = {c["id"]: c for c in page["workflows"]}
    assert set(by) == {f"pb:{i}" for i in ids.values()}
    assert [c["id"] for c in page["lessons"]] == [f"pb:{ids['volume']}"]            # taught: active
    assert {c["id"] for c in page["proposals"]} == {f"pb:{ids['tag']}", f"pb:{ids['paste']}", f"pb:{ids['warp']}"}
    card = by[f"pb:{ids['tag']}"]
    assert card["uses"] == 0 and card["confidence"] == 0.67 and card["state"] == "candidate" and card["steps"]


def test_workflow_edit_goes_through_the_ledger_and_an_edit_is_re_distilled_in_the_users_words(store):
    ids = _seed(store)
    assert P.workflow_edit(store, f"pb:{ids['tag']}", "save")["state"] == "active"
    assert "taught" in [e.type for e in P.ledger(store, ids["tag"])]
    assert P.workflow_edit(store, f"pb:{ids['warp']}", "edit")["ok"] is False          # an edit needs words
    queued = P.workflow_edit(store, f"pb:{ids['warp']}", "edit", "use ctrl+1 to ctrl+9, the tabs are numbered")
    assert queued["queued"] is True and P.pending_edits(store)[0][0].id == ids["warp"]
    upd = {"op": "UPDATE", "id": ids["warp"], "kind": "procedure", "intent": "switch to a Warp tab by its name",
           "apps": ["warp"], "steps": [{"do": "press ctrl and the tab's number", "from": []}],
           "quote": "use ctrl+1 to ctrl+9, the tabs are numbered"}
    worker = Worker(store, [{"outcome": "worked", "operations": [upd], "used": []}])
    out = L.Learner(worker).apply_edit(*P.pending_edits(store)[0], now=NOW)
    new = out["written"]["updated"][0]
    assert P.get(store, new).steps == ["press ctrl and the tab's number"] and P.get(store, ids["warp"]).valid_to
    assert P.pending_edits(store) == []
    assert P.workflow_edit(store, "pb:424242", "save")["ok"] is False
    assert P.workflow_edit(store, f"pb:{new}", "rename")["ok"] is False


def test_the_memory_api_exposes_the_playbook_page_by_the_names_the_window_probes(store):
    from yuki.memory.api import MemoryClient

    client = MemoryClient(store)
    _seed(store)
    assert callable(getattr(client, "workflows", None)) and callable(getattr(client, "workflow_edit", None))
    assert client.workflows()["available"] is True
    ref = client.workflows()["workflows"][0]["id"]
    assert client.workflow_edit(ref, "disable")["ok"] is True


# -- know-how intake -------------------------------------------------------------------------------------------------


def test_intake_converts_old_know_how_without_window_numbers_and_skips_talking_rules(store):
    k1 = store.add_knowhow("spotify", "Focus the Spotify window first with focus_window before clicking Pause. Not: "
                                      "the click failed (expected hwnd 32967468, got 10551598).")
    k2 = store.add_knowhow(None, "When the user says 'Scroll down', perform a scroll action. Not: stayed quiet.")
    answer = {"rows": [
        {"row": k1, "operations": [
            {"op": "ADD", "kind": "procedure", "intent": "pause Spotify", "apps": ["spotify"],
             "steps": [{"do": "focus the Spotify window, then click Button 'Pause'", "from": []}]},
            {"op": "ADD", "kind": "pitfall", "intent": "click in a window not in front", "apps": ["spotify"],
             "text": "a click on a window that lost focus failed (expected hwnd 32967468)"}]},
        {"row": k2, "skip": "a rule about how Yuki talks", "operations": []}]}
    worker = Worker(store, [answer])
    out = L.Intake(worker).run(now=NOW)
    assert len(out["added"]) == 1 and out["skipped"] == [{"row": k2, "why": "a rule about how Yuki talks"}]
    assert "window ids" in out["rejected"][0]
    o = P.get(store, out["added"][0])
    assert o.kind == "procedure" and o.origin == "knowhow" and o.apps == ["spotify"]
    assert store.source_checkpoint(L.INTAKE_CHECKPOINT) == str(k2) and L.Intake(worker).waiting() == []


# -- the task frame and the agent's memory -------------------------------------------------------------------------


def _practice(store: Store, words: str, apps: list[str]) -> dict[str, Any]:
    out = P.relevant(store, query_vec=vec(words), apps_in_play=apps)
    return out


def test_the_frame_shows_playbooks_and_keeps_the_pitfalls_when_it_is_over_its_size(store):
    from yuki.agent import frame as F

    ids = _seed(store)
    practice = _practice(store, "tag Hemanth in my usual Discord channel", ["discord"])
    fr = F.build({}, practice=practice)
    assert f"[pb:{ids['tag']}]" in fr.text and f"[pb:{ids['paste']}]" in fr.text
    assert fr.procedures == 1 and fr.pitfalls == 1 and set(fr.playbooks) == {ids["tag"], ids["paste"]}
    assert F.PITFALL_HEAD in fr.text and "first attempt" in fr.text
    # a frame whose person block alone fills it: the person lines give way, the pitfalls stay
    facts = {"named": [{"id": 1, "name": "Hemanth", "words": "Hemanth"}],
             "routes": {1: {"name": "Hemanth", "margin": 0.9, "routes": [
                 {"ref": f"route:{i}", "label": f"Discord DM route number {i} " + "x" * 300, "mine": 3, "theirs": 2,
                  "app": "Discord"} for i in range(3)]}}}
    small = F.build(facts, practice=practice, chars=900)
    assert f"[pb:{ids['paste']}]" in small.text and len(small.text) <= 900
    assert ids["paste"] in small.playbooks


def test_memory_context_uses_practice_memory_instead_of_know_how_lines(store):
    from yuki.agent.memory import MemoryAccess
    from yuki.memory.api import MemoryClient

    client = MemoryClient(store)
    client._embedder = Words()
    store.add_knowhow("discord", "an old know-how line")
    ids = _seed(store)
    access = MemoryAccess(open_client=lambda: client)
    ctx = access.context(app="Warp.exe", query="tag Hemanth in my Discord channel", open_apps=["Discord.exe"])
    assert ctx.practice is not None and ctx.knowhow == []
    assert ids["paste"] in [p["id"] for p in ctx.practice["pitfalls"]]        # Discord open, not in front
    assert [w["id"] for w in ctx.practice["ways"]] == [ids["tag"]]


def test_the_core_prompt_carries_the_playbook_contract_and_actions_declare_the_playbook():
    from yuki.agent.prompt import system_blocks
    from yuki.agent.tools import ACTION_TOOL_NAMES, PLAYBOOK_FIELD, TOOLS_BY_NAME

    core = system_blocks()[0]["text"]
    assert "A playbook is your first attempt" in core and "playbook field" in core
    for name in ACTION_TOOL_NAMES:
        assert PLAYBOOK_FIELD in TOOLS_BY_NAME[name]["input_schema"]["properties"]
    assert "learned" not in TOOLS_BY_NAME["done"]["input_schema"].get("required", [])


def test_the_hand_trace_api_queues_through_memory_access(tmp_path):
    from yuki.agent.memory import MemoryAccess
    from yuki.memory.api import MemoryClient

    client = MemoryClient(Store.open(tmp_path / "memory.db"))
    access = MemoryAccess(open_client=lambda: client, session_id="voice-1")
    assert access.record_hand_trace(name="app_volume", args={"app": "Yuki.exe", "change": -20}, ok=True,
                                    verified=True, report="Yuki's volume 80 -> 60", ms=90.0, app="Yuki.exe",
                                    words="lower your volume")
    assert access.flush_turns(5)
    head = P.pending_runs(client.store)[0]
    assert head.source == "hand" and head.session_id == "voice-1" and head.words == "lower your volume"
    client.close()
