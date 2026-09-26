"""Offline checks of calls in memory's outputs (yuki.memory.callspans, MemoryClient.activity,
format_activity, the MCP get_activity text and the request-origin line).

Temp databases with synthetic calls and timeline rows; nothing touches the desktop or a model.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from yuki.memory.api import MemoryClient
from yuki.memory.callspans import call_spans, coach_calls_text, open_calls
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


def call(store: Store, start: float, end: float | None, *, app: str = "Discord", process: str = "discord",
         reason: str = "call_ended") -> int:
    cid = store.start_call(app=app, process=process, via="mic", at=start, status="recording", consent="always")
    if end is not None:
        store.update_call(cid, status="ended", ended_at=end, stop_reason=reason)
    return cid


def present(store: Store, start: float, end: float, app: str, *, process: str | None = None,
            meeting: str | None = None) -> None:
    store.save_timeline(TimelineRow(None, start, end, "present", process or app.lower(), app, active_s=end - start,
                                    meeting=meeting, mic_s=(end - start) if meeting else 0.0))


def restart_split_day(store: Store) -> None:
    """A Discord call split by two service restarts and a reboot, then a separate call much later."""
    call(store, at("13:20"), at("14:05"), reason="service_restart")
    call(store, at("14:05"), at("21:05"), reason="service_restart")   # the PC off 21:01-21:03 inside it
    call(store, at("21:05"), at("01:14", 1))
    call(store, at("09:00", 1), at("09:30", 1))
    store.add_power_spans([("off", at("21:01"), at("21:03"), "shutdown")])
    present(store, at("13:30"), at("14:00"), "Discord")
    present(store, at("14:00"), at("16:00"), "Google Chrome", process="chrome")
    present(store, at("16:00"), at("16:31"), "Discord")
    present(store, at("16:31"), at("17:31"), "Warp")


def test_restart_split_calls_merge_and_keep_the_power_span(store):
    restart_split_day(store)
    spans = call_spans(store, at("00:00"), at("00:00", 1), now=at("12:00", 1))
    assert len(spans) == 1
    s = spans[0]
    assert (s["start"], s["end"], s["ongoing"]) == (at("13:20"), at("01:14", 1), False)
    assert s["seconds"] == pytest.approx(at("01:14", 1) - at("13:20"), abs=1)
    assert s["window_s"] == pytest.approx(at("00:00", 1) - at("13:20"), abs=1)
    assert [p["kind"] for p in s["power"]] == ["off"]
    assert s["in_front_s"] == pytest.approx(61 * 60, abs=1)
    assert s["background_s"] == pytest.approx(s["seconds"] - 61 * 60, abs=1)
    assert [m["app"] for m in s["meanwhile"]] == ["Google Chrome", "Warp"]


def test_call_before_since_keeps_its_real_start_and_a_late_one_stays_apart(store):
    restart_split_day(store)
    spans = call_spans(store, at("22:00"), at("10:00", 1), now=at("12:00", 1))
    assert [round(s["start"]) for s in spans] == [round(at("13:20")), round(at("09:00", 1))]
    assert spans[0]["window_s"] == pytest.approx(at("01:14", 1) - at("22:00"), abs=1)


def test_short_drop_is_one_call_long_gap_is_two(store):
    call(store, at("09:00"), at("09:10"), app="Google Meet", process="zen")
    call(store, at("09:16"), at("09:40"), app="Google Meet", process="zen")    # muted 6 min
    call(store, at("11:00"), at("11:30"), app="Google Meet", process="zen")    # another meeting
    spans = call_spans(store, at("08:00"), at("12:00"), now=at("13:00"))
    assert [len(s["call_ids"]) for s in spans] == [2, 1]
    assert spans[0]["drops"][0]["seconds"] == pytest.approx(360, abs=1)


def test_ongoing_call_and_as_of_now(store):
    call(store, at("13:20"), None)
    assert [c["app"] for c in open_calls(store, at("15:00"))] == ["Discord"]
    ended = call(store, at("16:00"), at("18:00"), app="Zoom", process="zoom")
    assert ended
    # as of 17:00 the Zoom call was still going (its end lies after that moment)
    assert sorted(c["app"] for c in open_calls(store, at("17:00"))) == ["Discord", "Zoom"]
    text = coach_calls_text(store, at("17:00"))
    assert text is not None and "on a Discord call since 13:20" in text and "still going" in text


def test_coach_calls_text_none_without_calls(store):
    present(store, at("10:00"), at("11:00"), "Warp")
    assert coach_calls_text(store, at("11:00")) is None


def test_activity_reports_calls_meetings_and_background(store):
    from yuki.agent.memory import format_activity

    restart_split_day(store)
    present(store, at("10:00"), at("10:20"), "Zen", process="zen", meeting="Google Meet")
    client = MemoryClient(store)
    agg = client.activity(since=at("00:00"), until=at("00:00", 1), group_by="app")
    discord = next(i for i in agg["items"] if i["label"] == "Discord")
    assert discord["call_s"] == pytest.approx(at("00:00", 1) - at("13:20"), abs=1)
    assert agg["calls"][0]["start"].startswith("2026-09-25T13:20")
    assert agg["background"][0]["app"] == "Discord"
    text = format_activity(agg, [])
    assert "Calls (an app using the microphone" in text
    assert "Discord call 13:20" in text or "Discord call Fri 13:20" in text
    assert "in a call 10h40m" in text
    assert "Meetings (" in text and "Google Meet" in text
    assert "Background (while other apps were in front)" in text and "Discord in a call" in text


def test_mcp_activity_text_lists_calls(store):
    from yuki.mcp.server import _format_activity
    from yuki.mcp.times import resolve_range

    restart_split_day(store)
    agg = MemoryClient(store).activity(since=at("00:00"), until=at("00:00", 1), group_by="site")
    window = resolve_range("2026-09-25", "2026-09-26", default_today=True)
    text = _format_activity(agg, window, "site")
    assert "Calls (an app using the microphone" in text and "in a call" in text


def test_origin_line_names_the_call_instead_of_windows_mic_time():
    from yuki.agent.loop import _origin_text

    facts = {"microphone": [{"app": "Discord.exe", "since": at("21:04")}], "camera": [], "media": []}
    calls = [{"app": "Discord", "process": "discord", "since": "2026-09-25T13:20:57+05:30",
              "seconds": 8 * 3600 + 16 * 60}]
    line = _origin_text({}, facts, calls=calls)
    assert "Discord.exe, on a Discord call since" in line and "(8h16m)" in line
    assert "since 21:04" not in line
    assert "Discord.exe since 21:04" in _origin_text({}, facts)
