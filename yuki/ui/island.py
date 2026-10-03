"""The island: Yuki's one always-on surface, top centre, in the Lattice design (phase E6, E7).

It merges what were three windows (the voice pill, the run strip and the call pill) into one small window with the
dot-matrix face (:class:`yuki.ui.dots.FaceWidget`), one status line, a mono sub line, chips on the right, and a bounded
halo under it (:class:`yuki.ui.dots.HaloWidget`, in its own click-through window). Its states, and which one wins when
several apply (:func:`resolve`):

==================  ====================================================================================
game-quiet          a real full screen on the monitor in use (an exclusive or borderless full-screen game, a
                    full-screen video, a presentation) or the session locked: hidden; the tray carries the
                    call timer (one clock, never reset). A borderless app that is only maximised is not one.
needs-you           Yuki asked something and waits (auto-expands: the question and Answer / Stop)
voice-stopped       voice failed: the reason and the fix (Retry, Settings), on screen at least 8 s (auto-expands)
speaking            Yuki talks: "Speaking"; on hover the words being said, "say stop to cut me off"
hearing, listening  your turn: "Hearing you" / "Listening"; on hover a live caption, unsure words dimmed
working, thinking   a request runs: "Working · 2/5"; on hover the step line, the time, Stop and the run card
done-flash          the run ended: the result in a few words for 3 s, a stamped check
call-notice         a call started recording: "Recording your mic · Discord" for 2.5 s, then nothing
quiet-in-call       a call is recorded and nothing else goes on: hidden (the timer is in the tray's hover card)
not-listening       Yuki isn't listening (switched off, quiet, off for this call): hidden (the tray says it)
hidden-armed        listening for its name only: the tiny resting pill (click-through), or nothing (a setting)
==================  ====================================================================================

**Compact by default.** Every state but needs-you and voice-stopped shows a compact pill (the face and one short
word); resting the pointer on it expands it to the full bar with the 300 ms morph, and it folds back 300 ms after
the pointer leaves (sizes and timings: the mock's Island spec, 3 Oct polish round). The halo hangs only under the open
island, and only while Yuki hears, thinks or works, or speaks.

**The run card** (E7) expands from the expanded island (the chevron, or a click on it while a run goes): the live steps from
the chat thread (:class:`yuki.ui.chat_model.ChatModel`), "from memory" and "using your lesson" chips where the agent's
task frame and its ``playbook`` field say so, the steer box ("added, not yet read" -> "read at step n", measured: the
next step taken after it was added), Stop, and "I'll take it from here".

Rules it keeps: never takes focus (``WS_EX_NOACTIVATE``; only a click into the steer box lets it take the keyboard,
and Enter gives it back), sits over full-screen-free desktops on the monitor in use (the foreground window's), stays
above other always-on-top windows (``HWND_TOPMOST`` re-asserted on every foreground change, an event), hides in real
full screens, paints from the shared tokens, at most 30 frames a second on the one shared loop, no timer while hidden,
nothing repaints when idle, and "reduce motion" draws still frames. Its fades are painted (an opacity effect on the
panel, an alpha on the halo), never the window's own opacity, so a shown island is always drawn at full strength (3
Oct: a bar left at window opacity 0 was invisible while its halo showed). What it does is logged without content
(:meth:`Island.log`). The controller (:class:`Island`) decides only what is on screen; every action goes out as a
signal for :mod:`yuki.ui.app` to carry out.
"""

from __future__ import annotations

import ctypes
import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from PySide6.QtCore import QObject, QPoint, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLineEdit, QSizePolicy, QWidget

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
#: How long the run's end word stays (the face's smile settles in 1.2 s).
DONE_MS = 3000
#: The pointer rests this long on the compact pill before it expands (a pass across the top of the screen doesn't).
HOVER_IN_MS = 150
#: The expanded island folds back to the pill this long after the pointer leaves.
HOVER_OUT_MS = 300
#: The compact pill (the mock's Island spec): 40 px tall plus its border, up to 360 px wide, radius 20, padding
#: 0 16 0 10, a 10 px gap, the face at 4 px cells (68 x 28) and one word in 13.5 px / 500.
COMPACT_H = 40
COMPACT_PAD = 10
COMPACT_PAD_R = 16
COMPACT_GAP = 10
COMPACT_MAX_W = 360
COMPACT_PITCH = 4
RADIUS_PILL = 20
#: The resting pill (Settings: the tiny pill when idle): the design's ``.island.mini.resting``.
RESTING_H = 26
RESTING_PAD = 9
#: Opacity of the mini and resting pills (the design's ``.island.mini`` .92 and ``.mini.resting`` .78).
MINI_OPACITY = 0.92
RESTING_OPACITY = 0.78
#: A call that starts recording says so this long, then nothing stays on screen (3 Oct, item 20).
CALL_NOTICE_MS = 2500
#: After an animated enter, the island is checked once this long later: anything not at full strength is settled.
SETTLE_CHECK_MS = ENTER_MS + 250
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
                           "thinking", "done-flash", "call-notice", "quiet-in-call", "not-listening", "hidden-armed")
