"""``VoiceSession``: voice for the whole app — conversations, the wake word, and their threads (docs/VOICE.md §4).

States, and what each one costs:

* **off** (the default): nothing. No thread, no socket, no model, no open
  microphone; ``yuki.voice`` has not even imported Pipecat.
* **listening for "Yuki"** (on by default; tray switch, remembered): the shared microphone is
  open and :class:`~yuki.voice.wake.WakeListener` decodes it locally on one
  thread. No network. Paused while a call or meeting is on (unless
  ``voice_wake_during_calls``), so saying the name to friends does nothing.
* **in a conversation** (the name was heard, or the tray / hotkey): one
  ``yuki-voice`` thread runs ``asyncio.run`` with the Pipecat pipeline
  (:class:`~yuki.voice.pipeline.Conversation`); Deepgram, Bedrock and the TTS
  are connected only for its length. It ends by itself when quiet.

Everything the GUI sees arrives through this object's Qt signals (queued from
the voice and wake threads). Starting while a conversation is on is a no-op;
stopping joins the thread (bounded) and closes the devices it opened.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from yuki.config import Settings
from yuki.voice.log import VoiceLog

#: Where the tray's voice choices are remembered.
PREFS_PATH = Path.home() / "AppData" / "Local" / "Yuki" / "voice.json"

#: How much audio from before the wake word's end is replayed into the conversation
#: (the name itself; the spotter fires as it ends).
WAKE_PREROLL_S = 1.2

#: Environment variable that sets the ElevenLabs voice id (the tray's "Voice id…" wins over it).
VOICE_ID_ENV = "YUKI_VOICE_ID"

#: ``voice.json`` keys of the tray's device choices, by kind (``Settings`` field on the right).
DEVICE_PREFS = {"capture": ("mic_device", "voice_input_device"), "render": ("speaker_device", "voice_output_device")}

#: How often the chosen devices are looked for while the mic is open (a Bluetooth headset
#: switched off must fall back to the default, not leave voice deaf). A status poll of Core
#: Audio (~15 ms, off the GUI thread), not a behaviour timer.
DEVICE_WATCH_MS = 3000


def voice_id(settings: Settings, prefs: dict[str, Any]) -> str:
    """The TTS voice id in force: the tray's choice (``voice.json``), else ``YUKI_VOICE_ID``, else the setting."""
    return str(prefs.get("tts_voice") or os.environ.get(VOICE_ID_ENV) or settings.voice_tts_voice or "").strip()


#: ``voice.json`` keys that set ElevenLabs voice settings (``Settings`` field names on the right).
TTS_PREFS = {"tts_speed": "voice_tts_speed", "tts_stability": "voice_tts_stability",
             "tts_similarity_boost": "voice_tts_similarity_boost", "tts_style": "voice_tts_style",
             "tts_speaker_boost": "voice_tts_speaker_boost"}


def conversation_settings(settings: Settings, prefs: dict[str, Any]) -> Settings:
    """The settings one conversation runs with: the voice id and voice settings from ``voice.json`` applied."""
    from dataclasses import replace

    over: dict[str, Any] = {"voice_tts_voice": voice_id(settings, prefs)}
    for key, name in TTS_PREFS.items():
        value = prefs.get(key)
        if isinstance(value, bool) if name == "voice_tts_speaker_boost" else isinstance(value, (int, float)):
            over[name] = value
    return replace(settings, **over)


def load_prefs(path: Path = PREFS_PATH) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_prefs(prefs: dict[str, Any], path: Path = PREFS_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(prefs, indent=1), encoding="utf-8")
    except OSError:
        pass


