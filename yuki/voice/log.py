"""The voice log: content-free, one line per event, and one CSV row per turn.

What was said lives only in memory (encrypted, through ``log_turn``), so these
files carry no words at all: timings, sizes, token counts, costs, languages,
states and errors. ``logs/voice/voice-YYYYMMDD.jsonl`` gets every event,
``logs/voice_turns.csv`` one row per turn (what ``scripts/voice_latency.py``
reads). Written from the voice thread, the wake thread and the GUI thread, so
every write takes a lock.

Turn fields (all milliseconds unless named otherwise; ``None`` when a stage
did not happen, e.g. no speech in a turn the mouth opened to speak a result):

* ``eos_to_transcript_ms``: the user's last voiced audio to Flux's final
  transcript (end of turn detection included).
* ``mouth_ttft_ms``: the mouth request to its first token (Pipecat's TTFB).
* ``tts_ttfb_ms``: text sent to TTS to its first audio byte.
* ``eos_to_first_audio_ms``: the user's last voiced audio to Yuki's first
  audio going out: what the user feels.
"""

from __future__ import annotations

import csv
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

#: Columns of ``voice_turns.csv``, in order.
TURN_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "session",
    "turn",
    "trigger",
    "language",
    "user_chars",
    "reply_chars",
    "eos_to_transcript_ms",
    "mouth_ttft_ms",
    "tts_ttfb_ms",
    "eos_to_first_audio_ms",
    "mouth_input_tokens",
    "mouth_cache_read_tokens",
    "mouth_cache_write_tokens",
    "mouth_output_tokens",
    "mouth_cost_usd",
    "tts_chars",
    "tts_cost_usd",
    "tool_calls",
    "interrupted",
    "error",
)


class VoiceLog:
    """Content-free JSONL + CSV writer for the voice layer.

    Args:
        log_dir: Where the JSONL files go (``Settings.voice_log_dir``).
        turns_csv: The per-turn CSV (default ``<log_dir>/../voice_turns.csv``).
    """

    def __init__(self, log_dir: Path, turns_csv: Path | None = None) -> None:
        self.log_dir = Path(log_dir)
        self.turns_csv = Path(turns_csv) if turns_csv else self.log_dir.parent / "voice_turns.csv"
        self._lock = threading.Lock()

    def _jsonl_path(self) -> Path:
        return self.log_dir / f"voice-{datetime.now():%Y%m%d}.jsonl"

    def event(self, event_type: str, **fields: Any) -> dict[str, Any]:
        """Write one event. Callers pass no user or model text, only facts about it."""
        record = {"ts": round(time.time(), 3), "at": datetime.now().isoformat(timespec="milliseconds"),
                  "type": event_type, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                with self._jsonl_path().open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass  # logging must never take voice down
        return record

    def turn(self, row: dict[str, Any]) -> None:
        """One finished turn: a ``turn`` event and a CSV row (missing columns stay empty)."""
        self.event("turn", **row)
        values = {key: row.get(key) for key in TURN_COLUMNS}
        values["timestamp"] = values["timestamp"] or datetime.now().isoformat(timespec="seconds")
        with self._lock:
            try:
                self.turns_csv.parent.mkdir(parents=True, exist_ok=True)
                new = not self.turns_csv.exists()
                with self.turns_csv.open("a", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=list(TURN_COLUMNS))
                    if new:
                        writer.writeheader()
                    writer.writerow({k: "" if v is None else v for k, v in values.items()})
            except OSError:
                pass
