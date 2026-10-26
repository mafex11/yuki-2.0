"""Hotfix 5 Oct 2026: the strict conversation tools' grammar size. (The one-off re-queue of the turns it cost,
``yuki.memory.grammar_requeue``, was deleted on 18 Oct with its tests.)

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

Schemas only; no model is called.
"""

from __future__ import annotations

import json
from typing import Any

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
