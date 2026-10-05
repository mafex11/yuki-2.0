"""Replay the outcome verdict (report 87, honest) over past agent session logs: counts only.

Reads the agents' session logs (``<log dir>/sessions/*-worker.jsonl`` and ``*-frontdesk.jsonl``, read-only), rebuilds
each request's action steps from its ``action_verdict`` and ``send_confirmation`` events, and grades it with
``yuki.agent.finish.outcome_verdict`` as the agent now does at its end. Prints counts only: requests, how many acted,
the level of each, sends seen or not, and how many closing messages would no longer be said as written (below
likely). Never prints or keeps any words of a request, reply or step.

Run from the repo with the project's interpreter::

    python scripts/replay_outcomes.py [--logs DIR]

``DIR`` defaults to ``%LOCALAPPDATA%/Yuki/logs/sessions``. A window number stands in for the step's app (the logs keep
no app per verdict), so a retry is matched by its window.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yuki.agent.finish import SAYABLE, outcome_verdict  # noqa: E402


def _events(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event


def requests(path: Path) -> Iterator[dict[str, Any]]:
    """One dict per request in a session log: ``steps`` (run-trace-shaped), ``unbacked`` and whether it ended in a
    closing message (``final``) or by the finishing action (``by_action``)."""
    current: dict[str, Any] | None = None
    pending_send: str | None = None
    calls: dict[str, dict[str, Any]] = {}   # the latest call of each tool not graded yet (its compared fields)
    for event in _events(path):
        kind = event.get("type")
        if kind == "user_message":
            if current is not None:
                yield current
            current = {"steps": [], "unbacked": [], "final": False, "by_action": False}
            pending_send = None
            calls: dict[str, dict[str, Any]] = {}
            continue
        if current is None:
            continue
        if kind == "tool_call" and isinstance(event.get("input"), dict):
            # review: the fields the verdict compares (the app an input names, a window's operation), nothing else
            calls[str(event.get("name"))] = {k: event["input"][k] for k in ("app", "action") if k in event["input"]}
        elif kind == "send_confirmation":
            # logged just before the send's own action_verdict
            pending_send = "confirmed" if event.get("confirmed") else "unconfirmed"
        elif kind == "action_verdict":
            why = str(event.get("why") or "")
            step = {"kind": "action", "tool": event.get("tool"), "ok": why != "it failed",
                    "verdict": event.get("verified"), "why": why, "app": event.get("target"),
                    "input": calls.pop(str(event.get("tool")), {}), "call": event.get("turn")}
            if pending_send:
                # the window number stands in for the chat too (the logs keep no message fingerprint): a retry is
                # matched by its window, as the live trace matches it by its window's title
                step["send"], step["window"], pending_send = pending_send, event.get("target"), None
            current["steps"].append(step)
        elif kind == "claim_accepted":
            current["unbacked"].append("a claim the check could not back")
        elif kind == "finished_by_action":
            current["by_action"] = True
        elif kind == "final":
            current["final"] = True
        elif kind == "request_summary":
            yield current
            current = None
    if current is not None:
        yield current


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Yuki" / "logs" / "sessions"
    parser.add_argument("--logs", type=Path, default=base)
    args = parser.parse_args(argv)
    files = sorted([*args.logs.glob("*-worker.jsonl"), *args.logs.glob("*-frontdesk.jsonl")])
    counts: Counter[str] = Counter()
    levels: Counter[str] = Counter()
    for path in files:
        for req in requests(path):
            counts["requests"] += 1
            if not req["final"]:
                continue
            counts["ended_with_words"] += 1
            verdict = outcome_verdict(req["steps"], unbacked=req["unbacked"], finished_by_action=req["by_action"])
            levels[verdict["level"]] += 1
            if verdict["acted"]:
                counts["acted"] += 1
            if verdict["send"]:
                counts[f"send_{verdict['send']}"] += 1
            if verdict["level"] not in SAYABLE:
                counts["no_longer_said_as_written"] += 1
                if req["by_action"]:
                    counts["of_which_finished_by_action"] += 1
            if verdict["partial"]:
                counts["partial"] += 1
    print(f"session logs read: {len(files)}")
    for key in ("requests", "ended_with_words", "acted", "send_confirmed", "send_unconfirmed",
                "no_longer_said_as_written", "of_which_finished_by_action", "partial"):
        print(f"{key}: {counts.get(key, 0)}")
    for level in ("verified", "likely", "unverified", "failed", "answer"):
        print(f"level {level}: {levels.get(level, 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
