"""capQ2a (report 103): input while Deskflow has the cursor on the other computer never becomes a row.

The 8 Oct audit: this PC is the Deskflow server, and clicks the user made on the OTHER computer were recorded as
anonymous clicks in Deskflow's own window (24 at 18:07). Deskflow's own log says when the cursor leaves and enters
this screen (``leaving screen`` / ``entering screen``, report 78 §4.2); the trace drops input in between, and any row
of Deskflow's own processes. The log here is a temp file in Deskflow's format with invented screen names.
"""

from __future__ import annotations

import time

import pytest
from test_trace_worker import APP, HWND, PROCS, Rig, button

from yuki.memory.trace_deskflow import DeskflowScreens, log_candidates, parse_line

DESK = 401
PROCS[DESK] = ("deskflow-core", "deskflow-core.exe", "Deskflow", False, 1.0)
DESK_HWND = 81


def _stamp(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)) + f".{int(round((at % 1) * 1000)) % 1000:03d}"


def _lines(*spans: tuple[float, float | None]) -> str:
    out = []
    for start, end in spans:
        out.append(f'[{_stamp(start)}] INFO: switch from "desk-a" to "laptop-b" at 400,1168')
        out.append(f"[{_stamp(start)}] INFO: leaving screen")
        if end is not None:
            out.append(f'[{_stamp(end)}] INFO: switch from "laptop-b" to "desk-a" at 1000,5')
            out.append(f"[{_stamp(end)}] INFO: entering screen")
    return "\n".join(out) + "\n"


def test_its_fixed_lines_are_read_and_others_passed_over():
    at, away = parse_line("[2026-10-08T18:07:02.451] INFO: leaving screen")
    assert away is True and at == pytest.approx(time.mktime((2026, 10, 8, 18, 7, 2, 0, 0, -1)) + 0.451)
    assert parse_line("[2026-10-08T18:07:02.467] INFO: entering screen")[1] is False
    assert parse_line('[2026-10-08T18:07:02.467] INFO: switch from "a" to "b" at 1,2') is None
    assert parse_line("[not a time] INFO: leaving screen") is None and parse_line("") is None


def test_spans_from_the_log_and_new_lines_read_incrementally(tmp_path):
    log = tmp_path / "deskflow-daemon.log"
    now = time.time()
    log.write_text("[2020-01-01T00:00:00.000] NOTE: starting daemon\n" + _lines((now - 100, now - 90)),
                   encoding="utf-8")
    d = DeskflowScreens(paths=lambda: [log])
    assert d.away(now - 95) and not d.away(now - 85) and not d.away(now - 101)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(_lines((now - 50, None)))
    assert d.away(now - 10) and d.counts["deskflow_reads"] == 2
    assert d.away(now - 10) and d.counts["deskflow_reads"] == 2            # unchanged: one stat, no read


def test_a_rotated_log_is_read_again_from_its_end(tmp_path):
    log = tmp_path / "deskflow-daemon.log"
    now = time.time()
    log.write_text(_lines((now - 100, now - 90)) * 50, encoding="utf-8")
    d = DeskflowScreens(paths=lambda: [log])
    d.refresh()
    log.write_text(_lines((now - 20, None)), encoding="utf-8")          # shorter: rotated
    assert d.away(now - 5)


def test_no_deskflow_log_drops_nothing(tmp_path):
    d = DeskflowScreens(paths=lambda: [tmp_path / "missing.log"])
    assert d.away(time.time()) is False


def test_a_restart_with_the_cursor_away_ends_the_span(tmp_path):
    """Review F1: the PC was shut down with the cursor on the other computer. Deskflow's log has ``leaving screen``,
    then its daemon starting again after the reboot, and never ``entering screen``: the span ends at the restart, and
    the next ``leaving screen`` starts a new one."""
    log = tmp_path / "deskflow-daemon.log"
    now = time.time()
    log.write_text(f"[{_stamp(now - 7200)}] INFO: leaving screen\n"
                   "Deskflow Daemon v1.26.0\n"
                   f"[{_stamp(now - 5400)}] NOTE: starting daemon\n"
                   f"[{_stamp(now - 5400)}] INFO: daemon is running\n", encoding="utf-8")
    d = DeskflowScreens(paths=lambda: [log], boot=now - 5500)
    assert d.away(now - 6000) and not d.away(now - 5400) and not d.away(now)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(_lines((now - 60, now - 30)))
    assert d.away(now - 45) and not d.away(now - 10)


def test_a_restart_ends_the_span_even_when_the_boot_is_unknown(tmp_path):
    log = tmp_path / "deskflow-daemon.log"
    now = time.time()
    log.write_text(f"[{_stamp(now - 7200)}] INFO: leaving screen\n"
                   f"[{_stamp(now - 5400)}] NOTE: starting daemon\n", encoding="utf-8")
    d = DeskflowScreens(paths=lambda: [log], boot=0.0)
    assert d.away(now - 6000) and not d.away(now)


