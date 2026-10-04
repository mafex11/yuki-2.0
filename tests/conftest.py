"""Test-run guard: nothing a test does can write into the user's real memory.

Every default memory location (the database, its key, the "Yuki is acting" marker,
the privacy and extract-profile files) follows ``YUKI_MEMORY_DIR``
(:data:`yuki.memory.store.MEMORY_DIR_ENV`) when it is set. Here it points at a temp
folder for the whole session, so a harness that forgets to pass a path (an ``Agent``
built without ``memory=``, ``MemoryClient.open()``, ``acting.begin(None, ...)``) writes
there instead of ``%LOCALAPPDATA%\\Yuki\\memory``. On 2026-09-25 19:40 a harness
without a path (``scripts/voice_scenarios.py hands3`` with no ``--db``) wrote three
exchanges and the acting marker into the real memory; they later showed up in a real
request as "Last 3 exchanges" (docs/research/coach-and-answers-review.md 2.1, 3.2 H).

``LOCALAPPDATA`` itself is left alone: other files live there (the embedding model
cache, speech models, the voice settings), and tests only read those.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _no_side_calls():
    """Revamp D: the authorization judge and the second look are extra model calls; a test's scripted fake client
    never answers them, so they are off unless a test turns them on (``Settings(judge=True, consult=True)``)."""
    old = {k: os.environ.get(k) for k in ("YUKI_JUDGE", "YUKI_CONSULT")}
    os.environ["YUKI_JUDGE"] = "0"
    os.environ["YUKI_CONSULT"] = "0"
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture(scope="session", autouse=True)
def _temp_ai_dir(tmp_path_factory: pytest.TempPathFactory):
    """Settings → AI (``ai.json`` and the DPAPI key file) in a temp folder: no test ever reads the user's own choice
    of AI or keys, so every test runs on the default path unless it writes its own ``ai.json`` there."""
    folder = tmp_path_factory.mktemp("yuki-ai")
    old = os.environ.get("YUKI_AI_DIR")
    os.environ["YUKI_AI_DIR"] = str(folder)
    try:
        yield folder
    finally:
        if old is None:
            os.environ.pop("YUKI_AI_DIR", None)
        else:
            os.environ["YUKI_AI_DIR"] = old


@pytest.fixture(scope="session", autouse=True)
def _temp_memory_dir(tmp_path_factory: pytest.TempPathFactory):
    """Point memory's default folder at a temp dir for the whole run (restored after)."""
    folder = tmp_path_factory.mktemp("yuki-memory")
    old = os.environ.get("YUKI_MEMORY_DIR")
    os.environ["YUKI_MEMORY_DIR"] = str(folder)
    try:
        yield folder
    finally:
        if old is None:
            os.environ.pop("YUKI_MEMORY_DIR", None)
        else:
            os.environ["YUKI_MEMORY_DIR"] = old


# -- a pinned clock (report 76: tests that format "today"/"yesterday" failed after midnight) --------------------------
#
# ``pinned_clock`` (opt-in) moves the wall clock a test sees to a fixed local time of a fixed day, ticking from there:
# ``time.time``, ``time.localtime``/``gmtime``/``strftime`` without a time, and ``datetime.now``/``today`` and
# ``date.today`` in every loaded ``yuki`` and ``test_*`` module (their ``datetime``/``date`` names are swapped for
# subclasses that read the pinned clock). ``time.monotonic`` and ``perf_counter`` are left alone, so timeouts and waits
# behave. Pick the time with ``@pytest.mark.clock("00:30")`` (default noon). ``YUKI_TEST_CLOCK=HH:MM`` pins every test
# of the run (``scripts/sweep.py --clock 00:30``): how the after-midnight failures are found.

import datetime as _dt
import sys as _sys
import time as _time
from dataclasses import dataclass as _dataclass

PINNED_DAY = _dt.date(2026, 9, 25)   # a Friday; fixed so day names in assertions never move


@_dataclass
class PinnedClock:
    """The pinned wall clock: ``now()`` (epoch seconds, ticking) and ``start`` (where it was pinned)."""

    start: float
    _mono0: float

    def now(self) -> float:
        return self.start + (_REAL_MONOTONIC() - self._mono0)


_REAL_TIME = _time.time
_REAL_MONOTONIC = _time.monotonic
_REAL_LOCALTIME = _time.localtime
_REAL_GMTIME = _time.gmtime
_REAL_STRFTIME = _time.strftime
_REAL_DATETIME = _dt.datetime
_REAL_DATE = _dt.date


def _pin(monkeypatch: pytest.MonkeyPatch, hhmm: str) -> PinnedClock:
    hour, minute = (int(x) for x in hhmm.split(":"))
    start = _REAL_DATETIME.combine(PINNED_DAY, _dt.time(hour, minute)).timestamp()
    clock = PinnedClock(start, _REAL_MONOTONIC())

    class PinnedDatetime(_REAL_DATETIME):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(clock.now(), tz)

        @classmethod
        def today(cls):
            return cls.fromtimestamp(clock.now())

        @classmethod
        def utcnow(cls):
            return cls.fromtimestamp(clock.now(), _dt.timezone.utc).replace(tzinfo=None)

    class PinnedDate(_REAL_DATE):
        @classmethod
        def today(cls):
            return cls.fromtimestamp(clock.now())

    monkeypatch.setattr(_time, "time", clock.now)
    monkeypatch.setattr(_time, "localtime", lambda secs=None: _REAL_LOCALTIME(clock.now() if secs is None else secs))
    monkeypatch.setattr(_time, "gmtime", lambda secs=None: _REAL_GMTIME(clock.now() if secs is None else secs))
    monkeypatch.setattr(_time, "strftime", lambda fmt, t=None: _REAL_STRFTIME(fmt, _REAL_LOCALTIME(clock.now())
                                                                              if t is None else t))
    monkeypatch.setattr(_dt, "datetime", PinnedDatetime)
    monkeypatch.setattr(_dt, "date", PinnedDate)
    for name, module in list(_sys.modules.items()):
        if module is None or not (name == "yuki" or name.startswith("yuki.") or name.split(".")[-1].startswith("test_")):
            continue
        attrs = getattr(module, "__dict__", {})
        if attrs.get("datetime") is _REAL_DATETIME:
            monkeypatch.setattr(module, "datetime", PinnedDatetime)
        if attrs.get("date") is _REAL_DATE:
            monkeypatch.setattr(module, "date", PinnedDate)
    return clock


def _clock_mark(request: pytest.FixtureRequest) -> str | None:
    mark = request.node.get_closest_marker("clock")
    return str(mark.args[0]) if mark is not None and mark.args else None


@pytest.fixture()
def pinned_clock(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> PinnedClock:
    """The wall clock pinned to ``@pytest.mark.clock("HH:MM")`` (default noon) of :data:`PINNED_DAY`, ticking."""
    return _pin(monkeypatch, _clock_mark(request) or "12:00")


@pytest.fixture(autouse=True)
def _clock_for_the_run(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """``YUKI_TEST_CLOCK=HH:MM`` (the sweep's ``--clock``) pins every test that does not pin its own."""
    pinned = os.environ.get("YUKI_TEST_CLOCK", "").strip()
    if pinned and "pinned_clock" not in request.fixturenames:
        _pin(monkeypatch, pinned)
    yield


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "clock(hhmm): the local time pinned_clock starts at (default 12:00)")
