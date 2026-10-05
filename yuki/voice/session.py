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
* **standby** (v3.11, ``voice_standby``, while listening for the name): the next conversation is built
  ahead on a ``yuki-voice-standby`` thread and waits dormant: speech recognition's socket open with no
  audio sent (no microphone subscription at all), the talker's and thinker's caches warm (an hour's cache,
  read again hourly while the user is at the PC), no speaker or echo canceller held. The name wakes it: the
  words said since the name stream at once, the speaker opens alongside. Lost or ended, the next one is built
  in the background; with none ready, a wake starts one the v3.10 way.

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
from yuki.voice.workers import gui_poster, posted

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
#: vGame review: when the name must be heard by speech recognition too (a game, a call, right after a goodbye) and its
#: end is known, this much before its end is replayed: "Yuki" said takes ~0.3-0.5 s (the v34 clips).
NAME_KEEP_S = 0.7

#: Environment variable that sets the ElevenLabs voice id (the tray's "Voice id…" wins over it).
VOICE_ID_ENV = "YUKI_VOICE_ID"

#: ``voice.json`` keys of the tray's device choices, by kind (``Settings`` field on the right).
DEVICE_PREFS = {"capture": ("mic_device", "voice_input_device"), "render": ("speaker_device", "voice_output_device")}
#: ``voice.json`` keys of the chosen devices' Core Audio endpoint ids (v3.16, 04 F7): matched before the name.
DEVICE_ID_PREFS = {"capture": "mic_device_id", "render": "speaker_device_id"}
#: ``voice.json`` key of the days the "saved device isn't connected" note was last shown, by kind (once a day).
DEVICE_NOTE_PREF = "device_note_days"
#: ``voice.json`` key of the end tone switch (the tray's "End tone"; ``Settings.voice_end_tone`` otherwise).
END_TONE_PREF = "end_tone"

#: The pill's line when a conversation ended by itself (v3.16, 04 F2), and when it did with the name not listened for.
ENDED_NOTE = "Say ‘Yuki’ to talk again"
ENDED_NOT_LISTENING_NOTE = "Ended. Yuki isn't listening for its name"
#: How Yuki says it is not listening (tray tooltip, call pill; v3.16, 04 F6).
NOT_LISTENING = "Yuki isn't listening"
#: Stops that end a conversation by themselves: only these get the end cue (never a stop the user caused).
SELF_ENDED = ("call_idle", "idle")

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


#: ``voice.json`` keys that choose the voice and the Gemini voice's settings (text values).
VOICE_TEXT_PREFS = {"tts_provider": "voice_tts_provider", "gemini_voice": "voice_gemini_voice",
                    "gemini_model": "voice_gemini_model", "gemini_style": "voice_gemini_style",
                    "speech_channel": "voice_speech_channel", "stt_provider": "voice_stt_provider"}
#: The voices ``tts_provider`` may name (vLocal: ``local``, a voice on this PC named by ``tts_voice``).
PROVIDERS = ("gemini", "elevenlabs", "deepgram", "local")
#: What ``stt_provider`` may name (vLocal): Deepgram Flux (the default) or Parakeet on this PC.
STT_PROVIDERS = ("deepgram", "local")
#: ``voice.json`` key of the "keep the models on this PC loaded" switch (vLocal).
LOCAL_KEEP_PREF = "local_keep_loaded"
#: The speech channels ``speech_channel`` may name (v3.16).
SPEECH_CHANNELS = ("guard", "marker")


def conversation_settings(settings: Settings, prefs: dict[str, Any]) -> Settings:
    """The settings one conversation runs with: the voice id and voice settings from ``voice.json`` applied."""
    from dataclasses import replace

    over: dict[str, Any] = {"voice_tts_voice": voice_id(settings, prefs)}
    for key, name in TTS_PREFS.items():
        value = prefs.get(key)
        if isinstance(value, bool) if name == "voice_tts_speaker_boost" else isinstance(value, (int, float)):
            over[name] = value
    for key, name in VOICE_TEXT_PREFS.items():
        value = prefs.get(key)
        if isinstance(value, str) and (value.strip() or name == "voice_gemini_style"):
            if name == "voice_tts_provider" and value.strip().lower() not in PROVIDERS:
                continue
            if name == "voice_speech_channel" and value.strip().lower() not in SPEECH_CHANNELS:
                continue
            if name == "voice_stt_provider" and value.strip().lower() not in STT_PROVIDERS:
                continue
            lower = name in ("voice_tts_provider", "voice_speech_channel", "voice_stt_provider")
            over[name] = value.strip().lower() if lower else value.strip()
    if isinstance(prefs.get(END_TONE_PREF), bool):
        over["voice_end_tone"] = prefs[END_TONE_PREF]
    if isinstance(prefs.get(LOCAL_KEEP_PREF), bool):
        over["voice_local_keep_loaded"] = prefs[LOCAL_KEEP_PREF]
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


#: Window classes of the shell that may cover a monitor without a caption and are never a game (review F8): the
#: desktop, the taskbars and perception's own list, plus the shell's CoreWindow surfaces (Start, Search, the emoji and
#: clipboard panel; a UWP app in front is its ApplicationFrameWindow, not a bare CoreWindow).
_SHELL_CLASSES = frozenset({
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland", "Shell_InputSwitchTopLevelWindow", "ForegroundStaging",
    "MultitaskingViewFrame", "XamlExplorerHostIslandWindow", "Xaml_WindowedPopupClass", "Windows.UI.Core.CoreWindow",
})


def fullscreen_front() -> bool:
    """vGame: whether the window in front is full screen (a game, an F11 video), read at a wake.

    The shell's own report of a Direct3D full-screen app, else a window without a caption covering its whole monitor
    that is not one of the shell's own (:data:`_SHELL_CLASSES`) and, unless the shell reports a full-screen app (busy or
    presentation mode), is a user window: visible, not cloaked, not a tool window, titled (the test memory's watcher
    makes, ``is_fullscreen_front`` with ``is_user_window``, without its imports: measured 5 Oct its
    module took ~0.3 s to import and its probe ~0.3 s on a first call, too slow for the wake's path). Win32 reads only,
    well under a millisecond; never raises."""
    import ctypes
    from ctypes import wintypes

    try:
        user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
        state = ctypes.c_int(0)
        if shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 and state.value == 3:
            return True   # QUNS_RUNNING_D3D_FULL_SCREEN
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(ctypes.c_void_p(hwnd), name, 64)
        if name.value in _SHELL_CLASSES:
            return False   # the desktop, the taskbar, Start, Search, the emoji and clipboard panel (review F8)
        user32.GetWindowLongW.restype = ctypes.c_long
        style = user32.GetWindowLongW(ctypes.c_void_p(hwnd), -16) & 0xFFFFFFFF   # GWL_STYLE
        if style & 0x00C00000 == 0x00C00000:   # WS_CAPTION: a normal window, maximized or not
            return False
        if state.value not in (2, 4):   # not QUNS_BUSY / QUNS_PRESENTATION_MODE: it must be a user window (review F8,
            ex_style = user32.GetWindowLongW(ctypes.c_void_p(hwnd), -20) & 0xFFFFFFFF   # the watcher's own test)
            cloaked = ctypes.c_int(0)
            ctypes.windll.dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(hwnd), 14, ctypes.byref(cloaked),
                                                       ctypes.sizeof(cloaked))   # DWMWA_CLOAKED
            if not user32.IsWindowVisible(ctypes.c_void_p(hwnd)) or cloaked.value or ex_style & 0x00000080 \
                    or user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd)) <= 0:   # WS_EX_TOOLWINDOW, no title
                return False

        class _MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]

        user32.MonitorFromWindow.restype = ctypes.c_void_p
        monitor = user32.MonitorFromWindow(ctypes.c_void_p(hwnd), 2)   # MONITOR_DEFAULTTONEAREST
        info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
        rect = wintypes.RECT()
        if not monitor or not user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)) \
                or not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return False
        m = info.rcMonitor
        return rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom
    except Exception:  # noqa: BLE001 - no reading: no game
        return False


