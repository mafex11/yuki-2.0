"""Report 76 quick win 7: scripts/sweep.py (one pytest process per file, failures again alone, flaky flagged) and the
pinned-clock fixture (tests/conftest.py). Temp test files only."""

from __future__ import annotations

import importlib.util
import time
from datetime import date, datetime
from pathlib import Path

import pytest

from yuki.core import doctor   # loaded before the fixture runs: its own "datetime" name is swapped

ROOT = Path(__file__).resolve().parents[1]


def _sweep():
    import sys

    if "yuki_sweep_script" not in sys.modules:
        spec = importlib.util.spec_from_file_location("yuki_sweep_script", ROOT / "scripts" / "sweep.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["yuki_sweep_script"] = module   # dataclasses look their module up while the class is made
        spec.loader.exec_module(module)
    return sys.modules["yuki_sweep_script"]


# -- the pinned clock -------------------------------------------------------------------------------------------------


def test_the_pinned_clock_is_noon_of_the_pinned_day_and_ticks(pinned_clock):
    import conftest

    now = datetime.now()
    assert now.date() == conftest.PINNED_DAY == date(2026, 9, 25) and now.hour == 12 and now.minute == 0
    assert date.today() == date(2026, 9, 25)
    first = time.time()
    time.sleep(0.02)
    assert time.time() > first and abs(first - pinned_clock.start) < 5
    assert time.strftime("%H") == "12"


@pytest.mark.clock("00:30")
def test_the_clock_mark_moves_it_after_midnight(pinned_clock):
    assert doctor.datetime.now().strftime("%H:%M") == "00:30"
    assert datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d") == "2026-09-25"


def test_without_the_fixture_the_clock_is_real():
    assert abs(datetime.now().timestamp() - time.time()) < 5 and date.today().year >= 2026


# -- the sweep --------------------------------------------------------------------------------------------------------


@pytest.fixture()
def tmp_tests(tmp_path):
    # the temp files' own pytest.ini makes tmp_path the inner run's rootdir: without one, pytest took the common
    # ancestor of the file and the worktree (the home folder) and walked it while collecting, and a file vanishing
    # there meanwhile (other processes' temp and lock files: a busy PC) failed the run with FileNotFoundError
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

    def write(name: str, body: str) -> str:
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        return str(path)

    return write


def test_a_passing_file_passes(tmp_tests):
    sweep = _sweep()
    run = sweep.sweep_file(tmp_tests("test_ok.py", "def test_a():\n    assert 1\n"), timeout_s=300,
                           python=__import__("sys").executable, env=dict(__import__("os").environ))
    assert run.result == "passed" and run.passed == 1, run


def test_a_failure_twice_is_failed_and_named(tmp_tests):
    sweep = _sweep()
    run = sweep.sweep_file(tmp_tests("test_bad.py", "def test_ok():\n    pass\n\ndef test_bad():\n    assert 0\n"),
                           timeout_s=300, python=__import__("sys").executable, env=dict(__import__("os").environ))
    assert run.result == "failed" and run.failed == 1 and len(run.failed_ids) == 1, run
    assert run.failed_ids[0].endswith("::test_bad")


def test_a_failure_that_passes_alone_is_flaky(tmp_tests, tmp_path):
    sweep = _sweep()
    marker = (tmp_path / "ran").as_posix()
    body = ("from pathlib import Path\n\n"
            f"def test_once():\n    p = Path({marker!r})\n    first = not p.exists()\n    p.write_text('x')\n"
            "    assert not first\n")
    run = sweep.sweep_file(tmp_tests("test_flaky.py", body), timeout_s=300, python=__import__("sys").executable,
                           env=dict(__import__("os").environ))
    assert run.result == "flaky" and run.failed_ids == [] and len(run.flaky_ids) == 1, run


def test_a_file_without_tests_is_empty(tmp_tests):
    sweep = _sweep()
    run = sweep.sweep_file(tmp_tests("test_none.py", "X = 1\n"), timeout_s=300, python=__import__("sys").executable,
                           env=dict(__import__("os").environ))
    assert run.result == "empty", run


def test_node_ids_from_junit_names():
    sweep = _sweep()
    assert sweep._node_id("tests.test_qwins_sweep", "test_a[x-1]") == "tests/test_qwins_sweep.py::test_a[x-1]"
    assert sweep._node_id("tests.test_qwins_sweep.TestCase", "test_b") == "tests/test_qwins_sweep.py::TestCase::test_b"
