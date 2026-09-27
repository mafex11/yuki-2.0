"""``VoiceSession``: voice for the whole app — conversations, the wake word, and their threads (docs/VOICE.md §4).

States, and what each one costs:

* **off** (the default): nothing. No thread, no socket, no model, no open
  microphone; ``yuki.voice`` has not even imported Pipecat.
* **listening for "Yuki"** (on by default; tray switch, remembered): the shared microphone is
  open and :class:`~yuki.voice.wake.WakeListener` decodes it locally on one
  thread. No network. It keeps listening during calls (``voice_wake_during_calls``,
  on since v3.4): a conversation started in a call runs in call mode (below).
* **in a conversation** (the name was heard, or the tray / hotkey): one
  ``yuki-voice`` thread runs ``asyncio.run`` with the Pipecat pipeline
  (:class:`~yuki.voice.pipeline.Conversation`); Deepgram, Bedrock and the TTS
  are connected only for its length. It ends by itself when quiet.

On speakers the conversation's :class:`~yuki.voice.echo.EchoGuard` keeps Yuki from
hearing herself (WebRTC echo cancellation, else half duplex; docs/VOICE.md §11). In
a voice call the conversation is in **call mode** (docs/VOICE.md §25): the mouth
judges whether each utterance was for Yuki, and the conversation ends soon after the
last exchange with her. A wake is acknowledged at once (a chime and "Listening");
everything said from the name on is kept in memory and reaches speech recognition
once it is connected. A conversation that cannot start, or ends in an error, says
so with a tone and the pill (:meth:`VoiceSession.notice`), never silently.

Everything the GUI sees arrives through this object's Qt signals (queued from
the voice and wake threads). Starting while a conversation is on is a no-op
(a wake heard while one is closing starts the next one right after it);
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

#: How long a wake heard while a conversation was closing may wait to start the next one (s).
PENDING_WAKE_S = 8.0
#: A reopen that keeps failing is logged the first time and then every this many tries.
REOPEN_LOG_EVERY = 20

#: Where the tray's voice choices are remembered.
PREFS_PATH = Path.home() / "AppData" / "Local" / "Yuki" / "voice.json"

#: How much audio from before the spotter's batch is replayed into the conversation when the spotter
#: could not say where the name ended (the name itself is then replayed with the words after it).
#: With the name's end known (v3.6, :attr:`yuki.voice.wake.WakeListener.name_end`), only what was said
#: after the name is replayed, so the name never reaches speech recognition.
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
    #: The chat UI's events (v3.5, docs/VOICE.md §12), queued into the GUI thread. Words, for the screen only.
    #: ``(text, ts, turn_id)``: a final transcript (``turn_closed`` in ``chat_event`` later says whether it was
    #: addressed to Yuki or to someone else).
    user_utterance = Signal(str, float, str)
    #: ``(turn_id, text_so_far, final, interrupted)``: Yuki's words as they become audible; on a barge-in the
    #: last one is ``final`` and ``interrupted``, cut at what was heard.
    assistant_speech = Signal(str, str, bool, bool)
    #: ``(turn_id, name, args_summary, ok, result_summary, ms)``: one of the talker's hands finished.
    hand_step = Signal(str, str, str, bool, str, int)
    #: ``(job_id, state, summary)``: a thinker job was submitted / running / step / waiting_for_answer /
    #: done / failed / cancelled.
    job_event = Signal(int, str, str)
    #: Every one of the above as a dict ``{"type": "user_utterance" | "assistant_speech" | "hand_step" |
    #: "job_event" | "turn_closed", ...}``, for a UI that wants one stream.
    chat_event = Signal(object)
    _devices_seen = Signal(object)
    _watch_start = Signal()
    _wake_heard = Signal(float, float, float)
    _thread_state = Signal(str, str)
    _thread_done = Signal(str, str)
    _closing = Signal(str)

    #: The device classes (a test or the offline harness replaces them with files; rule 7).
    mic_factory: Any = None
    speaker_factory: Any = None
    #: ``(pcm, rate, device)``: plays a notice's tone (replaced in tests: never the real speakers).
    play_tone: Any = None
    #: Extra Pipecat observers for every conversation (the offline harness's timeline; empty in the app).
    extra_observers: tuple = ()

    def __init__(self, settings: Settings, runtime: Any, memory: Any, ui_log: Any = None,
                 *, prefs_path: Path = PREFS_PATH) -> None:
        super().__init__()
        from yuki.voice.bridge import VoiceBridge

        self.settings = settings
        self.runtime = runtime
        self.memory = memory
        self.ui_log = ui_log
        # The turn CSV sits next to the voice log folder: logs/voice_turns.csv in the app, the run's own
        # temp folder in tests and the offline harness (their turns must never mix with the user's).
        self.log = VoiceLog(settings.voice_log_dir, settings.voice_log_dir.parent / "voice_turns.csv")
        self.bridge = VoiceBridge(runtime, self.log, settings=settings)
        self.bridge.on_job_event = self._job_event
        self._prefs_path = prefs_path
        prefs = load_prefs(prefs_path)
        self.wake_enabled = bool(prefs.get("wake", settings.voice_wake))
        self.call_active = False
        #: The app of the voice call going on (call mode), ``None`` when there is none.
        self.call_app: str | None = None
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
        self._closing.connect(self._on_closing)
        #: A conversation decided to end and is tearing down (the wake word listens again).
        self.closing = False
        self._pending_wake: tuple[float, float, float] | None = None
        #: When each failure reason was last announced with a tone (``notice``).
        self._noticed: dict[str, float] = {}
        self._reopen_failures = 0
        #: The echo canceller, kept across conversations on the same output (it stays adapted).
        self._aec: dict[str, Any] = {}
        self._guard: Any = None
        #: The running conversation's output (the tray's Speaker choice moves it live).
        self._speaker: Any = None
        #: Wake-to-ready timings of the last conversation (ms), for the log and the tests.
        self.last_timings: dict[str, Any] = {}
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
        """App start: listen for the name unless the user turned that off (it is on by default).

        With the wake word on and ``voice_prewarm``, the conversation's code is loaded in the background
        a little later (:meth:`prewarm`), so the first "Yuki" is not a cold start.
        """
        if self.wake_enabled:
            self._apply_wake()
            if self.settings.voice_prewarm:
                from PySide6.QtCore import QTimer

                QTimer.singleShot(int(self.settings.voice_prewarm_delay_s * 1000), self.prewarm)

    def prewarm(self) -> None:
        """Load what a conversation needs, off the GUI thread, once per app run (docs/VOICE.md §26, v3.5).

        Live 2026-09-26 the first "Yuki" after start-up took 8.5 s to reach speech recognition: 2.9 s
        importing Pipecat and the services, then 5.6 s more on the voice thread for the imports, the
        TLS set-up and the one-off checks the build does. Here, in the background: the imports, the
        stream decoders the first reply would load, the TTS can-speak check (one request, cached for
        the app run), the TLS context and the services' addresses, and the volume mixer's COM. No
        connection is kept open (a speech-recognition socket costs money while open); a warm
        conversation opens its three connections in ~1.2 s. Logged as ``prewarm`` (ms, RSS).
        """
        if getattr(self, "_prewarmed", False) or self.in_conversation:
            return
        self._prewarmed = True
        settings = conversation_settings(self.settings, load_prefs(self._prefs_path))
        log = self.log
        self._prewarm_job = _background(lambda: _prewarm(settings, log), "yuki-voice-prewarm")

    def toggle_conversation(self, via: str = "hotkey") -> None:
        if self.in_conversation:
            self.stop_conversation(reason=via)
        else:
            self.start_conversation(via)

    def start_conversation(self, via: str = "tray", *, wake_at: float | None = None,
                           heard_at: float | None = None, name_end: float | None = None) -> bool:
        """Start a conversation. A no-op (``False``) when one is already on.

        ``name_end`` is when the words said after the name began (the spotter's own token times):
        what was said from there on is replayed into speech recognition, and the name is not (v3.6).
        Without it, ``wake_at`` (when the audio batch with the name began) is used, with the name.
        ``heard_at`` is when the spotter heard it (the timings start there). A tray or hotkey start
        keeps what is said from the press on the same way.
        """
        if self.in_conversation:
            return False
        session = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        started = heard_at if heard_at is not None else time.monotonic()
        if name_end:
            listen_from = name_end
        else:
            listen_from = (wake_at - WAKE_PREROLL_S) if wake_at is not None else time.monotonic()
        self.closing = False
        self._set_state("starting", via)
        if self._listener is not None:
            self._listener.pause()
        self._thread = threading.Thread(target=self._thread_main,
                                        args=(session, via, listen_from, started, self.call_app, bool(name_end)),
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
        """The tray's choice: remembered in ``voice.json``, and applied at once. The mic switches
        (the wake word and a running conversation keep listening through it); a running
        conversation's speaker moves to the new output between two writes (echo mode re-chosen
        from the new output's form factor); otherwise the next conversation opens it."""
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
        elif self.in_conversation and self._speaker is not None:
            threading.Thread(target=self._switch_output, name="yuki-voice-switch", daemon=True).start()
        self._emit_devices()

    def device_in_use(self, kind: str) -> str | None:
        """The device voice has open now for ``kind`` (``capture`` / ``render``), ``None`` when none is."""
        if kind == "capture":
            mic = self._mic
            return mic.device_used if mic is not None and mic.is_open else None
        return self.speaker_used if self.in_conversation else None

    def _output_target(self, chosen: str | None) -> str | None:
        """The output to open by name: the tray's choice, else Windows' default output as Core Audio
        names it now (PortAudio's own "default" is whatever it was when PortAudio started)."""
        if chosen or self.speaker_factory is not None:
            return chosen
        try:
            from yuki.voice.audio import audio_devices

            return audio_devices().get("default_render")
        except Exception:  # noqa: BLE001 - PortAudio's default then
            return None

    def _refresh_audio_devices(self) -> bool:
        """Re-read PortAudio's device list (a device connected after it started is missing from it).

        PortAudio can only re-read with no stream open, so the mic's stream closes for a moment:
        its kept audio and its subscribers (the wake word, a running conversation) stay.
        """
        from yuki.voice.audio import refresh_devices

        with self._mic_lock:
            mic = self._mic
            was_open = mic is not None and mic.is_open
            if was_open:
                mic.close(keep_backlog=True)
            try:
                ok = refresh_devices()
            finally:
                if was_open:
                    try:
                        mic.open()
                    except Exception as exc:  # noqa: BLE001 - the device watch retries it
                        self.log.event("mic", state="error", error=f"{type(exc).__name__}: {exc}")
        self.log.event("device", state="list_refreshed" if ok else "list_refresh_refused")
        return ok

    def _open_output(self, chosen: str | None) -> tuple[Any, dict[str, Any]]:
        """Open the output voice speaks on, by name: the tray's choice, else Windows' default now.

        If PortAudio does not have that device (connected after the app started: live
        2026-09-26, the tray's "Headphones (WH-XB900N)" played on the M-Audio speakers), its list
        is re-read and the device opened again; if it is still missing, the default plays and
        the pill says so. Returns ``(speaker, facts)``: ``wanted`` / ``device`` (opened) /
        ``found`` / ``refreshed`` / ``index``.
        """
        from yuki.voice.audio import Speaker

        factory = self.speaker_factory or Speaker
        target = self._output_target(chosen)
        speaker = factory(24_000, target)
        try:
            facts = speaker.open()
        except Exception as exc:  # noqa: BLE001 - PortAudio's list may predate this output (or it changed): re-read once
            # The installed app's first voice start after a launch failed here with "Invalid device" and every
            # retry worked (2026-09-27): PortAudio reads its device list once, at start, which can be before the
            # output (a Bluetooth headset) is up; its old default index then points at nothing. Re-read, retry.
            self.log.event("device", kind="render", state="open_retry", error=f"{type(exc).__name__}: {str(exc)[:160]}")
            speaker.close()
            self._refresh_audio_devices()
            speaker = factory(24_000, target)
            facts = speaker.open()
        refreshed = False
        if target and not facts.get("found", True):
            speaker.close()
            refreshed = self._refresh_audio_devices()
            speaker = factory(24_000, target)
            facts = speaker.open()
        facts["refreshed"] = refreshed
        facts["tray_choice"] = chosen
        return speaker, facts

    def _output_note(self, facts: dict[str, Any]) -> str:
        """What the pill says when the output opened is not the one wanted ("" when it is)."""
        wanted = facts.get("wanted")
        if wanted and not facts.get("found", True):
            return f"Couldn't open '{wanted}'; Yuki is speaking on {facts.get('device')}"
        return ""

    def _switch_output(self) -> None:
        """The tray's Speaker changed during a conversation: move the output, re-choose the echo mode."""
        from yuki.voice.audio import headphones_check
        from yuki.voice.echo import choose_mode

        speaker = self._speaker
        if speaker is None or not hasattr(speaker, "switch"):
            self.state_changed.emit("note", "The new speaker is used from the next conversation")
            return
        try:
            target = self._output_target(self._resolve("render"))
            facts = speaker.switch(target)
            refreshed = False
            if target and not facts.get("found", True):
                box: list[bool] = []
                facts = speaker.switch(target, between=lambda: box.append(self._refresh_audio_devices()))
                refreshed = bool(box and box[0])
            on_headphones, heard = headphones_check(self.settings.voice_headphone_devices, device=facts.get("device"))
            mode, why = choose_mode(on_headphones, preference=self.settings.voice_echo)
            mic = self._mic
            guard = self._echo_guard(mode, facts.get("device"), speaker, mic)
            if mic is not None:
                mic.set_echo(guard)
            self._guard = guard
            self.speaker_used = facts.get("device")
            self.log.event("device", kind="render", state="switched", wanted=target, opened=facts.get("device"),
                           found=facts.get("found"), refreshed=refreshed, echo=mode, echo_why=why,
                           headphones=heard.get("form_factor") if isinstance(heard, dict) else None)
            mic_name = self.device_in_use("capture") or "the default"
            note = self._output_note(facts) or f"Speaker now {facts.get('device')}; mic: {mic_name}"
            self.state_changed.emit("note", note)
        except Exception as exc:  # noqa: BLE001 - the old output may be gone: say so
            self.log.event("device", kind="render", state="switch_error", error=f"{type(exc).__name__}: {exc}")
            self.state_changed.emit("error", f"Couldn't switch the speaker: {exc}")
        self.devices_changed.emit(self.devices_text())

    def devices_text(self) -> str:
        """What voice uses now, for tooltips: ``Mic: … · Speaker: …``."""
        mic = self._mic.device_used if self._mic is not None and self._mic.is_open else None
        speaker = self.speaker_used if self.in_conversation else None
        mic_text = mic or self.device_choice("capture") or "system default"
        speaker_text = speaker or self.device_choice("render") or "system default"
        return f"Mic: {mic_text} · Speaker: {speaker_text}"

    def _emit_devices(self) -> None:
        self.devices_changed.emit(self.devices_text())

    def _resolve(self, kind: str, *, quiet: bool = False) -> str | None:
        """The chosen device if it is connected, else ``None`` (the default) with a note in the pill
        (``quiet``: no note, for the device watch's retries)."""
        from yuki.voice.audio import audio_devices, resolve_device

        wanted = self.device_choice(kind)
        if not wanted:
            return None
        name, note = resolve_device(kind, wanted, audio_devices())
        if note and not quiet:
            self.log.event("device", kind=kind, state="missing_fallback")
            self.state_changed.emit("note", note)
        return name

    def _reopen_mic(self, *, retry: bool = False) -> None:
        """Open the microphone again on the device in force now (the subscribers stay).

        ``retry``: the mic should be open but an earlier reopen failed (the device was gone,
        or the PC was waking up): try again. The pill says so only the first time.
        """
        with self._mic_lock:
            mic = self._mic
            if mic is None:
                return
            was_open = mic.is_open
            if not (was_open or retry):
                return
            mic.device = self._resolve("capture", quiet=retry and self._reopen_failures > 0)
            if was_open:
                mic.close()
            try:
                facts = mic.open()
                self.log.event("mic", state="reopened", tries=self._reopen_failures + 1, **facts)
                self._reopen_failures = 0
            except Exception as exc:  # noqa: BLE001 - shown in the pill, never raised into Qt
                self._reopen_failures += 1
                if self._reopen_failures == 1 or self._reopen_failures % REOPEN_LOG_EVERY == 0:
                    self.log.event("mic", state="error", tries=self._reopen_failures,
                                   error=f"{type(exc).__name__}: {exc}")
                if self._reopen_failures == 1:
                    self.state_changed.emit("error", f"Can't open the microphone: {exc}")

    def _mic_wanted(self) -> bool:
        return self.in_conversation or self._wake_allowed()

    def _watch_devices(self) -> None:
        """Every few seconds while the mic should be open: is the chosen device still there, is
        audio arriving, is the wake listener alive? A device change or the PC waking from sleep
        stops the stream; it is reopened (and retried until it opens), never left deaf."""
        mic = self._mic
        if mic is None or not self._mic_wanted():
            self._device_timer.stop()
            return
        if not mic.is_open:
            self._reopen_mic(retry=True)
            if mic.is_open:
                self._last_blocks = -1
                self._emit_devices()
            return
        if self._wake_allowed() and not self.in_conversation and (
                self._listener is None or not self._listener.running):
            self.log.event("wake", state="restarted")
            try:
                self._ensure_listener()
            except Exception as exc:  # noqa: BLE001
                self.log.event("wake", state="error", error=f"{type(exc).__name__}: {exc}")
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
        """Memory's call status changed (``CallController.changed``): call mode on or off.

        The wake word keeps listening in a call (``voice_wake_during_calls``); a conversation
        running when a call starts or ends switches mode at once (docs/VOICE.md §25).
        """
        call = status.get("call") if isinstance(status, dict) else None
        active = call is not None
        app = (str(call.get("app") or "") or "a call app") if active else None
        if active != self.call_active or app != self.call_app:
            changed = active != self.call_active
            self.call_active, self.call_app = active, app
            if changed:
                self.log.event("wake", state="call_on" if active else "call_off",
                               listening=bool(self.wake_enabled and self._wake_allowed()))
            conversation = self._conversation
            if conversation is not None:
                conversation.set_call(app)
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

    # -- the chat UI (docs/VOICE.md §12) ---------------------------------------------------

    def _chat(self, event: dict[str, Any]) -> None:
        """A chat event from the voice thread: the typed signal and the one-stream signal (both queued)."""
        kind = event.get("type")
        if kind == "user_utterance":
            self.user_utterance.emit(str(event.get("text") or ""), float(event.get("ts") or 0.0),
                                     str(event.get("turn_id") or ""))
        elif kind == "assistant_speech":
            self.assistant_speech.emit(str(event.get("turn_id") or ""), str(event.get("text") or ""),
                                       bool(event.get("final")), bool(event.get("interrupted")))
        elif kind == "hand_step":
            self.hand_step.emit(str(event.get("turn_id") or ""), str(event.get("name") or ""),
                                str(event.get("args") or ""), bool(event.get("ok")), str(event.get("result") or ""),
                                int(event.get("ms") or 0))
        self.chat_event.emit(dict(event))

    def _job_event(self, job_id: int, state: str, summary: str) -> None:
        self.job_event.emit(int(job_id), state, summary)
        self.chat_event.emit({"type": "job_event", "job_id": int(job_id), "state": state, "summary": summary})

    # -- never silent (docs/VOICE.md §16) ---------------------------------------------

    def notice(self, text: str) -> bool:
        """Tell the user voice could not start or stopped: the reason in the pill, and a short tone.

        The same reason again within ``voice_notice_repeat_s`` only shows the pill (repeated
        wakes must not beep every time). A tone rather than a spoken line: the failing part is
        often the network or a speech service, and a tone needs neither. Returns whether the
        tone played.
        """
        now = time.monotonic()
        last = self._noticed.get(text)
        audible = last is None or now - last >= self.settings.voice_notice_repeat_s
        if audible:
            self._noticed[text] = now
            for key in [k for k, t in self._noticed.items() if now - t > 3600]:
                del self._noticed[key]
        self._set_state("error", text)
        self.log.event("notice", audible=audible, chars=len(text))
        if audible:
            from yuki.voice.audio import play_pcm
            from yuki.voice.echo import tone

            play = self.play_tone or play_pcm
            device = self._chosen_output()
            threading.Thread(target=play, args=(tone("error"), 24_000, device), name="yuki-voice-notice",
                             daemon=True).start()
        return audible

    def _chosen_output(self) -> str | None:
        """The chosen output if it is connected, else ``None`` (the default), without a pill note."""
        wanted = self.device_choice("render")
        if not wanted:
            return None
        try:
            from yuki.voice.audio import audio_devices, resolve_device

            return resolve_device("render", wanted, audio_devices())[0]
        except Exception:  # noqa: BLE001
            return None

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
                self._mic = (self.mic_factory or SharedMic)(None)
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
        from yuki.voice.wake import NameCheck, WakeListener, WakeSpotter

        mic = self._ensure_mic()
        if self._listener is None or not self._listener.running:
            s = self.settings
            self._listener = WakeListener(
                mic, lambda: WakeSpotter(threshold=s.voice_wake_threshold, boost=s.voice_wake_boost),
                on_wake=lambda at, _kw: self._wake_heard.emit(at, time.monotonic(), self._name_end()), log=self.log,
                check=NameCheck(),
            )
            self._listener.start()
            self.log.event("wake", state="listening", during_call=self.call_active)
        if self.in_conversation and not self.closing:
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

    def _name_end(self) -> float:
        """Where the words after the name began, for the wake just heard (0.0: not known). On the wake thread."""
        listener = self._listener
        end = getattr(listener, "name_end", None) if listener is not None else None
        return float(end) if end else 0.0

    def _on_wake_heard(self, at: float, heard_at: float = 0.0, name_end: float = 0.0) -> None:
        if not self._wake_allowed():
            return
        if self.in_conversation:
            if self.closing:  # the last one is tearing down: this wake starts the next one right after
                self._pending_wake = (at, heard_at or time.monotonic(), name_end)
                self.log.event("wake", state="pending")
            return
        self.start_conversation("wake", wake_at=at, heard_at=heard_at or None, name_end=name_end or None)

    def _on_closing(self, reason: str) -> None:
        """The conversation decided to end: listen for the name again during its teardown."""
        self.closing = True
        if self._listener is not None and self._wake_allowed():
            self._listener.resume()

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
        self.closing = False
        self._emit_devices()
        self.conversation_changed.emit(False)
        pending, self._pending_wake = self._pending_wake, None
        if self._wake_allowed():
            self._apply_wake()
        else:
            self._close_mic()
            if not error:
                self._set_state("off", reason)
        if error:
            self.notice(error)  # after the wake state, so the pill keeps the reason
        elif pending is not None and time.monotonic() - pending[1] < PENDING_WAKE_S and self._wake_allowed():
            self.start_conversation("wake", wake_at=pending[0], heard_at=pending[1], name_end=pending[2] or None)

    def _echo_guard(self, mode: str, output: str | None, speaker: Any, mic: Any) -> Any:
        """The conversation's echo guard for ``mode``, with the canceller loading off this thread."""
        from yuki.voice.echo import EchoGuard, PlaybackTimeline, load_apm

        timeline = PlaybackTimeline(latency_s=getattr(speaker, "latency_s", 0.03))
        speaker.timeline = timeline
        if mode == "full":
            return None
        guard = EchoGuard(mode, timeline, tail_s=self.settings.voice_echo_tail_ms / 1000.0,
                          input_latency_s=getattr(mic, "input_latency_s", 0.02), log=self.log)
        if mode == "aec":
            kept = self._aec if self._aec.get("output") == output else {}
            if kept.get("apm") is not None:
                guard.attach_apm(kept["apm"])
                guard.converged = bool(kept.get("converged"))
            else:
                def load() -> None:
                    apm, why = load_apm()
                    if apm is None:
                        self.log.event("echo", state="aec_unavailable", why=why)
                        return
                    self._aec = {"output": output, "apm": apm, "converged": False}
                    guard.attach_apm(apm)

                _background(load, "yuki-voice-aec")
        return guard

    def _thread_main(self, session: str, via: str, listen_from: float, started: float,
                     call_app: str | None, name_cut: bool = False) -> None:
        """The ``yuki-voice`` thread: devices and the wake's acknowledgement first, then (with the
        Pipecat import running alongside) the checks and memory, then the pipeline on its own loop.

        ``started`` is when the name was heard (or the press); every timing is from there.
        """
        from dataclasses import replace

        from yuki.voice.audio import Speaker, headphones_check
        from yuki.voice.echo import choose_mode, tone
        from yuki.voice.mouth import fetch_memory
        from yuki.voice.pipeline import VoiceUnavailable, preflight

        timings: dict[str, Any] = {}

        def mark(name: str) -> None:
            timings[name] = round((time.monotonic() - started) * 1000)

        mark("thread")
        timings["replay_from_ms"] = round((listen_from - started) * 1000)  # negative: from before the spotter fired
        reason, error = "error", ""
        speaker = None
        self._guard = None
        settings = conversation_settings(self.settings, load_prefs(self._prefs_path))
        prewarming = getattr(self, "_prewarm_job", None)
        # A prewarm still importing is waited for rather than raced (two threads importing anthropic at
        # once deadlocked once, pipeline.run); otherwise the modules are loaded here (instant when warm).
        importer = (prewarming if prewarming is not None and not prewarming.done()
                    else _background(_import_pipeline, "yuki-voice-import"))
        # Names and terms from memory for speech recognition (docs/VOICE.md §9), read alongside the rest.
        from yuki.voice.keyterms import memory_keyterms

        keyterms_job = _background(lambda: memory_keyterms(self.memory, limit=settings.voice_keyterms_max),
                                   "yuki-voice-keyterms")
        try:
            mic = self._ensure_mic()
            mark("mic")
            # The chosen output if it is connected (else the default, with a note), opened by
            # name; the device that really opened is the one whose form factor picks how Yuki
            # keeps from hearing herself, and the one the conversation reports.
            speaker, out_facts = self._open_output(self._resolve("render"))
            self._speaker = speaker
            settings = replace(settings, voice_output_device=out_facts.get("device"))
            self.speaker_used = out_facts.get("device")
            self.devices_changed.emit(self.devices_text())
            on_headphones, heard = headphones_check(settings.voice_headphone_devices, device=out_facts.get("device"))
            if settings.voice_require_headphones and not on_headphones:
                raise VoiceUnavailable(f"Voice needs headphones: sound is going to "
                                       f"{heard.get('device') or 'the speakers'}.")
            mode, why = choose_mode(on_headphones, preference=settings.voice_echo)
            guard = self._echo_guard(mode, out_facts.get("device"), speaker, mic)
            self._guard = guard
            mic.set_echo(guard)
            mark("speaker")
            if settings.voice_wake_chime and via == "wake":
                # Cancelled from the mic like Yuki's voice, but never a reason to silence it: the user
                # goes on with their request over the chime (measured: gating it cut "say something
                # nice about" out of the first sentence in the end-to-end run).
                threading.Thread(target=lambda: speaker.write(tone("listening"), gate=False),
                                 name="yuki-voice-chime", daemon=True).start()
            self._thread_state.emit("listening", "")
            mark("listening")  # what is said from here on (and since the name) is kept for speech recognition
            if self._output_note(out_facts):
                self._thread_state.emit("note", self._output_note(out_facts))
            facts = preflight(settings)
            mark("preflight")
            if facts.get("tts_voice"):
                # The chosen voice was refused (e.g. needs a paid plan): this conversation only.
                settings = replace(settings, voice_tts_voice=facts["tts_voice"])
                self.log.event("voice_id", state="refused_fallback", note=facts.get("tts_note"))
            mouth_memory = fetch_memory(self.memory)
            mark("memory")
            try:
                keyterms, keyterm_facts = keyterms_job.result(timeout=1.6)
            except Exception as exc:  # noqa: BLE001 - recognition works without them
                keyterms, keyterm_facts = [], {"terms": 0, "error": type(exc).__name__}
            importer.result()
            mark("imported")
            from pipecat.transports.base_transport import TransportParams

            from yuki.voice.pipeline import Conversation
            from yuki.voice.transport import MicInput, PairTransport, SpeakerOutput

            params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16_000,
                                     audio_out_enabled=True, audio_out_sample_rate=24_000)
            mic_input = MicInput(mic, params, backlog_since=listen_from)
            transport = PairTransport(mic_input, SpeakerOutput(speaker, params))
            conversation = Conversation(
                settings=settings, bridge=self.bridge, transport=transport, memory=self.memory,
                mouth_memory=mouth_memory, log=self.log,
                speech_end=lambda: mic.stats.last_voiced or None,
                voice_activity=lambda: (mic.stats.voiced_since, mic.stats.last_voiced),
                on_state=lambda st, d: self._thread_state.emit(st, d), session=session,
                tts_provider=facts.get("tts_provider"), call_app=call_app,
                on_closing=self._closing.emit, extra_observers=list(self.extra_observers), keyterms=keyterms,
                on_chat=self._chat,
            )
            conversation.on_stt_connected = lambda: mark("stt_connected")
            # What recognition hears starts after the name: the mouth is told its first words were said to it
            # (not when the name was a sound-alike the second opinion heard as the name: then it judges).
            listener = self._listener
            conversation.woken_by_name = (via == "wake" and name_cut
                                          and not bool(getattr(listener, "rescued", False)))
            if facts.get("tts_note"):
                self._thread_state.emit("starting", facts["tts_note"])
            self._conversation = conversation
            if self.call_app != call_app:  # the call started or ended while this was starting
                conversation.set_call(self.call_app)
            mark("built")
            self.last_timings = timings
            self.log.event("session", state="start", session=session, via=via, mic=mic.device_used,
                           mic_opened_as=mic.opened_as, mic_format=getattr(mic, "format", None),
                           output=out_facts.get("device"), output_wanted=out_facts.get("wanted"),
                           output_tray_choice=out_facts.get("tray_choice"), output_found=out_facts.get("found"),
                           output_refreshed=out_facts.get("refreshed"), output_index=out_facts.get("index"),
                           output_latency_ms=out_facts.get("output_latency_ms"),
                           headphones=heard, echo=mode, echo_why=why, call_mode=bool(call_app),
                           stt=self.settings.voice_stt_model,
                           mouth=self.settings.voice_mouth_model, tts=facts.get("tts_provider"),
                           tts_note=facts.get("tts_note"),
                           tts_model=self.settings.voice_tts_model, tts_voice_set=bool(settings.voice_tts_voice),
                           memory=mouth_memory.facts(), keyterms=keyterm_facts, timings_ms=dict(timings))
            reason = asyncio.run(conversation.run())
            timings["kept_s"] = round(mic_input.kept_s, 2)
            timings["replayed_s"] = round(mic_input.replayed_s, 2)
            if conversation.observer.fatal:
                error = _plain_error(conversation.observer.fatal)
        except VoiceUnavailable as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - reported, never raised into Qt
            error = _plain_error(f"{type(exc).__name__}: {exc}")
        finally:
            self._speaker = None
            if speaker is not None:
                speaker.close()  # plays what is queued first
            guard = self._guard
            if guard is not None:
                guard.close()  # guards the wake word until Yuki's last words and their echo are over
                if guard.converged and self._aec.get("apm") is not None:
                    self._aec["converged"] = True
                self.log.event("echo", op="summary", session=session, mode=guard.mode,
                               effective=guard.effective, **guard.stats.facts())
            if self._mic is not None:
                self.log.event("mic", state="levels", **self._mic.stats.levels())
            obs = getattr(self._conversation, "observer", None)
            self.log.event("session", state="stop", session=session, reason=reason, error=error or None,
                           seconds=round(time.monotonic() - started, 1),
                           turns=obs.totals["turns"] if obs else 0,
                           for_others=obs.totals.get("for_others", 0) if obs else 0,
                           mouth_cost_usd=round(obs.totals["mouth_cost_usd"], 5) if obs else 0,
                           tts_chars=obs.totals["tts_chars"] if obs else 0,
                           stt_cost_usd=round((time.monotonic() - started) / 60 * self.settings.voice_usd_per_stt_min, 5),
                           timings_ms=dict(timings))
            self._thread_done.emit(reason, error)
            if settings.voice_tts_provider == "elevenlabs":
                # What this conversation spoke counts against the month: read the quota again for the next one.
                from yuki.voice.pipeline import elevenlabs_quota

                _background(lambda: elevenlabs_quota(refresh=True), "yuki-voice-quota")