#: States with the halo figure of the same name: only while Yuki hears, thinks or works, or speaks (3 Oct, item 20).
HALO_STATES = frozenset({"hearing", "thinking", "working", "speaking"})
#: States that show the full bar without a hover: Yuki needs an answer, or voice broke.
AUTO_EXPAND = frozenset({"needs-you", "voice-stopped"})
#: The words the island and the tray use for "Yuki isn't listening" (v3.16, the voice session's states).
NOT_LISTENING_WORDS = {"off": "Yuki isn't listening: switched off", "quiet": "Yuki isn't listening: quiet for a while",
                       "call_off": "Yuki isn't listening: off for this call"}
#: Windows' notification states (``SHQueryUserNotificationState``) that alone mean a real full screen or nobody there:
#: QUNS_NOT_PRESENT (locked, screen saver), QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE. QUNS_BUSY (2) also
#: fires for a borderless window that only fills the monitor, so it counts only when the window in front measures as
#: full screen (:func:`fullscreen_kind`).
FULLSCREEN_STATES = frozenset({1, 3, 4})
QUNS_BUSY = 2


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
    fullscreen_kind: str = ""                # "locked" | "d3d" | "presentation" | "window" (why it hides; logged)
    notice: str = ""                         # the call's start notice ("Recording your mic · Discord"), 2.5 s
    call_asking: str = ""                    # the app of a call whose consent card waits ("" when none)
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
        return "call-notice" if i.notice else "quiet-in-call"
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
    compact: bool = False       # the compact pill: the face and one short word (``word``)
    word: str = ""
    resting: bool = False       # the tiny resting pill (click-through, dimmer)
    why_hidden: str = ""        # hidden: why (logged; never shown)


def compact_word(state: str, i: IslandInputs) -> str:
    """The compact pill's one short word for ``state`` ("Listening", "Working · 2/5", the done result)."""
    if state == "listening":
        return "Listening"
    if state == "hearing":
        return "Hearing you"
    if state == "speaking":
        return "Speaking"
    if state == "thinking":
        return "Stopping…" if i.run is not None and i.run.stopping else "Thinking"
    if state == "working":
        run = i.run
        if run is not None and run.stopping:
            return "Stopping…"
        n = run.steps if run is not None else 0
        of = run.of if run is not None else 0
        if n and of:
            return f"Working · {min(n, of)}/{of}"
        return f"Working · step {n}" if n else "Working"
    if state == "done-flash":
        f = i.final
        return " ".join(str(f.text if f is not None else "Done.").split()) or "Done."
    if state == "call-notice":
        return i.notice
    return ""


