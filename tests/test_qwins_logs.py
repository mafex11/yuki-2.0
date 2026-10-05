"""Report 76 quick win 3: logs are kept 14 days, and the agents' session logs and the request ledger keep counts, ids
and timings only (never the user's words, screen text or replies). Temp folders only; invented words."""

from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path

import pytest

from yuki.log.events import LOG_CONTENT_ENV, SessionLogger, content_free, content_kept
from yuki.log.requests import append_request_row
from yuki.log.retention import KEEP_NAMES, prune_logs

SECRET_WORDS = "send Mirela the plum invoice"   # invented: must never reach a content-free file
SCREEN = "Inbox - Quentin Vale - Plumtree Mail"


def _age(path: Path, days: float, now: float) -> None:
    t = now - days * 86400.0
    os.utime(path, (t, t))


# -- retention --------------------------------------------------------------------------------------------------------


def test_prune_logs_deletes_old_files_keeps_new_ones_and_the_ledgers(tmp_path):
    now = time.time()
    (tmp_path / "sessions" / "20260901-000000").mkdir(parents=True)
    (tmp_path / "memory").mkdir()
    old = [tmp_path / "sessions" / "20260901-000000-worker.jsonl", tmp_path / "memory" / "journal-20260901.jsonl",
           tmp_path / "sessions" / "20260901-000000" / "shot-1.png", tmp_path / "memory-20260901-000000.jsonl"]
    new = [tmp_path / "sessions" / "20261004-000000-worker.jsonl", tmp_path / "memory" / "journal-20261004.jsonl"]
    ledgers = [tmp_path / name for name in KEEP_NAMES]
    for p in old + new + ledgers:
        p.write_text("x" * 10, encoding="utf-8")
    for p in old + ledgers:
        _age(p, 20, now)
    _age(tmp_path / "sessions" / "20260901-000000", 20, now)
    out = prune_logs(tmp_path, now=now)
    assert out["files"] == len(old) and out["bytes"] == 10 * len(old)
    assert not any(p.exists() for p in old)
    assert all(p.exists() for p in new + ledgers)
    # the emptied screenshot folder goes on a later pass (deleting its file just moved its time)
    _age(tmp_path / "sessions" / "20260901-000000", 1, now)
    assert prune_logs(tmp_path, now=now)["dirs"] == 1
    assert not (tmp_path / "sessions" / "20260901-000000").exists() and (tmp_path / "sessions").exists()


def test_prune_logs_never_raises_on_a_missing_folder(tmp_path):
    assert prune_logs(tmp_path / "nope") == {"files": 0, "bytes": 0, "dirs": 0, "skipped": 0}
    assert prune_logs(tmp_path, days=0)["files"] == 0


def test_the_memory_service_prunes_logs_with_its_other_prune_pass():
    src = (Path(__file__).resolve().parents[1] / "yuki" / "memory" / "service.py").read_text(encoding="utf-8")
    assert "prune_logs(logs_dir())" in src


# -- content-free session logs ----------------------------------------------------------------------------------------


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_content_free_logger_keeps_counts_ids_and_timings_only(tmp_path):
    log = SessionLogger(tmp_path, session_id="t-worker", quiet=True, content=False)
    rec = log.log("user_message", text=SECRET_WORDS)
    assert rec["text"] == SECRET_WORDS                       # the caller still gets the record whole
    log.llm_request(model="claude-x", system=[{"type": "text", "text": "You are Yuki."}],
                    messages=[{"role": "user", "content": SECRET_WORDS}],
                    tools=[{"name": "open_app", "description": "Opens an app."}], max_tokens=900, stream=True)
    log.llm_response(content=[{"type": "text", "text": SECRET_WORDS},
                              {"type": "tool_use", "id": "tu_1", "name": "send_message", "input": {"text": SECRET_WORDS}}],
                     stop_reason="tool_use", usage={"input_tokens": 120, "output_tokens": 30}, latency_ms=812.0)
    log.tool_call("send_message", {"to": "Mirela", "text": SECRET_WORDS}, tool_use_id="tu_1")
    log.tool_result("read_window", ok=True, summary=SCREEN, result={"title": SCREEN}, elapsed_ms=41.0,
                    tool_use_id="tu_1")
    try:
        raise ValueError(f"no window called {SCREEN}")
    except ValueError as exc:
        log.error(f"failed on {SCREEN}", exc=exc)
    log.final(SECRET_WORDS)
    log.request_summary({"request": SECRET_WORDS, "lane": "worker", "outcome": "final", "wall_s": 3.2,
                         "tool_calls": {"send_message": 1}, "cost_usd": 0.01})
    log.close()
    text = log.path.read_text(encoding="utf-8")
    for word in ("Mirela", "plum", "Quentin", "Plumtree", "You are Yuki", "Opens an app"):
        assert word not in text
    recs = {r["type"]: r for r in _records(log.path)}
    assert recs["user_message"]["text_chars"] == len(SECRET_WORDS)
    req = recs["llm_request"]
    assert req["model"] == "claude-x" and req["messages"]["count"] == 1 and req["tools"] == ["open_app"]
    assert req["max_tokens"] == 900 and req["system_chars"] > 0
    resp = recs["llm_response"]
    assert [b["type"] for b in resp["content"]] == ["text", "tool_use"] and resp["content"][1]["name"] == "send_message"
    assert resp["usage"]["input_tokens"] == 120 and resp["latency_ms"] == 812.0
    call = recs["tool_call"]
    assert call["name"] == "send_message" and call["input_keys"] == ["text", "to"] and call["input_chars"] > 0
    res = recs["tool_result"]
    assert res["ok"] is True and res["elapsed_ms"] == 41.0 and res["summary_chars"] == len(SCREEN)
    err = recs["error"]
    assert "ValueError" in err["traceback"] and 'File "' in err["traceback"]
    assert recs["request_summary"]["request_chars"] == len(SECRET_WORDS)
    assert recs["request_summary"]["tool_calls"] == {"send_message": 1}