def _background(fn: Any, name: str) -> Any:
    """Run ``fn`` on a daemon thread; returns a future for its result (never holds up the app's exit)."""
    from concurrent.futures import Future

    future: Future = Future()

    def run() -> None:
        try:
            future.set_result(fn())
        except BaseException as exc:  # noqa: BLE001 - handed to whoever waits
            future.set_exception(exc)

    threading.Thread(target=run, name=name, daemon=True).start()
    return future


def _import_pipeline() -> None:
    """Import Pipecat and every service the pipeline builds, off the voice thread (3.6-5.5 s cold,
    measured), while the devices open and the checks run. Loaded once per app run; voice-off never
    gets here. Everything ``Conversation`` imports is here, so nothing is imported on the voice loop."""
    import yuki.voice.transport  # noqa: F401 - Pipecat's transports
    from pipecat.adapters.schemas.function_schema import FunctionSchema  # noqa: F401
    from pipecat.pipeline.pipeline import Pipeline  # noqa: F401
    from pipecat.pipeline.worker import PipelineWorker  # noqa: F401
    from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair  # noqa: F401
    from pipecat.services.anthropic.llm import AnthropicLLMService  # noqa: F401
    from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService  # noqa: F401
    from pipecat.services.deepgram.tts import DeepgramTTSService  # noqa: F401
    from pipecat.services.elevenlabs.tts import ElevenLabsTTSService  # noqa: F401
    from pipecat.utils.context.llm_context_summarization import LLMAutoContextSummarizationConfig  # noqa: F401
    from pipecat.workers.runner import WorkerRunner  # noqa: F401

    import yuki.voice.grounding  # noqa: F401
    import yuki.voice.narrator  # noqa: F401
    import yuki.voice.pipeline  # noqa: F401
    from yuki.voice.pipeline import shared_ssl_context, use_shared_tls

    # The TLS context every service connection uses, built here off the voice loop (v3.6: built per
    # connection on the loop it stalled the loop 2.8 s live, reading the Windows certificate store).
    shared_ssl_context()
    use_shared_tls()