class VoiceSession(QObject):
    """Voice for one app run.

    Args:
        settings: Voice settings (``voice_*``).
        runtime: The :class:`~yuki.ui.runtime.AgentRuntime` (the brain).
        memory: :class:`~yuki.agent.memory.MemoryAccess` (rules, portrait, turn log).
        ui_log: The UI's log (``event(kind, **fields)``), for the few UI-side records.
        prefs_path: Where "Listen for 'Yuki'" is remembered.

    Signals:
        state_changed: ``(state, detail)`` — ``off`` · ``wake`` (listening for
            the name) · ``starting`` · ``listening`` · ``hearing`` · ``thinking``
            · ``speaking`` · ``working`` · ``error``.
        conversation_changed: ``True`` when a conversation starts, ``False`` when it ends.
        wake_changed: the "Listen for 'Yuki'" switch.
    """

    state_changed = Signal(str, str)
    conversation_changed = Signal(bool)
    wake_changed = Signal(bool)
    #: ``"Mic: … · Speaker: …"``: the devices voice is using (the pill's and tray's tooltip).
    devices_changed = Signal(str)
    _devices_seen = Signal(object)
    _watch_start = Signal()
    _wake_heard = Signal(float)
    _thread_state = Signal(str, str)
    _thread_done = Signal(str, str)

    def __init__(self, settings: Settings, runtime: Any, memory: Any, ui_log: Any = None,
                 *, prefs_path: Path = PREFS_PATH) -> None:
        super().__init__()
        from yuki.voice.bridge import VoiceBridge

        self.settings = settings
        self.runtime = runtime
        self.memory = memory
        self.ui_log = ui_log
        self.log = VoiceLog(settings.voice_log_dir, settings.project_root / "logs" / "voice_turns.csv")
        self.bridge = VoiceBridge(runtime, self.log, settings=settings)
        self._prefs_path = prefs_path
        prefs = load_prefs(prefs_path)
        self.wake_enabled = bool(prefs.get("wake", settings.voice_wake))
        self.call_active = False
        self.state = "off"
        self._thread: threading.Thread | None = None
        self._conversation: Any = None
        self._mic: Any = None
        self._mic_lock = threading.Lock()  # the voice thread and the GUI thread both open it
        self._listener: Any = None
        self._wake_heard.connect(self._on_wake_heard)
        self._thread_state.connect(self._on_thread_state)
        self._thread_done.connect(self._on_thread_done)
        self._devices_seen.connect(self._on_devices_seen)
        #: The output the current (or last) conversation plays to (``None``: the default).
        self.speaker_used: str | None = None
        self._last_blocks = -1
        self._watching = False
        from PySide6.QtCore import QTimer

        self._device_timer = QTimer(self)
        self._device_timer.setInterval(DEVICE_WATCH_MS)
        self._device_timer.timeout.connect(self._watch_devices)
        self._watch_start.connect(self._device_timer.start)  # the mic may open on the voice thread

    # -- public (GUI thread) ------------------------------------------------------

    @property
    def in_conversation(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """App start: listen for the name unless the user turned that off (it is on by default). Nothing else starts."""
        if self.wake_enabled:
            self._apply_wake()

    def toggle_conversation(self, via: str = "hotkey") -> None:
        if self.in_conversation:
            self.stop_conversation(reason=via)
        else:
            self.start_conversation(via)

    def start_conversation(self, via: str = "tray", *, wake_at: float | None = None) -> bool:
        """Start a conversation. A no-op (``False``) when one is already on."""
        if self.in_conversation:
            return False
        session = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        self._set_state("starting", via)
        if self._listener is not None:
            self._listener.pause()
        self._thread = threading.Thread(target=self._thread_main, args=(session, via, wake_at),
                                        name="yuki-voice", daemon=True)
        self._thread.start()
        self.conversation_changed.emit(True)
        return True

    def stop_conversation(self, reason: str = "user", *, wait_s: float = 0.0) -> None:
        conversation = self._conversation
        if conversation is not None:
            conversation.request_stop(reason)
        if wait_s and self._thread is not None:
            self._thread.join(timeout=wait_s)

    def set_wake(self, enabled: bool) -> None:
        """The tray's "Listen for 'Yuki'" switch (remembered)."""
        self.wake_enabled = bool(enabled)
        prefs = load_prefs(self._prefs_path)
        prefs["wake"] = self.wake_enabled
        save_prefs(prefs, self._prefs_path)
        self.log.event("wake", state="enabled" if enabled else "disabled")
        self._apply_wake()
        self.wake_changed.emit(self.wake_enabled)

    # -- devices (tray "Microphone" / "Speaker") --------------------------------------

    def device_choice(self, kind: str) -> str | None:
        """The device the user chose for ``kind`` (``capture`` / ``render``), ``None``: system default."""
        key, field = DEVICE_PREFS[kind]
        chosen = load_prefs(self._prefs_path).get(key)
        return str(chosen) if chosen else getattr(self.settings, field, None)

    def set_device(self, kind: str, name: str | None) -> None:
        """The tray's choice: remembered in ``voice.json``; the mic switches at once (wake word and
        a running conversation keep listening through it), the speaker from the next conversation."""
        key, _field = DEVICE_PREFS[kind]
        prefs = load_prefs(self._prefs_path)
        if name:
            prefs[key] = name
        else:
            prefs.pop(key, None)
        save_prefs(prefs, self._prefs_path)
        self.log.event("device", kind=kind, state="chosen" if name else "default")
        if kind == "capture":
            self._reopen_mic()
        self._emit_devices()

    def devices_text(self) -> str:
        """What voice uses now, for tooltips: ``Mic: … · Speaker: …``."""
        mic = self._mic.device_used if self._mic is not None and self._mic.is_open else None
        speaker = self.speaker_used if self.in_conversation else None
        mic_text = mic or self.device_choice("capture") or "system default"
        speaker_text = speaker or self.device_choice("render") or "system default"
        return f"Mic: {mic_text} · Speaker: {speaker_text}"

    def _emit_devices(self) -> None:
        self.devices_changed.emit(self.devices_text())

    def _resolve(self, kind: str) -> str | None:
        """The chosen device if it is connected, else ``None`` (the default) with a note in the pill."""
        from yuki.voice.audio import audio_devices, resolve_device

        wanted = self.device_choice(kind)
        if not wanted:
            return None
        name, note = resolve_device(kind, wanted, audio_devices())
        if note:
            self.log.event("device", kind=kind, state="missing_fallback")
            self.state_changed.emit("note", note)
        return name

    def _reopen_mic(self) -> None:
        """Open the microphone again on the device in force now (the subscribers stay)."""
        with self._mic_lock:
            mic = self._mic
            if mic is None:
                return
            was_open = mic.is_open
            mic.device = self._resolve("capture")
            if was_open:
                mic.close()
                try:
                    facts = mic.open()
                    self.log.event("mic", state="reopened", **facts)
                except Exception as exc:  # noqa: BLE001 - shown in the pill, never raised into Qt
                    self.log.event("mic", state="error", error=f"{type(exc).__name__}: {exc}")
                    self.state_changed.emit("error", f"Can't open the microphone: {exc}")

    def _watch_devices(self) -> None:
        """Every few seconds while the mic is open: is the chosen device still there, is audio arriving?"""
        mic = self._mic
        if mic is None or not mic.is_open:
            self._device_timer.stop()
            return
        if self._watching:
            return
        self._watching = True
        blocks = mic.stats.blocks
        stalled = blocks == self._last_blocks  # no audio at all for a whole tick: the stream died
        self._last_blocks = blocks

        def look() -> None:
            from yuki.voice.audio import audio_devices

            self._devices_seen.emit({"devices": audio_devices(), "stalled": stalled})

        threading.Thread(target=look, name="yuki-voice-devices", daemon=True).start()

    def _on_devices_seen(self, seen: object) -> None:
        self._watching = False
        info = seen if isinstance(seen, dict) else {}
        devices = info.get("devices") or {}
        mic = self._mic
        if mic is None or not mic.is_open or devices.get("error"):
            return
        wanted = self.device_choice("capture")
        present = devices.get("capture") or []
        gone = bool(wanted) and mic.device == wanted and wanted not in present
        back = bool(wanted) and mic.device is None and wanted in present
        if gone or back or info.get("stalled"):
            self.log.event("device", kind="capture", state="gone" if gone else "back" if back else "stalled")
            self._reopen_mic()
            self._emit_devices()
        speaker = self.speaker_used
        if self.in_conversation and speaker and speaker not in (devices.get("render") or []):
            self.log.event("device", kind="render", state="gone")
            self.state_changed.emit("note", f"Speaker '{speaker}' disconnected; ending this conversation")
            self.stop_conversation("speaker_gone")

    @property
    def tts_voice(self) -> str:
        """The voice id conversations use (empty: the provider's default)."""
        return voice_id(self.settings, load_prefs(self._prefs_path))

    def set_tts_voice(self, voice: str) -> None:
        """The tray's "Voice id…": remembered in ``voice.json``; the next conversation uses it (empty: default)."""
        prefs = load_prefs(self._prefs_path)
        voice = (voice or "").strip()
        if voice:
            prefs["tts_voice"] = voice
        else:
            prefs.pop("tts_voice", None)
        save_prefs(prefs, self._prefs_path)
        from yuki.voice.pipeline import forget_tts_check

        forget_tts_check()
        self.log.event("voice_id", state="set" if voice else "default", chars=len(voice))

    def on_call_status(self, status: object) -> None:
        """Memory's call status changed (``CallController.changed``): gate the wake word."""
        call = status.get("call") if isinstance(status, dict) else None
        active = call is not None
        if active != self.call_active:
            self.call_active = active
            self.log.event("wake", state="call_on" if active else "call_off")
            self._apply_wake()

    def owns(self, job_id: int) -> bool:
        """Whether a request came from voice (its questions are spoken, not popped up)."""
        return self.bridge.owns(job_id)

    def interrupt(self) -> None:
        """Cut Yuki's speech now (the cancel hotkey)."""
        if self._conversation is not None:
            self._conversation.interrupt()

    def shutdown(self) -> None:
        """App exit: end the conversation (bounded join), stop listening, close the mic."""
        self.stop_conversation("quit", wait_s=5.0)
        self._stop_listener()
        self._close_mic()

    # -- wake word ------------------------------------------------------------------

    def _wake_allowed(self) -> bool:
        return self.wake_enabled and (self.settings.voice_wake_during_calls or not self.call_active)

    def _apply_wake(self) -> None:
        if self._wake_allowed():
            try:
                self._ensure_listener()
                if not self.in_conversation:
                    self._set_state("wake", "")
            except Exception as exc:  # noqa: BLE001 - shown, never raised into the tray
                self._set_state("error", f"Can't listen for 'Yuki': {exc}")
                self.log.event("wake", state="error", error=f"{type(exc).__name__}: {exc}")
        else:
            self._stop_listener()
            if not self.in_conversation:
                self._close_mic()
                self._set_state("off", "call" if self.wake_enabled and self.call_active else "")

    def _ensure_mic(self) -> Any:
        from yuki.voice.audio import SharedMic

        with self._mic_lock:
            if self._mic is None:
                self._mic = SharedMic(None)
            if not self._mic.is_open:
                self._mic.device = self._resolve("capture")
                facts = self._mic.open()
                self.log.event("mic", state="open", **facts)
                self._last_blocks = -1
                self._watch_start.emit()
                self._emit_devices()
            return self._mic

    def _close_mic(self) -> None:
        with self._mic_lock:
            if self._mic is not None and self._mic.is_open:
                self._mic.close()
                self.log.event("mic", state="closed", blocks=self._mic.stats.blocks,
                               overflows=self._mic.stats.overflows)

    def _ensure_listener(self) -> None:
        from yuki.voice.wake import WakeListener, WakeSpotter

        mic = self._ensure_mic()
        if self._listener is None or not self._listener.running:
            s = self.settings
            self._listener = WakeListener(
                mic, lambda: WakeSpotter(threshold=s.voice_wake_threshold, boost=s.voice_wake_boost),
                on_wake=lambda at, _kw: self._wake_heard.emit(at), log=self.log,
            )
            self._listener.start()
            self.log.event("wake", state="listening")
        if self.in_conversation:
            self._listener.pause()
        else:
            self._listener.resume()

    def _stop_listener(self) -> None:
        if self._listener is not None:
            listener, self._listener = self._listener, None
            listener.stop()
            heard = listener.heard_s
            self.log.event("wake", state="stopped", heard_s=round(heard, 1), decoded_s=round(listener.audio_s, 1),
                           cpu_pct_one_core=round(100 * listener.decode_s / heard, 3) if heard else None,
                           near_misses=listener.near_misses)

    def _on_wake_heard(self, at: float) -> None:
        if not self._wake_allowed() or self.in_conversation:
            return
        self.start_conversation("wake", wake_at=at)

    # -- the conversation thread ------------------------------------------------------

    def _set_state(self, state: str, detail: str) -> None:
        self.state = state
        self.state_changed.emit(state, detail)

    def _on_thread_state(self, state: str, detail: str) -> None:
        if self.in_conversation:
            self._set_state(state, detail)

    def _on_thread_done(self, reason: str, error: str) -> None:
        self._thread = None
        self._conversation = None
        self.speaker_used = None
        self._emit_devices()
        self.conversation_changed.emit(False)
        if error:
            self._set_state("error", error)
        if self._wake_allowed():
            self._apply_wake()
            if error:
                self._set_state("error", error)
        else:
            self._close_mic()
            if not error:
                self._set_state("off", reason)

    def _thread_main(self, session: str, via: str, wake_at: float | None) -> None:
        """The ``yuki-voice`` thread: preflight, devices, then the pipeline on its own asyncio loop."""
        from yuki.voice.audio import Speaker
        from yuki.voice.mouth import fetch_memory
        from yuki.voice.pipeline import Conversation, VoiceUnavailable, preflight

        from dataclasses import replace

        started = time.monotonic()
        reason, error = "error", ""
        speaker = None
        settings = conversation_settings(self.settings, load_prefs(self._prefs_path))
        try:
            # The chosen output if it is connected (else the default, with a note); the
            # headphone rule is checked on that very device.
            settings = replace(settings, voice_output_device=self._resolve("render"))
            facts = preflight(settings)
            if facts.get("tts_voice"):
                # The chosen voice was refused (e.g. needs a paid plan): this conversation only.
                settings = replace(settings, voice_tts_voice=facts["tts_voice"])
                self.log.event("voice_id", state="refused_fallback", note=facts.get("tts_note"))
            mouth_memory = fetch_memory(self.memory)
            mic = self._ensure_mic()
            speaker = Speaker(24_000, settings.voice_output_device)
            out_facts = speaker.open()
            self.speaker_used = out_facts.get("device")
            self.devices_changed.emit(self.devices_text())
            from pipecat.transports.base_transport import TransportParams

            from yuki.voice.transport import MicInput, PairTransport, SpeakerOutput

            params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16_000,
                                     audio_out_enabled=True, audio_out_sample_rate=24_000)
            since = (wake_at - WAKE_PREROLL_S) if wake_at is not None else None
            transport = PairTransport(MicInput(mic, params, backlog_since=since), SpeakerOutput(speaker, params))
            conversation = Conversation(
                settings=settings, bridge=self.bridge, transport=transport, memory=self.memory,
                mouth_memory=mouth_memory, log=self.log,
                speech_end=lambda: mic.stats.last_voiced or None,
                voice_activity=lambda: (mic.stats.voiced_since, mic.stats.last_voiced),
                on_state=lambda st, d: self._thread_state.emit(st, d), session=session,
                tts_provider=facts.get("tts_provider"),
            )
            if facts.get("tts_note"):
                self._thread_state.emit("starting", facts["tts_note"])
            self._conversation = conversation
            self.log.event("session", state="start", session=session, via=via, mic=mic.device_used,
                           mic_opened_as=mic.opened_as, output=out_facts.get("device"),
                           headphones=facts.get("output"), stt=self.settings.voice_stt_model,
                           mouth=self.settings.voice_mouth_model, tts=facts.get("tts_provider"),
                           tts_note=facts.get("tts_note"),
                           tts_model=self.settings.voice_tts_model, tts_voice_set=bool(settings.voice_tts_voice),
                           memory=mouth_memory.facts())
            reason = asyncio.run(conversation.run())
            if conversation.observer.fatal:
                error = _plain_error(conversation.observer.fatal)
        except VoiceUnavailable as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - reported, never raised into Qt
            error = _plain_error(f"{type(exc).__name__}: {exc}")
        finally:
            if speaker is not None:
                speaker.close()
            if self._mic is not None:
                self.log.event("mic", state="levels", **self._mic.stats.levels())
            obs = getattr(self._conversation, "observer", None)
            self.log.event("session", state="stop", session=session, reason=reason, error=error or None,
                           seconds=round(time.monotonic() - started, 1),
                           turns=obs.totals["turns"] if obs else 0,
                           mouth_cost_usd=round(obs.totals["mouth_cost_usd"], 5) if obs else 0,
                           tts_chars=obs.totals["tts_chars"] if obs else 0,
                           stt_cost_usd=round((time.monotonic() - started) / 60 * self.settings.voice_usd_per_stt_min, 5))
            self._thread_done.emit(reason, error)


def _plain_error(text: str) -> str:
    """A service error in words the user can act on (the raw text stays in the voice log)."""
    low = text.lower()
    if "401" in low or "unauthor" in low or "permission" in low or "invalid api key" in low:
        return "Voice can't sign in to a speech service (check the API keys and their permissions)."
    if "getaddrinfo" in low or "connect" in low or "timed out" in low or "network" in low:
        return "Voice can't reach the speech services right now (network?)."
    return f"Voice stopped: {text[:160]}"
