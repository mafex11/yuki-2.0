"""evalFix: phase F's moments backfill bars (PLAN.md Appendix B.1) on a copy, through a stand-in for Yuki's own
``moments rebuild`` (fMoments builds the real one): E1 on all 7 weekdays within a minute with the expected evidence,
no join for FL Studio / VALORANT / Settings, Discord on 15 evenings, YouTube pages with an entity, the rebuild's time
and idempotence; the hand-check sample; the time split. Each bar is shown to fail when its fact is missing.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from test_evalFix_world import (Rebuild, good_moments, hermetic, live, ts, write_moments)  # noqa: F401
from yuki.eval import dbcopy
from yuki.eval import habits_replay as H


@pytest.fixture()
def copy(live):
    path = dbcopy.copy_db(live)
    yield path
    dbcopy.remove_copy(path)


def test_every_bar_passes_on_a_correct_rebuild(copy):
    run = Rebuild(copy)
    out = H.moments_bars(copy, runner=run)
    assert run.calls == [["rebuild"], ["rebuild"]]
    b = out["bars"]
    assert b["e1"]["found"] == 7 and b["e1"]["evidence_ok"] == 7, b["e1"]
    assert b["e1"]["weekend_morning_joins"] == 0
    assert b["not_meetings"]["total"] == 0
    assert b["discord_evenings"]["evenings"] == 15 and b["discord_evenings"]["window_days"] == 16
    assert b["youtube_pages"]["youtube_with_entity"] == 2
    assert b["runtime"]["pass"] and b["idempotent"]["pass"]
    assert out["result"] == "pass"
    assert out["moments"]["by_kind"]["game_voice"] == 3
    assert any(day for day in out["moments"]["per_day"])


def test_the_8_oct_join_is_required(copy):
    t = ts("2026-10-08", "08:56:52")
    items = [m for m in good_moments(copy) if not (m[0] == "call_join" and abs(m[2] - t) < 90)]
    out = H.moments_bars(copy, runner=Rebuild(copy, items))
    day = next(d for d in out["bars"]["e1"]["days"] if d["day"] == "2026-10-08")
    assert not day["found"] and not out["bars"]["e1"]["pass"] and out["result"] == "fail"


def test_a_join_more_than_a_minute_off_does_not_count(copy):
    t = ts("2026-10-07", "08:55:59")
    items = [(k, a, at + 75 if k == "call_join" and abs(at - t) < 1 else at, s, i, e, x)
             for k, a, at, s, i, e, x in good_moments(copy)]
    out = H.moments_bars(copy, runner=Rebuild(copy, items))
    assert not next(d for d in out["bars"]["e1"]["days"] if d["day"] == "2026-10-07")["found"]


def test_a_hold_day_marked_seen_fails_the_evidence_check(copy):
    t = ts("2026-09-28", "09:01:01")
    items = [(k, a, at, s, i, "screen" if k == "call_join" and abs(at - t) < 1 else e, x)
             for k, a, at, s, i, e, x in good_moments(copy)]
    out = H.moments_bars(copy, runner=Rebuild(copy, items))
    e1 = out["bars"]["e1"]
    assert e1["found"] == 7 and e1["evidence_ok"] == 6 and not e1["pass"]


@pytest.mark.parametrize("app", ["fl64", "valorant-win64-shipping", "systemsettings"])
def test_any_join_of_an_app_that_was_never_a_meeting_fails(copy, app):
    items = good_moments(copy) + [("call_join", app, ts("2026-10-01", "09:00:00"), "timeline", 999_001, "hold", None)]
    out = H.moments_bars(copy, runner=Rebuild(copy, items))
    assert out["bars"]["not_meetings"]["by_app"][app] == 1 and not out["bars"]["not_meetings"]["pass"]


def test_a_games_voice_is_not_a_join(copy):
    out = H.moments_bars(copy, runner=Rebuild(copy))
    assert out["bars"]["not_meetings"]["pass"] and out["moments"]["by_kind"].get("game_voice") == 3


def test_fourteen_discord_evenings_fail(copy):
    first = ts("2026-09-25", "00:18:00")
    items = [m for m in good_moments(copy) if not (m[1] == "discord" and abs(m[2] - first) < 1)]
    out = H.moments_bars(copy, runner=Rebuild(copy, items))
    assert out["bars"]["discord_evenings"]["evenings"] == 14 and not out["bars"]["discord_evenings"]["pass"]


def test_a_discord_join_in_the_morning_is_no_evening(copy):
    items = [m for m in good_moments(copy) if m[1] != "discord"]
    items += [("call_join", "discord", ts(d, "11:00:00"), "calls", 900_000 + n, "screen", None)
              for n, d in enumerate(("2026-09-25", "2026-09-26"))]
    out = H.moments_bars(copy, runner=Rebuild(copy, items))
    assert out["bars"]["discord_evenings"]["evenings"] == 0


def test_youtube_pages_without_an_entity_fail(copy):
    items = [(k, a, at, s, i, e, None if k == "page" else x) for k, a, at, s, i, e, x in good_moments(copy)]
    out = H.moments_bars(copy, runner=Rebuild(copy, items))
    yt = out["bars"]["youtube_pages"]
    assert yt["youtube"] == 2 and yt["youtube_with_entity"] == 0 and not yt["pass"]


def test_a_slow_rebuild_fails_the_runtime_bar(copy):
    out = H.moments_bars(copy, runner=Rebuild(copy, seconds=6.2))
    assert not out["bars"]["runtime"]["pass"] and out["result"] == "fail"


class Timed(Rebuild):
    """Each call reports the next of ``walls`` as its wall time (and ``owns`` as the pipeline's own, if given)."""

    def __init__(self, path, walls, owns=None):
        super().__init__(path)
        self.walls, self.owns = list(walls), list(owns) if owns is not None else None

    def __call__(self, args):
        got = super().__call__(args)
        got["s"] = self.walls.pop(0)
        if self.owns is not None:
            got["own_s"] = self.owns.pop(0)
        return got


def test_the_runtime_bar_times_the_rebuild_not_opening_and_migrating_the_copy(copy):
    out = H.moments_bars(copy, runner=Timed(copy, [9.0, 0.5]))          # the first call also migrated the copy
    rt = out["bars"]["runtime"]
    assert rt["pass"] and rt["s"] == 0.5 and rt["first_call_s"] == 9.0 and "second rebuild" in rt["how"]


def test_the_pipelines_own_rebuild_time_is_used_when_it_prints_it(copy):
    out = H.moments_bars(copy, runner=Timed(copy, [9.0, 4.0], owns=[0.3, 0.2]))
    assert out["bars"]["runtime"]["s"] == 0.3 and out["bars"]["runtime"]["pass"]
    out = H.moments_bars(copy, runner=Timed(copy, [0.1, 0.1], owns=[5.5, 0.2]))
    assert out["bars"]["runtime"]["s"] == 5.5 and not out["bars"]["runtime"]["pass"]


def test_the_cli_runner_reads_the_pipelines_printed_time(copy, monkeypatch):
    class Mod:
        @staticmethod
        def main(argv):
            write_moments(copy, good_moments(copy))
            print(json.dumps({"cmd": "rebuild", "dry_run": False, "s": 7.25, "counts": {"call_join": 9}}, indent=1))
            return 0

    monkeypatch.setattr(H, "moments_module", lambda: Mod)
    out = H.moments_bars(copy)
    assert out["rebuild"][0]["own_s"] == 7.25 and out["bars"]["runtime"]["s"] == 7.25
    assert "own timing" in out["bars"]["runtime"]["how"] and not out["bars"]["runtime"]["pass"]


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_no_rebuild_starts_after_the_time_limit(copy):
    clock = Clock()
    caps = H.Caps(3.0, 1, clock=clock)
    clock.t = 61.0
    run = Rebuild(copy)
    out = H.moments_bars(copy, runner=run, caps=caps)
    assert run.calls == [] and out["result"] == "stopped" and "time limit" in out["why"]
    assert any(line.startswith("Stopped: the time limit") for line in H.moments_text(out))


def test_a_rebuild_that_runs_past_the_limit_stops_the_suite(copy):
    clock = Clock()
    caps = H.Caps(3.0, 1, clock=clock)

    class Slow(Rebuild):
        def __call__(self, args):
            clock.t += 70.0
            return super().__call__(args)

    run = Slow(copy)
    out = H.moments_bars(copy, runner=run, caps=caps)
    assert len(run.calls) == 1 and out["result"] == "stopped" and "bars" not in out


def test_a_child_process_rebuild_is_stopped_at_the_time_left(copy, monkeypatch):
    clock = Clock()
    caps = H.Caps(3.0, 1, clock=clock)
    clock.t = 45.0                                   # 15 s left
    seen = []

    class Mod:                                       # no main(argv): a child process
        main = None

    def fake_run(cmd, **kw):
        seen.append((cmd[-2:], kw["timeout"]))
        raise H.subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(H, "moments_module", lambda: Mod)
    monkeypatch.setattr(H.subprocess, "run", fake_run)
    out = H.moments_bars(copy, caps=caps)
    assert seen == [(["--db", str(copy)], 15.0)]
    assert out["result"] == "stopped" and out["rebuild"][0]["timed_out"]


def test_the_real_rebuild_runs_on_a_copy_once_fmoments_is_merged(copy):
    """At merge (fMoments): Yuki's own ``moments rebuild`` through the real CLI runner: it runs, twice, in time and
    idempotently. (The invented world is not shaped for the E1 bar: 29 Sep and 5 Oct need fMoments' calling sites and
    fLive's verdicts; the F moments bar is run on the real copy after both merge.)"""
    pytest.importorskip("yuki.memory.moments")
    out = H.moments_bars(copy, caps=H.Caps())
    assert out["result"] in ("pass", "fail"), out.get("why")
    assert [r["code"] for r in out["rebuild"]] == [0, 0]
    assert out["bars"]["idempotent"]["pass"] and out["bars"]["runtime"]["pass"]


def test_a_rebuild_that_adds_rows_again_is_not_idempotent(copy):
    out = H.moments_bars(copy, runner=Rebuild(copy, append=True))
    idem = out["bars"]["idempotent"]
    assert idem["rows"][1] > idem["rows"][0] and not idem["pass"]


def test_without_the_moments_pipeline_the_suite_says_so(copy, monkeypatch):
    monkeypatch.setattr(H, "moments_module", lambda: None)
    out = H.moments_bars(copy)
    assert out["result"] == "not available" and "fMoments" in out["why"]
    assert out["reference"]["e1_days_with_a_source"] == 7       # the raw-row reference still runs
    assert any("Not available" in line for line in H.moments_text(out))
    assert H.suite_status("moments")["available"] is False


def test_a_failing_rebuild_is_an_error_not_a_pass(copy):
    out = H.moments_bars(copy, runner=lambda args: {"code": 2, "s": 0.1, "how": "stub"})
    assert out["result"] == "error" and "code 2" in out["why"]


def test_the_bars_refuse_the_live_database(live):
    with pytest.raises(RuntimeError, match="copy"):
        H.moments_bars(live, runner=Rebuild(live))
    with pytest.raises(RuntimeError):
        H.cli_runner(live)


def test_the_cli_runner_passes_the_copy_and_runs_in_process(copy, monkeypatch):
    seen = []

    class Mod:
        @staticmethod
        def main(argv):
            seen.append(argv)
            print("rebuilt 12 moments")
            write_moments(copy, good_moments(copy))
            return 0

    monkeypatch.setattr(H, "moments_module", lambda: Mod)
    out = H.moments_bars(copy)
    assert seen == [["rebuild", "--db", str(copy)]] * 2
    assert out["rebuild"][0]["how"] == "in-process" and out["rebuild"][0]["lines"] == 1
    assert out["result"] == "pass"


def test_the_moments_report_is_content_free(copy):
    out = H.moments_bars(copy, runner=Rebuild(copy))
    text = json.dumps(out) + "\n".join(H.moments_text(out))
    for leak in ("meet.google.com", "www.youtube.com", "enc-video-id", "Google Meet", "FL Studio"):
        assert leak not in text
    path = H.write_report(out, "moments")
    assert path.parent == dbcopy.report_dir() and "enc-video-id" not in path.read_text(encoding="utf-8")


def test_the_hand_check_sample_is_stratified_and_names_independent_sources(copy, tmp_path):
    H.moments_bars(copy, runner=Rebuild(copy))
    logs = tmp_path / "logs"
    logs.mkdir()
    conn = sqlite3.connect(copy)
    cid, at = conn.execute("SELECT id, detected_at FROM calls WHERE process='zen' AND via='screen' LIMIT 1").fetchone()
    conn.close()
    (logs / "memory-20261007-080000.jsonl").write_text(
        json.dumps({"ts": at, "type": "call_detected", "call_id": cid, "process": "zen", "via": "screen",
                    "app": "Zen"}) + "\n" + json.dumps({"ts": at, "type": "other"}) + "\n", encoding="utf-8")
    items = H.sample_moments(copy, 10, memory_logs=logs)
    assert len(items) == 10
    assert {"call_join", "game_voice", "page"} <= {i["kind"] for i in items}
    every = H.sample_moments(copy, 1000, memory_logs=logs)
    assert len(every) == len(H.read_moments(copy))
    zen = [i for i in every if i["kind"] == "call_join" and i.get("calls_row") and i["calls_row"]["via"] == "screen"
           and i["app"] == "zen"]
    assert len(zen) == 2
    assert all(i["calls_row"]["delta_s"] == 0.0 for i in zen)
    assert sum("zen/screen" in i["log_call_detected"] for i in zen) >= 1
    site = [i for i in every if i["evidence"] == "site"]
    assert len(site) == 3 and all(any(t.startswith("zen/meet/meeting") for t in i["timeline_near"]) for i in site)
    holds = [i for i in every if i["kind"] == "call_join" and i["evidence"] == "hold"]
    assert holds and all(any("zen/mic" in c or "/meeting/mic" in t for c in i["calls_near"] + [""]
                             for t in i["timeline_near"] + [""]) for i in holds)
    text = "\n".join(H.sample_text(items))
    assert "agree? __" in text and "meet.google.com" not in text and "enc-video-id" not in text
    assert H.sample_moments(copy, 10, memory_logs=logs) == items          # the same draw for the same seed


def test_the_time_split_cuts_only_rows_written_since(copy):
    write_moments(copy, good_moments(copy))
    conn = sqlite3.connect(copy)
    conn.execute("CREATE TABLE skill_runs (id INTEGER PRIMARY KEY, at REAL NOT NULL)")
    conn.execute("CREATE TABLE routines (id INTEGER PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL)")
    cut = ts("2026-10-01", "00:00:00")
    conn.executemany("INSERT INTO skill_runs(at) VALUES (?)", [(cut - 10,), (cut,), (cut + 10,)])
    conn.executemany("INSERT INTO routines(created_at, updated_at) VALUES (?, ?)", [(cut - 10, cut + 99), (cut + 1, 0)])
    before = conn.execute("SELECT count(*) FROM moments WHERE seen_at >= ?", (cut,)).fetchone()[0]
    conn.commit()
    conn.close()
    got = H.time_split(copy, cut)
    assert got["skill_runs"] == 2 and got["routines"] == 1 and got["moments"] == before
    assert "tastes" not in got
    with pytest.raises(RuntimeError):
        H.time_split(dbcopy.real_db_path(), cut)


def test_only_the_moments_and_cost_suites_run_on_this_branch():
    assert H.suite_status("cost")["available"]
    for s in ("routines", "lane", "taste"):
        st = H.suite_status(s)
        assert not st["available"] and st["needs"]
