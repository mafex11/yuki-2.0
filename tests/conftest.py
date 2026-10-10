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


@pytest.fixture(scope="session", autouse=True)
def _temp_seen_ledger(tmp_path_factory: pytest.TempPathFactory):
    """appOpen final check: the seen-windows ledger (``yuki.perception.windows``, written by the timeline) in a temp
    folder, so a test that drives the timeline with the real windows module never writes the user's own file."""
    folder = tmp_path_factory.mktemp("yuki-seen")
    old = os.environ.get("YUKI_SEEN_FILE")
    os.environ["YUKI_SEEN_FILE"] = str(folder / "seen_windows.json")
    try:
        yield folder
    finally:
        if old is None:
            os.environ.pop("YUKI_SEEN_FILE", None)
        else:
            os.environ["YUKI_SEEN_FILE"] = old


# -- a pinned clock (report 76: tests that format "today"/"yesterday" failed after midnight) --------------------------
#
# ``pinned_clock`` (opt-in) moves the wall clock a test sees to a fixed local time of a fixed day, ticking from there:
# ``time.time``, ``time.localtime``/``gmtime``/``strftime`` without a time, and ``datetime.now``/``today`` and
# ``date.today`` in every loaded ``yuki`` and ``test_*`` module (their ``datetime``/``date`` names, as ``from datetime
# import datetime`` binds them, are swapped for subclasses that read the pinned clock; the datetime module itself stays
# real, so pickling works). ``time.monotonic`` and ``perf_counter`` are left alone, so timeouts and waits
# behave. Pick the time with ``@pytest.mark.clock("00:30")`` (default noon, on :data:`PINNED_DAY`). ``YUKI_TEST_CLOCK=HH:MM``
# pins every test of the run to the latest real moment at that time (today, or yesterday when it is still ahead;
# ``scripts/sweep.py --clock 00:30``): how the after-midnight failures are found. Never a fixed day and never the future:
# file times and other clocks a test can't pin keep their real order. Modules first imported inside a test body keep
# the real ``datetime`` class.

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
    classes: tuple = ()

    def now(self) -> float:
        return self.start + (_REAL_MONOTONIC() - self._mono0)


_REAL_TIME = _time.time
_REAL_MONOTONIC = _time.monotonic
_REAL_LOCALTIME = _time.localtime
_REAL_GMTIME = _time.gmtime
_REAL_STRFTIME = _time.strftime
_REAL_DATETIME = _dt.datetime
_REAL_DATE = _dt.date


def _pin(monkeypatch: pytest.MonkeyPatch, hhmm: str, day: _dt.date | None = None) -> PinnedClock:
    hour, minute = (int(x) for x in hhmm.split(":"))
    start = _REAL_DATETIME.combine(day or PINNED_DAY, _dt.time(hour, minute)).timestamp()
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

        def __reduce_ex__(self, protocol):   # pickled as the real class (a local class can't be pickled)
            return (_REAL_DATETIME, super().__reduce_ex__(protocol)[1])

    class PinnedDate(_REAL_DATE):
        @classmethod
        def today(cls):
            return cls.fromtimestamp(clock.now())

        def __reduce_ex__(self, protocol):
            return (_REAL_DATE, super().__reduce_ex__(protocol)[1])

    monkeypatch.setattr(_time, "time", clock.now)
    monkeypatch.setattr(_time, "localtime", lambda secs=None: _REAL_LOCALTIME(clock.now() if secs is None else secs))
    monkeypatch.setattr(_time, "gmtime", lambda secs=None: _REAL_GMTIME(clock.now() if secs is None else secs))
    monkeypatch.setattr(_time, "strftime", lambda fmt, t=None: _REAL_STRFTIME(fmt, _REAL_LOCALTIME(clock.now())
                                                                              if t is None else t))
    # (the datetime module's own names stay real: pickle looks classes up there by name)
    clock.classes = (PinnedDatetime, PinnedDate)
    _swap_modules(monkeypatch, clock)
    return clock


