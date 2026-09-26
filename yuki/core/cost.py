"""The day's model spend across the agent, the voice and memory, and the one soft alert a day.

Read from the logs Yuki already writes (estimates at list prices, ``Settings.pricing``):

* **agent** - ``logs/requests.csv``: one row per finished request of any lane (worker, front
  desk, CLI, spoken requests), read helpers' calls included (they are priced into the request
  that ran them);
* **agent start-up** - the ``startup_cost`` records (the model warm-up) in ``logs/sessions/*.jsonl``;
* **memory** - ``logs/memory/<worker>-YYYYMMDD.jsonl``. A worker file that logs one record per
  model call (a type ending in ``_call``) also logs roll-ups of the same calls (``portrait_run``,
  ``portrait_persona``, ``portrait_dream``, ``nudge_checkin``, ``episode_run``: measured on
  26 Sept, ``portrait_call`` = run + persona + dream to the cent), so only its ``*_call`` records
  count; a file without them (conversations: one record per call) counts every ``cost_usd``.
  The service's own ``logs/memory-*.jsonl`` repeats the roll-ups and is not read;
* **voice** - ``logs/voice/voice-YYYYMMDD.jsonl``: the mouth per turn (``turn.mouth_cost_usd``;
  the session record repeats the sum). Speech services (Deepgram STT from the session record,
  TTS per turn) are listed apart: they are not model spend.

:func:`today_spend` reads it all; :class:`CostLedger` does the same incrementally (each file read
from where it stopped) for a check every few minutes; :class:`CostAlert` fires once per day past
``Settings.daily_cost_alert_usd`` and remembers that in ``%LOCALAPPDATA%\\Yuki\\cost_alert.json``.

CLI: ``yuki cost today``.
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable


@dataclass
class Spend:
    """One day's spend, split by where it went (US dollars)."""

    day: str
    agent: float = 0.0
    agent_requests: int = 0
    agent_startup: float = 0.0
    memory: dict[str, float] = field(default_factory=dict)
    voice_mouth: float = 0.0
    speech: dict[str, float] = field(default_factory=dict)

    @property
    def memory_total(self) -> float:
        return sum(self.memory.values())

    @property
    def model_total(self) -> float:
        """Model spend: agent + start-up + memory + voice mouth (speech services not included)."""
        return self.agent + self.agent_startup + self.memory_total + self.voice_mouth

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day, "model_total_usd": round(self.model_total, 4), "agent_usd": round(self.agent, 4),
            "agent_requests": self.agent_requests, "agent_startup_usd": round(self.agent_startup, 4),
            "memory_usd": {k: round(v, 4) for k, v in sorted(self.memory.items())},
            "memory_total_usd": round(self.memory_total, 4), "voice_mouth_usd": round(self.voice_mouth, 4),
            "speech_usd": {k: round(v, 4) for k, v in sorted(self.speech.items())},
        }


def logs_dir(settings: Any = None) -> Path:
    """``<project>/logs`` (the parent of ``Settings.sessions_dir``)."""
    if settings is None:
        from yuki.config import Settings

        settings = Settings()
    return Path(settings.sessions_dir).parent


