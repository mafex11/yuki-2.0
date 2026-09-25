"""The talker's hands: single desktop steps the mouth does itself (docs/VOICE.md §5.6).

gradient-bang's voice LLM calls its quick tools itself and hands only
multi-step work to task agents (docs/research/voice-parallel.md §1). Yuki's
mouth gets the same: a few one-step desktop tools (a key or shortcut in one
window, a media or volume key, scrolling, a window's state, one tab of a
window's tab strip, opening a URL or an app), each backed by the existing
:mod:`yuki.actions` code through the brain's own backend, with the brain's
guards: keys and scrolling go to the named window only while it is in front
(``expect_hwnd``); and only a window (or tab) the mouth was shown in this
turn's desktop summary can be acted on (a hand never guesses).

**Instant hands act at once (v3.3).** Live on 2026-09-25 the reply's words
streamed first and its tool call came after them, so "scroll this window" was
heard in full ("okay, I'm scrolling this window...") before anything moved. An
instant hand (``press_keys``, ``media_key``, ``scroll``, ``window``, ``tab``) is
now called with no words before it: the reply is the tool call alone, and the
action runs the moment the call is complete. What is heard is the hand's done
line (``say_when_done``, written with the call), said by code the moment the
hand's own report *verifies* the effect, with no second model call. The slow
hands (``open_url``, ``open_app``: seconds) and hand-offs to the thinker keep a
few words first.

**"Done" only from a verified effect (v3.3).** Live, "done" was said three times
for effects that had not happened (a window focused for a tab switch; two
scrolls whose wheel went to the tab bar; YouTube "up" before it had loaded).
Each hand now reports ``verified`` True / False / None from what it read back:
a window's state, the selected tab, the scroll position, the media session's
status, the output volume, the page's address *and* title. The done line is
said only on True; otherwise the report goes to the mouth, which says honestly
what happened and hands it to the thinker (``press_keys`` always reports back:
a key may be meant for anything, so it has no verdict).

**What the hands did is shared (v3.3).** Live, the hands opened a YouTube tab and
the thinker, asked for "the new tab we just opened", clicked another one. Every
hand action and its verified report, and every desktop action of the thinker's,
goes into :class:`ActionLog`; its lines are in the mouth's desktop block and in
every request handed to the thinker (:func:`yuki.voice.mouth.brain_request`).
An opened page records which tab it is (place, name, window).

The desktop summary (:meth:`Hands.snapshot`) is the brain's own overview
(``format_overview``) plus the media sessions, the foreground window's tabs and
the action log, captured off the event loop for every mouth call and put in the
volatile block after every cache breakpoint. Sizes and times go to the voice log
(content-free).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: Media and volume keys (pyautogui names, as :func:`yuki.actions.input.normalize_key` takes them).
MEDIA_KEYS = ("playpause", "nexttrack", "prevtrack", "stop", "volumeup", "volumedown", "volumemute")
#: The media keys whose effect the media session shows (its status), watched after sending.
_TRANSPORT_KEYS = ("playpause", "stop", "nexttrack", "prevtrack")
#: The keys whose effect the output volume shows.
_VOLUME_KEYS = ("volumeup", "volumedown", "volumemute")
_WATCH_S = 0.8
_POLL_S = 0.05
#: How long an opened page may take to get its title (the page is at the address before it has one).
_TITLE_WAIT_S = 6.0
#: The foreground window's tabs are read within this when the summary needs them now.
_TABS_BUDGET_S = 0.15
#: A tab strip read ahead (when the user starts talking) serves a summary for this long, while the
#: window's title is the same and nothing was done on the desktop since.
_TABS_FRESH_S = 20.0
#: How many action-log lines the mouth's desktop block carries (the thinker gets more).
_MOUTH_ACTION_LINES = 4

#: Hands that act at once, with no words before them.
INSTANT_HANDS = ("press_keys", "media_key", "scroll", "window", "tab")

_WINDOW = {
    "type": "integer",
    "description": "The window's hwnd, the [number] on its line in the desktop block of the latest user message.",
}
_SAID_NOTE = "It worked, verified, and your done line was said. Do not say it again."
_UNCONFIRMED_NOTE = (
    "The report does not show that it took effect. Say so honestly in a few words from the report, never "
    "that it is done. If the report says why nothing more can happen (already at the end, already showing), "
    "say that and stop. Otherwise hand it to the thinker in this same reply: tell_yuki with the user's "
    "request as they asked it and this report as context."
)
_FAILED_NOTE = (
    "It did not work. Say so in a few words and hand it to the thinker in this same reply: tell_yuki "
    "with the user's request as they asked it and this report as context. If the report says why nothing "
    "more can happen (already at the end), say that and stop."
)
_KEYS_NOTE = (
    "The keys went out; this is what the report saw. Say in a few words what it shows happened, never "
    "more than it shows. If it shows the keys did nothing they wanted, say so and hand it to the thinker "
    "with tell_yuki (their request, and this report as context)."
)
#: Every hand's done line: said the moment its report verifies the step, with no second model call.
_DONE_LINE = {
    "type": "string",
    "description": (
        "The few words to say the moment the report verifies it worked, e.g. \"Done, it's minimized.\". "
        "Said only then, by the hand; if it did not work you get the report instead."
    ),
}
_NOW = (" Instant: call it with no words before it, the call alone; its done line is what the user hears, "
        "once it is verified.")

#: The hands' schemas (Pipecat standard function schema fields).
HAND_TOOLS: list[dict[str, Any]] = [
    {
        "name": "press_keys",
        "description": (
            "Your hand: press one key or one shortcut in one window, e.g. ['k'] to pause or play a YouTube "
            "video, ['space'], ['f5'], ['alt', 'left']. The window is brought to the front first if it is "
            "not; the keys are sent only while it is in front and ready. One step only. Its report comes back "
            "to you (a key has no verdict). Instant: call it with no words before it, the call alone; then say "
            "what its report shows."
        ),
        "properties": {
            "window": _WINDOW,
            "keys": {"type": "array", "items": {"type": "string"},
                     "description": "One key, or a shortcut's keys in order (pyautogui names)."},
            "times": {"type": "integer", "description": "Press it this many times (default 1)."},
        },
        "required": ["window", "keys"],
    },
    {
        "name": "media_key",
        "description": (
            "Your hand: a media or volume key for the whole PC. playpause, nexttrack, prevtrack and stop act "
            "on the media session the desktop block marks as the one media keys act on; volumeup / "
            "volumedown move the volume 2% per press. Verified from the session's status or the volume." + _NOW
        ),
        "properties": {
            "key": {"type": "string", "enum": list(MEDIA_KEYS)},
            "times": {"type": "integer", "description": "Presses (default 1); e.g. 5 for 'a bit' of volume."},
            "say_when_done": _DONE_LINE,
        },
        "required": ["key", "say_when_done"],
    },
    {
        "name": "scroll",
        "description": (
            "Your hand: scroll one window's content up or down (brought to the front first if it is not). "
            "It scrolls the window's scrollable page or list itself, wherever the mouse is, and reads the "
            "scroll position back: verified only if the content really moved. The same for 'again' or "
            "'more'." + _NOW
        ),
        "properties": {
            "window": _WINDOW,
            "direction": {"type": "string", "enum": ["up", "down"]},
            "amount": {"type": "integer", "description": "Wheel notches (default 5; about a screenful is 10)."},
            "say_when_done": _DONE_LINE,
        },
        "required": ["window", "direction", "say_when_done"],
    },
    {
        "name": "window",
        "description": (
            "Your hand: focus (bring to the front), minimize, maximize, restore or close one whole window. "
            "A window is not a tab: this never changes which tab a browser shows (that is the tab hand). The "
            "report says whether the window really reached that state. close is what its X button does." + _NOW
        ),
        "properties": {
            "window": _WINDOW,
            "action": {"type": "string", "enum": ["focus", "minimize", "maximize", "restore", "close"]},
            "say_when_done": _DONE_LINE,
        },
        "required": ["window", "action", "say_when_done"],
    },
    {
        "name": "tab",
        "description": (
            "Your hand: show one tab of a window's tab strip (a browser tab), from the tabs line of the "
            "desktop block: its number there. Verified: the tab reports itself selected. Only a tab on that "
            "line; which tab holds what inside its page is the thinker's." + _NOW
        ),
        "properties": {
            "window": _WINDOW,
            "tab": {"type": "integer", "description": "The tab's number on the tabs line (from 1, left to right)."},
            "say_when_done": _DONE_LINE,
        },
        "required": ["window", "tab", "say_when_done"],
    },
    {
        "name": "open_url",
        "description": (
            "Your hand: open a web address, in the app they named (e.g. 'Google Chrome') or else their "
            "default browser. A browser that is open opens it in a new tab of its own, so never press a "
            "new-tab key first. Verified once the page is at the address and has its title (seconds), so a "
            "few words first."
        ),
        "properties": {
            "url": {"type": "string", "description": "The full address, e.g. https://www.youtube.com"},
            "app": {"type": "string", "description": "Optional: the app to open it in, as the Start menu names it."},
            "say_when_done": _DONE_LINE,
        },
        "required": ["url", "say_when_done"],
    },
    {
        "name": "open_app",
        "description": ("Your hand: open (or bring up) an app by its Start menu name, e.g. 'Spotify'. Verified once "
                        "its window is in front (a second or more), so a few words first."),
        "properties": {"name": {"type": "string", "description": "The app's name as they said it."},
                       "say_when_done": _DONE_LINE},
        "required": ["name", "say_when_done"],
    },
]
HAND_NAMES = tuple(t["name"] for t in HAND_TOOLS)


@dataclass
class Snapshot:
    """One desktop summary for the mouth: the text, the windows and tabs it names, and its cost."""

    text: str = ""
    hwnds: set[int] = field(default_factory=set)
    foreground: int | None = None
    cursor: tuple[int, int] | None = None
    bounds: dict[int, tuple[int, int, int, int]] = field(default_factory=dict)
    #: The tabs shown: ``{hwnd: [name of tab 1, name of tab 2, ...]}``.
    tabs: dict[int, list[str]] = field(default_factory=dict)
    tabs_ms: float | None = None
    at: float = 0.0
    ms: float = 0.0
    error: str | None = None


def media_line(facts: dict[str, Any]) -> str:
    """The media sessions as one line of facts (``""`` when there are none)."""
    parts = []
    for s in facts.get("media") or []:
        if not isinstance(s, dict):
            continue
        what = " by ".join(x for x in (f"\"{s.get('title')}\"" if s.get("title") else "", s.get("artist") or "") if x)
        app = str(s.get("app_id") or "?").split("!")[-1]
        parts.append(f"{app}{' ' + what if what else ''} {s.get('status') or 'unknown'}"
                     + (" (media keys act on this one)" if s.get("current") else ""))
    return ("media: " + "; ".join(parts)) if parts else ""


@dataclass
class ActionRecord:
    """One desktop action done in this conversation, and what was verified about it (content: RAM only)."""

    at: float
    who: str        # "your hand" | "the thinker"
    what: str       # the action, e.g. "open_url https://www.youtube.com in Google Chrome"
    result: str     # its report
    verified: bool | None
    ok: bool

    def line(self, limit: int = 320) -> str:
        mark = "verified" if self.verified is True else ("failed" if not self.ok else "not verified")
        text = f"{datetime.fromtimestamp(self.at):%H:%M:%S} {self.who}: {self.what} -> {mark}: {self.result}"
        return text if len(text) <= limit else text[: limit - 1] + "…"


class ActionLog:
    """What was done on the desktop in this conversation, newest last (the hands' and the thinker's).

    Shared context, not a transcript: the mouth sees it in its desktop block, and the
    thinker is handed it with every voice request, so "the tab we just opened" has a
    referent. In RAM only (it holds titles and addresses: content), never in the
    voice log.
    """

    def __init__(self, keep: int = 12, max_age_s: float = 900.0) -> None:
        self._items: deque[ActionRecord] = deque(maxlen=keep)
        self._lock = threading.Lock()
        self.max_age_s = max_age_s
        #: Bumped on every action (what was read of the desktop before it may be out of date).
        self.version = 0

    def add(self, who: str, what: str, result: str, *, ok: bool, verified: bool | None) -> None:
        with self._lock:
            self._items.append(ActionRecord(time.time(), who, what, " ".join(str(result).split()), verified, ok))
            self.version += 1

    def lines(self, count: int = 6, limit: int = 320) -> list[str]:
        now = time.time()
        with self._lock:
            items = [r for r in self._items if now - r.at <= self.max_age_s][-count:]
        return [r.line(limit) for r in items]

    def text(self, count: int = 6) -> str:
        lines = self.lines(count)
        return "\n".join(lines)


class Hands:
    """The mouth's desktop tools over a backend (the brain's), with the summary they act on.

    Args:
        backend: A :class:`yuki.agent.tools.Backend` (the real desktop, or a stub in tests).
        log: The voice log (content-free ``event``), or ``None``.
        actions: The conversation's shared :class:`ActionLog` (a new one when ``None``).
    """

    def __init__(self, backend: Any, log: Any = None, actions: ActionLog | None = None) -> None:
        from yuki.agent.tools import Dispatcher

        self.backend = backend
        self.log = log
        self.actions = actions if actions is not None else ActionLog()
        self._dispatcher = Dispatcher(backend)
        self._lock = threading.Lock()
        self.last = Snapshot()
        #: Hands in flight (the narrator waits for them, like the mouth's other quick tools).
        self.busy = 0
        self.bridge: Any = None
        #: ``(sent_at)``: a hand's action went out (the turn log's end of speech -> action).
        self.on_action: Any = None
        #: ``{hwnd: (window title, action log version, strip, read at)}``: tabs read ahead.
        self._tabs: dict[int, tuple[str, int, Any, float]] = {}
        self._prefetching = False

    # -- the desktop summary ------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """Capture the summary now (blocking, a few ms; call it off the event loop). Never raises."""
        started = time.perf_counter()
        snap = Snapshot(at=time.monotonic())
        try:
            text, payload, _ms = self._dispatcher.overview_text()
            windows = [w for w in (payload.get("windows") or []) if isinstance(w, dict)]
            snap.hwnds = {int(w["hwnd"]) for w in windows if w.get("hwnd") is not None}
            snap.bounds = {int(w["hwnd"]): tuple(w.get("bounds") or (0, 0, 0, 0)) for w in windows
                           if w.get("hwnd") is not None and not w.get("is_minimized")}
            snap.foreground = int(payload["foreground_hwnd"]) if payload.get("foreground_hwnd") else None
            cursor = payload.get("cursor")
            snap.cursor = (int(cursor[0]), int(cursor[1])) if cursor else None
            parts = [text]
            try:
                media = media_line(self._dispatcher.activity_facts())
            except Exception:  # noqa: BLE001 - the windows are the part that matters
                media = ""
            if media:
                parts.append(media)
            titles = {int(w["hwnd"]): str(w.get("title") or "") for w in windows if w.get("hwnd") is not None}
            tabs = self._tabs_line(snap, titles.get(snap.foreground or 0, ""))
            if tabs:
                parts.append(tabs)
            done = self.actions.lines(_MOUTH_ACTION_LINES, 240)
            if done:
                parts.append("done on the desktop in this conversation (newest last):\n" + "\n".join(done))
            snap.text = "\n".join(parts)
        except Exception as exc:  # noqa: BLE001 - the mouth goes without, and its hands refuse
            snap.error = f"{type(exc).__name__}: {exc}"
        snap.ms = (time.perf_counter() - started) * 1000.0
        with self._lock:
            if not snap.error:
                self.last = snap
        return snap

    def _tabs_line(self, snap: Snapshot, title: str) -> str:
        """The foreground window's tabs, recorded on ``snap``: read ahead when still good, else now (bounded)."""
        read = getattr(self.backend, "read_tabs", None)
        if not callable(read) or snap.foreground is None or snap.foreground not in snap.bounds:
            return ""
        from yuki.perception.tabs import tabs_line

        started = time.perf_counter()
        with self._lock:
            ahead = self._tabs.get(snap.foreground)
        if (ahead is not None and ahead[0] == title and ahead[1] == self.actions.version
                and time.monotonic() - ahead[3] < _TABS_FRESH_S):
            strip = ahead[2]
        else:
            try:
                strip = read(snap.foreground, budget_s=_TABS_BUDGET_S)
            except Exception:  # noqa: BLE001
                return ""
            self._keep_tabs(snap.foreground, title, strip)
        snap.tabs_ms = round((time.perf_counter() - started) * 1000.0, 1)
        if not getattr(strip, "tabs", None):
            return ""
        snap.tabs[snap.foreground] = [t.name for t in strip.tabs]
        return tabs_line(strip)

    def _keep_tabs(self, hwnd: int, title: str, strip: Any) -> None:
        if getattr(strip, "error", None) is None:
            with self._lock:
                self._tabs = {hwnd: (title, self.actions.version, strip, time.monotonic())}

    def prefetch(self) -> None:
        """Read the foreground window's tabs ahead, off every thread that matters (the user started talking).

        Returns at once; one read at a time. The summary for the reply then takes them from
        here instead of reading them on the reply's path (a UIA walk of Chrome's window: tens
        of milliseconds).
        """
        read = getattr(self.backend, "read_tabs", None)
        if not callable(read) or self._prefetching:
            return
        self._prefetching = True

        def run() -> None:
            try:
                _text, payload, _ms = self._dispatcher.overview_text()
                front = int(payload["foreground_hwnd"]) if payload.get("foreground_hwnd") else None
                if front is None:
                    return
                title = next((str(w.get("title") or "") for w in payload.get("windows") or []
                              if isinstance(w, dict) and w.get("hwnd") == front), "")
                self._keep_tabs(front, title, read(front, budget_s=0.4))
            except Exception:  # noqa: BLE001 - only a head start
                pass
            finally:
                self._prefetching = False

        threading.Thread(target=run, name="yuki-voice-tabs", daemon=True).start()

    async def for_call(self, wait_ms: float) -> tuple[Snapshot | None, str]:
        """A fresh summary for one mouth call, waiting at most ``wait_ms``; else the last one (``how``)."""
        task = asyncio.ensure_future(asyncio.to_thread(self.snapshot))
        try:
            snap = await asyncio.wait_for(asyncio.shield(task), timeout=max(wait_ms, 1.0) / 1000.0)
            return (snap, "fresh") if not snap.error else (self._last_or_none(), "previous")
        except asyncio.TimeoutError:
            return self._last_or_none(), "previous"

    def _last_or_none(self) -> Snapshot | None:
        with self._lock:
            return self.last if self.last.text else None

    # -- the tools (Pipecat handlers, on the voice loop) --------------------------

    async def handle(self, params: Any) -> None:
        """One hand: run it off the loop, then its report goes back as the call's result."""
        name = params.function_name
        args = params.arguments if isinstance(params.arguments, dict) else {}
        started = time.monotonic()
        self.busy += 1
        if self.bridge is not None:
            self.bridge.busy_tools += 1
        try:
            ok, report, facts = await asyncio.to_thread(self._run, name, args)
        except Exception as exc:  # noqa: BLE001 - a hand's crash is a failed hand
            ok, report, facts = False, f"{type(exc).__name__}: {exc}", {}
        finally:
            self.busy = max(0, self.busy - 1)
            if self.bridge is not None:
                self.bridge.busy_tools = max(0, self.bridge.busy_tools - 1)
                if self.bridge.gate is not None:
                    self.bridge.gate.poke()
        ms = round((time.monotonic() - started) * 1000)
        if self.on_action is not None and facts.get("sent_at") is not None:
            self.on_action(facts["sent_at"])
        line = str(args.get("say_when_done") or "").strip()
        # The done line goes out at once, with no second model call, only when the hand's own
        # report verified the effect (a window's state, the selected tab, the scroll position,
        # the media status or volume, the page's address and title). Otherwise the mouth reads
        # the report and says what happened (or hands it over).
        verified = facts.get("verified")
        confirmed = ok and verified is True and name != "press_keys"
        speak = bool(line) and confirmed
        self.actions.add("your hand", _what(name, args), report[:400], ok=ok, verified=verified)
        if self.log is not None:
            self.log.event("hands", op=name, ok=ok, ms=ms, report_chars=len(report), call=params.tool_call_id,
                           done_line=speak, **{k: v for k, v in facts.items()
                                               if isinstance(v, (int, float, bool)) or v is None})
        if speak:
            from pipecat.frames.frames import FunctionCallResultProperties, TTSSpeakFrame

            await params.llm.push_frame(TTSSpeakFrame(text=line))
            await params.result_callback({"done": report[:700], "said": line, "how_to_handle_it": _SAID_NOTE},
                                         properties=FunctionCallResultProperties(run_llm=False))
            return
        if name == "press_keys" and ok:
            note, key = _KEYS_NOTE, "report"
        elif ok:
            note, key = _UNCONFIRMED_NOTE, "not_verified"
        else:
            note, key = _FAILED_NOTE, "failed"
        await params.result_callback({key: report[:700], "how_to_say_it": note})

    def _window(self, args: dict[str, Any]) -> int:
        """The hwnd a hand names, refused unless the mouth was shown that window."""
        try:
            hwnd = int(args.get("window"))
        except (TypeError, ValueError):
            raise _Refused("give the window's hwnd from the desktop block") from None
        with self._lock:
            known = set(self.last.hwnds)
        if hwnd not in known:
            raise _Refused(f"window {hwnd} is not in the desktop block you were shown; nothing was done")
        return hwnd

    def _front(self, hwnd: int) -> str:
        """Bring ``hwnd`` to the front unless it is there (an empty string: it is in front now)."""
        foreground = self._foreground()
        if foreground == hwnd:
            return ""
        result = self.backend.focus_window(hwnd)
        if not getattr(result, "ok", False):
            raise _Refused(f"could not bring window {hwnd} to the front: {getattr(result, 'summary', '')}")
        return "brought it to the front; "

    def _foreground(self) -> int | None:
        try:
            _text, payload, _ms = self._dispatcher.overview_text()
            return int(payload["foreground_hwnd"]) if payload.get("foreground_hwnd") else None
        except Exception:  # noqa: BLE001
            return None

    def _run(self, name: str, args: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:  # noqa: C901
        """Blocking: validate, act through the backend, verify, return ``(ok, report, facts)``."""
        facts: dict[str, Any] = {"sent_at": None, "verified": None}
        try:
            if name == "press_keys":
                hwnd = self._window(args)
                keys = [str(k) for k in (args.get("keys") or []) if str(k).strip()]
                if not keys:
                    raise _Refused("no keys given")
                times = max(1, min(int(args.get("times") or 1), 20))
                lead = self._front(hwnd)
                before = self._media_status()
                facts["sent_at"] = time.monotonic()
                if len(keys) == 1:
                    result = self.backend.press(keys[0], times=times, expect_hwnd=hwnd)
                else:
                    result = self.backend.hotkey(*keys, expect_hwnd=hwnd)
                ok, report, facts = self._report(result, lead, facts)
                if ok and before is not None:
                    # A fact for the mouth to read (a key may be meant for anything, so no verdict).
                    report += "; " + self._media_after(before)[1]
                return ok, report, facts
            if name == "media_key":
                key = str(args.get("key") or "")
                if key not in MEDIA_KEYS:
                    raise _Refused(f"unknown media key {key!r} ({', '.join(MEDIA_KEYS)})")
                times = max(1, min(int(args.get("times") or 1), 50))
                before = self._media_status() if key in _TRANSPORT_KEYS else None
                volume = self._volume() if key in _VOLUME_KEYS else None
                facts["sent_at"] = time.monotonic()
                result = self.backend.press(key, times=times)
                ok, report, facts = self._report(result, "", facts)
                if ok and before is not None:
                    changed, words = self._media_after(before)
                    report += "; " + words
                    facts["verified"] = changed
                elif ok and volume is not None:
                    changed, words = self._volume_after(volume)
                    report += "; " + words
                    facts["verified"] = changed
                return ok, report, facts
            if name == "scroll":
                hwnd = self._window(args)
                direction = str(args.get("direction") or "down")
                notches = max(1, min(int(args.get("amount") or 5), 50))
                lead = self._front(hwnd)
                x, y = self._scroll_point(hwnd)
                facts["sent_at"] = time.monotonic()
                result = self.backend.scroll(x, y, dy=notches if direction == "up" else -notches, expect_hwnd=hwnd)
                ok, report, facts = self._report(result, lead, facts)
                facts["verified"] = _detail(result, "verified")
                return ok, report, facts
            if name == "window":
                hwnd = self._window(args)
                action = str(args.get("action") or "")
                act = getattr(self.backend, "window_action", None)
                if not callable(act):
                    raise _Refused("this desktop has no window hand")
                facts["sent_at"] = time.monotonic()
                ok, report, facts = self._report(act(hwnd, action), "", facts)
                # window_action's ok is the new state observed (focus: the window in front).
                facts["verified"] = ok
                return ok, report, facts
            if name == "tab":
                return self._tab(args, facts)
            if name == "open_url":
                return self._open_url(args, facts)
            if name == "open_app":
                query = str(args.get("name") or "").strip()
                if not query:
                    raise _Refused("no app name given")
                facts["sent_at"] = time.monotonic()
                ok, report, facts = self._report(self.backend.launch_app(query), "", facts)
                # launch_app's ok is its window found and in front, ready for input.
                facts["verified"] = ok
                return ok, report, facts
            raise _Refused(f"no hand called {name!r}")
        except _Refused as exc:
            return False, str(exc), facts

    def _tab(self, args: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        hwnd = self._window(args)
        select = getattr(self.backend, "select_tab", None)
        if not callable(select):
            raise _Refused("this desktop has no tab hand")
        try:
            index = int(args.get("tab"))
        except (TypeError, ValueError):
            raise _Refused("give the tab's number from the tabs line") from None
        with self._lock:
            shown = list(self.last.tabs.get(hwnd) or [])
        if not 1 <= index <= len(shown):
            raise _Refused(f"tab {index} of window {hwnd} is not on the tabs line you were shown; nothing was done")
        facts["sent_at"] = time.monotonic()
        result = select(hwnd, index, expect_name=shown[index - 1])
        ok, report, facts = self._report(result, "", facts)
        facts["verified"] = bool(_detail(result, "verified")) if ok else False
        if ok and self._foreground() != hwnd:
            report = self._front(hwnd) + report
        return ok, report, facts

    def _open_url(self, args: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        url = str(args.get("url") or "").strip()
        if not url:
            raise _Refused("no address given")
        app = str(args.get("app") or "").strip() or None
        facts["sent_at"] = time.monotonic()
        result = self.backend.open_url(url, app=app) if app else self.backend.open_url(url)
        ok, report, facts = self._report(result, "", facts)
        details = getattr(result, "details", None) or {}
        page = details.get("page") if isinstance(details.get("page"), dict) else {}
        hwnd = details.get("hwnd")
        if not ok or details.get("page_verified") is not True:
            facts["verified"] = False if details.get("page_verified") is False else None
            return ok, report, facts
        # At the address is not loaded: a page gets its title once it has arrived.
        title = str(page.get("title") or "")
        titled = bool(title) and title != str(page.get("url") or "")
        waited = time.monotonic()
        deadline = waited + _TITLE_WAIT_S
        can_read = callable(getattr(self.backend, "read_tabs", None))
        while True:
            tab = self._selected_tab(hwnd)
            if not titled and tab is not None and tab[1] and not _is_address(tab[1]):
                titled, title = True, tab[1]
            if titled or not can_read or time.monotonic() >= deadline:
                break
            time.sleep(_POLL_S * 2)
        facts["title_wait_ms"] = round((time.monotonic() - waited) * 1000)
        facts["verified"] = titled
        # The verified facts first (they are what the action log and the thinker need), then the
        # opening's own report.
        where = f"it is tab {tab[0]} of {tab[2]} in window {hwnd}, \"{tab[1]}\", now showing; " if tab else ""
        state = (f"the page has its title (\"{title}\"): loaded" if titled
                 else f"after {_TITLE_WAIT_S:g} s the page still has no title: not loaded yet")
        return ok, f"opened {url}: {where}{state}. ({report})", facts

    def _selected_tab(self, hwnd: Any) -> tuple[int, str, int] | None:
        """``(place, name, count)`` of the tab ``hwnd`` shows, or ``None``."""
        read = getattr(self.backend, "read_tabs", None)
        if not callable(read) or not hwnd:
            return None
        try:
            strip = read(int(hwnd), budget_s=0.3)
        except Exception:  # noqa: BLE001
            return None
        tab = getattr(strip, "selected", None)
        return (tab.index, tab.name, len(strip.tabs)) if tab is not None else None

    def _media_status(self) -> tuple[str, str, str] | None:
        """``(app, status, title)`` of the media session the media keys act on, or ``None``."""
        try:
            sessions = self._dispatcher.activity_facts().get("media") or []
        except Exception:  # noqa: BLE001
            return None
        current = next((m for m in sessions if isinstance(m, dict) and m.get("current")), None)
        if current is None:
            return None
        return (str(current.get("app_id") or "?").split("!")[-1], str(current.get("status") or "unknown"),
                str(current.get("title") or ""))

    def _media_after(self, before: tuple[str, ...]) -> tuple[bool, str]:
        """What the media session says after a transport key: watched until it changes, at most
        :data:`_WATCH_S` (a condition poll). A measured fact for the report."""
        deadline = time.monotonic() + _WATCH_S
        after = before
        while time.monotonic() < deadline:
            now = self._media_status()
            if now is not None and now != before:
                after = now
                break
            time.sleep(_POLL_S)
        if after == before:
            return False, f"media: {before[0]} still {before[1]} after {_WATCH_S:g} s"
        track = f" \"{after[2]}\"" if len(after) > 2 and after[2] and after[2] != before[2] else ""
        return True, f"media: {after[0]} now {after[1]}{track} (was {before[0]} {before[1]})"

    def _volume(self) -> dict[str, Any] | None:
        read = getattr(self.backend, "master_volume", None)
        try:
            return read() if callable(read) else None
        except Exception:  # noqa: BLE001
            return None

    def _volume_after(self, before: dict[str, Any]) -> tuple[bool, str]:
        deadline = time.monotonic() + _WATCH_S
        after = before
        while time.monotonic() < deadline:
            now = self._volume()
            if now is not None and now != before:
                after = now
                break
            time.sleep(_POLL_S)
        if after == before:
            return False, f"the volume is still {before.get('level')}%{' (muted)' if before.get('muted') else ''}"
        return True, (f"the volume is now {after.get('level')}%{' (muted)' if after.get('muted') else ''} "
                      f"(was {before.get('level')}%{' muted' if before.get('muted') else ''})")

    def _scroll_point(self, hwnd: int) -> tuple[int, int]:
        """The window's centre: its content, whatever the cursor is over (plumbing: where to look for it)."""
        with self._lock:
            bounds = self.last.bounds.get(hwnd)
        if not bounds:
            raise _Refused(f"window {hwnd} has no position on screen (minimized?); bring it up first")
        left, top, right, bottom = bounds
        return (left + right) // 2, (top + bottom) // 2

    @staticmethod
    def _report(result: Any, lead: str, facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        ok = bool(getattr(result, "ok", False))
        facts["action_ms"] = round(float(getattr(result, "elapsed_ms", 0.0) or 0.0))
        return ok, lead + str(getattr(result, "summary", "")), facts


def _detail(result: Any, key: str) -> Any:
    details = getattr(result, "details", None)
    return details.get(key) if isinstance(details, dict) else None


def _is_address(text: str) -> bool:
    try:
        from yuki.perception.tree import is_address

        return bool(is_address(text))
    except Exception:  # noqa: BLE001
        return False


def _what(name: str, args: dict[str, Any]) -> str:
    """One line naming a hand's action from its arguments (for the shared action log)."""
    parts = [name]
    for key in ("window", "tab", "action", "direction", "amount", "key", "keys", "times", "url", "app", "name"):
        if args.get(key) not in (None, "", []):
            parts.append(f"{key}={args[key]}")
    return " ".join(str(p) for p in parts)


class _Refused(Exception):
    """A hand refused before doing anything (the message is the report)."""


def hands_backend(runtime: Any) -> Any:
    """The desktop backend the brain's worker acts through (the real one, or a test's stub).

    ``None`` when the runtime has no worker with a backend (a stand-in brain in a test):
    then the mouth has no hands, rather than hands on the real desktop.
    """
    worker = getattr(runtime, "worker", None)
    agent = getattr(worker, "agent", None)
    dispatcher = getattr(agent, "dispatcher", None)
    return getattr(dispatcher, "backend", None)
