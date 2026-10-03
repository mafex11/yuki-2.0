"""The reader lane (revamp D, S6/D6; 07 §6 G): read-only questions never queue behind a UI job.

The front-desk lane was a second full agent shown every tool with its hands blocked, used only while the worker was
busy; in 36 logged sessions it never got a request (01 §2.2). It is now the **reader**: an agent that is shown only the
tools that read (by tool metadata: no action tool, no lane-only tool but its own ``hand_over``), with a smaller cached
prefix and so a faster first token, and a framing that says what it is. A request that turns out to need hands ends
with ``hand_over``: that tool is not in the agent's own subset, so the runtime counts the call as a tool the lane does
not have (``Lane.allowed_tools``) and queues the request for the worker - the front desk's contract, unchanged.

:func:`route` is where a request should go: the worker when the caller says it needs hands (the voice's ``acts`` mark,
a typed request with nothing that says otherwise) or the reader is busy; the reader when it is a read and the worker
is busy, or - once the runtime adopts it - when it is a read even with the worker idle, so a UI job that comes next is
not stuck behind it. The caller's marks, never the words. The runtime (``yuki/ui/runtime.py``, owned by the UI stream)
keeps its own routing until it calls this; with it unchanged the reader still takes every request while the worker is
busy, which is when a read used to queue.
"""

from __future__ import annotations

from typing import Any

from yuki.agent.tools import ACTION_TOOL_NAMES, ALL_TOOL_NAMES, LANE_ONLY_TOOLS, TOOL_TRAITS

#: The lane names that are the reader (the runtime still calls it ``front_desk``).
READER_LANES: frozenset[str] = frozenset({"front_desk", "reader"})

#: What the reader is shown: every tool that only reads or keeps a note (no action, no lane-only tool), by metadata.
READER_TOOLS: tuple[str, ...] = tuple(
    n for n in ALL_TOOL_NAMES
    if n not in ACTION_TOOL_NAMES and n not in LANE_ONLY_TOOLS and n not in TOOL_TRAITS
    and n not in ("open_yuki", "take_screenshot")
)
#: Its own lane-only tool.
READER_LANE_TOOLS: tuple[str, ...] = ("hand_over",)

#: Who the reader is (its role framing, the agent's ``extra_instructions``).
READER_INSTRUCTIONS = (
    "You are Yuki's reader: you answer what can be answered by reading - memory, the windows and pages on screen, "
    "files, the web - while the other you does the work that needs hands. You have no hands here. When the request "
    "needs something done on the desktop (opening, clicking, typing, sending, changing anything), call hand_over: it "
    "is queued for the task lane the moment you finish; then say in one line that it comes right after what is "
    "running, and call done. Never say it is being done or is done."
)


def is_reader(lane: str | None) -> bool:
    return str(lane or "") in READER_LANES


def route(*, needs_hands: bool | None, worker_busy: bool, reader_busy: bool = False) -> str:
    """``worker`` | ``reader`` for one request, from the caller's marks and who is busy (never the words).

    ``needs_hands``: the caller's mark (voice: the talker's ``acts``); ``None`` when the caller cannot say (a typed
    request), which goes to the worker when it is free, as before, and to the reader while it is busy (the reader
    hands it over if it needs hands)."""
    if needs_hands:
        return "worker"
    if reader_busy:
        return "worker"
    if needs_hands is False:
        return "reader"
    return "reader" if worker_busy else "worker"


def reader_agent(settings: Any, logger: Any, *, memory: Any = None, backend: Any = None, lane: str = "front_desk"
                 ) -> Any:
    """A reader-lane agent: the read tools only, its framing, no warm-up of a shell it cannot use."""
    from dataclasses import replace

    from yuki.agent.loop import Agent

    return Agent(replace(settings), logger, backend=backend, tool_names=READER_TOOLS,
                 extra_instructions=READER_INSTRUCTIONS, prewarm=False, lane=lane, memory=memory)


__all__ = ["READER_INSTRUCTIONS", "READER_LANES", "READER_LANE_TOOLS", "READER_TOOLS", "is_reader", "reader_agent",
           "route"]
