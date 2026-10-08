"""capC3 (report 78 §10.3, §7; report 105): the three arms on the same snapshot, plan-only.

B0 (learning off), B1 (copy the nearest past example) and L (the learning build) plan on one window snapshot; the
thinker is called with only the plan tool (forced: nothing that acts exists in the call) and every call is costed;
a learned way is replayed on the snapshot first and hands over at the first step off its path. Synthetic world from
test_capC3_world; the model is a stand-in.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from test_c_practice import Words
from test_capC2_links import store  # noqa: F401 - the store fixture
from test_capC3_world import APP, NAMES, REQUEST, StubThinker, _hermetic, own_request, world  # noqa: F401
from yuki.eval import c3_arms as A
from yuki.eval import c3_data as D
from yuki.eval import c3_score as S


def _ready(store: Any, **kw: Any) -> tuple[dict[str, Any], D.Instance, Any]:
    w = world(store, **kw)
    [inst], _ = D.build_instances(store, [own_request()], Words(), names=NAMES)
    D.cut(store, inst.cutoff, embedder=Words())
    return w, inst, D.request_vector(store, Words(), REQUEST, inst.snapshot.open_processes())


def _block(user: str) -> str:
    return user.split("BEGIN_UNTRUSTED_DATA", 1)[1].split("END_UNTRUSTED_DATA", 1)[0]


def test_the_three_arms_plan_on_the_same_snapshot_with_the_plan_tool_only(store):
    w, inst, vec = _ready(store, learned=False)
    stub, budget = StubThinker(cost=0.02), A.Budget(1.0)
    b1 = A.example_arm(store, inst, vec=vec, now=inst.cutoff)
    b0 = A.yuki_arm(store, inst, stub, budget, learned=False, vec=vec, now=inst.cutoff)
    lv = A.yuki_arm(store, inst, stub, budget, learned=True, vec=vec, now=inst.cutoff)
    assert len(stub.calls) == 2                                           # B0 and L; B1 is code only
    assert (_block(stub.calls[0]["user"]) == _block(stub.calls[1]["user"])
            == " (the screen snapshot)\n" + inst.snapshot.text() + "\n")
    assert all(c["user"].count(f'Request: "{REQUEST}"') == 1 for c in stub.calls)
    for c in stub.calls:                                                  # plan-only: the plan tool and nothing else
        assert c["tool"] is A.PLAN_TOOL and c["tool"]["name"] == "record_plan"
        assert "PLAN ONLY" in c["system"] and c["system"].startswith("You are Yuki.")
    # every element an arm planned is a control of that one snapshot (or a machine step of one of its windows)
    keys = inst.snapshot.keys()
    for res in (b0, lv):
        assert all(t in keys or "|" in t for t in res.tokens)
    # costs counted per arm and in the run's budget
    assert (b0.cost_usd, lv.cost_usd, b1.cost_usd) == (0.02, 0.02, 0.0) and budget.calls == 2
    assert budget.spent == 0.04
    # learning off: no usual path, example or go-tos in its frame; learning on: the usual path is there
    assert "How the user usually does this" not in stub.calls[0]["user"] and b0.frame_chars == 0
    assert "How the user usually does this" in stub.calls[1]["user"] and lv.frame_chars > 0
    assert b0.mode == lv.mode and b0.mode in ("switch", "fallback")
    ref = inst.reference
    sc = {a: S.score(r.tokens, ref)["composite"] for a, r in (("B0", b0), ("B1", b1), ("L", lv))}
    assert sc["L"] > sc["B1"] > sc["B0"]


def test_a_learned_way_runs_first_on_the_snapshot_and_needs_no_model_call(store):
    w, inst, vec = _ready(store, learned=True)
    stub = StubThinker()
    lv = A.yuki_arm(store, inst, stub, A.Budget(1.0), learned=True, vec=vec, now=inst.cutoff)
    assert lv.way and lv.stopped is None and lv.calls == 0 and not stub.calls
    assert S.score(lv.tokens, inst.reference)["composite"] == 100.0
    assert lv.words[1] == f"press Ctrl+K in {APP}"
    # learning off: the way the trace taught is left out, so today's Yuki plans with the thinker
    b0 = A.yuki_arm(store, inst, stub, A.Budget(1.0), learned=False, vec=vec, now=inst.cutoff)
    assert not b0.way and b0.calls == 1


def test_the_way_stops_at_the_first_step_off_its_path_and_hands_the_rest_to_the_thinker(store):
    w, inst, vec = _ready(store, learned=True)
    inst.snapshot.elements = [e for e in inst.snapshot.elements if e.key != "k-msg"]   # the message field is gone
    stub = StubThinker()
    lv = A.yuki_arm(store, inst, stub, A.Budget(1.0), learned=True, vec=vec, now=inst.cutoff)
    assert lv.way and lv.stopped == "the control was not on the screen" and lv.calls == 1
    assert lv.tokens[:4] == inst.reference[:4]                            # the steps done before it stopped
    msg = stub.calls[0]["user"]
    assert "It stopped at step 5: the control was not on the screen" in msg and "do not redo them" in msg
    # a window that was not open, with no step of the way bringing it up first, stops it too
    item = {"run": [{"do": "type", "slot": "message"}], "id": None}
    assert A.run_way(store, item, inst.snapshot)[2:] == (1, "nothing to type into")
    snap = D.Snapshot([D.Window("w1", APP, APP, open=False)],
                      [D.Element("e1", "w1", APP, "k-msg", "Edit", "an unnamed Edit")])
    item = {"run": [{"do": "click", "role": "Edit", "name": "x"}], "id": None}
    assert A.run_way(store, item, snap)[2:] == (1, "the control was not on the screen")
    snap.elements[0].name = "x"
    assert A.run_way(store, item, snap)[2:] == (1, "its window was not open")
    item = {"run": [{"do": "open_app", "app": APP}, {"do": "click", "role": "Edit", "name": "x"}], "id": None}
    assert A.run_way(store, item, snap) == ([f"{APP}|switch", "k-msg"], [f"bring {APP} to the front",
                                                                        f"an unnamed Edit in {APP}"], None, None)
    item = {"run": [{"do": "open_app", "app": "elsewhere"}], "id": None}
    assert A.run_way(store, item, snap)[2:] == (1, "another app than when you did it")
    item = {"run": [{"do": "keys", "app": APP, "keys": ["Ctrl", "K"]}], "id": None}   # keys into a window not up
    assert A.run_way(store, item, snap)[2:] == (1, "its window was not open")
    snap.windows[0].open = True
    assert A.run_way(store, item, snap)[0] == [f"{APP}|keys|Ctrl+K"]


def test_a_plan_becomes_route_steps_by_the_snapshots_ids():
    snap = D.Snapshot([D.Window("w1", "chatter", "Chatter", True, front=True), D.Window("w2", "notepadx", "Notes",
                                                                                          False)],
                      [D.Element("e1", "w1", "chatter", "k-edit", "Edit", "an unnamed Edit"),
                       D.Element("e2", "w2", "notepadx", "k-doc", "Document", 'Document "Notes"')])
    data = {"steps": [{"do": "keys", "keys": "Ctrl+K"},                          # no window: the one in front
                      {"do": "click", "element": "e1"}, {"do": "type", "text": "hi"},   # typing: the focus
                      {"do": "keys", "window": "w1", "keys": "Enter"},
                      {"do": "open_app", "window": "w2"}, {"do": "type", "element": "e2", "text": "x"},
                      {"do": "save_file", "window": "w2"}, {"do": "command"}, {"do": "copy", "window": "w2"},
                      {"do": "click", "element": "e99"}, {"do": "scroll", "window": "w1"}, "junk", {"do": "other"}]}
    toks, words = A.plan_tokens(data, snap)
    assert toks == ["chatter|keys|Ctrl+K", "k-edit", "k-edit", "chatter|keys|Enter", "notepadx|switch", "k-doc",
                    "notepadx|file_save", "notepadx|command", "notepadx|copy", "?unknown:e99"]
    assert words[0] == "press Ctrl+K in Chatter" and words[-1] == "a control not on the screen"
    assert A.plan_tokens({"nothing": 1}, snap) == ([], []) and A.plan_tokens(None, snap) == ([], [])
    # an id the snapshot never had counts against the plan, never for it
    assert S.score(["?unknown:e99"], ["k-edit"])["composite"] == 0.0


class _Stream:
    def __init__(self, response: Any) -> None:
        self.response = response

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def get_final_message(self) -> Any:
        return self.response


class _Client:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(stream=self._stream)

    def prompt_caching(self) -> str:
        return "explicit"

    def _stream(self, **request: Any) -> _Stream:
        self.requests.append(request)
        block = SimpleNamespace(type="tool_use", name="record_plan", input={"steps": [{"do": "click", "element": "e1"}]})
        return _Stream(SimpleNamespace(content=[block], usage={"input_tokens": 1000, "output_tokens": 200},
                                       stop_reason="tool_use"))


def test_the_thinker_is_called_plan_only_with_the_plan_tool_forced_and_costed():
    priced: list[Any] = []
    settings = SimpleNamespace(model="sonnet", voice_brain_model="",
                               estimate_cost=lambda m, t: priced.append((m, t)) or 0.0123)
    client = _Client()
    planner = A.ThinkerPlanner(settings, client=client)
    data, cost = planner.plan(system="SYS", user="hello", tool=A.PLAN_TOOL)
    assert data == {"steps": [{"do": "click", "element": "e1"}]} and cost == 0.0123
    [req] = client.requests
    assert req["tools"] == [A.PLAN_TOOL] and req["tool_choice"] == {"type": "tool", "name": "record_plan"}
    assert "thinking" not in req and req["max_tokens"] == A.PLAN_MAX_TOKENS
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"} and req["system"][0]["text"] == "SYS"
    assert priced[0][1]["output_tokens"] == 200 and req["model"] == planner.model
    # the estimate: the whole output budget at most, half of it as typical
    assert planner.estimate(3000) == 0.0123 and priced[-1][1]["output_tokens"] == A.PLAN_MAX_TOKENS
    planner.estimate(3000, typical=True, cached=30_000)
    assert priced[-1][1]["output_tokens"] == A.PLAN_MAX_TOKENS // 2
    assert priced[-1][1]["cache_read_tokens"] == 10_000 and priced[-1][1]["input_tokens"] == 1_300
    # no price in the settings: list prices, never zero
    planner = A.ThinkerPlanner(SimpleNamespace(model="sonnet", voice_brain_model="", estimate_cost=lambda m, t: None),
                               client=client)
    assert planner.estimate(3000) > 0.0
