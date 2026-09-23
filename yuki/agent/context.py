"""Conversation state and context hygiene.

The message list is append-only in structure: nothing is ever deleted or
reordered. What *is* edited in place is the body of stale perception results --
a window tree from eight steps ago is many kilobytes describing a window that has
since changed, and keeping it wastes context and invites the model to act on a
picture of the past.

This is housekeeping about payload size and staleness, never about behaviour: the
stub says what was there and that it has been superseded, so the model can simply
look again if it still cares.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from yuki.agent.tools import PERCEPTION_TOOLS
from yuki.log.events import SessionLogger

#: First line of every text block Yuki writes into a ``user`` message itself.
#: The API joins adjacent text blocks with no separator, so without it the
#: model reads the user's "... on yt" and the next block's "Desktop right now:"
#: as one run of text ("ytDesktop") and cannot tell which part the user wrote.
ATTACHED_LABEL = "[Attached automatically by Yuki, not written by the user]"

#: Last line of the same blocks, so whatever follows (a new request after a
#: cancelled run, say) is just as clearly separated at the other end.
ATTACHED_END = "[End of attached context]"


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
        stubbed: Keys already replaced, so each edit is logged exactly once.
    """

    role: str
    content: Any
    turn: int
    tool_names: dict[str, str] = field(default_factory=dict)
    situation_pos: int | None = None
    stubbed: set[str] = field(default_factory=set)


class ContextManager:
    """Owns the message list, the running note, and stale-payload stubbing.

    Args:
        logger: Session logger, used to record every ``context_edit``.
        keep_turns: How many of the most recent assistant turns keep their full
            perception payloads.
    """

    def __init__(self, logger: SessionLogger, *, keep_turns: int = 2) -> None:
        self.logger = logger
        self.keep_turns = max(1, keep_turns)
        self.running_summary: str = ""
        self._slots: list[_Slot] = []

    # -- state -------------------------------------------------------------

    @property
    def messages(self) -> list[dict[str, Any]]:
        """The request's ``messages`` parameter, rebuilt from the slots."""
        return [{"role": slot.role, "content": slot.content} for slot in self._slots]

    def __len__(self) -> int:
        return len(self._slots)

    def set_note(self, text: str) -> None:
        """Replace the running note the model carries between turns."""
        self.running_summary = text.strip()

    # -- composing messages ------------------------------------------------

    def situation_text(self, overview_text: str, self_facts: str | None = None) -> str:
        """Build the per-turn context block: fresh desktop, request facts, note.

        The architecture contract says the model gets a fresh ``look_at_desktop``
        every turn without asking; this is that text. The running note and the
        facts about the request itself (``self_facts``: how long it has been
        running, how many model calls) ride along here rather than in the system
        prompt so the cached prefix stays stable. All of it lives in one block,
        so pruning stubs it as one unit once it is stale.

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
        self, text: str, overview_text: str, *, self_facts: str | None = None
    ) -> None:
        """Append a new user request together with the current situation.

        The request is the first block, verbatim and alone; the situation is a
        second, separately labelled block (see :meth:`situation_text`).
        """
        content = [
            {"type": "text", "text": text},
            {"type": "text", "text": self.situation_text(overview_text, self_facts)},
        ]
        self._slots.append(
            _Slot(role="user", content=content, turn=self.logger.turn, situation_pos=1)
        )

    def add_assistant(self, content: Any) -> None:
        """Append the model's full response content, thinking blocks included."""
        self._slots.append(_Slot(role="assistant", content=content, turn=self.logger.turn))

    def add_tool_results(
        self,
        results: list[dict[str, Any]],
        overview_text: str,
        *,
        tool_names: dict[str, str] | None = None,
        self_facts: str | None = None,
    ) -> None:
        """Append one user message holding every tool result plus the situation.

        Args:
            results: ``tool_result`` blocks, in the order the tools were called.
            overview_text: Formatted fresh desktop overview for this turn.
            tool_names: ``tool_use_id`` -> tool name, so pruning knows which
                results are perception snapshots.
            self_facts: One line of facts about the running request, placed in
                the situation block next to the overview.
        """
        content: list[Any] = list(results)
        content.append(
            {"type": "text", "text": self.situation_text(overview_text, self_facts)}
        )
        self._slots.append(
            _Slot(
                role="user",
                content=content,
                turn=self.logger.turn,
                tool_names=dict(tool_names or {}),
                situation_pos=len(content) - 1,
            )
        )

    def add_note(self, text: str) -> None:
        """Append a notice from Yuki (cancellations and the like) as a user message.

        Framed like the situation block: the next request may land right after
        it, and the two must not read as one piece of the user's writing.
        """
        self._slots.append(
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

    # -- hygiene -----------------------------------------------------------

    def prune(self) -> int:
        """Stub out perception payloads older than the last ``keep_turns`` turns.

        Returns:
            Number of payloads replaced by this call.
        """
        assistant_positions = [i for i, slot in enumerate(self._slots) if slot.role == "assistant"]
        if len(assistant_positions) <= self.keep_turns:
            return 0
        cutoff = assistant_positions[-self.keep_turns]
        edits = 0
        for slot in self._slots[:cutoff]:
            if slot.role != "user" or not isinstance(slot.content, list):
                continue
            edits += self._stub_slot(slot)
        return edits

    def _stub_slot(self, slot: _Slot) -> int:
        """Replace stale payloads inside one user message. Returns the edit count."""
        edits = 0
        for position, block in enumerate(slot.content):
            if position == slot.situation_pos:
                key = f"situation@{position}"
                if key in slot.stubbed:
                    continue
                original = _chars(block)
                # Same leading break as the block it replaces: in the first
                # message it sits right after the user's own words.
                stub = f"\n\n[desktop overview attached by Yuki on turn {slot.turn} - superseded]"
                slot.content[position] = {"type": "text", "text": stub}
                slot.stubbed.add(key)
                self.logger.context_edit(
                    tool_use_id="",
                    name="look_at_desktop",
                    from_turn=slot.turn,
                    original_chars=original,
                    stub=stub,
                )
                edits += 1
                continue
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = str(block.get("tool_use_id") or "")
            name = slot.tool_names.get(tool_use_id, "")
            if name not in PERCEPTION_TOOLS or tool_use_id in slot.stubbed:
                continue
            original = _chars(block.get("content"))
            stub = f"[{name} result from turn {slot.turn} - superseded]"
            block["content"] = [{"type": "text", "text": stub}]
            slot.stubbed.add(tool_use_id)
            self.logger.context_edit(
                tool_use_id=tool_use_id,
                name=name,
                from_turn=slot.turn,
                original_chars=original,
                stub=stub,
            )
            edits += 1
        return edits


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
