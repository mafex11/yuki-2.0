"""Hotfix 5 Oct 2026: the strict conversation tools' grammar size, and the re-queue of the turns it cost.

Release 2 added ``for_yuki`` (an inline integer array) to the strict ``record_conversation_memory`` tool; Bedrock then
refused every extraction call ("The compiled grammar is too large ..."). Measured on the real service (Haiku 4.5,
5 Oct 2026, one cheap forced call per schema):

* the schema before ``for_yuki`` (9 inline arrays) compiled, but not with one more string field: no room at all;
* with ``for_yuki`` (10 inline arrays): 400. Without any one inline array (``for_yuki``, ``operations.exchanges`` or
  ``operations.keyterms``): compiles. Without a string field (``operations.reason``, ``knowhow.avoid``) or the
  10-value ``importance`` enum instead: still 400. ``for_yuki`` as a string instead of an array: still 400;
* the four exchange-number lists as one ``$defs`` entry referenced four times (the fix): compiles, and still compiles
  with one more string field; with two more number-list references: 400.

So the size that matters is the schema's structure (inline arrays most, then fields), not its descriptions. These
tests hold the strict tools to that measured budget: a schema that grows past it fails here, before it can fail every
call in the field. To grow it, measure the new schema with one real call first and update the budget.

Temp databases and invented ids only; no model is called.
"""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

#: Measured 5 Oct 2026 (see above): the fixed extraction schema compiles, and so does it with one more field.
BUDGET = {"properties": 46, "inline_arrays": 7, "refs": 4, "enum_values": 51}


def schema_size(node: Any, acc: dict[str, int] | None = None) -> dict[str, int]:
    """Structure counts of a JSON schema, a proxy for its compiled grammar's size (``$defs`` counted once)."""
    acc = acc if acc is not None else {"properties": 0, "inline_arrays": 0, "refs": 0, "enum_values": 0}
    if isinstance(node, dict):
        acc["inline_arrays"] += node.get("type") == "array"
        acc["refs"] += "$ref" in node
        acc["enum_values"] += len(node.get("enum") or [])
        acc["properties"] += len(node.get("properties") or {})
        for key, value in node.items():
            if key == "$defs":
                for d in value.values():
                    schema_size(d, acc)
            elif key not in ("enum", "required", "description"):
                schema_size(value, acc)
    elif isinstance(node, list):
        for item in node:
            schema_size(item, acc)
    return acc


