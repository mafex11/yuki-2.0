"""The island: Yuki's one always-on surface, top centre, in the Lattice design (phase E6, E7).

It merges what were three windows (the voice pill, the run strip and the call pill) into one small window with the
dot-matrix face (:class:`yuki.ui.dots.FaceWidget`), one status line, a mono sub line, chips on the right, and a bounded
halo under it (:class:`yuki.ui.dots.HaloWidget`, in its own click-through window). Its states, and which one wins when
several apply (:func:`resolve`):

==================  ====================================================================================
game-quiet          a full-screen app (a game, a video, a presentation) or the session locked: hidden; the
                    tray carries the call timer (one clock, never reset)
needs-you           Yuki asked something and waits (auto-expands: the question and Answer / Stop)
voice-stopped       voice failed: the reason and the fix (Retry, Settings), on screen at least 8 s
speaking            Yuki talks: the words being said, "say stop to cut me off"
hearing, listening  your turn: a live caption where unsure words are dimmed (auto-expands)
working, thinking   a request runs: the step line, "step n", the time, Stop and the run card's chevron
done-flash          the run ended: the answer (or Stopped / the error) for 4 s, a stamped check
quiet-in-call       a call is recorded: "Quiet. You're on Discord." with the rec chip and the session's timer
not-listening       Yuki isn't listening (switched off, quiet, off for this call): the mini island, eyes shut
hidden-armed        listening for its name only: the mini island, two resting dots
==================  ====================================================================================

**The run card** (E7) expands from the island (the chevron, or a click on it while a run goes): the live steps from
the chat thread (:class:`yuki.ui.chat_model.ChatModel`), "from memory" and "using your lesson" chips where the agent's
task frame and its ``playbook`` field say so, the steer box ("added, not yet read" -> "read at step n", measured: the
next step taken after it was added), Stop, and "I'll take it from here".

Rules it keeps: never takes focus (``WS_EX_NOACTIVATE``; only a click into the steer box lets it take the keyboard,
and Enter gives it back), sits over full-screen-free desktops on the monitor in use (the foreground window's), hides
in games, paints from the shared tokens, at most 30 frames a second on the one shared loop, no timer while hidden,
nothing repaints when idle, and "reduce motion" draws still frames. The controller (:class:`Island`) decides only what
is on screen; every action goes out as a signal for :mod:`yuki.ui.app` to carry out.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from PySide6.QtCore import QObject, QPoint, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QFontMetricsF, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QLineEdit, QSizePolicy, QWidget

from yuki.ui import lattice as L
from yuki.ui import tokens as T
from yuki.ui.dots import FACE_W, HALO_GAP, HALO_H, HALO_W, FaceWidget, HaloWidget

# -- geometry and timing (tokens ``island`` / ``motion``; the design's CSS) ---------------------------------

BAR_H = int(T.ISLAND["heightPx"])          # 74
MINI_H = int(T.ISLAND["miniHeightPx"])     # 30
MIN_W = int(T.ISLAND["minWidthPx"])        # 360
MAX_W = int(T.ISLAND["maxWidthPx"])        # 760
TOP_PX = int(T.ISLAND["topPx"])            # 12
CAPTION_INDENT = int(T.ISLAND["captionIndentPx"])   # 162
RADIUS = int(T.RADIUS["island"])           # 22
RADIUS_EXP = int(T.RADIUS["r4"])           # 20 (an expanded island)
RADIUS_MINI = int(T.RADIUS["islandMini"])  # 13
RUN_W = 600                                # the design's ``.runcard`` width
MORPH_MS = int(L.DUR["islandMorph"])       # 300
ENTER_MS = 300
LEAVE_MS = 260
HALO_IN_MS = 360
#: How long the run's end word stays (the strip's linger; the face's smile and the halo's check settle in 1.2 s).
DONE_MS = 4000
#: A voice failure stays at least this long, even when voice has already gone back to waiting for its name.
VOICE_ERROR_MS = 8000
#: After the run ended, an open run card stays this long (untouched) before it folds back.
RUN_CARD_LINGER_MS = 8000
#: The design's ``.bar`` also matches its Me page's dot-bar rule (``.bar { margin-left: 8px }``), so every island bar
#: sits 8 px right of its padding (the mini island is 8 px wider): kept, so the island is the design's to the pixel.
BAR_SHIFT = 8
#: The window: the widest island, so morphs paint inside it (the rest is transparent and lets clicks through).
WIN_W = MAX_W

STATES: tuple[str, ...] = ("game-quiet", "needs-you", "voice-stopped", "speaking", "hearing", "listening", "working",
                           "thinking", "done-flash", "quiet-in-call", "not-listening", "hidden-armed")
#: States with the halo figure of the same name (the run card has none).
HALO_STATES = frozenset({"listening", "hearing", "thinking", "working", "speaking", "needs-you", "quiet-in-call",
                         "voice-stopped", "done-flash"})
#: The words the island and the tray use for "Yuki isn't listening" (v3.16, the voice session's states).
NOT_LISTENING_WORDS = {"off": "Yuki isn't listening: switched off", "quiet": "Yuki isn't listening: quiet for a while",
                       "call_off": "Yuki isn't listening: off for this call"}
#: Windows' notification states that mean a full screen (a game, a video, a presentation) or nobody there.
FULLSCREEN_STATES = frozenset({1, 2, 3, 4})


# -- what the island knows ---------------------------------------------------------------------------------


@dataclass
class Steer:
    """One thing the user added to a running request, and what became of it."""

    text: str
    state: str = "added, not yet read"      # -> "read at step n" | "arrived after the last step" | "it had finished"
    at_step: int = 0


@dataclass
class RunInfo:
    """The request the island shows."""

    id: int
    started: float                           # time.monotonic()
    steps: int = 0
    line: str = ""
    hint: str = ""
    stopping: bool = False
    request: str = ""
    of: int = 0                              # the plan's step count when one is known (0: not known)
    steers: list[Steer] = field(default_factory=list)
    chips: dict[int, list[tuple]] = field(default_factory=dict)   # step number -> [(icon, text[, playbook ref])]
    frame: dict[str, Any] | None = None


@dataclass
class FinalInfo:
    """How the shown run ended: ``done`` | ``stopped`` | ``failed`` | ``handed``."""

    text: str
    outcome: str = "done"
    meta: str = ""
    run: RunInfo | None = None
    ended: float = 0.0                       # time.monotonic() when it ended (the run card's time)
    cost: str = ""                           # the run's cost, once the summary came ("33¢")


@dataclass
class IslandInputs:
    voice: str = "off"
    voice_detail: str = ""
    voice_error: str = ""
    listening: str = "on"
    run: RunInfo | None = None
    question: str = ""
    question_id: int | None = None
    question_voice: bool = False
    call: dict | None = None
    fullscreen: bool = False
    fullscreen_title: str = ""
    final: FinalInfo | None = None
    transient: str = ""
    spoken: str = ""
    caption: list[tuple[str, float | None]] = field(default_factory=list)
    heard: str = ""
    waiting: int = 0


def resolve(i: IslandInputs) -> str:
    """Which state the island shows when several apply (most urgent first; see the module docstring)."""
    if i.fullscreen:
        return "game-quiet"
    if i.question:
        return "needs-you"
    if i.voice_error:
        return "voice-stopped"
    if i.voice == "speaking":
        return "speaking"
    if i.voice == "hearing":
        return "hearing"
    if i.voice in ("listening", "starting"):
        return "listening"
    if i.final is not None:
        return "done-flash"
    if i.run is not None:
        return "working" if i.run.steps > 0 else "thinking"
    if i.voice in ("thinking", "working") or i.transient:
        return "thinking"
    if call_on(i.call):
        return "quiet-in-call"
    if i.listening != "on":
        return "not-listening"
    return "hidden-armed"


def call_on(call: dict | None) -> bool:
    return bool(call) and call.get("status") in ("recording", "waiting_headphones")


def call_since(call: dict | None) -> float | None:
    """When the call **session** started (epoch s): ``session_started_at`` (one running time across rows the
    service cut and reopened), else the row's own start."""
    if not call:
        return None
    from yuki.ui.memory import local_moment

    m = local_moment(call.get("session_started_at") or call.get("detected_at") or call.get("recording_since"))
    return m.timestamp() if m is not None else None


def clock(seconds: float | None) -> str:
    """The call timer: ``46:04``, ``1:02:05`` past an hour; ``""`` without a start."""
    if seconds is None:
        return ""
    secs = max(0, int(seconds))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def elapsed(seconds: float) -> str:
    """A run's time: ``0:46``, ``12:05``, ``1:02:05``."""
    secs = max(0, int(seconds))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def step_of(run: RunInfo | None) -> tuple[int, int]:
    """The face's and the halo's step: ``(n, of)``; without a known plan, five segments and the current one filling."""
    if run is None:
        return (2, 5)
    of = run.of if run.of > 0 else 5
    n = max(1, min(run.steps if run.steps > 0 else 1, of))
    return (n, of)


@dataclass
class Spec:
    """What the island draws for a state (the design's ``islandSpec``)."""

    state: str
    face: str = "idle"
    line: str = ""
    sub: str = ""
    width: int = 0              # 0: fit the content within MIN_W..MAX_W
    exp: bool = False
    mini: bool = False
    hidden: bool = False
    halo: str | None = None
    step: tuple[int, int] = (2, 5)
    rec: bool = False
    keys: tuple[str, ...] = ()  # right side, in order: stop, chev, retry, settings
    chip: str = ""              # a plain chip on the right: "done", "stopped", "failed", "handed"
    body: str = ""              # caption | choices | run
    title: str = ""             # the tooltip
    line_left: bool = False     # elide the line from the left (speaking: the newest words stay)