def spec_for(state: str, i: IslandInputs, *, voice_key: str = "", stop_key: str = "", expanded: bool = False,
             now: float | None = None, compact: bool = False, resting: bool = True) -> Spec:
    """The island's look for ``state`` from what it knows (pure: the tests read it).

    ``compact``: the pointer isn't on the island (and no run card is open), so a state that has a compact pill shows
    it; needs-you and voice-stopped always show the full bar. ``resting``: the setting for idle (the tiny pill, or
    nothing)."""
    now = time.monotonic() if now is None else now
    rec = bool(i.call) and (i.call or {}).get("status") == "recording"
    run = i.run
    app = str((i.call or {}).get("app") or "the call")
    if state == "game-quiet":
        return Spec(state, face="game-quiet", hidden=True, why_hidden=f"full screen: {i.fullscreen_kind or 'yes'}")
    if state == "hidden-armed":
        if not resting:
            return Spec(state, face="idle", hidden=True, why_hidden="idle, resting pill off")
        return Spec(state, face="idle", mini=True, resting=True, title="Yuki is listening for its name")
    if state == "not-listening":
        return Spec(state, face="not-listening", hidden=True, why_hidden="not listening",
                    title=NOT_LISTENING_WORDS.get(i.listening, "Yuki isn't listening"))
    if state == "quiet-in-call":
        return Spec(state, face="quiet-in-call", hidden=True, why_hidden="in a call", rec=rec)
    if compact and state not in AUTO_EXPAND and not expanded and state in ("listening", "hearing", "speaking",
                                                                          "thinking", "working", "done-flash",
                                                                          "call-notice"):
        face = {"listening": "woke" if i.voice == "starting" else "listening", "call-notice": "quiet-in-call"}.get(
            state, state)
        if state == "done-flash":
            face = "done-flash" if (i.final is None or i.final.outcome == "done") else "handed"
        n, of = step_of(run)   # the halo belongs to the open island: the pill never carries it
        return Spec(state, face=face, compact=True, word=compact_word(state, i), rec=rec, step=(n, of),
                    title=compact_word(state, i))
    if state == "call-notice":
        return Spec(state, face="quiet-in-call", compact=True, word=i.notice, rec=rec, title=i.notice)
    if state == "listening":
        starting = i.voice == "starting"
        sub = (f"{voice_key} to stop · dim words are unsure" if voice_key else "dim words are unsure")
        return Spec(state, face="woke" if starting else "listening", exp=True, width=600,
                    line="Yes?" if starting else "Listening", sub="starting voice…" if starting else sub, rec=rec,
                    body="caption")
    if state == "hearing":
        return Spec(state, face="hearing", exp=True, width=660, line="Hearing you", sub="Low-confidence words are dimmed",
                    rec=rec, body="caption", halo="hearing")
    if state == "speaking":
        return Spec(state, face="speaking", width=660, line=i.spoken or "Speaking",
                    sub="Speaking · say “stop” to cut me off", rec=rec, halo="speaking", line_left=bool(i.spoken))
    if state == "needs-you":
        sub = "Say it, or press Answer to type it" if i.question_voice else "Answer in the ask bar"
        return Spec(state, face="needs-you", exp=True, width=600, line=i.question, sub=sub, rec=rec,
                    body="choices", title=i.question)
    if state == "voice-stopped":
        return Spec(state, face="voice-stopped", width=720, line="Voice stopped",
                    sub=i.voice_error, keys=("retry", "settings"), title=i.voice_error)
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
        keys = ("chev",) if f.run is not None else ()   # the finished run's card opens from here
        if f.outcome == "done":   # the design's done flash carries its chip only (no rec chip)
            return Spec(state, face="done-flash", line=f.text, sub=f.meta or _final_sub(f), chip="done", keys=keys)
        return Spec(state, face="handed", line=f.text, sub=f.meta or _final_sub(f), chip=f.outcome, keys=keys)
    del app
    return Spec("hidden-armed", face="idle", hidden=True, why_hidden="unknown state")


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


@dataclass(frozen=True)
class WindowFacts:
    """What the window in front measures as (no title, no content): whether it covers its whole monitor, whether it
    is maximised (``IsZoomed``), whether it has a title bar (``WS_CAPTION``), and its monitor's device name."""

    covers: bool = False
    zoomed: bool = False
    caption: bool = False
    device: str = ""


def window_facts(hwnd: int) -> WindowFacts:
    """Measure ``hwnd`` against its monitor (``GetWindowRect`` vs ``rcMonitor``); empty facts when unknown."""
    if not hwnd or not _is_windows():
        return WindowFacts()
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFOEXW)]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.IsZoomed.argtypes = [wintypes.HWND]
        get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        get_long.restype = ctypes.c_ssize_t
        h = ctypes.c_void_p(hwnd)
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(user32.MonitorFromWindow(h, 2), ctypes.byref(info)):
            return WindowFacts()
        r = wintypes.RECT()
        if not user32.GetWindowRect(h, ctypes.byref(r)):
            return WindowFacts(device=str(info.szDevice))
        m = info.rcMonitor
        covers = r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom
        style = int(get_long(h, -16))                       # GWL_STYLE
        caption = (style & 0x00C00000) == 0x00C00000          # WS_CAPTION
        return WindowFacts(covers=bool(covers), zoomed=bool(user32.IsZoomed(h)), caption=caption,
                           device=str(info.szDevice))
    except Exception:
        return WindowFacts()


def fullscreen_kind(quns: int, facts: WindowFacts) -> str:
    """Whether a real full screen covers the monitor in use, and which: ``"locked"`` (the session is locked or a
    screen saver runs), ``"d3d"`` (an exclusive full-screen game), ``"presentation"``, ``"window"`` (a borderless
    full-screen game, a full-screen video or browser: Windows says busy AND the window in front covers its whole
    monitor without a title bar and is not merely maximised), or ``""``.

    Measured, not guessed: Windows' QUNS_BUSY alone also fires for a borderless app maximised on its monitor (Arc),
    which is not a full screen (3 Oct: the island vanished over a maximised Arc)."""
    if quns == 1:
        return "locked"
    if quns == 3:
        return "d3d"
    if quns == 4:
        return "presentation"
    if quns == QUNS_BUSY and facts.covers and not facts.zoomed and not facts.caption:
        return "window"
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
    """The island's body: the field colour, a 1 px ``#2A2D31`` border, 22 px corners (20 expanded, 13 mini). The
    resting pill is the design's ``.island.mini.resting``: ``rgba(11,11,12,.86)`` with a ``#222428`` border."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.radius = float(RADIUS)
        self.resting = False

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        rad = min(self.radius, r.height() / 2, r.width() / 2)
        path.addRoundedRect(r, rad - 0.5, rad - 0.5)
        p.fillPath(path, QColor(11, 11, 12, 219) if self.resting else L.BG)
        p.setPen(QPen(QColor("#222428") if self.resting else L.BORDER, 1.0))
        p.drawPath(path)
        p.end()


class RecDot(QWidget):
    """The island's still mint rec dot (the design's ``.recmark .recdot.still``, 7 px): the call is recorded. The
    call's timer lives only in the tray's hover card (3 Oct, item 20)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(7, 7)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setToolTip("Recording your mic")

    def set_time(self, _text: str) -> None:   # the old rec chip's interface: no timer on the island any more
        return

    def paintEvent(self, _e: Any) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(L.live())
        p.drawEllipse(QRectF(0, 0, 7, 7))
        p.end()


