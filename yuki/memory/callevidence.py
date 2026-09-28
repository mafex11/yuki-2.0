"""Whether the user has really joined a call: judged from the screen, never from the microphone alone.

Live 2026-09-28 09:01-09:11 the Zen browser held the microphone (a tab with microphone permission) while it
played YouTube, and the call worker took that for a call: voice went into call mode and refused the user's
own commands. Earlier, a Google Meet pre-join lobby (which holds the microphone for its preview) counted as a
meeting too. The user: "call detection must use the screen, which Yuki already captures ... a mic held by an
app is only a hint, never proof."

So a call needs **evidence from the screen**. The watcher (:mod:`yuki.memory.watcher`) hands every window it
extracts to :meth:`CallEvidence.seen`: the app, the page host, and the names of the visible controls and
headings (:func:`yuki.memory.extract.screen_controls`, structural, capped). For an app that holds the
microphone now (the hint), the latest screen of each of its sites is judged once by a small model (Claude
Haiku, forced tool ``judge_screen``, worked examples): does it show a call the user has joined (call controls,
participants, a leave or disconnect button, a voice-connected panel) or not (a pre-join lobby, a text chat,
a video page)? A verdict is cached by the screen's content, so an unchanged window costs nothing again.

:meth:`CallEvidence.state` then says, for an app and its current microphone hold:

* ``"fresh"``: a screen taken during this hold was judged in a call, within ``fresh_s`` (default 900 s);
  seeing the same screen again renews it;
* ``"stale"``: the in-call screen was taken during this hold but longer ago than ``fresh_s`` (the user joined,
  then went full screen in a game for an hour): probably still in the call, not certain;
* ``None``: no in-call screen during this hold (mic only, a lobby, a video page): not a call.

A later screen of the same app and site judged not in a call clears that site's evidence (they left: the
Discord panel is gone, the Meet page shows "You left the meeting"); a screen of another site in the same
browser does not (a Meet call goes on while they look at YouTube in another tab).

Logs are content-free (``call_evidence_judged``: app, whether in a call, ms, cost): never a control name.
Nothing here decides from words in code: the model judges the screen.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any

#: An in-call screen stays "fresh" evidence this long after it was last seen (s); then "stale".
FRESH_S = 900.0
#: A screen taken this long before the microphone hold began still counts for it (s): the two clocks are
#: read by different processes (the ConsentStore's start time, the capture's wall clock).
HOLD_TOLERANCE_S = 5.0
#: Verdicts kept (by screen content).
VERDICT_CACHE = 256
#: Controls sent to the model at most (names; the extraction already caps them).
MAX_CONTROLS = 80
#: A site with in-call evidence is judged again at most this often (s) while its screen keeps changing (a busy
#: chat changes with every message); a site with none (joining), or known content, is answered at once.
REJUDGE_S = 60.0

JUDGE_TOOL: dict[str, Any] = {
    "name": "judge_screen",
    "description": "Say whether this window shows a voice or video call that the user has joined right now.",
    "input_schema": {
        "type": "object",
        "properties": {
            "in_call": {
                "type": "boolean",
                "description": "true only when the window shows a call the user is in now (joined): call controls "
                               "such as leave, hang up, end call or disconnect, a voice-connected panel, participant "
                               "tiles. false for a pre-join lobby or waiting screen, a call that ended, a text "
                               "chat, a video or any other page.",
            },
            "service": {
                "type": "string",
                "description": "When in_call: the product's common name (Google Meet, Discord, Zoom, Microsoft "
                               "Teams, WhatsApp, Slack). Otherwise empty.",
            },
        },
        "required": ["in_call", "service"],
    },
}

JUDGE_PROMPT = """\
You look at one window of the user's PC, given as its title, its web address host (if it is a web page) and \
the names of the controls and headings visible in it, in on-screen order. Decide whether it shows a voice or \
video call that the user has actually joined right now. The microphone being in use is not proof: a meeting's \
pre-join lobby, a browser tab that was once given the microphone, or a game also use it. Only the screen says \
whether they are in the call.

In a call: the controls of a live call (leave call, hang up, end call, disconnect, mute and camera toggles of \
the call, share screen, participants or people in the call, meeting details), or a panel saying the voice is \
connected. Not in a call: a lobby that asks them to join (join now, ask to join, ready to join?), a screen \
saying the call ended or they left, a chat or channel with messages only, a video page, an editor, a list of \
chats, settings.