def spec_for(state: str, i: IslandInputs, *, voice_key: str = "", stop_key: str = "", expanded: bool = False,
             now: float | None = None) -> Spec:
    """The island's look for ``state`` from what it knows (pure: the tests read it)."""
    now = time.monotonic() if now is None else now
    rec = bool(i.call) and (i.call or {}).get("status") == "recording"
    run = i.run
    app = str((i.call or {}).get("app") or "the call")
    if state == "game-quiet":
        return Spec(state, face="game-quiet", hidden=True)
    if state == "hidden-armed":
        return Spec(state, face="hidden-armed", mini=True, title="Yuki is listening for its name")
    if state == "not-listening":
        return Spec(state, face="not-listening", mini=True, title=NOT_LISTENING_WORDS.get(i.listening, "Yuki isn't listening"))
    if state == "listening":
        starting = i.voice == "starting"
        sub = (f"{voice_key} to stop · dim words are unsure" if voice_key else "dim words are unsure")
        return Spec(state, face="woke" if starting else "listening", exp=True, width=600,
                    line="Yes?" if starting else "Listening", sub="starting voice…" if starting else sub, rec=rec,
                    body="caption", halo="listening")
    if state == "hearing":
        return Spec(state, face="hearing", exp=True, width=660, line="Hearing you", sub="Low-confidence words are dimmed",
                    rec=rec, body="caption", halo="hearing")
    if state == "speaking":
        return Spec(state, face="speaking", width=660, line=i.spoken or "Speaking",
                    sub="Speaking · say “stop” to cut me off", rec=rec, halo="speaking", line_left=bool(i.spoken))
    if state == "needs-you":
        sub = "Say it, or press Answer to type it" if i.question_voice else "Answer in the ask bar"
        return Spec(state, face="needs-you", exp=True, width=600, line=i.question, sub=sub, rec=rec, halo="needs-you",
                    body="choices", title=i.question)
    if state == "voice-stopped":
        return Spec(state, face="voice-stopped", width=700, line="Voice stopped",
                    sub=i.voice_error, keys=("retry", "settings"), halo="voice-stopped", title=i.voice_error)
    if state == "working" and expanded and run is not None:
        n, of = step_of(run)
        head = f"step {run.steps}" + (f" of {run.of}" if run.of else "")
        return Spec("working", face="working", exp=True, width=RUN_W, line=run.line or "Working…",
                    sub=f"{head} · {elapsed(now - run.started)}", rec=rec, step=(n, of), body="run", keys=("chev",))
    if state == "working" and run is not None:
        n, of = step_of(run)
        head = f"step {run.steps}" + (f" of {run.of}" if run.of else "")
        return Spec(state, face="working", width=680, line=run.line or "Working…",
                    sub=f"{head} · {elapsed(now - run.started)}", rec=rec, keys=("stop", "chev"), halo="working",
                    step=(n, of))
    if state == "thinking":
        if run is not None:
            sub = run.hint or (f"you said “{i.heard}”" if i.heard else "") or elapsed(now - run.started)
            if run.hint or i.heard:
                sub = f"{elapsed(now - run.started)} · {sub}"
            line = run.line or "Thinking…"
            if expanded:
                return Spec("thinking", face="thinking", exp=True, width=RUN_W, line=line, sub=sub, rec=rec,
                            body="run", keys=("chev",))
            return Spec(state, face="thinking", line=line, sub=sub, rec=rec, keys=("stop", "chev"), halo="thinking")
        return Spec(state, face="thinking", line=i.transient or "Thinking…",
                    sub=(f"you said “{i.heard}”" if i.heard else "Working it out"), rec=rec, halo="thinking")
    if state == "done-flash":
        f = i.final or FinalInfo("Done.")
        if expanded and f.run is not None:
            return Spec(state, face="done-flash" if f.outcome == "done" else "handed", exp=True, width=RUN_W,
                        line=f.text, sub=f.meta or _final_sub(f), rec=rec, body="run", keys=("chev",))
        if f.outcome == "done":   # the design's done flash carries its chip only (no rec chip)
            return Spec(state, face="done-flash", line=f.text, sub=f.meta or _final_sub(f), chip="done",
                        halo="done-flash")
        return Spec(state, face="handed", line=f.text, sub=f.meta or _final_sub(f), chip=f.outcome)
    if state == "quiet-in-call":
        waiting = (i.call or {}).get("status") == "waiting_headphones"
        line = f"Waiting for headphones, {app}" if waiting else f"Quiet. You’re on {app}."
        if i.listening != "on":
            sub = NOT_LISTENING_WORDS.get(i.listening, "Yuki isn't listening")
        else:
            sub = "Say “Yuki” when it’s for me"
        return Spec(state, face="quiet-in-call", line=line, sub=sub, rec=rec, halo="quiet-in-call")
    return Spec("hidden-armed", face="hidden-armed", mini=True)


def _final_sub(f: FinalInfo) -> str:
    """The end word's sub line (the design's run card heads: "Nothing was sent · 0:44", "You have the controls")."""
    word = {"done": "Done", "stopped": "Stopped by you", "failed": "It didn't finish",
            "handed": "You have the controls"}.get(f.outcome, "")
    if f.run is not None and f.ended:
        return f"{word} · {elapsed(f.ended - f.run.started)}"
    return word


def caption_spans(words: list[tuple[str, float | None]], threshold: float) -> tuple[str, list[L.Span]]:
    """The caption's text and its dimmed (unsure) words: a word whose speech-recognition confidence is under
    ``threshold`` is drawn in text-3 with a dotted underline (the design's ``.lc``)."""
    text, spans, pos = "", [], 0
    for k, (w, conf) in enumerate(words):
        word = str(w)
        if k:
            text += " "
            pos += 1
        if conf is not None and float(conf) < threshold:
            spans.append(L.Span(pos, len(word), color=L.TEXT3, dotted=True))
        text += word
        pos += len(word)
    return text, spans


def unsure_threshold() -> float:
    """The confidence under which a heard word reads as unsure: the voice side's own constant when it can be read
    without loading the voice pipeline, else the design's 0.6."""
    import sys

    mod = sys.modules.get("yuki.voice.pipeline")
    value = getattr(mod, "LOW_WORD_CONFIDENCE", None) if mod is not None else None
    return float(value) if isinstance(value, (int, float)) else 0.6


# -- the monitor in use, full screen ------------------------------------------------------------------------


def _is_windows() -> bool:
    return QGuiApplication.platformName() == "windows"


def foreground_hwnd() -> int:
    if not _is_windows():
        return 0
    try:
        user32 = ctypes.WinDLL("user32")
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        return int(user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def monitor_device(hwnd: int) -> str:
    """The display device name (``\\\\.\\DISPLAY2``) of the monitor ``hwnd`` is on; ``""`` when unknown."""
    if not hwnd or not _is_windows():
        return ""
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        mon = user32.MonitorFromWindow(hwnd, 2)          # MONITOR_DEFAULTTONEAREST
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFOEXW)]
        if not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            return ""
        return str(info.szDevice)
    except Exception:
        return ""


def window_title(hwnd: int) -> str:
    if not hwnd or not _is_windows():
        return ""
    try:
        user32 = ctypes.WinDLL("user32")
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 256)
        return buf.value
    except Exception:
        return ""


def pick_screen(device: str, screens: list[Any], primary: Any = None) -> Any:
    """The screen whose name is ``device`` (Qt names Windows screens by their display device), else the primary."""
    for s in screens:
        try:
            if device and s.name() == device:
                return s
        except Exception:
            continue
    return primary if primary is not None else (screens[0] if screens else None)


#: The display device of the monitor in use, as the foreground watch last saw it ("" until it has).
_ACTIVE_DEVICE: list[str] = [""]


def active_screen() -> Any:
    """The monitor in use: the foreground window's (kept fresh by :class:`ForegroundWatch`), else the primary."""
    device = _ACTIVE_DEVICE[0] or monitor_device(foreground_hwnd())
    return pick_screen(device, list(QGuiApplication.screens()), QGuiApplication.primaryScreen())


class ForegroundWatch(QObject):
    """Windows says when the window in front changes (``SetWinEventHook(EVENT_SYSTEM_FOREGROUND)``, out of context:
    delivered on this thread's message loop): no polling. Each change re-reads the monitor in use and whether a full
    screen covers it (``SHQueryUserNotificationState``)."""

    changed = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._hook = None
        self._proc = None

    def start(self) -> bool:
        if self._hook or not _is_windows():
            return bool(self._hook)
        try:
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32")
            proto = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
                                       wintypes.LONG, wintypes.DWORD, wintypes.DWORD)

            def on_event(_hook: Any, _event: int, hwnd: Any, _obj: int, _child: int, _thread: int, _t: int) -> None:
                try:
                    self.changed.emit(int(hwnd or 0))
                except Exception:
                    pass

            self._proc = proto(on_event)
            user32.SetWinEventHook.restype = wintypes.HANDLE
            self._hook = user32.SetWinEventHook(3, 3, None, self._proc, 0, 0, 0)   # EVENT_SYSTEM_FOREGROUND, OUTOFCONTEXT
            return bool(self._hook)
        except Exception:
            self._hook = None
            return False

    def stop(self) -> None:
        if self._hook:
            try:
                ctypes.WinDLL("user32").UnhookWinEvent(self._hook)
            except Exception:
                pass
            self._hook = None


# -- widgets ------------------------------------------------------------------------------------------------


