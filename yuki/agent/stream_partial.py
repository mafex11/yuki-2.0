"""The reply, the thinking and the spoken step line as they stream, so nothing ever looks (or sounds) dead.

A typed turn takes a few seconds: the round trip, a short thought, then the reply,
which arrives as the ``done`` tool's ``message`` input. The agent loop hands every
stream event to :func:`stream_partial`; it keeps a little state per request and
returns a :class:`~yuki.log.events.Partial` when there is something new to show:

* ``reply`` -- the ``done`` message written so far (the SDK's ``input_json``
  event carries the parsed partial input as its ``snapshot``);
* ``thinking`` -- the summarised thinking written so far (``thinking`` events);
* ``step`` -- the few words a voice request's step is to be announced with (the
  ``say_now`` field every action and slower read takes, written first:
  docs/VOICE.md v3.14), once, the moment its string is complete in the call's
  streaming JSON, while the rest of the call is still being written. The voice
  decides whether and when it is said;
* ``now`` -- the brain's live note of what it checks or does and why (appPT, the ``now`` field on the step tools),
  once its string is complete, before the step starts: the island's thinking and working line (never spoken).

At most one reply or thinking update every :data:`MIN_GAP_S`, so a long reply costs a handful of
UI updates a second. Nothing here changes what the loop does: the final text still
comes from the finished request, and a message the loop later discards (a tool in
the same turn failed) is replaced by what happens next.
"""

from __future__ import annotations

import json
import time
from typing import Any

from yuki.log.events import Partial

#: The fewest seconds between two partial updates of the same kind.
MIN_GAP_S = 0.08
#: The tool whose ``message`` input is the reply the user reads.
REPLY_TOOL = "done"
#: The input field of a call that is its spoken step line (``yuki.agent.tools.STEP_FIELD``).
STEP_KEY = "say_now"
#: The input field of a call that is the brain's live note of what it checks or does and why (``yuki.agent.tools.
#: NOW_FIELD``; appPT): streamed out as ``Partial("now", ...)`` for the island's thinking and working line.
NOW_KEY = "now"


def string_value(partial_json: str, key: str) -> str | None:
    """The value of ``key`` in a tool call's input JSON still being written, once that string is complete.

    Parsing the call's own JSON (a fixed machine format): ``None`` until the value's closing quote has arrived,
    when the key is not there, or when its value is not a string.
    """
    quoted = f'"{key}"'
    at = partial_json.find(quoted)
    while at >= 0:   # the key itself (followed by its colon), never the same word as another field's string value
        colon = at + len(quoted)
        while colon < len(partial_json) and partial_json[colon] in " \t\r\n":
            colon += 1
        if colon < len(partial_json) and partial_json[colon] == ":":
            break
        at = partial_json.find(quoted, at + 1)
    if at < 0:
        return None
    start = colon + 1
    while start < len(partial_json) and partial_json[start] in " \t\r\n":
        start += 1
    if start >= len(partial_json) or partial_json[start] != '"':
        return None
    try:
        value, _end = json.JSONDecoder().raw_decode(partial_json, start)
    except ValueError:
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def stream_partial(event: Any, state: dict[str, Any]) -> Partial | None:
    """One stream event in, maybe one :class:`Partial` out. ``state``: a dict kept for one request."""
    kind = getattr(event, "type", None)
    if kind == "content_block_start":
        block = getattr(event, "content_block", None)
        state["block"] = (getattr(block, "type", None), getattr(block, "name", None))
        # the reply tool carries no note (``now`` is on the step tools only)
        state["json"], state["step_done"] = "", False
        state["now_done"] = state["block"] == ("tool_use", REPLY_TOOL)
        return None
    now = time.monotonic()
    if kind == "input_json" and (state.get("block") or (None,))[0] == "tool_use":
        if not (state.get("step_done") and state.get("now_done")):
            state["json"] = state.get("json", "") + (getattr(event, "partial_json", "") or "")
        if not state.get("step_done"):
            line = string_value(state["json"], STEP_KEY)
            if line is not None:
                state["step_done"] = True
                state["step_at"] = now
                return Partial("step", line)
        if not state.get("now_done"):
            # appPT (review F4): the brain's ``now`` note the moment its string is complete, while the rest of the call
            # is still being written: before the step starts, so the island's thinking line has it too
            note = string_value(state["json"], NOW_KEY)
            if note is not None:
                state["now_done"] = True
                return Partial("now", note)
        if state.get("block") != ("tool_use", REPLY_TOOL):
            return None
        snap = getattr(event, "snapshot", None)
        text = snap.get("message") if isinstance(snap, dict) else None
        if isinstance(text, str) and text.strip() and len(text) > state.get("reply_len", 0) \
                and now - state.get("reply_at", 0.0) >= MIN_GAP_S:
            state["reply_len"], state["reply_at"] = len(text), now
            return Partial("reply", text)
        return None
    if kind == "thinking":
        text = getattr(event, "snapshot", "") or ""
        if text.strip() and now - state.get("think_at", 0.0) >= MIN_GAP_S * 3:
            state["think_at"] = now
            return Partial("thinking", text)
    return None


__all__ = ["stream_partial", "string_value", "MIN_GAP_S", "NOW_KEY", "REPLY_TOOL", "STEP_KEY"]
