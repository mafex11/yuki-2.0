"""Report 76 quick win 4: the cost alert speaks only on an unusual day (a measured line), and the portrait passes mark
their instructions for the prompt cache. Temp folders, stub clients; no model is called."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuki.core.cost import BASELINE_DAYS, SPIKE_FACTOR, CostAlert, CostLedger, past_day_spend

TODAY = date.today()


def _memory_day(logs: Path, day: date, usd: float) -> None:
    (logs / "memory").mkdir(parents=True, exist_ok=True)
    (logs / "memory" / f"journal-{day:%Y%m%d}.jsonl").write_text(
        json.dumps({"type": "journal_call", "cost_usd": usd}) + "\n", encoding="utf-8")


def _alert(tmp_path: Path, floor: float = 3.0, notes: list | None = None) -> CostAlert:
    return CostAlert(floor, CostLedger(tmp_path / "logs"), state_path=tmp_path / "alert.json",
                     notify=(lambda text, spend: notes.append(text)) if notes is not None else None)


def _history(tmp_path: Path, values: dict[int, float]) -> None:
    hist = {(TODAY - timedelta(days=k)).isoformat(): v for k, v in values.items()}
    (tmp_path / "alert.json").write_text(json.dumps({"history": hist}), encoding="utf-8")


def test_the_line_is_measured_from_a_usual_day(tmp_path):
    _history(tmp_path, {k: v for k, v in zip(range(1, 8), (9.6, 16.0, 8.4, 7.8, 4.3, 4.5, 10.7))})
    line, usual = _alert(tmp_path).line(TODAY)
    assert usual == pytest.approx(8.4) and line == pytest.approx(SPIKE_FACTOR * 8.4)


def test_a_usual_day_no_longer_alerts_and_an_unusual_one_does(tmp_path):
    _history(tmp_path, {k: 9.0 for k in range(1, 8)})
    notes: list[str] = []
    _memory_day(tmp_path / "logs", TODAY, 10.0)                # over the old $3 line, a usual day now
    assert _alert(tmp_path, notes=notes).check() is False and notes == []
    with (tmp_path / "logs" / "memory" / f"journal-{TODAY:%Y%m%d}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "journal_call", "cost_usd": 5.0}) + "\n")   # 15 > 1.5 x 9
    alert = _alert(tmp_path, notes=notes)
    assert alert.check() is True and alert.check() is False    # once a day
    assert "more than a usual day (about $9.00)" in notes[0] and "$15.00" in notes[0]


def test_the_floor_holds_on_cheap_weeks(tmp_path):
    _history(tmp_path, {k: 0.5 for k in range(1, 8)})
    assert _alert(tmp_path, floor=3.0).line(TODAY) == (3.0, pytest.approx(0.5))


def test_without_three_known_days_the_floor_is_the_line(tmp_path):
    _history(tmp_path, {1: 9.0, 2: 0.0})                       # a day with no spend is not a usual day
    assert _alert(tmp_path).line(TODAY) == (3.0, None)


def test_missing_days_are_read_from_the_logs_one_per_check(tmp_path):
    logs = tmp_path / "logs"
    for k in range(1, 5):
        _memory_day(logs, TODAY - timedelta(days=k), 2.0 * k)
    alert = _alert(tmp_path)
    assert alert.baseline(TODAY) == (None, 1)                  # one day read per call
    assert alert.baseline(TODAY) == (None, 2)
    assert alert.baseline(TODAY) == (pytest.approx(4.0), 3)    # three known: the median of 2, 4, 6
    state = json.loads((tmp_path / "alert.json").read_text(encoding="utf-8"))
    assert len(state["history"]) == 3
    usual, n = alert.baseline(TODAY)
    assert n == 4 and usual == pytest.approx(5.0)              # median of 2, 4, 6, 8
    for _ in range(BASELINE_DAYS):
        alert.baseline(TODAY)
    state = json.loads((tmp_path / "alert.json").read_text(encoding="utf-8"))
    assert set(state["history"]) == {(TODAY - timedelta(days=k)).isoformat() for k in range(1, BASELINE_DAYS + 1)}


def test_past_day_spend_reads_the_daily_files(tmp_path):
    _memory_day(tmp_path, TODAY - timedelta(days=2), 1.25)
    assert past_day_spend(tmp_path, TODAY - timedelta(days=2)) == pytest.approx(1.25)
    assert past_day_spend(tmp_path, TODAY - timedelta(days=3)) == 0.0


# -- portrait prompt caching ------------------------------------------------------------------------------------------


class StreamClient:
    """Records each request; answers with one tool call (the shape PortraitWorker._call reads)."""

    def __init__(self, caching: str | None) -> None:
        self.requests: list[dict] = []
        if caching is not None:
            self.prompt_caching = lambda: caching
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **request):
        self.requests.append(request)
        response = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0,
                                  cache_read_input_tokens=0),
            content=[SimpleNamespace(type="tool_use", name="t", input={"ok": True})], stop_reason="tool_use")

        class Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get_final_message=lambda: response)

            def __exit__(self_inner, *exc):
                return False

        return Ctx()


@pytest.fixture()
def worker_for(tmp_path):
    from yuki.memory.portrait import PortraitWorker
    from yuki.memory.store import PortraitRunStats, Store

    store = Store.open(tmp_path / "memory.db")

    def make(caching):
        client = StreamClient(caching)
        w = PortraitWorker(store, client=client, log_dir=tmp_path / "logs")
        w._call(purpose="audit", run_id=None, system="You audit the portrait." * 50, user="part 1",
                tool={"name": "t", "input_schema": {"type": "object"}}, effort="low", max_tokens=100,
                stats=PortraitRunStats(kind="audit", model="stub"))
        return client.requests[0]

    yield make
    store.close()


def test_the_portrait_passes_mark_their_instructions_for_the_cache(worker_for):
    req = worker_for("explicit")
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["system"][0]["text"].startswith("You audit the portrait.")
    assert isinstance(req["messages"][0]["content"], str)      # the per-call message stays out of the prefix


@pytest.mark.parametrize("caching", ["none", "auto", None])
def test_no_mark_where_the_explicit_cache_does_not_apply(worker_for, caching):
    assert isinstance(worker_for(caching)["system"], str)


def test_the_memory_client_says_explicit_on_the_default_path():
    from yuki.agent.failover import make_client
    from yuki.config import Settings

    assert make_client(Settings()).prompt_caching() in ("explicit", "auto", "none")
