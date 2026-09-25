"""The talker's hands: single desktop steps the mouth does itself (docs/VOICE.md §5.6).

gradient-bang's voice LLM calls its quick tools itself and hands only
multi-step work to task agents (docs/research/voice-parallel.md §1). Yuki's
mouth gets the same: a few one-step desktop tools (a key or shortcut in one
window, a media or volume key, scrolling, focusing / minimizing / maximizing /
restoring / closing a window, opening a URL or an app), each backed by the
existing :mod:`yuki.actions` code through the brain's own backend, with the
brain's guards: keys and scrolling go to the named window only while it is in
front (``expect_hwnd``) and ready for input; a window action verifies the new
state; and only a window the mouth was shown in this turn's desktop summary
can be acted on (a hand never guesses an hwnd).

The mouth calls a hand in the same reply as its spoken acknowledgement, so the
words and the action happen together (Pipecat runs the call while TTS streams).
The hand's own report comes back as the call's result and the mouth says it in
a few words. Whether a request is one step it can see, or needs the thinker,
is the mouth's call from the tool descriptions and its prompt (never a word
list); a failed hand says so in its result, and the mouth then hands the
request to the thinker with that report.

The desktop summary (:meth:`Hands.snapshot`) is the brain's own overview
(``format_overview``) plus one line of media sessions, captured off the event
loop for every mouth call and put in the volatile block after every cache
breakpoint (:func:`yuki.voice.mouth.volatile_block`). Measured sizes and times
go to the voice log (content-free).
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any

#: Media and volume keys (pyautogui names, as :func:`yuki.actions.input.normalize_key` takes them).
MEDIA_KEYS = ("playpause", "nexttrack", "prevtrack", "stop", "volumeup", "volumedown", "volumemute")
#: The media keys whose effect the media session shows (its status), watched after sending.
_TRANSPORT_KEYS = ("playpause", "stop")
_MEDIA_WATCH_S = 0.8
_MEDIA_POLL_S = 0.1

_WINDOW = {
    "type": "integer",
    "description": "The window's hwnd, the [number] on its line in the desktop block of the latest user message.",
}
_REPORT_NOTE = (
    "Your hand's own report. Say what it did in a few spoken words ('done, it's minimized'), nothing "
    "else: no question, no offer. If the report shows it did not take effect, treat it as failed."
)
_SAID_NOTE = "It worked and your done line was said. Do not say it again."
_UNCONFIRMED_NOTE = (
    "The keys went out, but the report does not show the effect happened. Say so in a few words and "
    "hand it to the thinker in this same reply: tell_yuki with the user's request and this report as context."
)
#: Every hand's done line: said the moment its report confirms the step, with no second model call.
_DONE_LINE = {
    "type": "string",
    "description": (
        "The few words to say the moment the report confirms it worked, e.g. \"Done, it's minimized.\". "
        "Said only then; if it did not work you get the report instead."
    ),
}
_FAILED_NOTE = (
    "It did not work. Say so in a few words and hand it to the thinker in this same reply: tell_yuki "
    "with the user's request as they asked it and this report as context."
)

#: The hands' schemas (Pipecat standard function schema fields).
HAND_TOOLS: list[dict[str, Any]] = [
    {
        "name": "press_keys",
        "description": (
            "Your hand: press one key or one shortcut in one window, e.g. ['k'] to pause or play a YouTube "
            "video, ['space'], ['ctrl', 'w'], ['f5'], ['alt', 'left']. The window is brought to the front "
            "first if it is not; the keys are sent only while it is in front and ready. One step only."
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
            "on the media session the desktop block marks as the one media keys act on. volumeup / "
            "volumedown move the volume 2% per press."
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
            "Your hand: scroll one window up or down (brought to the front first if it is not), at the "
            "cursor when the cursor is over it, else at its centre."
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
            "Your hand: focus (bring to the front), minimize, maximize, restore or close one window. The "
            "report says whether the window really reached that state. close is what its X button does."
        ),
        "properties": {
            "window": _WINDOW,
            "action": {"type": "string", "enum": ["focus", "minimize", "maximize", "restore", "close"]},
            "say_when_done": _DONE_LINE,
        },
        "required": ["window", "action", "say_when_done"],
    },
    {
        "name": "open_url",
        "description": (
            "Your hand: open a web address, in the app they named (e.g. 'Google Chrome') or else their "
            "default browser. Returns once the page is showing."
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
        "description": "Your hand: open (or bring up) an app by its Start menu name, e.g. 'Spotify'.",
        "properties": {"name": {"type": "string", "description": "The app's name as they said it."},
                       "say_when_done": _DONE_LINE},
        "required": ["name", "say_when_done"],
    },
]
HAND_NAMES = tuple(t["name"] for t in HAND_TOOLS)


@dataclass
class Snapshot:
    """One desktop summary for the mouth: the text, the windows it names, and its cost."""

    text: str = ""
    hwnds: set[int] = field(default_factory=set)
    foreground: int | None = None
    cursor: tuple[int, int] | None = None
    bounds: dict[int, tuple[int, int, int, int]] = field(default_factory=dict)
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


class Hands:
    """The mouth's desktop tools over a backend (the brain's), with the summary they act on.

    Args:
        backend: A :class:`yuki.agent.tools.Backend` (the real desktop, or a stub in tests).
        log: The voice log (content-free ``event``), or ``None``.
    """

    def __init__(self, backend: Any, log: Any = None) -> None:
        from yuki.agent.tools import Dispatcher

        self.backend = backend
        self.log = log
        self._dispatcher = Dispatcher(backend)
        self._lock = threading.Lock()
        self.last = Snapshot()
        #: Hands in flight (the narrator waits for them, like the mouth's other quick tools).
        self.busy = 0
        self.bridge: Any = None

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
            try:
                media = media_line(self._dispatcher.activity_facts())
            except Exception:  # noqa: BLE001 - the windows are the part that matters
                media = ""
            snap.text = text + (f"\n{media}" if media else "")
        except Exception as exc:  # noqa: BLE001 - the mouth goes without, and its hands refuse
            snap.error = f"{type(exc).__name__}: {exc}"
        snap.ms = (time.perf_counter() - started) * 1000.0
        with self._lock:
            if not snap.error:
                self.last = snap
        return snap

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
        line = str(args.get("say_when_done") or "").strip()
        # The done line goes out at once, with no second model call, when the hand's own report
        # confirms the step: the action succeeded and, where the effect can be measured (a
        # window's state, a page at the address, a media session's status), it was. Otherwise
        # the mouth reads the report and says what happened (or hands it over).
        confirmed = ok and facts.get("verified") is not False
        speak = bool(line) and confirmed
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
        note = _REPORT_NOTE if confirmed else (_UNCONFIRMED_NOTE if ok else _FAILED_NOTE)
        body = {"done" if confirmed else "failed": report[:700], "how_to_say_it": note}
        await params.result_callback(body)

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

    def _run(self, name: str, args: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """Blocking: validate, act through the backend, return ``(ok, report, facts)``."""
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
                facts["sent_at"] = time.monotonic()
                result = self.backend.press(key, times=times)
                ok, report, facts = self._report(result, "", facts)
                if ok and before is not None:
                    changed, words = self._media_after(before)
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
                return self._report(result, lead, facts)
            if name == "window":
                hwnd = self._window(args)
                action = str(args.get("action") or "")
                act = getattr(self.backend, "window_action", None)
                if not callable(act):
                    raise _Refused("this desktop has no window hand")
                facts["sent_at"] = time.monotonic()
                return self._report(act(hwnd, action), "", facts)
            if name == "open_url":
                url = str(args.get("url") or "").strip()
                if not url:
                    raise _Refused("no address given")
                app = str(args.get("app") or "").strip() or None
                facts["sent_at"] = time.monotonic()
                result = self.backend.open_url(url, app=app) if app else self.backend.open_url(url)
                return self._report(result, "", facts)
            if name == "open_app":
                query = str(args.get("name") or "").strip()
                if not query:
                    raise _Refused("no app name given")
                facts["sent_at"] = time.monotonic()
                return self._report(self.backend.launch_app(query), "", facts)
            raise _Refused(f"no hand called {name!r}")
        except _Refused as exc:
            return False, str(exc), facts

    def _media_status(self) -> tuple[str, str] | None:
        """``(app, status)`` of the media session the media keys act on, or ``None``."""
        try:
            sessions = self._dispatcher.activity_facts().get("media") or []
        except Exception:  # noqa: BLE001
            return None
        current = next((m for m in sessions if isinstance(m, dict) and m.get("current")), None)
        if current is None:
            return None
        return str(current.get("app_id") or "?").split("!")[-1], str(current.get("status") or "unknown")

    def _media_after(self, before: tuple[str, str]) -> tuple[bool, str]:
        """What the media session says after a transport key: watched until it changes, at most
        :data:`_MEDIA_WATCH_S` (a condition poll). A measured fact for the report, never a verdict."""
        deadline = time.monotonic() + _MEDIA_WATCH_S
        after = before
        while time.monotonic() < deadline:
            now = self._media_status()
            if now is not None and now != before:
                after = now
                break
            time.sleep(_MEDIA_POLL_S)
        if after == before:
            return False, f"media: {before[0]} still {before[1]} after {_MEDIA_WATCH_S:g} s"
        return True, f"media: {after[0]} now {after[1]} (was {before[0]} {before[1]})"

    def _scroll_point(self, hwnd: int) -> tuple[int, int]:
        """At the cursor when it is over the window, else the window's centre (plumbing: where the wheel goes)."""
        with self._lock:
            bounds = self.last.bounds.get(hwnd)
            cursor = self.last.cursor
        if not bounds:
            raise _Refused(f"window {hwnd} has no position on screen (minimized?); bring it up first")
        left, top, right, bottom = bounds
        if cursor is not None and left <= cursor[0] < right and top <= cursor[1] < bottom:
            return cursor
        return (left + right) // 2, (top + bottom) // 2

    @staticmethod
    def _report(result: Any, lead: str, facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        ok = bool(getattr(result, "ok", False))
        facts["action_ms"] = round(float(getattr(result, "elapsed_ms", 0.0) or 0.0))
        return ok, lead + str(getattr(result, "summary", "")), facts


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
