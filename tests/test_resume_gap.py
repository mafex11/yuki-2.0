"""Offline checks of the coach's "welcome back" gap (yuki.memory.nudges.resume_gap).

Synthetic timelines on temp databases and a stub model; nothing touches the desktop.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuki.memory.nudges import NudgeConfig, NudgeWorker, RESUME_STATE, resume_gap
from yuki.memory.store import Store, TimelineRow

DAY = datetime(2026, 9, 25)


def at(hhmm: str, day: int = 0) -> float:
    h, m = (int(x) for x in hhmm.split(":"))
    return (DAY + timedelta(days=day, hours=h, minutes=m)).timestamp()


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def present(store: Store, start: float, end: float, app: str = "Warp", *, step: float = 300.0) -> None:
    """Present rows (as the recorder writes them: a row per few minutes of one app)."""
    t = start
    while t < end:
        e = min(end, t + step)
        store.save_timeline(TimelineRow(None, t, e, "present", app.lower(), app, active_s=e - t))
        t = e


def away(store: Store, start: float, end: float, app: str = "Warp", *, blips_every: float | None = None) -> None:
    """Away rows (PC on, no input); ``blips_every``: a 60 s present row of mouse drift that often."""
    t = start
    while t < end:
        e = min(end, t + (blips_every or 600.0))
        store.save_timeline(TimelineRow(None, t, e, "away", app.lower(), app, away_s=e - t))
        if blips_every and e + 60 < end:
            store.save_timeline(TimelineRow(None, e, e + 60, "present", app.lower(), app, active_s=60))
            e += 60
        t = e


CFG = NudgeConfig()


def test_pc_on_all_night_away_4h_back_at_9(store):
    present(store, at("20:00", -1), at("05:00"))
    away(store, at("05:00"), at("09:00"))
    present(store, at("09:00"), at("09:06"))
    early = resume_gap(store, at("09:03"), CFG)                       # back 3 min: not settled yet, not this gap
    assert early is None or (early.back_at == at("20:00", -1) and early.expired)   # (the evening's own start)
    gap = resume_gap(store, at("09:05") + 5, CFG)                     # settled: 5 min of presence
    assert gap is not None and gap.qualifies and not gap.expired
    assert gap.back_at == at("09:00") and gap.last_present == at("05:00")
    assert round(gap.seconds / 3600, 2) == 4.0 and gap.off_s == 0 and round(gap.away_s) == 4 * 3600
    assert not gap.new_day                                             # 05:00 and 09:00 are the same day
    assert resume_gap(store, at("09:06"), CFG, done_key=gap.key) is None


def test_mouse_drift_blips_do_not_split_the_night(store):
    present(store, at("22:00", -1), at("02:00"))
    away(store, at("02:00"), at("08:55"), blips_every=20 * 60)       # one-minute "present" drift rows
    present(store, at("08:55"), at("09:05"))
    gap = resume_gap(store, at("09:01"), CFG)
    assert gap is not None and gap.back_at == at("08:55") and gap.last_present == at("02:00")
    assert gap.new_day                                                  # 02:00 belongs to the day before (04:00)


def test_a_glance_in_the_night_does_not_split_it(store):
    present(store, at("22:00", -1), at("01:30"))
    away(store, at("01:30"), at("06:00"))
    present(store, at("06:00"), at("06:03"))                          # a 3-minute look, then back to bed
    away(store, at("06:03"), at("08:40"))
    present(store, at("08:40"), at("09:00"))
    gap = resume_gap(store, at("08:50"), CFG)
    assert gap is not None and gap.back_at == at("08:40") and gap.last_present == at("01:30")


def test_no_presence_at_all_counts(store):
    present(store, at("21:00", -1), at("03:30"))                      # then nothing recorded (memory not running)
    present(store, at("09:10"), at("09:20"))
    gap = resume_gap(store, at("09:15"), CFG)
    assert gap is not None and round(gap.nothing_s / 60) == 5 * 60 + 40 and gap.away_s == 0


def test_pc_off_overnight_still_counts(store):
    present(store, at("20:00", -1), at("01:00"))
    store.add_power_spans([("off", at("01:02"), at("08:50"), "shutdown")])
    present(store, at("08:52"), at("09:00"))
    gap = resume_gap(store, at("08:58"), CFG)
    assert gap is not None and gap.spans and gap.spans[0]["kind"] == "off"
    assert round(gap.off_s / 60) == 468 and gap.back_at == at("08:52")


def test_short_breaks_never_trigger(store):
    present(store, at("09:00"), at("10:00"))
    for start, end in (("10:10", "12:30"), ("13:15", "15:00"), ("15:07", "18:00")):   # 10 min, 45 min, 7 min breaks
        present(store, at(start), at(end))
    first = resume_gap(store, at("09:06"), CFG)                        # the morning's return (no evening before it)
    assert first is not None and first.back_at == at("09:00")
    for when in ("10:15", "13:20", "15:10", "17:59"):
        gap = resume_gap(store, at(when), CFG, done_key=first.key)
        assert gap is None, when


def test_break_straddling_day_start_is_a_new_day(store):
    present(store, at("23:00", -1), at("03:50"))
    present(store, at("04:10"), at("04:30"))
    gap = resume_gap(store, at("04:20"), CFG)
    assert gap is not None and gap.new_day and round(gap.seconds / 60) == 20
    # the same 20 minutes in the afternoon: nothing
    present(store, at("14:00"), at("15:00"))
    present(store, at("15:20"), at("15:40"))
    later = resume_gap(store, at("15:30"), CFG, done_key=resume_gap(store, at("14:30"), CFG).key)
    assert later is None


def test_window_runs_from_the_return_and_a_short_break_does_not_hide_it(store):
    present(store, at("20:00", -1), at("01:00"))
    away(store, at("01:00"), at("09:00"))
    present(store, at("09:00"), at("09:10"))
    present(store, at("09:20"), at("10:30"))                          # a 10-minute break after coming back
    gap = resume_gap(store, at("09:25"), CFG)
    assert gap is not None and gap.back_at == at("09:00") and not gap.expired
    assert resume_gap(store, at("10:01"), CFG).expired                 # 60 min after the return itself


# -- the whole tick, with a stub model ---------------------------------------------------


class StubClient:
    """Answers welcome_back with a message, coach_decision with none."""

    def __init__(self):
        self.calls: list[str] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **request):
        name = request["tool_choice"]["name"]
        self.calls.append(name)
        if name == "welcome_back":
            data = {"reason": "stub", "say": "resume", "detail": "the resume fix", "evidence": ["NOW"],
                    "text": "Morning, you were on the resume fix.", "mentions": []}
        elif name == "coach_decision":
            data = {"reason": "stub", "say": "none", "is_break_suggestion": False, "detail": "", "evidence": [],
                    "mentions": []}
        else:
            data = {"reason": "stub", "say": "none", "text": None}
        block = SimpleNamespace(type="tool_use", name=name, input=data)
        return SimpleNamespace(content=[block], stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20,
                                                     cache_creation_input_tokens=0, cache_read_input_tokens=0))


def ticks(worker: NudgeWorker, start: float, end: float, step: float = 60.0) -> list[dict]:
    out = []
    t = start
    while t <= end:
        out += worker.tick(t)
        t += step
    return out


def test_worker_welcomes_back_once_after_a_night_away(store):
    with tempfile.TemporaryDirectory() as logs:
        present(store, at("20:00", -1), at("05:00"))
        away(store, at("05:00"), at("09:00"))
        present(store, at("09:00"), at("11:00"))
        client = StubClient()
        worker = NudgeWorker(store, config=CFG, client=client, log_dir=Path(logs), locked_fn=lambda: False,
                             event_name="Local\\YukiNudgeReadyTest")
        done = ticks(worker, at("08:00"), at("11:00"))
        resumes = [d for d in done if d["trigger"] == "resume"]
        # the evening's own start (nothing recorded before it) is an old return: skipped once, then this one
        assert [d["outcome"] for d in resumes] in (["resume"], ["skipped:window", "resume"])
        assert client.calls.count("welcome_back") == 1
        assert at("09:05") <= next(n.at for n in store.nudges(kinds=("resume",))) <= at("09:07")
        assert store.nudge_state(RESUME_STATE) == f"resume:{int(at('09:00'))}"


def test_worker_short_breaks_never_welcome(store):
    with tempfile.TemporaryDirectory() as logs:
        present(store, at("09:00"), at("10:00"))
        present(store, at("10:10"), at("12:30"))
        present(store, at("13:15"), at("18:00"))
        client = StubClient()
        worker = NudgeWorker(store, config=CFG, client=client, log_dir=Path(logs), locked_fn=lambda: False,
                             event_name="Local\\YukiNudgeReadyTest")
        done = ticks(worker, at("09:00"), at("18:00"))
        resumes = [d for d in done if d["trigger"] == "resume"]
        assert len(resumes) == 1 and resumes[0]["outcome"] == "resume"   # the morning's first presence only
        assert client.calls.count("welcome_back") == 1