Examples:
- title "Meet - abc-defg-hij", host meet.google.com, controls: "Ready to join?", "Join now", "Other ways to \
join", "Turn off microphone", "Turn off camera" -> in_call false (the pre-join lobby)
- title "Meet - abc-defg-hij", host meet.google.com, controls: "Leave call", "Turn off microphone", "Turn off \
camera", "Share screen", "Meeting details", "People", "Chat with everyone", "Rohan", "You" -> in_call true, \
Google Meet
- title "#general | Friends - Discord", host discord.com, controls: "Voice Connected", "General / Friends", \
"Disconnect", "Noise Suppression", "Mute", "Deafen", "#general", "Message #general" -> in_call true, Discord
- title "#memes | Friends - Discord", host discord.com, controls: "#general", "#memes", "Message #memes", \
"Mute", "Deafen", "User Settings" -> in_call false (a text channel; no voice-connected panel)
- title "Zoom Meeting", controls: "Mute", "Stop Video", "Participants", "Share Screen", "End" -> in_call \
true, Zoom
- title "Microsoft Teams", controls: "Join now", "Choose your video and audio options", "Computer audio" -> \
in_call false (Teams pre-join)
- title "(1558) YouTube", host youtube.com, controls: "Search", "Home", "Shorts", "Subscriptions", video \
titles -> in_call false
- title "WhatsApp", controls: "Chats", "Search", "Bro", "Res", "Elina", "Archived" -> in_call false (a chat \
list)
- title "Voice call", controls: "End call", "Mute", "Speaker", "Video" -> in_call true, WhatsApp