def call_facing(call: dict[str, Any] | None) -> tuple[str | None, str | None, bool]:
    """``(app, evidence, alone)`` a conversation is given for memory's ``call_status()["call"]`` (v3.15).

    ``app`` is the call's app for call mode (docs/VOICE.md §25), None when there is no call or when the user is
    alone in it: its screen shows nobody else in the call (``others`` == 0, judged by memory's screen model;
    live 2026-09-30 he sat alone in a Discord voice channel waiting for a friend and call mode took 15 of 41
    turns for the people on the call). ``others`` unknown (None) keeps call mode, as before. ``evidence`` is
    "fresh" or "stale" (v3.10; a call the user listed themselves has no screen evidence: as fresh).
    """
    if not isinstance(call, dict):
        return None, None, False
    others = call.get("others")
    alone = others is not None and int(others) == 0
    if alone:
        return None, None, True
    return (str(call.get("app") or "") or "a call app"), str(call.get("evidence") or "fresh"), False


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
    #: ``(until)``: the user asked Yuki to go quiet until this time (epoch seconds; 0.0 when awake again, v3.10).
    quiet_changed = Signal(float)
    #: ``(state)``: whether Yuki listens for the name now: ``on`` · ``off`` (the user's switch, stored) · ``quiet``
    #: (a timed quiet) · ``call_off`` (off for this call only, never stored; v3.16, 04 F6). For the tray and the pills.
    listening_changed = Signal(str)
    _quiet_asked = Signal(float, bool)
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
    #: A standby conversation ended without being woken (``reason``; v3.11): the next one is built in the background.
    _standby_done = Signal(str)
    #: vLocal: ``{model: {"state", "done", "total", ...}}``: the download of the speech models on this PC moved on
    #: (at most about once a second while bytes arrive), for Settings.
    local_models_changed = Signal(object)

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
        #: Quiet until this time (epoch seconds; v3.10: "shut up for a couple of hours" holds across restarts): the
        #: wake word is off until then. The tray shows it and can undo it (``wake_now``).
        try:
            self.quiet_until = float(prefs.get("quiet_until") or 0.0)
        except (TypeError, ValueError):
            self.quiet_until = 0.0
        if self.quiet_until <= time.time():
            if self.quiet_until:   # it ran out while Yuki was not running: listening again, and the note is gone
                prefs.pop("quiet_until", None)
                save_prefs(prefs, prefs_path)
            self.quiet_until = 0.0
        self.call_active = False
        #: Off for this call (v3.16, 04 F6): the wake word is off until the call ends; never written to voice.json.
        self.call_off = False
        #: How the last conversation ended, for its end cue: ``{"reason", "exchanges"}`` (v3.16, 04 F2).
        self._last_end: dict[str, Any] = {}
        #: Why the running conversation must end in an error the user should hear about (the speaker died, v3.16).
        self._pending_error = ""
        #: The days the "saved device isn't connected" note was shown, by kind (once a day; v3.16).
        self._note_days: dict[str, str] = dict(prefs.get(DEVICE_NOTE_PREF) or {}) \
            if isinstance(prefs.get(DEVICE_NOTE_PREF), dict) else {}
        #: In a call with nobody else in it (the screen judge's count, v3.15): conversations are not in call mode.
        self.call_alone = False
        #: The app of the voice call going on (call mode), ``None`` when there is none.
        self.call_app: str | None = None
        #: How sure memory is of that call: "fresh" (seen on the screen during this hold) or "stale" (v3.10).
        self.call_evidence: str | None = None
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
        self._quiet_asked.connect(self._go_quiet)
        self._standby_done.connect(self._on_standby_done)
        #: The conversation built ahead and waiting for the name (v3.11): its thread and the conversation.
        self._standby_thread: threading.Thread | None = None
        self._standby_conv: Any = None
        self._standby_output: Any = None
        self._standby_failures = 0
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
        self._quiet_timer = QTimer(self)
        self._quiet_timer.setSingleShot(True)
        self._quiet_timer.timeout.connect(self._quiet_over)
        self._standby_timer = QTimer(self)
        self._standby_timer.setSingleShot(True)
        self._standby_timer.timeout.connect(self._ensure_standby)
        # -- vGame (5 Oct) ------------------------------------------------------------------------------------
        #: The game whose own voice chat memory sees now (``call_status()["sessions"]``, ``kind`` "game"), else None.
        self.game_session: str | None = None
        #: ``() -> dict | None``: the window in front when it is full screen (``avsignals.foreground_fullscreen``),
        #: read at a wake (one Win32 look, no polling); the offline harness and tests replace it.
        self.fullscreen_probe: Any = None
        #: ``(pcm, kind)``: a test's stand-in for playing the instant acknowledgement (else the conversation's speaker).
        self.play_sound: Any = None
        #: The game in front and whether the last wake needs the name confirmed by speech recognition (set at a wake).
        self._wake_game: str | None = None
        self._wake_strict = False
        #: Review F1: when the last conversation the user dismissed decided to end (monotonic): a name hit for words
        #: from before then ("Yuki, go away" heard late) is not a new wake.
        self._dismissed_at: float | None = None

    # -- vGame: focus mode (a game or a call), the name heard while talking, the instant answers ---------------------

    def _game_now(self) -> str | None:
        """The full-screen game in front, by measured signals only: memory's game voice session (its name), else the
        window in front being full screen (``"fullscreen"``). ``None``: none, or focus mode is off."""
        if not getattr(self.settings, "voice_focus_mode", True):
            return None
        if self.game_session:
            return self.game_session
        probe = self.fullscreen_probe or fullscreen_front
        try:
            return "fullscreen" if probe() else None
        except Exception:  # noqa: BLE001
            return None

    def _name_while_talking(self) -> bool:
        """Whether the wake word keeps listening while a conversation runs (its hits say a turn was said to Yuki)."""
        return bool(getattr(self.settings, "voice_name_while_talking", True))

    def _play_sound(self, pcm: bytes, kind: str) -> None:
        """The conversation's instant acknowledgement: through its open speaker, off the calling thread; never raises."""
        if self.play_sound is not None:
            try:
                self.play_sound(pcm, kind)
            except Exception:  # noqa: BLE001
                pass
            return
        speaker = self._speaker
        if speaker is None:
            return
        # gate=True (review F6): it is Yuki's voice, not a tone: on speakers the echo guard treats it like her replies
        # until the canceller has converged, so no residue of "Mm-hm" reaches speech recognition as the user's words
        threading.Thread(target=lambda: _quietly(speaker.write, pcm, gate=True), name="yuki-voice-ack",
                         daemon=True).start()

    def _dismiss_cue(self) -> None:
        """The user dismissed Yuki on the fast path: the soft end tone now (the user's End tone choice), off the loop."""
        def run() -> None:
            try:
                on = self.end_tone
            except Exception:  # noqa: BLE001
                on = True
            if on:
                self._earcon("ended")

        threading.Thread(target=run, name="yuki-voice-dismiss-cue", daemon=True).start()

    def _wire_fast(self, conversation: Any) -> None:
        """vGame hooks of a conversation: the acknowledgement's output, the dismiss cue, the game in front."""
        conversation.on_sound = self._play_sound
        conversation.on_dismissed = self._dismiss_cue

    # -- public (GUI thread) ------------------------------------------------------

    @property
    def in_conversation(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """App start: listen for the name unless the user turned that off (it is on by default).

        With the wake word on and ``voice_prewarm``, the conversation's code is loaded in the background
        a little later (:meth:`prewarm`), so the first "Yuki" is not a cold start.
        """
        if self.quiet_until:
            self._arm_quiet_timer()
            self.log.event("wake", state="quiet", minutes_left=round((self.quiet_until - time.time()) / 60))
            self.quiet_changed.emit(self.quiet_until)
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
        # Then the whole conversation is built and waits for the name (v3.11): the first "Yuki" goes live at once.
        self._arm_standby(3000)

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
        started = heard_at if heard_at is not None else time.monotonic()
        if name_end and wake_at is not None and name_end < wake_at - WAKE_PREROLL_S:
            # The spotter's token clock drifted on a long open stream: it put the name's end seconds before the
            # batch that heard it (live 4 Oct: 7-39 s, while the listener was 10 ms behind the mic). Replaying from
            # there fed that much old audio to speech recognition before the request. Use the batch instead.
            self.log.event("wake", state="name_end_ignored", drift_ms=round((wake_at - name_end) * 1000))
            name_end = None
        self._wake_game = self._game_now()   # vGame: a full-screen game in front now (one look, at the wake)
        # Review F2/F5: in a game or a call, or right after a goodbye, the spotter's hit alone does not make the first
        # words said to Yuki (accidental wakes on sound-alikes; the name said in a story): the name is kept in what
        # speech recognition hears, and the first words count as said to Yuki when it wrote the name opening them.
        self._wake_strict = via == "wake" and bool(self._wake_game or self._call_focus() or self._after_goodbye())
        keep_from = None
        if self._wake_strict and name_end:
            # the name itself and no more than the pre-roll (each replayed second delays the first answer: offline the
            # kept name cost ~0.3-0.4 s on the first words after a wake in a game)
            keep_from = name_end - NAME_KEEP_S
            if wake_at is not None:
                keep_from = max(keep_from, wake_at - WAKE_PREROLL_S)
            self.log.event("wake", state="name_kept", game=bool(self._wake_game), call=self._call_focus(),
                           after_goodbye=self._after_goodbye())
            name_end = None
        if name_end:
            listen_from = name_end
        elif keep_from is not None:
            listen_from = keep_from
        else:
            listen_from = (wake_at - WAKE_PREROLL_S) if wake_at is not None else time.monotonic()
        self.closing = False
        if self._listener is not None and not self._name_while_talking():
            self._listener.pause()
        if self._wake_standby(via, listen_from, started, bool(name_end)):
            return True
        return self._start_cold(via, listen_from, started, bool(name_end))

    def _start_cold(self, via: str, listen_from: float, started: float, name_cut: bool) -> bool:
        """Build and start a conversation now (no standby was ready): the v3.10 path."""
        if self.in_conversation:
            return False
        session = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        self.closing = False
        self._set_state("starting", via)
        if self._listener is not None and not self._name_while_talking():
            self._listener.pause()
        self._thread = threading.Thread(target=self._thread_main,
                                        args=(session, via, listen_from, started, self.call_app, name_cut),
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

    def set_wake(self, enabled: bool, *, by: str = "user") -> None:
        """The user's own "Listen for 'Yuki'" switch (the tray, the window): remembered.

        Only this persists ``wake: false``: a timed quiet ("quiet for two hours") keeps a quiet-until time instead
        and never touches it (v3.13: live 2026-09-28 a click during a quiet hour left the wake word off for good, and
        every later start came up deaf). Turning it on also ends a quiet period: the user wants to be heard now.
        """
        self.wake_enabled = bool(enabled)
        prefs = load_prefs(self._prefs_path)
        prefs["wake"] = self.wake_enabled
        save_prefs(prefs, self._prefs_path)
        self.log.event("wake", state="enabled" if enabled else "disabled", by=by, quiet=self.quiet,
                       call_off=self.call_off)
        if enabled and self.call_off:
            self.call_off = False   # turning listening on means now, in this call too
        if enabled and self.quiet:
            self._quiet_over(by=by)
        self._apply_wake()
        self.wake_changed.emit(self.wake_enabled)
        self._emit_listening()

    @property
    def listening_state(self) -> str:
        """``"on"`` (listening for the name), ``"quiet"`` (a timed quiet holds), ``"call_off"`` (off for this call
        only, v3.16) or ``"off"`` (the user switched it off: stored)."""
        if not self.wake_enabled:
            return "off"
        if self.quiet:
            return "quiet"
        return "call_off" if self.call_off else "on"

    @property
    def listening(self) -> bool:
        """Whether the name is listened for now (the switch on, no timed quiet, not off for this call)."""
        return self.listening_state == "on"

    def _emit_listening(self) -> None:
        self.listening_changed.emit(self.listening_state)

    def set_call_off(self, off: bool, *, by: str = "user") -> None:
        """"Off for this call" (v3.16, 04 F6): the wake word stops until the call ends, then listens again by itself.

        Never written to ``voice.json`` (the permanent off is :meth:`set_wake`). Outside a call it does nothing.
        """
        off = bool(off) and self.call_active
        if off == self.call_off:
            return
        self.call_off = off
        self.log.event("wake", state="call_off" if off else "call_on_again", by=by)
        if off and by != "voice" and self.in_conversation and self.call_app:
            self.stop_conversation("call_off")   # by voice, the talker's okay plays and the bridge ends it
        self._apply_wake()
        self.wake_changed.emit(self.wake_enabled)
        self._emit_listening()

    @property
    def end_tone(self) -> bool:
        """The end tone (v3.16, 04 F2): ``voice.json`` ``end_tone`` (the tray), else ``Settings.voice_end_tone``."""
        value = load_prefs(self._prefs_path).get(END_TONE_PREF)
        return value if isinstance(value, bool) else bool(getattr(self.settings, "voice_end_tone", True))

    def set_end_tone(self, on: bool) -> None:
        """The tray's "End tone" switch: remembered in ``voice.json``."""
        prefs = load_prefs(self._prefs_path)
        prefs[END_TONE_PREF] = bool(on)
        save_prefs(prefs, self._prefs_path)
        self.log.event("end_tone", on=bool(on))

    # -- quiet for a while (v3.10) ------------------------------------------------------

    @property
    def quiet(self) -> bool:
        """Whether the user asked Yuki to stay quiet and that time has not run out."""
        return bool(self.quiet_until and time.time() < self.quiet_until)

    def go_quiet(self, minutes: float, this_call: bool = False) -> None:
        """Stay quiet for ``minutes``: the wake word is off until then, remembered across restarts. Thread-safe.

        ``this_call`` (v3.16, the talker's judgement of "stop listening" in a call): off for the rest of this call
        instead, never stored (:meth:`set_call_off`)."""
        self._quiet_asked.emit(float(minutes), bool(this_call))

    def _go_quiet(self, minutes: float, this_call: bool = False) -> None:
        if this_call and self.call_active:
            self.set_call_off(True, by="voice")
            return
        minutes = max(1.0, min(float(minutes), 24 * 60.0))
        self.quiet_until = time.time() + minutes * 60.0
        prefs = load_prefs(self._prefs_path)
        prefs["quiet_until"] = self.quiet_until
        save_prefs(prefs, self._prefs_path)
        self.log.event("wake", state="quiet", minutes=round(minutes))
        self._arm_quiet_timer()
        self._apply_wake()
        self.quiet_changed.emit(self.quiet_until)
        self._emit_listening()

    def wake_now(self) -> None:
        """The tray's "Wake Yuki now": listen for the name again before the quiet time is over."""
        self._quiet_over(by="user")

    def _quiet_over(self, by: str = "time") -> None:
        was = self.quiet_until
        self.quiet_until = 0.0
        self._quiet_timer.stop()
        prefs = load_prefs(self._prefs_path)
        if prefs.pop("quiet_until", None) is not None:
            save_prefs(prefs, self._prefs_path)
        if was:
            self.log.event("wake", state="awake", by=by)
        self._apply_wake()
        self.quiet_changed.emit(0.0)
        self._emit_listening()

    def _arm_quiet_timer(self) -> None:
        left_ms = int(max(0.0, self.quiet_until - time.time()) * 1000)
        self._quiet_timer.start(min(left_ms, 2**31 - 1))

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
            # v3.16 (04 F7): the endpoint's id too, so a renamed endpoint ("4- " prefixes, profiles) stays chosen
            endpoint = None
            try:
                from yuki.voice.audio import device_id

                endpoint = device_id(kind, name, self._devices())
            except Exception:  # noqa: BLE001 - the name alone, as before
                endpoint = None
            if endpoint:
                prefs[DEVICE_ID_PREFS[kind]] = endpoint
            else:
                prefs.pop(DEVICE_ID_PREFS[kind], None)
        else:
            prefs.pop(key, None)
            prefs.pop(DEVICE_ID_PREFS[kind], None)
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

    def _move_output(self, speaker: Any, target: str | None) -> dict[str, Any]:
        """Move the conversation's speaker to ``target`` (``None``: the default), re-choosing the echo mode."""
        from yuki.voice.audio import headphones_check
        from yuki.voice.echo import choose_mode

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
        return facts

    def _switch_output(self) -> None:
        """The tray's Speaker changed during a conversation: move the output, re-choose the echo mode."""
        speaker = self._speaker
        if speaker is None or not hasattr(speaker, "switch"):
            self.state_changed.emit("note", "The new speaker is used from the next conversation")
            return
        try:
            facts = self._move_output(speaker, self._output_target(self._resolve("render")))
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

    #: ``() -> dict``: the active audio devices (Core Audio, read-only, :func:`yuki.voice.audio.audio_devices`);
    #: a test replaces it.
    devices_source: Any = None

    def _devices(self) -> dict[str, Any]:
        from yuki.voice.audio import audio_devices

        return (self.devices_source or audio_devices)()

    def _resolve(self, kind: str, *, quiet: bool = False) -> str | None:
        """The chosen device if it is connected, else ``None`` (the default) with a note in the pill
        (``quiet``: no note, for the device watch's retries).

        v3.16 (04 F7): matched by its endpoint id first; found under a new name, the new name is remembered. The
        "isn't connected" note shows at most once a day per kind (live: an AirPods choice not connected put the
        note on the pill at every wake)."""
        from yuki.voice.audio import resolve_device

        wanted = self.device_choice(kind)
        if not wanted:
            return None
        prefs = load_prefs(self._prefs_path)
        wanted_id = prefs.get(DEVICE_ID_PREFS[kind]) if prefs.get(DEVICE_PREFS[kind][0]) == wanted else None
        name, note = resolve_device(kind, wanted, self._devices(), wanted_id=wanted_id)
        if note and not quiet:
            self.log.event("device", kind=kind, state="missing_fallback")
            if self._note_due(kind):
                self.state_changed.emit("note", note)
        elif name and name != wanted and wanted_id:
            self.log.event("device", kind=kind, state="renamed")
            prefs[DEVICE_PREFS[kind][0]] = name
            save_prefs(prefs, self._prefs_path)
        return name

    def _note_due(self, kind: str) -> bool:
        """Whether the "saved device isn't connected" note may show now: once a day per kind (remembered)."""
        today = time.strftime("%Y-%m-%d")
        if self._note_days.get(kind) == today:
            return False
        self._note_days[kind] = today
        prefs = load_prefs(self._prefs_path)
        prefs[DEVICE_NOTE_PREF] = dict(self._note_days)
        save_prefs(prefs, self._prefs_path)
        return True

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
        # v3.16: the open mic is known by the name it was opened under (its endpoint's name then), the choice by its
        # endpoint id as well as its name (04 F7)
        gone = bool(wanted) and bool(mic.device) and mic.device not in present
        back = bool(wanted) and mic.device is None and (wanted in present or self._id_present("capture", devices))
        if gone or back or info.get("stalled"):
            self.log.event("device", kind="capture", state="gone" if gone else "back" if back else "stalled")
            self._reopen_mic()
            self._emit_devices()
        speaker = self.speaker_used
        if (self.in_conversation and speaker and speaker not in (devices.get("render") or [])
                and not getattr(self, "_rerouting", False)):
            self.log.event("device", kind="render", state="gone")
            # v3.16 (04 F7): the conversation goes on on Windows' default output (a Bluetooth profile switch or a
            # renamed endpoint looks the same); it ends only when that cannot open.
            self._rerouting = True
            threading.Thread(target=self._reroute_speaker, args=(speaker,), name="yuki-voice-reroute",
                             daemon=True).start()

    def _id_present(self, kind: str, devices: dict[str, Any]) -> bool:
        wanted_id = load_prefs(self._prefs_path).get(DEVICE_ID_PREFS[kind])
        return bool(wanted_id) and wanted_id in ((devices.get(f"{kind}_ids") or {}).values())

    def _reroute_speaker(self, old: str) -> None:
        """The conversation's speaker is gone: reopen on the default output and go on, with a tone (04 F7).

        v3.18 (34-review M7): in a call, or with ``voice_require_headphones``, only onto headphones: when the default
        output is not (``headphones_check``), the conversation ends with ``speaker_gone`` instead, so Yuki never plays
        through the laptop's speakers into the call's microphone."""
        try:
            speaker = self._speaker
            if speaker is None or not hasattr(speaker, "switch"):
                raise RuntimeError("no speaker to move")
            target = self._output_target(None)
            in_call = bool(getattr(self, "call_app", None))
            if in_call or getattr(self.settings, "voice_require_headphones", False):
                from yuki.voice.audio import headphones_check

                ok, heard = headphones_check(self.settings.voice_headphone_devices, device=target)
                if not ok:
                    self.log.event("device", kind="render", state="reroute_refused", in_call=in_call,
                                   form_factor=(heard or {}).get("form_factor"))
                    self.state_changed.emit("note", f"Speaker '{old}' disconnected; ending this conversation"
                                            + (" so Yuki doesn't play into the call" if in_call else ""))
                    self.stop_conversation("speaker_gone")
                    return
            facts = self._move_output(speaker, target)
            device = facts.get("device")
            self.log.event("device", kind="render", state="rerouted", opened=device, found=facts.get("found"))
            self.state_changed.emit("note", f"Speaker '{old}' disconnected; Yuki is on {device} now")
            self._earcon("listening")
        except Exception as exc:  # noqa: BLE001 - nothing to play on: the conversation ends, and says so
            self.log.event("device", kind="render", state="reroute_failed", error=f"{type(exc).__name__}: {exc}")
            self.state_changed.emit("note", f"Speaker '{old}' disconnected; ending this conversation")
            self.stop_conversation("speaker_gone")
        finally:
            self._rerouting = False
            self.devices_changed.emit(self.devices_text())

    def _reopen_speaker(self, error: BaseException) -> bool:
        """Writes to the speaker kept failing (3 in a row): reopen it once, on the chosen output or the default.

        On the conversation's writer thread (``SpeakerOutput.on_write_failing``). ``True`` when it opened again."""
        speaker = self._speaker
        if speaker is None or not hasattr(speaker, "switch"):
            return False
        try:
            facts = self._move_output(speaker, self._output_target(self._resolve("render", quiet=True)))
            self.log.event("device", kind="render", state="reopened_after_write_errors", opened=facts.get("device"),
                           error=type(error).__name__)
            return True
        except Exception as exc:  # noqa: BLE001
            self.log.event("device", kind="render", state="reopen_failed", error=f"{type(exc).__name__}: {exc}")
            return False

    def _take_pending_error(self) -> str:
        error, self._pending_error = getattr(self, "_pending_error", ""), ""
        return error

    def _speaker_failed(self, conversation: Any, error: BaseException) -> None:
        """The reopen did not help: the conversation ends, and the error tone and the pill say why (04 F7)."""
        self.log.event("device", kind="render", state="write_failed", error=type(error).__name__)
        self._pending_error = "Yuki's speaker stopped working, so voice stopped. Check the output in the tray."
        conversation.request_stop("speaker_failed")

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
        self.rebuild_standby()   # the next conversation speaks with the new voice

    def on_call_status(self, status: object) -> None:
        """Memory's call status changed (``CallController.changed``): call mode on or off.

        The wake word keeps listening in a call (``voice_wake_during_calls``); a conversation
        running when a call starts or ends switches mode at once (docs/VOICE.md §25).
        """
        call = status.get("call") if isinstance(status, dict) else None
        active = call is not None
        app, evidence, alone = call_facing(call)
        # vGame: a game's own voice chat going on (memory's measured detector: a full-screen app in front drawing like a
        # game, holding the microphone) is focus mode for the voice, whether or not it records
        sessions = status.get("sessions") if isinstance(status, dict) else None
        game = next((str(s.get("app") or "a game") for s in (sessions or []) if isinstance(s, dict)
                     and s.get("kind") == "game" and not s.get("ended")), None)
        if game != self.game_session:
            self.game_session = game
            self.log.event("wake", state="game_on" if game else "game_off")
            for conversation in (self._conversation, self._standby_conv):
                if conversation is not None and hasattr(conversation, "set_game"):
                    conversation.set_game(self._game_now() if conversation is self._conversation else None)
        if active != self.call_active or app != self.call_app or evidence != self.call_evidence \
                or alone != self.call_alone:
            changed = active != self.call_active or alone != self.call_alone
            self.call_active, self.call_app, self.call_evidence, self.call_alone = active, app, evidence, alone
            if changed:
                # Alone in the call (its screen shows nobody else in it, v3.15): the wake word listens as in a
                # call, but conversations run in normal mode (every turn is for Yuki).
                self.log.event("wake", state="call_alone" if alone else ("call_on" if active else "call_off"),
                               listening=bool(self.wake_enabled and self._wake_allowed()),
                               **({"evidence": str(call.get("evidence") or "fresh"),
                                   "others": call.get("others")} if active else {}))
            for conversation in (self._conversation, self._standby_conv):
                if conversation is not None:
                    conversation.set_call(app, evidence)
            if not active and self.call_off:
                # "Off for this call" ends with the call (v3.16, 04 F6): listening for the name again by itself.
                self.call_off = False
                self.log.event("wake", state="call_on_again", by="call_end")
                self.wake_changed.emit(self.wake_enabled)
            self._apply_wake()
            if changed:
                self._emit_listening()
        for hook in ("_resume_local_download", "_local_busy_changed"):   # vLocal (a test's stand-in has none)
            fn = getattr(self, hook, None)
            if callable(fn):
                fn()

    def on_front_changed(self) -> None:
        """The window in front changed, or changed its size (the island's WinEvent watch): a download paused by a
        full-screen window may go on, and a standby whose speech-on-this-PC choice no longer fits a game starting or
        ending is built again. Cheap: nothing is read unless a download is paused or a standby runs speech on this PC
        (then one Win32 look)."""
        self._resume_local_download()
        self._local_busy_changed()

    def _resume_local_download(self) -> None:
        """vLocal: a download of the speech models that stopped for a game, a call or a full-screen window goes on once
        none is on (a call-status change, or the window in front changing)."""
        import sys

        lm = sys.modules.get("yuki.voice.local_models")   # never imported when local speech was never chosen
        if lm is None or lm.downloader().running:
            return
        if any(row.get("state") == "paused" for row in lm.downloader().status().values()) and self._pc_free():
            self.local_models_wanted()

    def owns(self, job_id: int) -> bool:
        """Whether a request came from voice (its questions are spoken, not popped up)."""
        return self.bridge.owns(job_id)

    def interrupt(self) -> None:
        """Cut Yuki's speech now (the cancel hotkey)."""
        if self._conversation is not None:
            self._conversation.interrupt()

    def shutdown(self) -> None:
        """App exit: end the conversation (bounded join), stop listening, close the mic."""
        self._standby_timer.stop()
        self._drop_standby("quit", wait_s=3.0)
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
            from yuki.voice.audio import resolve_device

            wanted_id = load_prefs(self._prefs_path).get(DEVICE_ID_PREFS["render"])
            return resolve_device("render", wanted, self._devices(), wanted_id=wanted_id)[0]
        except Exception:  # noqa: BLE001
            return None

    def _earcon(self, kind: str) -> None:
        """A non-speech tone now (:func:`yuki.voice.echo.tone`): through the conversation's open speaker (the echo
        guard knows it), else on its own short stream on the chosen output. Off the calling thread; never raises."""
        try:
            from yuki.voice.echo import tone

            pcm = tone(kind)
        except Exception:  # noqa: BLE001
            return
        speaker = self._speaker
        if speaker is not None and self.play_tone is None:
            threading.Thread(target=lambda: _quietly(speaker.write, pcm, gate=False), name="yuki-voice-earcon",
                             daemon=True).start()
            return
        from yuki.voice.audio import play_pcm

        play = self.play_tone or play_pcm
        threading.Thread(target=lambda: _quietly(play, pcm, 24_000, self._chosen_output()), name="yuki-voice-earcon",
                         daemon=True).start()

    def _end_cue(self, reason: str) -> None:
        """A conversation ended by itself after an exchange: the pill line, and the soft end tone (v3.16, 04 F2).

        Only for ``call_idle`` / ``idle`` (never a stop the user caused); a non-speech sound, never words: Yuki never
        speaks first. The tone is the user's choice (``end_tone``, the tray); the line is always shown."""
        end = self._last_end or {}
        if reason not in SELF_ENDED or int(end.get("exchanges") or 0) < 1:
            return
        listening = self._wake_allowed()
        tone_on = self.end_tone
        self.state_changed.emit("ended", ENDED_NOTE if listening else ENDED_NOT_LISTENING_NOTE)
        self.log.event("session", state="end_cue", reason=reason, tone=tone_on, listening=listening)
        if tone_on:
            self._earcon("ended")

    # -- wake word ------------------------------------------------------------------

    # -- vLocal: speech on this PC -----------------------------------------------------------------------------------

    def _pc_free(self) -> bool:
        """No game and no call now (the download of the local models waits for that; nothing heavy then)."""
        return not (self.call_active or self.call_app or self._game_now())

    def local_models_wanted(self) -> list[str]:
        """The speech models on this PC the saved choices need, the missing ones downloading in the background when no
        game or call is on (Settings' switch, the next standby). Off the GUI thread's work: it only starts a thread."""
        from yuki.voice import local_models as lm

        settings = conversation_settings(self.settings, load_prefs(self._prefs_path))
        keys = lm.wanted(settings.voice_stt_provider, settings.voice_tts_provider, settings.voice_tts_voice)
        root = str(getattr(settings, "voice_local_models_dir", "") or "") or None
        if keys and any(not lm.installed(k, root) for k in keys):
            dl = lm.downloader()
            dl.on_change = lambda status: self.local_models_changed.emit(status)
            if dl.start(keys, allowed=self._pc_free, root=root, log=self.log):
                self.log.event("local_models", op="download_start", models=keys)
        return keys

    def _local_choice(self, settings: Settings, facts: dict[str, Any]) -> str | None:
        """What a standby chose for speech on this PC (review F3): ``"busy_cloud"`` (local chosen, but a game or a call
        sent it to the cloud), ``"local"`` (it runs on this PC), ``None`` (local not chosen, or not ready anyway)."""
        if facts.get("local_busy"):
            return "busy_cloud"
        if "local" in (facts.get("stt_provider"), facts.get("tts_provider")):
            return "local"
        return None

    def _local_busy_changed(self) -> None:
        """A game or a call started or ended (call status, the window in front): a standby whose speech-on-this-PC
        choice no longer fits is built again. Cloud for a game, and back on this PC after it; a local standby whose
        models are not loaded goes to the cloud when a game starts (its wake would load about 1 GB mid-game)."""
        built = getattr(self, "_standby_local", None)
        if built is None or self.in_conversation or self._standby_conv is None:
            return
        busy = not self._pc_free()
        if built == "busy_cloud" and busy:
            return
        if built == "local":
            if not busy:
                return
            import sys

            lm = sys.modules.get("yuki.voice.local_models")
            settings = (getattr(self._standby_conv, "_standby_meta", {}) or {}).get("settings") or self.settings
            needed = lm.wanted(settings.voice_stt_provider, settings.voice_tts_provider,
                               settings.voice_tts_voice) if lm is not None else []
            loaded = lm.engine().loaded() if lm is not None else {}
            if all(loaded.get(k) for k in needed if k != lm.FALLBACK_VOICE.family):
                return   # already loaded: nothing heavy to load at the wake
        self._standby_local = None
        self.log.event("local_models", op="standby_rebuild", busy=busy, was=built)
        self.rebuild_standby()

    def _local_setup(self, settings: Settings, *, busy: bool = False) -> None:
        """Before a conversation is built (its own thread, never an event loop): the speech models on this PC.

        Local chosen: the engine's threads exist, VAD and Smart Turn load (once; not while a game or a call is on),
        the big models load ahead only with "keep loaded" and no game or call on (else at the wake), and missing models
        download in the background. Local not chosen: anything still loaded from before is let go. Nothing at all
        when local was never used."""
        from yuki.voice import local_models as lm

        stt_local = settings.voice_stt_provider == "local"
        tts_local = settings.voice_tts_provider == "local"
        eng = lm.engine()
        if not (stt_local or tts_local):
            if eng.started and any(eng.loaded().values()):
                eng.unload(everything=True)
            return
        lm.configure_from(settings, log=self.log, busy=lambda: not self._pc_free())
        if not stt_local and eng.loaded().get("turn"):
            eng.unload(everything=True)
        self.local_models_wanted()
        root = str(getattr(settings, "voice_local_models_dir", "") or "") or None
        if stt_local and lm.installed("parakeet", root) and (not busy or eng.loaded().get("parakeet")):
            eng.turn_models()
        if settings.voice_local_keep_loaded:
            family = lm.parse_voice(settings.voice_tts_voice).family if tts_local else None
            eng.preload(stt_local and lm.installed("parakeet", root),
                        family if family and lm.installed(family, root) else None)

    def _wake_allowed(self) -> bool:
        return (self.wake_enabled and not self.quiet and not self.call_off
                and (self.settings.voice_wake_during_calls or not self.call_active))

    def _apply_wake(self) -> None:
        if self._wake_allowed():
            if not self.in_conversation and self._standby_conv is None and not self._standby_timer.isActive()                     and getattr(self, "_prewarmed", False):
                self._arm_standby(2000)
            try:
                self._ensure_listener()
                if not self.in_conversation:
                    self._set_state("wake", "")
            except Exception as exc:  # noqa: BLE001 - shown, never raised into the tray
                self._set_state("error", f"Can't listen for 'Yuki': {exc}")
                self.log.event("wake", state="error", error=f"{type(exc).__name__}: {exc}")
        else:
            self._drop_standby("wake_off")
            self._stop_listener()
            if not self.in_conversation:
                self._close_mic()
                self._set_state("off", "quiet" if self.quiet else ("call_off" if self.call_off else (
                    "call" if self.wake_enabled and self.call_active else "")))

    def _ensure_mic(self) -> Any:
        from yuki.voice.audio import SharedMic

        with self._mic_lock:
            if self._mic is None:
                self._mic = (self.mic_factory or SharedMic)(None)
                # D7 (v3.17): its callback thread on time (yuki.voice.qos), unless turned off.
                if hasattr(self._mic, "qos"):
                    self._mic.qos = bool(getattr(self.settings, "voice_qos", True))
                    self._mic.mmcss = bool(getattr(self.settings, "voice_mmcss", True))
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
            on_wake = lambda at, _kw: self._wake_heard.emit(at, time.monotonic(), self._name_end())  # noqa: E731
            listener = None
            if getattr(s, "voice_wake_process", True) and self.mic_factory is None:
                # The spotter in its own process (v3.12): it never waits for the app's other threads. In-process
                # only when that cannot start (and in tests and the offline harness, with a pretend mic).
                try:
                    from yuki.voice.wakeproc import ProcessWakeListener

                    listener = ProcessWakeListener(mic, on_wake, self.log, threshold=s.voice_wake_threshold,
                                                   boost=s.voice_wake_boost)
                    listener.start()
                except Exception as exc:  # noqa: BLE001 - the in-process listener then
                    self.log.event("wake", state="process_failed", error=f"{type(exc).__name__}: {str(exc)[:160]}")
                    listener = None
            if listener is None:
                listener = WakeListener(
                    mic, lambda: WakeSpotter(threshold=s.voice_wake_threshold, boost=s.voice_wake_boost),
                    on_wake=on_wake, log=self.log, check=NameCheck(),
                )
                listener.start()
            self._listener = listener
            self.log.event("wake", state="listening", during_call=self.call_active,
                           process=type(listener).__name__ != "WakeListener")
        if self.in_conversation and not self.closing and not self._name_while_talking():
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

    #: Review F1: a name hit is checked against the last dismissal for this long after it (s): the spotter runs behind
    #: the mic by up to ~1.8 s at worst (live logs), plus the teardown.
    DISMISSED_HIT_S = 6.0

    def _said_before_dismissal(self, at: float, name_end: float) -> bool:
        """Review F1: whether a name hit is for words said before the user dismissed the last conversation ("Yuki, go
        away": the eager end of turn can close it before the spotter reports the name). Measured times only: where the
        name ended (else the start of the audio batch that heard it) against when the conversation decided to end."""
        dismissed = self._dismissed_at
        if dismissed is None or time.monotonic() - dismissed > self.DISMISSED_HIT_S:
            return False
        said = name_end or at
        return bool(said) and said < dismissed

    def _note_dismissed(self, reason: str) -> None:
        """Remember when a conversation the user dismissed decided to end (its own clock, else now)."""
        if reason == "user_asked":
            stopped = getattr(self._conversation, "stopped_at", None)
            self._dismissed_at = float(stopped) if isinstance(stopped, (int, float)) and stopped else time.monotonic()

    def _on_wake_heard(self, at: float, heard_at: float = 0.0, name_end: float = 0.0) -> None:
        if not self._wake_allowed():
            return
        if self._said_before_dismissal(at, name_end):
            # review F1: the name in "Yuki, go away" heard after the fast dismissal closed it: never a new wake
            self.log.event("wake", state="ignored", why="before_dismissal",
                           before_ms=round((self._dismissed_at - (name_end or at)) * 1000))
            return
        if self.in_conversation:
            if self.closing:  # the last one is tearing down: this wake starts the next one right after
                self._pending_wake = (at, heard_at or time.monotonic(), name_end)
                self.log.event("wake", state="pending")
            elif self._conversation is not None and hasattr(self._conversation, "name_heard"):
                # vGame: the name said while talking: the turn it is in was said to Yuki (measured, never words)
                self._conversation.name_heard(heard_at or time.monotonic())
                self.log.event("wake", state="name_in_conversation",
                               rescued=bool(getattr(self._listener, "rescued", False)))
            return
        if bool(getattr(self._listener, "rescued", False)) and (self._call_focus() or self._game_now()):
            # vGame: in a game or a call a wake needs the main spotter's own hit; a sound-alike the second opinion
            # rescued is not enough there (report 76: ~13 accidental wakes, several in Discord calls and VALORANT)
            self.log.event("wake", state="ignored", why="rescued_in_focus")
            return
        self.start_conversation("wake", wake_at=at, heard_at=heard_at or None, name_end=name_end or None)

    def _call_focus(self) -> bool:
        """A call with fresh evidence and others in it (call mode), with focus mode on (vGame)."""
        return bool(getattr(self.settings, "voice_focus_mode", True) and self.call_app
                    and self.call_evidence != "stale")

    def _on_closing(self, reason: str) -> None:
        """The conversation decided to end: listen for the name again during its teardown."""
        self._note_dismissed(reason)   # review F1: before any hit can be kept as the next wake
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

    #: After the user ended a conversation ("stop listening"), a wake this soon is judged, not taken as addressed
    #: (v3.12: live 13:32:22 his "I had a long talk with, uh, Yuki" woke it seconds after "Ok, stop listening").
    AFTER_GOODBYE_S = 90.0

    def _after_goodbye(self) -> bool:
        return time.monotonic() - getattr(self, "_goodbye_at", -1e9) < self.AFTER_GOODBYE_S

    def _on_thread_done(self, reason: str, error: str) -> None:
        if reason == "user_asked":
            self._goodbye_at = time.monotonic()
            self._note_dismissed(reason)   # review F1 (its own stop time; the same as at closing)
        self._arm_standby(1500 if not error else 10_000)   # the next conversation waits ready for the name (v3.11)
        self._thread = None
        self._conversation = None
        self.speaker_used = None
        self.closing = False
        self._emit_devices()
        self.conversation_changed.emit(False)
        pending, self._pending_wake = self._pending_wake, None
        if pending is not None and self._said_before_dismissal(pending[0], pending[2]):
            self.log.event("wake", state="ignored", why="before_dismissal", pending=True)   # review F1
            pending = None
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
        else:
            self._end_cue(reason)   # after the wake state too: "Say 'Yuki' to talk again" (v3.16)

    # -- standby: the conversation built ahead of the name (v3.11) --------------------------------------------------

    #: Waits (ms) before building the next standby after one was lost: each failure doubles it, up to the last.
    STANDBY_BACKOFF_MS = (5_000, 30_000, 120_000, 600_000)

    def _arm_standby(self, delay_ms: int) -> None:
        if getattr(self.settings, "voice_standby", True):
            self._standby_timer.start(max(0, int(delay_ms)))

    def _ensure_standby(self) -> None:
        """Build the next conversation now, dormant (GUI thread): when the name is on and nothing else runs.

        Everything a conversation needs is made and connected here, off the wake's path: the services, speech
        recognition's socket (sent no audio at all while it waits), the talker's and thinker's caches. What is not
        held while waiting: the speaker (an open output keeps the PC awake), the echo canceller's processing and the
        microphone subscription.
        """
        if not getattr(self.settings, "voice_standby", True) or not self._wake_allowed():
            return
        if self.in_conversation or (self._standby_thread is not None and self._standby_thread.is_alive()):
            return
        from yuki.voice.keys import DEEPGRAM, get_key

        prefs = load_prefs(self._prefs_path)
        if not get_key(DEEPGRAM) and str(prefs.get("stt_provider") or "").lower() != "local":
            return
        session = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        self._standby_thread = threading.Thread(target=self._standby_main, args=(session,), name="yuki-voice-standby",
                                                daemon=True)
        self._standby_thread.start()

    def _drop_standby(self, reason: str, wait_s: float = 0.0) -> None:
        conversation, thread = self._standby_conv, self._standby_thread
        if conversation is not None:
            conversation.request_stop(reason)
        if wait_s and thread is not None:
            thread.join(timeout=wait_s)

    def _on_standby_done(self, reason: str) -> None:
        """A standby ended unwoken: build the next one (at once after a normal end, with a backoff after a failure)."""
        self._standby_thread = None
        self._standby_conv = None
        self._standby_output = None
        if reason in ("wake_off", "quit", "rebuild"):
            if reason == "rebuild":
                self._arm_standby(500)
            return
        self._standby_failures += 1
        wait = self.STANDBY_BACKOFF_MS[min(self._standby_failures - 1, len(self.STANDBY_BACKOFF_MS) - 1)]
        self._arm_standby(wait)

    def rebuild_standby(self) -> None:
        """A setting it was built with changed (the voice, the microphone): the next one is built afresh."""
        if self._standby_conv is not None:
            self._drop_standby("rebuild")
        else:
            self._arm_standby(500)

    def _standby_main(self, session: str) -> None:
        """The ``yuki-voice-standby`` thread: build a dormant conversation and run it until woken or lost."""
        from dataclasses import replace

        from yuki.voice.mouth import fetch_memory
        from yuki.voice.pipeline import preflight

        started = time.monotonic()
        reason, error = "error", ""
        conversation = None
        settings = conversation_settings(self.settings, load_prefs(self._prefs_path))
        prewarming = getattr(self, "_prewarm_job", None)
        importer = (prewarming if prewarming is not None and not prewarming.done()
                    else _background(_import_pipeline, "yuki-voice-import"))
        from yuki.voice.keyterms import memory_keyterms

        keyterms_job = _background(lambda: memory_keyterms(self.memory, limit=settings.voice_keyterms_max),
                                   "yuki-voice-keyterms")
        def on_speakers() -> bool:
            try:
                from yuki.voice.audio import headphones_check

                ok, _facts = headphones_check(settings.voice_headphone_devices,
                                              device=self._output_target(self._resolve("render", quiet=True)))
                return not ok
            except Exception:  # noqa: BLE001
                return True

        if not self._aec.get("apm") and settings.voice_echo != "full" and on_speakers():
            # The echo canceller is loaded while waiting, not on the wake (the first reply can come ~2 s after the
            # name, before a canceller loaded then would be ready: offline 2026-09-28 Yuki heard her own answer).
            def load_aec() -> None:
                from yuki.voice.echo import load_apm

                apm, why = load_apm()
                if apm is not None and not self._aec.get("apm"):
                    self._aec = {"output": None, "apm": apm, "converged": False}
                elif apm is None:
                    self.log.event("echo", state="aec_unavailable", why=why)

            _background(load_aec, "yuki-voice-aec")
        try:
            mic = self._ensure_mic()
            busy = not self._pc_free()   # vLocal: a game or a call (speech on this PC only if already loaded)
            self._local_setup(settings, busy=busy)
            facts = preflight(settings, need_headphones=False, busy=busy)
            self._standby_local = self._local_choice(settings, facts)
            if facts.get("tts_voice"):
                settings = replace(settings, voice_tts_voice=facts["tts_voice"])
            if facts.get("stt_provider", settings.voice_stt_provider) != settings.voice_stt_provider:
                settings = replace(settings, voice_stt_provider=facts["stt_provider"])   # vLocal: not ready yet
            mouth_memory = fetch_memory(self.memory)
            try:
                keyterms, keyterm_facts = keyterms_job.result(timeout=3.0)
            except Exception as exc:  # noqa: BLE001 - recognition works without them
                keyterms, keyterm_facts = [], {"terms": 0, "error": type(exc).__name__}
            importer.result()
            from pipecat.transports.base_transport import TransportParams

            from yuki.voice.pipeline import Conversation
            from yuki.voice.transport import MicInput, PairTransport, SpeakerOutput

            params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16_000,
                                     audio_out_enabled=True, audio_out_sample_rate=24_000)
            mic_input = MicInput(mic, params, dormant=True)
            output = SpeakerOutput(None, params, qos=bool(getattr(settings, "voice_qos", True)),
                                   mmcss=bool(getattr(settings, "voice_mmcss", True)))
            box: dict[str, Any] = {}

            def closing(why: str) -> None:
                conv = box.get("conv")
                if conv is not None and conv.woken_at is not None:
                    gui_poster().post(lambda: self._closing.emit(why))   # only a woken one is the user's (v3.14b: off the loop)

            conversation = Conversation(
                settings=settings, bridge=self.bridge, transport=PairTransport(mic_input, output), memory=self.memory,
                mouth_memory=mouth_memory, log=self.log,
                speech_end=lambda: mic.stats.last_voiced or None,
                voice_activity=lambda: (mic.stats.voiced_since, mic.stats.last_voiced),
                on_state=posted(self._thread_state.emit), session=session,
                tts_provider=facts.get("tts_provider"), call_app=self.call_app, on_closing=closing,
                extra_observers=list(self.extra_observers), keyterms=keyterms, on_chat=posted(self._chat),
                call_evidence=self.call_evidence if self.call_app else None, on_quiet=self.go_quiet, dormant=True)
            box["conv"] = conversation
            conversation.on_tone = self._earcon   # v3.16: the conversation's earcons (error, note) via its speaker
            output.on_write_failing = self._reopen_speaker
            output.on_write_failed = lambda exc, c=conversation: self._speaker_failed(c, exc)

            def connected() -> None:
                if box.get("ready"):
                    return
                box["ready"] = True
                self._standby_failures = 0
                self.log.event("standby", state="ready", session=session,
                               built_ms=round((time.monotonic() - started) * 1000),
                               tts=facts.get("tts_provider"), stt=facts.get("stt_provider"), keyterms=keyterm_facts)

            conversation.on_stt_connected = connected
            self._standby_output = output
            self._standby_conv = conversation
            conversation._standby_meta = {"session": session, "settings": settings, "facts": facts, "mic_input": mic_input,
                                  "mouth_memory": mouth_memory, "keyterms": keyterm_facts}
            reason = asyncio.run(conversation.run())
            if conversation.observer.fatal:
                error = _plain_error(conversation.observer.fatal)
        except Exception as exc:  # noqa: BLE001 - reported, never raised into Qt
            error = _plain_error(f"{type(exc).__name__}: {exc}")
        finally:
            woken = conversation is not None and conversation.woken_at is not None
            if not woken:
                self.log.event("standby", state="stop", session=session, reason=reason, error=error or None,
                               dormant_s=round(time.monotonic() - started))
                self._standby_done.emit(reason if not error else "error")
            else:
                self._finish_woken(conversation, reason, error, settings)

    def _wake_evidence(self, via: str, listener: Any) -> tuple[bool, bool]:
        """``(woken_by_name, name_check)`` for a conversation starting now.

        ``woken_by_name``: the spotter's own hit makes the first words said to Yuki (vGame: whether or not the name could
        be cut from them). Not for a sound-alike the second opinion rescued, and (review F2/F5) not in a game or a call
        or right after a goodbye (:attr:`_wake_strict`): there ``name_check`` is set instead, and the first words are
        said to Yuki when speech recognition wrote the name opening them (the name was kept in what it hears)."""
        woken = via == "wake" and not bool(getattr(listener, "rescued", False)) and not self._wake_strict
        return woken, via == "wake" and not woken

    def _wake_standby(self, via: str, listen_from: float, started: float, name_cut: bool) -> bool:
        """Wake the dormant conversation (GUI thread): its words stream at once, the devices open alongside."""
        conversation = self._standby_conv
        if conversation is None or not getattr(conversation, "ready", False):
            return False
        listener = self._listener
        # vGame: the spotter's own hit is the evidence the first words were said to Yuki, whether or not the name could
        # be cut from them (its token times are often not the name's on a long stream: then the name is replayed too;
        # live 5 Oct 5 of 7 wakes, 4 of them in a call, lost "said to Yuki" that way)
        del name_cut
        woken_by_name, conversation.name_check = self._wake_evidence(via, listener)
        conversation.after_goodbye = via == "wake" and self._after_goodbye()
        self._wire_fast(conversation)
        if hasattr(conversation, "set_game"):
            conversation.set_game(getattr(self, "_wake_game", None))   # queued on its loop before the activation
        if not conversation.activate(listen_from=listen_from, woken_by_name=woken_by_name, call_app=self.call_app,
                                     call_evidence=self.call_evidence, heard_at=started):
            return False
        self._thread, self._standby_thread = self._standby_thread, None
        self._conversation, self._standby_conv = conversation, None
        output, self._standby_output = self._standby_output, None
        self.conversation_changed.emit(True)
        self._set_state("hearing" if self._mic is not None and time.monotonic() - (self._mic.stats.last_voiced or 0)
                        < 0.6 else "listening", "")
        threading.Thread(target=self._open_for_wake, args=(conversation, output, via, listen_from, started),
                         name="yuki-voice-wake-devices", daemon=True).start()
        return True

    def _open_for_wake(self, conversation: Any, output: Any, via: str, listen_from: float, started: float) -> None:
        """The woken conversation's speaker and echo guard (its words already stream to speech recognition)."""
        from yuki.voice.audio import headphones_check
        from yuki.voice.echo import choose_mode, tone

        meta = getattr(conversation, "_standby_meta", {}) or {}
        settings = meta.get("settings") or self.settings
        timings: dict[str, Any] = {"thread": round((time.monotonic() - started) * 1000), "stt_connected": 0,
                                   "replay_from_ms": round((listen_from - started) * 1000), "standby": True}
        try:
            mic = self._ensure_mic()
            speaker, out_facts = self._open_output(self._resolve("render"))
            self._speaker = speaker
            self.speaker_used = out_facts.get("device")
            timings["speaker"] = round((time.monotonic() - started) * 1000)
            on_headphones, heard = headphones_check(settings.voice_headphone_devices, device=out_facts.get("device"))
            if settings.voice_require_headphones and not on_headphones:
                speaker.close()
                self._speaker = None
                conversation.request_stop("error")
                self.notice(f"Voice needs headphones: sound is going to {heard.get('device') or 'the speakers'}.")
                return
            mode, why = choose_mode(on_headphones, preference=settings.voice_echo)
            guard = self._echo_guard(mode, out_facts.get("device"), speaker, mic)
            self._guard = guard
            mic.set_echo(guard)
            output.set_speaker(speaker)
            self.devices_changed.emit(self.devices_text())
            if settings.voice_wake_chime and via == "wake":
                threading.Thread(target=lambda: speaker.write(tone("listening"), gate=False),
                                 name="yuki-voice-chime", daemon=True).start()
            if self._output_note(out_facts):
                self._thread_state.emit("note", self._output_note(out_facts))
            timings["listening"] = round((time.monotonic() - started) * 1000)
            self.last_timings = timings
            facts = meta.get("facts") or {}
            memory = meta.get("mouth_memory")
            self.log.event("session", state="start", session=conversation.session, via=via, standby=True,
                           mic=mic.device_used, output=out_facts.get("device"), output_found=out_facts.get("found"),
                           output_latency_ms=out_facts.get("output_latency_ms"), headphones=heard, echo=mode,
                           echo_why=why, call_mode=bool(self.call_app), stt=self.settings.voice_stt_model,
                           stt_provider=settings.voice_stt_provider,
                           mouth=self.settings.voice_mouth_model, tts=facts.get("tts_provider"),
                           memory=memory.facts() if memory is not None else None, keyterms=meta.get("keyterms"),
                           timings_ms=dict(timings))
        except Exception as exc:  # noqa: BLE001 - the conversation goes on without a voice; said in the pill
            self.log.event("session", state="wake_devices_error", error=f"{type(exc).__name__}: {str(exc)[:200]}")
            conversation.request_stop("error")

    def _finish_woken(self, conversation: Any, reason: str, error: str, settings: Any) -> None:
        """A woken standby conversation ended: the same closing as a cold one."""
        started = conversation.woken_at or time.monotonic()
        speaker, self._speaker = self._speaker, None
        if speaker is not None:
            speaker.close()
        guard = self._guard
        if guard is not None:
            guard.close()
            if guard.converged and self._aec.get("apm") is not None:
                self._aec["converged"] = True
            self.log.event("echo", op="summary", session=conversation.session, mode=guard.mode,
                           effective=guard.effective, **guard.stats.facts())
        if self._mic is not None:
            self.log.event("mic", state="levels", **self._mic.stats.levels())
        obs = conversation.observer
        mic_input = (getattr(conversation, "_standby_meta", {}) or {}).get("mic_input")
        timings = dict(self.last_timings or {})
        if mic_input is not None:
            timings["kept_s"] = round(mic_input.kept_s, 2)
            timings["replayed_s"] = round(mic_input.replayed_s, 2)
            self.last_timings = timings
        error = error or self._take_pending_error()
        self.log.event("session", state="stop", session=conversation.session, reason=reason, error=error or None,
                       seconds=round(time.monotonic() - started, 1), turns=obs.totals["turns"],
                       for_others=obs.totals.get("for_others", 0), mouth_cost_usd=round(obs.totals["mouth_cost_usd"], 5),
                       tts_chars=obs.totals["tts_chars"],
                       stt_cost_usd=round((time.monotonic() - started) / 60 * self.settings.voice_usd_per_stt_min
                                            * (getattr(settings, "voice_stt_provider", "") != "local"), 5),
                       timings_ms=timings, standby=True, **_stop_facts(conversation))
        self._last_end = {"reason": reason, "exchanges": int(getattr(obs, "exchanges", 0) or 0)}
        self._thread_done.emit(reason, error)

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
            # A canceller loaded ahead by the standby (for no output in particular) is taken by the first one.
            kept = self._aec if self._aec.get("output") in (output, None) else {}
            if kept and kept.get("output") is None:
                kept["output"] = output
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
            busy = not self._pc_free()   # vLocal: a game or a call (speech on this PC only if already loaded)
            self._local_setup(settings, busy=busy)
            facts = preflight(settings, busy=busy)
            mark("preflight")
            if facts.get("tts_voice"):
                # The chosen voice was refused (e.g. needs a paid plan): this conversation only.
                settings = replace(settings, voice_tts_voice=facts["tts_voice"])
                self.log.event("voice_id", state="refused_fallback", note=facts.get("tts_note"))
            if facts.get("stt_provider", settings.voice_stt_provider) != settings.voice_stt_provider:
                settings = replace(settings, voice_stt_provider=facts["stt_provider"])   # vLocal: not ready yet
                self.log.event("local_models", op="stt_fallback", note=facts.get("stt_note"))
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
            output = SpeakerOutput(speaker, params, qos=bool(getattr(settings, "voice_qos", True)),
                                   mmcss=bool(getattr(settings, "voice_mmcss", True)))
            transport = PairTransport(mic_input, output)
            conversation = Conversation(
                settings=settings, bridge=self.bridge, transport=transport, memory=self.memory,
                mouth_memory=mouth_memory, log=self.log,
                speech_end=lambda: mic.stats.last_voiced or None,
                voice_activity=lambda: (mic.stats.voiced_since, mic.stats.last_voiced),
                on_state=posted(self._thread_state.emit), session=session,
                tts_provider=facts.get("tts_provider"), call_app=call_app,
                on_closing=posted(self._closing.emit), extra_observers=list(self.extra_observers), keyterms=keyterms,
                on_chat=posted(self._chat), call_evidence=self.call_evidence if call_app else None, on_quiet=self.go_quiet,
            )
            conversation.on_stt_connected = lambda: mark("stt_connected")
            conversation.on_tone = self._earcon   # v3.16: the conversation's earcons (error, note) via its speaker
            self._wire_fast(conversation)          # vGame
            if hasattr(conversation, "set_game"):
                conversation.set_game(getattr(self, "_wake_game", None) if via == "wake" else self._game_now())
            output.on_write_failing = self._reopen_speaker
            output.on_write_failed = lambda exc, c=conversation: self._speaker_failed(c, exc)
            # What recognition hears starts after the name: the mouth is told its first words were said to it
            # (not when the name was a sound-alike the second opinion heard as the name: then it judges).
            listener = self._listener
            conversation.after_goodbye = via == "wake" and self._after_goodbye()
            conversation.woken_by_name, conversation.name_check = self._wake_evidence(via, listener)
            if facts.get("tts_note") or facts.get("stt_note"):
                self._thread_state.emit("starting", facts.get("tts_note") or facts["stt_note"])
            self._conversation = conversation
            if self.call_app != call_app:  # the call started or ended while this was starting
                conversation.set_call(self.call_app, self.call_evidence)
            mark("built")
            self.last_timings = timings
            self.log.event("session", state="start", session=session, via=via, mic=mic.device_used,
                           mic_opened_as=mic.opened_as, mic_format=getattr(mic, "format", None),
                           output=out_facts.get("device"), output_wanted=out_facts.get("wanted"),
                           output_tray_choice=out_facts.get("tray_choice"), output_found=out_facts.get("found"),
                           output_refreshed=out_facts.get("refreshed"), output_index=out_facts.get("index"),
                           output_latency_ms=out_facts.get("output_latency_ms"),
                           headphones=heard, echo=mode, echo_why=why, call_mode=bool(call_app),
                           stt=self.settings.voice_stt_model, stt_provider=settings.voice_stt_provider,
                           mouth=self.settings.voice_mouth_model, tts=facts.get("tts_provider"),
                           tts_note=facts.get("tts_note"),
                           tts_model=(settings.voice_gemini_model if facts.get("tts_provider") == "gemini"
                                      else self.settings.voice_tts_model),
                           tts_voice_set=bool(settings.voice_tts_voice),
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
            error = error or self._take_pending_error()
            self.log.event("session", state="stop", session=session, reason=reason, error=error or None,
                           seconds=round(time.monotonic() - started, 1),
                           turns=obs.totals["turns"] if obs else 0,
                           for_others=obs.totals.get("for_others", 0) if obs else 0,
                           mouth_cost_usd=round(obs.totals["mouth_cost_usd"], 5) if obs else 0,
                           tts_chars=obs.totals["tts_chars"] if obs else 0,
                           stt_cost_usd=round((time.monotonic() - started) / 60 * self.settings.voice_usd_per_stt_min
                                            * (getattr(settings, "voice_stt_provider", "") != "local"), 5),
                           timings_ms=dict(timings), **_stop_facts(self._conversation))
            self._last_end = {"reason": reason, "exchanges": int(getattr(obs, "exchanges", 0) or 0) if obs else 0}
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

    # D7 (v3.17): what the audit of an offline session found imported on the voice loop (each a read of .pyc files,
    # once a .pyc written): the narrator's shared gates, the voice's own late imports, and the talker's client's HTTP
    # stack (httpx, httpcore, h11, the SDK's base client, read lazily when a client is first made).
    import yuki.core.attention  # noqa: F401
    import yuki.voice.gcwatch  # noqa: F401
    import yuki.voice.priority  # noqa: F401
    import yuki.voice.qos  # noqa: F401
    import yuki.voice.router  # noqa: F401
    import yuki.voice.speechguard  # noqa: F401
    import yuki.voice.stallwatch  # noqa: F401
    import yuki.voice.steplines  # noqa: F401
    import yuki.voice.ttscache  # noqa: F401
    # v3.18: practice memory's lookups and the learned-way runner (the audit found the lookup imported in run)
    import yuki.voice.practice  # noqa: F401
    import yuki.voice.skills  # noqa: F401
    from yuki.voice.gemini_tts import service_class

    service_class()
    # vLocal: the services for speech on this PC (their models load on their own thread, never here)
    import yuki.voice.local_stt  # noqa: F401
    import yuki.voice.local_tts  # noqa: F401

    # The TLS context every service connection uses, built here off the voice loop (v3.6: built per
    # connection on the loop it stalled the loop 2.8 s live, reading the Windows certificate store).
    shared_ssl_context()
    use_shared_tls()
    try:
        from yuki.config import Settings
        from yuki.voice.pipeline import bedrock_client

        settings = Settings()
        bedrock_client(settings.aws_region, getattr(settings, "model_backend", None))   # thrown away: its imports
    except Exception:  # noqa: BLE001 - only a head start
        pass


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
        for host in ("api.deepgram.com", "generativelanguage.googleapis.com", "api.elevenlabs.io",
                     f"bedrock-runtime.{settings.aws_region}.amazonaws.com"):
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
    # v3.14: everything is imported now: one collection, then the heap so far is frozen, so the collector's later
    # full passes (which hold the interpreter lock and so freeze the voice loop) walk only what came after
    # (yuki.voice.gcwatch; measured there).
    t = time.perf_counter()
    try:
        from yuki.voice import gcwatch

        gcwatch.watch()
        facts["gc"] = gcwatch.freeze_heap()
        facts["gc_ms"] = round((time.perf_counter() - t) * 1000)
    except Exception as exc:  # noqa: BLE001 - only an optimisation
        facts["gc_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
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


def _quietly(fn: Any, *args: Any, **kwargs: Any) -> None:
    """Call ``fn``; a failure is swallowed (an earcon never breaks anything)."""
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass


def _stop_facts(conversation: Any) -> dict[str, Any]:
    """Why and how a conversation ended (content-free; ``Conversation.stop_facts``, 04 F3), for ``session stop``."""
    facts = getattr(conversation, "stop_facts", None) or {}
    return {k: v for k, v in facts.items() if k != "reason"} if isinstance(facts, dict) else {}
