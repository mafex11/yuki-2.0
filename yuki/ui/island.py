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
paused              you took over the keyboard or mouse while a request worked: "You took over · paused" with
                    Resume and Stop (auto-expands; appTakeover, 4 Oct); Yuki says nothing
send-countdown      a plain send is about to go (auto-expands): who / where (swap chips), the exact text, "3 s to
                    stop" and Stop / Esc; then the send goes on (appPT, the user's 3 Oct screenshot of the mock)
voice-stopped       voice failed: the reason and the fix (Retry, Settings), on screen at least 8 s (auto-expands)
speaking            Yuki talks: "Speaking"; on hover the words being said, "say stop to cut me off"
hearing, listening  your turn: "Hearing you" / "Listening"; on hover a live caption, unsure words dimmed
working, thinking   a request runs: "Working · step 2" (no plan, so never "of N": it acts on the go); on hover
                    the brain's own live line of what it checks or does and why, the step, the time, Stop and the
                    run card
done-flash          the run ended: the result in a few words for 3 s, a stamped check
call-notice         a call or a game's voice chat started recording: what is kept ("Recording · Discord + you",
                    "Recording · VALORANT voice + you") for 2.5 s, then nothing; said again when a game joins a call
quiet-in-call       a call is recorded and nothing else goes on: hidden (the timer is in the tray's hover card)
not-listening       Yuki isn't listening (switched off, quiet, off for this call): hidden (the tray says it)
hidden-armed        listening for its name only: the tiny resting pill (click-through), or nothing (a setting)
==================  ====================================================================================

**Compact by default.** Every state but needs-you and voice-stopped shows a compact pill (the face and one short
word); resting the pointer on it expands it to the full bar with the 300 ms morph, and it folds back 300 ms after
the pointer leaves (sizes and timings: the mock's Island spec, 3 Oct polish round). The halo hangs only under the open
island, and only while Yuki hears, thinks or works, or speaks.

**The run card** (E7) expands from the expanded island (the chevron, or a click on it while a run goes): the live steps from
the chat thread (:class:`yuki.ui.chat_model.ChatModel`), a "from memory" chip where the agent's task frame says so,
the steer box ("added, not yet read" -> "read at step n", measured: the
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
from yuki.ui.call_words import call_rec
from yuki.ui.dots import FACE_W, HALO_GAP, HALO_H, HALO_W, FaceWidget, HaloWidget
from yuki.ui.lattice_cards import SendCard

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

STATES: tuple[str, ...] = ("game-quiet", "needs-you", "send-countdown", "paused", "voice-stopped", "speaking", "hearing",
                           "listening", "working", "thinking", "done-flash", "call-notice", "quiet-in-call",
                           "not-listening", "hidden-armed")
#: States with the halo figure of the same name: only while Yuki hears, thinks or works, or speaks (3 Oct, item 20).
HALO_STATES = frozenset({"hearing", "thinking", "working", "speaking"})
#: States that show the full bar without a hover: Yuki needs an answer, a send is about to go (3 s to stop), or voice
#: broke (the mock's Island spec: "On its own").
AUTO_EXPAND = frozenset({"needs-you", "send-countdown", "paused", "voice-stopped"})
#: appTakeover: the paused bar's words (the user took over the keyboard or mouse while a request worked).
PAUSED_LINE = "You took over · paused"
PAUSED_SUB = "Resume when you're ready, or Stop · say “go on” or type to Yuki to continue"
#: The run card's and the thread's line for a paused run, the thread's line when it goes on, and the end line of a
#: paused run that was stopped or never resumed (the agent's ``PAUSE_STOP_LINE``, the same words).
PAUSED_NOTE = "Paused (you took over)"
RESUMED_NOTE = "Going on from what's on screen now"
PAUSE_STOP_LINE = "Stopped. Nothing else was done."
#: The send countdown's open width (the mock's Island spec: "send countdown 620").
SEND_W = 620
#: The words the island and the tray use for "Yuki isn't listening" (v3.16, the voice session's states).
NOT_LISTENING_WORDS = {"off": "Yuki isn't listening: switched off", "quiet": "Yuki isn't listening: quiet for a while",
                       "call_off": "Yuki isn't listening: off for this call"}
#: Windows' notification states (``SHQueryUserNotificationState``) that alone mean a real full screen or nobody there:
#: QUNS_NOT_PRESENT (locked, screen saver), QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE. QUNS_BUSY (2) also
#: fires for a borderless window that only fills the monitor, so it counts only when the window in front measures as
#: full screen (:func:`fullscreen_kind`).
FULLSCREEN_STATES = frozenset({1, 3, 4})
QUNS_BUSY = 2
#: The window in front changed its size or place (going full screen inside one window: a browser video's F, F11, a
#: game's Alt+Enter): the full screen is checked again this long after the last such change (a drag is one check).
RESHAPE_SETTLE_MS = 200
#: WinEvent ids: the window in front changed, an object moved or resized (``EVENT_OBJECT_LOCATIONCHANGE``).
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
OBJID_WINDOW = 0
WINEVENT_SKIPOWNPROCESS = 0x0002
#: More location events a second than this from the window in front's thread is logged (the reshape watch's cost).
LOC_EVENTS_BUSY = 50


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
    # (no step total: Yuki acts on the go and plans nothing beforehand, so nothing on screen says "of N"; the user,
    # 3 Oct: "planning beforehand is not the right choice")
    steers: list[Steer] = field(default_factory=list)
    chips: dict[int, list[tuple]] = field(default_factory=dict)   # step number -> [(icon, text)]
    frame: dict[str, Any] | None = None
    #: The brain's own live line of what it checks or does now and why (its ``now`` note, or the situation check's
    #: plain line), for the current step; "" when the step came without one (its own words show instead).
    thought: str = ""
    #: appTakeover: paused because the user took over the keyboard or mouse (``paused_why``: keys | click | mouse).
    paused: bool = False
    paused_why: str = ""


@dataclass
class SendInfo:
    """The send countdown the island shows (appPT; ``yuki.log.events.SendPending``): ``phase`` ``counting`` (seconds
    to stop or swap) or ``switching`` (a chip was tapped; the countdown starts again when the send is ready)."""

    request_id: int
    phase: str = "counting"
    who: str = ""
    where: str = ""
    app: str = ""
    text: str = ""
    seconds: float = 3.0
    who_alts: tuple[str, ...] = ()
    where_alts: tuple[str, ...] = ()
    typed: bool = False
    from_memory: bool = False
    started: float = 0.0                     # time.monotonic() when this countdown began
    #: The title of the window the text goes into, as the app writes it (the chat it shows): a wrong chat in front is
    #: on the card whatever Yuki believes it is sending to (review F1). "" while switching.
    window: str = ""


@dataclass
class FinalInfo:
    """How the shown run ended: ``done`` | ``stopped`` | ``failed`` | ``handed``."""

    text: str
    outcome: str = "done"
    meta: str = ""
    run: RunInfo | None = None
    ended: float = 0.0                       # time.monotonic() when it ended (the run card's time)
    cost: str = ""                           # the run's cost, once the summary came ("33¢")
    #: honest (87): a done run's outcome level (verified | likely | unverified | failed; "" unknown): below likely the
    #: flash never shows the done chip
    level: str = ""


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
    send: SendInfo | None = None             # the send countdown (appPT)
    call: dict | None = None
    fullscreen: bool = False
    fullscreen_title: str = ""
    fullscreen_kind: str = ""                # "locked" | "d3d" | "presentation" | "window" (why it hides; logged)
    notice: str = ""                         # the call's start notice ("Recording · Discord + you"), 2.5 s
    call_asking: str = ""                    # the app of a call whose consent card waits ("" when none)
    call_asking_kind: str = ""               # that card's row: "call" | "game" (a game's voice chat)
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
    if i.send is not None:
        return "send-countdown"
    if i.run is not None and i.run.paused and not i.run.stopping:
        return "paused"
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
    """A call session is on the island: something is kept (``recording``, the status's top-level flag that
    :func:`yuki.ui.call_words.live_view` carries), or its row may record (recording, waiting for headphones)."""
    return bool(call) and (call.get("recording") is True or call.get("status") in ("recording", "waiting_headphones"))


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
    """The face's and the halo's step: ``(n, 0)``: the step it is on, and no total (there is no plan: the segments
    show the steps done so far and the current one filling, never the ones to come; ``yuki.ui.dots``)."""
    if run is None:
        return (1, 0)
    return (max(1, run.steps), 0)


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
    step: tuple[int, int] = (1, 0)
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
    """The compact pill's one short word for ``state`` ("Listening", "Working · step 2", the done result)."""
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
        return f"Working · step {n}" if n else "Working"
    if state == "send-countdown":
        s = i.send
        return f"Sending to {s.who}" if s is not None and s.who else "Sending"
    if state == "paused":
        return "Paused"
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
    rec = call_rec(i.call)
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
            face = _done_face(i.final)
        # the halo belongs to the open island: the pill never carries it
        return Spec(state, face=face, compact=True, word=compact_word(state, i), rec=rec, step=step_of(run),
                    title=_notice_title(i) if state == "call-notice" else compact_word(state, i))
    if state == "call-notice":
        return Spec(state, face="quiet-in-call", compact=True, word=i.notice, rec=rec, title=_notice_title(i))
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
    if state == "send-countdown" and i.send is not None:
        s = i.send
        target = f"{s.who} on {s.app}" if s.who and s.app else (s.who or s.app or "them")
        if s.phase == "counting":
            line = f"Sending to {target}…"
            swaps = bool(s.who_alts or s.where_alts)
            sub = ("resolved from memory" if s.from_memory else "from your words") + (
                " · tap a chip to swap" if swaps else f" · {int(round(s.seconds))} s to stop")
        else:
            line = f"Switching to {target}…"
            # review F3: text already typed stays in the first chat's box until it is cleared: said, never hidden
            sub = ("the text typed in the first chat stays there, not sent, until it is cleared" if s.typed
                   else "the countdown starts again once it's ready there")
        return Spec(state, face="speaking", exp=True, width=SEND_W, line=line, sub=sub, rec=rec, body="send",
                    title=line)
    if state == "paused" and run is not None:
        # appTakeover: the user took over: the request waits, Resume / Stop under the line; the chevron opens its card
        if expanded:
            return Spec(state, face="handed", exp=True, width=RUN_W, line=PAUSED_LINE, sub=PAUSED_SUB, rec=rec,
                        body="run", keys=("chev",), title=PAUSED_NOTE)
        return Spec(state, face="handed", exp=True, width=600, line=PAUSED_LINE, sub=PAUSED_SUB, rec=rec,
                    body="paused", keys=("chev",), title=PAUSED_NOTE)
    if state == "voice-stopped":
        return Spec(state, face="voice-stopped", width=720, line="Voice stopped",
                    sub=i.voice_error, keys=("retry", "settings"), title=i.voice_error)
    if state == "working" and run is not None:
        # the brain's own line of what it checks or does now and why, when the step came with one; its step's own
        # words then move to the sub line (appPT: real reasoning, not a canned status)
        step = f"step {max(1, run.steps)}"
        line = run.thought or run.line or "Working…"
        mid = f" · {run.line}" if run.thought and run.line else ""
        sub = f"{step}{mid} · {elapsed(now - run.started)}"
        if expanded:
            return Spec("working", face="working", exp=True, width=RUN_W, line=line, sub=sub, rec=rec,
                        step=step_of(run), body="run", keys=("chev",))
        return Spec(state, face="working", width=680, line=line, sub=sub, rec=rec, keys=("stop", "chev"),
                    halo="working", step=step_of(run))
    if state == "thinking":
        if run is not None:
            sub = run.hint or (f"you said “{i.heard}”" if i.heard else "") or elapsed(now - run.started)
            if run.hint or i.heard:
                sub = f"{elapsed(now - run.started)} · {sub}"
            line = run.thought or run.line or "Thinking…"
            if expanded:
                return Spec("thinking", face="thinking", exp=True, width=RUN_W, line=line, sub=sub, rec=rec,
                            body="run", keys=("chev",))
            return Spec(state, face="thinking", line=line, sub=sub, rec=rec, keys=("stop", "chev"), halo="thinking")
        return Spec(state, face="thinking", line=i.transient or "Thinking…",
                    sub=(f"you said “{i.heard}”" if i.heard else "Working it out"), rec=rec, halo="thinking")
    if state == "done-flash":
        f = i.final or FinalInfo("Done.")
        if expanded and f.run is not None:
            return Spec(state, face=_done_face(f), exp=True, width=RUN_W,
                        line=f.text, sub=f.meta or _final_sub(f), rec=rec, body="run", keys=("chev",))
        keys = ("chev",) if f.run is not None else ()   # the finished run's card opens from here
        if f.outcome == "done" and f.level in LEVEL_CHIPS:
            # honest (87): ended with words its checks do not back: never the done chip
            return Spec(state, face="handed", line=f.text, sub=f.meta or _final_sub(f), chip=LEVEL_CHIPS[f.level],
                        keys=keys)
        if f.outcome == "done":   # the design's done flash carries its chip only (no rec chip)
            return Spec(state, face="done-flash", line=f.text, sub=f.meta or _final_sub(f), chip="done", keys=keys)
        return Spec(state, face="handed", line=f.text, sub=f.meta or _final_sub(f), chip=f.outcome, keys=keys)
    del app
    return Spec("hidden-armed", face="idle", hidden=True, why_hidden="unknown state")


def _notice_title(i: IslandInputs) -> str:
    """The call notice's tooltip: the whole line ("Recording · Discord + VALORANT voice + you"), even when the pill
    shows its shorter form."""
    from yuki.ui.call_words import call_line

    return (call_line(i.call) if i.call else "") or i.notice


def notice_words(call: dict | None, avail: float | None = None) -> str:
    """The call notice's words for the compact pill: the short line ("Recording · Discord + VALORANT + you"); when
    it measures wider than the pill's room (``avail`` px, the pill's widest less its face, gaps and rec dot), the
    words alone (the rec dot already says recording). Measured on the pill's own font."""
    from yuki.ui.call_words import call_line

    call = call or {}
    line = str(call.get("short") or "") or call_line(call)
    words = str(call.get("short_words") or "")
    if not words or avail is None:
        return line
    return line if QFontMetricsF(L.ui_font(13.5, 500)).horizontalAdvance(line) <= avail else words


#: The compact pill's room for its words while the rec dot shows (COMPACT_MAX_W less the face, pads and gaps).
NOTICE_ROOM = COMPACT_MAX_W - (1 + COMPACT_PAD + FACE_W * COMPACT_PITCH + COMPACT_GAP + 4 + COMPACT_GAP + 7
                               + COMPACT_PAD_R + 1)


#: honest (87): the done flash's chip for a run whose outcome level is below likely, and its sub line's word.
LEVEL_CHIPS = {"unverified": "unconfirmed", "failed": "failed"}
LEVEL_WORDS = {"unverified": "Not confirmed", "failed": "Didn't work"}


def _done_face(f: FinalInfo | None) -> str:
    """The end's face: the stamped done check only for a run that ended done at likely or better (honest review F6:
    the pill and the open run card too, not only the flash); below likely, or not done, the plain hand-back face."""
    if f is None:
        return "done-flash"
    return "done-flash" if f.outcome == "done" and f.level not in LEVEL_CHIPS else "handed"


def _final_sub(f: FinalInfo) -> str:
    """The end word's sub line (the design's run card heads: "Nothing was sent · 0:44", "You have the controls")."""
    word = {"done": "Done", "stopped": "Stopped by you", "failed": "It didn't finish",
            "handed": "You have the controls"}.get(f.outcome, "")
    if f.outcome == "done" and f.level in LEVEL_WORDS:
        word = LEVEL_WORDS[f.level]
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


def reshape_matters(hwnd: int, obj: int, child: int, watched: int) -> bool:
    """Whether a location-change event is the window in front itself changing size or place (not its caret, cursor
    or one of its parts)."""
    return bool(watched) and int(hwnd or 0) == int(watched) and int(obj) == OBJID_WINDOW and int(child) == 0


class ForegroundWatch(QObject):
    """Windows says when the window in front changes (``SetWinEventHook(EVENT_SYSTEM_FOREGROUND)``, out of context:
    delivered on this thread's message loop): no polling. Each change re-reads the monitor in use and whether a full
    screen covers it (``SHQueryUserNotificationState``).

    Going full screen inside the same window (a browser video's F, F11, a game's Alt+Enter) is no foreground change,
    so the window in front is also watched for changing its size or place: ``EVENT_OBJECT_LOCATIONCHANGE``, hooked
    only for that window's own thread (re-hooked on each foreground change) and filtered to the window itself;
    :attr:`reshaped` says so once, :data:`RESHAPE_SETTLE_MS` after the last change (a drag is one check)."""

    changed = Signal(int)
    reshaped = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._hook = None
        self._proc = None
        self._loc_hook = None
        self._loc_proc = None
        #: The window in front whose own size changes are watched (0: none).
        self.watched = 0
        #: Location events delivered for the watched window's thread since it was hooked (a cost check in the log).
        self.loc_events = 0
        self.loc_since = time.monotonic()
        self._rehook = QTimer(self)
        self._rehook.setSingleShot(True)
        self._rehook.setInterval(0)
        self._rehook.timeout.connect(self._hook_location)
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(RESHAPE_SETTLE_MS)
        self._settle.timeout.connect(lambda: self.reshaped.emit(int(self.watched)))

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
                    self.follow(int(hwnd or 0))
                    self.changed.emit(int(hwnd or 0))
                except Exception:
                    pass

            def on_location(_hook: Any, _event: int, hwnd: Any, obj: int, child: int, _thread: int, _t: int) -> None:
                self.loc_events += 1
                if reshape_matters(int(hwnd or 0), obj, child, self.watched):
                    self._settle.start()          # restarted by each change: one check after the last

            self._proc = proto(on_event)
            self._loc_proc = proto(on_location)
            user32.SetWinEventHook.restype = wintypes.HANDLE
            self._hook = user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, self._proc,
                                                0, 0, 0)                                   # out of context
            return bool(self._hook)
        except Exception:
            self._hook = None
            return False

    def follow(self, hwnd: int) -> None:
        """Watch ``hwnd`` (the new window in front) for changing its own size; hooked on the next loop turn, not inside
        the WinEvent callback."""
        self.watched = int(hwnd or 0)
        self._settle.stop()
        self._rehook.start()

    def _thread_of(self, hwnd: int) -> tuple[int, int]:
        """``(process id, thread id)`` of ``hwnd``; zeros when unknown."""
        try:
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32")
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            pid = wintypes.DWORD(0)
            tid = int(user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid)))
            return int(pid.value), tid
        except Exception:
            return 0, 0

    def _unhook_location(self) -> None:
        if self._loc_hook:
            try:
                ctypes.WinDLL("user32").UnhookWinEvent(self._loc_hook)
            except Exception:
                pass
            self._loc_hook = None

    def _hook_location(self) -> None:
        self._unhook_location()
        self.loc_events = 0
        self.loc_since = time.monotonic()
        if not self._hook or not self.watched or self._loc_proc is None:
            return
        pid, tid = self._thread_of(self.watched)
        if not tid:
            return
        try:
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32")
            user32.SetWinEventHook.restype = wintypes.HANDLE
            self._loc_hook = user32.SetWinEventHook(EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_LOCATIONCHANGE, None,
                                                    self._loc_proc, pid, tid, WINEVENT_SKIPOWNPROCESS)
        except Exception:
            self._loc_hook = None

    def stop(self) -> None:
        self._rehook.stop()
        self._settle.stop()
        self._unhook_location()
        self.watched = 0
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
    resume = Signal()

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
        self.k_resume = L.Key("Resume", variant="pri", parent=self)    # appTakeover: shown while paused
        self.k_resume.hide()
        self.k_resume.clicked.connect(self.resume.emit)
        self.paused = False
        self.k_hand = L.Key("I’ll take it from here", parent=self)
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
        self.mode = "live"
        self._say_on = False
        self._sum_on = False

    def set_card(self, *, mode: str, title: str, touch: str, count: str, cost: str, asked: str,
                 steps: list[dict[str, Any]], steers: list[Steer], summary: tuple[str, str] | None = None,
                 foot_note: str = "", stop_kbd: str = "Alt Shift X", waiting: str = "",
                 paused: bool = False) -> None:
        """``mode``: ``live`` | ``done`` | ``stopped`` | ``handed`` | ``failed``. ``steps``: ``{kind, verb, obj, ms,
        note, chips}`` in order, the steps so far (never the ones to come). ``waiting``: the brain's own line before
        its first step, in place of "Getting ready…"."""
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
        self.empty.set_text("" if steps else ((waiting or "Getting ready…") if live else "No steps."))
        self.empty.setVisible(not steps)
        self.steerbox.setVisible(live)
        if live:
            self.steerbox.set_steers(steers)
        self.k_stop.set_parts(kbd=stop_kbd)
        for k in (self.k_stop, self.k_hand, self.k_ledger):
            k.setVisible(live)
        self.paused = live and bool(paused)
        self.k_resume.setVisible(self.paused)   # appTakeover: the user took over; Resume goes on
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
        for k in (self.k_resume, self.k_stop, self.k_hand, self.k_again):
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
        self.setToolTip("Recording")

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
        self.k_resume = L.Key("Resume", variant="pri", size="sm", parent=P)    # appTakeover: the paused bar
        self.k_pstop = L.Key("Stop", variant="ghost", size="sm", parent=P)
        self.run = RunCard(P)
        self.send = SendCard(P)    # appPT: the send countdown, the island's open view while a send waits
        self.send.hide()
        for w in (self.k_stop, self.k_chev, self.k_retry, self.k_settings, self.k_answer, self.k_qstop, self.k_resume,
                  self.k_pstop):
            w.hide()
        self.k_resume.setToolTip("Go on: Yuki reads the screen again first")
        self.k_pstop.setToolTip("Stop this request: nothing else is done")
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
        if spec.body in ("choices", "paused"):
            return 26 + 16
        if spec.body == "run":
            return self.run.height_for(w - 2)
        if spec.body == "send":
            return self.send.height_for(w - 2)
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
        elif spec.body == "paused":
            self.k_resume.move(1 + CAPTION_INDENT, y)
            self.k_pstop.move(1 + CAPTION_INDENT + self.k_resume.width() + 8, y)
        elif spec.body == "run":
            self.run.setGeometry(1, y, w - 2, max(0, self.run.height_for(w - 2)))
        elif spec.body == "send":
            self.send.setGeometry(1, y, w - 2, max(0, self.send.height_for(w - 2)))
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
            icon = {"done": "check", "stopped": "stop", "failed": "x", "handed": "hand",
                    "unconfirmed": "dash"}.get(spec.chip, "check")
            self.chip.icon_name = icon
            self.chip.set_text(spec.chip)
        self.k_chev.icon_name = "chev"
        self.k_chev.angle = 0
        self.caption.setVisible(spec.body == "caption")
        for w in (self.k_answer, self.k_qstop):
            w.setVisible(spec.body == "choices")
        for w in (self.k_resume, self.k_pstop):
            w.setVisible(spec.body == "paused")
        self.run.setVisible(spec.body == "run")
        self.send.setVisible(spec.body == "send")
        pitch =3 if spec.mini else COMPACT_PITCH if spec.compact else 8
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
        halo_new = self._place_halo(spec, reduced)
        if not was_visible or halo_new:
            # Qt shows these no-activate tool windows with SW_SHOWNOACTIVATE, which keeps their old place in the
            # z-order: an always-on-top window that came forward while they were hidden would stay above them
            self.island.reassert_topmost()

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

    def _place_halo(self, spec: Spec, reduced: bool) -> bool:
        """Show, move or hide the halo for ``spec``; True when it has just been shown."""
        hw = self.halo_win
        if spec.halo and not spec.mini:
            first = not self._halo_on or not hw.isVisible()
            self._halo_on = True
            hw.halo.set_state(spec.halo, spec.step)
            self._move_halo()
            if first:
                hw.fade_in(animate=not reduced)
            return first
        if self._halo_on:
            hw.halo.model.set_state("idle", L.now())   # blends away the next time it is shown, as the design
            hw._fade.stop()
            hw.hide()
        self._halo_on = False
        return False

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
        self._move_kind = "enter"     # set first: changing a stopped animation's values emits valueChanged
        self._move.setDuration(ENTER_MS)
        self._move.setEasingCurve(L.ease_curve())
        self._move.setStartValue(0.0)
        self._move.setEndValue(1.0)
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
        self._move_kind = "leave"
        self._move.setDuration(LEAVE_MS)
        self._move.setEasingCurve(L.ease_curve())
        self._move.setStartValue(0.0)
        self._move.setEndValue(1.0)
        self._move.start()

    def _on_move(self, v: Any) -> None:
        # Only a running animation moves the window: Qt also emits valueChanged when a stopped animation's start or
        # end value is set (the old code then applied the previous move's kind: an enter could jump the window to
        # the leave's opacity and offset before it started).
        if self._move.state() != QVariantAnimation.State.Running:
            return
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
        send_stop(int): Stop (or Esc) on the send countdown of that request: nothing is sent.
        send_swap(int, str, str): a chip tapped on the send countdown: ``(request, "who" | "where", the new value)``.
    """

    stop_clicked = Signal(int)
    handback_clicked = Signal(int)
    steer_requested = Signal(int, str)
    answer_clicked = Signal(int)
    retry_voice = Signal()
    open_page = Signal(str)
    ask_again = Signal(str)
    state_changed = Signal(str)
    send_stop = Signal(int)
    send_swap = Signal(int, str, str)
    #: appTakeover: Resume on the paused bar or the run card (the user took over; the request goes on).
    resume_clicked = Signal(int)

    def __init__(self, *, bindings: dict[str, str] | None = None, model: Any = None) -> None:
        super().__init__()
        self.inputs = IslandInputs()
        self.bindings = dict(bindings or {})
        self.model = model
        self.state = ""
        self.expanded = False
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
        self.window.k_resume.clicked.connect(self._on_resume)
        self.window.k_pstop.clicked.connect(self._on_stop)
        rc = self.window.run
        rc.resume.connect(self._on_resume)
        rc.stop.connect(self._on_stop)
        rc.handback.connect(self._on_handback)
        rc.steer.connect(self._on_steer)
        rc.ledger.connect(lambda: self.open_page.emit("activity"))
        rc.close.connect(self._close_card)
        rc.again.connect(self._on_again)
        self.window.send.stop.connect(self._on_send_stop)
        self.window.send.swap.connect(self._on_send_swap)
        #: A new send countdown is to start its drain at the next render (once per countdown, not per redraw).
        self._send_restart = False
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
        self.watch.reshaped.connect(self.on_reshaped)
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
        hwnd = foreground_hwnd()
        self.watch.follow(hwnd)
        self.on_foreground(hwnd)
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
        if spec.body == "send":
            return "send countdown"
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
        island itself is on top there, or unknown). A class name is not content (``Chrome_WidgetWin_1``).

        ``"n/a"`` while the island lets clicks through (the resting pill): hit-testing skips a WS_EX_TRANSPARENT
        window, so it would name the app beneath although nothing covers the pill."""
        if not _is_windows() or not self.window.isVisible():
            return ""
        if self.window._click_through:
            return "n/a"
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
        """A call (or a game's voice chat) that starts recording says what is kept for 2.5 s ("Recording · Discord
        + you"), once per call session, and once more when a game joins the call or the call ends while the game
        goes on (the kinds of rows that may record changed); after that nothing about the call stays on screen (the
        tray's icon and hover card carry it). While the notice shows, its words follow the status (an app's sound
        that was still opening joins them)."""
        if not call_rec(now):
            if self.inputs.notice:
                self._notice_timer.stop()
                self.inputs = replace(self.inputs, notice="")
            return
        now = now or {}
        session = now.get("session_started_at") or now.get("id") or now.get("detected_at")
        key = (session, tuple(now.get("kinds") or ()))
        words = notice_words(now, NOTICE_ROOM)
        if key == self._noticed_call:
            if self.inputs.notice and self._notice_timer.isActive() and words != self.inputs.notice:
                self.inputs = replace(self.inputs, notice=words)
            return
        self._noticed_call = key
        self.inputs = replace(self.inputs, notice=words)
        self._notice_timer.start(CALL_NOTICE_MS)
        del before

    def refresh(self, *, animate: bool = True) -> None:
        if not self.enabled:
            return
        state = resolve(self.inputs)
        if state not in ("working", "thinking", "done-flash", "paused"):
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
        if (self.hovered or self._hover_in.isActive() or self._hover_out.isActive()) and (spec.hidden or spec.resting):
            # the resting pill never opens and lets the pointer through (and a hidden island has nothing under the
            # pointer), so Qt may never send the leave that clears the hover: clear it here, or the next state would
            # open as the full bar though nothing opened it
            self.hovered = False
            self._hover_in.stop()
            self._hover_out.stop()
            spec = self.current_spec()
        if spec.body == "caption":
            text, spans = caption_spans(self.inputs.caption, unsure_threshold())
            self.window.caption.set_text(text, spans)
        if spec.body == "send":
            self._fill_send()
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
        need = (self.window.isVisible() and not spec.hidden and not spec.compact and self.inputs.run is not None
                and spec.state in ("working", "thinking"))
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
            for timer in (self._clock, self._hover_in, self._hover_out):
                L.stop_timer(timer)
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
        """The window in front changed: the monitor in use, the full screen, and the island back on top."""
        w = self.watch
        took = time.monotonic() - float(getattr(w, "loc_since", time.monotonic()))
        if took > 5 and w.loc_events / took > LOC_EVENTS_BUSY:   # the reshape watch's cost on the last window
            self._log("island_reshape_watch", events=int(w.loc_events), seconds=round(took), busy=True)
        self.check_screen(hwnd)
        self.reassert_topmost()

    def on_reshaped(self, hwnd: int) -> None:
        """The window in front changed its size or place (F11, a video's full screen, Alt+Enter): check again."""
        if hwnd and hwnd == self.watch.watched:
            self.check_screen(hwnd)

    def check_screen(self, hwnd: int) -> None:
        """Measure the window in front against its monitor and Windows' notification state: hide only for a real
        full screen (:func:`fullscreen_kind`), and follow the monitor in use."""
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

    def reassert_topmost(self) -> None:
        """Put the island and its halo back on top of every other always-on-top window (``HWND_TOPMOST``, no move, no
        size, no activation). Called on every foreground change and whenever the island or its halo comes on screen
        (a hidden window is skipped on a foreground change, and Qt shows it again at its old place in the z-order):
        events, never a poll. A loss is logged."""
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
        if self.state not in ("working", "thinking", "done-flash", "paused"):
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
        # the steps done so far, never "of N" (there is no plan; the user, 3 Oct); nothing before the first is done
        count = str(done) if done or mode != "live" else ""
        cost = ("done so far" if count else "") if mode == "live" else \
            (f"{final.cost} total" if final and final.cost else "total")
        if mode == "live" and run.thought and steps:
            # the brain's own line for what it checks or does now, under the current step (else the latest)
            cur = next((s for s in reversed(steps) if s["kind"] == "running"), steps[-1])
            if not cur.get("note"):
                cur["note"] = run.thought
        asked = _clean(run.request)
        title = asked
        touch = _touching(run.frame)
        paused = mode == "live" and run.paused
        if paused:   # appTakeover: the card says it waits for the user, above everything it touches
            touch = PAUSED_NOTE
        summary = None
        if mode in ("stopped", "handed"):
            did = [f"{s['verb']} {s['obj']}".strip() for s in steps if s["kind"] == "done"]
            summary = ("; ".join(did) or "Nothing yet", "What was left of the request")
        note = {"handed": "You have the mouse and keys", "stopped": "Nothing else ran", "failed": "It stopped there",
                "done": ""}.get(mode, "")
        if mode == "done" and final is not None and final.level in LEVEL_WORDS:
            note = LEVEL_WORDS[final.level]   # honest (87): the run's checked level, never a plain done
        self.window.run.set_card(mode=mode, title=title, touch=touch, count=count, cost=cost,
                                 asked=asked if self._needs_say(asked) else "", steps=steps, steers=run.steers,
                                 summary=summary, foot_note=note,
                                 stop_kbd=_words(self.bindings.get("cancel")).replace("+", " ") or "Alt Shift X",
                                 waiting=run.thought if mode == "live" else "",
                                 paused=paused)

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
            chips = [(chip[0], chip[1]) for chip in run.chips.get(k, [])]
            out.append({"kind": kind, "verb": verb.strip(), "obj": obj.strip(), "ms": ms, "note": note[:160],
                        "chips": chips})
        return out

    def _on_stop(self) -> None:
        run = self.inputs.run
        if run is not None:
            self.strip.stopping()
            self.stop_clicked.emit(run.id)

    def _on_resume(self) -> None:
        """appTakeover: Resume on the paused bar or the run card."""
        run = self.inputs.run
        if run is not None and run.paused:
            self.resume_clicked.emit(run.id)

    def set_paused(self, request_id: int, paused: bool, why: str = "") -> None:
        """appTakeover: the runtime's ``paused_changed`` for the request shown: paused (the bar opens to "You took over ·
        paused" with Resume and Stop) or going on again. Content-free log."""
        run = self.inputs.run
        if run is None or run.id != int(request_id) or run.paused == bool(paused):
            return
        self._log("island_takeover", paused=bool(paused), why=str(why or ""))
        self.update(run=replace(run, paused=bool(paused), paused_why=str(why or "") if paused else ""))

    def _on_question_stop(self) -> None:
        run = self.inputs.run
        if run is not None:
            self.stop_clicked.emit(run.id)
        self.update(question="", question_id=None)

    # -- the send countdown (appPT) ----------------------------------------------------------------------------

    def show_send(self, request_id: int, send: dict[str, Any]) -> None:
        """The runtime's ``send_pending`` for request ``request_id``: ``counting`` opens the send countdown on its own
        (a new drain each time), ``swapped`` keeps it open as "Switching to …" until the send is ready again,
        ``going`` and ``stopped`` close it."""
        state = str(send.get("state") or "")
        cur = self.inputs.send
        rid = int(request_id)

        def alts(key: str) -> tuple[str, ...]:
            return tuple(str(x) for x in (send.get(key) or ()) if str(x).strip())[:2]

        # the exact text: spaces tidied within each line, its line breaks kept (one may go out as Enter: review F9)
        lines = [" ".join(part.split()) for part in str(send.get("text") or "").splitlines()]
        fields = {"who": str(send.get("who") or ""), "where": str(send.get("where") or ""),
                  "app": str(send.get("app") or ""), "text": "\n".join(lines).strip("\n"),
                  "who_alts": alts("who_alts"), "where_alts": alts("where_alts"), "typed": bool(send.get("typed")),
                  "from_memory": bool(send.get("from_memory")), "window": " ".join(str(send.get("window") or "").split())}
        if state == "counting":
            self._send_restart = True
            self._log("send_countdown", state=state, swaps=len(fields["who_alts"]) + len(fields["where_alts"]))
            self.update(send=SendInfo(rid, "counting", seconds=float(send.get("seconds") or 3.0),
                                      started=time.monotonic(), **fields))
        elif state == "swapped":
            self._log("send_countdown", state=state)
            self.update(send=SendInfo(rid, "switching", seconds=float(send.get("seconds") or 3.0),
                                      started=time.monotonic(), **fields))
        elif cur is not None and cur.request_id == rid:
            self._log("send_countdown", state=state)
            self.update(send=None)

    def clear_send(self, request_id: int | None = None) -> None:
        """The run ended (or asked something): no send countdown stays open for it."""
        cur = self.inputs.send
        if cur is not None and (request_id is None or cur.request_id == int(request_id)):
            self.update(send=None)

    def _fill_send(self) -> None:
        s = self.inputs.send
        if s is None:
            return
        run = self.inputs.run
        said = _clean(run.request) if run is not None and run.id == s.request_id else ""
        if len(said) > 70:
            said = said[:69].rstrip() + "…"
        restart, self._send_restart = self._send_restart, False
        self.window.send.set_send(phase=s.phase, who=s.who, where=s.where, app=s.app, text=s.text, said=said,
                                  seconds=s.seconds, who_swap=bool(s.who_alts), where_swap=bool(s.where_alts),
                                  restart=restart and s.phase == "counting", window=s.window)

    def _on_send_stop(self) -> None:
        s = self.inputs.send
        if s is not None:
            self.send_stop.emit(s.request_id)

    def _on_send_swap(self, kind: str) -> None:
        s = self.inputs.send
        if s is None or s.phase != "counting":
            return
        alts = s.who_alts if kind == "who" else s.where_alts
        if alts:
            self.send_swap.emit(s.request_id, kind, alts[0])

    def set_thought(self, request_id: int, text: str) -> None:
        """The brain's own live line for the current step (its ``now`` note, or the situation check's plain line): the
        thinking / working line and the run card's current step show it until the next step (appPT)."""
        from yuki.ui.step_words import plain_thought

        run = self.inputs.run
        line = plain_thought(text)
        if run is None or run.id != int(request_id) or not line or line == run.thought:
            return
        self.update(run=replace(run, thought=line))

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

    def set_frame(self, request_id: int, frame: dict[str, Any]) -> None:
        """The person the model's own call named (``yuki.ui.runtime.named_view``) and their usual route from memory:
        that call's step's "from memory" chip ("you message Sam on WhatsApp, 78 times"). The frame comes right after
        the call's ``tool_called`` (or, for an ``about`` call, its ``tool_finished``), so the chip goes on the latest
        step shown (the first when none shows yet). Keyword
        audit D1 (20 Oct): never the thinking line, and never before the model named the person (the review F4 line
        that showed a name memory found in the request's words before any plan is gone)."""
        run = self.inputs.run
        if run is None or run.id != request_id:
            return
        run.frame = frame
        chip = _route_chip(frame)
        if chip:
            run.chips.setdefault(max(1, self._shown_steps(run)), []).insert(0, ("memory", chip))
        if self.expanded:
            self._render(animate=False)

    def _shown_steps(self, run: RunInfo) -> int:
        """How many steps the run's card shows now (the chat thread's real steps; 0 without one)."""
        finder = getattr(self.model, "item_for_job", None) if self.model is not None else None
        try:
            item = finder(run.id) if callable(finder) else None
            return len(item.real_steps()) if item is not None else 0
        except Exception:
            return 0

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
            run.steers, run.chips, run.frame, run.thought = keep.steers, keep.chips, keep.frame, keep.thought
            run.paused, run.paused_why = keep.paused, keep.paused_why
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
        # a new step starts without the last one's thought (its own note, if any, comes right after it)
        self.island.update(run=replace(run, line=text, steps=new_steps, hint="",
                                       thought="" if new_steps > run.steps else run.thought))

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
        send = self.island.inputs.send
        self.island.update(run=None, send=None if send is not None and send.request_id == run.id else send)
        self._changed(was)
        return True

    def show_step(self, text: str) -> None:
        run = self.island.inputs.run
        if run is not None:
            self.island.update(run=replace(run, line=text))
        else:
            self.island.update(transient=text)

    def show_final(self, text: str, *, tone: str = "final", outcome: str | None = None, level: str = "") -> None:
        was = self.isVisible()
        run = self.island.inputs.run
        if run is not None:
            self._ended = run
            self.island.inputs = replace(self.island.inputs, run=None)
        if self.island.inputs.send is not None and (run is None or self.island.inputs.send.request_id == run.id):
            self.island.inputs = replace(self.island.inputs, send=None)   # the run is over: no countdown stays
        ended = getattr(self, "_ended", None)
        kind = outcome or ("failed" if tone == "error" else "done")
        isl = self.island
        isl.last_final = (" ".join(str(text or "").split()), kind, time.time())
        isl.update(final=FinalInfo(text, kind, "", ended, ended=time.monotonic(), level=level or ""), transient="")
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
        """Yuki asked something and waits: needs-you (it auto-expands). A send countdown left switching for this
        request closes: the question decides what happens next."""
        send = self.island.inputs.send
        self.island.update(question=" ".join(str(question or "").split()), question_id=int(request_id),
                           question_voice=bool(voice),
                           send=None if send is not None and send.request_id == int(request_id) else send)

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
    """``CallController.changed``: the session being recorded (or waiting for headphones) goes on the island, as
    :func:`yuki.ui.call_words.live_view` says it (what is kept: the mic, the call's own sound, a game's voice chat);
    a consent card that waits (the live call's, or a game's right after its session) makes the tray's call-ask."""
    from yuki.ui.call_words import card_target, live_view

    status = status if isinstance(status, dict) else {}
    view = live_view(status)
    target = card_target(status)
    asking = str(target.get("app") or "a call") if target else ""
    island.update(call=view if call_on(view) else None, call_asking=asking,
                  call_asking_kind=str((target or {}).get("kind") or "call") if target else "")


__all__ = ["BAR_H", "FinalInfo", "ForegroundWatch", "Island", "IslandCallIndicator", "IslandInputs", "IslandStrip",
           "IslandVoice", "IslandWindow", "RunInfo", "STATES", "Spec", "Steer", "active_screen", "call_on",
           "call_rec", "call_since", "caption_spans", "clock", "compact_word", "elapsed", "fullscreen_kind",
           "notice_words", "on_call_status", "pick_screen",
           "resolve", "spec_for", "WindowFacts", "window_facts"]
