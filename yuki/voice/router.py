"""The hand-off router (E3a, v3.17, 07 §6 E3a): the thinker started ~1 s after the user stops, not ~2 s.

The talker decides every turn, and its decision to hand over shows only once its reply has begun a ``tell_yuki`` call:
its first token (~1.5 s with its ~16k-token prompt) plus a few tokens of the call. The thinker then starts at about end of
speech + 2.0-2.4 s (measured, 07 §2.2). A second, tiny call made at the same moment as the talker's, with a short prompt
(well under 2k tokens: a bare Haiku call's first token is ~0.9 s here, 07 §4), decides only whether this turn needs the
thinker, by the model's judgement on the user's words, a few earlier turns and the screen summary the talker's hands act
on. When it says so, the thinker starts on the user's own words at once (the same early start the talker's hand-off
already makes, ``VoiceBridge.begin_early``); the talker's ``tell_yuki`` then adopts that job when it comes. When the
talker decides otherwise (an answer, a hand, silence) or does not hand over within ``voice_router_adopt_s``, the job is
withdrawn before its first action, unspoken.

Behind ``Settings.voice_router`` (off by default; measured in docs/research/revamp-2026-10-02/26-build-voice-speed.md).
No keywords anywhere: the decision is the router model's, from worked examples; the code reads only its tool call's own
JSON (a fixed format).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

#: The router's prompt (about 600 tokens; with the turn's inputs the request stays well under 2k).
ROUTER_PROMPT = """\
You route one turn of a spoken conversation between a user and Yuki, an assistant on their Windows PC. Yuki has two \
halves. The talker is a quick voice: it answers, and does single steps itself on what the screen summary shows. The \
thinker is slower and has the user's memory, activity timeline, calls, files, the inside of every window, and many \
steps. Decide whether this turn needs the thinker, and call route once.

The talker handles alone (to = talker):
- social turns: greetings, thanks, reactions, small talk, an acknowledgement ("okay", "nice"), frustration at Yuki;
- words not said to Yuki: dictation to another app, talk to someone in the room, talk to the people on a call;
- the time, what Yuki said earlier, one of their standing rules;
- asking what they meant when the words make no sense;
- one step of its hands on what the screen summary shows: a media or volume key, one app's volume, scrolling a window, \
focusing, minimizing, maximizing or closing a window they name, showing or closing a tab on the tabs line, clicking an \
item that is on an items line, typing their words into the field the focus line names, opening an address or an app, \
opening Yuki's own window;
- ending this conversation, or quiet time.

The thinker is needed (to = thinker) for everything else: their day, activity, calls, meetings and memory, their files, \
messages, people and plans; anything of two or more steps; anything inside a window that no items line lists; looking \
something up, working something out, advising; something they tell Yuki to keep (a rule, a correction, a fact about \
their life); and a redo of something Yuki did wrong.

screen is true when doing it needs what is inside the window in front (a video, a button, an item, a message on it).

