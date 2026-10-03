"""The voice log: content-free, one line per event, and one CSV row per turn.

What was said lives only in memory (encrypted, through ``log_turn``), so these
files carry no words at all: timings, sizes, token counts, costs, languages,
states and errors. ``logs/voice/voice-YYYYMMDD.jsonl`` gets every event,
``logs/voice_turns.csv`` one row per turn (what ``scripts/voice_latency.py``
reads). Written from the voice thread, the wake thread and the GUI thread, so
every write takes a lock. A write asked for on the voice loop's own thread (an
asyncio loop running there) goes to one small writer thread instead (v3.13: live
2026-09-29 one append stalled the voice loop 1.7 s while the disk was busy); other
threads write at once. ``flush()`` waits for the writer.

Turn fields (all milliseconds unless named otherwise; ``None`` when a stage
did not happen, e.g. no speech in a turn the mouth opened to speak a result):

* ``eos_source``: how the end of speech is known: ``mic`` (the last voiced
  block, judged against the room's own floor), ``stt`` (speech recognition's
  last new word, when the mic level never rose above the floor) or ``none``.
* ``eos_to_transcript_ms``: the user's last voiced audio to Flux's final
  transcript (end of turn detection included).
* ``eos_to_first_token_ms``: end of speech to the mouth's first text token.
* ``mouth_ttft_ms``: the mouth request to its first token (Pipecat's TTFB).
* ``tts_ttfb_ms``: text sent to TTS to its first audio byte.
* ``eos_to_first_audio_ms``: the user's last voiced audio to Yuki's first
  audio going out: what the user feels.
* Stage by stage: ``eos_to_eot_ms`` (Flux's end-of-turn event),
  ``eos_to_eager_ms`` (its eager prediction, when on), ``transcript_to_context_ms``
  (the user aggregator hands the context to the mouth), ``llm_start_after_eos_ms``
  (the mouth's call begins), ``grounding_wait_ms`` (waiting for the memory
  lookup; ``grounding`` says which one it got), ``desk`` / ``desk_ms`` / ``desk_chars``
  (the desktop summary for the mouth's hands: fresh or the previous one, its
  capture time or age, its size), ``llm_prep_ms`` (building the
  request), ``llm_open_ms`` (request out to stream open), ``llm_first_event_ms``,
  ``llm_api_ttft_ms`` (request out to the first content block),
  ``llm_first_text_ms``, ``text_to_tts_ms`` (first text to the TTS request),
  ``tts_audio_to_speaking_ms`` (first TTS audio to it playing); ``eager`` says
  the reply was started from Flux's prediction.

A CSV written with an older column set is moved aside (``*.old-<time>.csv``)
the first time a row with the new set is written, so every file has one header.
"""

from __future__ import annotations

import atexit
import csv
import json
import queue
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
    # the user-felt path first: end of speech -> transcript -> mouth's first token -> first audio
    "eos_source",
    "eos_to_transcript_ms",
    "eos_to_first_token_ms",
    "eos_to_first_audio_ms",
    "eos_to_action_ms",
    "eos_to_handoff_ms",
    # then every stage in pipeline order (2026-09-25, docs/VOICE.md §14)
    "eos_to_eot_ms",
    "eos_to_eager_ms",
    "transcript_to_context_ms",
    "llm_start_after_eos_ms",
    "grounding",
    "grounding_facts",
    "grounding_wait_ms",
    "desk",
    "desk_ms",
    "desk_chars",
    "desk_tabs_ms",
    "llm_prep_ms",
    "llm_open_ms",
    "llm_first_event_ms",
    "llm_api_ttft_ms",
    "mouth_ttft_ms",
    "llm_first_text_ms",
    "llm_early_say_ms",
    "text_to_tts_ms",
    "tts_ttfb_ms",
    "tts_audio_to_speaking_ms",
    "eager",
    # usage and outcome
    "mouth_input_tokens",
    "mouth_cache_read_tokens",
    "mouth_cache_write_tokens",
    "mouth_output_tokens",
    "mouth_cost_usd",
    "tts_chars",
    "tts_cost_usd",
    "tool_calls",
    "interrupted",
    "cut_by_user",
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
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._writer: threading.Thread | None = None
        self._queued = 0
        self._drained = threading.Condition()
        # v3.17 (D7): the writer thread starts here, where the log is made (never on the voice loop: a thread started
        # from the loop waits for the interpreter lock there, v3.14), so the loop's first write only queues.
        with self._drained:
            self._ensure_writer()

    def _ensure_writer(self) -> None:
        """Start the writer thread unless it runs (call with ``_drained`` held)."""
        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(target=self._write_loop, name="yuki-voice-log", daemon=True)
            self._writer.start()
            atexit.register(self.flush)

    def _jsonl_path(self) -> Path:
        return self.log_dir / f"voice-{datetime.now():%Y%m%d}.jsonl"

    @staticmethod
    def _on_loop() -> bool:
        """Whether this thread runs an asyncio loop now (the voice loop): its writes must never wait on the disk."""
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    def _later(self, write: Any) -> None:
        with self._drained:
            self._queued += 1
            self._ensure_writer()
        self._queue.put(write)

    def _write_loop(self) -> None:
        while True:
            write = self._queue.get()
            try:
                write()
            except Exception:  # noqa: BLE001 - logging must never take voice down
                pass
            with self._drained:
                self._queued -= 1
                if self._queued <= 0:
                    self._drained.notify_all()

    def flush(self, timeout_s: float = 2.0) -> None:
        """Wait (bounded) until every write handed to the writer thread is on disk."""
        with self._drained:
            self._drained.wait_for(lambda: self._queued <= 0, timeout=timeout_s)

    def event(self, event_type: str, **fields: Any) -> dict[str, Any]:
        """Write one event. Callers pass no user or model text, only facts about it."""
        record = {"ts": round(time.time(), 3), "at": datetime.now().isoformat(timespec="milliseconds"),
                  "type": event_type, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        if self._on_loop():
            self._later(lambda: self._append(line))
        else:
            self._append(line)
        return record

    def _append(self, line: str) -> None:
        with self._lock:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                with self._jsonl_path().open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass  # logging must never take voice down

    def turn(self, row: dict[str, Any]) -> None:
        """One finished turn: a ``turn`` event and a CSV row (missing columns stay empty)."""
        self.event("turn", **row)
        values = {key: row.get(key) for key in TURN_COLUMNS}
        values["timestamp"] = values["timestamp"] or datetime.now().isoformat(timespec="seconds")
        if self._on_loop():
            self._later(lambda: self._row(values))
        else:
            self._row(values)

    def _row(self, values: dict[str, Any]) -> None:
        with self._lock:
            try:
                self.turns_csv.parent.mkdir(parents=True, exist_ok=True)
                new = not self.turns_csv.exists()
                if not new:
                    with self.turns_csv.open(encoding="utf-8") as fh:
                        header = fh.readline().strip().split(",")
                    if tuple(header) != TURN_COLUMNS:
                        self.turns_csv.rename(self.turns_csv.with_name(
                            f"{self.turns_csv.stem}.old-{datetime.now():%Y%m%d-%H%M%S}.csv"))
                        new = True
                with self.turns_csv.open("a", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=list(TURN_COLUMNS))
                    if new:
                        writer.writeheader()
                    writer.writerow({k: "" if v is None else v for k, v in values.items()})
            except OSError:
                pass
