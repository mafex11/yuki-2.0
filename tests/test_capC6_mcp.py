"""capC6 (report 78 §5.4, §12 C6; report 101): the read-only MCP tools over the workflow graph.

``get_workflows`` (families and usual paths, by words or by app, with the app's go-tos, shortcuts and usual next
steps) and ``get_tasks`` (tasks in a time range with goal, apps and outcome; tasks the user rejected left out): both
registered read-only, plain text, starting with the 'Covers:' line in the PC's local time like every other tool, and
the server's instructions say what they hold. Calls are logged content-free.

Offline: temp databases, invented apps, names and goals; a stand-in embedder.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from test_c_practice import Words
from test_capC2_families import task, three_reports
from test_capC2_links import NOW, act, store  # noqa: F401 - the store fixture
from yuki.mcp import server as S
from yuki.mcp.times import utc_offset
from yuki.memory import trace_families as F
from yuki.memory import trace_tasks as T


def _text(result: Any) -> str:
    assert not getattr(result, "is_error", False), result
    return "\n".join(getattr(c, "text", "") for c in getattr(result, "content", None) or [])


def _server(store: Any, tmp_path: Path) -> tuple[Any, Any]:
    server, memory = S.build_server(store.path, tmp_path / "mcplogs")
    memory.client()._embedder = Words()       # the stand-in for the service's embedder (no model loaded)
    return server, memory


def _iso(at: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(at).strftime("%Y-%m-%dT%H:%M")


def test_both_tools_are_registered_read_only_with_their_arguments(store, tmp_path):
    server, memory = _server(store, tmp_path)
    try:
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
    finally:
        memory.close()
    for name in ("get_workflows", "get_tasks"):
        ann = tools[name].annotations
        assert ann.read_only_hint is True and ann.destructive_hint is False and ann.open_world_hint is False
    assert {"query", "app", "limit"} <= set(tools["get_workflows"].input_schema["properties"])
    assert {"since", "until", "limit"} <= set(tools["get_tasks"].input_schema["properties"])
    assert tools["get_tasks"].input_schema["properties"]["limit"]["maximum"] == 40
    # the instructions describe them in the same voice
    assert "Record how I work" in S.INSTRUCTIONS and "usual path" in S.INSTRUCTIONS
    assert "not theirs are left out" in S.INSTRUCTIONS


def test_get_workflows_by_words_and_by_app_in_plain_text_with_a_local_time_header(store, tmp_path):
    three_reports(store)
    F.refresh(store, Words(), now=NOW + 3 * 86400)
    act(store, NOW + 3 * 86400, "chatter", "click", role="Button", name="Attach", key="k-attach")
    store._write(lambda c: c.execute(
        "INSERT INTO ui_transitions(process, from_key, to_key, n, median_gap_s, gaps, last_at) VALUES (?,?,?,?,?,?,?)",
        ("chatter", "k-attach", "k-file", 5, 3.0, "[]", NOW)))
    from yuki.memory import trace_habits

    trace_habits.update(store, now=NOW + 3 * 86400 + 10)               # the go-tos' tallies (C1b's tick)
    server, memory = _server(store, tmp_path)
    try:
        by_words = _text(asyncio.run(server.call_tool("get_workflows", {"query": "send the weekly report to the team"})))
        by_app = _text(asyncio.run(server.call_tool("get_workflows", {"app": "chatter.exe"})))
        none = _text(asyncio.run(server.call_tool("get_workflows", {"query": "water the tomato plants"})))
    finally:
        memory.close()
    head = by_words.splitlines()[0]
    assert head.startswith("Covers: workflows learned from the user's own clicks and keys, as of ")
    assert f"(local time, {utc_offset()})" in head
    assert 'Workflows nearest to "send the weekly report to the team"' in by_words
    assert '1. "You sent' in by_words and "done 3 times" in by_words and "apps: chatter" in by_words
    assert "Usual path (how many of the last 3 times took each step):" in by_words
    assert '1. in chatter: click Button "Attach" (3 of 3)' in by_words
    assert "Another way, 1 of the last 3 times, at the start: in chatter: press Ctrl+K" in by_words
    assert "outcome: typed text sent (checked by the PC)" in by_words
    assert "weekly-report" not in by_words                            # a content name: never
    assert "In chatter" in by_app and 'Goes to most: Button "Attach"' in by_app
    assert 'after Button "Attach", an unnamed ListItem (5 times, about 3 s later)' in by_app
    assert none.splitlines()[0].startswith("Covers:") and "Nothing near that." in none
    # content-free call log: the tool and its argument names, never the words
    logs = [json.loads(x) for f in (tmp_path / "mcplogs").glob("mcp-*.jsonl") for x in f.read_text("utf-8").splitlines()]
    assert {e["tool"] for e in logs} == {"get_workflows"} and all(e["ok"] for e in logs)
    assert "weekly" not in json.dumps(logs) and "tomato" not in json.dumps(logs)


def test_get_workflows_without_the_embedder_says_so_and_lists_by_recency(store, tmp_path):
    three_reports(store)
    F.refresh(store, Words(), now=NOW + 3 * 86400)
    server, memory = S.build_server(store.path, tmp_path / "mcplogs")

    class Down:
        def embed(self, texts: list[str], wait_s: float | None = None) -> Any:
            raise TimeoutError("no embedder")

    memory.client()._embedder = Down()
    try:
        out = _text(asyncio.run(server.call_tool("get_workflows", {"query": "send the report"})))
    finally:
        memory.close()
    assert "Could not rank by meaning right now" in out and '1. "You sent' in out


def test_get_workflows_on_an_empty_memory_says_how_it_learns(store, tmp_path):
    server, memory = _server(store, tmp_path)
    try:
        out = _text(asyncio.run(server.call_tool("get_workflows", {})))
    finally:
        memory.close()
    assert out.startswith("Covers:") and "No workflows yet." in out and "Record how I work" in out


def test_get_tasks_lists_goals_apps_and_outcomes_and_leaves_rejected_ones_out(store, tmp_path):
    reports = three_reports(store)
    unclear = task(store, NOW + 3600, "You read the seed swap forum", [("webby", "click", "Button", "Next", "k-n", None)])
    F.refresh(store, Words(), now=NOW + 3 * 86400)
    T.correct_task(store, reports[1], "not_mine")
    T.correct_task(store, reports[2], "renamed", "You posted the report for the team")
    server, memory = _server(store, tmp_path)
    try:
        out = _text(asyncio.run(server.call_tool("get_tasks", {"since": _iso(NOW - 3600), "until": _iso(NOW + 3 * 86400)})))
        capped = _text(asyncio.run(server.call_tool("get_tasks", {"since": _iso(NOW - 3600),
                                                                  "until": _iso(NOW + 3 * 86400), "limit": 1})))
    finally:
        memory.close()
    lines = out.splitlines()
    assert lines[0].startswith("Covers: ") and f"local time, {utc_offset()}" in lines[0]
    assert "3 tasks, oldest first" in lines[1]
    assert "You sent the weekly report to the team | apps: chatter | outcome: typed text sent, checked by the PC at" in out
    assert "You sent the weekly report to the team chat" not in out   # the user said it was not theirs
    assert "You posted the report for the team (the user's own words)" in out
    assert "You read the seed swap forum | apps: webby | outcome: not checked (maybe not done)" in out
    assert "(1 task in this range the user said was not theirs or not one task: left out.)" in out
    assert "(2 more in this range: narrow since/until.)" in capped
    assert f"task:{unclear}" not in out and f"task:{reports[0]}" not in out   # no ids for the reader


def test_get_tasks_default_is_today_and_empty_says_how_it_learns(store, tmp_path):
    server, memory = _server(store, tmp_path)
    try:
        out = _text(asyncio.run(server.call_tool("get_tasks", {})))
    finally:
        memory.close()
    assert out.startswith("Covers: ") and "0 tasks" in out and "No tasks in this range." in out


def test_get_tasks_with_only_until_reads_everything_before_it(store, tmp_path):
    """Second verification V4: the Covers line says 'everything before'; the read must not stop at that day's
    midnight."""
    reports = three_reports(store)                                    # NOW, NOW + 1 day, NOW + 2 days
    server, memory = _server(store, tmp_path)
    try:
        out = _text(asyncio.run(server.call_tool("get_tasks", {"until": _iso(NOW + 2 * 86400 + 3600)})))
    finally:
        memory.close()
    assert out.splitlines()[0].startswith("Covers: everything before ")
    assert "3 tasks, oldest first" in out and "No tasks in this range" not in out
    assert len(reports) == 3
