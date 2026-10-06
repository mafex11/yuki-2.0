"""capC1 (report 96): the acceptance tools run on invented data: the §10.1 analyzer (with its typed-marker scan of
every column and the logs) on a temp database, and the on/off cost report on an invented log.
"""

from __future__ import annotations

import importlib.util
import json
import time
from datetime import datetime
from pathlib import Path

from yuki.memory import trace_store
from yuki.memory.store import Store

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _session_db(tmp: Path) -> tuple[Path, float]:
    db = tmp / "memory.db"
    s = Store.open(db)
    t0 = datetime.now().replace(second=0, microsecond=0).timestamp() - 600
    rows = [
        {"at": t0 + 10, "process": "chrome", "kind": "click", "source": "user", "resolved": "event", "role": "Button",
         "name_digest": "n1", "element_key": "k1", "name_ciphertext": s._enc("Invented button")},
        {"at": t0 + 20, "process": "chrome", "kind": "click", "source": "user", "resolved": "point", "role": "Button",
         "name_digest": "n1", "element_key": "k1"},
        {"at": t0 + 30, "process": "discord", "kind": "type", "source": "user_injected", "typed": 14,
         "resolved": "focus"},
        {"at": t0 + 40, "process": "discord", "kind": "click", "source": "user", "during_run": 1,
         "resolved": "unresolved"},
        {"at": t0 + 50, "process": "fl64", "kind": "click", "source": "user", "resolved": "unresolved"},
        {"at": t0 + 60, "process": "warp", "kind": "command", "source": "yuki", "resolved": "app_data"},
    ]
    trace_store.write_batch(s, rows)
    s.close()
    return db, t0


def test_the_analyzer_reports_the_numbers_and_finds_no_typed_marker(tmp_path):
    mod = _load("trace_acceptance")
    db, t0 = _session_db(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "memory-1.jsonl").write_text(json.dumps({"type": "stats", "trace": {"rows": 6}}) + "\n", encoding="utf-8")
    report = mod.analyze(db, t0, t0 + 3600, {"password": (t0 + 100, t0 + 160)}, steps=False)
    assert report["rows"] == 6 and report["tree_app_named_share"] == 0.667 and report["takeovers"] == 1
    assert report["valorant_rows"] == 0 and report["fl_studio_rows"] == 1
    assert report["keys_per_control"] == {"chrome": 1.0} and report["quiet_spans"] == {"password": 0}
    assert mod.scan_for_marker(db, "violet-otter-4417", logs) == {}
    checks = {what: ok for what, ok, _v in mod.verdict(report, {})}
    assert checks["zero typed characters in the trace's tables or the memory logs"] is True
    assert checks["VALORANT: zero rows"] is True and checks["FL Studio maximised: rows present"] is True


def test_the_marker_scan_finds_typed_text_in_a_decrypted_column_or_a_log(tmp_path):
    mod = _load("trace_acceptance")
    db = tmp_path / "memory.db"
    s = Store.open(db)
    trace_store.write_batch(s, [{"at": time.time(), "process": "x", "kind": "click", "source": "user",
                                 "name_ciphertext": s._enc("violet otter 4417")}])
    s.close()
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "memory-2.jsonl").write_text('{"type": "x", "note": "violet-otter-4417"}\n', encoding="utf-8")
    hits = mod.scan_for_marker(db, "violet-otter-4417", logs)
    assert hits == {"ui_actions.name_ciphertext": 1, "log:memory-2.jsonl": 1}


def test_the_marker_seen_by_the_ambient_capture_is_reported_apart_and_missing_logs_fail_loudly(tmp_path, capsys):
    """Review F7: other tables are not blamed on the trace; no memory logs means the check cannot pass silently."""
    mod = _load("trace_acceptance")
    trace_hits, other = mod.split_hits({"ui_actions.name_ciphertext": 1, "log:memory-2.jsonl": 1,
                                        "captures.delta_ciphertext": 3})
    assert trace_hits == {"ui_actions.name_ciphertext": 1, "log:memory-2.jsonl": 1}
    assert other == {"captures.delta_ciphertext": 3}
    checks = {what: ok for what, ok, _v in mod.verdict(
        {"valorant_rows": 0, "fl_studio_rows": 1, "takeovers": 1}, {"captures.delta_ciphertext": 3})}
    assert checks["zero typed characters in the trace's tables or the memory logs"] is True
    db, t0 = _session_db(tmp_path)
    empty = tmp_path / "empty-logs"
    empty.mkdir()
    rc = mod.main(["--db", str(db), "--since", "00:00", "--until", "23:59", "--typed", "violet-otter-4417",
                   "--logs", str(empty)])
    assert rc == 2 and "No memory-*.jsonl logs" in capsys.readouterr().err
    ab = _load("trace_ab_report")
    assert ab.main(["--logs", str(empty)]) == 2


def test_the_checklist_is_plain_words_and_covers_the_session():
    mod = _load("trace_acceptance")
    text = mod.CHECKLIST
    for must in ("Chrome", "T3 Code", "Warp", "Discord", "Explorer", "take over", "Handy", "G Hub", "Deskflow",
                 "VALORANT", "FL Studio", "password field", "private browser window", "blocked list", "dead key",
                 "Alt+0169"):
        assert must in text, must
    assert "ui_actions" not in text and "sqlite" not in text


def test_the_on_off_report_compares_hours(tmp_path):
    mod = _load("trace_ab_report")
    logs = tmp_path / "logs"
    logs.mkdir()
    now = time.time()
    lines = [json.dumps({"ts": now - 100, "type": "trace_ab", "hour": 10,
                         "samples": {"on:chrome": {"mean": 2.0, "n": 60}, "on:__service__": {"mean": 0.6, "n": 60}}}),
             json.dumps({"ts": now - 50, "type": "trace_ab", "hour": 11,
                         "samples": {"off:chrome": {"mean": 1.9, "n": 60}, "off:__service__": {"mean": 0.5, "n": 60}}})]
    (logs / "memory-3.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    data = mod.collect(logs, now - 3600)
    assert mod.weighted(data["chrome"]["on"]) == (2.0, 60)
    assert mod.main(["--logs", str(logs), "--json"]) == 0
