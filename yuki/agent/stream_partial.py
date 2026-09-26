"""The reply and the thinking as they stream, for a chat that should never look dead (2026-09-27).

A typed turn takes a few seconds: the round trip, a short thought, then the reply,
which arrives as the ``done`` tool's ``message`` input. The agent loop hands every
stream event to :func:`stream_partial`; it keeps a little state per request and
returns a :class:`~yuki.log.events.Partial` when there is something new to show:

* ``reply`` -- the ``done`` message written so far (the SDK's ``input_json``
  event carries the parsed partial input as its ``snapshot``);
* ``thinking`` -- the summarised thinking written so far (``thinking`` events).

At most one of each every :data:`MIN_GAP_S`, so a long reply costs a handful of
UI updates a second. Nothing here changes what the loop does: the final text still
comes from the finished request, and a message the loop later discards (a tool in
the same turn failed) is replaced by what happens next.
"""

from __future__ import annotations

import time
from typing import Any

from yuki.log.events import Partial

#: The fewest seconds between two partial updates of the same kind.
MIN_GAP_S = 0.08
#: The tool whose ``message`` input is the reply the user reads.
REPLY_TOOL = "done"


def stream_partial(event: Any, state: dict[str, Any]) -> Partial | None:
    """One stream event in, maybe one :class:`Partial` out. ``state``: a dict kept for one request."""
    kind = getattr(event, "type", None)
    if kind == "content_block_start":
        block = getattr(event, "content_block", None)
        state["block"] = (getattr(block, "type", None), getattr(block, "name", None))
        return None
    now = time.monotonic()
    if kind == "input_json" and state.get("block") == ("tool_use", REPLY_TOOL):
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


__all__ = ["stream_partial", "MIN_GAP_S", "REPLY_TOOL"]
