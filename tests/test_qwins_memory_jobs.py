"""Report 76 quick win 1: the memory jobs' own read timeout, and the nightly catch-up only when the user is idle.

Offline: the real model client is never built (the SDK constructor is replaced by a recorder), temp databases.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuki.memory.portrait import BUSY_CALL_WINDOW_S, PortraitScheduler, RunResult
from yuki.memory.store import PortraitRunStats, Store

T0 = datetime(2026, 9, 25, 4, 0).timestamp()


# -- the timeout ------------------------------------------------------------------------------------------------------


def _captured_read_timeout(monkeypatch, make) -> float:
    seen = {}

    def fake_sync_client(region, *, backend=None, **options):
        seen.update(options)
        return SimpleNamespace(messages=None)

    import yuki.core.models as models

    monkeypatch.setattr(models, "sync_client", fake_sync_client)
    client = make()
    client.client_for(client.region)
    return float(seen["timeout"].read)


def test_memory_jobs_get_their_own_longer_read_timeout(monkeypatch):
    from yuki.agent.failover import make_client
    from yuki.config import Settings

    settings = Settings()
    assert settings.memory_model_timeout_s > settings.model_timeout_s
    read = _captured_read_timeout(monkeypatch, lambda: make_client(settings))
    assert read == settings.memory_model_timeout_s
    assert make_client(settings).read_timeout_s == settings.memory_model_timeout_s   # routed clients take it too


def test_the_agent_keeps_the_short_read_timeout(monkeypatch):
    from yuki.agent.failover import FailoverClient, make_client
    from yuki.config import Settings

    settings = Settings()
    read = _captured_read_timeout(monkeypatch, lambda: FailoverClient.from_settings(settings))
    assert read == settings.model_timeout_s
    assert FailoverClient.from_settings(settings).read_timeout_s is None
    assert make_client(settings, role="thinker").read_timeout_s is None


def test_a_memory_call_the_user_waits_on_keeps_the_short_read_timeout(monkeypatch):
    # review (item 1): the window's to-do due words are a memory call the user waits on, not a background pass
    from pathlib import Path

    from yuki.agent.failover import make_client
    from yuki.config import Settings

    settings = Settings()
    read = _captured_read_timeout(monkeypatch, lambda: make_client(settings, waited_on=True))
    assert read == settings.model_timeout_s and make_client(settings, waited_on=True).read_timeout_s is None
    src = (Path(__file__).resolve().parents[1] / "yuki" / "ui" / "web_window.py").read_text(encoding="utf-8")
    assert "make_client(waited_on=True)" in src


def test_routed_memory_client_is_built_with_the_memory_timeout(monkeypatch):
    from yuki.agent import failover
    from yuki.agent.providers import client as provider_client
    from yuki.config import Settings

    built = []
    monkeypatch.setattr(provider_client, "sync_provider_client",
                        lambda pid, settings=None, *, transport=None, timeout=None: built.append((pid, timeout)) or
                        SimpleNamespace())
    owner = failover.make_client(Settings())
    chosen = SimpleNamespace(primary=SimpleNamespace(provider="openai", model="m"), backup=None,
                             signature=lambda: ("openai", "m"))
    routed = owner._routed_client(chosen, "claude-x")
    routed.client_for(routed.region)
    assert built == [("openai", Settings().memory_model_timeout_s)]


# -- the catch-up gate ------------------------------------------------------------------------------------------------


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            s.save_portrait("Work\n- x")
            yield s
        finally:
            s.close()


class FakeWorker:
    def __init__(self):
        self.runs = []

    def run(self, kind):
        self.runs.append(kind)
        return RunResult(1, kind, True, stats=PortraitRunStats(kind=kind, model="stub"))

    def rerender(self, reason=""):
        return RunResult(1, "render", True, stats=PortraitRunStats(kind="render", model="stub"))

    def audit(self, *, full=False, reason=""):
        return None

    def loop_check_pending(self):
        return False


def _sched(store, *, idle, live=None, now=T0):
    return PortraitScheduler(store, FakeWorker(), db_path=store.path, idle_fn=lambda: idle, live_fn=live,
                             clock=lambda: now)


def test_a_missed_slot_no_longer_runs_at_start_while_the_user_is_active(store):
    sched = _sched(store, idle=5.0)
    kind, reason = sched.due(T0)
    assert kind is None and "waiting for idle" in reason
    assert sched.tick() is None and sched.worker.runs == []


def test_a_missed_slot_runs_once_the_user_is_idle(store):
    sched = _sched(store, idle=9999.0)
    kind, reason = sched.due(T0)
    assert kind == "weekly" and reason.startswith("missed slot")
    assert sched.tick() == "weekly"


def test_an_open_call_holds_the_night_back_even_when_idle(store):
    store.start_call(app="Discord", process="discord.exe", via="test", at=T0 - 600, status="recording")
    kind, reason = _sched(store, idle=9999.0).due(T0)
    assert kind is None and "call" in reason


def test_an_old_stuck_call_row_does_not_hold_the_night_back(store):
    store.start_call(app="Discord", process="discord.exe", via="test", at=T0 - BUSY_CALL_WINDOW_S - 60,
                     status="recording")
    assert _sched(store, idle=9999.0).due(T0)[0] == "weekly"


@pytest.mark.parametrize("live, word", [({"fullscreen": True}, "full-screen"), ({"meeting": True}, "meeting")])
def test_a_game_or_meeting_in_front_holds_the_night_back(store, live, word):
    kind, reason = _sched(store, idle=9999.0, live=lambda: live).due(T0)
    assert kind is None and word in reason


def test_a_broken_live_reading_does_not_stop_the_night(store):
    def broken():
        raise RuntimeError("no recorder")

    assert _sched(store, idle=9999.0, live=broken).due(T0)[0] == "weekly"


# -- the weekly review's missed slot waits the same way (review: item 1, other catch-ups) ----------------------------

RNOW = datetime(2026, 9, 28, 9, 0).timestamp()   # a Monday morning: Sunday 20:00's review was missed


def _review(store, *, idle, live=None):
    from yuki.memory.nudges import NudgeConfig
    from yuki.memory.review import ReviewConfig, ReviewScheduler

    return ReviewScheduler(store, SimpleNamespace(), db_path=store.path, config=ReviewConfig(),
                           nudge_config=NudgeConfig(), idle_fn=lambda: idle, live_fn=live, clock=lambda: RNOW,
                           signal=lambda: None)


def test_a_missed_weekly_review_no_longer_runs_at_start_while_the_user_is_active(store):
    ok, reason = _review(store, idle=5.0).due(RNOW)
    assert ok is False and "waiting for idle" in reason


def test_a_missed_weekly_review_runs_once_the_user_is_idle(store):
    ok, reason = _review(store, idle=9999.0).due(RNOW)
    assert ok is True and reason.startswith("missed slot")


@pytest.mark.parametrize("live, word", [({"fullscreen": True}, "full-screen"), ({"meeting": True}, "meeting")])
def test_a_game_or_meeting_holds_the_weekly_review_back(store, live, word):
    ok, reason = _review(store, idle=9999.0, live=lambda: live).due(RNOW)
    assert ok is False and word in reason


def test_a_call_holds_the_weekly_review_back(store):
    store.start_call(app="Discord", process="discord.exe", via="test", at=RNOW - 600, status="recording")
    ok, reason = _review(store, idle=9999.0).due(RNOW)
    assert ok is False and "call" in reason
