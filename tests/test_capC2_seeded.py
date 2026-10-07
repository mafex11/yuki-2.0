"""capC2 (report 78 §10.2; report 98): the seeded-session analyzer (scripts/trace_seeded_session.py) on a synthetic
database: six scripted tasks with 2-minute pauses between them, interleaved (task 4 in two parts around task 5),
recovered with the right outcome facts by a stand-in model; a task built from a Yuki or stale row is caught.

Offline: a temp database, invented apps; the model is a stand-in.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from test_capC2_links import NOW, act, store  # noqa: F401 - the store fixture
from test_capC2_tasks import Worker, episode

_SPEC = importlib.util.spec_from_file_location(
    "trace_seeded_session", Path(__file__).resolve().parents[1] / "scripts" / "trace_seeded_session.py")
S = importlib.util.module_from_spec(_SPEC)
sys.modules["trace_seeded_session"] = S
_SPEC.loader.exec_module(S)

PAUSE = 150.0


def session(store: Any, t0: float) -> float:
    """The script, done: each block's actions a few seconds apart, a 2.5-minute pause between blocks."""
    t = t0

    def at(step: float = 4.0) -> float:
        nonlocal t
        t += step
        return t

    # 1. copy in the browser, paste and send in a chat
    act(store, at(), "webby", "copy", resolved="event", clip_seq=31)
    act(store, at(), "chatter", "click", role="ListItem", key="k-self", position="0/List")
    act(store, at(), "chatter", "paste", resolved="focus", role="Edit", key="k-msg", clip_seq=31)
    act(store, at(), "chatter", "keys", resolved="focus", role="Edit", key="k-msg", chord="Enter",
        facts={"field_submitted": {"typed": 0}})
    at(PAUSE)
    # 2. Notepad, save a new file (with one of Yuki's rows and a stale click among them: never a task's)
    act(store, at(), "notepadx", "type", resolved="focus", role="Document", key="k-doc", typed=30)
    act(store, at(), "notepadx", "click", role="Button", name="Format", key="k-fmt", source="yuki")
    act(store, at(), "notepadx", "click", role="Button", name="Edit", key="k-ed", resolved="stale")
    act(store, at(), "notepadx", "keys", resolved="focus", role="Document", key="k-doc", chord="Ctrl+S")
    act(store, at(), "notepadx", "file_save", resolved="event", file_digest="d-seed", file_ext=".txt",
        facts={"file_saved": "d-seed"})
    at(PAUSE)
    # 3. a Warp command that works
    act(store, at(), "warp", "command", resolved="app_data", exit_code=0, facts={"command_ok": True})
    at(PAUSE)
    # 4a. a link in the browser
    act(store, at(), "webby", "click", role="Hyperlink", key="k-l1", facts={"page_reached": "pg-1"})
    act(store, at(), "webby", "nav", resolved="event", page_key="pg-1", facts={"page_reached": "pg-1"})
    at(PAUSE)
    # 5. a setting off and on
    act(store, at(), "settingsx", "click", role="CheckBox", name="Transparency effects", key="k-tr",
        facts={"toggle_state": "off"})
    act(store, at(), "settingsx", "click", role="CheckBox", name="Transparency effects", key="k-tr",
        facts={"toggle_state": "on"})
    at(PAUSE)
    # 4b. one more link
    act(store, at(), "webby", "click", role="Hyperlink", key="k-l2", facts={"page_reached": "pg-2"})
    act(store, at(), "webby", "nav", resolved="event", page_key="pg-2", facts={"page_reached": "pg-2"})
    at(PAUSE)
    # 6. open an older file from Explorer
    act(store, at(), "explorer", "file_open", resolved="event", file_digest="d-old", file_ext=".png",
        facts={"file_opened": "d-old"})
    return at(10)


def labeller(kw: dict[str, Any]) -> dict[str, Any]:
    """A stand-in model that reads the step lines like the real one should: each piece's last recorded outcome."""
    user = kw["user"]
    pieces: dict[str, list[str]] = {}
    current = None
    for line in user.splitlines():
        head = line.split(" ", 1)[0]
        if head.startswith("p") and head[1:].isdigit():
            current = head
            pieces[current] = []
        elif current and line.startswith("a") and "{f" in line:
            pieces[current] += [part.split(" ", 1)[0] for part in line.split("{")[1:]]
    order = sorted(pieces, key=lambda p: int(p[1:]))
    by_task = [[order[0]], [order[1]], [order[2]], [order[3], order[5]], [order[4]], [order[6]]]
    goals = ["You shared a sentence in a chat", "You saved a short note in Notepad", "You ran a command in Warp",
             "You followed links in the browser", "You switched transparency off and on", "You opened an old picture"]
    return {"tasks": [{"pieces": ps, "goal": g, "outcome": pieces[ps[-1]][-1] if pieces[ps[-1]] else "none"}
                      for ps, g in zip(by_task, goals)]}


def test_the_analyzer_recovers_the_six_scripted_tasks_and_finds_them_clean(store):
    end = session(store, NOW)
    episode(store, NOW, end)
    worker = Worker([labeller])
    out = S.analyze(store, NOW - 1, end + 1, worker, now=end + 60)
    assert out["pieces"] == 7 and out["outcome"] == "ok" and out["tasks"] == 6
    assert [m["kind"] for m in out["matches"]] == ["paste_sent", "file_saved", "command_ok", "page_reached",
                                                   "toggle_state", "file_opened"]
    assert out["recovered"] == 6 and out["unclean"] == [] and out["passed"] is True
    text = S.report(out)
    assert "Recovered 6 of 6" in text and "PASS" in text and "no task built from a stale row or one of Yuki's" in text


def test_the_analyzer_fails_below_five_and_catches_a_task_built_from_yukis_row(store):
    end = session(store, NOW)
    episode(store, NOW, end)

    def weak(kw: dict[str, Any]) -> dict[str, Any]:
        ans = labeller(kw)
        for t in ans["tasks"][:2]:
            t["outcome"] = "none"
        return ans

    out = S.analyze(store, NOW - 1, end + 1, Worker([weak]), now=end + 60)
    assert out["recovered"] == 4 and out["passed"] is False and "FAIL" in S.report(out)
    yuki = store._query("SELECT id FROM ui_actions WHERE source = 'yuki'")[0][0]
    tid = store._query("SELECT id FROM ui_tasks WHERE outcome_fact = 'file_saved' OR goal_ciphertext IS NOT NULL"
                       " ORDER BY started_at LIMIT 1 OFFSET 1")[0][0]
    store._write(lambda c: c.execute("UPDATE ui_tasks SET outcome_action = ? WHERE id = ?", (yuki, tid)))
    ok, why = S.built_cleanly(store, tid)
    assert not ok and "Yuki" in why


def test_the_dry_run_makes_no_call_and_the_script_reads_plainly(store):
    end = session(store, NOW)
    worker = Worker([])
    out = S.analyze(store, NOW - 1, end + 1, worker, dry_run=True)
    assert out["dry_run"] and out["pieces"] == 7 and out["lines"] >= 13 and not worker.calls
    assert "No model call" in S.report(out)
    assert S.SCRIPT_TEXT.count("\n  PAUSE\n") == 6 and "2 full minutes" in S.SCRIPT_TEXT
    assert len(S.SCRIPT) == 6 and S.PASS_AT == 5
    assert {"paste_sent", "file_saved", "command_ok"} <= {e["kind"] for e in S.SCRIPT}
