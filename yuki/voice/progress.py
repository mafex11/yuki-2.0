"""What the thinker is really doing: its action log, spoken progress, and "how's it going?" (docs/VOICE.md §6.3).

* :class:`JobLog` is one task's real record, kept by the bridge from the
  runtime's signals: the request, each tool call (its label and a few
  harmless details of its input), each result's one-line summary, ``say``
  lines, questions, the outcome. Only real events go in; nothing is inferred.
* :func:`step_of` turns a tool call into a step (its label, for the job table and the log). Since v3.14 the
  thinker's own step lines are said by :mod:`yuki.voice.steplines`, not paced here.
* :func:`answer_progress` answers "how's it going?" with one isolated model
  call over that task's log alone (gradient-bang's ``query_task_progress`` with
  ``task_progress.md``): no conversation, no memory, nothing accumulated, and
  told never to say what the log does not show.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

#: Tools that are the thinker talking to itself or to the user, not steps of work.
NOT_STEPS = frozenset({"done", "note_to_self", "say", "ask_user"})
#: Input fields shown with a step (short strings only); everything else stays out.
DETAIL_KEYS = ("query", "since", "until", "group_by", "app", "url")

TASK_PROGRESS_PROMPT = """\
You answer a question about a task Yuki is working on (or just finished) for the user, from the \
task's log alone. Your answer is spoken to the user by Yuki's voice, so write one or two short \
spoken sentences, in the first person as Yuki ("I'm going through your activity now").

The log lists, with seconds since the task started: the request, each step Yuki took (a tool \
and what it was for), what each step returned in one line, anything Yuki said or asked, and the \
outcome if it has finished.

- Answer only from the log. Do not make up information that is not in the log: no findings, \
numbers, names, steps or time estimates it does not show.
- "How's it going" or "what are you doing": say what the latest steps are doing, and what has \
come back so far if the log shows it.
- If it has finished, say it has finished and, briefly, what the outcome says.
- If the log does not answer the question, say what you are doing now instead, and that you do \
not know the rest yet.
- No lists, no markdown, no ids or code."""


@dataclass
class JobLog:
    """A task's real action log, in memory only (it holds the user's words; never written to the voice log)."""

    request: str
    started: float = field(default_factory=time.monotonic)
    lines: list[str] = field(default_factory=list)
    steps: int = 0

    def add(self, kind: str, text: str) -> None:
        at = round(time.monotonic() - self.started)
        self.lines.append(f"[{at}s] {kind}: {' '.join(str(text).split())[:300]}")
        del self.lines[:-60]

    def render(self, state: str) -> str:
        return "\n".join([f"Request: {self.request}", f"State now: {state}",
                          f"Seconds since it started: {round(time.monotonic() - self.started)}", *self.lines])


def step_of(name: str, tool_input: dict[str, Any] | None) -> tuple[str, str] | None:
    """``(label, detail)`` of a tool call as a progress step, or ``None`` for a non-step (``done``, ``say``, ...)."""
    if name in NOT_STEPS:
        return None
    from yuki.agent.tools import tool_label

    label = tool_label(name) or name.replace("_", " ")
    details = []
    for key in DETAIL_KEYS:
        value = (tool_input or {}).get(key)
        if isinstance(value, str) and value.strip() and len(value) <= 80:
            details.append(f"{key}: {value.strip()}")
    return label, "; ".join(details)


async def answer_progress(client: Any, model: str, log_text: str, question: str, *,
                          max_tokens: int = 150) -> tuple[str | None, dict[str, Any]]:
    """One isolated call: the log and the question in, a short spoken answer out.

    Returns ``(text or None, facts)`` where facts are content-free (ms, tokens, error).
    """
    started = time.monotonic()
    facts: dict[str, Any] = {}
    try:
        response = await client.messages.create(
            model=model, max_tokens=max_tokens, system=TASK_PROGRESS_PROMPT,
            messages=[{"role": "user", "content": f"Task log:\n{log_text}\n\nQuestion: {question or 'How is it going?'}"}],
        )
        text = "".join(getattr(b, "text", "") for b in response.content).strip() or None
        usage = getattr(response, "usage", None)
        facts.update(input_tokens=getattr(usage, "input_tokens", None), output_tokens=getattr(usage, "output_tokens", None))
    except Exception as exc:  # noqa: BLE001 - the mouth falls back to the table
        text = None
        facts["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    facts["ms"] = round((time.monotonic() - started) * 1000)
    return text, facts