@pytest.mark.parametrize("line", ["NOTE: stopped server", "INFO: daemon is stopping",
                                  "IPC: started server, waiting for clients"])
def test_its_own_stop_and_start_lines_end_a_span(tmp_path, line):
    log = tmp_path / "deskflow-daemon.log"
    now = time.time()
    log.write_text(f"[{_stamp(now - 100)}] INFO: leaving screen\n[{_stamp(now - 50)}] {line}\n", encoding="utf-8")
    d = DeskflowScreens(paths=lambda: [log], boot=now - 1000)
    assert d.away(now - 70) and not d.away(now - 20)


def test_a_span_from_before_this_pcs_boot_ended_at_the_boot(tmp_path):
    """Deskflow wrote nothing at all after the cursor left (the PC lost power): this PC's boot ends the span."""
    log = tmp_path / "deskflow-daemon.log"
    now = time.time()
    log.write_text(f"[{_stamp(now - 7200)}] INFO: leaving screen\n", encoding="utf-8")
    d = DeskflowScreens(paths=lambda: [log], boot=now - 3600)
    assert d.away(now - 5000) and not d.away(now - 3000) and not d.away(now)
    with log.open("a", encoding="utf-8") as fh:            # after the boot it leaves again: a new span
        fh.write(f"[{_stamp(now - 20)}] INFO: leaving screen\n")
    assert d.away(now - 5) and not d.away(now - 3000)


def test_the_restart_lines_parse_as_back_on_this_screen():
    assert parse_line("[2026-10-08T18:07:02.451] NOTE: starting daemon")[1] is False
    assert parse_line("Deskflow Daemon v1.26.0") is None


def test_the_boot_time_is_windows_own():
    from yuki.memory.trace_deskflow import boot_time

    assert 0 < boot_time() <= time.time()


def test_its_settings_file_can_name_the_log(tmp_path):
    conf = tmp_path / "Deskflow"
    conf.mkdir()
    (conf / "Deskflow.conf").write_text("[log]\ntoFile=true\nfile=" + str(tmp_path / "chosen.log") + "\n",
                                        encoding="utf-8")
    got = log_candidates({"APPDATA": str(tmp_path), "ProgramData": str(tmp_path / "pd")})
    assert got[0] == tmp_path / "chosen.log" and got[1] == tmp_path / "pd" / "Deskflow" / "deskflow-daemon.log"


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    base = time.time() - r.clock.t
    r.w._wall = lambda: base + r.clock.t
    r.log_path = tmp_path / "deskflow-daemon.log"
    r.log_path.write_text("", encoding="utf-8")
    r.w._other_screen = DeskflowScreens(paths=lambda: [r.log_path]).away
    r.uia.points[(150, 120)] = button()
    yield r
    r.store.close()


def test_input_while_the_cursor_is_on_the_other_screen_is_dropped(rig):
    now = time.time()
    rig.log_path.write_text(_lines((now - 1, now + 3)), encoding="utf-8")
    rig.click()                                                   # on the other computer
    rig.feed("key", rig.clock.t, "chord", "Ctrl+C", "user", 0, dt=0.2)
    rig.clock.t += 4                                              # the cursor is back
    rig.click()
    rig.settle()
    assert [r["kind"] for r in rig.rows()] == ["click"]
    assert rig.w.counters["skip_other_screen"] == 2
    assert rig.uia.calls.count(("point", 150, 120)) == 1          # no lookup for the other computer's click


def test_a_line_written_after_the_action_began_still_drops_it(rig):
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False)
    rig.feed("up", t + 0.05, "left", 150, 120, "user", False, dt=0.05)
    rig.log_path.write_text(_lines((time.time() - 0.5, None)), encoding="utf-8")  # Deskflow wrote it a moment late
    rig.settle()
    assert rig.rows() == [] and rig.w.counters["skip_other_screen"] == 1


def test_deskflows_own_window_gives_no_row_and_names_no_outcome(rig):
    rig.under = rig.fg = (DESK_HWND, DESK)
    rig.click()
    rig.under = rig.fg = (HWND[APP], APP)
    rig.click()
    rig.feed("fg", rig.clock.t + 0.1, DESK_HWND, DESK, dt=0.1)  # Deskflow takes the front (cursor leaving)
    rig.settle()
    rows = rig.rows()
    assert [r["kind"] for r in rows] == ["click"] and rows[0]["process"] == "fieldnotes"
    assert "window_verified" not in (rows[0]["facts"] or "")
    assert rig.w.counters["skip_deskflow"] >= 2                  # its click and its switch