def _swap_modules(monkeypatch: pytest.MonkeyPatch, clock: PinnedClock) -> None:
    """Every loaded yuki and test module's ``datetime``/``date`` name (``from datetime import ...``) -> the pinned
    classes of ``clock``."""
    PinnedDatetime, PinnedDate = clock.classes
    for name, module in list(_sys.modules.items()):
        if module is None or not (name == "yuki" or name.startswith("yuki.") or name.split(".")[-1].startswith("test_")):
            continue
        attrs = getattr(module, "__dict__", {})
        held = attrs.get("datetime")
        if isinstance(held, type) and issubclass(held, _REAL_DATETIME):   # the real class, or a run's pinned one
            monkeypatch.setattr(module, "datetime", PinnedDatetime)
        held = attrs.get("date")
        if isinstance(held, type) and issubclass(held, _REAL_DATE) and not issubclass(held, _REAL_DATETIME):
            monkeypatch.setattr(module, "date", PinnedDate)


def _clock_mark(request: pytest.FixtureRequest) -> str | None:
    mark = request.node.get_closest_marker("clock")
    return str(mark.args[0]) if mark is not None and mark.args else None


@pytest.fixture()
def pinned_clock(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> PinnedClock:
    """The wall clock pinned to ``@pytest.mark.clock("HH:MM")`` (default noon) of :data:`PINNED_DAY`, ticking."""
    return _pin(monkeypatch, _clock_mark(request) or "12:00")


_RUN_CLOCK: list[tuple[pytest.MonkeyPatch, PinnedClock]] = []


@pytest.fixture(autouse=True)
def _run_clock_modules(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """A pinned run (``YUKI_TEST_CLOCK``): the modules imported since it began get the pinned classes too."""
    if _RUN_CLOCK and "pinned_clock" not in request.fixturenames:
        _swap_modules(monkeypatch, _RUN_CLOCK[-1][1])
    yield


def _run_day(hhmm: str) -> _dt.date:
    """The most recent day on which ``hhmm`` has already passed: today, or yesterday when it is still ahead. A pinned
    run is never in the future, so file times and other clocks a test can't pin stay behind it, as they really are."""
    hour, minute = (int(x) for x in hhmm.split(":"))
    now = _REAL_DATETIME.fromtimestamp(_REAL_TIME())
    today = now.date()
    return today if (hour, minute) <= (now.hour, now.minute) else today - _dt.timedelta(days=1)


def pytest_configure(config: pytest.Config) -> None:
    """``YUKI_TEST_CLOCK=HH:MM`` (the sweep's ``--clock``) pins the whole run, before any test module is imported, so a
    module's own ``NOW = time.time()`` reads the pinned clock too, as it would really run at that hour."""
    config.addinivalue_line("markers", "clock(hhmm): the local time pinned_clock starts at (default 12:00)")
    pinned = os.environ.get("YUKI_TEST_CLOCK", "").strip()
    if pinned and not _RUN_CLOCK:
        patch = pytest.MonkeyPatch()
        _RUN_CLOCK.append((patch, _pin(patch, pinned, _run_day(pinned))))


def pytest_unconfigure(config: pytest.Config) -> None:
    while _RUN_CLOCK:
        _RUN_CLOCK.pop()[0].undo()


@pytest.fixture(autouse=True)
def _no_live_game_check(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """fGate (D17, review r2 L8): the input backstop reads the live foreground window's program and the window under
    the cursor; a game in front during a test run would refuse every stubbed action. Tests see no game, unless their
    module sets ``LIVE_GAMEGATE = True`` (the fGate tests stub the facts themselves). A front source or refusal
    listener a test registers (the tray's ``_wire_game_backstop``) never outlives it."""
    from yuki.memory import gamegate

    monkeypatch.setattr(gamegate, "_front_source", None)
    monkeypatch.setattr(gamegate, "_refusal_listeners", [])
    if getattr(request.module, "LIVE_GAMEGATE", False):
        yield
        return
    monkeypatch.setattr(gamegate, "game_in_front", lambda: "")
    monkeypatch.setattr(gamegate, "_point_root", lambda x, y: 0)
    monkeypatch.setattr(gamegate, "pid_game", lambda pid, front=False, games=None: "")
    yield
