"""Conversation state, context hygiene and the conversation cache breakpoint.

The message list is append-only in structure: nothing is ever deleted or
reordered, and assistant turns are never touched at all -- they are stored as
the SDK returned them, thinking blocks included, and passed back unchanged.
What *is* edited in place is the body of superseded perception results: a
window tree the model has since looked at again is many kilobytes describing a
picture of the past, and keeping it wastes context and invites the model to act
on it.

Supersession, not age
---------------------
A perception result is replaced by a one-line stub as soon as a newer result of
the same kind exists: a later successful ``look_at_window`` of the same hwnd, a
later desktop overview (explicit ``look_at_desktop`` or the one Yuki attaches to
every turn -- the same text from the same function), a later ``system_facts``,
a later ``take_screenshot`` of the same target, or a later ``recall`` with the
same arguments. The most recent look of each
window, and of each other kind, always stays in full however old it is. A
failed look (``is_error``) supersedes nothing: it says nothing about the window.

The window view Yuki attaches after a turn of successful actions ("Window after
your actions") is a look like any other: keyed as ``look_at_window`` of its hwnd,
it supersedes older looks of that window (explicit or attached) and is
superseded by newer ones. It travels as its own attached block right *after*
the situation block rather than inside it, because the situation block is
stubbed every round by the next overview while the view must live exactly as
long as nothing newer has looked at its window.
This is housekeeping about payload size, never about behaviour: the stub says
what was there and that something newer exists, so the model can look again if
it still cares.

The conversation cache, and the trade-off it forces on stubbing
---------------------------------------------------------------
System blocks and tools carry their own breakpoints (``prompt.py``,
``tools.py``). :meth:`ContextManager.request_messages` adds one more, moving
forward every request, on the newest user message, so each request reads the
whole previous conversation from cache and pays full price only for what was
appended since. Caching is a byte-exact prefix match, so any rewrite of a block
at or before the previous request's breakpoint invalidates the cache from that
block on: the rewritten block and everything after it are processed uncached
(and written again) on that one request.

That is why stubbing is by supersession only. The old rule (stub every
perception payload older than two turns) rewrote something two turns back on
*every* round, so no messages prefix ever matched twice. Now:

* The breakpoint sits on the last block *before* the situation block of the
  newest user message (the last tool result, or the user's own words on a new
  request), never on the situation block itself. The situation block is the one
  thing guaranteed to be superseded next round -- by the next turn's overview.
  Leaving it after the breakpoint means stubbing it next round touches only
  content *after* the previous breakpoint, which costs no cache. The price: each
  turn's overview (~1-2k tokens) is always sent uncached, once. The window view
  after actions sits after the situation block for the same reason: stubbing
  it one round later touches only content after the previous breakpoint.
* A superseded ``look_at_window``/``system_facts``/``take_screenshot`` result is
  usually one or two turns back, i.e. at or just before the previous
  breakpoint. That rewrite is accepted: that one request misses from the old
  look's position (entries written by earlier requests at earlier positions
  stay valid read points, so everything before it is still read from cache),
  and every later request reads the stubbed version from cache again. A
  one-time miss of a few thousand tokens buys a permanently smaller history.
  Each such edit is logged with ``before_cache_breakpoint=True``.
* Nothing else is ever rewritten. There is no age-based stubbing any more: an
  unsuperseded result stays in full, and stays cached.

Limits respected:

* At most 4 breakpoints per request. The caller passes how many slots system
  and tools already use; with ``extra_instructions`` that is 3 (two system
  blocks plus the tools), leaving exactly one for the conversation. With none
  left, no conversation breakpoint is added.
* Minimum cacheable prefix: 1024 tokens on Claude Sonnet 5 (512 on Claude
  Opus 5). A breakpoint's prefix includes tools and system (~4.6k tokens here),
  so every conversation breakpoint clears it; a marker below the minimum would
  be a silent no-op anyway, never an error.
* A breakpoint looks back at most 20 content positions for the previous
  request's entry. Consecutive breakpoints here are one assistant turn apart
  (previous situation block, the assistant's thinking/text/tool_use run, the
  tool_result run: about five positions), well inside that window.
* The marker is put on a *copy* of the chosen block at request time; the stored
  transcript never carries ``cache_control``, so markers never pile up.

In-session compaction: finished requests shrink to one line each
------------------------------------------------------------------
One conversation spans a whole app run, so without more than stubbing every
request would still carry every earlier request's words, thinking, tool calls,
tool results, memory block and last window view. After each request ends,
:meth:`ContextManager.compact` collapses every finished request older than the
newest :data:`KEEP_FULL_REQUESTS` into one plain-text user/assistant pair::

    user:      [earlier request, 14:02] <what the user asked>
    assistant: → <what Yuki finally said> (actions: <one line each>)

Structure stays valid by construction: the pair replaces *all* of that request's
messages (its tool_use blocks and their tool_result blocks go together, its
thinking blocks go with the assistant turns they belonged to), the pair is
rebuilt as plain text with no thinking, tool or image block in it, and requests
are collapsed oldest first, so the collapsed pairs are always a prefix of the
transcript followed by untouched requests. Assistant turns that are kept are
never edited.

Cache: compaction rewrites content, so it is confined to what lies wholly
*before* the previous request's committed breakpoint (a request not yet
covered by one is left for the next time) and happens once per request, after
it ends. The next request then misses the conversation cache from the first
rewritten message on, once. To keep that miss small the last collapsed pair
carries a second conversation breakpoint when a slot is free (not when
``extra_instructions`` already takes the fourth): the collapsed prefix only
ever grows by one pair per request, so the next request finds the previous
entry two positions back and reads every earlier pair from cache, paying full
price only for the three requests kept in full. Every compaction is logged as
``context_compaction`` with the transcript size before and after.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from yuki.agent.tools import PERCEPTION_TOOLS
from yuki.log.events import SessionLogger, _as_plain

#: First line of every text block Yuki writes into a ``user`` message itself.
#: The API joins adjacent text blocks with no separator, so without it the
#: model reads the user's "... on yt" and the next block's "Desktop right now:"
#: as one run of text ("ytDesktop") and cannot tell which part the user wrote.
ATTACHED_LABEL = "[Attached automatically by Yuki, not written by the user]"

#: Last line of the same blocks, so whatever follows (a new request after a
#: cancelled run, say) is just as clearly separated at the other end.
ATTACHED_END = "[End of attached context]"

#: The marker for the moving conversation breakpoint.
CACHE_MARKER: dict[str, str] = {"type": "ephemeral"}

#: Supersession key shared by the per-turn overview and explicit look_at_desktop.
_DESKTOP_KEY: tuple[str, ...] = ("look_at_desktop",)

#: Finished requests kept in full by :meth:`ContextManager.compact`; older ones
#: are collapsed to one line each.
KEEP_FULL_REQUESTS = 3

#: Budget guards for one collapsed request (plumbing, not behaviour): most
#: action lines listed, and longest reply / request text kept.
COLLAPSED_ACTIONS = 8
COLLAPSED_TEXT_CHARS = 600


@dataclass
class RequestRecord:
    """How one finished request went, for its collapsed form.

    Attributes:
        text: What the user asked.
        reply: What Yuki finally said, or how the request ended.
        actions: One line per thing Yuki did.
        at: When the request was made (local time).
        outcome: ``final``, ``error``, ``cancelled`` or ``abandoned``.
    """

    text: str
    reply: str
    actions: list[str]
    at: datetime
    outcome: str = "final"


def collapsed_pair(record: RequestRecord) -> tuple[str, str]:
    """The user and assistant text a finished request collapses to."""

    def cut(text: str) -> str:
        text = " ".join(str(text or "").split())
        return text if len(text) <= COLLAPSED_TEXT_CHARS else text[: COLLAPSED_TEXT_CHARS - 1] + "…"

    user = f"[earlier request, {record.at.strftime('%H:%M')}] {cut(record.text)}"
    reply = cut(record.reply) or "(no reply)"
    if record.outcome == "cancelled":
        reply = f"(cancelled) {reply}"
    elif record.outcome in ("error", "abandoned"):
        reply = f"(ended without finishing: {record.outcome}) {reply}"
    actions = [" ".join(line.split()) for line in record.actions if str(line).strip()]
    shown = actions[:COLLAPSED_ACTIONS]
    if len(actions) > len(shown):
        shown.append(f"+{len(actions) - len(shown)} more")
    tail = f" (actions: {'; '.join(shown)})" if shown else ""
    return user, f"→ {reply}{tail}"


def attached_text(body: str) -> str:
    """Frame text Yuki attaches to a ``user`` message so it cannot pass as the user's.

    The leading blank line keeps the label off the end of whatever block comes
    before it once the API has joined them.
    """
    return f"\n\n{ATTACHED_LABEL}\n{body.strip()}\n{ATTACHED_END}"


@dataclass
class _Slot:
    """One message plus the bookkeeping needed to prune it later.

    Attributes:
        role: ``user`` or ``assistant``.
        content: The message content, exactly as it will be sent.
        turn: Turn number the message was created on.
        tool_names: ``tool_use_id`` -> tool name, for user messages carrying
            tool results.
        situation_pos: Index within ``content`` of the per-turn desktop overview
            block, if this message has one.
        view_pos: Index within ``content`` of the window view attached after
            actions, if this message has one.
        view_hwnd: The window that view shows.
        stubbed: Keys already replaced, so each edit is logged exactly once.
        request: Which request (1, 2, ...) the message belongs to.
        collapsed: Part of a collapsed request's plain-text pair.
    """

    role: str
    content: Any
    turn: int
    tool_names: dict[str, str] = field(default_factory=dict)
    situation_pos: int | None = None
    view_pos: int | None = None
    view_hwnd: int | None = None
    stubbed: set[str] = field(default_factory=set)
    request: int = 0
    collapsed: bool = False


@dataclass
class _Look:
    """One perception payload in the transcript, located for supersession."""

    key: tuple[str, ...]
    slot_index: int
    position: int
    tool_use_id: str
    name: str
    is_situation: bool
    stubbed: bool
    failed: bool
    is_view: bool = False


class ContextManager:
    """Owns the message list, the running note, stubbing and the cache breakpoint.

    Args:
        logger: Session logger, used to record every ``context_edit``.
        keep_turns: Kept for compatibility with ``Settings.keep_perception_turns``.
            Stubbing no longer depends on age (an age rule rewrites content
            before the cache breakpoint every round; see the module docstring),
            so this value does not change what is stubbed.
    """

    def __init__(self, logger: SessionLogger, *, keep_turns: int = 2) -> None:
        self.logger = logger
        self.keep_turns = max(1, keep_turns)
        self.running_summary: str = ""
        self._slots: list[_Slot] = []
        #: ``tool_use_id`` -> tool input, from the assistant turns, so a result
        #: can be matched to what it looked at (the hwnd of a look_at_window).
        self._tool_inputs: dict[str, dict[str, Any]] = {}
        #: ``(slot_index, block_index)`` of the breakpoint in the last request
        #: that got a response: the newest conversation cache entry.
        self._breakpoint: tuple[int, int] | None = None
        #: Placed by the latest :meth:`request_messages`, committed by
        #: :meth:`commit_breakpoint` once that request succeeded.
        self._pending_breakpoint: tuple[int, int] | None = None
        #: Number of the request being added to (1 from the first request on).
        self._request_seq = 0
        #: How each finished request went (:meth:`end_request`), until collapsed.
        self._records: dict[int, RequestRecord] = {}
        #: Slot index of the last collapsed pair's assistant message, which
        #: carries the compaction breakpoint when a slot is free.
        self._compact_end: int | None = None

    def _append(self, slot: _Slot) -> None:
        slot.request = self._request_seq
        self._slots.append(slot)

    # -- state -------------------------------------------------------------

    @property
    def messages(self) -> list[dict[str, Any]]:
        """The transcript as ``messages``, without any cache marker."""
        return [{"role": slot.role, "content": slot.content} for slot in self._slots]

    def __len__(self) -> int:
        return len(self._slots)

    def set_note(self, text: str) -> None:
        """Replace the running note the model carries between turns."""
        self.running_summary = text.strip()

    # -- the conversation cache breakpoint -----------------------------------

    def request_messages(self, *, max_breakpoints: int = 1) -> list[dict[str, Any]]:
        """The request's ``messages`` with the moving cache breakpoint attached.

        The marker goes on the newest user message, on the last block before its
        situation block (the module docstring says why not on the situation block
        itself). Assistant messages are never marked or copied: their blocks go
        back exactly as the SDK returned them. Only the one marked block is
        copied; the stored transcript stays unmarked.

        With a second slot free, the last collapsed pair (see :meth:`compact`)
        gets one too, so the collapsed prefix stays cached across requests.
        That pair is plain text Yuki wrote, so marking a copy of it is safe.

        Args:
            max_breakpoints: Breakpoint slots left after system and tools. Zero
                or less means no conversation breakpoint on this request.

        Returns:
            The ``messages`` parameter.
        """
        messages = self.messages
        self._pending_breakpoint = None
        if max_breakpoints <= 0:
            return messages
        target = self._breakpoint_target()
        if target is None:
            return messages
        slot_index, position = target
        content = list(self._slots[slot_index].content)
        content[position] = {**content[position], "cache_control": dict(CACHE_MARKER)}
        messages[slot_index] = {"role": "user", "content": content}
        self._pending_breakpoint = target
        end = self._compact_end
        if max_breakpoints >= 2 and end is not None and end < slot_index:
            slot = self._slots[end]
            marked = list(slot.content)
            marked[-1] = {**marked[-1], "cache_control": dict(CACHE_MARKER)}
            messages[end] = {"role": slot.role, "content": marked}
        return messages

    def commit_breakpoint(self) -> None:
        """Record that the last request, and so its cache write, went through."""
        if self._pending_breakpoint is not None:
            self._breakpoint = self._pending_breakpoint

    def breakpoint_info(self) -> dict[str, Any] | None:
        """Where the latest :meth:`request_messages` put the marker, for the log."""
        if self._pending_breakpoint is None:
            return None
        slot_index, position = self._pending_breakpoint
        block = self._slots[slot_index].content[position]
        return {
            "message_index": slot_index,
            "block_index": position,
            "block_type": _block_field(block, "type"),
            "tool_use_id": _block_field(block, "tool_use_id"),
            "previous": list(self._breakpoint) if self._breakpoint else None,
        }

    def _breakpoint_target(self) -> tuple[int, int] | None:
        """``(slot_index, block_index)`` for the marker, or ``None``."""
        for slot_index in range(len(self._slots) - 1, -1, -1):
            slot = self._slots[slot_index]
            if slot.role != "user":
                continue
            if not isinstance(slot.content, list) or not slot.content:
                return None
            if slot.situation_pos is not None and slot.situation_pos > 0:
                position = slot.situation_pos - 1
            else:
                position = len(slot.content) - 1
            block = slot.content[position]
            if not isinstance(block, dict):
                return None
            if block.get("type") == "text" and not (block.get("text") or "").strip():
                return None  # an empty text block cannot carry a marker
            return slot_index, position
        return None

    # -- composing messages ------------------------------------------------

    def situation_text(self, overview_text: str, self_facts: str | None = None) -> str:
        """Build the per-turn context block: fresh desktop, request facts, note.

        The architecture contract says the model gets a fresh ``look_at_desktop``
        every turn without asking; this is that text. The running note and the
        facts about the request itself (``self_facts``: how long it has been
        running, how many model calls) ride along here rather than in the system
        prompt so the cached prefix stays stable. All of it lives in one block,
        so it is stubbed as one unit once the next turn's block supersedes it.

        The block is framed by :func:`attached_text`: it always travels as its
        own content block, never inside the user's words, and its first line
        says Yuki attached it and the user did not write it.
        """
        parts = [f"Desktop right now:\n{overview_text}"]
        if self_facts:
            parts.append(self_facts)
        if self.running_summary:
            parts.append(f"Your working note:\n{self.running_summary}")
        return attached_text("\n\n".join(parts))

    def add_request(
        self,
        text: str,
        overview_text: str,
        *,
        self_facts: str | None = None,
        memory_text: str | None = None,
        origin_text: str | None = None,
    ) -> None:
        """Append a new user request together with the current situation.

        The request is the first block, verbatim and alone; the situation is the
        last, separately labelled block (see :meth:`situation_text`). Between
        them, when there are any, sit the request origin (where the user was when
        they asked: the window in front, media, microphone and camera, framed by
        :func:`attached_text`) and the memory block (what Yuki knows about the
        user, already framed by :func:`yuki.agent.memory.memory_block_text`).
        Both are background for the whole request and are never stubbed -- the
        origin matters most at the end, when the model decides whether to put
        the user back; being before the situation block, they are also where this
        message's cache breakpoint lands, so they are read from cache on every
        later round.
        """
        content = [{"type": "text", "text": text}]
        if origin_text and origin_text.strip():
            content.append({"type": "text", "text": attached_text(origin_text)})
        if memory_text and memory_text.strip():
            content.append({"type": "text", "text": memory_text})
        content.append(
            {"type": "text", "text": self.situation_text(overview_text, self_facts)}
        )
        self._request_seq += 1
        self._append(
            _Slot(
                role="user",
                content=content,
                turn=self.logger.turn,
                situation_pos=len(content) - 1,
            )
        )

    def add_assistant(self, content: Any) -> None:
        """Append the model's full response content, thinking blocks included.

        Stored as given and never edited: assistant turns are append-only, so
        thinking blocks go back exactly as they came. Tool inputs are indexed on
        the side so results can be matched to what they looked at.
        """
        if isinstance(content, (list, tuple)):
            for block in content:
                if _block_field(block, "type") != "tool_use":
                    continue
                tool_input = _block_field(block, "input")
                self._tool_inputs[str(_block_field(block, "id") or "")] = (
                    dict(tool_input) if isinstance(tool_input, dict) else {}
                )
        self._append(_Slot(role="assistant", content=content, turn=self.logger.turn))

    def add_tool_results(
        self,
        results: list[dict[str, Any]],
        overview_text: str,
        *,
        tool_names: dict[str, str] | None = None,
        self_facts: str | None = None,
        window_view: tuple[int, str] | None = None,
    ) -> None:
        """Append one user message holding every tool result plus the situation.

        Args:
            results: ``tool_result`` blocks, in the order the tools were called.
            overview_text: Formatted fresh desktop overview for this turn.
            tool_names: ``tool_use_id`` -> tool name, so pruning knows which
                results are perception snapshots.
            self_facts: One line of facts about the running request, placed in
                the situation block next to the overview.
            window_view: ``(hwnd, text)`` of the window read after this turn's
                actions, attached as its own block right after the situation
                block and tracked as a look at ``hwnd`` for stubbing.
        """
        content: list[Any] = list(results)
        content.append(
            {"type": "text", "text": self.situation_text(overview_text, self_facts)}
        )
        situation_pos = len(content) - 1
        view_pos: int | None = None
        view_hwnd: int | None = None
        if window_view is not None:
            view_hwnd, view_text = int(window_view[0]), window_view[1]
            content.append({"type": "text", "text": attached_text(view_text)})
            view_pos = len(content) - 1
        self._append(
            _Slot(
                role="user",
                content=content,
                turn=self.logger.turn,
                tool_names=dict(tool_names or {}),
                situation_pos=situation_pos,
                view_pos=view_pos,
                view_hwnd=view_hwnd,
            )
        )

    def add_note(self, text: str) -> None:
        """Append a notice from Yuki (cancellations and the like) as a user message.

        Framed like the situation block: the next request may land right after
        it, and the two must not read as one piece of the user's writing.
        """
        self._append(
            _Slot(
                role="user",
                content=[{"type": "text", "text": attached_text(text)}],
                turn=self.logger.turn,
            )
        )

    def dangling_tool_uses(self) -> list[tuple[str, str]]:
        """Tool calls at the end of the transcript that never got a result.

        This happens when a run is cancelled or interrupted mid-turn. The API
        requires one ``tool_result`` per ``tool_use``, so the next request must
        close them out first.

        Returns:
            ``(tool_use_id, tool_name)`` pairs, empty when the transcript is sound.
        """
        if not self._slots or self._slots[-1].role != "assistant":
            return []
        content = self._slots[-1].content
        if not isinstance(content, (list, tuple)):
            return []
        dangling = []
        for block in content:
            if _block_field(block, "type") != "tool_use":
                continue
            dangling.append(
                (str(_block_field(block, "id") or ""), str(_block_field(block, "name") or ""))
            )
        return dangling

    # -- compaction --------------------------------------------------------

    def end_request(self, record: RequestRecord) -> None:
        """Record how the current request went, so it can be collapsed later."""
        if self._request_seq > 0:
            self._records[self._request_seq] = record

    def transcript_chars(self) -> int:
        """Size of the transcript as sent (JSON characters of ``messages``)."""
        return len(json.dumps(_as_plain(self.messages), ensure_ascii=False, default=str))

    def compact(self, *, keep: int = KEEP_FULL_REQUESTS) -> dict[str, Any] | None:
        """Collapse finished requests older than the newest ``keep`` into one pair each.

        Call once per request, after it has ended (:meth:`end_request`).
        Oldest first, and only a request that has ended and lies wholly before
        the committed cache breakpoint (the module docstring says why); the
        first request that does not qualify stops it, so the collapsed pairs
        stay a prefix of the transcript.

        Returns:
            What was done (also logged as ``context_compaction``), or ``None``
            when nothing qualified. ``turns`` lists the turn each collapsed
            request began on, so the caller can tell whether something it
            attached there (the portrait) has left the conversation.
        """
        newest = self._request_seq
        bound = self._breakpoint[0] if self._breakpoint is not None else len(self._slots)
        spans: dict[int, list[int]] = {}
        for index, slot in enumerate(self._slots):
            if not slot.collapsed and slot.request > 0:
                spans.setdefault(slot.request, []).append(index)
        chosen: list[int] = []
        for request in sorted(spans):
            if request > newest - keep or request not in self._records:
                break
            if spans[request][-1] >= bound:
                break
            chosen.append(request)
        if not chosen:
            return None

        chars_before = self.transcript_chars()
        messages_before = len(self._slots)
        first = spans[chosen[0]][0]
        last = spans[chosen[-1]][-1]
        replaced = self._slots[first : last + 1]
        per_request = []
        pairs: list[_Slot] = []
        for request in chosen:
            record = self._records.pop(request)
            slots = [self._slots[i] for i in spans[request]]
            user_text, assistant_text = collapsed_pair(record)
            per_request.append(
                {
                    "request": request,
                    "turn": slots[0].turn,
                    "messages": len(slots),
                    "chars": len(
                        json.dumps(
                            _as_plain([{"role": s.role, "content": s.content} for s in slots]),
                            ensure_ascii=False,
                            default=str,
                        )
                    ),
                    "user": user_text,
                    "assistant": assistant_text,
                }
            )
            turn = slots[0].turn
            pairs.append(
                _Slot(
                    role="user",
                    content=[{"type": "text", "text": user_text}],
                    turn=turn,
                    request=request,
                    collapsed=True,
                )
            )
            pairs.append(
                _Slot(
                    role="assistant",
                    content=[{"type": "text", "text": assistant_text}],
                    turn=slots[-1].turn,
                    request=request,
                    collapsed=True,
                )
            )
        for slot in replaced:
            if slot.role == "assistant" and isinstance(slot.content, (list, tuple)):
                for block in slot.content:
                    if _block_field(block, "type") == "tool_use":
                        self._tool_inputs.pop(str(_block_field(block, "id") or ""), None)
        self._slots[first : last + 1] = pairs
        shift = len(replaced) - len(pairs)

        def moved(point: tuple[int, int] | None) -> tuple[int, int] | None:
            if point is None or point[0] <= last:
                return point
            return point[0] - shift, point[1]

        before_breakpoint = self._breakpoint is not None
        self._breakpoint = moved(self._breakpoint)
        self._pending_breakpoint = moved(self._pending_breakpoint)
        self._compact_end = first + len(pairs) - 1
        info = {
            "requests": chosen,
            "turns": [item["turn"] for item in per_request],
            "kept_full": keep,
            "messages_before": messages_before,
            "messages_after": len(self._slots),
            "chars_before": chars_before,
            "chars_after": self.transcript_chars(),
            "collapsed": per_request,
            "before_cache_breakpoint": before_breakpoint,
        }
        self.logger.log("context_compaction", **info)
        printer = getattr(self.logger, "_print", None)
        if callable(printer):
            printer(
                f"[dim]   context: collapsed request{'s' if len(chosen) > 1 else ''} "
                f"{', '.join(map(str, chosen))} ({info['chars_before']} -> "
                f"{info['chars_after']} chars)[/dim]"
            )
        return info

    # -- hygiene -----------------------------------------------------------

    def prune(self) -> int:
        """Stub every perception payload that a newer one of the same kind supersedes.

        Call before each request. The newest successful look of each key (each
        window, the desktop, system facts, each screenshot target) stays in full;
        every older one is stubbed once and logged as ``context_edit``.

        Returns:
            Number of payloads replaced by this call.
        """
        looks = self._looks()
        newest: dict[tuple[str, ...], _Look] = {}
        for look in looks:  # transcript order: the last one kept is the newest
            if not look.failed:
                newest[look.key] = look
        edits = 0
        for look in looks:
            if look.stubbed or look.failed:
                continue
            latest = newest.get(look.key)
            if latest is None or latest is look:
                continue
            self._stub(look, latest)
            edits += 1
        return edits

    def _looks(self) -> list[_Look]:
        """Every perception payload in the transcript, in transcript order."""
        looks: list[_Look] = []
        for slot_index, slot in enumerate(self._slots):
            if slot.role != "user" or not isinstance(slot.content, list):
                continue
            for position, block in enumerate(slot.content):
                if position == slot.situation_pos:
                    looks.append(
                        _Look(
                            key=_DESKTOP_KEY,
                            slot_index=slot_index,
                            position=position,
                            tool_use_id="",
                            name="look_at_desktop",
                            is_situation=True,
                            stubbed=f"situation@{position}" in slot.stubbed,
                            failed=False,
                        )
                    )
                    continue
                if position == slot.view_pos:
                    looks.append(
                        _Look(
                            key=("look_at_window", str(slot.view_hwnd)),
                            slot_index=slot_index,
                            position=position,
                            tool_use_id="",
                            name="look_at_window",
                            is_situation=False,
                            stubbed=f"view@{position}" in slot.stubbed,
                            failed=False,
                            is_view=True,
                        )
                    )
                    continue
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_use_id = str(block.get("tool_use_id") or "")
                name = slot.tool_names.get(tool_use_id, "")
                if name not in PERCEPTION_TOOLS:
                    continue
                looks.append(
                    _Look(
                        key=self._look_key(name, tool_use_id),
                        slot_index=slot_index,
                        position=position,
                        tool_use_id=tool_use_id,
                        name=name,
                        is_situation=False,
                        stubbed=tool_use_id in slot.stubbed,
                        failed=bool(block.get("is_error")),
                    )
                )
        return looks

    def _look_key(self, name: str, tool_use_id: str) -> tuple[str, ...]:
        """What a perception result is a look *at*; a newer look at it supersedes it."""
        if name == "look_at_desktop":
            return _DESKTOP_KEY
        tool_input = self._tool_inputs.get(tool_use_id, {})
        if name in ("look_at_window", "read_page"):
            hwnd = tool_input.get("hwnd")
            try:
                hwnd = int(hwnd)
            except (TypeError, ValueError):
                pass
            return (name, str(hwnd))
        if name in ("take_screenshot", "recall", "activity"):
            # A newer recall with the same arguments supersedes an older one; a
            # recall asking anything else is a different look.
            return (name, _canonical(tool_input))
        return (name,)

    def _stub(self, look: _Look, latest: _Look) -> None:
        """Replace one superseded payload with a stub and log the edit."""
        slot = self._slots[look.slot_index]
        newer_turn = self._slots[latest.slot_index].turn
        block = slot.content[look.position]
        if latest.slot_index == look.slot_index:
            if latest.is_situation:
                by = "the desktop overview attached at the end of this message"
            elif latest.is_view:
                by = "the window view attached after your actions, at the end of this message"
            else:
                by = "a later result in this same message"
        elif latest.is_view:
            by = f"the window view attached after your actions on turn {newer_turn}"
        else:
            by = f"a newer one on turn {newer_turn}"
        what = look.name
        if look.key[0] == "look_at_window" and len(look.key) > 1:
            what = f"look_at_window hwnd={look.key[1]}"
        if look.is_view:
            original = _chars(block)
            stub = (
                f"\n\n[window view of hwnd={look.key[1]} attached by Yuki after your "
                f"actions on turn {slot.turn} - superseded by {by}]"
            )
            slot.content[look.position] = {"type": "text", "text": stub}
            slot.stubbed.add(f"view@{look.position}")
        elif look.is_situation:
            original = _chars(block)
            # Same leading break as the block it replaces: in the first message
            # it sits right after the user's own words.
            stub = (
                f"\n\n[desktop overview attached by Yuki on turn {slot.turn}"
                f" - superseded by {by}]"
            )
            slot.content[look.position] = {"type": "text", "text": stub}
            slot.stubbed.add(f"situation@{look.position}")
        else:
            original = _chars(block.get("content"))
            stub = f"[{what} result from turn {slot.turn} - superseded by {by}]"
            block["content"] = [{"type": "text", "text": stub}]
            slot.stubbed.add(look.tool_use_id)
        before_breakpoint = self._breakpoint is not None and (
            (look.slot_index, look.position) <= self._breakpoint
        )
        self.logger.log(
            "context_edit",
            reason="superseded",
            tool_use_id=look.tool_use_id,
            name=look.name,
            key=list(look.key),
            from_turn=slot.turn,
            message_index=look.slot_index,
            block_index=look.position,
            superseded_by_turn=newer_turn,
            superseded_by_message_index=latest.slot_index,
            superseded_by_block_index=latest.position,
            original_chars=original,
            stub=stub,
            # True: at or before the previous request's cache breakpoint, so
            # this request misses the conversation cache from here on.
            before_cache_breakpoint=before_breakpoint,
        )
        printer = getattr(self.logger, "_print", None)
        if callable(printer):
            printer(
                f"[dim]   context: stubbed {what} from turn {slot.turn} "
                f"({original} chars, superseded by turn {newer_turn}"
                f"{'; cache miss from here' if before_breakpoint else ''})[/dim]"
            )


def _canonical(value: Any) -> str:
    """Canonical JSON of a tool input, so equal inputs share a key."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def _block_field(block: Any, field_name: str) -> Any:
    """Read a field from a content block, whether it is a dict or an SDK object."""
    if isinstance(block, dict):
        return block.get(field_name)
    return getattr(block, field_name, None)


def _chars(content: Any) -> int:
    """Rough character size of a content block or list of blocks."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, dict):
        total = 0
        for key, value in content.items():
            if key == "source" and isinstance(value, dict):
                total += len(str(value.get("data") or ""))
            else:
                total += _chars(value) if isinstance(value, (dict, list)) else len(str(value))
        return total
    if isinstance(content, list):
        return sum(_chars(item) for item in content)
    return len(str(content))