def test_a_full_logger_is_unchanged(tmp_path):
    log = SessionLogger(tmp_path, session_id="t-full", quiet=True)
    log.user_message(SECRET_WORDS)
    log.close()
    assert SECRET_WORDS in log.path.read_text(encoding="utf-8")


def test_content_free_record_without_the_heavy_fields_keeps_numbers():
    out = content_free({"type": "takeover", "state": "paused", "why": "keys", "n": 2, "error": "OSError: no such"})
    assert out["state"] == "paused" and out["n"] == 2 and out["why_chars"] == 4
    assert out["error_kind"] == "OSError" and "error" not in out


def test_the_window_falls_back_to_memory_for_a_content_free_log(tmp_path):
    from yuki.ui.chat_model import history_from_session_log

    log = SessionLogger(tmp_path, session_id="t-worker", quiet=True, content=False)
    log.user_message(SECRET_WORDS)
    log.final("Done.")
    log.close()
    assert history_from_session_log(log.path) == []


# -- the request ledger -----------------------------------------------------------------------------------------------


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_the_request_ledger_keeps_no_words_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv(LOG_CONTENT_ENV, raising=False)
    path = tmp_path / "requests.csv"
    append_request_row(path, {"timestamp": "2026-10-05T01:00:00", "lane": "worker", "request": SECRET_WORDS,
                              "wall_s": 2.5, "outcome": "final", "tool_calls": {"open_app": 1}})
    row = _rows(path)[0]
    assert row["request"] == "" and row["wall_s"] == "2.5" and row["outcome"] == "final"
    assert row["tool_calls"] == "open_app=1"
    assert SECRET_WORDS not in path.read_text(encoding="utf-8")


def test_the_developer_switch_keeps_the_words(tmp_path, monkeypatch):
    monkeypatch.setenv(LOG_CONTENT_ENV, "1")
    assert content_kept()
    path = tmp_path / "requests.csv"
    append_request_row(path, {"request": "open the notes"})
    assert _rows(path)[0]["request"] == "open the notes"


@pytest.mark.parametrize("value", ["", "0", "yes"])
def test_only_exactly_one_turns_the_words_on(monkeypatch, value):
    monkeypatch.setenv(LOG_CONTENT_ENV, value)
    assert not content_kept()


def test_the_desktop_agents_log_content_free():
    src = (Path(__file__).resolve().parents[1] / "yuki" / "ui" / "runtime.py").read_text(encoding="utf-8")
    assert src.count("content=content_kept()") == 2   # the worker's and the front desk's session logs


def test_the_window_takes_a_requests_words_from_memory(tmp_path, monkeypatch):
    # review F7: requests.csv keeps no words now; Home's "while you were away" and Activity's request rows take them
    # from memory's exchanges (matched by start time), so each row is still told apart; no match -> "A request"
    from datetime import datetime

    from test_web_window import _Memory
    from yuki.ui import web_data

    monkeypatch.delenv(LOG_CONTENT_ENV, raising=False)
    now = time.time()
    path = tmp_path / "requests.csv"
    started = [now - 1200.6, now - 600.2, now - 300.0]
    for at in started:   # the agent stamps the row with the request's start, to the second
        append_request_row(path, {"timestamp": datetime.fromtimestamp(at).isoformat(timespec="seconds"),
                                  "session": "s", "lane": "worker", "request": SECRET_WORDS, "wall_s": 3.0,
                                  "outcome": "final"})
    assert SECRET_WORDS not in path.read_text(encoding="utf-8")

    class _WithTurns(_Memory):
        def turns_on(self, day, limit=200):
            return [{"at": started[0], "user": "open my budget sheet"}, {"at": started[1], "user": "pause the music"},
                    {"at": now - 4000, "user": "an older one"}]

    home = web_data.build_home(_WithTurns(), requests_csv=path, now=now)
    titles = sorted(a["title"] for a in home["away"] if a["kind"] == "request")
    assert titles == ["A request", "“open my budget sheet”", "“pause the music”"]
    activity = web_data.build_activity(_WithTurns(), requests_csv=path, now=now)
    rows = {r["what"]: r for r in activity["rows"] if r["kind"] == "request"}
    assert "“pause the music”" in rows and "pause the music" in rows["“pause the music”"]["wrong_text"]
    assert rows["A request"]["wrong_text"] == ""
    assert web_data.request_words_from_memory(object(), [{"at": now, "what": ""}]) == [{"at": now, "what": ""}]