Answer with the judge_screen tool only."""


@dataclass
class Screen:
    key: str               # app key of the process ("zen", "discord")
    host: str | None       # page host, or None for an app window without a page
    hwnd: int
    exe: str               # the process's executable (matched to a microphone holder)
    at: float              # wall clock of the capture
    title: str
    controls: tuple[str, ...]
    digest: str


@dataclass
class Verdict:
    in_call: bool
    service: str = ""


def _digest(title: str, host: str | None, controls: Iterable[str]) -> str:
    raw = json.dumps([title, host or "", list(controls)], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


class CallEvidence:
    """The screens of the user's apps, judged for a joined call (see the module docstring). Thread-safe.

    Args:
        judge_fn: ``(screen) -> Verdict``; default: the model (Haiku, forced tool). Tests pass a stub.
        settings: :class:`yuki.config.Settings` for the model client and costs (default ``Settings()``).
        log: ``log(type, **fields)`` (content-free), or None.
        fresh_s: How long an in-call screen stays fresh evidence (``[calls] evidence_fresh_s``).
        client: A model client (tests); default ``yuki.agent.failover.make_client(settings)``.
    """

    def __init__(self, *, judge_fn: Callable[[Screen], Verdict] | None = None, settings: Any = None,
                 log: Callable[..., Any] | None = None, fresh_s: float = FRESH_S, client: Any = None,
                 model: str | None = None, rejudge_s: float = REJUDGE_S) -> None:
        self._judge_fn = judge_fn
        self._settings = settings
        self._log_fn = log
        self.fresh_s = float(fresh_s)
        self._client = client
        self._model = model
        self._lock = threading.Lock()
        #: The latest screen per (app key, host).
        self._screens: dict[tuple[str, str | None], Screen] = {}
        #: Screens whose content has no verdict applied yet, per (app key, host).
        self._pending: set[tuple[str, str | None]] = set()
        #: The verdict each (app key, host) was last given, with its digest.
        self._applied: dict[tuple[str, str | None], str] = {}
        #: In-call evidence per (app key, host): ``(last seen at, service)``.
        self._evidence: dict[tuple[str, str | None], tuple[float, str]] = {}
        #: The executable each app key was last seen running as (to match a microphone holder by path or package).
        self._exes: dict[str, str] = {}
        self._verdicts: OrderedDict[str, Verdict] = OrderedDict()
        self.rejudge_s = float(rejudge_s)
        #: When the model last judged a screen of each (app key, host) (monotonic).
        self._judged_at: dict[tuple[str, str | None], float] = {}
        self.counters = {"seen": 0, "judged": 0, "cached": 0, "errors": 0}
        self.cost_usd = 0.0

    # -- input (the watcher's worker thread) -------------------------------------------------

    def seen(self, screen: dict[str, Any]) -> None:
        """A window was extracted: keep it as its app's (and site's) latest screen. Cheap; never raises."""
        try:
            key = str(screen.get("process") or "").strip().casefold()
            if not key:
                return
            host = (str(screen.get("host") or "").strip().casefold() or None)
            controls = tuple(str(c) for c in (screen.get("controls") or [])[:MAX_CONTROLS] if str(c).strip())
            title = str(screen.get("title") or "")
            shot = Screen(key=key, host=host, hwnd=int(screen.get("hwnd") or 0), exe=str(screen.get("exe") or ""),
                          at=float(screen.get("at") or time.time()), title=title, controls=controls,
                          digest=_digest(title, host, controls))
        except Exception:  # noqa: BLE001 - a hint only
            return
        where = (shot.key, shot.host)
        with self._lock:
            self.counters["seen"] += 1
            self._screens[where] = shot
            if shot.exe:
                self._exes[shot.key] = shot.exe
            if self._applied.get(where) == shot.digest:
                # The same screen again: its verdict stands, and in-call evidence is renewed.
                if where in self._evidence:
                    self._evidence[where] = (shot.at, self._evidence[where][1])
                return
            self._pending.add(where)
            if len(self._screens) > 512:   # bounded: forget the oldest screens
                for old in sorted(self._screens, key=lambda w: self._screens[w].at)[:256]:
                    self._screens.pop(old, None)
                    self._pending.discard(old)

    # -- judging (the call worker's thread) ----------------------------------------------------

    def _matches(self, holder: Any, key: str) -> bool:
        """Whether screen app ``key`` is ``holder`` (an app key string, or a :class:`~yuki.memory.avsignals.Holder`:
        same key, same executable, or a packaged app whose install folder is its package family's)."""
        if isinstance(holder, str):
            return holder.casefold() == key
        if getattr(holder, "key", None) == key:
            return True
        exe = self._exes.get(key, "")
        try:
            parts = tuple(part.casefold() for part in PureWindowsPath(exe).parts) if exe else ()
            return bool(holder.matches(process_key=key, exe=exe, ids=parts))
        except Exception:  # noqa: BLE001
            return False

    def judge(self, holders: Iterable[Any]) -> int:
        """Judge the unjudged latest screens of these apps (the microphone holders now: keys or ``Holder``).

        Returns how many verdicts were applied. A verdict already known for the same content costs nothing.
        """
        holders = [h for h in holders if h]
        if not holders:
            return 0
        with self._lock:
            todo = [self._screens[w] for w in list(self._pending)
                    if w in self._screens and any(self._matches(h, w[0]) for h in holders)]
        applied = 0
        for shot in todo:
            where = (shot.key, shot.host)
            with self._lock:
                cached = shot.digest in self._verdicts
                # A site in a call judged moments ago keeps its evidence while its screen changes (a busy chat):
                # the newest screen waits for the next pass. Joining is never delayed (no evidence yet).
                recent = where in self._evidence and                     time.monotonic() - self._judged_at.get(where, -1e9) < self.rejudge_s
            if recent and not cached:
                continue
            verdict = self._verdict(shot)
            if verdict is None:
                continue
            with self._lock:
                latest = self._screens.get(where)
                if latest is not None and latest.digest != shot.digest:
                    continue   # a newer screen arrived meanwhile: it is judged next time
                self._pending.discard(where)
                self._applied[where] = shot.digest
                if verdict.in_call:
                    self._evidence[where] = (shot.at, verdict.service or "")
                else:
                    self._evidence.pop(where, None)
            applied += 1
        return applied

    def _verdict(self, shot: Screen) -> Verdict | None:
        with self._lock:
            known = self._verdicts.get(shot.digest)
            if known is not None:
                self._verdicts.move_to_end(shot.digest)
                self.counters["cached"] += 1
                return known
        started = time.perf_counter()
        cost = 0.0
        try:
            if self._judge_fn is not None:
                verdict = self._judge_fn(shot)
            else:
                verdict, cost = self._model_verdict(shot)
        except Exception as exc:  # noqa: BLE001 - judged again on the next pass
            with self._lock:
                self.counters["errors"] += 1
            self._log("call_evidence_error", app=shot.key, error=f"{type(exc).__name__}: {str(exc)[:200]}")
            return None
        ms = round((time.perf_counter() - started) * 1000)
        with self._lock:
            self._judged_at[(shot.key, shot.host)] = time.monotonic()
            self.counters["judged"] += 1
            self.cost_usd += cost
            self._verdicts[shot.digest] = verdict
            while len(self._verdicts) > VERDICT_CACHE:
                self._verdicts.popitem(last=False)
        self._log("call_evidence_judged", app=shot.key, page=shot.host is not None, in_call=verdict.in_call,
                  controls=len(shot.controls), ms=ms, cost_usd=round(cost, 6))
        return verdict

    def _model_verdict(self, shot: Screen) -> tuple[Verdict, float]:
        from yuki.log.requests import usage_tokens
        from yuki.memory.journal import HAIKU_MODEL

        settings = self._settings
        if settings is None:
            from yuki.config import Settings

            settings = self._settings = Settings()
        if self._client is None:
            from yuki.agent.failover import make_client

            self._client = make_client(settings)
        model = self._model or HAIKU_MODEL
        lines = [f"title: {json.dumps(shot.title, ensure_ascii=False)}",
                 f"host: {shot.host or '(an app window, no web page)'}",
                 "controls, in on-screen order: " + " / ".join(json.dumps(c, ensure_ascii=False)
                                                               for c in shot.controls) if shot.controls
                 else "controls: (none visible)"]
        response = self._client.messages.create(
            model=model, max_tokens=120, system=JUDGE_PROMPT, tools=[JUDGE_TOOL],
            tool_choice={"type": "tool", "name": JUDGE_TOOL["name"]},
            messages=[{"role": "user", "content": "\n".join(lines)}])
        block = next((b for b in response.content if getattr(b, "type", None) == "tool_use"), None)
        data = getattr(block, "input", None) or {}
        verdict = Verdict(in_call=data.get("in_call") is True,
                          service=" ".join(str(data.get("service") or "").split())[:40] if data.get("in_call") is True
                          else "")
        try:
            cost = float(settings.estimate_cost(model, usage_tokens(response.usage)) or 0.0)
        except Exception:  # noqa: BLE001
            cost = 0.0
        return verdict, cost

    # -- output ------------------------------------------------------------------------------

    def state(self, holder: Any, mic_since: float, now: float | None = None) -> tuple[str | None, str]:
        """``(evidence, service)`` for an app (its key, or its ``Holder``) and its microphone hold that began at
        ``mic_since``.

        ``evidence`` is ``"fresh"``, ``"stale"`` or ``None`` (see the module docstring); ``service`` is the
        call's product name from the verdict ("" when unknown).
        """
        if not holder:
            return None, ""
        now = time.time() if now is None else float(now)
        with self._lock:
            found = [(at, service) for (k, _h), (at, service) in self._evidence.items()
                     if self._matches(holder, k) and at >= float(mic_since) - HOLD_TOLERANCE_S]
        if not found:
            return None, ""
        at, service = max(found)
        return ("fresh" if now - at <= self.fresh_s else "stale"), service

    def host_of(self, holder: Any) -> str | None:
        """The site of the newest in-call evidence of an app (key or ``Holder``; None: an app window, or none)."""
        if not holder:
            return None
        with self._lock:
            found = [(at, h or "") for (k, h), (at, _s) in self._evidence.items() if self._matches(holder, k)]
        return (max(found)[1] or None) if found else None

    def forget(self, key: str) -> None:
        """The app let go of the microphone: its in-call evidence is over (a new hold needs a new screen)."""
        with self._lock:
            for where in [w for w in self._evidence if w[0] == key.casefold()]:
                self._evidence.pop(where, None)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {**self.counters, "screens": len(self._screens), "evidence": len(self._evidence),
                    "cost_usd": round(self.cost_usd, 5)}

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is not None:
            try:
                self._log_fn(type, **fields)
            except Exception:  # noqa: BLE001
                pass


__all__ = ["CallEvidence", "Screen", "Verdict", "JUDGE_PROMPT", "JUDGE_TOOL", "FRESH_S"]