def _prewarm(settings: Settings, log: Any) -> dict[str, Any]:
    """The background prewarm (see :meth:`VoiceSession.prewarm`). Never raises; returns what it did."""
    import socket

    started = time.perf_counter()
    facts: dict[str, Any] = {}

    def rss_mb() -> float | None:
        try:
            import psutil

            return round(psutil.Process().memory_info().rss / 1_048_576, 1)
        except Exception:  # noqa: BLE001
            return None

    facts["rss_before_mb"] = rss_mb()
    steps: list[tuple[str, Any]] = [("imports", _import_pipeline)]

    def decoders() -> None:
        from yuki.voice.pipeline import _preimport, quiet_pipecat_logging

        quiet_pipecat_logging(settings.voice_log_dir)
        _preimport()

    def checks() -> None:
        from yuki.voice.pipeline import preflight

        preflight(settings, need_headphones=False)

    def network() -> None:
        from yuki.voice.pipeline import shared_ssl_context

        shared_ssl_context()  # the one TLS context conversations use (built off every event loop)
        for host in ("api.deepgram.com", "api.elevenlabs.io", f"bedrock-runtime.{settings.aws_region}.amazonaws.com"):
            try:
                socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            except OSError:
                pass

    def mixer() -> None:
        from yuki.actions.app_volume import audio_apps

        audio_apps()

    def quota() -> None:
        from yuki.voice.pipeline import elevenlabs_quota

        if settings.voice_tts_provider == "elevenlabs":
            left = elevenlabs_quota(refresh=True)
            facts["tts_quota_left"] = left[0] if left else None

    steps += [("decoders", decoders), ("checks", checks), ("network", network), ("mixer", mixer), ("quota", quota)]
    for name, step in steps:
        t = time.perf_counter()
        try:
            step()
            facts[f"{name}_ms"] = round((time.perf_counter() - t) * 1000)
        except Exception as exc:  # noqa: BLE001 - only an optimisation
            facts[f"{name}_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    facts["rss_after_mb"] = rss_mb()
    facts["ms"] = round((time.perf_counter() - started) * 1000)
    if log is not None:
        log.event("prewarm", **facts)
    return facts


def _plain_error(text: str) -> str:
    """A service error in words the user can act on (the raw text stays in the voice log)."""
    low = text.lower()
    if "401" in low or "unauthor" in low or "permission" in low or "invalid api key" in low:
        return "Voice can't sign in to a speech service (check the API keys and their permissions)."
    if "getaddrinfo" in low or "connect" in low or "timed out" in low or "network" in low:
        return "Voice can't reach the speech services right now (network?)."
    return f"Voice stopped: {text[:160]}"