Good: "what did I do today?" -> thinker. "pause the music" -> talker. "open Chrome" -> talker. "thanks, perfect" -> \
talker. "click the mix" and the mix is on the items line -> talker; it is not -> thinker, screen true. "open Spotify \
and play my focus playlist" -> thinker. "who's my accountant?" -> thinker. "close the Claude tabs" and they are on the \
tabs line -> talker. On a call, "bro did you see that clutch" -> talker. "my cousin just got a job at Zomato" -> \
thinker (a fact to keep)."""

#: The one tool, forced: its fields are written in this order, so ``to`` is known a few tokens in.
ROUTE_TOOL: dict[str, Any] = {
    "name": "route",
    "description": "Say which half of Yuki this turn needs.",
    "input_schema": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "enum": ["thinker", "talker"]},
            "screen": {"type": "boolean", "description": "Doing it needs what is inside the window in front."},
        },
        "required": ["to", "screen"],
    },
}

#: Inputs are cut to these sizes (characters): the earlier turns, and the screen summary.
HISTORY_CHARS = 700
DESK_CHARS = 2400


@dataclass
class Route:
    """One decision: ``to`` (``thinker`` / ``talker`` / ``None`` when it failed), ``screen``, and its timing."""

    to: str | None = None
    screen: bool = False
    ms: float = 0.0
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


def router_request(words: str, *, history: list[tuple[str, str]] | None = None, desk: str | None = None,
                   call: str | None = None, model: str = "") -> dict[str, Any]:
    """The router's request: its prompt, the forced tool, and one user message with the turn's inputs."""
    lines: list[str] = []
    if call:
        lines.append(f"[They are on a voice call on {call}: most of what they say is for the people on it.]")
    past = list(history or [])[-4:]
    if past:
        text = " / ".join(f"{who}: {' '.join(str(said).split())}" for who, said in past)
        lines.append("[Earlier: " + (text if len(text) <= HISTORY_CHARS else "…" + text[-HISTORY_CHARS:]) + "]")
    if desk:
        summary = desk.strip()
        if len(summary) > DESK_CHARS:
            summary = summary[:DESK_CHARS] + "…"
        lines.append("[Screen summary the talker's hands act on:\n" + summary + "]")
    lines.append(f"Their words now: \"{' '.join(str(words).split())}\"")
    return {"model": model, "max_tokens": 40, "system": ROUTER_PROMPT, "tools": [ROUTE_TOOL],
            "tool_choice": {"type": "tool", "name": "route"},
            "messages": [{"role": "user", "content": "\n".join(lines)}]}


def _field(partial: str, name: str) -> Any:
    """A field of the tool call's input JSON while it is still being written, once its value is complete (``None``
    before). Parsing our own fixed format."""
    key = partial.find(f'"{name}"')
    if key < 0:
        return None
    colon = partial.find(":", key + len(name) + 2)
    if colon < 0:
        return None
    rest = partial[colon + 1:].lstrip(" \t\r\n")
    if rest.startswith("true"):
        return True
    if rest.startswith("false"):
        return False
    if rest.startswith('"'):
        try:
            value, _end = json.JSONDecoder().raw_decode(rest)
        except ValueError:
            return None
        return value
    return None


async def decide(client: Any, request: dict[str, Any], *, timeout_s: float = 3.0) -> Route:
    """Stream the router's call and return as soon as its decision is complete (``to``, and ``screen`` when it is the
    thinker). Never raises: a failure or a timeout gives ``Route(to=None)`` (the talker's own hand-off then starts the
    thinker, as without a router)."""
    started = time.monotonic()
    route = Route()
    partial = ""
    try:
        async with asyncio.timeout(timeout_s):
            async with client.messages.stream(**request) as stream:
                async for event in stream:
                    kind = getattr(event, "type", "")
                    if kind != "content_block_delta" or not hasattr(getattr(event, "delta", None), "partial_json"):
                        continue
                    partial += event.delta.partial_json or ""
                    to = _field(partial, "to")
                    if to in ("thinker", "talker"):
                        screen = _field(partial, "screen")
                        if to == "talker" or screen is not None:
                            route.to, route.screen = str(to), bool(screen)
                            route.ms = round((time.monotonic() - started) * 1000, 1)
                            return route
                final = await stream.get_final_message()
                for block in getattr(final, "content", []) or []:
                    if getattr(block, "type", "") == "tool_use":
                        data = dict(getattr(block, "input", {}) or {})
                        if data.get("to") in ("thinker", "talker"):
                            route.to, route.screen = str(data["to"]), bool(data.get("screen"))
                usage = getattr(final, "usage", None)
                if usage is not None:
                    route.usage = {"input": int(getattr(usage, "input_tokens", 0) or 0),
                                   "output": int(getattr(usage, "output_tokens", 0) or 0)}
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the router is only a head start
        route.error = type(exc).__name__
    route.ms = round((time.monotonic() - started) * 1000, 1)
    return route


__all__ = ["ROUTER_PROMPT", "ROUTE_TOOL", "Route", "decide", "router_request"]