class HaloWindow(QWidget):
    """The halo's own window: 560 x 170, under the island, transparent to the mouse (clicks reach the app beneath),
    never focused. Fades in over 360 ms the first time a state brings it (a painted alpha: the window's own opacity
    stays 1); leaves at once when one has none."""

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
        self._fade.valueChanged.connect(self._on_fade)
        self._fade.finished.connect(lambda: self._on_fade(1.0))

    def _on_fade(self, v: Any) -> None:
        self.halo.alpha = max(0.0, min(1.0, float(v)))
        self.halo.update()

    def fade_in(self, animate: bool) -> None:
        """Show at full strength at once (``animate`` False) or fade in over 360 ms."""
        self._fade.stop()
        self.setWindowOpacity(1.0)
        if animate:
            self._on_fade(0.0)
            self.show()
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.start()
        else:
            self._on_fade(1.0)
            self.show()

    def strength(self) -> float:
        """How strongly the halo is drawn now (window opacity times the painted alpha)."""
        return float(self.windowOpacity()) * float(getattr(self.halo, "alpha", 1.0))


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
        #: The compact pill's one word (13.5 px, 500).
        self.word = L.Para("", font=L.ui_font(13.5, 500), line_height=1.25, parent=P, max_lines=1)
        self.rec = RecDot(P)
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
        #: The enter / leave fade, 0..1, painted through an opacity effect on the panel (never the window's opacity).
        self._fade = 1.0
        self._move_kind = ""
        self._click_through = False
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.timeout.connect(self._settle_check)

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
        if spec.resting:
            return 1 + RESTING_PAD + FACE_W * 3 + RESTING_PAD + 1
        if spec.mini:
            return 1 + BAR_SHIFT + 12 + FACE_W * 3 + 12 + 1
        if spec.compact:
            if self.word.text() != spec.word:
                self.word.set_text(spec.word)
            text = int(math.ceil(self.word.natural_width())) + 4
            w = 1 + COMPACT_PAD + FACE_W * COMPACT_PITCH + COMPACT_GAP + text + (COMPACT_GAP + 7 if spec.rec else 0) \
                + COMPACT_PAD_R + 1
            return max(1 + COMPACT_PAD + FACE_W * COMPACT_PITCH + COMPACT_PAD_R + 1, min(COMPACT_MAX_W, w))
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
        if spec.resting:
            return RESTING_H + 2
        if spec.compact:
            return COMPACT_H + 2
        if spec.mini:
            return MINI_H + 2
        return BAR_H + 2 + self.body_height(spec, w)

    def layout_parts(self, spec: Spec, w: int, h: int) -> None:
        """Place the parts inside a panel of ``w`` x ``h`` (called every morph frame: the bar reflows, as CSS)."""
        P = self.panel
        if spec.resting:
            self.face.move((w - self.face.width()) // 2, (h - self.face.height()) // 2)
            return
        if spec.mini:
            inner = w - 2 - BAR_SHIFT
            self.face.move(1 + BAR_SHIFT + (inner - self.face.width()) // 2, (h - self.face.height()) // 2)
            return
        if spec.compact:
            self.face.move(1 + COMPACT_PAD, (h - self.face.height()) // 2)
            x = 1 + COMPACT_PAD + self.face.width() + COMPACT_GAP
            right = w - 1 - COMPACT_PAD_R
            if spec.rec:
                self.rec.move(right - self.rec.width(), (h - self.rec.height()) // 2)
                right -= self.rec.width() + COMPACT_GAP
            lh = int(round(13.5 * 1.25))
            self.word.setGeometry(x, (h - lh) // 2, max(10, right - x), lh)
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
        self.word.set_text(spec.word)
        self.setToolTip(spec.title)
        bar = not (spec.mini or spec.compact)
        for w in (self.ln1, self.ln2):
            w.setVisible(bar)
        self.word.setVisible(spec.compact)
        items = self._right_items(spec) if bar else ([self.rec] if spec.compact and spec.rec else [])
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
        pitch = 3 if spec.mini else COMPACT_PITCH if spec.compact else 8
        small_was = (old.mini or old.compact) if old is not None and not old.hidden else None
        if self.face.pitch != pitch:   # the face scales between the pill (4 px cells) and the open island (8 px)
            self.face.set_pitch(pitch, grow=(small_was is True and pitch == 8 and not reduced))
        self.face.set_state(spec.face, spec.step)
        self.panel.radius = float(RADIUS_MINI if spec.mini else RADIUS_PILL if spec.compact
                                  else RADIUS_EXP if spec.exp else RADIUS)
        self.panel.resting = spec.resting
        self._set_click_through(spec.resting)
        if self._move.state() != QVariantAnimation.State.Running:
            self._apply_opacity()
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
        if self.spec is not None and not (self.spec.mini or self.spec.compact):
            w = max(MIN_W, w)    # the design's island keeps its min-width while it morphs (from the mini: 360 at once)
        self._set_rect(QRect((WIN_W - w) // 2, 0, w, h))

    def _morph_done(self) -> None:
        self._set_rect(self._to)
        self.setFixedHeight(self._to.height())

    # -- the halo --------------------------------------------------------------------------------------------

    def _place_halo(self, spec: Spec, reduced: bool) -> None:
        hw = self.halo_win
        if spec.halo and not spec.mini:
            first = not self._halo_on or not hw.isVisible()
            self._halo_on = True
            hw.halo.set_state(spec.halo, spec.step)
            self._move_halo()
            if first:
                hw.fade_in(animate=not reduced)
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

    # The fades are painted: an opacity effect on the panel while one runs (and on the mini and resting pills, which
    # the design draws at .92 / .78), removed when the panel is at full strength. The window's own opacity is never
    # anything but 1: on 3 Oct a bar was painted (PrintWindow showed it) yet invisible while its halo showed, which is
    # what a layered window left at window opacity 0 looks like. Painting the fade keeps what is drawn and what is
    # shown one and the same, and an enter is checked once after it should have ended (:meth:`_settle_check`).

    def design_opacity(self, spec: Spec | None = None) -> float:
        """The opacity the design gives a settled panel: .78 resting, .92 mini, else 1."""
        spec = spec if spec is not None else self.spec
        if spec is None:
            return 1.0
        return RESTING_OPACITY if spec.resting else MINI_OPACITY if spec.mini else 1.0

    def panel_opacity(self) -> float:
        """The panel's opacity now (1 when no effect is set)."""
        eff = self.panel.graphicsEffect()
        return float(eff.opacity()) if isinstance(eff, QGraphicsOpacityEffect) else 1.0

    def _apply_opacity(self) -> None:
        v = max(0.0, min(1.0, self._fade)) * self.design_opacity()
        eff = self.panel.graphicsEffect()
        if v >= 0.999:
            if eff is not None:
                self.panel.setGraphicsEffect(None)
        else:
            if not isinstance(eff, QGraphicsOpacityEffect):
                eff = QGraphicsOpacityEffect(self.panel)
                self.panel.setGraphicsEffect(eff)
            if abs(eff.opacity() - v) > 0.001:
                eff.setOpacity(v)
        if self.windowOpacity() != 1.0:
            self.setWindowOpacity(1.0)

    def _set_click_through(self, on: bool) -> None:
        """The resting pill lets every click and hover through to the app beneath (the design's resting island)."""
        if on == self._click_through:
            return
        self._click_through = on
        if _is_windows():
            try:
                from yuki.ui.focus import set_click_through

                set_click_through(int(self.winId()), on)
            except Exception:
                pass

    def _pin_native(self) -> None:
        if not getattr(self, "_native_pinned", False) and _is_windows():
            from yuki.ui.focus import set_no_activate

            set_no_activate(int(self.winId()), True)   # never activated, not even by a click
            set_no_activate(int(self.halo_win.winId()), True)
            self._native_pinned = True

    def enter(self, *, animate: bool) -> None:
        self._leaving = False
        self._origin = self.anchor()
        self._pin_native()
        self._move.stop()
        self._settle.stop()
        if not animate:
            self.move(self._origin)
            self._fade = 1.0
            self._apply_opacity()
            self.show()
            self.island._entered()
            return
        self._fade = 0.0
        self._apply_opacity()
        self.move(self._origin + QPoint(0, -10))
        self.show()
        self._move.setDuration(ENTER_MS)
        self._move.setEasingCurve(L.ease_curve())
        self._move.setStartValue(0.0)
        self._move.setEndValue(1.0)
        self._move_kind = "enter"
        self._move.start()
        self._settle.start(SETTLE_CHECK_MS)

    def leave(self, *, animate: bool) -> None:
        self._settle.stop()
        if not self.isVisible() or self._leaving:
            self.halo_win.hide()
            self._halo_on = False
            return
        self.halo_win.hide()
        if self._halo_on:
            self.halo_win.halo.model.set_state("idle", L.now())
        self._halo_on = False
        if not animate:
            self._move.stop()
            self.hide()
            self.island._left()
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
        if self._move_kind == "enter":
            self._fade = k
            self.move(self._origin + QPoint(0, int(round(-10 * (1 - k)))))
        else:
            self._fade = 1 - k
            self.move(self._origin + QPoint(0, int(round(-14 * k))))
        self._apply_opacity()
        if self._halo_on:
            self._move_halo()

    def _move_done(self) -> None:
        if self._move_kind == "leave":
            self._leaving = False
            self.hide()
            self.move(self._origin)
            self._fade = 1.0          # the next enter starts its own fade; nothing waits at 0
            self._apply_opacity()
            self.island._left()
        else:
            self._settle.stop()
            self.move(self._origin)
            self._fade = 1.0
            self._apply_opacity()
            self.island._entered()

    def _settle_check(self) -> None:
        """Once, after an animated enter should have ended: a shown island not at full strength is settled now."""
        if not self.isVisible() or self._leaving:
            return
        running = self._move.state() == QVariantAnimation.State.Running
        if self._fade >= 0.999 and not running and self.windowOpacity() == 1.0:
            return
        before = (round(self._fade, 3), running, round(float(self.windowOpacity()), 3))
        self._move.stop()
        self.move(self._origin)
        self._fade = 1.0
        self._apply_opacity()
        self.island._log("island_settled", fade_was=before[0], anim_running=before[1], window_opacity_was=before[2])
        self.island._entered()

    def shown_strength(self) -> float:
        """How strongly the island is drawn now: the window's opacity times the fade (1 when settled and shown)."""
        return float(self.windowOpacity()) * max(0.0, min(1.0, self._fade))

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

    def enterEvent(self, e: Any) -> None:  # noqa: D102 - the pointer rests on the island: it expands
        super().enterEvent(e)
        self.island.pointer(True)

    def leaveEvent(self, e: Any) -> None:  # noqa: D102 - it folds back 300 ms after the pointer leaves
        super().leaveEvent(e)
        self.island.pointer(False)

    def mousePressEvent(self, e: Any) -> None:  # noqa: D102 - a click expands the pill, or opens the run card
        inside = self._rect.contains(e.position().toPoint())
        if self.spec is not None and inside and self.spec.compact:
            self.island.pointer(True, now=True)
        elif self.spec is not None and self.spec.state in ("working", "thinking") and self.spec.body != "run" \
                and inside:
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
        #: The pointer rests on the island (it shows the full bar instead of the compact pill).
        self.hovered = False
        #: Settings → Island: the tiny resting pill when idle (True, the default) or nothing.
        self.resting = True
        #: Content-free logging: ``log(event, **fields)`` (the UI log's ``event``; set by the app).
        self.log: Callable[..., None] | None = None
        #: The last run that finished: ``(text, outcome, epoch seconds)`` (the tray's hover card says it).
        self.last_final: tuple[str, str, float] | None = None
        self._noticed_call: Any = None
        self._shown = ""
        self._fg_facts = WindowFacts()
        self._quns = 0
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
        self._hover_in = QTimer(self)
        self._hover_in.setSingleShot(True)
        self._hover_in.timeout.connect(lambda: self._set_hovered(True))
        self._hover_out = QTimer(self)
        self._hover_out.setSingleShot(True)
        self._hover_out.timeout.connect(lambda: self._set_hovered(False))
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(lambda: self.update(notice=""))
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
        for t in (self._clock, self._final_timer, self._voice_error_timer, self._card_timer, self._hover_in,
                  self._hover_out, self._notice_timer, self.window._settle):
            t.stop()
        self.window.halo_win.hide()
        self.window.hide()

    def set_resting(self, on: bool) -> None:
        """Settings → Island: the tiny resting pill when idle, or nothing on screen (only the tray icon)."""
        if bool(on) != self.resting:
            self.resting = bool(on)
            self._log("island_setting", resting=self.resting)
            self.refresh()

    # -- logging (content-free: states, numbers, flags; never a line of text) -----------------------------------

    def _log(self, event: str, **fields: Any) -> None:
        if self.log is None:
            return
        try:
            self.log(event, **fields)
        except Exception:
            pass

    def snapshot(self) -> dict[str, Any]:
        """What the island is doing now, without content: the state, how it shows, whether it is visible and at what
        strength, where, on which monitor, the full-screen verdict, topmost, and why it is hidden."""
        w = self.window
        spec = w.spec
        g = w.geometry()
        hwnd = int(w.winId()) if _is_windows() and w.testAttribute(Qt.WidgetAttribute.WA_WState_Created) else 0
        topmost: bool | None = None
        if hwnd:
            try:
                from yuki.ui.focus import is_topmost

                topmost = is_topmost(hwnd)
            except Exception:
                topmost = None
        return {
            "state": self.state, "shown": self._shown_kind(spec), "visible": bool(w.isVisible()),
            "leaving": bool(w._leaving), "strength": round(w.shown_strength(), 3),
            "window_opacity": round(float(w.windowOpacity()), 3), "panel_opacity": round(w.panel_opacity(), 3),
            "rect": [g.x(), g.y(), w._rect.width(), w._rect.height()], "monitor": _ACTIVE_DEVICE[0],
            "fullscreen": self.inputs.fullscreen_kind or "", "quns": self._quns, "topmost": topmost,
            "halo": bool(w.halo_win.isVisible()), "hovered": self.hovered, "run_card": self.expanded,
            "why_hidden": spec.why_hidden if spec is not None and spec.hidden else "",
        }

    @staticmethod
    def _shown_kind(spec: Spec | None) -> str:
        if spec is None or spec.hidden:
            return "hidden"
        if spec.resting:
            return "resting"
        if spec.compact:
            return "compact"
        if spec.body == "run":
            return "run card"
        return "full"

    def _entered(self) -> None:
        """The island finished coming on screen: log it, with what covers its middle if anything does."""
        snap = self.snapshot()
        snap["covered_by"] = self._covered_by()
        self._log("island_shown", **snap)

    def _left(self) -> None:
        self._log("island_hidden", **self.snapshot())

    def _covered_by(self) -> str:
        """The window class at the middle of the island's panel when another window sits over it (``""`` when the
        island itself is on top there, or unknown). A class name is not content (``Chrome_WidgetWin_1``)."""
        if not _is_windows() or not self.window.isVisible():
            return ""
        try:
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32")
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            user32.WindowFromPoint.argtypes = [wintypes.POINT]
            user32.WindowFromPoint.restype = ctypes.c_void_p
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.GetAncestor.restype = ctypes.c_void_p
            hwnd = int(self.window.winId())
            r = wintypes.RECT()
            if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r)):
                return ""
            dpr = float(self.window.devicePixelRatioF() or 1.0)
            c = self.window._rect.center()
            pt = wintypes.POINT(int(r.left + c.x() * dpr), int(r.top + c.y() * dpr))
            hit = user32.WindowFromPoint(pt)
            root = int(user32.GetAncestor(hit, 2) or 0) if hit else 0     # GA_ROOT
            if not root or root == hwnd:
                return ""
            buf = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(ctypes.c_void_p(root), buf, 128)
            return buf.value or "unknown"
        except Exception:
            return ""

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
        before = self.inputs.call
        self.inputs = replace(self.inputs, **changes)
        if "call" in changes:
            self._call_changed(before, self.inputs.call)
        self.refresh()

    def _call_changed(self, before: dict | None, now: dict | None) -> None:
        """A call that starts recording says so for 2.5 s ("Recording your mic · Discord"), once per call session;
        after that nothing about the call stays on screen (the tray's icon and hover card carry it)."""
        recording = bool(now) and (now or {}).get("status") == "recording"
        if not recording:
            if self.inputs.notice:
                self._notice_timer.stop()
                self.inputs = replace(self.inputs, notice="")
            return
        key = (now or {}).get("session_started_at") or (now or {}).get("id") or (now or {}).get("detected_at")
        if key == self._noticed_call:
            return
        self._noticed_call = key
        app = str((now or {}).get("app") or "").strip()
        self.inputs = replace(self.inputs, notice=f"Recording your mic · {app}" if app else "Recording your mic")
        self._notice_timer.start(CALL_NOTICE_MS)
        del before

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
                        stop_key=_words(self.bindings.get("cancel")), expanded=self.expanded,
                        compact=not self.hovered, resting=self.resting)

    def _render(self, *, animate: bool = True) -> None:
        spec = self.current_spec()
        if spec.body == "caption":
            text, spans = caption_spans(self.inputs.caption, unsure_threshold())
            self.window.caption.set_text(text, spans)
        if spec.body == "run":
            self._fill_card()
        self.window.show_spec(spec, animate=animate)
        self._timers(spec)
        shown = f"{spec.state}/{self._shown_kind(spec)}"
        if shown != self._shown:
            self._shown = shown
            self._log("island", **self.snapshot())

    def _timers(self, spec: Spec) -> None:
        # one clock, only for the full bar's run time ("step 2 · 0:46"); the pill's words don't change by the second,
        # and the call's timer lives in the tray's hover card (its own clock, only while the card is open)
        need = self.window.isVisible() and not spec.hidden and not spec.compact and self.inputs.run is not None
        if need and not self._clock.isActive():
            self._clock.start()
            self._tick()
        elif not need:
            self._clock.stop()

    def _call_seconds(self) -> float | None:
        since = call_since(self.inputs.call)
        return None if since is None else time.time() - since

    def _tick(self) -> None:
        spec = self.window.spec
        if spec is None or not self.window.isVisible():
            self._clock.stop()
            return
        if self.inputs.run is not None and spec.state in ("working", "thinking") and not spec.compact:
            fresh = self.current_spec()
            if fresh.sub != self.window.ln2.text():
                self.window.ln2.set_text(fresh.sub)

    def _visibility_changed(self, visible: bool) -> None:
        if not visible:
            self._clock.stop()
            self._hover_in.stop()
            self._hover_out.stop()
            if self.hovered and not self.window._leaving:
                self.hovered = False
        elif self.window.spec is not None:
            self._timers(self.window.spec)

    def call_time(self) -> str:
        """The call's running time now (the tray's hover card and tooltip show the same clock)."""
        return clock(self._call_seconds())

    # -- hover: the compact pill expands while the pointer rests on it -------------------------------------------

    def pointer(self, inside: bool, *, now: bool = False) -> None:
        """The pointer came onto the island (``inside``) or left it. Resting on it 150 ms expands the pill to the full
        bar; leaving folds it back 300 ms later. ``now``: a click expands at once."""
        if inside:
            self._hover_out.stop()
            if now:
                self._hover_in.stop()
                self._set_hovered(True)
            elif not self.hovered and not self._hover_in.isActive():
                self._hover_in.start(HOVER_IN_MS)
        else:
            self._hover_in.stop()
            if self.hovered and not self._hover_out.isActive():
                self._hover_out.start(HOVER_OUT_MS)

    def _set_hovered(self, on: bool) -> None:
        if on == self.hovered:
            return
        spec = self.window.spec
        if on and (spec is None or spec.hidden or spec.resting):
            return
        self.hovered = on
        self._render(animate=True)

    # the monitor in use, a full screen, always on top
    def on_foreground(self, hwnd: int) -> None:
        facts = window_facts(hwnd)
        device = facts.device or monitor_device(hwnd)
        if device:
            _ACTIVE_DEVICE[0] = device
        try:
            value, _name = self.screen_state()
        except Exception:
            value = 0
        self._quns, self._fg_facts = int(value or 0), facts
        kind = fullscreen_kind(int(value or 0), facts)
        full = bool(kind)
        title = window_title(hwnd) if full else ""
        if full != self.inputs.fullscreen or kind != self.inputs.fullscreen_kind:
            self._log("island_fullscreen", kind=kind, quns=int(value or 0), covers=facts.covers, zoomed=facts.zoomed,
                      caption=facts.caption, monitor=device)
        if full != self.inputs.fullscreen or title != self.inputs.fullscreen_title or kind != self.inputs.fullscreen_kind:
            self.update(fullscreen=full, fullscreen_title=title, fullscreen_kind=kind)
        else:
            self.window.reposition()
        self.reassert_topmost()

    def reassert_topmost(self) -> None:
        """Put the island and its halo back on top of every other always-on-top window (``HWND_TOPMOST``, no move, no
        size, no activation). Called on every foreground change: an event, never a poll. A loss is logged."""
        if not _is_windows():
            return
        try:
            from yuki.ui.focus import assert_topmost
        except Exception:
            return
        for name, win in (("halo", self.window.halo_win), ("island", self.window)):
            if not win.isVisible():
                continue
            was, ok = assert_topmost(int(win.winId()))
            if not was or not ok:
                self._log("island_topmost", window=name, was_topmost=was, ok=ok)

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
        if self.hovered:                                  # being read: it stays until the pointer has left
            self._final_timer.start(HOVER_OUT_MS + 700)
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
        isl.last_final = (" ".join(str(text or "").split()), kind, time.time())
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
    asking = str(call.get("app") or "a call") if isinstance(call, dict) and call.get("asking") else ""
    island.update(call=call if call_on(call) else None, call_asking=asking)


__all__ = ["BAR_H", "FinalInfo", "ForegroundWatch", "Island", "IslandCallIndicator", "IslandInputs", "IslandStrip",
           "IslandVoice", "IslandWindow", "RunInfo", "STATES", "Spec", "Steer", "active_screen", "call_since",
           "caption_spans", "clock", "compact_word", "elapsed", "fullscreen_kind", "on_call_status", "pick_screen",
           "resolve", "spec_for", "WindowFacts", "window_facts"]