def _num(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _day_of_ts(ts: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(ts)).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


class _Tail:
    """Reads a growing JSONL file from where it stopped (whole lines only)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.rest = b""

    def new_records(self) -> list[dict[str, Any]]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:            # rotated or rewritten: start again
            self.offset, self.rest = 0, b""
        if size == self.offset:
            return []
        with self.path.open("rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read(size - self.offset)
        self.offset = size
        data = self.rest + chunk
        lines = data.split(b"\n")
        self.rest = lines.pop()           # a line still being written
        out = []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(record, dict):
                out.append(record)
        return out


class CostLedger:
    """The day's spend, kept up to date incrementally (thread-safe)."""

    def __init__(self, logs: Path | None = None, *, day: date | None = None) -> None:
        self.logs = Path(logs) if logs is not None else logs_dir()
        self._fixed_day = day
        self._lock = threading.Lock()
        self._reset(self._today())

    def _today(self) -> date:
        return self._fixed_day or date.today()

    def _reset(self, day: date) -> None:
        self.day = day
        self.spend = Spend(day.isoformat())
        self._tails: dict[Path, _Tail] = {}
        #: per memory file: ``(has *_call records, {kind: sum of *_call}, {kind: sum of all})``
        self._memory_files: dict[Path, dict[str, Any]] = {}
        self._voice: dict[str, float] = {"mouth": 0.0, "tts": 0.0, "stt": 0.0}

    def _tail(self, path: Path) -> _Tail:
        tail = self._tails.get(path)
        if tail is None:
            tail = self._tails[path] = _Tail(path)
        return tail

    # -- sources -----------------------------------------------------------------------------

    def _requests(self) -> None:
        path = self.logs / "requests.csv"
        total, count = 0.0, 0
        prefix = self.day.isoformat()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for row in csv.DictReader(io.StringIO(text)):
            if str(row.get("timestamp") or "").startswith(prefix):
                total += _num(row.get("cost_usd"))
                count += 1
        self.spend.agent, self.spend.agent_requests = total, count

    def _startup(self) -> None:
        sessions = self.logs / "sessions"
        day = self.day.isoformat()
        start = datetime.combine(self.day, datetime.min.time()).timestamp()
        try:
            files = [p for p in sessions.glob("*.jsonl") if p.stat().st_mtime >= start]
        except OSError:
            files = []
        for path in files:
            for record in self._tail(path).new_records():
                if record.get("type") == "startup_cost" and _day_of_ts(record.get("ts")) == day:
                    self.spend.agent_startup += _num(record.get("cost_usd"))

    def _memory(self) -> None:
        stamp = self.day.strftime("%Y%m%d")
        for path in (self.logs / "memory").glob(f"*-{stamp}.jsonl"):
            state = self._memory_files.setdefault(path, {"calls": {}, "all": {}, "has_calls": False})
            kind = path.name.rsplit("-", 1)[0]
            for record in self._tail(path).new_records():
                cost = record.get("cost_usd")
                if cost is None:
                    continue
                rtype = str(record.get("type") or "")
                state["all"][kind] = state["all"].get(kind, 0.0) + _num(cost)
                if rtype.endswith("_call"):
                    state["has_calls"] = True
                    state["calls"][kind] = state["calls"].get(kind, 0.0) + _num(cost)
        memory: dict[str, float] = {}
        for path, state in self._memory_files.items():
            source = state["calls"] if state["has_calls"] else state["all"]
            for kind, value in source.items():
                memory[kind] = memory.get(kind, 0.0) + value
        self.spend.memory = memory

    def _voice_logs(self) -> None:
        path = self.logs / "voice" / f"voice-{self.day.strftime('%Y%m%d')}.jsonl"
        for record in self._tail(path).new_records():
            rtype = record.get("type") or record.get("event")
            if rtype == "turn":
                self._voice["mouth"] += _num(record.get("mouth_cost_usd"))
                self._voice["tts"] += _num(record.get("tts_cost_usd"))
            elif rtype == "session":
                self._voice["stt"] += _num(record.get("stt_cost_usd"))
        self.spend.voice_mouth = self._voice["mouth"]
        self.spend.speech = {"stt": self._voice["stt"], "tts": self._voice["tts"]}

    def refresh(self) -> Spend:
        """Read what is new and return the day's spend (a new day starts from zero)."""
        with self._lock:
            today = self._today()
            if today != self.day:
                self._reset(today)
            for step in (self._requests, self._startup, self._memory, self._voice_logs):
                try:
                    step()
                except Exception:   # one unreadable source must not hide the others
                    continue
            return self.spend


def today_spend(settings: Any = None, *, day: date | None = None) -> Spend:
    """The day's spend, read in full (the CLI)."""
    return CostLedger(logs_dir(settings), day=day).refresh()


def _state_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki" / "cost_alert.json"


class CostAlert:
    """Once a day, when the day's model spend passes the threshold: log it and call ``notify``.

    Args:
        threshold_usd: ``Settings.daily_cost_alert_usd`` (0 or less: never).
        ledger: Where the spend comes from.
        notify: ``notify(text, spend)`` - the tray notice (the runtime's ``notice`` signal).
        log: ``log(event, **fields)``.
        state_path: Remembers the day already alerted (survives restarts).
    """

    def __init__(self, threshold_usd: float, ledger: CostLedger, *, notify: Callable[[str, Spend], Any] | None = None,
                 log: Callable[..., Any] | None = None, state_path: Path | None = None) -> None:
        self.threshold = float(threshold_usd)
        self.ledger = ledger
        self.notify = notify
        self.log = log
        self.state_path = state_path or _state_path()
        self._lock = threading.Lock()

    def _alerted_day(self) -> str | None:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8")).get("day")
        except (OSError, ValueError, AttributeError):
            return None

    def _remember(self, day: str, total: float) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"day": day, "total_usd": round(total, 4),
                                                   "at": datetime.now().isoformat(timespec="seconds")}),
                                       encoding="utf-8")
        except OSError:
            pass

    def check(self) -> bool:
        """Read the spend; alert (once a day) past the threshold. True when it alerted now. Never raises."""
        if self.threshold <= 0:
            return False
        with self._lock:
            try:
                spend = self.ledger.refresh()
            except Exception:
                return False
            total = spend.model_total
            if total < self.threshold or self._alerted_day() == spend.day:
                return False
            self._remember(spend.day, total)
            text = alert_text(spend, self.threshold)
            if self.log is not None:
                try:
                    self.log("cost_alert", threshold_usd=self.threshold, **spend.as_dict())
                except Exception:
                    pass
            if self.notify is not None:
                try:
                    self.notify(text, spend)
                except Exception:
                    pass
            return True


def alert_text(spend: Spend, threshold: float) -> str:
    """The notice, in plain words."""
    parts = [f"memory ${spend.memory_total:.2f}", f"chat and tasks ${spend.agent + spend.agent_startup:.2f}"]
    if spend.voice_mouth:
        parts.append(f"voice ${spend.voice_mouth:.2f}")
    return (f"Heads up: Yuki has used about ${spend.model_total:.2f} of AI today (over your ${threshold:.2f} "
            f"line) - {', '.join(parts)}. Nothing is slowed down; this is just so you know.")


def format_spend(spend: Spend, threshold: float | None = None) -> str:
    """``yuki cost today``: the day's spend as a few plain lines."""
    lines = [f"Yuki's AI spend on {spend.day} (estimates at list prices)"]
    lines.append(f"  total (models):   ${spend.model_total:.2f}"
                 + (f"   (alert line ${threshold:.2f})" if threshold else ""))
    lines.append(f"  chat and tasks:   ${spend.agent:.2f}  ({spend.agent_requests} requests)")
    lines.append(f"  agent start-up:   ${spend.agent_startup:.2f}")
    lines.append(f"  memory:           ${spend.memory_total:.2f}")
    for kind, value in sorted(spend.memory.items(), key=lambda kv: -kv[1]):
        lines.append(f"    {kind:<15} ${value:.2f}")
    lines.append(f"  voice (mouth):    ${spend.voice_mouth:.2f}")
    if any(spend.speech.values()):
        lines.append(f"  speech services (not in the total): STT ${spend.speech.get('stt', 0):.2f}, "
                     f"TTS ${spend.speech.get('tts', 0):.2f}")
    return "\n".join(lines)