class TypingInput(QLineEdit):
    """A line edit on a window that never takes focus: a click into it lets the window take the keyboard (only
    then), and Enter or Esc gives it back to the window that had it. Emits ``submitted(text)``."""

    submitted = Signal(str)

    def __init__(self, placeholder: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setFrame(False)
        self.setFont(L.ui_font(13.5))
        self.setStyleSheet(f"QLineEdit {{ background: transparent; border: none; color: {L.css(L.TEXT)};"
                           f" selection-background-color: {L.css(L.live())}; selection-color: {L.css(L.live_ink())}; }}")
        pal = self.palette()
        pal.setColor(pal.ColorRole.PlaceholderText, L.TEXT3)
        self.setPalette(pal)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._prev_hwnd = 0
        self._typing = False
        #: The question card's answer field (the design's ``.answer``): a dashed hair-2 underline, live when typing.
        self.underline = False

    def paintEvent(self, e: Any) -> None:  # noqa: D102
        super().paintEvent(e)
        if self.underline:
            p = QPainter(self)
            pen = QPen(L.live() if self.hasFocus() else L.HAIR2, 1.0)
            if not self.hasFocus():
                pen.setStyle(Qt.PenStyle.CustomDashLine)
                pen.setDashPattern([3.0, 3.0])
            p.setPen(pen)
            y = self.height() - 0.5
            p.drawLine(QRectF(0, y, self.width(), 0).topLeft(), QRectF(0, y, self.width(), 0).topRight())
            p.end()

    def mousePressEvent(self, e: Any) -> None:  # noqa: D102
        self.begin_typing()
        super().mousePressEvent(e)

    def begin_typing(self) -> None:
        win = self.window()
        if _is_windows() and not self._typing:
            from yuki.ui.focus import force_foreground, set_no_activate

            hwnd = int(win.winId())
            before = foreground_hwnd()
            self._prev_hwnd = before if before and before != hwnd else 0
            set_no_activate(hwnd, False)
            force_foreground(hwnd, allow_unlock=False)
        self._typing = True
        win.activateWindow()
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.window().update()

    def end_typing(self, *, give_back: bool) -> None:
        if not self._typing:
            return
        self._typing = False
        win = self.window()
        self.clearFocus()
        if _is_windows():
            from yuki.ui.focus import force_foreground, set_no_activate

            set_no_activate(int(win.winId()), True)
            if give_back and self._prev_hwnd:
                force_foreground(self._prev_hwnd, allow_unlock=False)
        self._prev_hwnd = 0
        win.update()

    def keyPressEvent(self, e: Any) -> None:  # noqa: D102
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            text = self.text().strip()
            if text:
                self.setText("")
                self.submitted.emit(text)
            self.end_typing(give_back=True)
            return
        if e.key() == Qt.Key.Key_Escape:
            self.end_typing(give_back=True)
            return
        super().keyPressEvent(e)

    def focusOutEvent(self, e: Any) -> None:  # noqa: D102
        super().focusOutEvent(e)
        if self._typing:
            self.end_typing(give_back=False)


class SteerBox(QWidget):
    """The run card's steer box (``.steer``): a 38 px input with an Enter hint, then what you added and its state."""

    submitted = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.input = TypingInput("Steer: “also…” or “no, the other one”", self)
        self.input.submitted.connect(self.submitted.emit)
        self.kbd = L.Kbd("Enter", self)
        self.rows: list[tuple[L.Para, L.Para]] = []
        self._steers: list[Steer] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_steers(self, steers: list[Steer]) -> None:
        self._steers = list(steers)
        while len(self.rows) < len(steers):
            self.rows.append((L.Para("", font=L.ui_font(13), line_height=1.45, parent=self, max_lines=1),
                              L.Para("", font=L.mono_font(11), line_height=1.6, parent=self, max_lines=1)))
        for k, (a, b) in enumerate(self.rows):
            on = k < len(steers)
            a.setVisible(on)
            b.setVisible(on)
            if on:
                s = steers[k]
                a.set_text(f"“{s.text}”")
                b.set_text(s.state)
                b.set_color(L.live() if s.state.startswith("added") else L.TEXT2 if s.state.startswith("read")
                            else L.TEXT3)
        self.setFixedHeight(self.height_for(self.width() or RUN_W - 36))
        self._place()

    def height_for(self, w: int) -> int:
        return 13 + 38 + sum(8 + int(round(13 * 1.45)) for _ in self._steers)

    def resizeEvent(self, e: Any) -> None:  # noqa: D102
        super().resizeEvent(e)
        self._place()

    def _place(self) -> None:
        w = self.width()
        top = 13
        kw = self.kbd.width()
        self.input.setGeometry(13, top + 1, max(10, w - 12 - 10 - kw - 6 - 2), 36)
        self.kbd.move(w - 1 - 6 - kw, top + (38 - self.kbd.height()) // 2)
        y = top + 38
        for k, (a, b) in enumerate(self.rows[: len(self._steers)]):
            y += 8
            lh = int(round(13 * 1.45))
            bw = int(b.natural_width()) + 2
            b.setGeometry(w - 2 - bw, y + (lh - int(round(11 * 1.6))) // 2, bw, int(round(11 * 1.6)))
            a.setGeometry(2, y, max(10, w - 4 - bw - 12), lh)
            y += lh

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(QRectF(0, 0, self.width(), 1), L.HAIR)
        r = QRectF(0.5, 12.5, self.width() - 1, 38)
        p.setPen(QPen(L.live_alpha(0.55) if self.input.hasFocus() else L.HAIR2, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 11.5, 11.5)
        p.end()


class StepRow(QWidget):
    """One step of the run card (``.steps li``): the mark (done, the turning spinner, failed, stopped), the verb in
    600 and its object, the time on the right, an optional note and chips under it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mark = L.Icon("check", 11, L.TEXT, self)
        self.spin = L.Spinner(11, self)
        self.text = L.Para("", font=L.ui_font(13.5), line_height=1.45, parent=self)
        self.ms = L.Para("", font=L.mono_font(11.5), line_height=1.5, parent=self, max_lines=1)
        self.note = L.Para("", font=L.ui_font(12.5), color=L.TEXT2, line_height=1.45, parent=self)
        self.chips: list[L.Chip] = []
        self.first = False
        self.kind = "done"

    def set_step(self, kind: str, verb: str, obj: str, ms: str, note: str = "",
                 chips: list[tuple[str, str]] | None = None, first: bool = False) -> None:
        self.kind, self.first = kind, first
        running = kind in ("running", "retrying")
        self.spin.setVisible(running)
        self.mark.setVisible(not running)
        self.mark.name = {"done": "check", "failed": "x", "cancelled": "dash", "skipped": "dash"}.get(kind, "dot")
        self.mark.ghost = kind == "pending"
        self.mark.set_color(L.TEXT if kind in ("done", "failed") else L.TEXT3)
        dim = kind in ("pending", "skipped", "cancelled")
        text = f"{verb} {obj}" if verb and obj else (verb or obj)
        spans = [L.Span(0, len(verb), weight=600, color=L.TEXT3 if dim else None)] if verb else []
        self.text.set_text(text, spans)
        self.text.set_color(L.TEXT3 if dim else L.TEXT)
        self.ms.set_text(ms)
        self.ms.set_color(L.live() if running else L.TEXT3)
        self.note.set_text(note)
        self.note.setVisible(bool(note))
        chips = chips or []
        while len(self.chips) < len(chips):
            self.chips.append(L.Chip("", kind="mem", parent=self))
        for k, c in enumerate(self.chips):
            on = k < len(chips)
            c.setVisible(on)
            if on:
                c.icon_name = chips[k][0]
                c.max_width = 0
                c.set_text(chips[k][1])
        self.update()

    def height_for(self, w: int) -> int:
        col = max(10, w - 18 - 10 - 10 - self._ms_w())
        h = 9 + max(self.text.heightForWidth(col), int(round(11.5 * 1.5))) + 9
        if self.note.isVisibleTo(self) and self.note.text():
            h += 10 - 2 + self.note.heightForWidth(w - 28)
        if any(c.isVisibleTo(self) for c in self.chips):
            h += 10 + 2 + 24
        return h

    def _ms_w(self) -> int:
        return int(self.ms.natural_width()) + 2 if self.ms.text() else 0

    def resizeEvent(self, e: Any) -> None:  # noqa: D102
        super().resizeEvent(e)
        self._place()

    def _place(self) -> None:
        w = self.width()
        msw = self._ms_w()
        col = max(10, w - 18 - 10 - 10 - msw)
        th = self.text.heightForWidth(col)
        self.mark.move(0 + 3, 9 + 3 + 1)
        self.spin.move(0 + 3, 9 + 3 + 1)
        self.text.setGeometry(28, 9, col, th)
        lh = int(round(11.5 * 1.5))
        self.ms.setGeometry(w - msw, 9, msw, lh)
        y = 9 + max(th, lh)
        if self.note.isVisibleTo(self) and self.note.text():
            y += 10 - 2
            nh = self.note.heightForWidth(w - 28)
            self.note.setGeometry(28, y, w - 28, nh)
            y += nh
        if any(c.isVisibleTo(self) for c in self.chips):
            y += 10 + 2
            x = 28
            for c in self.chips:
                if c.isVisibleTo(self):
                    c.max_width = max(40, w - x)
                    c._fit()
                    c.move(x, y)
                    x += c.width() + 6

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        if self.first:
            return
        p = QPainter(self)
        p.fillRect(QRectF(0, 0, self.width(), 1), L.HAIR)
        p.end()


class RunCard(QWidget):
    """The run card's body under the island's bar (the design's ``runCard``): the head (what it is doing, the step
    count in Doto), what you asked, the stop summary when stopped, the steps, the steer box, and the foot."""

    stop = Signal()
    handback = Signal()
    steer = Signal(str)
    ledger = Signal()
    close = Signal()
    again = Signal()
    save_lesson = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tt = L.Para("", font=L.ui_font(15, 600), line_height=1.45, parent=self, max_lines=1)
        self.touch = L.Para("", font=L.mono_font(11), color=L.TEXT3, line_height=1.3, parent=self, max_lines=1)
        self.count = L.Para("", font=L.doto_font(30, 800, 0.04), line_height=1.0, parent=self, max_lines=1)
        self.cost = L.Para("", font=L.mono_font(11), color=L.TEXT3, line_height=1.0, parent=self, max_lines=1)
        self.say_icon = L.Icon("chat", 12, L.TEXT3, self)
        self.say = L.Para("", font=L.ui_font(14), line_height=1.45, parent=self)
        self.sum_done_l = L.Para("Done", font=L.mono_font(11), color=L.TEXT3, line_height=1.2, parent=self)
        self.sum_not_l = L.Para("Not done", font=L.mono_font(11), color=L.TEXT3, line_height=1.2, parent=self)
        self.sum_done_i = L.Icon("check", 13, L.TEXT3, self)
        self.sum_not_i = L.Icon("dash", 13, L.TEXT3, self)
        self.sum_done = L.Para("", font=L.ui_font(13), line_height=1.45, parent=self)
        self.sum_not = L.Para("", font=L.ui_font(13), line_height=1.45, parent=self)
        self.rows: list[StepRow] = []
        self.empty = L.Para("", font=L.ui_font(13.5), color=L.TEXT3, line_height=1.45, parent=self)
        self.steerbox = SteerBox(self)
        self.steerbox.submitted.connect(self.steer.emit)
        self.k_stop = L.Key("Stop", variant="stop", icon="stop", kbd="Alt Shift X", parent=self)
        self.k_hand = L.Key("I’ll take it from here", parent=self)
        self.k_save = L.Key("Save the lesson", variant="pri", parent=self)
        self.k_again = L.Key("Ask again", variant="pri", parent=self)
        self.k_close = L.Key("Close", variant="ghost", parent=self)
        self.k_ledger = L.Key("Full ledger", variant="ghost", parent=self)
        self.k_ledger.setFont(L.mono_font(11.5))
        self.k_ledger.set_parts()
        self.foot_note = L.Para("", font=L.mono_font(11), color=L.TEXT3, line_height=1.0, parent=self, max_lines=1)
        self.k_stop.clicked.connect(self.stop.emit)
        self.k_hand.clicked.connect(self.handback.emit)
        self.k_ledger.clicked.connect(self.ledger.emit)
        self.k_close.clicked.connect(self.close.emit)
        self.k_again.clicked.connect(self.again.emit)
        self.k_save.clicked.connect(self.save_lesson.emit)
        self.mode = "live"
        self._say_on = False
        self._sum_on = False

    def set_card(self, *, mode: str, title: str, touch: str, count: str, cost: str, asked: str,
                 steps: list[dict[str, Any]], steers: list[Steer], summary: tuple[str, str] | None = None,
                 foot_note: str = "", stop_kbd: str = "Alt Shift X", lesson: bool = False) -> None:
        """``mode``: ``live`` | ``done`` | ``stopped`` | ``handed`` | ``failed``. ``steps``: ``{kind, verb, obj, ms,
        note, chips}`` in order."""
        self.mode = mode
        live = mode == "live"
        self.tt.set_text(title)
        self.touch.set_text(touch)
        self.touch.setVisible(bool(touch))
        self.count.set_text(count)
        self.cost.set_text(cost)
        self._say_on = bool(asked)
        self.say.set_text(f"“{asked}”" if asked else "")
        for w in (self.say, self.say_icon):
            w.setVisible(self._say_on)
        self._sum_on = summary is not None
        for w in (self.sum_done_l, self.sum_not_l, self.sum_done_i, self.sum_not_i, self.sum_done, self.sum_not):
            w.setVisible(self._sum_on)
        if summary is not None:
            self.sum_done.set_text(summary[0])
            self.sum_not.set_text(summary[1])
        while len(self.rows) < len(steps):
            self.rows.append(StepRow(self))
        for k, row in enumerate(self.rows):
            on = k < len(steps)
            row.setVisible(on)
            if on:
                s = steps[k]
                row.set_step(s.get("kind", "done"), s.get("verb", ""), s.get("obj", ""), s.get("ms", ""),
                             s.get("note", ""), s.get("chips"), first=k == 0)
        self.empty.set_text("" if steps else ("Getting ready…" if live else "No steps."))
        self.empty.setVisible(not steps)
        self.steerbox.setVisible(live)
        if live:
            self.steerbox.set_steers(steers)
        self.k_stop.set_parts(kbd=stop_kbd)
        for k in (self.k_stop, self.k_hand, self.k_ledger):
            k.setVisible(live)
        self.k_save.setVisible(mode == "done" and lesson)
        self.k_close.setVisible(not live)
        self.k_again.setVisible(mode in ("stopped", "handed", "failed"))
        self.foot_note.set_text(foot_note)
        self.foot_note.setVisible(not live and bool(foot_note))
        self._place(self.width() or RUN_W)

    # the design's spacing: head 14/18/12 (a hair rule on top), say 0 18, summary 12 18 0, steps from 12, steer 12 18 0,
    # foot 14 18 16
    def height_for(self, w: int) -> int:
        return self._place(w, apply=False)

    def resizeEvent(self, e: Any) -> None:  # noqa: D102
        super().resizeEvent(e)
        self._place(self.width())

    def _place(self, w: int, apply: bool = True) -> int:
        inner = w - 36
        y = 1 + 14
        cw = int(max(self.count.natural_width(), self.cost.natural_width())) + 2
        tw = max(10, inner - cw - 16)
        tth = int(round(15 * 1.45))
        if apply:
            self.tt.setGeometry(18, y, tw, tth)
        ty = y + tth + 4
        toh = int(round(11 * 1.3)) if self.touch.isVisibleTo(self) else 0
        if apply and toh:
            self.touch.setGeometry(18, ty, tw, toh)
        left_h = tth + (4 + toh if toh else 0)
        right_h = 30 + 4 + 11
        if apply:
            self.count.setGeometry(w - 18 - cw, y, cw, 30)
            self.cost.setGeometry(w - 18 - cw, y + 34, cw, 11)
            self.count.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        y += max(left_h, right_h) + 12
        if self._say_on:
            sw = inner - 2 - 24 - 12 - 10
            sh = self.say.heightForWidth(sw)
            box_h = 10 + sh + 10 + 2
            if apply:
                self._say_rect = QRectF(18, y, inner, box_h)
                self.say_icon.move(18 + 1 + 12, int(y + box_h / 2 - 6))
                self.say.setGeometry(18 + 1 + 12 + 12 + 10, y + 11, sw, sh)
            y += box_h
        else:
            self._say_rect = None
        if self._sum_on:
            y += 12
            colw = (inner - 2 - 28 - 14) // 2
            dh = max(self.sum_done.heightForWidth(colw), self.sum_not.heightForWidth(colw))
            box_h = 12 + 13 + 4 + dh + 12 + 2
            if apply:
                self._sum_rect = QRectF(18, y, inner, box_h)
                x1 = 18 + 1 + 14
                x2 = x1 + colw + 14
                for (ic, lab, body, x) in ((self.sum_done_i, self.sum_done_l, self.sum_done, x1),
                                           (self.sum_not_i, self.sum_not_l, self.sum_not, x2)):
                    ic.move(x, y + 13)
                    lab.setGeometry(x + 13 + 8, y + 13, colw - 21, 14)
                    body.setGeometry(x, y + 13 + 13 + 4 + 1, colw, dh)
            y += box_h
        else:
            self._sum_rect = None
        y += 12
        visible_rows = [r for r in self.rows if r.isVisibleTo(self)]
        if not visible_rows:
            eh = int(round(13.5 * 1.45))
            if apply:
                self.empty.setGeometry(18, y + 9, inner, eh)
            y += 9 + eh + 9
        for row in visible_rows:
            rh = row.height_for(inner)
            if apply:
                row.setGeometry(18, y, inner, rh)
            y += rh
        if self.steerbox.isVisibleTo(self):
            y += 12
            self.steerbox.setFixedWidth(inner)
            sh = self.steerbox.height_for(inner)
            if apply:
                self.steerbox.setGeometry(18, y, inner, sh)
            y += sh
        y += 14
        x = 18
        for k in (self.k_stop, self.k_hand, self.k_save, self.k_again):
            if k.isVisibleTo(self):
                if apply:
                    k.move(x, y)
                x += k.width() + 8
        right = w - 18
        for k in (self.k_ledger, self.k_close):
            if k.isVisibleTo(self):
                right -= k.width()
                if apply:
                    k.move(right + (6 if k is self.k_ledger else 0), y + (30 - k.height()) // 2)
                right -= 8
        if self.foot_note.isVisibleTo(self):
            fw = int(self.foot_note.natural_width()) + 2
            if apply:
                self.foot_note.setGeometry(right - fw, y + 10, fw, 11)
        y += 30 + 16
        return y

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(QRectF(0, 0, self.width(), 1), L.HAIR)
        if getattr(self, "_say_rect", None) is not None:
            pen = QPen(L.HAIR2, 1.0)
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([3.0, 3.0])
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(self._say_rect.adjusted(0.5, 0.5, -0.5, -0.5), 9.5, 9.5)
        if getattr(self, "_sum_rect", None) is not None:
            p.setPen(QPen(L.HAIR2, 1.0))
            p.drawRoundedRect(self._sum_rect.adjusted(0.5, 0.5, -0.5, -0.5), 11.5, 11.5)
        p.end()


class Panel(QWidget):
    """The island's body: the field colour, a 1 px ``#2A2D31`` border, 22 px corners (20 expanded, 13 mini)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.radius = float(RADIUS)
        self.opacity = 1.0

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        rad = min(self.radius, r.height() / 2, r.width() / 2)
        path.addRoundedRect(r, rad - 0.5, rad - 0.5)
        p.fillPath(path, L.BG)
        p.setPen(QPen(L.BORDER, 1.0))
        p.drawPath(path)
        p.end()


class HaloWindow(QWidget):
    """The halo's own window: 560 x 170, under the island, transparent to the mouse (clicks reach the app beneath),
    never focused. Fades in over 360 ms the first time a state brings it; leaves at once when one has none."""

    def __init__(self) -> None:
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.NoDropShadowWindowHint | Qt.WindowType.WindowDoesNotAcceptFocus
                 | Qt.WindowType.WindowTransparentForInput)
        super().__init__(None, flags)
        self.setObjectName("YukiIslandHalo")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(HALO_W, HALO_H)
        self.halo = HaloWidget(self)
        self.halo.move(0, 0)
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(HALO_IN_MS)
        self._fade.setEasingCurve(L.ease_curve())
        self._fade.valueChanged.connect(lambda v: self.setWindowOpacity(float(v)))


class IslandWindow(QWidget):
    """The island's top-level window (the widest island's width; outside the panel it is fully transparent, which
    Windows treats as click-through). Lays out the panel's parts for a :class:`Spec`, morphs between sizes (300 ms,
    the design's ease), enters (fade + 10 px drop, 300 ms) and leaves (fade + 14 px rise, 260 ms)."""

    def __init__(self, island: "Island") -> None:
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.NoDropShadowWindowHint)
        super().__init__(None, flags)
        self.island = island
        self.setObjectName("YukiIsland")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedWidth(WIN_W)
        self.panel = Panel(self)
        P = self.panel
        self.face = FaceWidget(8, P)
        self.ln1 = L.Para("", font=L.ui_font(15, 500), line_height=1.25, parent=P, max_lines=1)
        self.ln2 = L.Para("", font=L.mono_font(11, 500), color=L.TEXT3, line_height=1.3, parent=P, max_lines=1)
        self.rec = L.RecChip(P)
        self.chip = L.Chip("done", icon="check", parent=P)
        self.k_stop = L.Key("Stop", variant="stop", size="sm", icon="stop", parent=P)
        self.k_chev = L.Key("", variant="ghost", size="sm", icon="chev", parent=P)
        self.k_retry = L.Key("Retry", variant="pri", size="sm", parent=P)
        self.k_settings = L.Key("Settings", size="sm", parent=P)
        self.caption = L.Para("", font=L.ui_font(17), line_height=1.4, parent=P, caret=True)
        self.k_answer = L.Key("Answer", variant="pri", size="sm", parent=P)
        self.k_qstop = L.Key("Stop", variant="ghost", size="sm", parent=P)
        self.run = RunCard(P)
        for w in (self.k_stop, self.k_chev, self.k_retry, self.k_settings, self.k_answer, self.k_qstop):
            w.hide()
        self.k_chev.setToolTip("Open the run card")
        self.k_stop.setToolTip("Stop this request")
        self.halo_win = HaloWindow()
        self.spec: Spec | None = None
        self._halo_on = False
        self._rect = QRect()           # the panel now (window coordinates)
        self._from = QRect()
        self._to = QRect()
        self._morph = QVariantAnimation(self)
        self._morph.setStartValue(0.0)
        self._morph.setEndValue(1.0)
        self._morph.setDuration(MORPH_MS)
        self._morph.setEasingCurve(L.ease_curve())
        self._morph.valueChanged.connect(self._on_morph)
        self._morph.finished.connect(self._morph_done)
        self._move = QVariantAnimation(self)
        self._move.valueChanged.connect(self._on_move)
        self._move.finished.connect(self._move_done)
        self._leaving = False
        self._origin = QPoint(0, 0)    # where the window sits when settled
        self._opacity = 1.0

    # -- geometry ------------------------------------------------------------------------------------------

    def anchor(self) -> QPoint:
        screen = active_screen()
        area = screen.availableGeometry() if screen is not None else QRect(0, 0, 1440, 900)
        return QPoint(area.center().x() + 1 - WIN_W // 2, area.top() + TOP_PX)

    def _right_items(self, spec: Spec) -> list[QWidget]:
        out: list[QWidget] = []
        if spec.rec:
            out.append(self.rec)
        if spec.chip:
            out.append(self.chip)
        keys = {"stop": self.k_stop, "chev": self.k_chev, "retry": self.k_retry, "settings": self.k_settings}
        out += [keys[k] for k in spec.keys if k in keys]
        return out

    def natural_width(self, spec: Spec) -> int:
        if spec.mini:
            return 1 + BAR_SHIFT + 12 + FACE_W * 3 + 12 + 1
        if spec.width:
            return spec.width
        face_w = FACE_W * 8
        text = max(self.ln1.natural_width(), self.ln2.natural_width())
        items = self._right_items(spec)
        chips = sum(w.width() for w in items) + 6 * max(0, len(items) - 1)
        w = 1 + BAR_SHIFT + 12 + face_w + 14 + int(text) + 1 + (14 + chips if items else 0) + 10 + 1
        return max(MIN_W, min(MAX_W, w))

    def body_height(self, spec: Spec, w: int) -> int:
        if spec.body == "caption":
            return max(40, self.caption.heightForWidth(max(10, w - 2 - CAPTION_INDENT - 20)) + 16)
        if spec.body == "choices":
            return 26 + 16
        if spec.body == "run":
            return self.run.height_for(w - 2)
        return 0

    def panel_height(self, spec: Spec, w: int) -> int:
        if spec.mini:
            return MINI_H + 2
        return BAR_H + 2 + self.body_height(spec, w)

    def layout_parts(self, spec: Spec, w: int, h: int) -> None:
        """Place the parts inside a panel of ``w`` x ``h`` (called every morph frame: the bar reflows, as CSS)."""
        P = self.panel
        if spec.mini:
            inner = w - 2 - BAR_SHIFT
            self.face.move(1 + BAR_SHIFT + (inner - self.face.width()) // 2, (h - self.face.height()) // 2)
            return
        fy = 1 + (BAR_H - self.face.height()) // 2
        self.face.move(1 + BAR_SHIFT + 12, fy)
        items = self._right_items(spec)
        x = w - 1 - 10
        for it in reversed(items):
            x -= it.width()
            it.move(x, 1 + (BAR_H - it.height()) // 2)
            x -= 6
        right = x + 6 - (14 if items else 0)
        tx = 1 + BAR_SHIFT + 12 + self.face.width() + 14
        tw = max(10, right - tx)
        l1 = int(round(15 * 1.25))
        l2 = int(round(11 * 1.3))
        block = l1 + 2 + l2 if self.ln2.text() else l1
        ty = 1 + (BAR_H - block) // 2
        self.ln1.setGeometry(tx, ty, tw, l1)
        self.ln2.setGeometry(tx, ty + l1 + 2, tw, l2)
        y = 1 + BAR_H
        if spec.body == "caption":
            cw = max(10, w - 2 - CAPTION_INDENT - 20)
            self.caption.setGeometry(1 + CAPTION_INDENT, y, cw, max(24, self.caption.heightForWidth(cw)))
        elif spec.body == "choices":
            self.k_answer.move(1 + CAPTION_INDENT, y)
            self.k_qstop.move(1 + CAPTION_INDENT + self.k_answer.width() + 8, y)
        elif spec.body == "run":
            self.run.setGeometry(1, y, w - 2, max(0, self.run.height_for(w - 2)))
        del P

    # -- showing a spec --------------------------------------------------------------------------------------

    def show_spec(self, spec: Spec, *, animate: bool = True) -> None:
        old = self.spec
        self.spec = spec
        reduced = L.reduced() or not animate
        if spec.hidden:
            self.leave(animate=not reduced)
            return
        # contents first (the design swaps the DOM and morphs the box from the old size)
        self.ln1._elide_left = spec.line_left
        self.ln1.set_text(spec.line)
        self.ln2.set_text(spec.sub)
        self.setToolTip(spec.title)
        for w in (self.ln1, self.ln2):
            w.setVisible(not spec.mini)
        items = self._right_items(spec)
        for w in (self.rec, self.chip, self.k_stop, self.k_chev, self.k_retry, self.k_settings):
            w.setVisible(w in items)
        if spec.chip:
            icon = {"done": "check", "stopped": "stop", "failed": "x", "handed": "hand"}.get(spec.chip, "check")
            self.chip.icon_name = icon
            self.chip.set_text(spec.chip)
        self.k_chev.icon_name = "chev"
        self.k_chev.angle = 0
        self.caption.setVisible(spec.body == "caption")
        for w in (self.k_answer, self.k_qstop):
            w.setVisible(spec.body == "choices")
        self.run.setVisible(spec.body == "run")
        mini_was = old.mini if old is not None else None
        if mini_was is None or mini_was != spec.mini:
            self.face.set_pitch(3 if spec.mini else 8, grow=(mini_was is True and not spec.mini and not reduced))
        self.face.set_state(spec.face, spec.step)
        self.panel.radius = float(RADIUS_MINI if spec.mini else RADIUS_EXP if spec.exp else RADIUS)
        if self._move.state() != QVariantAnimation.State.Running:
            self.setWindowOpacity(self._opacity * (0.92 if spec.mini else 1.0))
        w = self.natural_width(spec)
        h = self.panel_height(spec, w)
        target = QRect((WIN_W - w) // 2, 0, w, h)
        was_visible = self.isVisible() and not self._leaving
        if was_visible and not reduced and self._rect.isValid() and (
                abs(self._rect.width() - w) > 2 or abs(self._rect.height() - h) > 2):
            self._from, self._to = QRect(self._rect), target
            self.setFixedHeight(max(self._rect.height(), h))
            self._morph.stop()
            self._set_rect(self._from)   # the new contents in the old box at once; the box then morphs
            self._morph.start()
        else:
            self._morph.stop()
            self._set_rect(target)
            self.setFixedHeight(h)
        if not was_visible:
            self.enter(animate=not reduced)
        self._place_halo(spec, reduced)

    def _set_rect(self, r: QRect) -> None:
        self._rect = QRect(r)
        self.panel.setGeometry(r)
        if self.spec is not None:
            self.layout_parts(self.spec, r.width(), r.height())
        if self._halo_on:
            self._move_halo()

    def _on_morph(self, v: Any) -> None:
        k = float(v)
        a, b = self._from, self._to
        w = int(round(a.width() + (b.width() - a.width()) * k))
        h = int(round(a.height() + (b.height() - a.height()) * k))
        if self.spec is not None and not self.spec.mini:
            w = max(MIN_W, w)    # the design's island keeps its min-width while it morphs (from the mini: 360 at once)
        self._set_rect(QRect((WIN_W - w) // 2, 0, w, h))

    def _morph_done(self) -> None:
        self._set_rect(self._to)
        self.setFixedHeight(self._to.height())

    # -- the halo --------------------------------------------------------------------------------------------

    def _place_halo(self, spec: Spec, reduced: bool) -> None:
        hw = self.halo_win
        if spec.halo and not spec.mini:
            first = not self._halo_on
            self._halo_on = True
            hw.halo.set_state(spec.halo, spec.step)
            self._move_halo()
            if first:
                if reduced:
                    hw.setWindowOpacity(1.0)
                    hw.show()
                else:
                    hw.setWindowOpacity(0.0)
                    hw.show()
                    hw._fade.stop()
                    hw._fade.setStartValue(0.0)
                    hw._fade.setEndValue(1.0)
                    hw._fade.start()
        else:
            if self._halo_on:
                hw.halo.model.set_state("idle", L.now())   # blends away the next time it is shown, as the design
                hw._fade.stop()
                hw.hide()
            self._halo_on = False

    def _move_halo(self) -> None:
        pos = self.pos()
        x = pos.x() + WIN_W // 2 - HALO_W // 2
        y = pos.y() + self._rect.height() + HALO_GAP
        if self.halo_win.pos() != QPoint(x, y):
            self.halo_win.move(x, y)

    # -- enter / leave ---------------------------------------------------------------------------------------

    def enter(self, *, animate: bool) -> None:
        self._leaving = False
        self._origin = self.anchor()
        if not getattr(self, "_native_pinned", False) and _is_windows():
            from yuki.ui.focus import set_no_activate

            set_no_activate(int(self.winId()), True)   # never activated, not even by a click
            set_no_activate(int(self.halo_win.winId()), True)
            self._native_pinned = True
        mini = bool(self.spec and self.spec.mini)
        if not animate:
            self._move.stop()
            self.move(self._origin)
            self._opacity = 1.0
            self.setWindowOpacity(0.92 if mini else 1.0)
            self.show()
            return
        self._opacity = 0.0
        self.setWindowOpacity(0.0)
        self.move(self._origin + QPoint(0, -10))
        self.show()
        self._move.stop()
        self._move.setDuration(ENTER_MS)
        self._move.setEasingCurve(L.ease_curve())
        self._move.setStartValue(0.0)
        self._move.setEndValue(1.0)
        self._move_kind = "enter"
        self._move.start()

    def leave(self, *, animate: bool) -> None:
        if not self.isVisible() or self._leaving:
            self.halo_win.hide()
            self._halo_on = False
            return
        self.halo_win.hide()
        if self._halo_on:
            self.halo_win.halo.model.set_state("idle", L.now())
        self._halo_on = False
        if not animate:
            self.hide()
            return
        self._leaving = True
        self._origin = self.pos()
        self._move.stop()
        self._move.setDuration(LEAVE_MS)
        self._move.setEasingCurve(L.ease_curve())
        self._move.setStartValue(0.0)
        self._move.setEndValue(1.0)
        self._move_kind = "leave"
        self._move.start()

    def _on_move(self, v: Any) -> None:
        k = float(v)
        mini = 0.92 if (self.spec and self.spec.mini) else 1.0
        if getattr(self, "_move_kind", "") == "enter":
            self._opacity = k
            self.setWindowOpacity(k * mini)
            self.move(self._origin + QPoint(0, int(round(-10 * (1 - k)))))
        else:
            self._opacity = 1 - k
            self.setWindowOpacity((1 - k) * mini)
            self.move(self._origin + QPoint(0, int(round(-14 * k))))
        if self._halo_on:
            self._move_halo()

    def _move_done(self) -> None:
        if getattr(self, "_move_kind", "") == "leave":
            self._leaving = False
            self.hide()
            self.move(self._origin)
        else:
            self.move(self._origin)
            self._opacity = 1.0
            self.setWindowOpacity(0.92 if (self.spec and self.spec.mini) else 1.0)

    def reposition(self) -> None:
        """The monitor in use changed: move there (no animation)."""
        if not self.isVisible() or self._leaving or self._move.state() == QVariantAnimation.State.Running:
            return
        target = self.anchor()
        if target != self.pos():
            self.move(target)
            self._origin = target
            if self._halo_on:
                self._move_halo()

    def hideEvent(self, e: Any) -> None:  # noqa: D102 - nothing runs while hidden
        super().hideEvent(e)
        self.halo_win.hide()
        self.island._visibility_changed(False)

    def showEvent(self, e: Any) -> None:  # noqa: D102
        super().showEvent(e)
        self.island._visibility_changed(True)

    def mousePressEvent(self, e: Any) -> None:  # noqa: D102 - a click on a running island opens its run card
        if self.spec is not None and self.spec.state in ("working", "thinking") and self.spec.body != "run" \
                and self._rect.contains(e.position().toPoint()):
            self.island.toggle_card()
        super().mousePressEvent(e)


# -- the controller ------------------------------------------------------------------------------------------


class Island(QObject):
    """What the island shows and when (a pure :func:`resolve` over :class:`IslandInputs`), its timers, and the
    adapters that let the tray's existing wiring drive it (:attr:`strip`, :attr:`voice_pill`, :attr:`call_indicator`).

    Signals (the tray carries them out):
        stop_clicked(int): Stop on the request shown.
        handback_clicked(int): "I'll take it from here" (stop, and leave the rest to the user).
        steer_requested(int, str): the steer box.
        answer_clicked(int): Answer on needs-you (the ask bar, for typing the answer).
        retry_voice(): Retry on voice-stopped.
        open_page(str): a window page ("settings", "activity").
        ask_again(str): put a stopped request's words back in the ask bar, unsent.
        state_changed(str): the resolved state (the tray's icon follows it).
        save_lesson(): the run card's "Save the lesson".
    """

    stop_clicked = Signal(int)
    handback_clicked = Signal(int)
    steer_requested = Signal(int, str)
    answer_clicked = Signal(int)
    retry_voice = Signal()
    open_page = Signal(str)
    ask_again = Signal(str)
    state_changed = Signal(str)
    save_lesson = Signal()

    def __init__(self, *, bindings: dict[str, str] | None = None, model: Any = None) -> None:
        super().__init__()
        self.inputs = IslandInputs()
        self.bindings = dict(bindings or {})
        self.model = model
        self.state = ""
        self.expanded = False
        self.lesson_available = False
        self.window = IslandWindow(self)
        self.window.k_stop.clicked.connect(self._on_stop)
        self.window.k_chev.clicked.connect(self.toggle_card)
        self.window.k_retry.clicked.connect(self.retry_voice.emit)
        self.window.k_settings.clicked.connect(lambda: self.open_page.emit("settings"))
        self.window.k_answer.clicked.connect(lambda: self.answer_clicked.emit(int(self.inputs.question_id or 0)))
        self.window.k_qstop.clicked.connect(self._on_question_stop)
        rc = self.window.run
        rc.stop.connect(self._on_stop)
        rc.handback.connect(self._on_handback)
        rc.steer.connect(self._on_steer)
        rc.ledger.connect(lambda: self.open_page.emit("activity"))
        rc.close.connect(self._close_card)
        rc.again.connect(self._on_again)
        rc.save_lesson.connect(self.save_lesson.emit)
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._tick)
        self._final_timer = QTimer(self)
        self._final_timer.setSingleShot(True)
        self._final_timer.timeout.connect(self._final_over)
        self._voice_error_timer = QTimer(self)
        self._voice_error_timer.setSingleShot(True)
        self._voice_error_timer.timeout.connect(self._voice_error_over)
        self._card_timer = QTimer(self)
        self._card_timer.setSingleShot(True)
        self._card_timer.timeout.connect(self._close_card)
        self.watch = ForegroundWatch()
        self.watch.changed.connect(self.on_foreground)
        self.strip = IslandStrip(self)
        self.voice_pill = IslandVoice(self)
        self.call_indicator = IslandCallIndicator(self)
        self._model_unsub: Callable[[], None] | None = None
        if model is not None:
            self.attach_model(model)
        self.screen_state: Callable[[], tuple[int, str]] = _notification_state
        self.enabled = True

    # -- lifecycle -------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Watch the window in front (the monitor in use, full screen) and show the island's first state."""
        self.watch.start()
        self.on_foreground(foreground_hwnd())
        self.refresh()

    def stop(self) -> None:
        self.enabled = False
        self.watch.stop()
        for t in (self._clock, self._final_timer, self._voice_error_timer, self._card_timer):
            t.stop()
        self.window.halo_win.hide()
        self.window.hide()

    def attach_model(self, model: Any) -> None:
        """The chat thread (the run card reads the run's steps from it)."""
        self.model = model
        if self._model_unsub is not None:
            self._model_unsub()
        subscribe = getattr(model, "subscribe", None)
        self._model_unsub = subscribe(self._on_model) if callable(subscribe) else None

    def _on_model(self, what: str, item: Any) -> None:
        run = self.inputs.run or (self.inputs.final.run if self.inputs.final else None)
        if run is None or not self.expanded or item is None:
            return
        if run.id in (getattr(item, "jobs", None) or []):
            self._render(animate=True)

    # -- inputs ----------------------------------------------------------------------------------------------

    def update(self, **changes: Any) -> None:
        self.inputs = replace(self.inputs, **changes)
        self.refresh()

    def refresh(self, *, animate: bool = True) -> None:
        if not self.enabled:
            return
        state = resolve(self.inputs)
        if state not in ("working", "thinking", "done-flash"):
            self.expanded = False
        elif state == "done-flash" and (self.inputs.final is None or self.inputs.final.run is None):
            self.expanded = False
        changed = state != self.state
        self.state = state
        self._render(animate=animate)
        if changed:
            self.state_changed.emit(state)

    def current_spec(self) -> Spec:
        return spec_for(self.state or resolve(self.inputs), self.inputs, voice_key=_words(self.bindings.get("voice")),
                        stop_key=_words(self.bindings.get("cancel")), expanded=self.expanded)

    def _render(self, *, animate: bool = True) -> None:
        spec = self.current_spec()
        if spec.body == "caption":
            text, spans = caption_spans(self.inputs.caption, unsure_threshold())
            self.window.caption.set_text(text, spans)
        if spec.body == "run":
            self._fill_card()
        self.window.show_spec(spec, animate=animate)
        self._timers(spec)

    def _timers(self, spec: Spec) -> None:
        need = self.window.isVisible() and not spec.hidden and (
            spec.rec or self.inputs.run is not None or spec.state == "quiet-in-call")
        if need and not self._clock.isActive():
            self._clock.start()
            self._tick()
        elif not need:
            self._clock.stop()
        if spec.rec:
            self.window.rec.set_time(clock(self._call_seconds()))

    def _call_seconds(self) -> float | None:
        since = call_since(self.inputs.call)
        return None if since is None else time.time() - since

    def _tick(self) -> None:
        spec = self.window.spec
        if spec is None or not self.window.isVisible():
            self._clock.stop()
            return
        if spec.rec:
            self.window.rec.set_time(clock(self._call_seconds()))
        if self.inputs.run is not None and spec.state in ("working", "thinking"):
            fresh = self.current_spec()
            if fresh.sub != self.window.ln2.text():
                self.window.ln2.set_text(fresh.sub)

    def _visibility_changed(self, visible: bool) -> None:
        if not visible:
            self._clock.stop()
        elif self.window.spec is not None:
            self._timers(self.window.spec)

    def call_time(self) -> str:
        """The call's running time now (the tray's hover card and tooltip show the same clock)."""
        return clock(self._call_seconds())

    # the monitor in use, a full screen
    def on_foreground(self, hwnd: int) -> None:
        device = monitor_device(hwnd)
        if device:
            _ACTIVE_DEVICE[0] = device
        try:
            value, _name = self.screen_state()
        except Exception:
            value = 0
        full = value in FULLSCREEN_STATES
        title = window_title(hwnd) if full else ""
        if full != self.inputs.fullscreen or title != self.inputs.fullscreen_title:
            self.update(fullscreen=full, fullscreen_title=title)
        else:
            self.window.reposition()

    # -- the run card ----------------------------------------------------------------------------------------

    def toggle_card(self) -> None:
        if self.state not in ("working", "thinking", "done-flash"):
            return
        self.expanded = not self.expanded
        self._card_timer.stop()
        self._render(animate=True)

    def _close_card(self) -> None:
        self._card_timer.stop()
        if self.expanded:
            self.expanded = False
            if self.inputs.final is not None and self.inputs.final.run is not None and not self._final_timer.isActive():
                self.update(final=None)
                return
            self._render(animate=True)

    def _fill_card(self) -> None:
        final = self.inputs.final
        run = self.inputs.run or (final.run if final else None)
        if run is None:
            return
        mode = "live" if self.inputs.run is not None else (final.outcome if final else "done")
        steps = self._steps(run)
        done = sum(1 for s in steps if s["kind"] == "done")
        count = f"{done}/{run.of}" if run.of else str(done if mode != "live" else max(done, run.steps))
        cost = "so far" if mode == "live" else (f"{final.cost} total" if final and final.cost else "total")
        asked = _clean(run.request)
        title = asked
        touch = _touching(run.frame)
        summary = None
        if mode in ("stopped", "handed"):
            did = [f"{s['verb']} {s['obj']}".strip() for s in steps if s["kind"] == "done"]
            summary = ("; ".join(did) or "Nothing yet", "What was left of the request")
        note = {"handed": "You have the mouse and keys", "stopped": "Nothing else ran", "failed": "It stopped there",
                "done": ""}.get(mode, "")
        self.window.run.set_card(mode=mode, title=title, touch=touch, count=count, cost=cost,
                                 asked=asked if self._needs_say(asked) else "", steps=steps, steers=run.steers,
                                 summary=summary, foot_note=note,
                                 stop_kbd=_words(self.bindings.get("cancel")).replace("+", " ") or "Alt Shift X",
                                 lesson=self.lesson_available)

    def _needs_say(self, asked: str) -> bool:
        """The request in full under the title only when the title had to cut it (no word said twice)."""
        if not asked:
            return False
        fm = QFontMetricsF(L.ui_font(15, 600))
        return fm.horizontalAdvance(asked) > RUN_W - 36 - 80

    def _steps(self, run: RunInfo) -> list[dict[str, Any]]:
        """The run's steps from the chat thread: the mark, the verb (the tool's label) and its object, the time."""
        item = None
        model = self.model
        if model is not None:
            finder = getattr(model, "item_for_job", None)
            if callable(finder):
                item = finder(run.id)
        if item is None:
            return []
        try:
            shown = item.real_steps()
            times, _tail = item.step_times()
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for k, st in enumerate(shown, 1):
            verb, _sep, obj = str(st.label or st.name or "").partition(" — ")
            if st.running and self.inputs.run is None:   # the run is over: a step still open was stopped there
                kind, ms = "cancelled", "stopped"
            elif st.running:
                kind, ms = "running", "now"
            elif st.ok is False:
                kind, ms = "failed", "failed"
            else:
                kind = "done"
                secs = times.get(st.key, st.seconds())
                ms = f"{int(round(secs * 1000))} ms" if secs < 1 else f"{secs:.1f} s"
            note = st.result if kind == "failed" and st.result else ""
            chips = []
            for chip in run.chips.get(k, []):
                icon, text = chip[0], chip[1]
                if len(chip) > 2 and chip[2]:   # a playbook the step follows: its title from memory, once known
                    text = f"using your lesson: {self._lesson_text(str(chip[2]))}"
                chips.append((icon, text))
            out.append({"kind": kind, "verb": verb.strip(), "obj": obj.strip(), "ms": ms, "note": note[:160],
                        "chips": chips})
        return out

    def _on_stop(self) -> None:
        run = self.inputs.run
        if run is not None:
            self.strip.stopping()
            self.stop_clicked.emit(run.id)

    def _on_question_stop(self) -> None:
        run = self.inputs.run
        if run is not None:
            self.stop_clicked.emit(run.id)
        self.update(question="", question_id=None)

    def _on_handback(self) -> None:
        run = self.inputs.run
        if run is not None:
            self.handback_clicked.emit(run.id)

    def _on_steer(self, text: str) -> None:
        run = self.inputs.run
        if run is None:
            return
        steer = Steer(text, at_step=run.steps)
        run.steers.append(steer)
        self.steer_requested.emit(run.id, text)
        self._render(animate=True)

    def steer_result(self, request_id: int, text: str, delivered: bool) -> None:
        """What the runtime said about a steer: not running any more means it was never read."""
        run = self.inputs.run
        if run is None or run.id != request_id or delivered:
            return
        for s in run.steers:
            if s.text == text and s.state.startswith("added"):
                s.state = "it had already finished"
        self._render(animate=False)

    def steer_unread(self, request_id: int, texts: list[str]) -> None:
        """The runtime's ``steer_unread``: these arrived after the request's last step (never read)."""
        run = self.inputs.run or (self.inputs.final.run if self.inputs.final else None)
        if run is None or run.id != request_id:
            return
        for s in run.steers:
            if s.text in texts and s.state.startswith("added"):
                s.state = "arrived after the last step"
        self._render(animate=False)

    def _on_again(self) -> None:
        final = self.inputs.final
        if final is not None and final.run is not None and final.run.request:
            self.ask_again.emit(_clean(final.run.request))
        self._close_card()

    def step_meta(self, request_id: int, name: str, tool_input: dict) -> None:
        """A step's own fields: a ``playbook`` it follows becomes the step's "using your lesson" chip."""
        run = self.inputs.run
        if run is None or run.id != request_id or not isinstance(tool_input, dict):
            return
        ref = tool_input.get("playbook")
        if isinstance(ref, str) and ref.strip():
            run.chips.setdefault(run.steps or 1, []).append(("lesson", "", ref.strip()))

    #: Playbook ids -> their title (filled from memory by the tray, :meth:`set_lessons`).
    _lessons: dict[str, str] = {}

    def set_lessons(self, lessons: dict[str, str]) -> None:
        self._lessons = dict(lessons)
        if self.expanded:
            self._render(animate=False)

    def lesson_refs(self) -> list[str]:
        """Playbook refs the shown run's steps follow whose titles are not known yet (the tray fetches them)."""
        run = self.inputs.run
        refs = [str(c[2]) for cs in (run.chips.values() if run else []) for c in cs if len(c) > 2 and c[2]]
        return [r for r in refs if r not in self._lessons]

    def _lesson_text(self, ref: str) -> str:
        return self._lessons.get(ref) or ref

    def set_frame(self, request_id: int, frame: dict[str, Any]) -> None:
        """The agent's task frame for the run: a person and their usual route become the first step's
        "from memory" chip ("you message Sam on WhatsApp, 78 times")."""
        run = self.inputs.run
        if run is None or run.id != request_id:
            return
        run.frame = frame
        chip = _route_chip(frame)
        if chip:
            run.chips.setdefault(1, []).insert(0, ("memory", chip))
        if self.expanded:
            self._render(animate=False)

    # -- final word ------------------------------------------------------------------------------------------

    def _final_over(self) -> None:
        if self.expanded and self.inputs.final is not None and self.inputs.final.run is not None:
            self._card_timer.start(RUN_CARD_LINGER_MS)   # an open card stays to be read; Close folds it
            return
        self.update(final=None)

    def _voice_error_over(self) -> None:
        self.update(voice_error="")


def _notification_state() -> tuple[int, str]:
    try:
        from yuki.ui.nudges import notification_state

        return notification_state()
    except Exception:
        return 0, "error"


def _words(combo: Any) -> str:
    """``ctrl+alt+y`` -> ``Ctrl+Alt+Y`` (a hotkey spec in the island's sub line)."""
    return "+".join(part.strip().capitalize() for part in str(combo or "").split("+") if part.strip())


def _clean(request: str) -> str:
    try:
        from yuki.ui.chat_model import clean_request

        return " ".join(clean_request(request or "").split())
    except Exception:
        return " ".join(str(request or "").split())


def _route_chip(frame: dict[str, Any] | None) -> str:
    """"from memory: you message <person> on <route>, <n> times" from the frame's first person and their usual route."""
    persons = (frame or {}).get("persons") or []
    for p in persons:
        if not isinstance(p, dict):
            continue
        routes = [r for r in p.get("routes") or [] if isinstance(r, dict)]
        if not routes or not p.get("name"):
            continue
        r = routes[0]
        label = str(r.get("label") or r.get("app") or "").strip()
        mine = r.get("mine")
        if not label:
            continue
        times = f", {int(mine)} times" if isinstance(mine, (int, float)) and mine > 0 else ""
        return f"from memory: you message {p['name']} on {label}{times}"
    return ""


def _touching(frame: dict[str, Any] | None) -> str:
    persons = [p for p in (frame or {}).get("persons") or [] if isinstance(p, dict)]
    for p in persons:
        routes = [r for r in p.get("routes") or [] if isinstance(r, dict)]
        if routes:
            r = routes[0]
            parts = [str(x) for x in (r.get("app"), r.get("label")) if x]
            if parts:
                return "touching: " + " · ".join(dict.fromkeys(parts))
    return ""


# -- adapters: the tray's existing wiring drives the island through these ----------------------------------


class IslandStrip(QObject):
    """The run strip's interface (:class:`yuki.ui.status.StatusStrip`) on the island, so :mod:`yuki.ui.app`'s run
    handling is unchanged: ``isVisible`` means "a run or its end word is on show" (the old strip's meaning: corner
    cards wait for it), and ``faded_out`` fires when that ends."""

    stop_clicked = Signal(int)
    faded_out = Signal()

    def __init__(self, island: Island) -> None:
        super().__init__()
        self.island = island
        self.closing = False
        island.stop_clicked.connect(self.stop_clicked.emit)

    @property
    def run_id(self) -> int | None:
        run = self.island.inputs.run
        return run.id if run is not None else None

    @property
    def running(self) -> bool:
        return self.run_id is not None

    def isVisible(self) -> bool:  # noqa: N802 - the strip's Qt-style name
        i = self.island.inputs
        return i.run is not None or i.final is not None or bool(i.transient)

    def _changed(self, was: bool) -> None:
        if was and not self.isVisible():
            self.faded_out.emit()

    def start_run(self, request_id: int, *, started: float | None = None, steps: int = 0, line: str = "",
                  hint: str = "", request: str = "") -> None:
        isl = self.island
        old = isl.inputs.run
        keep = old if old is not None and old.id == int(request_id) else None
        run = RunInfo(int(request_id), time.monotonic() if started is None else float(started), int(steps), line,
                      hint=hint if not line else "", request=request or (keep.request if keep else ""))
        if keep is not None:
            run.steers, run.chips, run.frame, run.of = keep.steers, keep.chips, keep.frame, keep.of
        isl._final_timer.stop()
        isl.update(run=run, final=None, transient="")

    def run_step(self, request_id: int, text: str, steps: int | None = None) -> None:
        run = self.island.inputs.run
        if run is None or run.id != int(request_id):
            return
        new_steps = run.steps if steps is None else int(steps)
        if new_steps > run.steps:
            for s in run.steers:   # the model read it: this step came after it was added
                if s.state.startswith("added"):
                    s.state = f"read at step {new_steps}"
        self.island.update(run=replace(run, line=text, steps=new_steps, hint=""))

    def stopping(self) -> None:
        run = self.island.inputs.run
        if run is not None and not run.stopping:
            self.island.update(run=replace(run, stopping=True, line="Stopping…"))

    def end_run(self, request_id: int | None = None) -> bool:
        run = self.island.inputs.run
        if run is None or (request_id is not None and run.id != int(request_id)):
            return False
        was = self.isVisible()
        self._ended = run
        self.island.update(run=None)
        self._changed(was)
        return True

    def show_step(self, text: str) -> None:
        run = self.island.inputs.run
        if run is not None:
            self.island.update(run=replace(run, line=text))
        else:
            self.island.update(transient=text)

    def show_final(self, text: str, *, tone: str = "final", outcome: str | None = None) -> None:
        was = self.isVisible()
        run = self.island.inputs.run
        if run is not None:
            self._ended = run
            self.island.inputs = replace(self.island.inputs, run=None)
        ended = getattr(self, "_ended", None)
        kind = outcome or ("failed" if tone == "error" else "done")
        isl = self.island
        isl.update(final=FinalInfo(text, kind, "", ended, ended=time.monotonic()), transient="")
        isl._final_timer.start(DONE_MS)
        self._ended = None
        self._changed(was)

    def set_meta(self, text: str) -> None:
        final = self.island.inputs.final
        if final is not None:
            self.island.update(final=replace(final, meta=text))

    def set_cost(self, cost: str) -> None:
        """The finished run's cost (the run card's "33¢ total")."""
        final = self.island.inputs.final
        if final is not None and cost:
            self.island.update(final=replace(final, cost=cost))

    def dismiss(self) -> None:
        was = self.isVisible()
        isl = self.island
        isl._final_timer.stop()
        if isl.expanded and isl.inputs.final is not None:
            isl.expanded = False
        isl.update(final=None, transient="")
        self._changed(was)

    def ask(self, request_id: int, question: str, *, voice: bool) -> None:
        """Yuki asked something and waits: needs-you (it auto-expands)."""
        self.island.update(question=" ".join(str(question or "").split()), question_id=int(request_id),
                           question_voice=bool(voice))

    def answered(self, request_id: int | None = None) -> None:
        i = self.island.inputs
        if i.question and (request_id is None or i.question_id == request_id):
            self.island.update(question="", question_id=None)

    def set_request(self, request_id: int, request: str) -> None:
        run = self.island.inputs.run
        if run is not None and run.id == int(request_id):
            run.request = request


class IslandVoice(QObject):
    """The voice pill's interface (:class:`yuki.voice.indicator.VoicePill`) on the island: ``show_state(state,
    detail)``. A failure stays at least 8 s; a note and the end-of-conversation line go to a toast."""

    def __init__(self, island: Island) -> None:
        super().__init__()
        self.island = island
        self.devices = ""

    def set_devices(self, text: str) -> None:
        self.devices = text or ""

    def show_state(self, state: str, detail: str = "") -> None:
        isl = self.island
        if state == "error":
            isl._voice_error_timer.start(VOICE_ERROR_MS)
            isl.update(voice="off", voice_detail=detail, voice_error=detail or "Voice stopped")
            return
        if state in ("note", "ended"):
            from yuki.ui.toast import show_toast

            if detail and detail not in ("tray", "hotkey", "wake"):
                show_toast(detail, icon="mic" if state == "note" else "check",
                           ms=3000 if state == "ended" else None)
            if state == "ended":
                isl.update(voice="off", spoken="", caption=[])
            return
        changes: dict[str, Any] = {"voice": state, "voice_detail": detail}
        active = state in ("starting", "listening", "hearing", "thinking", "speaking", "working")
        if active and isl.inputs.voice_error:
            isl._voice_error_timer.stop()
            changes["voice_error"] = ""
        if state in ("listening", "starting") and isl.inputs.voice not in ("listening", "hearing", "starting"):
            changes["caption"] = []
            changes["heard"] = ""
        if state != "speaking":
            changes["spoken"] = ""
        isl.update(**changes)

    def chat_event(self, ev: Any) -> None:
        """The voice side's ``chat_event``: what you said (the caption; words with confidences dim the unsure ones)
        and what Yuki is saying (the speaking line)."""
        if not isinstance(ev, dict):
            return
        isl = self.island
        kind = str(ev.get("type") or "")
        if kind in ("user_utterance", "voice_user"):
            text = " ".join(str(ev.get("text") or "").split())
            words = ev.get("words")
            pairs: list[tuple[str, float | None]] = []
            if isinstance(words, list) and words:
                for w in words:
                    if isinstance(w, dict) and str(w.get("word") or "").strip():
                        conf = w.get("confidence")
                        pairs.append((str(w["word"]).strip(), float(conf) if isinstance(conf, (int, float)) else None))
            elif text:
                pairs = [(w, None) for w in text.split(" ")]
            final = ev.get("final", True) is not False
            isl.update(caption=pairs, heard=text if final else isl.inputs.heard)
        elif kind == "assistant_speech":
            text = " ".join(str(ev.get("text") or "").split())
            if text:
                isl.update(spoken=text)


class IslandCallIndicator(QObject):
    """Stands where :class:`yuki.ui.calls.CallIndicator` stood in :class:`~yuki.ui.calls.CallController`: the call
    is on the island now (``on_call_status``), so the controller's show / hide calls do nothing here."""

    def __init__(self, island: Island) -> None:
        super().__init__()
        self.island = island
        self.closing = False
        self.call: dict | None = None

    def set_call(self, call: dict) -> None:
        self.call = call

    def set_listening(self, listening: bool) -> None:
        del listening

    def isVisible(self) -> bool:  # noqa: N802
        return False

    def fade_in(self, *_a: Any) -> None:
        pass

    def fade_out(self) -> None:
        pass

    def move(self, *_a: Any) -> None:
        pass

    def hide(self) -> None:
        pass

    def anchor(self, *, above: int = 0) -> QPoint:
        del above
        return QPoint(0, 0)

    def elapsed(self, now: float | None = None) -> str:
        since = call_since(self.island.inputs.call)
        return "" if since is None else clock((now if now is not None else time.time()) - since)


def on_call_status(island: Island, status: Any) -> None:
    """``CallController.changed``: the call being recorded (or waiting for headphones) goes on the island."""
    call = (status or {}).get("call") if isinstance(status, dict) else None
    island.update(call=call if call_on(call) else None)


__all__ = ["BAR_H", "FinalInfo", "ForegroundWatch", "Island", "IslandCallIndicator", "IslandInputs", "IslandStrip",
           "IslandVoice", "IslandWindow", "RunInfo", "STATES", "Spec", "Steer", "active_screen", "call_since",
           "caption_spans", "clock", "elapsed", "on_call_status", "pick_screen", "resolve", "spec_for"]