def inline(node: Any, defs: dict[str, Any]) -> Any:
    """The schema with every ``$ref`` written out in place (what it was before the fix)."""
    if isinstance(node, dict):
        if "$ref" in node:
            base = dict(defs[node["$ref"].rsplit("/", 1)[-1]])
            base.update({k: v for k, v in node.items() if k != "$ref"})
            return inline(base, defs)
        return {k: inline(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [inline(x, defs) for x in node]
    return node


def strict_tools() -> dict[str, dict[str, Any]]:
    from yuki.memory import conversations

    return {name: value for name, value in vars(conversations).items()
            if name.isupper() and isinstance(value, dict) and value.get("strict") is True and "input_schema" in value}


def over_budget(size: dict[str, int]) -> dict[str, tuple[int, int]]:
    return {k: (size[k], BUDGET[k]) for k in BUDGET if size[k] > BUDGET[k]}


def test_every_strict_conversation_tool_stays_within_the_measured_grammar_budget():
    tools = strict_tools()
    assert {"RECORD_CONVERSATION_TOOL", "RECORD_KNOWHOW_TOOL", "SAVE_SUMMARY_TOOL"} <= set(tools)
    for name, tool in tools.items():
        size = schema_size(tool["input_schema"])
        assert not over_budget(size), (
            f"{name}'s strict schema grew past the grammar budget measured on Bedrock {over_budget(size)}: "
            "measure the new schema with one real call before raising BUDGET (5 Oct 2026: every extraction failed)")


def test_the_budget_catches_the_schema_that_broke_release_2():
    from yuki.memory.conversations import RECORD_CONVERSATION_TOOL

    schema = RECORD_CONVERSATION_TOOL["input_schema"]
    broken = inline(schema, schema["$defs"])        # release 2: the same schema with the number lists inline
    assert over_budget(schema_size(broken)) == {"inline_arrays": (10, BUDGET["inline_arrays"])}
    grown = json.loads(json.dumps(schema))           # one more inline list on the fixed schema
    grown["properties"]["extra"] = {"type": "array", "items": {"type": "integer"}}
    assert over_budget(schema_size(grown))


def test_the_fix_changes_no_accepted_json_and_no_description():
    """Written out in place, the fixed tools are the release-2 tools exactly (same fields, types, descriptions)."""
    from yuki.memory.conversations import NUMBERS_DEFS, RECORD_CONVERSATION_TOOL, RECORD_KNOWHOW_TOOL

    for tool in (RECORD_CONVERSATION_TOOL, RECORD_KNOWHOW_TOOL):
        schema = tool["input_schema"]
        assert schema["$defs"] == NUMBERS_DEFS        # every $ref resolves inside the tool sent
        flat = inline(schema, schema["$defs"])
        assert "$ref" not in json.dumps(flat)
    props = inline(RECORD_CONVERSATION_TOOL["input_schema"], NUMBERS_DEFS)["properties"]
    for field in ("not_for_yuki", "for_yuki"):
        assert props[field]["type"] == "array" and props[field]["items"] == {"type": "integer"}
        assert props[field]["description"]
    assert props["operations"]["items"]["properties"]["exchanges"]["items"] == {"type": "integer"}
    assert props["knowhow"]["items"]["properties"]["exchanges"]["description"].startswith("Numbers [n]")
    assert "for_yuki" in RECORD_CONVERSATION_TOOL["input_schema"]["required"]


# -- the re-queue --------------------------------------------------------------------------------------------------

GRAMMAR = ("BadRequestError: Error code: 400 - {'message': 'The compiled grammar is too large, which would cause "
           "performance issues. Simplify your tool schemas or reduce the number of strict tools.'}")


@pytest.fixture()
def store():
    from yuki.memory.store import Store

    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def _turn(store: Any, n: int, attempts: int, extracted: int = 0) -> int:
    tid = store.add_turn("s-hot", f"r{n}", 1_791_150_000.0 + n, f"invented words {n}", "Okay.", [], "done")
    store._write(lambda conn: conn.execute("UPDATE conversation_turns SET extract_attempts=?, extracted=? WHERE id=?",
                                           (attempts, extracted, tid)))
    return tid


def _attempts(store: Any, tid: int) -> int:
    return store._query("SELECT extract_attempts FROM conversation_turns WHERE id=?", (tid,))[0][0]


def test_requeue_gives_back_only_the_attempts_the_grammar_error_took(store, tmp_path):
    from yuki.memory import grammar_requeue as g

    given_up = _turn(store, 1, 3)       # three grammar failures: given up
    mixed = _turn(store, 2, 3)          # one other error, then two grammar failures
    done = _turn(store, 3, 1, extracted=1)
    other = _turn(store, 4, 3)          # given up for another reason: stays given up
    rows = [{"type": "conversation_error", "purpose": "extract", "turn_ids": [given_up, mixed, done], "error": GRAMMAR},
            {"type": "conversation_error", "purpose": "extract", "turn_ids": [given_up, mixed], "error": GRAMMAR},
            {"type": "conversation_error", "purpose": "extract", "turn_ids": [given_up], "error": GRAMMAR},
            {"type": "conversation_error", "purpose": "extract", "turn_ids": [mixed, other], "error": "Timeout"},
            {"type": "conversation_error", "purpose": "summary", "turn_ids": [other], "error": GRAMMAR},
            {"type": "conversation_extract", "turn_ids": [done]}]
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "conversations-20261005.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n",
                                                       encoding="utf-8")
    failures, per_turn, other_errors = g.grammar_failures(g.read_conversation_logs(logs))
    assert failures == 3 and per_turn == Counter({given_up: 3, mixed: 2, done: 1})
    assert other_errors == Counter({mixed: 1, other: 1})

    p = g.plan(store, failures, per_turn, other_errors)
    assert p.requeue == {given_up: (3, 0), mixed: (3, 1)} and p.already_extracted == 1
    undo_file = tmp_path / "undo.json"
    assert g.apply(store, p, undo_file) == sorted([given_up, mixed])
    assert (_attempts(store, given_up), _attempts(store, mixed), _attempts(store, other)) == (0, 1, 3)
    assert {t.id for t in store.turns_pending(3)} == {given_up, mixed}
    assert g.plan(store, failures, per_turn, other_errors).requeue == {}   # run twice: nothing more to do

    assert sorted(g.undo(store, undo_file)["restored"]) == sorted([given_up, mixed])
    assert (_attempts(store, given_up), _attempts(store, mixed)) == (3, 3)
    assert store.turns_pending(3) == []


def test_requeue_dry_run_reads_only_and_apply_is_exact(store, tmp_path):
    from yuki.memory import grammar_requeue as g

    tid = _turn(store, 1, 3)
    pending = _turn(store, 2, 1)        # one grammar failure, still in the queue: the worker retries it
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "conversations-20261005.jsonl").write_text(
        json.dumps({"type": "conversation_error", "purpose": "extract", "turn_ids": [tid, pending],
                    "error": GRAMMAR}) + "\n",
        encoding="utf-8")
    db = Path(store.path)
    assert g.main(["--db", str(db), "--logs", str(logs)]) == 0
    assert _attempts(store, tid) == 3                               # the dry run changed nothing
    p = g.plan(store, *g.grammar_failures(g.read_conversation_logs(logs)))
    assert p.requeue == {tid: (3, 2)}                              # one logged grammar failure
    store._write(lambda conn: conn.execute("UPDATE conversation_turns SET extract_attempts=2 WHERE id=?", (tid,)))
    assert g.apply(store, p, tmp_path / "undo.json") == []          # changed since the plan: left alone
    assert _attempts(store, tid) == 2
