"""Calls, both sides: remember what was said in a call - the user's microphone and the call app's own audio.

Decided with the owner (docs/MEMORY.md, "Calls: both sides"; handbook rule 9). From
24 Sept to 3 Oct 2026 only the user's own microphone was recorded. Since 3 Oct a call
is recorded on both sides, on this PC, only when the user said yes for this call (or
"always" for the app): the user's microphone (headphones only, as before) plus the
call app's own audio output through Windows process loopback
(:mod:`yuki.memory.loopback`: only the call app's process tree - never the whole
output, so games, music and Yuki's own voice stay out). A game's own voice chat
(the measured :func:`yuki.memory.avsignals.gaming_with_voice`, never an app list)
is a voice session of its own (``calls.kind`` "game"), with its own track and the
same per-app consent, asked only when a card can be seen. The microphone is recorded
once per voice session, whatever number of app tracks run beside it. Every
transcribed line says who spoke (``who`` user | others), on which app (``app``,
``track_kind`` call | game), and a user line which apps were taking the microphone
then (``heard_by``, from Core Audio's capture-session states: a game that opens its
microphone only while push-to-talk is held tells a reply to teammates from talk to
the friends on the call). Knobs: ``[calls] record_others`` and ``game_voice``.

One thread in ``yuki-memory`` (:class:`CallWorker`, ``yuki-memory-calls``):

1. **Detect: the screen is the proof, the microphone only the hint** (2026-09-28: the Zen browser held the
   microphone for a tab while it played YouTube and was taken for a call; a Google Meet lobby counted too).
   Every ``poll_s`` it reads, content-free, which apps hold the microphone (the ConsentStore via
   :mod:`yuki.memory.avsignals`, re-read only when Windows changes it; Yuki's own processes left out). A
   holder is a call only when a screen of it captured during this hold was judged to show a joined call
   (:class:`yuki.memory.callevidence.CallEvidence`: the watcher's window reads, their visible control names
   judged once by a small model): ``via`` "screen" while that evidence is fresh (``evidence_fresh_s``),
   "screen_stale" after it (probably still in the call, not certain). An app the user listed in
   ``[calls] apps`` holding the microphone is a call too (``via`` "mic"). Sound out, the camera and the
   timeline's meeting row are hints only. A full-screen game in front using its own voice chat is not a
   call (measured: :func:`yuki.memory.avsignals.gaming_with_voice`): it is a **game voice session** of its
   own (``kind`` "game", ``via`` "game"), living like a call (:class:`CallLife`: the game's microphone
   hold, its release grace, and the game seen in front drawing like a game as its "screen"). A call on a
   site inside a browser (an
   app the timeline saw showing ``browser_sites``+ sites) is that site's: its standing answer is kept for
   the site's service, never for the whole browser.
   **Once started, the microphone hold is the session** (:class:`CallLife`, 2026-10-03): the screen decided
   that the hold is a call; the call then lives while its app holds the microphone (a re-grab of the
   microphone by the same app is the same call), for ``release_grace_s`` after it lets go, or while an
   in-call screen shown after it let go is fresh (Meet's mute). The screen ends a held call only on a
   persistent, reliable contradiction (``neg_frames`` healthy "not in a call" sights over ``neg_span_s``),
   never on one frame. A restarted service adopts the open row of a hold that goes on (:meth:`CallWorker.recover`)
   and ends any other at its ``last_alive_at``.
2. **Ask, every call.** A ``calls`` row with status ``asking`` and the named
   event ``Local\\YukiCallReady`` for the UI, which shows a glass card
   (:mod:`yuki.ui.calls`): Yes / No / Always for <app> / Never for <app>. The
   answer comes back through the store (:meth:`Store.answer_call`) and the
   event :func:`yuki.memory.store.call_event_name`. "Always"/"Never" are kept
   per app (``call_prefs``) and answer later calls without a card. No answer
   (card timed out, UI not running, call over) = nothing recorded. While
   nudges are snoozed no card is shown (``not_asked``); memory's pause stops
   everything. A game's card cannot be seen over the full-screen game: its row
   waits as ``ask_later`` and the card goes up at the first moment nothing is
   full screen (the game not in front, or right after its session); recording
   starts from the answer on.
3. **Headphones only.** Before and during recording the default output
   devices' form factor is read (Core Audio, :mod:`yuki.perception.audio`);
   unless it is headphones/headset (or a device the user listed as
   headphones), the microphone is not opened (``waiting_headphones``) and the
   card says why. The app tracks need no headphones: their sound comes from the
   app, not the room.
4. **Keep, do not transcribe.** Shared-mode WASAPI capture of the input
   device the call app records from, and process loopback of each consented
   app (``record_others``), into memory, one :class:`RecordingSession` per
   track. The microphone belongs to the call's row while a call may record,
   else to the game's; a game's track goes into the call's row too while both
   may record (one transcript, both conversations in time order). A speech gate (energy
   floor, then Silero VAD on what is louder) drops everything that is not
   speech; speech is cut into rolling chunks at pauses, compressed (Opus),
   encrypted with the store's key (AES-256-GCM, DPAPI-protected) and written
   to the spool next to the database (:meth:`Store.write_spool`) - never in
   plaintext. No speech-to-text model runs during a call: the deferred
   transcriber (:mod:`yuki.memory.transcribe`) does that when the PC is free,
   deletes each chunk's audio once its words are stored, and hands the call
   to the journal (``my_call_side``: facts and the user's own commitments).

Logs are content-free: ``call_detected``, ``call_consent``,
``call_recording_start``/``call_recording_stop``, ``call_chunk`` (audio and
speech seconds, bytes spooled, encode ms, VAD CPU), ``call_regrab`` (a new
microphone hold inside a call), ``call_alive_by`` (what keeps it alive changed),
``call_resumed`` (adopted after a restart), ``call_ended`` (with the hold, the
screen's last in-call frame, the negatives after it and their frame sizes, what
was in front), ``game_voice_detected`` / ``game_voice_ask``, ``call_track_start`` /
``call_track_stop`` / ``call_track_unavailable`` (an app's own audio: opened,
closed, or why it could not be), ``call_micwatch`` (the capture-session watch),
never words or audio.
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from yuki.memory.avsignals import (
    AvSignals,
    Holder,
    display_name,
    foreground_fullscreen,
    gaming_with_voice,
    gpu_share,
    own_executables,
)
from yuki.memory.store import (
    CALL_OPEN_STATUSES,
    CALL_READY_EVENT,
    Store,
    app_key,
    call_event_name,
    signal_event,
)

RATE = 16_000
#: One gate frame = one Silero VAD window (512 samples at 16 kHz).
FRAME = 512
FRAME_S = FRAME / RATE

# ---------------------------------------------------------------------------
# Configuration: the privacy file's [calls] table (data, not code paths)
# ---------------------------------------------------------------------------


def _packaged_calls_table() -> dict:
    """The packaged ``[calls]`` table (privacy_default.toml); {} when unreadable."""
    try:
        import tomllib

        from yuki.memory.privacy import DEFAULT_PATH

        with DEFAULT_PATH.open("rb") as fh:
            return tomllib.load(fh).get("calls") or {}
    except Exception:
        return {}


@dataclass(frozen=True)
class CallConfig:
    enabled: bool = True
    #: Also record the other side: the call app's (and a game's) own audio through process loopback, as its own
    #: track (owner's decision 3 Oct 2026). False: only the user's microphone, as from 24 Sept to 3 Oct.
    record_others: bool = True
    #: A full-screen game's own voice chat is a voice session of its own (its own track, asked like a call).
    #: False: a game's voice chat is not recorded at all (and never a call).
    game_voice: bool = True
    #: A user line was heard by an app whose capture session was active over at least this share of the line.
    heard_by_overlap: float = 0.3
    #: An app track that could not be opened is tried again after this long (s).
    track_retry_s: float = 300.0
    #: A game's own capture session (Core Audio) active this recently still counts as the game being used for voice
    #: (push-to-talk released a moment ago) - only while its process runs (s).
    game_mic_grace_s: float = 3.0
    #: The user's own additions (empty by default): apps that are a call whenever they hold the
    #: microphone, sound out or not. Every other call is measured (see the module docstring).
    apps: frozenset[str] = frozenset()
    #: The user's own display names (process or package family -> name); default: the app's own name.
    names: tuple[tuple[str, str], ...] = ()
    #: An in-call screen stays fresh evidence this long after it was last seen (s); then the call is "stale"
    #: (probably still going, not certain: voice asks "was that for me?" on ambiguous words).
    evidence_fresh_s: float = 900.0
    # -- one call = one session (CallLife; docs/research/revamp-2026-10-02/05-call-pill-timer.md §7.2) --
    #: The call's app let go of the microphone less than this long ago: still the same call (a re-grab with a new
    #: ConsentStore ``since`` inside it continues the call). Measured: in the mic-only era one drop in 33.8 call
    #: hours (919 s, a real leave); Meet mute gaps 5-536 s are kept by the screen instead (``screen_alive_s``).
    release_grace_s: float = 90.0
    #: A call whose app still holds the microphone ends on the screen only after this many reliable "not in a
    #: call" sights spanning at least ``neg_span_s`` with no in-call frame between (the longest transient measured
    #: was 21 s of Discord in front mid alt-tab; 6 of 6 call-ending flips were within it).
    neg_frames: int = 3
    neg_span_s: float = 120.0
    #: Without the microphone, an in-call frame shown after the app let go of it keeps the call alive this long
    #: (Meet drops the microphone on mute; the screen still shows the meeting).
    screen_alive_s: float = 900.0
    #: The row's ``last_alive_at`` is written at most this often while the call lives (a restart ends a call it
    #: cannot adopt at that moment, never at the restart).
    alive_write_s: float = 15.0
    #: A restarted service adopts the open row of a call whose app still holds the microphone with the same
    #: ``since`` (within ``HOLD_MATCH_S``) when the row was alive at most this long ago.
    adopt_within_s: float = 600.0
    #: A row that records (the microphone or an app track listening) but kept no new chunk for this many minutes is
    #: flagged as stalled: logged once (``call_recording_stalled``), shown on the island and by the doctor
    #: (report 76: three calls showed "recording" for up to an hour and kept nothing, and nobody noticed).
    stall_min: float = 10.0
    #: An app seen showing at least this many different sites in the last ``sites_days`` is a browser: a call
    #: in it is its site's (the standing answer is kept for the site's service, not the browser).
    browser_sites: int = 2
    sites_days: float = 30.0
    poll_s: float = 5.0
    holders_backstop_s: float = 60.0
    end_grace_s: float = 30.0
    ask_timeout_s: float = 120.0
    headphone_form_factors: tuple[str, ...] = ("headphones", "headset", "handset")
    headphone_devices: tuple[str, ...] = ()
    capture_device: str = ""
    stt_model: str = "faster-whisper:large-v3"
    stt_fallback: str = "faster-whisper:small"
    model_dir: str = ""
    languages: tuple[str, ...] = ()
    gate_dbfs: float = -50.0
    vad: bool = True
    vad_threshold: float = 0.5
    min_speech_s: float = 0.5
    chunk_min_s: float = 6.0
    chunk_max_s: float = 25.0
    pause_s: float = 0.7
    # -- speech to text in the deferred worker (the stt_* keys; docs/MEMORY.md "Calls: both sides") --
    stt_beam_size: int = 5
    #: Whisper's temperature fallback: a decode that fails the thresholds below is retried hotter.
    stt_temperatures: tuple[float, ...] = (0.0, 0.4, 0.8)
    #: Whisper's own rules: retry when the text compresses too well (a loop) or its average token
    #: log-probability is too low; a result is silence when no_speech_prob is high AND the log-probability low.
    stt_compression_ratio_threshold: float = 2.4
    stt_log_prob_threshold: float = -1.0
    stt_no_speech_threshold: float = 0.6
    #: Kept segments are dropped when Whisper's no-speech probability is over ``stt_drop_no_speech`` while
    #: its log-probability is under ``stt_drop_log_prob`` (a stricter use of the rule above).
    stt_drop_no_speech: float = 0.2
    stt_drop_log_prob: float = -0.8
    #: A segment is marked unclear for the journal under this average log-probability or over this
    #: no-speech probability; an exact repeat of the previous segment that is unclear and at most
    #: ``stt_repeat_max_tokens`` tokens long is dropped (Whisper's loop on breathing, clicks, game sound).
    stt_unclear_log_prob: float = -0.6
    stt_unclear_no_speech: float = 0.4
    stt_repeat_max_tokens: int = 12
    #: Upper bound of Whisper tokens per second of audio, per language (measured: ~2x the rate of
    #: synthetic speech - en 4, hi 14, ta 13, kn 30, te 25 tokens/s): a decode stuck in a loop stops there.
    stt_tokens_per_s: tuple[tuple[str, float], ...] = (("en", 8.0), ("hi", 28.0), ("ta", 26.0), ("kn", 60.0),
                                                       ("te", 52.0))
    #: A chunk gets ``stt_chunk_limit_s + stt_chunk_limit_rtf * its seconds``; past it no new decode starts
    #: and what was decoded so far is kept.
    stt_chunk_limit_s: float = 10.0
    stt_chunk_limit_rtf: float = 1.0
    #: Names and terms from memory given to Whisper as a hint (hotwords), at most this many tokens; 0 = off.
    stt_vocab_tokens: int = 40
    stt_vocab_days: float = 14.0
    #: Languages the hint is given for (memory's names are in Latin script; with them Whisper translated
    #: Kannada speech into English - measured). Empty = every language.
    stt_vocab_languages: tuple[str, ...] = ("en", "hi")
    #: A style example per language, given to Whisper as the previous text (``[calls.stt_prompts]``).
    stt_prompts: tuple[tuple[str, str], ...] = ()

    def tokens_per_s(self, lang: str | None) -> float:
        rates = dict(self.stt_tokens_per_s)
        return rates.get(lang or "", max(rates.values(), default=60.0))

    def prompt_for(self, lang: str | None) -> str:
        return next((p for k, p in self.stt_prompts if k == lang), "")

    @classmethod
    def from_dict(cls, table: dict | None) -> CallConfig:
        t = table or {}
        d = cls()

        def num(key: str, default: float) -> float:
            try:
                return float(t.get(key, default))
            except (TypeError, ValueError):
                return default

        def strings(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
            raw = t.get(key)
            if not isinstance(raw, list):
                return default
            return tuple(str(x).strip() for x in raw if str(x).strip())

        def table_of(key: str) -> dict:
            # a user file written before a key existed still gets the packaged value (stt_* only)
            raw = t.get(key)
            if raw is None:
                raw = _packaged_calls_table().get(key)
            return raw if isinstance(raw, dict) else {}

        apps = frozenset(k for k in (app_key(a) for a in strings("apps", ())) if k)
        names = tuple((app_key(k) or "", str(v)) for k, v in (t.get("names") or {}).items() if str(v).strip())
        try:
            temps = tuple(float(x) for x in t.get("stt_temperatures", d.stt_temperatures))
        except (TypeError, ValueError):
            temps = d.stt_temperatures
        try:
            rates = tuple((str(k).casefold(), float(v)) for k, v in table_of("stt_tokens_per_s").items())
        except (TypeError, ValueError):
            rates = d.stt_tokens_per_s
        prompts = tuple((str(k).casefold(), str(v).strip()) for k, v in table_of("stt_prompts").items()
                        if str(v).strip())
        return cls(
            enabled=bool(t.get("enabled", d.enabled)), apps=apps, names=names,
            record_others=bool(t.get("record_others", d.record_others)),
            game_voice=bool(t.get("game_voice", d.game_voice)),
            heard_by_overlap=min(1.0, max(0.0, num("heard_by_overlap", d.heard_by_overlap))),
            track_retry_s=max(10.0, num("track_retry_s", d.track_retry_s)),
            game_mic_grace_s=min(30.0, max(0.0, num("game_mic_grace_s", d.game_mic_grace_s))),
            evidence_fresh_s=max(30.0, num("evidence_fresh_s", d.evidence_fresh_s)),
            release_grace_s=min(600.0, max(30.0, num("release_grace_s", d.release_grace_s))),
            neg_frames=max(2, int(num("neg_frames", d.neg_frames))),
            neg_span_s=max(30.0, num("neg_span_s", d.neg_span_s)),
            screen_alive_s=max(0.0, num("screen_alive_s", d.screen_alive_s)),
            alive_write_s=max(5.0, num("alive_write_s", d.alive_write_s)),
            adopt_within_s=max(0.0, num("adopt_within_s", d.adopt_within_s)),
            stall_min=max(1.0, num("stall_min", d.stall_min)),
            browser_sites=max(2, int(num("browser_sites", d.browser_sites))),
            sites_days=max(1.0, num("sites_days", d.sites_days)),
            poll_s=max(1.0, num("poll_s", d.poll_s)),
            holders_backstop_s=max(5.0, num("holders_backstop_s", d.holders_backstop_s)), end_grace_s=max(0.0, num("end_grace_s", d.end_grace_s)),
            ask_timeout_s=max(10.0, num("ask_timeout_s", d.ask_timeout_s)),
            headphone_form_factors=tuple(s.casefold() for s in strings("headphone_form_factors",
                                                                        d.headphone_form_factors)),
            headphone_devices=strings("headphone_devices", ()),
            capture_device=str(t.get("capture_device") or "").strip(),
            stt_model=str(t.get("call_stt_model") or d.stt_model).strip(),
            stt_fallback=str(t.get("call_stt_fallback") or d.stt_fallback).strip(),
            model_dir=str(t.get("model_dir") or "").strip(),
            languages=tuple(s.casefold() for s in strings("languages", ())),
            gate_dbfs=num("gate_dbfs", d.gate_dbfs), vad=bool(t.get("vad", d.vad)),
            vad_threshold=num("vad_threshold", d.vad_threshold), min_speech_s=num("min_speech_s", d.min_speech_s),
            chunk_min_s=num("chunk_min_s", d.chunk_min_s), chunk_max_s=max(2.0, num("chunk_max_s", d.chunk_max_s)),
            pause_s=num("pause_s", d.pause_s),
            stt_beam_size=max(1, int(num("stt_beam_size", d.stt_beam_size))),
            stt_temperatures=temps or d.stt_temperatures,
            stt_compression_ratio_threshold=num("stt_compression_ratio_threshold", d.stt_compression_ratio_threshold),
            stt_log_prob_threshold=num("stt_log_prob_threshold", d.stt_log_prob_threshold),
            stt_no_speech_threshold=num("stt_no_speech_threshold", d.stt_no_speech_threshold),
            stt_drop_no_speech=num("stt_drop_no_speech", d.stt_drop_no_speech),
            stt_drop_log_prob=num("stt_drop_log_prob", d.stt_drop_log_prob),
            stt_unclear_log_prob=num("stt_unclear_log_prob", d.stt_unclear_log_prob),
            stt_unclear_no_speech=num("stt_unclear_no_speech", d.stt_unclear_no_speech),
            stt_repeat_max_tokens=int(num("stt_repeat_max_tokens", d.stt_repeat_max_tokens)),
            stt_tokens_per_s=rates or d.stt_tokens_per_s,
            stt_chunk_limit_s=max(1.0, num("stt_chunk_limit_s", d.stt_chunk_limit_s)),
            stt_chunk_limit_rtf=max(0.0, num("stt_chunk_limit_rtf", d.stt_chunk_limit_rtf)),
            stt_vocab_tokens=max(0, int(num("stt_vocab_tokens", d.stt_vocab_tokens))),
            stt_vocab_days=max(0.0, num("stt_vocab_days", d.stt_vocab_days)),
            stt_vocab_languages=tuple(s.casefold() for s in strings("stt_vocab_languages", d.stt_vocab_languages)),
            stt_prompts=prompts,
        )

    def name_of(self, key: str | None, fallback: str = "") -> str:
        return next((v for k, v in self.names if k == key), None) or fallback or (key or "the call")

    def models_path(self) -> Path:
        if self.model_dir:
            return Path(os.path.expandvars(self.model_dir))
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Yuki" / "models" / "whisper"


# ---------------------------------------------------------------------------
# Content-free facts: who holds the microphone, what the output is
# ---------------------------------------------------------------------------


def mic_holders(exclude_paths: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """Apps using the microphone now (ConsentStore; ``activity_facts()["microphone"]``), minus ``exclude_paths``."""
    from yuki.memory.avsignals import _consent_users

    return [u for u in _consent_users("microphone")
            if not (u.get("path") and str(u["path"]).casefold() in exclude_paths)]


def headphones_now(cfg: CallConfig) -> tuple[bool, dict[str, Any]]:
    """Whether every default output is headphones (see :func:`yuki.perception.audio.output_is_headphones`)."""
    from yuki.perception import audio

    return audio.output_is_headphones(form_factors=cfg.headphone_form_factors,
                                      known_headphones=cfg.headphone_devices)


def capture_device_for(cfg: CallConfig, app: str | None) -> str | None:
    """The input device to record: ``capture_device``, else the one the call app records from, else the
    default communications input (friendly names; None = the system default)."""
    if cfg.capture_device:
        return cfg.capture_device
    from yuki.perception import audio

    try:
        if app:
            from yuki.memory.loopback import process_table

            # image names from one Toolhelp snapshot: no process is opened (a game's anti-cheat is never touched)
            pids = {pid for pid, _parent, image in process_table() if app_key(image) == app_key(app)}
            for s in audio.capture_sessions():
                if s["state"] == "active" and s["pid"] in pids:
                    return s["device"]
        comms = (audio.default_endpoints().get("capture") or {}).get("communications")
        return comms["name"] if comms else None
    except Exception:
        return None


class Gpu:
    """NVML (``nvml.dll`` from the driver) for GPU load and memory; silent when absent."""

    class _Util(ctypes.Structure):
        _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

    class _ProcUtil(ctypes.Structure):
        _fields_ = [("pid", ctypes.c_uint), ("timeStamp", ctypes.c_ulonglong), ("smUtil", ctypes.c_uint),
                    ("memUtil", ctypes.c_uint), ("encUtil", ctypes.c_uint), ("decUtil", ctypes.c_uint)]

    class _Mem(ctypes.Structure):
        _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]

    def __init__(self) -> None:
        self._dll = None
        self._handle = ctypes.c_void_p()
        try:
            dll = ctypes.WinDLL("nvml.dll")
            if dll.nvmlInit_v2() == 0 and dll.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(self._handle)) == 0:
                self._dll = dll
        except OSError:
            self._dll = None

    def snapshot(self) -> dict[str, Any] | None:
        if self._dll is None:
            return None
        util, mem = self._Util(), self._Mem()
        if self._dll.nvmlDeviceGetUtilizationRates(self._handle, ctypes.byref(util)) != 0:
            return None
        self._dll.nvmlDeviceGetMemoryInfo(self._handle, ctypes.byref(mem))
        return {"gpu_util": int(util.gpu), "vram_used_mb": round(mem.used / 2**20), "vram_total_mb": round(mem.total / 2**20)}

    def process_utilization(self, since_s: float = 10.0) -> dict[int, float] | None:
        """Each process's share of the GPU's compute (SM %, mean of NVML's samples over the last
        ``since_s``), ``{pid: percent}``; ``None`` when NVML or the driver cannot say. Content-free."""
        if self._dll is None:
            return None
        import time as _time

        last = ctypes.c_ulonglong(int((_time.time() - since_s) * 1e6))
        count = ctypes.c_uint(0)
        ret = self._dll.nvmlDeviceGetProcessUtilization(self._handle, None, ctypes.byref(count), last)
        if ret not in (0, 7) or not count.value:   # 7: NVML_ERROR_INSUFFICIENT_SIZE (the size query)
            return {} if ret in (0, 7) else None
        samples = (self._ProcUtil * count.value)()
        if self._dll.nvmlDeviceGetProcessUtilization(self._handle, samples, ctypes.byref(count), last) != 0:
            return None
        acc: dict[int, list[int]] = {}
        for s in samples[:count.value]:
            acc.setdefault(int(s.pid), []).append(int(s.smUtil))
        return {pid: sum(v) / len(v) for pid, v in acc.items()}


# ---------------------------------------------------------------------------
# Audio: sources (in memory only) and the energy-gated chunker
# ---------------------------------------------------------------------------


class MicSource:
    """Shared-mode WASAPI capture of one input device as 16 kHz mono float32 blocks.

    Blocks go to an in-memory queue as ``(wall time of the first sample,
    samples)``; nothing is written anywhere. Shared mode only: the device
    stays usable by the call app and everything else at the same time. Of a
    multi-channel input the loudest channel of each block is kept (an audio
    interface's mic is usually on one input).
    """

    def __init__(self, device: str | None = None, *, block_s: float = 0.1) -> None:
        self.device = device
        self.block_s = block_s
        self._q: queue.Queue = queue.Queue()
        self._stream: Any = None
        self._resampler: Any = None
        self._rate = RATE
        self.device_used: str | None = None
        self.opened_as: str | None = None
        self.overflows = 0

    def _index(self, sd: Any) -> int | None:
        apis = [i for i, a in enumerate(sd.query_hostapis()) if "wasapi" in a["name"].lower()]
        if not apis:
            return None
        api = apis[0]
        devices = [(i, d) for i, d in enumerate(sd.query_devices()) if d["hostapi"] == api and d["max_input_channels"] > 0]
        if self.device:
            want = self.device.casefold()
            for i, d in devices:
                if d["name"].casefold() == want:
                    return i
            for i, d in devices:
                if want in d["name"].casefold() or d["name"].casefold() in want:
                    return i
        default = sd.query_hostapis(api)["default_input_device"]
        return default if default is not None and default >= 0 else None

    def start(self) -> None:
        import sounddevice as sd

        index = self._index(sd)
        info = sd.query_devices(index if index is not None else sd.default.device[0])
        self.device_used = info["name"]
        channels = max(1, min(2, int(info["max_input_channels"])))

        def callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
            if status and status.input_overflow:
                self.overflows += 1
            self._q.put((time.time() - frames / float(self._rate), indata.copy()))

        try:
            self._rate = RATE
            self._stream = sd.InputStream(device=index, samplerate=RATE, channels=channels, dtype="float32",
                                          blocksize=int(RATE * self.block_s), callback=callback,
                                          extra_settings=sd.WasapiSettings(auto_convert=True))
            self.opened_as = "wasapi_shared_auto_convert"
        except Exception:
            import av

            self._rate = int(info["default_samplerate"])
            self._resampler = av.AudioResampler(format="flt", layout="mono", rate=RATE)
            self._stream = sd.InputStream(device=index, samplerate=self._rate, channels=channels, dtype="float32",
                                          blocksize=int(self._rate * self.block_s), callback=callback)
            self.opened_as = "wasapi_shared_native_resampled"
        self._stream.start()

    def _mono(self, block: np.ndarray) -> np.ndarray:
        if block.ndim == 2 and block.shape[1] > 1:
            loudest = int(np.argmax(np.sqrt(np.mean(block.astype(np.float64) ** 2, axis=0))))
            block = block[:, loudest]
        mono = np.ascontiguousarray(block.reshape(-1), dtype=np.float32)
        if self._resampler is None:
            return mono
        import av

        frame = av.AudioFrame.from_ndarray(mono.reshape(1, -1), format="flt", layout="mono")
        frame.sample_rate = self._rate
        out = [f.to_ndarray().reshape(-1) for f in self._resampler.resample(frame)]
        return np.concatenate(out).astype(np.float32) if out else np.zeros(0, np.float32)

    def read(self, timeout_s: float) -> tuple[float, np.ndarray] | None:
        try:
            at, block = self._q.get(timeout=timeout_s)
        except queue.Empty:
            return None
        return at, self._mono(block)

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        while not self._q.empty():   # drop what was not read: nothing is kept
            try:
                self._q.get_nowait()
            except queue.Empty:
                break


class ArraySource:
    """An in-memory recording played as if live (offline checks with synthetic WAVs): the same
    ``start``/``read``/``stop`` as :class:`MicSource`, ``realtime=False`` delivers as fast as read."""

    def __init__(self, audio: np.ndarray, *, start_at: float | None = None, block_s: float = 0.1,
                 realtime: bool = False) -> None:
        self.audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        self.start_at = time.time() if start_at is None else float(start_at)
        self.block = int(RATE * block_s)
        self.realtime = realtime
        self.pos = 0
        self.device_used = "array"
        self.opened_as = "array"
        self.overflows = 0
        self.done = threading.Event()
        self._idle = threading.Event()

    def start(self) -> None:
        self._t0 = time.monotonic()

    def read(self, timeout_s: float) -> tuple[float, np.ndarray] | None:
        if self.pos >= len(self.audio):
            self.done.set()
            self._idle.wait(timeout_s)   # like an empty queue.get(timeout): nothing more is coming
            return None
        if self.realtime:
            due = self._t0 + self.pos / RATE
            delay = due - time.monotonic()
            if delay > timeout_s:
                return None
            if delay > 0:
                threading.Event().wait(delay)
        block = self.audio[self.pos:self.pos + self.block]
        at = self.start_at + self.pos / RATE
        self.pos += len(block)
        return at, block

    def stop(self) -> None:
        pass


@dataclass
class Chunk:
    audio: np.ndarray
    start_at: float
    speech_s: float

    @property
    def seconds(self) -> float:
        return len(self.audio) / RATE


def silero_path() -> Path | None:
    """Silero VAD v6 (ONNX, 1.2 MB) as shipped inside faster-whisper, found without importing it."""
    import importlib.util

    spec = importlib.util.find_spec("faster_whisper")
    for base in (spec.submodule_search_locations or []) if spec else []:
        path = Path(base) / "assets" / "silero_vad_v6.onnx"
        if path.exists():
            return path
    return None


class SileroVad:
    """Silero VAD (ONNX, CPU, one thread, no arena), streamed window by window with its state carried over.

    ``probs(windows)`` takes consecutive 512-sample windows ``(n, 512)`` and
    returns their speech probabilities; the 64-sample context and the LSTM
    state run on across calls, as in faster-whisper's own use of the model.
    """

    CONTEXT = 64

    def __init__(self, path: Path | None = None) -> None:
        import onnxruntime as ort

        path = path or silero_path()
        if path is None:
            raise FileNotFoundError("silero_vad_v6.onnx not found (faster-whisper assets)")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.enable_cpu_mem_arena = False
        opts.log_severity_level = 4
        self.session = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
        self.reset()
        self.windows = 0
        self.cpu_s = 0.0

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._tail = np.zeros(self.CONTEXT, dtype=np.float32)

    def probs(self, windows: np.ndarray) -> np.ndarray:
        w = np.asarray(windows, dtype=np.float32).reshape(-1, FRAME)
        ctx = np.concatenate([self._tail[None, :], w[:-1, -self.CONTEXT:]], axis=0)
        batch = np.concatenate([ctx, w], axis=1)
        t0 = time.thread_time()
        out, self._h, self._c = self.session.run(None, {"input": batch, "h": self._h, "c": self._c})
        self.cpu_s += time.thread_time() - t0
        self._tail = w[-1, -self.CONTEXT:].copy()
        self.windows += len(w)
        return np.asarray(out, dtype=np.float32).reshape(-1)[: len(w)]


class Chunker:
    """Speech gate + rolling chunks: speech is cut at pauses, silence never becomes a chunk.

    32 ms frames under ``gate_dbfs`` are silence (cheap: no model runs on
    them); the rest are speech only when Silero VAD says so (``vad``, with
    hysteresis: ``vad_threshold`` to start, 0.15 lower to go on), or, without
    a VAD, whenever they are loud enough. While nothing voiced has
    come, silence is dropped as it arrives (a short pre-roll is kept). A chunk
    ends at a pause of ``pause_s`` once it is ``chunk_min_s`` long, after a
    pause of ``4 * pause_s`` whatever its length (a short "yes"), or at
    ``chunk_max_s``. A chunk with less than ``min_speech_s`` of voiced frames
    is counted and dropped, never transcribed.

    With ``breaks`` (an app track, whose blocks carry the time Windows stamped
    on the audio: :class:`yuki.memory.loopback.Restamper`), a block that starts
    more than :data:`BREAK_S` after the previous one ended is a break in the
    audio (nothing was delivered while the app was silent): the chunk in
    progress is cut there, so a chunk's samples are always one stretch of time
    and its ``start_at`` stays true. A microphone's blocks are stamped when they
    arrive (late under load, while the audio itself is unbroken), so it never
    breaks on them.
    """

    #: A jump in an app track's block times larger than this is a break.
    BREAK_S = 0.25

    def __init__(self, cfg: CallConfig, vad: SileroVad | None = None, *, breaks: bool = False) -> None:
        self.cfg = cfg
        self.vad = vad
        self.detect_breaks = breaks
        self._in_speech = False
        self.frame = FRAME
        self._buf: list[np.ndarray] = []
        self._voiced: list[bool] = []
        self._start_at: float | None = None
        self._rest = np.zeros(0, np.float32)
        self._rest_at: float | None = None
        self._next_at: float | None = None
        self.silent_dropped = 0
        self.silence_s = 0.0
        self.breaks = 0

    def _break(self) -> list[Chunk]:
        """The audio jumped: cut what is voiced, drop the silence and the partial frame before the jump."""
        self.breaks += 1
        out: list[Chunk] = []
        if self._buf and any(self._voiced):
            chunk = self._cut(keep_tail=0)
            if chunk is not None:
                out.append(chunk)
        else:
            self.silence_s += len(self._buf) * FRAME_S
            self._buf, self._voiced, self._start_at = [], [], None
        self._rest, self._rest_at = np.zeros(0, np.float32), None
        self._in_speech = False
        return out

    def _frames(self, at: float, samples: np.ndarray) -> list[tuple[float, np.ndarray]]:
        if self._rest.size:
            samples = np.concatenate([self._rest, samples])
            at = self._rest_at if self._rest_at is not None else at
        n = len(samples) // self.frame
        frames = [(at + i * FRAME_S, samples[i * self.frame:(i + 1) * self.frame]) for i in range(n)]
        self._rest = samples[n * self.frame:]
        self._rest_at = at + n * FRAME_S
        return frames

    def feed(self, at: float, samples: np.ndarray) -> list[Chunk]:
        out: list[Chunk] = []
        preroll = max(1, int(0.3 / FRAME_S))
        if self.detect_breaks and self._next_at is not None and at - self._next_at > self.BREAK_S:
            out += self._break()
        self._next_at = at + len(samples) / RATE
        frames = self._frames(at, samples)
        for t, frame, voiced in zip((t for t, _ in frames), (f for _, f in frames), self._voicing(frames)):
            if not self._buf:
                self._start_at = t
            self._buf.append(frame)
            self._voiced.append(voiced)
            if not any(self._voiced):
                if len(self._buf) > preroll:           # silence before speech: dropped as it comes
                    self._buf.pop(0)
                    self._voiced.pop(0)
                    self._start_at = (self._start_at or t) + FRAME_S
                    self.silence_s += FRAME_S
                continue
            length = len(self._buf) * FRAME_S
            trailing = 0
            for v in reversed(self._voiced):
                if v:
                    break
                trailing += 1
            pause = trailing * FRAME_S
            if (length >= self.cfg.chunk_max_s or (length >= self.cfg.chunk_min_s and pause >= self.cfg.pause_s)
                    or pause >= 4 * self.cfg.pause_s):
                chunk = self._cut(keep_tail=min(trailing, preroll))
                if chunk is not None:
                    out.append(chunk)
        return out

    def _voicing(self, frames: list[tuple[float, np.ndarray]]) -> list[bool]:
        if not frames:
            return []
        block = np.stack([f for _, f in frames]).astype(np.float64)
        loud = 20.0 * np.log10(np.sqrt(np.mean(block ** 2, axis=1)) + 1e-12) >= self.cfg.gate_dbfs
        if self.vad is None:
            return [bool(x) for x in loud]
        if not loud.any():               # nothing above the floor: the model is not run
            self._in_speech = False
            return [False] * len(frames)
        probs = self.vad.probs(block.astype(np.float32))
        out = []
        for is_loud, p in zip(loud, probs):
            limit = self.cfg.vad_threshold - (0.15 if self._in_speech else 0.0)
            self._in_speech = bool(is_loud and p >= limit)
            out.append(self._in_speech)
        return out

    def _cut(self, keep_tail: int) -> Chunk | None:
        trailing = 0
        for v in reversed(self._voiced):
            if v:
                break
            trailing += 1
        end = len(self._buf) - max(0, trailing - keep_tail)
        audio = np.concatenate(self._buf[:end]) if end > 0 else np.zeros(0, np.float32)
        speech = sum(self._voiced[:end]) * FRAME_S
        start = self._start_at or time.time()
        self.silence_s += (len(self._buf) - end) * FRAME_S
        self._buf, self._voiced, self._start_at = [], [], None
        if speech < self.cfg.min_speech_s:
            self.silent_dropped += 1
            return None
        return Chunk(audio=audio, start_at=start, speech_s=speech)

    def flush(self) -> list[Chunk]:
        if not self._buf or not any(self._voiced):
            self._buf, self._voiced, self._start_at = [], [], None
            return []
        chunk = self._cut(keep_tail=0)
        return [chunk] if chunk is not None else []


# ---------------------------------------------------------------------------
# Speech to text: local backends, pluggable by name (call_stt_model)
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    start: float
    end: float
    text: str
    lang: str | None = None
    #: Whisper's own signals for the decode this segment came from.
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    compression_ratio: float = 0.0
    tokens: int = 0
    #: Recognised with low confidence (see :func:`screen_segments`); the journal is told.
    unclear: bool = False


def _same_words(text: str) -> str:
    """Casefolded words without punctuation, for "is this the same phrase again"."""
    import unicodedata

    kept = "".join(" " if unicodedata.category(ch)[0] in "PS" else ch for ch in text.casefold())
    return " ".join(kept.split())


def screen_segments(segments: Sequence[Segment], cfg: CallConfig,
                    previous: str | None = None) -> tuple[list[Segment], dict[str, int], str | None]:
    """Whisper's own signals decide what of a chunk is kept: ``(kept, dropped counts by reason, last phrase)``.

    * ``no_speech``: Whisper's no-speech probability over ``stt_drop_no_speech`` while its average
      log-probability is under ``stt_drop_log_prob`` - it heard no speech and was unsure of its words.
    * ``repetitive``: compression ratio over ``stt_compression_ratio_threshold`` even after the
      temperature fallback (a decoding loop: "भाग भाग भाग ...").
    * ``repeat``: the same phrase as the previous segment (``previous`` carries it across the chunks
      of a call), unclear and at most ``stt_repeat_max_tokens`` tokens: the loop Whisper falls into on
      breathing, clicks and game sound ("कर दो" six times running).

    A kept segment is marked ``unclear`` when its log-probability is under ``stt_unclear_log_prob`` or
    its no-speech probability over ``stt_unclear_no_speech``.
    """
    kept: list[Segment] = []
    dropped: dict[str, int] = {}
    last = previous
    for s in segments:
        phrase = _same_words(s.text)
        reason = None
        if not phrase:
            reason = "empty"
        elif s.no_speech_prob > cfg.stt_drop_no_speech and s.avg_logprob < cfg.stt_drop_log_prob:
            reason = "no_speech"
        elif s.compression_ratio > cfg.stt_compression_ratio_threshold:
            reason = "repetitive"
        else:
            s.unclear = s.avg_logprob < cfg.stt_unclear_log_prob or s.no_speech_prob > cfg.stt_unclear_no_speech
            if phrase == last and s.unclear and s.tokens <= cfg.stt_repeat_max_tokens:
                reason = "repeat"
        if reason is not None:
            dropped[reason] = dropped.get(reason, 0) + 1
        else:
            kept.append(s)
        if phrase:
            last = phrase
    return kept, dropped, last


def heard_by(spans: Sequence[dict[str, Any]], at: float, end: float, *, overlap: float = 0.3) -> list[str] | None:
    """The apps that were taking the microphone while the user said a line ``[at, end]`` (display names, sorted).

    ``spans``: :meth:`Store.mic_spans_between` rows - ``capture`` spans (an app's capture session was active)
    and ``watch`` spans (the watch ran). An app heard the line when its capture spans cover at least
    ``overlap`` of it (at least 0.1 s). ``None`` when no watch covered the line (not measured: unknown, never
    "nobody"); ``[]`` when it was watched and no app took the microphone (the user spoke to the room or to Yuki).
    A game that opens its microphone only while push-to-talk is held is in the list only for what was said
    with the key down (measured from Windows' session state; not checked live yet).
    """
    end = max(float(end), float(at))
    length = max(end - float(at), 0.05)

    def covered(rows: Iterable[dict[str, Any]]) -> float:
        """Seconds of the line inside the union of these spans (an app with two processes counts once)."""
        parts = sorted((max(float(s["start"]), float(at)), min(s["end"] if s.get("end") is not None else end, end))
                       for s in rows)
        total, reach = 0.0, float(at)
        for lo, hi in parts:
            lo = max(lo, reach)
            if hi > lo:
                total += hi - lo
                reach = hi
        return total

    if covered(s for s in spans if s.get("kind") == "watch") < 0.5 * length:
        return None
    by_app: dict[str, list[dict[str, Any]]] = {}
    for s in spans:
        if s.get("kind") == "capture":
            by_app.setdefault(str(s.get("app") or s.get("process") or "?"), []).append(s)
    need = max(0.1, overlap * length)
    return sorted(app for app, rows in by_app.items() if covered(rows) >= need)


def vocabulary_hint(subjects: Iterable[str], count_tokens: Callable[[str], int], budget_tokens: int,
                    *, min_facts: int = 2) -> str:
    """Names and terms from memory for Whisper's hint (``hotwords``): ``"Claude, Corvana, ..."``.

    ``subjects``: what memory's models named as the subject of each fact (portrait and conversation
    facts, the questions' unknowns, the names the user goes by on screen) - who and what the facts are
    about, as the models wrote them. A word of them counts when it is the subject of at least
    ``min_facts`` facts (it recurs in the user's life) and Whisper's own tokenizer needs two tokens or
    more for it in every casing (Whisper does not know it as one word; a common word or one it knows
    needs no hint - the measured filter, no capital-letter guess about what a name is). Words are ranked
    by facts x (tokens - 1) and taken until the hint would pass ``budget_tokens``: a short hint, so that it
    guides spelling without being heard where nobody said it.
    """
    if budget_tokens <= 0:
        return ""
    counts: dict[str, int] = {}
    forms: dict[str, dict[str, int]] = {}
    for text in subjects:
        seen: set[str] = set()
        for raw in (text or "").split():
            word = ""
            for ch in raw + " ":
                if ch.isalpha():
                    word += ch
                    continue
                if len(word) > 1:
                    seen.add(word)
                word = ""
        for word in seen:
            key = word.casefold()
            counts[key] = counts.get(key, 0) + 1
            forms.setdefault(key, {})[word] = forms.setdefault(key, {}).get(word, 0) + 1
    scored = []
    for key, n in counts.items():
        if n < min_facts:
            continue
        form = max(forms[key].items(), key=lambda kv: kv[1])[0]
        cost = min(count_tokens(" " + form), count_tokens(" " + form.lower()), count_tokens(" " + form.capitalize()))
        if cost >= 2:
            scored.append((n * (cost - 1), n, form))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    chosen: list[str] = []
    for _score, _n, form in scored:
        if count_tokens(" " + ", ".join([*chosen, form])) > budget_tokens:
            continue
        chosen.append(form)
    return ", ".join(chosen)


class ChunkTimeout(Exception):
    """A chunk ran past its time limit (:class:`_DecodeGuard`)."""


class _DecodeGuard:
    """Stands in for CTranslate2's Whisper inside faster-whisper: counts ``generate`` calls and tokens, and
    refuses to start another once the chunk's time is up. One ``generate`` is bounded by ``max_new_tokens``,
    so a chunk never runs much past its limit (a temperature-fallback loop on noise used to take 300 s)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.deadline: float | None = None
        self.calls = 0
        self.tokens = 0

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        if self.deadline is not None and time.perf_counter() > self.deadline:
            raise ChunkTimeout()
        out = self._inner.generate(*args, **kwargs)
        self.calls += 1
        self.tokens += sum(len(seq) for r in out for seq in r.sequences_ids)
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _add_cuda_dirs() -> list[str]:
    """Make cuBLAS/cuDNN findable for CTranslate2 (:func:`yuki.core.cuda.add_dll_dirs`: pip nvidia-* wheels,
    the frozen build, ``%LOCALAPPDATA%/Yuki/cuda``, the CUDA toolkit), on ``PATH`` too."""
    from yuki.core.cuda import add_dll_dirs

    return add_dll_dirs()


class FasterWhisperBackend:
    """``faster-whisper:<model>``: CTranslate2 Whisper, int8 (int8_float16 on CUDA)."""

    #: Whisper's decoder context (prompt + generated tokens).
    MAX_LENGTH = 448

    def __init__(self, model: str, *, device: str, model_dir: Path, languages: Sequence[str] = (),
                 cpu_threads: int = 4, options: CallConfig | None = None) -> None:
        self.model_name = model
        self.device = device
        self.model_dir = Path(model_dir)
        self.languages = tuple(languages)
        self.cpu_threads = cpu_threads
        self.options = options or CallConfig()
        self.model: Any = None
        self.guard: _DecodeGuard | None = None
        self.load_ms = 0.0

    @property
    def name(self) -> str:
        return f"faster-whisper:{self.model_name}"

    def load(self) -> None:
        from faster_whisper import WhisperModel

        t0 = time.perf_counter()
        if self.device == "cuda":
            _add_cuda_dirs()
        compute = "int8_float16" if self.device == "cuda" else "int8"
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.model = WhisperModel(self.model_name, device=self.device, compute_type=compute,
                                  download_root=str(self.model_dir), cpu_threads=self.cpu_threads)
        # one tiny pass: a missing CUDA library shows here, not mid-call
        list(self.model.transcribe(np.zeros(RATE // 2, np.float32), language="en", beam_size=1)[0])
        self.guard = _DecodeGuard(self.model.model)
        self.model.model = self.guard
        self.load_ms = (time.perf_counter() - t0) * 1000.0

    def count_tokens(self, text: str) -> int:
        """Whisper tokens of ``text`` (its own tokenizer)."""
        return len(self.model.hf_tokenizer.encode(text, add_special_tokens=False).ids)

    def pick_language(self, audio: np.ndarray) -> tuple[str | None, float]:
        """The most likely language by Whisper's language detection (on the speech only), among ``languages``
        when some are configured; None = let Whisper choose (nothing to detect on)."""
        try:
            top, top_p, all_probs = self.model.detect_language(audio, vad_filter=True)
        except Exception:   # e.g. nothing left after its VAD
            return None, 0.0
        if not self.languages:
            return top, float(top_p)
        probs = dict(all_probs or [])
        best = max(self.languages, key=lambda code: probs.get(code, 0.0))
        return best, float(probs.get(best, 0.0))

    def transcribe(self, audio: np.ndarray, *, hint: str = "") -> tuple[list[Segment], dict[str, Any]]:
        """Every segment Whisper decodes, with its own signals (:func:`screen_segments` decides what is kept).

        The language is detected first and the chunk decoded once in it (it used to be decoded twice when
        Whisper's first guess was not one of ``languages``). ``hint`` (memory's names and terms) goes in as
        faster-whisper's ``hotwords``, the language's style example from ``stt_prompts`` as the previous
        text. Each decode may produce at most ``stt_tokens_per_s`` x the chunk's seconds, and no new decode
        starts past the chunk's time limit (``info["timed_out"]``; what was decoded before is kept).
        """
        o = self.options
        seconds = len(audio) / RATE
        lang, lang_prob = self.pick_language(audio)
        prompt = o.prompt_for(lang)
        if o.stt_vocab_languages and lang not in o.stt_vocab_languages:
            hint = ""
        used = (self.count_tokens(" " + prompt) if prompt else 0) + (self.count_tokens(" " + hint) if hint else 0)
        room = self.MAX_LENGTH - used - 8          # the prompt's special tokens and the start sequence
        max_new = int(max(1, min(room, 16 + o.tokens_per_s(lang) * seconds)))
        # beam search on the GPU; greedy on the CPU, where beam search costs ~1.6x for the same words (measured)
        kwargs = dict(beam_size=o.stt_beam_size if self.device == "cuda" else 1, vad_filter=True,
                      condition_on_previous_text=False, task="transcribe", temperature=list(o.stt_temperatures),
                      compression_ratio_threshold=o.stt_compression_ratio_threshold,
                      log_prob_threshold=o.stt_log_prob_threshold, no_speech_threshold=o.stt_no_speech_threshold,
                      initial_prompt=prompt or None, hotwords=hint or None, max_new_tokens=max_new)
        guard = self.guard
        calls0, tokens0 = (guard.calls, guard.tokens) if guard else (0, 0)
        if guard is not None:
            guard.deadline = time.perf_counter() + o.stt_chunk_limit_s + o.stt_chunk_limit_rtf * seconds
        out: list[Segment] = []
        timed_out = False
        try:
            segments, info = self.model.transcribe(audio, language=lang, **kwargs)
            lang = lang or info.language
            for s in segments:
                out.append(Segment(float(s.start), float(s.end), (s.text or "").strip(), lang,
                                   avg_logprob=float(s.avg_logprob), no_speech_prob=float(s.no_speech_prob),
                                   compression_ratio=float(s.compression_ratio), tokens=len(s.tokens)))
        except ChunkTimeout:
            timed_out = True
        finally:
            if guard is not None:
                guard.deadline = None
        return out, {"lang": lang, "lang_prob": round(lang_prob, 3), "timed_out": timed_out,
                     "max_new_tokens": max_new, "decodes": (guard.calls - calls0) if guard else None,
                     "tokens": (guard.tokens - tokens0) if guard else None}

    def close(self) -> None:
        self.model = None
        import gc

        gc.collect()


def make_backend(spec: str, *, device: str, model_dir: Path, languages: Sequence[str] = (),
                 options: CallConfig | None = None) -> Any:
    """A speech-to-text backend by name: ``faster-whisper:<model>`` (``large-v3``, ``large-v3-turbo``, ``small``, ...).

    Raises ``ValueError`` for an unknown backend. (Handy's Nemotron 3.5 GGUF
    needs transcribe.cpp, which has no Python binding, and does not cover
    Kannada, Tamil or Telugu: docs/MEMORY.md.)
    """
    kind, _, model = (spec or "").partition(":")
    if kind == "faster-whisper" and model:
        return FasterWhisperBackend(model, device=device, model_dir=model_dir, languages=languages, options=options)
    raise ValueError(f"unknown speech-to-text backend {spec!r}")


def cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def load_backend(cfg: CallConfig, log: Callable[..., Any] | None = None) -> Any:
    """``call_stt_model`` on CUDA when there is a GPU, else (or when that fails) ``call_stt_fallback`` on the CPU."""
    attempts = []
    if cuda_available():
        attempts.append((cfg.stt_model, "cuda"))
    attempts.append((cfg.stt_fallback or cfg.stt_model, "cpu"))
    last: Exception | None = None
    for spec, device in attempts:
        try:
            backend = make_backend(spec, device=device, model_dir=cfg.models_path(), languages=cfg.languages,
                                   options=cfg)
            backend.load()
            return backend
        except Exception as exc:
            last = exc
            if log is not None:
                log("call_stt_load_failed", model=spec, device=device, error=f"{type(exc).__name__}: {exc}")
    raise RuntimeError(f"no speech-to-text backend could load: {last}")


# ---------------------------------------------------------------------------
# One recording: source -> chunker -> backend -> utterances -> store
# ---------------------------------------------------------------------------


def encode_speech(audio: np.ndarray, *, bitrate: int = 24_000) -> tuple[bytes, str]:
    """16 kHz mono float32 -> ``(bytes, codec)``: Opus in Ogg (PyAV's libopus, in memory), else int16 + zlib.

    Never touches the disk: the caller encrypts the bytes before anything is written.
    """
    pcm = (np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0) * 32767.0).astype(np.int16)
    try:
        import io

        import av

        buf = io.BytesIO()
        with av.open(buf, "w", format="ogg") as out:
            stream = out.add_stream("libopus", rate=RATE)
            stream.bit_rate = int(bitrate)
            stream.layout = "mono"
            frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = RATE
            for packet in stream.encode(frame):
                out.mux(packet)
            for packet in stream.encode(None):
                out.mux(packet)
        return buf.getvalue(), "opus"
    except Exception:
        import zlib

        return zlib.compress(pcm.tobytes(), 6), "pcm16-zlib"


def decode_speech(payload: bytes, codec: str) -> np.ndarray:
    """:func:`encode_speech`'s bytes back to 16 kHz mono float32 (in memory)."""
    if codec == "pcm16-zlib":
        import zlib

        return np.frombuffer(zlib.decompress(payload), dtype=np.int16).astype(np.float32) / 32768.0
    import io

    from faster_whisper.audio import decode_audio

    return np.asarray(decode_audio(io.BytesIO(payload), sampling_rate=RATE), dtype=np.float32)


@dataclass
class RecordingStats:
    chunks: int = 0
    chunks_silent: int = 0
    audio_s: float = 0.0
    speech_s: float = 0.0
    bytes: int = 0
    encode_ms: list[float] = field(default_factory=list)
    cpu_s: float = 0.0
    errors: int = 0


@dataclass(frozen=True)
class Track:
    """Where a recording's audio comes from, as its chunks are labelled (``call_chunks.side`` / ``track_*``).

    ``side`` "user": the microphone (``app``/``kind``/``call_id``: the voice session's row it is recorded for);
    "others": an app's own audio (``app``/``process``/``kind``/``call_id``: that app and its own row).
    """

    side: str = "user"
    app: str | None = None
    process: str | None = None
    kind: str = "call"
    call_id: int | None = None

    @property
    def label(self) -> str:
        return "mic" if self.side != "others" else f"{self.kind}:{self.process or self.app or '?'}"


class RecordingSession:
    """Records one track of a voice session on its own thread: gate, compress, encrypt, spool. No speech-to-text.

    The track is the user's microphone (``track.side`` "user", the default) or an app's own audio ("others",
    process loopback). :meth:`pause` closes the source (output left headphones; the app let go), :meth:`stop`
    ends it. Chunks the gate passes are Opus-compressed in memory, encrypted with the store's key and written to
    the spool under row ``call_id`` (:meth:`Store.write_spool`); the deferred transcriber
    (:mod:`yuki.memory.transcribe`) turns them into text later, when the PC is free, and deletes them. Several
    sessions may write into one row at once (the microphone and the call app's audio): ``seq_fn`` hands out the
    row's chunk numbers. Only the microphone's session keeps the row's counters (``chunks``, ``speech_s``: the
    user's own speech).
    """

    def __init__(self, store: Store, *, call_id: int, app: str, process: str | None, cfg: CallConfig,
                 source_factory: Callable[[], Any], log: Callable[..., Any], start_seq: int = 0,
                 base: RecordingStats | None = None, track: Track | None = None,
                 seq_fn: Callable[[], int] | None = None) -> None:
        self.store = store
        self.call_id = call_id
        self.app = app
        self.process = process
        self.cfg = cfg
        self.track = track or Track(side="user", app=app, process=process, call_id=call_id)
        self._seq_fn = seq_fn
        self._source_factory = source_factory
        self.log = log
        vad = None
        if cfg.vad:
            try:
                vad = SileroVad()
            except Exception as exc:   # the energy gate alone still keeps silence out
                log("call_vad_unavailable", call_id=call_id, track=self.track.label, error=f"{type(exc).__name__}: {exc}")
        self.chunker = Chunker(cfg, vad, breaks=self.track.side == "others")
        #: A call adopted after a restart goes on from its row: its counts (``base``) and its last spool chunk
        #: number (``start_seq``), so the row's totals and the spool's file names carry on.
        base = base or RecordingStats()
        self.stats = RecordingStats(chunks=base.chunks, chunks_silent=base.chunks_silent, audio_s=base.audio_s,
                                    speech_s=base.speech_s)
        self._silent_base = base.chunks_silent
        self._seq = int(start_seq)
        self._source: Any = None
        self.last_source: Any = None
        self._source_lock = threading.Lock()
        self._stop = threading.Event()
        self._drain = threading.Event()
        self._thread: threading.Thread | None = None
        self.started_at = time.time()
        self.device: str | None = None
        self.opened_as: str | None = None
        self.stop_reason: str | None = None
        self.backend = None   # no speech-to-text during a call
        #: Liveness, measured (``time.time()``): since when the source is open (None while paused), the last read
        #: that brought any audio (silence included), and the last chunk spooled. The worker's stall flag reads them.
        self.listening_since: float | None = None
        self.last_audio_at: float | None = None
        self.last_kept_at: float | None = None

    # -- control -------------------------------------------------------------

    def start(self) -> None:
        self.resume()
        name = "yuki-memory-call-rec" if self.track.side != "others" else "yuki-memory-call-track"
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def listening(self) -> bool:
        return self._source is not None

    def resume(self) -> None:
        with self._source_lock:
            if self._source is None:
                source = self._source_factory()
                source.start()
                self.device = getattr(source, "device_used", None)
                self.opened_as = getattr(source, "opened_as", None)
                self._source = source
                self.listening_since = time.time()

    def pause(self) -> None:
        """Close the source (the microphone, or the app's audio) now; what was heard so far is still spooled."""
        with self._source_lock:
            source, self._source = self._source, None
            self.listening_since = None
        if source is not None:
            source.stop()
            self.last_source = source   # its counters (an app track's packets and gaps) for the stop log

    def stop(self, reason: str, *, timeout_s: float = 30.0) -> RecordingStats:
        """Close the microphone, spool what is left; returns the stats."""
        self.stop_reason = reason
        self.pause()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
        return self.stats

    def wait_drained(self, timeout_s: float) -> bool:
        """Offline checks: wait until an :class:`ArraySource` was read to its end."""
        return self._drain.wait(timeout_s)

    @property
    def source(self) -> Any:
        """The open source (None while paused): an app track's :class:`~yuki.memory.loopback.LoopbackSource` says
        whether it is ``ready`` and ``alive``."""
        return self._source

    # -- thread --------------------------------------------------------------

    def _run(self) -> None:
        c0 = time.thread_time()
        try:
            while not self._stop.is_set():
                source = self._source
                if source is None:
                    self._stop.wait(0.25)
                    continue
                got = source.read(0.25)
                if got is None:
                    if getattr(source, "done", None) is not None and source.done.is_set():
                        for chunk in self.chunker.flush():
                            self._spool(chunk)
                        self._drain.set()
                    continue
                at, samples = got
                self.last_audio_at = time.time()
                for chunk in self.chunker.feed(at, samples):
                    self._spool(chunk)
            for chunk in self.chunker.flush():
                self._spool(chunk)
        except Exception as exc:
            self.stats.errors += 1
            self.log("error", where="calls.recording", error=f"{type(exc).__name__}: {exc}",
                     traceback=traceback.format_exc())
        finally:
            self.stats.cpu_s = time.thread_time() - c0
            self.stats.chunks_silent = self._silent_base + self.chunker.silent_dropped
            self._update_row()

    def _spool(self, chunk: Chunk) -> None:
        t0 = time.perf_counter()
        tr = self.track
        try:
            payload, codec = encode_speech(chunk.audio)
            self._seq = self._seq_fn() if self._seq_fn is not None else self._seq + 1
            self.store.write_spool(self.call_id, self._seq, start_at=chunk.start_at, audio_s=chunk.seconds,
                                   speech_s=chunk.speech_s, payload=payload, codec=codec, side=tr.side,
                                   track_app=tr.app, track_process=tr.process, track_kind=tr.kind,
                                   track_call_id=tr.call_id)
        except Exception as exc:
            self.stats.errors += 1
            self.log("error", where="calls.spool", track=tr.label, error=f"{type(exc).__name__}: {exc}")
            return
        ms = (time.perf_counter() - t0) * 1000.0
        self.last_kept_at = time.time()
        st = self.stats
        st.chunks += 1
        st.audio_s += chunk.seconds
        st.speech_s += chunk.speech_s
        st.bytes += len(payload)
        st.encode_ms.append(ms)
        st.chunks_silent = self._silent_base + self.chunker.silent_dropped
        vad = self.chunker.vad
        self.log("call_chunk", call_id=self.call_id, track=tr.label, side=tr.side, audio_s=round(chunk.seconds, 2),
                 speech_s=round(chunk.speech_s, 2), bytes=len(payload), codec=codec, ms=round(ms, 1),
                 silent_dropped=st.chunks_silent, vad_cpu_s=round(vad.cpu_s, 2) if vad is not None else None)
        self._update_row()
        del chunk   # the audio goes with it; only the encrypted file is left

    def _update_row(self) -> None:
        if self.track.side == "others":   # the row's counters are the user's own speech
            return
        st = self.stats
        try:
            self.store.update_call(self.call_id, chunks=st.chunks, chunks_silent=st.chunks_silent,
                                   speech_s=round(st.speech_s, 2), audio_s=round(st.audio_s, 2))
        except Exception:
            pass


def summarize(stats: RecordingStats) -> dict[str, Any]:
    ms = sorted(stats.encode_ms)

    def pct(p: float) -> float | None:
        return round(ms[min(len(ms) - 1, int(p * len(ms)))], 1) if ms else None

    return {"chunks": stats.chunks, "chunks_silent": stats.chunks_silent, "audio_s": round(stats.audio_s, 1),
            "speech_s": round(stats.speech_s, 1), "spooled_bytes": stats.bytes,
            "encode_ms": {"p50": pct(0.5), "max": pct(1.0)}, "cpu_s": round(stats.cpu_s, 2), "errors": stats.errors}



# ---------------------------------------------------------------------------
# The worker: detect, ask, check headphones, record, end
# ---------------------------------------------------------------------------


class _NamedEvent:
    """A named auto-reset event this process creates (the UI sets it after an answer)."""

    def __init__(self, name: str) -> None:
        k = ctypes.windll.kernel32
        k.CreateEventW.restype = ctypes.c_void_p
        k.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.SetEvent.argtypes = [ctypes.c_void_p]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        self._k = k
        self.handle = k.CreateEventW(None, False, False, name)

    def wait(self, timeout_s: float) -> bool:
        if not self.handle:
            threading.Event().wait(timeout_s)
            return False
        return self._k.WaitForSingleObject(self.handle, int(max(0.0, timeout_s) * 1000)) == 0

    def set(self) -> None:
        if self.handle:
            self._k.SetEvent(self.handle)

    def close(self) -> None:
        if self.handle:
            self._k.CloseHandle(self.handle)
            self.handle = None


class _ConsentStoreWatch:
    """A change notification on the ConsentStore's microphone key (``RegNotifyChangeKeyValue``, subtree,
    values): Windows writes it when an app starts or stops using the microphone, so the holders are read
    only then instead of on every poll."""

    _KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"

    def __init__(self) -> None:
        import winreg

        self.ok = False
        self._evt = None
        self._k = ctypes.WinDLL("kernel32")
        self._a = ctypes.WinDLL("advapi32")
        self._k.CreateEventW.restype = ctypes.c_void_p
        self._k.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        self._k.WaitForMultipleObjects.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
        self._k.WaitForMultipleObjects.restype = ctypes.c_uint32
        self._k.CloseHandle.argtypes = [ctypes.c_void_p]
        self._a.RegNotifyChangeKeyValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p,
                                                    ctypes.c_int]
        self._a.RegNotifyChangeKeyValue.restype = ctypes.c_long
        try:
            self._key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._KEY, 0, winreg.KEY_NOTIFY | winreg.KEY_READ)
        except OSError:
            self._key = None
            return
        self._evt = self._k.CreateEventW(None, False, False, None)
        self.ok = bool(self._evt) and self._arm()

    def _arm(self) -> bool:
        # REG_NOTIFY_CHANGE_NAME | REG_NOTIFY_CHANGE_LAST_SET over the subtree, asynchronous (sets the event)
        return self._a.RegNotifyChangeKeyValue(ctypes.c_void_p(int(self._key)), 1, 0x1 | 0x4,
                                               ctypes.c_void_p(self._evt), 1) == 0

    def wait(self, event: _NamedEvent, timeout_s: float) -> str:
        ms = int(max(0.0, timeout_s) * 1000)
        if event.handle:
            handles = (ctypes.c_void_p * 2)(event.handle, self._evt)
            r = self._k.WaitForMultipleObjects(2, handles, False, ms)
            which = {0: "event", 1: "registry"}.get(r, "timeout")
        else:
            handles = (ctypes.c_void_p * 1)(self._evt)
            r = self._k.WaitForMultipleObjects(1, handles, False, ms)
            which = "registry" if r == 0 else "timeout"
        if which == "registry":
            self.ok = self._arm()
        return which

    def close(self) -> None:
        try:
            if self._key is not None:
                self._key.Close()
            if self._evt:
                self._k.CloseHandle(ctypes.c_void_p(self._evt))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# One call = one session (docs/research/revamp-2026-10-02/05-call-pill-timer.md §7)
# ---------------------------------------------------------------------------

#: Two ConsentStore ``since`` values this close are the same microphone hold (the record's start, read by two
#: processes, or before and after a restart; the capture clock and the ConsentStore clock differ by this much).
HOLD_MATCH_S = 5.0
#: The ``nudge_state`` row with the microphone holders the call worker read last (``{"keys": [...], "at": t}``):
#: the transcriber's and the digest's gates read it (:func:`lingering_call`).
HOLDERS_STATE = "call_mic_holders"
#: A call row that ended this recently while its app still holds the microphone may not have ended at all (a
#: persistent screen contradiction, a pause, a restart it could not adopt): heavy work waits (05 §7.5, §8.4).
LINGER_S = 600.0


@dataclass
class LifeStep:
    """What :meth:`CallLife.observe` decided for one poll (content-free)."""

    #: The call goes on.
    alive: bool
    #: Why it ends now (``alive`` False): ``mic_released`` or ``screen_contradicts``.
    end: str | None = None
    #: A new microphone hold of the same app inside this call: ``{"gap_s", "old_since", "new_since"}``.
    regrab: dict[str, float] | None = None
    #: What keeps it alive: ``hold`` (the app holds the microphone), ``release_grace`` (it let go less than
    #: ``release_grace_s`` ago), ``screen`` (an in-call frame shown since it let go), ``end_grace`` (dying).
    by: str = "hold"


class CallLife:
    """One call's lifetime from measured signals only (pure: no clock, no I/O; the worker feeds it every poll).

    The live problem (2 Oct 2026): one Discord frame read mid alt-tab and judged "not in a call" ended a call
    whose microphone was held the whole time, and the pill came back at 00:00 32 minutes later. The rule now:

    * **The microphone hold is the session.** While the call's app holds the microphone the call is alive. A
      new hold of the same app (a new ConsentStore ``since``) while the call is alive is a **re-grab**: the
      same call. The screen's "newer than the hold" test applies to starting a call only
      (:meth:`yuki.memory.callevidence.CallEvidence.state`).
    * **The screen ends a held call only on a persistent, reliable contradiction**: at least ``neg_frames``
      reliable "not in a call" sights spanning at least ``neg_span_s``, with no in-call frame between (a
      "You left the meeting" page while the browser keeps the microphone). One frame never ends a held call;
      half-drawn frames never count (:meth:`yuki.memory.callevidence.CallEvidence.life`).
    * **Without the microphone** the call lives on for ``release_grace_s`` unless the screen already says it
      is over, or while an in-call frame shown *after* the app let go is at most ``screen_alive_s`` old (Meet
      drops the microphone on mute, its page still shows the meeting). Not alive for ``end_grace_s``: it ends.

    Args:
        cfg: The ``[calls]`` numbers.
        now: When the call was started (or adopted).
        hold_since: The ConsentStore ``since`` of the hold it is in.
        held: Whether the app holds the microphone now (False: adopted after a restart while it had let go).
        last_held_at: When it was last known to hold it (default ``now`` when held).
    """

    def __init__(self, cfg: CallConfig, *, now: float, hold_since: float | None, held: bool = True,
                 last_held_at: float | None = None) -> None:
        self.cfg = cfg
        self.hold_since = hold_since
        self.last_held_at: float | None = last_held_at if last_held_at is not None else (now if held else None)
        #: The last poll the call was alive by something other than the end grace.
        self.alive_at = float(now)
        self.regrabs = 0

    def contradicted(self, negatives: Sequence[float]) -> bool:
        """At least ``neg_frames`` reliable negative sights spanning ``neg_span_s`` (no positive between)."""
        return (len(negatives) >= self.cfg.neg_frames
                and float(negatives[-1]) - float(negatives[0]) >= self.cfg.neg_span_s)

    def observe(self, now: float, *, held: bool, hold_since: float | None, positive_at: float | None,
                negatives: Sequence[float] = ()) -> LifeStep:
        """One poll: does the call go on? ``negatives``: capture times of the reliable not-in-call sights of the
        call's site since its newest in-call frame ``positive_at``."""
        cfg = self.cfg
        negs = sorted(float(t) for t in negatives)
        if held:
            regrab = None
            if hold_since is not None and self.hold_since is not None and abs(hold_since - self.hold_since) > HOLD_MATCH_S:
                held_until = self.last_held_at if self.last_held_at is not None else hold_since
                regrab = {"gap_s": round(max(0.0, float(hold_since) - held_until), 1),
                          "old_since": float(self.hold_since), "new_since": float(hold_since)}
                self.regrabs += 1
            if hold_since is not None:
                self.hold_since = float(hold_since)
            self.last_held_at = now
            if self.contradicted(negs):
                return LifeStep(False, end="screen_contradicts", regrab=regrab)
            self.alive_at = now
            return LifeStep(True, regrab=regrab, by="hold")
        let_go = self.last_held_at if self.last_held_at is not None else self.alive_at
        by = None
        if not negs and now - let_go < cfg.release_grace_s:
            by = "release_grace"
        elif (positive_at is not None and not negs and now - positive_at <= cfg.screen_alive_s
              and positive_at > let_go):
            # an in-call frame captured after the last poll that saw the microphone held (the same clock): the
            # screen still showed the call once the app had let go (Meet's page while muted)
            by = "screen"
        if by is not None:
            self.alive_at = now
            return LifeStep(True, by=by)
        if now - self.alive_at < cfg.end_grace_s:
            return LifeStep(True, by="end_grace")
        return LifeStep(False, end="screen_contradicts" if negs else "mic_released")


def lingering_call(store: Store, now: float | None = None, *, within_s: float = LINGER_S) -> Any:
    """A call row that ended at most ``within_s`` ago whose app still holds the microphone, per the call worker's
    last reading (:data:`HOLDERS_STATE`), or None: the call may not be over, so heavy work (the transcriber,
    the digest) does not start (05 §7.5). Content-free; never raises."""
    now = time.time() if now is None else float(now)
    try:
        data = json.loads(store.nudge_state(HOLDERS_STATE) or "{}")
        keys = {str(k) for k in (data.get("keys") or [])} if isinstance(data, dict) else set()
        if not keys:
            return None
        rows = [c for c in store.calls_overlapping(now - within_s, now + 1.0)
                if c.ended_at is not None and now - c.ended_at <= within_s and (c.process or "") in keys]
        return max(rows, key=lambda c: c.ended_at) if rows else None
    except Exception:
        return None


@dataclass
class _Active:
    """One open voice-session row the worker drives: a call (``kind`` "call") or a game's voice chat ("game")."""

    call_id: int
    key: str
    app: str
    via: str
    service: str | None
    detected_at: float
    last_seen: float
    #: The microphone's recording while this row owns it (one row at a time: the call's, else the game's).
    session: RecordingSession | None = None
    status: str = "asking"
    headphones: bool | None = None
    #: Other people in the call as its screen last showed (0: the user is alone in it), None: not shown (v3.15).
    others: int | None = None
    #: The row's microphone holder as last seen (an ``avsignals.Holder``; kept while it has let go).
    holder: Any = None
    #: Its lifetime rules (:class:`CallLife`), and whether the app holds the microphone this poll.
    life: CallLife | None = None
    held: bool = True
    #: What kept it alive at the last poll (:attr:`LifeStep.by`) and when ``last_alive_at`` was last written.
    alive_by: str = "hold"
    alive_written: float = 0.0
    #: An adopted call's stored chunks and counts (kept for older callers; a recording now always goes on from
    #: the row's own counts and chunk numbers).
    seq_base: int = 0
    base: RecordingStats | None = None
    #: ``call`` | ``game`` (``calls.kind``).
    kind: str = "call"
    #: A game: the process of its full-screen window (the app track's target; Yuki never opens it) and the last
    #: poll it was seen in front drawing like a game (its "screen" for :class:`CallLife`).
    pid: int | None = None
    gaming_at: float | None = None
    #: A game's card: when it went up (None while it is down) and how long it was up before.
    asking_since: float | None = None
    asked_s: float = 0.0
    #: The app's own audio (process loopback): its recording, the row its chunks go to, the process tree
    #: captured, its state for the UI (recording | paused | off | unavailable | asking) and why, and when an
    #: unavailable track is tried again.
    track: RecordingSession | None = None
    track_target: int | None = None
    track_pid: int | None = None
    track_state: str = "off"
    track_reason: str | None = None
    track_retry_at: float = 0.0


#: Row statuses in which a voice session may record (said yes or always; not stopped).
RECORDING_STATUSES: tuple[str, ...] = ("recording", "waiting_headphones")
#: The ``nudge_state`` row with every track's state (``call_status()`` reads it for the UI).
TRACKS_STATE = "call_tracks"


@dataclass(frozen=True)
class TrackSpec:
    """What :attr:`CallWorker.track_factory` opens: the own audio of ``pid``'s process tree, for row ``call_id``."""

    call_id: int
    app: str
    process: str
    kind: str
    pid: int


def loopback_source(cfg: CallConfig, spec: TrackSpec) -> Any:
    """The real app track: process loopback of ``spec.pid``'s tree (:class:`yuki.memory.loopback.LoopbackSource`).
    It opens on its own thread (``wait=False``): the worker's poll never waits on Windows; the next polls see it
    ``ready``, or dead with its ``error``."""
    from yuki.memory.loopback import LoopbackSource

    return LoopbackSource(spec.pid, include_tree=True, label=spec.process, wait=False)


def app_pid(holder: Holder, *, avoid: Iterable[int] = ()) -> int | None:
    """The root process of a call app's tree (the loopback target), from one Toolhelp snapshot: the processes that
    run the holder's image, walked up while the parent runs the same image; of several, the one playing sound
    (Core Audio render sessions: state only). No process handle is opened. A Store app (package family, no
    image) is found through its playing processes' package identity, never through ``avoid`` (the full-screen
    window's process: a game is not opened). For a browser the tree is the whole browser: every tab's sound."""
    from yuki.memory.loopback import app_root_pid, process_table
    from yuki.perception import audio

    table = process_table()
    playing: list[int] = []
    try:
        playing = [int(s.get("pid") or 0) for s in audio.render_sessions() if s.get("state") == "active"]
    except Exception:
        playing = []
    if not holder.packaged:
        image = Path(holder.path).name if holder.path else holder.app
        return app_root_pid(image, table, playing)
    from yuki.memory.avsignals import _pid_identity

    skip = {int(p) for p in avoid}
    names = {pid: image for pid, _parent, image in table}
    for pid in playing:
        if pid in skip or pid not in names:
            continue
        exe, family = _pid_identity(pid)
        if holder.matches(exe=exe, family=family):
            return app_root_pid(names[pid], table, playing)
    return None


class _SeqBook:
    """Chunk numbers per row, shared by every recording writing into it (the microphone and app tracks)."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._last: dict[int, int] = {}
        self._lock = threading.Lock()

    def _get(self, call_id: int) -> int:
        if call_id not in self._last:
            self._last[call_id] = self._store.call_max_seq(call_id)
        return self._last[call_id]

    def last(self, call_id: int) -> int:
        with self._lock:
            return self._get(int(call_id))

    def next(self, call_id: int) -> int:
        with self._lock:
            value = self._get(int(call_id)) + 1
            self._last[int(call_id)] = value
            return value


_DEFAULT: Any = object()


def _screen_busy() -> bool:
    from yuki.memory.transcribe import screen_busy

    return screen_busy()


class CallWorker:
    """Detects calls and game voice chats, asks, and records every track (see the module docstring). One thread.

    Every outside fact comes through a replaceable function, so the whole
    flow runs offline on a temp database with synthetic audio. With a stub
    microphone (``source_factory``) and no ``track_factory`` / ``watch_factory``
    no app audio is opened and no capture session is watched: an offline worker
    never touches a live stream.
    """

    def __init__(
        self, store: Store, *, db_path: Path | None = None, privacy: Any = None,
        log: Callable[..., Any] | None = None, meeting_fn: Callable[[], dict | None] = lambda: None,
        holders_fn: Callable[[], list[dict]] | None = None,
        signals: AvSignals | None = None,
        headphones_fn: Callable[[CallConfig], tuple[bool, dict]] = headphones_now,
        source_factory: Callable[[CallConfig, str | None], Any] | None = None,
        config: CallConfig | None = None,
        foreground_fn: Callable[[], dict | None] = foreground_fullscreen,
        gpu_fn: Callable[..., float | None] = gpu_share,
        evidence: Any = None,
        sites_fn: Callable[[str], int] | None = None,
        track_factory: Any = _DEFAULT,
        pid_fn: Callable[[Holder], int | None] | None = None,
        busy_fn: Callable[[], bool] | None = None,
        watch_factory: Any = _DEFAULT,
        table_fn: Callable[[], list[tuple[int, int, str]]] | None = None,
    ) -> None:
        from yuki.memory.timeline import PrivacySection

        self.store = store
        self.db_path = Path(db_path) if db_path is not None else store.path
        self._section = PrivacySection(privacy, "calls", CallConfig.from_dict) if config is None else None
        self._config = config
        self._log_fn = log
        self.meeting_fn = meeting_fn
        #: The full-screen window in front (``{"hwnd", "pid", "exe"}`` or None) and the GPU share reader:
        #: a full-screen game with voice chat is a game voice session, never a call
        #: (yuki.memory.avsignals.gaming_with_voice).
        self.foreground_fn = foreground_fn
        self.gpu_fn = gpu_fn
        self._gaming_logged: set[str] = set()
        #: What the screen shows (:class:`yuki.memory.callevidence.CallEvidence`, fed by the watcher): a
        #: microphone holder is a call only with an in-call screen of it. ``None``: nothing is ever proof
        #: (only the user's own ``[calls] apps`` make a call).
        self.evidence = evidence
        #: ``(app key) -> int``: how many sites the timeline saw the app show (a browser shows many).
        self.sites_fn = sites_fn
        self._sites: dict[str, tuple[float, int]] = {}
        #: The measured facts (shared with the timeline in the service). Holders are read again only when
        #: Windows changed a ConsentStore key or every ``holders_backstop_s``; a poll between is a cached read.
        #: ``holders_fn`` (a stub of the microphone holders alone, no sound out, no camera) is the old test hook.
        if signals is None:
            signals = (AvSignals(frozenset(), mic_fn=holders_fn, camera_fn=list, render_fn=list, watch=False,
                                 backstop_s=0.0) if holders_fn is not None else AvSignals(own_executables()))
        self.signals = signals
        #: The wake-up on a ConsentStore change (the loop's wait), only with real signals.
        self._watch_registry = holders_fn is None and bool(getattr(signals, "_watch", False))
        self._held: set[str] = set()
        #: The microphone holders of the last read, by app key; the keys last written to HOLDERS_STATE.
        self._holders: dict[str, Holder] = {}
        self._holders_written: list[str] | None = None
        self._camera: tuple[Holder, ...] = ()
        self.headphones_fn = headphones_fn
        live = source_factory is None
        self.source_factory = source_factory or (lambda cfg, app: MicSource(capture_device_for(cfg, app)))
        #: The app's own audio: ``(cfg, TrackSpec) -> source`` (process loopback by default; None: app tracks are
        #: never opened, e.g. offline checks with a stub microphone).
        self.track_factory = (loopback_source if live else None) if track_factory is _DEFAULT else track_factory
        self.pid_fn = pid_fn or (lambda holder: app_pid(holder, avoid=self._front_pids()))
        #: Windows says a full-screen app, a game or a presentation is up (a card cannot be seen then).
        self.busy_fn = busy_fn or _screen_busy
        #: ``(on_change) -> watch`` with ``start()``/``stop()``/``mode``: which apps take the microphone, moment by
        #: moment (:class:`yuki.memory.loopback.CaptureWatch`; None: not measured, lines get no ``heard_by``).
        self.watch_factory = (self._capture_watch if live else None) if watch_factory is _DEFAULT else watch_factory
        self.table_fn = table_fn
        #: The open call row and the open game row (a game's voice chat), at most one of each; a game row whose
        #: card goes up right after its session (``after``).
        self.active: _Active | None = None
        self.game: _Active | None = None
        self.after: _Active | None = None
        self._gaming: tuple[Holder, dict, dict] | None = None
        self._poll: dict[str, Any] = {}
        self._seqs = _SeqBook(store)
        self._watch: Any = None
        self._watch_span: int | None = None
        self._spans_open: dict[int, int] = {}
        self._span_lock = threading.Lock()
        self._pid_apps: dict[int, tuple[str | None, str | None]] = {}
        #: Per app key: the processes whose capture session is active now, and when its last one went inactive.
        self._capture_live: dict[str, set[int]] = {}
        self._capture_last: dict[str, float] = {}
        self._capture_keys: dict[int, str] = {}
        self._mic_state: dict[str, Any] = {"state": "off", "call_id": None, "reason": None}
        #: A row that records but keeps nothing (:meth:`_check_stall`), or None.
        self._stall: dict[str, Any] | None = None
        self._tracks_written: str | None = None
        self.paused = False
        self._event: _NamedEvent | None = None
        self._stop = threading.Event()
        self._errors = 0

    # -- plumbing ------------------------------------------------------------

    @property
    def cfg(self) -> CallConfig:
        return self._config if self._config is not None else self._section.get()

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is not None:
            try:
                self._log_fn(type, **fields)
            except Exception:
                pass

    def _tell_ui(self) -> None:
        signal_event(CALL_READY_EVENT)

    def _quiet(self, now: float) -> bool:
        try:
            until = float(self.store.nudge_state("quiet_until") or 0.0)
        except (TypeError, ValueError):
            until = 0.0
        return until > now

    def _rows(self) -> list[_Active]:
        return [r for r in (self.active, self.game) if r is not None]

    def _front_now(self) -> dict | None:
        """The full-screen window in front, read once per poll."""
        if "front" not in self._poll:
            try:
                self._poll["front"] = self.foreground_fn()
            except Exception:
                self._poll["front"] = None
        return self._poll["front"]

    def _front_pids(self) -> list[int]:
        front = self._poll.get("front")
        return [int(front.get("pid") or 0)] if front else []

    def _hp(self, cfg: CallConfig) -> tuple[bool, dict[str, Any]]:
        """Headphones, read once per poll."""
        if "hp" not in self._poll:
            self._poll["hp"] = self._headphones(cfg)
        return self._poll["hp"]

    def _card_visible(self) -> bool:
        """Whether a card can be seen now: nothing full screen in front, and Windows not saying a game or
        presentation is up."""
        if self._front_now() is not None:
            return False
        try:
            return not bool(self.busy_fn())
        except Exception:
            return True

    # -- lifecycle -----------------------------------------------------------

    def recover(self, now: float | None = None) -> int:
        """Calls left open by a previous run (crash, restart): adopt the one that is still going, end the rest.

        The service restarting is not the call ending (05 §7.3: rows 39-45 were cut by restarts, and row 41 ran
        "505 min" because it was ended at the restart after the PC had been off for 7.9 h). An open row is
        **adopted** (the same row goes on; a new recording continues its chunks) when its app still holds the
        microphone with the same ConsentStore ``since`` (within :data:`HOLD_MATCH_S`) and the row was alive at
        most ``adopt_within_s`` ago - or, by the same rules as a live call, when it was alive less than
        ``release_grace_s`` ago (a re-grab or a short release around the restart). A row from before migration
        26 (no ``hold_since``) is adopted when the hold it would be in began before the row did. One call row
        and one game row can be adopted. Every other open row ends at its ``last_alive_at`` (an older row: now,
        as before); a game row whose card was waiting after its session ends unanswered. Returns how many were
        ended.
        """
        now = time.time() if now is None else float(now)
        cfg = self.cfg
        try:
            holders = {h.key: h for h in self.signals.read().mic if h.key}
        except Exception as exc:
            holders = {}
            self._log("error", where="calls.recover_holders", error=f"{type(exc).__name__}: {exc}")
        n = 0
        for c in self.store.calls(statuses=CALL_OPEN_STATUSES):   # newest first
            h = holders.get(c.process or "")
            if c.ended_at is not None:   # a game's card was waiting after its session: no answer now
                if c.consent is None:
                    self.store.update_call(c.id, consent="none", consent_by="call_ended", consent_at=now)
                self.store.update_call(c.id, status="ended")
                self._log("call_consent", call_id=c.id, app=c.app, call_kind=c.kind, answer="none", by="service_restart")
                n += 1
                continue
            game = c.kind == "game"
            free = self.game is None if game else self.active is None
            if free and cfg.enabled and (cfg.game_voice or not game) and self._adoptable(cfg, c, h, now):
                self._adopt(cfg, c, h, now)
                continue
            end_at = min(max(c.last_alive_at, c.detected_at), now) if c.last_alive_at is not None else now
            if c.kind == "game" and c.consent is None and c.status in ("asking", "ask_later"):
                self.store.update_call(c.id, consent="none", consent_by="call_ended", consent_at=end_at)
            self.store.update_call(c.id, status="ended", ended_at=end_at, stop_reason="service_restart")
            self._log("call_ended", call_id=c.id, app=c.app, call_kind=c.kind, reason="service_restart",
                      seconds=round(end_at - c.detected_at, 1), held=h is not None, hold_since=c.hold_since,
                      down_s=round(now - c.last_alive_at, 1) if c.last_alive_at is not None else None)
            n += 1
        try:   # who-took-the-microphone spans the previous run left open
            self.store.close_open_mic_spans(now)
        except Exception as exc:
            self._log("error", where="calls.recover_spans", error=f"{type(exc).__name__}: {exc}")
        try:
            self.rekey_prefs()
        except Exception as exc:
            self._log("error", where="calls.rekey_prefs", error=f"{type(exc).__name__}: {exc}")
        return n

    @staticmethod
    def _adoptable(cfg: CallConfig, c: Any, h: Holder | None, now: float) -> bool:
        """Whether a restarted service goes on with open row ``c`` (see :meth:`recover`)."""
        if c.hold_since is None:   # a row written before migration 26: the hold must have begun before it
            return h is not None and h.since <= c.detected_at + HOLD_MATCH_S
        if c.last_alive_at is None:
            return False
        down = now - c.last_alive_at
        if h is not None and abs(h.since - c.hold_since) <= HOLD_MATCH_S:
            return down <= cfg.adopt_within_s
        return down < cfg.release_grace_s   # let go or re-grabbed around the restart: the live rules decide

    def _adopt(self, cfg: CallConfig, c: Any, h: Holder | None, now: float) -> _Active:
        """Go on with open row ``c`` after a restart: the same row, its counts and chunk numbers continue."""
        held = h is not None
        hold_since = c.hold_since if c.hold_since is not None else (h.since if h is not None else None)
        act = _Active(c.id, c.process or (h.key if h is not None else ""), c.app, c.via, c.service, c.detected_at,
                      now, status=c.status, headphones=c.headphones, holder=h, held=held, kind=c.kind)
        # The previous run's hold epoch: a new ``since`` now is a re-grab inside the call (logged on the next poll).
        act.life = CallLife(cfg, now=now, hold_since=hold_since, held=held,
                            last_held_at=None if held else (c.last_alive_at or now))
        act.seq_base = self.store.call_max_seq(c.id)
        act.base = RecordingStats(chunks=c.chunks, chunks_silent=c.chunks_silent, audio_s=c.audio_s,
                                  speech_s=c.speech_s)
        act.alive_written = now
        if c.kind == "game":
            act.gaming_at = now   # it was a game in front when it began; the next polls measure it again
        self.store.update_call(c.id, last_alive_at=now)
        if c.kind == "game":
            self.game = act
        else:
            self.active = act
        self._log("call_resumed", call_id=c.id, app=c.app, call_kind=c.kind, process=act.key, status=c.status, held=held,
                  hold_since=hold_since, now_since=h.since if h is not None else None,
                  down_s=round(now - (c.last_alive_at if c.last_alive_at is not None else c.updated_at), 1),
                  seq=act.seq_base, legacy=c.hold_since is None)
        self._tell_ui()
        return act

    def rekey_prefs(self) -> list[tuple[str, str]]:
        """Standing answers given on a site's call inside a browser move from the browser to that service.

        Before 2026-09-28 an "Always" on a Google Meet card was kept for the process (``zen``), so every later
        microphone use of the browser was that call. A process's pref whose every card answer was given on a
        call with a service (and the same decision) belongs to that service; idempotent, from Yuki's own rows.
        Returns the ``(process, service)`` pairs moved.
        """
        moved: list[tuple[str, str]] = []
        calls = self.store.calls()
        for pref in self.store.call_prefs():
            key, decision = str(pref.get("app") or ""), pref.get("decision")
            answered = [c for c in calls if (c.process or "") == key and c.consent_by == "card"
                        and c.consent in ("always", "never")]
            if not answered or any(not c.service for c in answered):
                continue
            services = {app_key(c.service) for c in answered if c.consent == decision}
            if len(services) != 1 or key in services:
                continue
            service = next(iter(services)) or ""
            if not service:
                continue
            name = next((c.service for c in answered if app_key(c.service) == service), service)
            if self.store.call_pref(service) is None:
                self.store.set_call_pref(service, decision, name=name)
            self.store.set_call_pref(key, None)
            moved.append((key, service))
            self._log("call_pref_rekeyed", process=key, to=service)
        return moved

    def run(self, stop: threading.Event | None = None, pause_path: Path | None = None) -> None:
        stop = stop or self._stop
        self._stop = stop
        self._event = _NamedEvent(call_event_name(self.db_path))
        watch = _ConsentStoreWatch() if self._watch_registry else None
        recovered = self.recover()
        self._log("calls_start", recovered=recovered, adopted=self.active.call_id if self.active else None,
                  adopted_game=self.game.call_id if self.game else None, poll_s=self.cfg.poll_s,
                  enabled=self.cfg.enabled, record_others=self.cfg.record_others, game_voice=self.cfg.game_voice)
        try:
            while not stop.is_set():
                self.paused = bool(pause_path is not None and pause_path.exists())
                try:
                    self.step()
                    self._errors = 0
                except Exception as exc:
                    self._errors += 1
                    self._log("error", where="calls.step", error=f"{type(exc).__name__}: {exc}",
                              traceback=traceback.format_exc())
                timeout = self.cfg.poll_s * (1 if self._errors < 3 else 6)
                if watch is not None and watch.ok:
                    watch.wait(self._event, timeout)   # the signals see the same change on their next read
                else:
                    self._event.wait(timeout)
        finally:
            self.close("service_stop")
            if watch is not None:
                watch.close()
            self._event.close()
            self._log("calls_stop")

    def close(self, reason: str = "service_stop", now: float | None = None) -> None:
        """Stop every open row's recordings (each track's last chunk is spooled), the waiting card and the capture
        watch. On ``service_stop`` (an install, a restart from the tray, Windows shutting down) a row is **left open**
        (:meth:`_suspend`), not ended: the service stopping is not the call ending. The next start adopts it when
        the call goes on (:meth:`recover`: same microphone hold, within ``adopt_within_s``), else ends it at this
        moment. Before, every install cut the call in two (call 63 on 3 Oct, and the "timer restarts" the user
        reported). Any other reason ends the rows."""
        now = time.time() if now is None else float(now)
        for r in self._rows():
            try:
                if reason == "service_stop":
                    self._suspend(r, now)
                else:
                    self._end(r, reason, now, ask=False)
            except Exception as exc:
                self._log("error", where="calls.close", error=f"{type(exc).__name__}: {exc}")
        self.active = self.game = None
        if self.after is not None:
            self._close_after(now, reason)
        self._sync_watch(False, now)
        # nothing records now: no stall stays behind in what the UI and the doctor read (review F5; a suspended row
        # is open on purpose)
        self._stall = None
        self._note_tracks()

    def _suspend(self, r: _Active, now: float) -> None:
        """The service stops while row ``r`` goes on: its microphone and app track stop (each spools its last
        chunk; the row's own stop reason is left for its real end), and the row stays open, alive as of ``now``,
        for :meth:`recover` to adopt or end at the next start."""
        if r.session is not None:
            self._stop_recording(r, "service_stop", final=False)
        self._stop_track(r, "service_stop")
        self.store.update_call(r.call_id, last_alive_at=now)
        self._log("call_suspended", call_id=r.call_id, app=r.app, call_kind=r.kind, status=r.status,
                  held=r.key in self._held, seconds=round(now - r.detected_at, 1))

    def stop(self) -> None:
        self._stop.set()
        if self._event is not None:
            self._event.set()

    # -- one pass ------------------------------------------------------------

    @property
    def holder_reads(self) -> int:
        return int(self.signals.counters.get("holder_reads", 0))

    def _is_browser(self, cfg: CallConfig, key: str, now: float) -> bool:
        """Whether app ``key`` shows many sites (measured from the timeline, cached 10 min): a browser."""
        if not key:
            return False
        hit = self._sites.get(key)
        if hit is None or now - hit[0] > 600.0:
            try:
                if self.sites_fn is not None:
                    count = int(self.sites_fn(key) or 0)
                else:
                    count = int(self.store.sites_shown(key, since=now - cfg.sites_days * 86400.0) or 0)
            except Exception:
                count = 0
            hit = self._sites[key] = (now, count)
        return hit[1] >= cfg.browser_sites

    def _candidates(self, cfg: CallConfig, now: float | None = None) -> list[tuple[str, str, str | None, str]]:
        """``(app key, via, service, display name)`` of the calls going on now, per the screen.

        A microphone holder is only a hint: it is a call when the screen showed it in a joined call during this
        hold (:class:`~yuki.memory.callevidence.CallEvidence`: ``via`` "screen" while that is fresh,
        "screen_stale" after ``evidence_fresh_s``), or when it is one of the user's own ``[calls] apps``
        ("mic"). Sound out, the camera and the timeline's meeting row are hints only (the meeting row may name
        the service). ``service`` is set only for a call on a site inside a browser (its standing answer is the
        site's). A holder that is the full-screen app in front drawing like a game (``gaming_with_voice``) is
        never a call: with ``game_voice`` it is kept in :attr:`_gaming` (a game voice session begins from it).
        The open game row's app is never a call either. Also sets :attr:`_held` (every microphone holder now).
        """
        now = time.time() if now is None else float(now)
        meeting = None
        try:
            meeting = self.meeting_fn()
        except Exception:
            meeting = None
        meeting_key = app_key((meeting or {}).get("process")) if meeting else None
        snap = self.signals.read()
        mic: tuple[Holder, ...] = snap.mic
        self._camera = tuple(snap.camera)
        self._held = {h.key for h in mic if h.key}
        self._holders = {h.key: h for h in mic if h.key}
        self._gaming = None
        game_key = self.game.key if self.game is not None else None
        evidence = self.evidence
        if evidence is not None:
            evidence.fresh_s = cfg.evidence_fresh_s
        # the holders' screens, and the open call's even while its app has let go (a "you left" screen ends it);
        # never the open game's (its screens are not a call's, and judging them costs a model call each)
        judge_for: list[Any] = [h for h in mic if h.key and h.key != game_key]
        act = self.active
        if act is not None and act.key not in self._held:
            judge_for.append(act.holder or act.key)
        if evidence is not None and judge_for:
            try:
                evidence.judge(judge_for)
            except Exception as exc:
                self._log("error", where="calls.evidence", error=f"{type(exc).__name__}: {exc}")
        out: list[tuple[str, str, str | None, str]] = []
        for h in mic:
            key = h.key
            if not key:
                continue
            if key not in cfg.apps:
                front = self._front_now()
                if front and h.matches(exe=str(front.get("exe") or "")):
                    gaming, facts = gaming_with_voice(h, fullscreen=True, shows_page=False, camera=snap.camera,
                                                      pids=[int(front.get("pid") or 0)], gpu_fn=self.gpu_fn)
                    if gaming:
                        if cfg.game_voice:
                            if self._gaming is None:
                                self._gaming = (h, front, facts)
                        elif key not in self._gaming_logged:
                            self._gaming_logged.add(key)
                            self._log("call_skipped_gaming", app=key, **facts)
                        continue   # a full-screen game's own voice chat: never a call
            if key == game_key:
                continue   # the open game voice session's app (it may hold the microphone out of full screen)
            state, service = None, ""
            if evidence is not None:
                try:
                    state, service = evidence.state(h, h.since, now)
                except Exception:
                    state, service = None, ""
            if key not in cfg.apps and state is None:
                continue
            if state is None:
                via = "mic"
            else:
                via = "screen" if state == "fresh" else "screen_stale"
            if key == meeting_key and not service:
                service = str((meeting or {}).get("service") or "")
            site_service: str | None = None
            if self._is_browser(cfg, key, now):
                site_service = service or (evidence.host_of(h) if evidence is not None else None) or None
            out.append((key, via, site_service, cfg.name_of(key, site_service or display_name(h))))
        return out

    def step(self, now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        cfg = self.cfg
        self._poll = {}
        if self.paused or not cfg.enabled:
            reason = "paused" if self.paused else "disabled"
            for r in self._rows():
                self._end(r, reason, now, ask=False)
            self.active = self.game = None
            if self.after is not None:
                self._close_after(now, reason)
            self._sync_audio(cfg, now)
            self._check_stall(cfg, now)
            self._note_tracks()
            return
        cands = self._candidates(cfg, now)
        self._note_holders(now)
        act = self.active
        if act is not None and not self._goes_on(cfg, act, now):
            self.active = act = None
        if act is None and cands:
            key, via, service, name = cands[0]
            act = self._begin(cfg, key, via, service, name, now)
            self.active = act
        g = self.game
        if g is not None and not cfg.game_voice:
            self._end(g, "disabled", now, ask=False)
            self.game = g = None
        if (g is not None and self._gaming is not None and self._gaming[0].key != g.key
                and g.key not in self._held):
            self._end(g, "other_game", now)   # straight into another game: the old one is over
            self.game = g = None
        if g is not None and not self._game_goes_on(cfg, g, now):
            self.game = g = None
        if g is None and self._gaming is not None and cfg.game_voice:
            g = self._begin_game(cfg, now)
            self.game = g
        if act is not None:
            self._note_people(act)
            self._drive(cfg, act, now)
            self._heartbeat(cfg, act, now)
        if g is not None:
            self._drive(cfg, g, now)
            self._heartbeat(cfg, g, now)
        if self.after is not None:
            self._drive_after(cfg, now)
        self._sync_audio(cfg, now)
        self._check_stall(cfg, now)
        self._note_tracks()

    def _check_stall(self, cfg: CallConfig, now: float) -> None:
        """Flag a row that records but keeps nothing (:attr:`CallConfig.stall_min`), measured on its open
        recordings: the microphone it owns and every app track writing into it. Nothing is flagged while nothing
        listens (a paused microphone already says why). ``reason`` "no_audio": the microphone's source brought no
        audio at all (silence included) in that time, a dead capture; "no_speech": audio arrives but no chunk was
        kept (nobody spoke, the user muted, or the gate drops everything). Logged once per stall
        (``call_recording_stalled``) and when chunks flow again (``call_recording_flowing``); content-free."""
        limit = cfg.stall_min * 60.0
        stall: dict[str, Any] | None = None
        for r in self._rows():
            sessions = [s for s in ([r.session] + [o.track for o in self._rows() if o.track_target == r.call_id])
                        if s is not None and s.listening_since is not None]
            if not sessions:
                continue
            since = max(max(s.last_kept_at or 0.0, s.listening_since or 0.0) for s in sessions)
            if now - since < limit:
                continue
            mic = r.session if r.session is not None and r.session.listening_since is not None else None
            dead = mic is not None and now - max(mic.last_audio_at or 0.0, mic.listening_since or 0.0) >= limit
            stall = {"call_id": r.call_id, "app": r.app, "kind": r.kind, "since": round(since, 1),
                     "minutes": int((now - since) // 60), "reason": "no_audio" if dead else "no_speech"}
            break
        before = self._stall
        if stall is not None and (before is None or (before["call_id"], before["reason"])
                                  != (stall["call_id"], stall["reason"])):
            self._log("call_recording_stalled", call_id=stall["call_id"], app=stall["app"], call_kind=stall["kind"],
                      reason=stall["reason"], quiet_min=stall["minutes"])
        elif stall is None and before is not None:
            self._log("call_recording_flowing", call_id=before["call_id"], app=before["app"],
                      call_kind=before["kind"], stalled_min=round((now - before["since"]) / 60.0, 1))
        self._stall = stall

    def _goes_on(self, cfg: CallConfig, act: _Active, now: float) -> bool:
        """One poll of the open call's life (:class:`CallLife`); ends it (and returns False) when it is over."""
        h = self._holders.get(act.key)
        if h is not None:
            act.holder = h
        act.held = h is not None
        life = self._life(act)
        if act.life is None:   # a call begun before CallLife existed in this object (tests that build _Active)
            act.life = CallLife(cfg, now=act.detected_at, hold_since=h.since if h is not None else None)
        step = act.life.observe(now, held=act.held, hold_since=h.since if h is not None else None,
                                positive_at=life.positive_at, negatives=life.negatives)
        if step.regrab is not None:
            self._log("call_regrab", call_id=act.call_id, app=act.app, regrabs=act.life.regrabs, **step.regrab)
            try:
                self.store.update_call(act.call_id, hold_since=step.regrab["new_since"])
            except Exception as exc:
                self._log("error", where="calls.regrab", error=f"{type(exc).__name__}: {exc}")
        if not step.alive:
            self._end(act, step.end or "call_ended", now, life=life)
            return False
        if step.by != act.alive_by:
            self._log("call_alive_by", call_id=act.call_id, app=act.app, by=step.by, was=act.alive_by,
                      held=act.held, positive_age_s=None if life.positive_at is None else round(now - life.positive_at, 1),
                      neg_frames=len(life.negatives))
            act.alive_by = step.by
        act.last_seen = now
        via = self._via(cfg, act, life, now)
        if via != act.via:
            self._log("call_evidence", call_id=act.call_id, app=act.app, via=via, was=act.via)
            act.via = via
            self.store.update_call(act.call_id, via=via)
            self._tell_ui()
        return True

    def _front_is(self, g: _Active, front: dict | None) -> Holder | None:
        """The game's holder when the full-screen window in front is the game's (by its executable; a row adopted
        while the game had let go of the microphone matches by its app key)."""
        if not front:
            return None
        exe = str(front.get("exe") or "")
        if g.holder is not None:
            return g.holder if g.holder.matches(exe=exe) else None
        if exe and app_key(Path(exe).name) == g.key:
            return Holder(app=Path(exe).name, packaged=False, path=exe, since=0.0)
        return None

    def _game_goes_on(self, cfg: CallConfig, g: _Active, now: float) -> bool:
        """One poll of a game voice session's life (:class:`CallLife`): alive while the game holds the microphone,
        for ``release_grace_s`` after it lets go, and while the game is seen in front drawing like a game (its
        "screen": a push-to-talk game may hold the microphone only while the key is down). Ends it otherwise."""
        h = self._holders.get(g.key)
        if h is not None:
            g.holder = h
        g.held = h is not None
        pid = 0
        if self._gaming is not None and self._gaming[0].key == g.key:
            g.gaming_at = now
            pid = int(self._gaming[1].get("pid") or 0)
        else:
            front = self._front_now()
            holder = self._front_is(g, front)
            if holder is not None:
                gaming, _facts = gaming_with_voice(holder, fullscreen=True, shows_page=False, camera=self._camera,
                                                   pids=[int(front.get("pid") or 0)], gpu_fn=self.gpu_fn)
                if gaming:
                    g.gaming_at = now
                    pid = int(front.get("pid") or 0)
        if pid and pid != g.pid:
            if g.pid is not None:
                self._log("game_voice_pid", call_id=g.call_id, app=g.app, was=g.pid, now=pid)
            g.pid = pid
        if g.life is None:
            g.life = CallLife(cfg, now=g.detected_at, hold_since=h.since if h is not None else None)
        step = g.life.observe(now, held=g.held, hold_since=h.since if h is not None else None,
                              positive_at=g.gaming_at, negatives=())
        if step.regrab is not None:
            self._log("call_regrab", call_id=g.call_id, app=g.app, call_kind="game", regrabs=g.life.regrabs, **step.regrab)
            try:
                self.store.update_call(g.call_id, hold_since=step.regrab["new_since"])
            except Exception as exc:
                self._log("error", where="calls.regrab", error=f"{type(exc).__name__}: {exc}")
        if not step.alive:
            self._end(g, step.end or "game_ended", now)
            return False
        if step.by != g.alive_by:
            self._log("call_alive_by", call_id=g.call_id, app=g.app, call_kind="game", by=step.by, was=g.alive_by,
                      held=g.held, gaming_age_s=None if g.gaming_at is None else round(now - g.gaming_at, 1))
            g.alive_by = step.by
        g.last_seen = now
        return True

    def _life(self, act: _Active) -> Any:
        """What the screen says about the open call (:meth:`CallEvidence.life`); empty without evidence."""
        from yuki.memory.callevidence import Life

        evidence = self.evidence
        if evidence is None or not hasattr(evidence, "life"):
            return Life()
        try:
            return evidence.life(act.holder or act.key)
        except Exception:
            return Life()

    @staticmethod
    def _via(cfg: CallConfig, act: _Active, life: Any, now: float) -> str:
        """How sure the open call is: ``screen`` (an in-call frame within ``evidence_fresh_s``, not contradicted
        since), ``mic`` (one of the user's own ``[calls] apps``), else ``screen_stale`` (going on by the
        microphone hold; voice then asks "was that for me?" on ambiguous words)."""
        if life.positive_at is not None and not life.negatives and now - life.positive_at <= cfg.evidence_fresh_s:
            return "screen"
        if act.key in cfg.apps and (life.positive_at is None or life.negatives):
            return "mic"
        return "screen_stale"

    def _heartbeat(self, cfg: CallConfig, act: _Active, now: float) -> None:
        """``last_alive_at`` at most every ``alive_write_s`` while the call lives (what a restart reads)."""
        if act.life is None or act.alive_by == "end_grace" or now - act.alive_written < cfg.alive_write_s:
            return
        act.alive_written = now
        try:
            self.store.update_call(act.call_id, last_alive_at=act.life.alive_at)
        except Exception as exc:
            self._log("error", where="calls.heartbeat", error=f"{type(exc).__name__}: {exc}")

    def _note_holders(self, now: float) -> None:
        """Keep the microphone holders' app keys in :data:`HOLDERS_STATE` when they change (the transcriber's and
        the digest's gates read it, :func:`lingering_call`)."""
        keys = sorted(self._held)
        if keys == self._holders_written:
            return
        try:
            self.store.set_nudge_state(HOLDERS_STATE, json.dumps({"keys": keys, "at": round(now, 1)}))
            self._holders_written = keys
        except Exception as exc:
            self._log("error", where="calls.holders_state", error=f"{type(exc).__name__}: {exc}")

    #: The ``nudge_state`` row that carries who is in the open call (:meth:`MemoryAPI.call_status` reads it).
    PEOPLE_STATE = "call_others"

    def _note_people(self, act: _Active) -> None:
        """How many others its screen shows in the call (0: alone), kept for ``call_status`` on a change (v3.15).

        Live 2026-09-30 the user sat alone in a Discord voice channel waiting for a friend, and voice's call mode
        treated 15 of his 41 turns as said to the people on the call. The count is the screen judge's (a model).
        """
        evidence = self.evidence
        if evidence is None or not hasattr(evidence, "others_of"):
            return
        try:
            others = evidence.others_of(act.key)
        except Exception:
            return
        if others == act.others:
            return
        act.others = others
        try:
            self.store.set_nudge_state(self.PEOPLE_STATE, json.dumps({"call_id": act.call_id, "others": others}))
        except Exception as exc:
            self._log("error", where="calls.people", error=f"{type(exc).__name__}: {exc}")
            return
        self._log("call_people", call_id=act.call_id, app=act.app, others=others)
        self._tell_ui()

    def _begin(self, cfg: CallConfig, key: str, via: str, service: str | None, name: str, now: float) -> _Active:
        # a site's call inside a browser has its own standing answer, never the browser's (see Store.answer_call)
        pref = self.store.call_pref(service) if service else self.store.call_pref(key)
        ok, facts = self._hp(cfg)
        if pref == "never":
            status, consent, by = "declined", "never", "pref"
        elif pref == "always":
            status, consent, by = "asking", "always", "pref"
        elif self._quiet(now):
            status, consent, by = "not_asked", None, "quiet"
        else:
            status, consent, by = "asking", None, None
        holder = self._holders.get(key)
        hold_since = holder.since if holder is not None else None
        call_id = self.store.start_call(app=name, process=key, via=via, service=service, at=now, status=status,
                                        consent=consent, consent_by=by, output_form_factor=facts.get("form_factor"),
                                        output_device=facts.get("device"), headphones=ok, hold_since=hold_since)
        act = _Active(call_id, key, name, via, service, now, now, status=status, headphones=ok, holder=holder,
                      held=holder is not None, alive_written=now)
        act.life = CallLife(cfg, now=now, hold_since=hold_since, held=holder is not None)
        self._log("call_detected", call_id=call_id, app=name, process=key, via=via, service=service,
                  headphones=ok, form_factor=facts.get("form_factor"), asked=status == "asking" and consent is None,
                  hold_since=hold_since, hold_age_s=None if hold_since is None else round(now - hold_since, 1))
        if consent is not None or by == "quiet":
            self._log("call_consent", call_id=call_id, app=name, answer=consent or "none", by=by)
        self._tell_ui()
        return act

    def _begin_game(self, cfg: CallConfig, now: float) -> _Active:
        """A full-screen game's own voice chat began (:attr:`_gaming`): its own row (``kind`` "game"), with the same
        per-app consent as a call. No card can be seen over the game: without a standing answer the row waits as
        ``ask_later`` until one can (:meth:`_ask`)."""
        h, front, facts = self._gaming   # type: ignore[misc]
        key = h.key
        name = cfg.name_of(key, display_name(h))
        if self.after is not None:   # a card still waiting for the last session: this one asks afresh
            self._close_after(now, "new_session")
        pref = self.store.call_pref(key)
        ok, hp = self._hp(cfg)
        if pref == "never":
            status, consent, by = "declined", "never", "pref"
        elif pref == "always":
            status, consent, by = "asking", "always", "pref"
        elif self._quiet(now):
            status, consent, by = "not_asked", None, "quiet"
        else:
            status, consent, by = "ask_later", None, None   # the game is full screen in front: nothing can be seen
        call_id = self.store.start_call(app=name, process=key, via="game", at=now, status=status, consent=consent,
                                        consent_by=by, output_form_factor=hp.get("form_factor"),
                                        output_device=hp.get("device"), headphones=ok, hold_since=h.since, kind="game")
        g = _Active(call_id, key, name, "game", None, now, now, status=status, headphones=ok, holder=h, held=True,
                    alive_written=now, kind="game", pid=int(front.get("pid") or 0) or None, gaming_at=now)
        g.life = CallLife(cfg, now=now, hold_since=h.since, held=True)
        self._log("game_voice_detected", call_id=call_id, app=name, process=key, status=status, headphones=ok,
                  hold_since=h.since, **facts)
        if consent is not None or by == "quiet":
            self._log("call_consent", call_id=call_id, app=name, call_kind="game", answer=consent or "none", by=by)
        self._tell_ui()
        return g

    def _headphones(self, cfg: CallConfig) -> tuple[bool, dict[str, Any]]:
        try:
            return self.headphones_fn(cfg)
        except Exception as exc:
            return False, {"form_factor": None, "device": None, "error": f"{type(exc).__name__}: {exc}"}

    def _set_status(self, act: _Active, status: str, **fields: Any) -> None:
        if status != act.status or fields:
            act.status = status
            self.store.update_call(act.call_id, status=status, **fields)
            self._tell_ui()

    def _drive(self, cfg: CallConfig, act: _Active, now: float) -> None:
        """One row's consent and status (its audio is driven with every row's together: :meth:`_sync_audio`)."""
        row = self.store.call_record(act.call_id)
        if row is None:
            return
        if row.consent is not None and act.status in ("asking", "ask_later") and row.consent_by == "card":
            self._log("call_consent", call_id=act.call_id, app=act.app, call_kind=act.kind, answer=row.consent, by="card",
                      headphones=act.headphones, after_s=round((row.consent_at or now) - act.detected_at, 1))
        if row.status in ("declined", "unanswered") or row.status == "stopped":
            act.status = row.status
            return
        if row.stop_requested:
            self._stop_row_audio(act, "user")
            self._set_status(act, "stopped", stop_reason="user")
            return
        if row.consent is None:
            if act.kind == "game":
                self._ask(cfg, act, row, now)
            elif row.status == "asking" and now - act.detected_at >= cfg.ask_timeout_s:
                if self.store.answer_call(act.call_id, "none", by="ui_timeout"):
                    act.status = "unanswered"
                    self._log("call_consent", call_id=act.call_id, app=act.app, answer="none", by="ui_timeout")
                    self._tell_ui()
            else:
                act.status = row.status
            return
        if row.consent not in ("yes", "always"):
            act.status = row.status
            return
        ok, facts = self._hp(cfg)
        if ok != act.headphones:
            self._log("call_headphones", call_id=act.call_id, ok=ok, form_factor=facts.get("form_factor"))
            act.headphones = ok
            self.store.update_call(act.call_id, headphones=ok, output_form_factor=facts.get("form_factor"),
                                   output_device=facts.get("device"))
        self._set_status(act, "recording" if ok else "waiting_headphones")

    def _ask(self, cfg: CallConfig, r: _Active, row: Any, now: float) -> None:
        """A game row without an answer: its card goes up only when it can be seen (nothing full screen in front),
        comes down again if the game comes back in front (the answer then waits), and counts as unanswered after
        ``ask_timeout_s`` of being up. The status moves only while the row has no answer (never over one)."""
        visible = self._card_visible()
        if row.status == "ask_later":
            if visible and self.store.set_call_status_if(r.call_id, "asking", when_status="ask_later"):
                r.status, r.asking_since = "asking", now
                self._log("game_voice_ask", call_id=r.call_id, app=r.app, after=r is self.after,
                          waited_s=round(now - r.detected_at, 1), up_before_s=round(r.asked_s, 1))
                self._tell_ui()
            else:
                r.status = "ask_later"
            return
        if row.status == "asking":
            if r.asking_since is None:   # adopted while it was up
                r.asking_since = now
            if not visible:
                if self.store.set_call_status_if(r.call_id, "ask_later", when_status="asking"):
                    r.asked_s += now - r.asking_since
                    r.status, r.asking_since = "ask_later", None
                    self._log("game_voice_ask_withdrawn", call_id=r.call_id, app=r.app, up_s=round(r.asked_s, 1))
                    self._tell_ui()
                return
            if r.asked_s + (now - r.asking_since) >= cfg.ask_timeout_s:
                if self.store.answer_call(r.call_id, "none", by="ui_timeout"):
                    r.status = "unanswered"
                    self._log("call_consent", call_id=r.call_id, app=r.app, call_kind=r.kind, answer="none",
                              by="ui_timeout")
                    self._tell_ui()
            return
        r.status = row.status

    def _drive_after(self, cfg: CallConfig, now: float) -> None:
        """The card of a game session that ended before it could be seen: up at the first moment it can be, its
        answer kept (Always / Never hold for the next session; nothing is recorded for the one that ended), then
        the row is ended. Given up after ``5 x ask_timeout_s`` without a moment to show it."""
        a = self.after
        if a is None:
            return
        row = self.store.call_record(a.call_id)
        if row is None:
            self.after = None
            return
        if row.consent is not None:
            if row.consent_by == "card":
                self._log("call_consent", call_id=a.call_id, app=a.app, call_kind=a.kind, answer=row.consent, by="card",
                          after_session=True)
            self.store.update_call(a.call_id, status="ended")
            self.after = None
            self._tell_ui()
            return
        if row.status == "ask_later" and now - (row.ended_at or now) > 5 * cfg.ask_timeout_s:
            self._close_after(now, "never_visible")
            return
        self._ask(cfg, a, row, now)

    def _close_after(self, now: float, reason: str) -> None:
        a, self.after = self.after, None
        if a is None:
            return
        row = self.store.call_record(a.call_id)
        if row is not None and row.consent is None:
            self.store.update_call(a.call_id, consent="none", consent_by="call_ended", consent_at=now)
            self._log("call_consent", call_id=a.call_id, app=a.app, call_kind=a.kind, answer="none", by=reason)
        self.store.update_call(a.call_id, status="ended")
        self._tell_ui()

    # -- audio: one microphone, one track per app ---------------------------------

    def _game_alive(self, g: _Active) -> bool:
        """Whether the game's process still runs (an image of its app in the Toolhelp snapshot, read at most once
        per poll; no process is opened). True when the snapshot cannot be read."""
        if "table" not in self._poll:
            table_fn = self.table_fn
            if table_fn is None:
                from yuki.memory.loopback import process_table as table_fn
            try:
                self._poll["table"] = {app_key(image) for _pid, _parent, image in table_fn()}
            except Exception:
                self._poll["table"] = None
        names = self._poll["table"]
        return names is None or g.key in names

    def _game_in_use(self, cfg: CallConfig, g: _Active, now: float) -> tuple[bool, str]:
        """Whether the user may be talking through the game now, measured at this poll: its full-screen window is in
        front drawing like a game; or, while its process runs, it holds the microphone (ConsentStore) or its own
        capture session was active within ``game_mic_grace_s`` (push-to-talk just released). A closed game is never
        in use, whatever its row's lifetime grace says: the microphone must not outlive the game."""
        if g.gaming_at is not None and g.gaming_at >= now:
            return True, "in_front"
        if not self._game_alive(g):
            return False, "game_closed"
        if g.held:
            return True, "hold"
        seen = self._capture_seen(g.key, now)
        if seen is not None and now - seen <= cfg.game_mic_grace_s:
            return True, "capture_session"
        return False, "game_not_in_use"

    def _sync_audio(self, cfg: CallConfig, now: float) -> None:
        """Every recording follows the rows. The microphone is recorded once: it belongs to the call's row while
        the call may record, else to the game's. It listens on headphones only, and only while what it is recorded
        for is going on: the call's app holds the microphone, or the game is in use (:meth:`_game_in_use`). A game
        never records the microphone while a call the user did not say yes to (asking, declined, unanswered, not
        asked, stopped) holds it: what the user says there is the call's, and the call was not consented. Each
        consented row records its app's own audio (``record_others``, no headphones needed); a game's goes into the
        call's row while the call may record. The capture-session watch runs while any row may record."""
        act, g = self.active, self.game
        call_may = act is not None and act.status in RECORDING_STATUSES
        game_may = g is not None and g.status in RECORDING_STATUSES
        owner = act if call_may else (g if game_may else None)
        for r in (act, g):
            if r is not None and r.session is not None and r is not owner:
                self._stop_recording(r, "moved" if owner is not None else "not_recording", final=owner is None)
        mic = {"state": "off", "call_id": None, "reason": None}
        if owner is not None:
            ok, facts = self._hp(cfg)
            game_use, game_why = self._game_in_use(cfg, g, now) if game_may else (False, "")   # type: ignore[arg-type]
            call_blocks = act is not None and not call_may and act.held
            if owner is act:
                held = act.held or game_use   # type: ignore[union-attr]
                why = "mic_released"
            else:
                held = game_use and not call_blocks
                why = "call_not_consented" if call_blocks else game_why
            session = owner.session
            if not ok or not held:
                reason = "not_headphones" if not ok else why
                if session is not None and session.listening:
                    session.pause()
                    self._log("call_recording_paused", call_id=owner.call_id, reason=reason,
                              **({"form_factor": facts.get("form_factor")} if not ok else {}))
                mic = {"state": "paused", "call_id": owner.call_id, "reason": reason}
            else:
                try:
                    if session is None:
                        self._start_recording(cfg, owner)
                    elif not session.listening:
                        session.resume()
                        self._log("call_recording_resumed", call_id=owner.call_id)
                    mic = {"state": "recording", "call_id": owner.call_id, "reason": None}
                except Exception as exc:   # tried again next poll; the app tracks go on meanwhile
                    self._log("error", where="calls.microphone", error=f"{type(exc).__name__}: {exc}")
                    mic = {"state": "paused", "call_id": owner.call_id, "reason": "mic_error"}
        elif any(r.status in ("asking", "ask_later") for r in self._rows()):
            mic = {"state": "asking", "call_id": None, "reason": None}
        self._mic_state = mic
        target = act if call_may else None
        for r in (act, g):
            if r is not None:
                self._sync_track(cfg, r, target, now)
        self._sync_watch(owner is not None, now)

    def _sync_track(self, cfg: CallConfig, r: _Active, call_target: _Active | None, now: float) -> None:
        """Start, pause, move or stop one row's app track (see :meth:`_sync_audio`)."""
        target = call_target if (r.kind == "game" and call_target is not None) else r
        if not cfg.record_others:
            state, reason = "off", "record_others_off"
        elif r.status in ("asking", "ask_later"):
            state, reason = "asking", None
        elif r.status not in RECORDING_STATUSES:
            state, reason = "off", r.status
        elif self.track_factory is None:
            state, reason = "unavailable", "no_track_source"
        elif r.kind == "call" and not (r.held or r.alive_by == "screen"):
            state, reason = "paused", "mic_released"   # the call may be over: its app's sound is not the call's
        elif r.kind == "game" and not r.pid:
            state, reason = "paused", "no_window"
        elif r.kind == "game" and not self._game_alive(r):
            state, reason = "paused", "game_closed"   # its pid may be reused: never capture a stranger's tree
        else:
            state, reason = "recording", None
        if r.track is not None and (state not in ("recording", "paused") or r.track_target != target.call_id
                                    or (r.kind == "game" and r.pid and r.pid != r.track_pid)):
            self._stop_track(r, reason or ("moved" if r.track_target != target.call_id else "process_changed"))
        if state != "recording":
            if state == "paused" and r.track is not None and r.track.listening:
                r.track.pause()
                self._log("call_track_paused", call_id=r.call_id, app=r.app, call_kind=r.kind, reason=reason)
            if not (state == "paused" and r.track_state == "unavailable"):
                self._track_state(r, state, reason)
            return
        if r.track is not None:
            if not r.track.listening:
                try:
                    r.track.resume()
                    self._log("call_track_resumed", call_id=r.call_id, app=r.app, call_kind=r.kind)
                except Exception as exc:
                    self._track_failed(cfg, r, exc, now)
                    return
            self._track_running(cfg, r, now)
            return
        if r.track_state == "unavailable" and now < r.track_retry_at:
            return
        try:
            pid = r.pid if r.kind == "game" else (self.pid_fn(r.holder) if r.holder is not None else None)
        except Exception as exc:
            self._track_failed(cfg, r, exc, now)
            return
        if not pid:
            self._track_failed(cfg, r, "no_process", now)
            return
        try:
            self._start_track(cfg, r, target, int(pid))
        except Exception as exc:
            self._track_failed(cfg, r, exc, now)
            return
        self._track_running(cfg, r, now)

    def _track_running(self, cfg: CallConfig, r: _Active, now: float) -> None:
        """An open app track's real state: its source opens on its own thread (``starting`` until it runs), and a
        source whose capture thread ended (it could not open; a GetBuffer error; an exception) is a failed track -
        logged with its error, tried again after ``track_retry_s`` - never one that shows "recording" while it
        captures nothing."""
        src = r.track.source if r.track is not None else None
        if src is not None and getattr(src, "alive", True) is False:
            self._track_failed(cfg, r, getattr(src, "error", None) or "capture_stopped", now)
            return
        if src is not None and not getattr(src, "ready", True):
            self._track_state(r, "starting", None)
            return
        if r.track_state != "recording" and src is not None:
            r.track.device = getattr(src, "device_used", None) or r.track.device
            r.track.opened_as = getattr(src, "opened_as", None) or r.track.opened_as
            self._log("call_track_ready", call_id=r.call_id, app=r.app, call_kind=r.kind,
                      opened_as=r.track.opened_as, tried=list(getattr(src, "tried", []) or [])[:4])
        self._track_state(r, "recording", None)

    def _track_state(self, r: _Active, state: str, reason: str | None) -> None:
        if (state, reason) != (r.track_state, r.track_reason):
            r.track_state, r.track_reason = state, reason

    def _track_failed(self, cfg: CallConfig, r: _Active, why: Any, now: float) -> None:
        """The app's own audio could not be opened: the track is ``unavailable`` (the microphone goes on as
        always), tried again after ``track_retry_s``."""
        if r.track is not None:
            self._stop_track(r, "unavailable")
        reason = why if isinstance(why, str) else f"{type(why).__name__}: {why}"
        r.track_retry_at = now + cfg.track_retry_s
        self._track_state(r, "unavailable", reason[:200])
        self._log("call_track_unavailable", call_id=r.call_id, app=r.app, call_kind=r.kind, reason=reason[:300],
                  retry_s=cfg.track_retry_s)

    def _start_track(self, cfg: CallConfig, r: _Active, target: _Active, pid: int) -> None:
        spec = TrackSpec(call_id=r.call_id, app=r.app, process=r.key, kind=r.kind, pid=pid)
        factory = self.track_factory
        session = RecordingSession(
            self.store, call_id=target.call_id, app=r.app, process=r.key, cfg=cfg,
            source_factory=lambda: factory(cfg, spec), log=self._log, start_seq=self._seqs.last(target.call_id),
            track=Track(side="others", app=r.app, process=r.key, kind=r.kind, call_id=r.call_id),
            seq_fn=lambda cid=target.call_id: self._seqs.next(cid),
        )
        session.start()   # a source that opens in the background returns at once (see _track_running)
        r.track, r.track_target, r.track_pid = session, target.call_id, pid
        self._log("call_track_start", call_id=r.call_id, into=target.call_id, app=r.app, call_kind=r.kind, pid=pid,
                  opened_as=session.opened_as, device=session.device, vad=session.chunker.vad is not None)

    def _stop_track(self, r: _Active, reason: str) -> None:
        session, r.track = r.track, None
        into, r.track_target, r.track_pid = r.track_target, None, None
        if session is None:
            return
        stats = session.stop(reason)
        src = session.last_source
        loop = getattr(src, "stats", None)
        restamp = getattr(src, "_restamper", None)
        vad = session.chunker.vad
        self._log("call_track_stop", call_id=r.call_id, into=into, app=r.app, call_kind=r.kind, reason=reason,
                  silence_dropped_s=round(session.chunker.silence_s, 1), breaks=session.chunker.breaks,
                  vad_windows=vad.windows if vad is not None else None,
                  vad_cpu_s=round(vad.cpu_s, 2) if vad is not None else None,
                  packets=getattr(loop, "packets", None), discontinuities=getattr(loop, "discontinuities", None),
                  capture_cpu_s=round(loop.cpu_s, 2) if loop is not None else None,
                  gaps=getattr(restamp, "gaps", None), **summarize(stats))

    def _stop_row_audio(self, r: _Active, reason: str) -> None:
        """Every recording of row ``r``: its microphone (when it owns it), its app track, and any other row's track
        writing into it."""
        if r.session is not None:
            self._stop_recording(r, reason)
        self._stop_track(r, reason)
        for other in self._rows():
            if other is not r and other.track is not None and other.track_target == r.call_id:
                self._stop_track(other, "moved")

    def _start_recording(self, cfg: CallConfig, act: _Active) -> None:
        """The microphone, for row ``act``: its counts and chunk numbers go on from the row's own."""
        row = self.store.call_record(act.call_id)
        base = (RecordingStats(chunks=row.chunks, chunks_silent=row.chunks_silent, audio_s=row.audio_s,
                               speech_s=row.speech_s) if row is not None else None)
        start_seq = self._seqs.last(act.call_id)
        session = RecordingSession(
            self.store, call_id=act.call_id, app=act.app, process=act.key, cfg=cfg,
            source_factory=lambda: self.source_factory(cfg, act.key), log=self._log,
            start_seq=start_seq, base=base,
            track=Track(side="user", app=act.app, process=act.key, kind=act.kind, call_id=act.call_id),
            seq_fn=lambda cid=act.call_id: self._seqs.next(cid),
        )
        session.start()
        act.session = session
        if row is None or row.recording_started_at is None:   # an adopted call keeps its first start
            self.store.update_call(act.call_id, recording_started_at=time.time())
        self._log("call_recording_start", call_id=act.call_id, app=act.app, call_kind=act.kind, device=session.device,
                  opened_as=session.opened_as, vad=session.chunker.vad is not None, spool="encrypted, deferred",
                  **({"continues_seq": start_seq} if start_seq else {}))

    def _stop_recording(self, act: _Active, reason: str, *, final: bool = True) -> None:
        """Close the microphone's recording of row ``act`` (its last chunk is spooled). ``final`` False: the
        microphone moves to another row (the row's own stop reason is left for its end)."""
        session, act.session = act.session, None
        if session is None:
            return
        stats = session.stop(reason)
        info = summarize(stats)
        self.store.update_call(act.call_id, recording_stopped_at=time.time(), **({"stop_reason": reason} if final else {}))
        vad = session.chunker.vad
        self._log("call_recording_stop", call_id=act.call_id, app=act.app, call_kind=act.kind, reason=reason,
                  silence_dropped_s=round(session.chunker.silence_s, 1),
                  vad_windows=vad.windows if vad is not None else None,
                  vad_cpu_s=round(vad.cpu_s, 2) if vad is not None else None, **info)

    def _end(self, act: _Active, reason: str, now: float | None = None, *, life: Any = None, ask: bool = True) -> None:
        """End row ``act``: every recording of it stops (each spools its last chunk), its row is ended. A game row
        whose card could not be seen during the session keeps it for right after (``ask``; never on a pause, a
        disable or the service stopping): the row's time ends now, its card goes up when it can be seen."""
        now = time.time() if now is None else float(now)
        self._stop_row_audio(act, reason)
        row = self.store.call_record(act.call_id)
        if (ask and act.kind == "game" and row is not None and row.consent is None
                and row.status in ("ask_later", "asking")):
            self.store.update_call(act.call_id, ended_at=now, stop_reason=reason)
            self._log("call_ended", call_id=act.call_id, app=act.app, call_kind=act.kind, reason=reason,
                      seconds=round(now - act.detected_at, 1), card="after", **self._end_facts(act, now, life))
            if self.after is not None:
                self._close_after(now, "new_session")
            if row.status == "asking" and act.asking_since is not None:   # the card stays up: its time goes on
                act.asked_s += now - act.asking_since
            act.asking_since = None if row.status == "ask_later" else now
            self.after = act
            self._tell_ui()
            return
        if row is not None and row.consent is None and row.status in ("asking", "ask_later"):
            if row.status == "asking":
                self.store.answer_call(act.call_id, "none", by="call_ended")
            else:   # a game's card that was never shown (a pause, a disable, the service stopping)
                self.store.update_call(act.call_id, consent="none", consent_by="call_ended", consent_at=now)
            self._log("call_consent", call_id=act.call_id, app=act.app, call_kind=act.kind, answer="none", by="call_ended")
        self.store.update_call(act.call_id, status="ended", ended_at=now,
                               **({} if row is not None and row.stop_reason else {"stop_reason": reason}))
        self._log("call_ended", call_id=act.call_id, app=act.app, call_kind=act.kind, reason=reason,
                  seconds=round(now - act.detected_at, 1), **self._end_facts(act, now, life))
        self._tell_ui()

    def _end_facts(self, act: _Active, now: float, life: Any = None) -> dict[str, Any]:
        """What the call's signals were when it ended (content-free, 05 §7.5), so the next unexplained split can
        be read from the log: the hold, how long since the app let go, the screen's newest in-call frame and the
        negatives after it with the frame sizes, what was in front and whether it was full screen."""
        try:
            life = life if life is not None else self._life(act)
            cl = act.life
            held = act.key in self._held
            front = None
            try:
                front = self.foreground_fn()
            except Exception:
                front = None
            front_key = app_key(str(front.get("exe") or "")) if front else None
            if not front_key and self.evidence is not None and hasattr(self.evidence, "front"):
                front_key = self.evidence.front()[0]
            return {
                "held": held, "hold_since": cl.hold_since if cl is not None else None,
                "hold_gap_s": 0.0 if held else (round(now - cl.last_held_at, 1)
                                               if cl is not None and cl.last_held_at is not None else None),
                "regrabs": cl.regrabs if cl is not None else 0,
                "positive_age_s": None if life.positive_at is None else round(now - life.positive_at, 1),
                "neg_frames": len(life.negatives), "last_controls": life.last_controls,
                "median_controls": None if life.median_controls is None else round(life.median_controls, 1),
                "front_app_key": front_key, "fullscreen": front is not None,
            }
        except Exception as exc:   # the log line itself must never stop a call from ending
            return {"facts_error": f"{type(exc).__name__}: {exc}"}

    # -- who takes the microphone: spans for heard_by -------------------------------

    def _capture_watch(self, on_change: Callable[[float, int, bool], None]) -> Any:
        from yuki.memory.loopback import CaptureWatch

        return CaptureWatch(on_change, log=self._log, exclude_pids={os.getpid()})

    def _sync_watch(self, on: bool, now: float) -> None:
        """The capture-session watch runs while a voice session may record: its spans tell which apps were taking
        the microphone at each moment (``heard_by``); a ``watch`` span marks the time it was measured at all."""
        if on and self._watch is None and self.watch_factory is not None:
            try:
                self._watch_span = self.store.open_mic_span(kind="watch", at=now)
                watch = self.watch_factory(self._on_capture)
                watch.start()
                self._watch = watch
                self._log("call_micwatch", state="on", mode=getattr(watch, "mode", None))
            except Exception as exc:
                self._log("error", where="calls.micwatch_start", error=f"{type(exc).__name__}: {exc}")
                if self._watch_span is not None:
                    self.store.close_mic_span(self._watch_span, now)
                    self._watch_span = None
        elif not on and self._watch is not None:
            watch, self._watch = self._watch, None
            mode = getattr(watch, "mode", None)
            try:
                watch.stop()   # reports every process still taking the microphone as stopped now
            except Exception as exc:
                self._log("error", where="calls.micwatch_stop", error=f"{type(exc).__name__}: {exc}")
            with self._span_lock:
                left, self._spans_open = self._spans_open, {}
                self._capture_live, self._capture_last, self._capture_keys = {}, {}, {}   # unknown from now on
            for span_id in left.values():
                self.store.close_mic_span(span_id, now)
            if self._watch_span is not None:
                self.store.close_mic_span(self._watch_span, now)
                self._watch_span = None
            self._log("call_micwatch", state="off", mode=mode, **dict(getattr(watch, "counters", {}) or {}))

    def _on_capture(self, at: float, pid: int, active: bool) -> None:
        """A process started or stopped taking the microphone (called on the watch's thread): a ``capture`` span,
        and the app's capture state for :meth:`_game_in_use`. A change of an open row's app wakes the worker at once
        (a game that closes stops the microphone now, not at the next poll)."""
        key = None
        try:
            with self._span_lock:
                if active:
                    key, name = self._pid_app(pid)
                    if key is None or pid in self._spans_open:
                        return
                    self._capture_live.setdefault(key, set()).add(pid)
                    self._capture_keys[pid] = key
                    self._spans_open[pid] = self.store.open_mic_span(kind="capture", at=at, process=key, app=name,
                                                                     pid=pid)
                else:
                    key = self._capture_keys.pop(pid, None)
                    if key is not None:
                        self._capture_live.get(key, set()).discard(pid)
                        self._capture_last[key] = max(self._capture_last.get(key, 0.0), float(at))
                    span_id = self._spans_open.pop(pid, None)
                    if span_id is not None:
                        self.store.close_mic_span(span_id, at)
        except Exception as exc:
            self._log("error", where="calls.micwatch_span", error=f"{type(exc).__name__}: {exc}")
        if key is not None and key in {r.key for r in self._rows()} and self._event is not None:
            self._event.set()

    def _capture_seen(self, key: str, now: float) -> float | None:
        """When the app ``key`` last had a capture session active: ``now`` while it has one, else the moment its
        last one went inactive; None when the watch never saw one (or is not running)."""
        with self._span_lock:
            if self._capture_live.get(key):
                return now
            return self._capture_last.get(key)

    def _pid_app(self, pid: int) -> tuple[str | None, str | None]:
        """``(app key, display name)`` of a process taking the microphone, from the Toolhelp snapshot's image name
        (no process opened); ``(None, None)`` for Yuki's own processes."""
        hit = self._pid_apps.get(pid)
        if hit is not None:
            return hit
        table_fn = self.table_fn
        if table_fn is None:
            from yuki.memory.loopback import process_table as table_fn
        image = next((img for p, _parent, img in table_fn() if p == pid), "")
        key = app_key(image)
        out: tuple[str | None, str | None]
        if pid == os.getpid() or not key or self._own(pid, image):
            out = (None, None)
        else:
            row = next((r for r in self._rows() if r.key == key), None)
            name = row.app if row is not None else None
            if not name:
                try:
                    from yuki.core import appnames

                    name = appnames.name_for_key(key) or key
                except Exception:
                    name = key
            out = (key, name)
        if len(self._pid_apps) > 256:
            self._pid_apps.clear()
        self._pid_apps[pid] = out
        return out

    def _own(self, pid: int, image: str) -> bool:
        """Whether ``pid`` is one of Yuki's own processes (its interpreters; only a process running the image of
        one of them is looked at more closely)."""
        exclude = getattr(self.signals, "exclude", frozenset()) or frozenset()
        if not exclude or image.casefold() not in {Path(p).name.casefold() for p in exclude}:
            return False
        try:
            import psutil

            return str(Path(psutil.Process(pid).exe())).casefold() in exclude
        except Exception:
            return False

    # -- what the UI reads: every track's state ---------------------------------------

    def track_states(self) -> dict[str, Any]:
        """The microphone's and every app track's state now (content-free; :data:`TRACKS_STATE`)."""
        rows = self._rows()
        return {
            "rows": [r.call_id for r in rows],
            "mic": dict(self._mic_state),
            "tracks": [{"call_id": r.call_id, "app": r.app, "process": r.key, "track_kind": r.kind,
                        "state": r.track_state if r.track_state else "off", "reason": r.track_reason,
                        "into": r.track_target} for r in rows],
            "watch": getattr(self._watch, "mode", None) if self._watch is not None else "off",
            # a row recording but keeping nothing (:meth:`_check_stall`): {"call_id", "app", "kind", "since",
            # "minutes", "reason"} or None
            "stall": dict(self._stall) if self._stall is not None else None,
        }

    def _note_tracks(self) -> None:
        """Keep :meth:`track_states` in :data:`TRACKS_STATE` when it changes (``call_status()`` reads it)."""
        text = json.dumps(self.track_states(), sort_keys=True)
        if text == self._tracks_written:
            return
        try:
            self.store.set_nudge_state(TRACKS_STATE, text)
            self._tracks_written = text
            self._tell_ui()
        except Exception as exc:
            self._log("error", where="calls.tracks_state", error=f"{type(exc).__name__}: {exc}")


__all__ = [
    "CallConfig", "CallWorker", "CallLife", "LifeStep", "lingering_call", "HOLDERS_STATE", "LINGER_S",
    "TRACKS_STATE", "RECORDING_STATUSES", "Chunker", "Chunk", "MicSource", "ArraySource", "RecordingSession",
    "Track", "TrackSpec", "loopback_source", "app_pid", "heard_by",
    "FasterWhisperBackend", "make_backend", "load_backend", "mic_holders", "headphones_now",
    "capture_device_for", "own_executables", "summarize", "Gpu", "RATE", "Segment", "screen_segments",
    "vocabulary_hint", "ChunkTimeout",
]
