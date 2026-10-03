"""Think before acting: the situation check before a consequential action (report 47, N0 and N5).

Live 3 Oct the user asked Yuki, by voice, to message their best friend an explanation they had already sent him an
hour earlier. Yuki opened the chat and sent it again. The user: "it did not check anything on the screen, it just
blindly obeyed my order ... if I gave you the same job you would check the chat and tell me 'you already sent this,
send again?' ... you are the same model; the pipeline makes it dumb." From the code and the logs:

* the hand-off reached the thinker as an order, not a situation;
* the loop had no "should I?" step: the authorization judge asks *permission* ("did they ask for this?"), never
  *wisdom* ("is it already done, does it conflict, would they want to know something first, is there a better way?");
* the chat was read only far enough to find the message box (reads stop at a few hundred elements), so its latest
  messages were never seen;
* memory was asked *who* the person is, never what the user recently said to them, although its capture of the chat
  held the user's own message from an hour before.

So before a step with a lasting effect (a send, a post, a close, a delete, anything the brain declares cannot be
undone) the thinker calls ``check_situation``. This module gathers, in code and in parallel, what a careful person
would look at first, and hands it back with the question a good assistant asks itself:

1. **the target's current state**: the window read again with a larger budget than an ordinary look, and its latest
   text (the bottom of a chat, the visible part of a page) listed by where it sits on the screen, newest last;
2. **memory's recent history** of the person or thing: the user's own last messages to that person on each route,
   with when they were sent, their last message back, and memory's facts of the last hours on the same subject;
3. the user's own messages captured in that app in the same hours, the ones most like what is about to be sent first
   (a measured similarity, shown as a number: the model decides what it means).

What to make of it is the model's judgement, taught by worked examples in the tool's description and in the result:
it tells, suggests or asks instead of acting when something turns up ("You already told him at 6:08 PM, the same
thing. Send it again?"), and goes ahead without a question when nothing does. The agent loop refuses a
consequential step whose request has no situation check (:func:`consequence`), so the check cannot be skipped by a
fast first pass. A small reversible step needs none: its usual look at the window is enough.

No keyword lists and no rules about words: the gate is tool metadata (the judge's ``sends``, a close operation, the
brain's own ``risk`` declaration), the evidence is gathered by geometry, time and string similarity, and the decision
is the model's.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Sequence

#: The tool's name, and how the loop's refusal and the result are labelled.
TOOL_NAME = "check_situation"
CHECK_LABEL = "[Yuki's check, not the user]"

#: The read of the target: a larger budget than an ordinary look (400 elements, which stopped short of a chat's
#: newest messages live), and how many of its latest text lines are listed.
READ_CAP = 1200
READ_TIMEOUT_S = 4.0
LATEST_LINES = 18
LINE_CHARS = 220
#: An element whose box covers more than this share of the window is a container, not a line of its content.
CONTAINER_SHARE = 0.4
#: How far back memory looks by default (hours), and the most it may be asked to.
DEFAULT_HOURS = 24.0
MAX_HOURS = 24.0 * 14
#: Memory's parts: how long each may take (s; they run in parallel with the read), how many of the user's own recent
#: messages are listed, and how many characters of related facts.
MEMORY_TIMEOUT_S = 2.5
OWN_MESSAGES = 6
SAMPLES_PER_ROUTE = 3
FACTS_CHARS = 1400
#: A captured message of the user's at least this similar to what is about to be sent is marked as close to it
#: (``yuki.agent.finish.similar``: difflib's ratio over the two texts; one containing the other counts as 1.0).
SIMILAR_MARK = 0.5

SITUATION_TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "label": "Checking first",
    "description": (
        "Before a step that lasts (a send, post or reply to anyone, closing a window, deleting, buying, anything "
        "that cannot be undone), in a response before that step: reads the target window again for its latest "
        "content (a chat's newest messages), what memory holds of the person or thing lately (the user's own last "
        "messages to them, with times) and the user's recent messages in that app most like what you are about to "
        "send. Then you judge what a good assistant would notice, and tell or ask instead of acting when something "
        "turns up."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "The step, its target and its content (the message)."},
            "hwnd": {"type": "integer", "description": "The window it happens in."},
            "about": {"type": "string", "description": "The person or thing, by the name memory knows."},
            "hours": {"type": "number", "description": "How far back memory looks (default 24)."},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

#: The question the result ends with (the model answers it in its next step; nothing here reads the answer).
QUESTION = (
    "Now, before you act, answer from what is above what a good assistant would notice: is it already done (the same "
    "thing already said or sent, already closed)? Does it conflict with something here? Is there something they "
    "would want to know first? Is there a better way? If something turns up, do not act: ask them in one short "
    "line naming what you saw and when, by its time as shown (\"at 6:08 PM\"), with ask_user, and wait for their "
    "answer: the choice is theirs, so never decide for them that it is not needed. If nothing does, go "
    "ahead now, without asking. Examples: they ask to tell Sam the build is fixed, and they wrote \"build's fixed\" "
    "to him at 6:08 PM -> \"You already told Sam the build's fixed, at 6:08 PM. Send it again?\"; they ask to ask "
    "Maya if she is free tonight, and her message 10 minutes ago says she is out of town -> \"Maya just said she's "
    "out of town tonight. Still ask?\"; they ask to close a window that holds a post they were writing -> \"That "
    "window has your unsent post. Close it anyway?\"; they ask to tell Sam they are on their way and nothing like "
    "it is there -> send it, no question. Their yes to such a question is the go-ahead: then send it.")

#: What the loop says when a consequential step comes with no situation check in its request (or only one sent in the
#: same response, whose result was not read before the step).
REFUSAL = (
    "{label}: not run: this step {what}, and nothing in this request has looked at the situation yet (a check sent "
    "in the same response as the step does not count: its result was not read). This is your own look, not a "
    "question for the user: call check_situation now, alone (action: what you are about to do with its content; "
    "hwnd: the window it happens in; about: the person or thing), then decide from what it shows: act at once when "
    "nothing turns up, or ask them when something does.")


# ---------------------------------------------------------------------------
# When a check is needed (tool metadata only)
# ---------------------------------------------------------------------------


def consequence(name: str, tool_input: dict[str, Any] | None, *, sends: bool = False) -> str | None:
    """What makes this call consequential, in a few words for the refusal, or ``None`` for a step that needs no check.

    From facts of the call, never its words: the authorization judge's ``sends`` (it sends, posts or submits to a
    person), a close operation of the window or tab tools, or the brain's own ``risk`` declaration that it cannot be
    undone. A small reversible step (opening, switching, scrolling, typing into a field without sending) needs none.
    """
    tool_input = tool_input or {}
    if sends:
        return "sends something to a person"
    if name == "window_action" and str(tool_input.get("action") or "") == "close":
        # a closed window can lose what it held (an unsent post, unsaved work); a closed tab comes back from the
        # browser's own history, so a tab close is a small reversible step (its tool reads the tab by identity)
        return "closes a window"
    risk = tool_input.get("risk")
    if isinstance(risk, dict) and risk.get("reversible") is False:
        effect = " ".join(str(risk.get("effect") or "").split())[:120]
        return f"cannot be undone ({effect})" if effect else "cannot be undone"
    return None


@dataclass
class CheckRecord:
    """One situation check made in this request: which window it read (``None``: none named), when, and the model
    call it came from (its result is seen only by a later call)."""

    hwnd: int | None
    at: float
    about: str = ""
    call: int = 0


def covered(checks: Sequence[CheckRecord], hwnd: int | None, call: int | None = None) -> bool:
    """Whether a check made in this request covers a step on ``hwnd`` made in model call ``call``: one whose result the
    model had seen when it wrote the step (made in an earlier call: a check sent in the same response as the send ran
    first but was never read), and that read that window, or named no window, or any such check when the step's window
    is unknown."""
    seen = [c for c in checks if call is None or c.call < call]
    if not seen:
        return False
    if hwnd is None:
        return True
    return any(c.hwnd is None or c.hwnd == hwnd for c in seen)


# ---------------------------------------------------------------------------
# The target's latest state (geometry only)
# ---------------------------------------------------------------------------


def _area(b: Any) -> int:
    try:
        return max(0, int(b[2]) - int(b[0])) * max(0, int(b[3]) - int(b[1]))
    except (TypeError, ValueError, IndexError):
        return 0


def latest_lines(payload: dict[str, Any] | None, *, limit: int = LATEST_LINES) -> list[str]:
    """The window's latest text, newest last: elements with a name or a value, containers left out (a box over
    :data:`CONTAINER_SHARE` of the window), ordered by where they sit (top to bottom, then left to right), the
    bottom-most ``limit`` kept. In a chat that is its newest messages and the message box; on a page, its lower part.
    Consecutive repeats are folded. Geometry and text only."""
    if not isinstance(payload, dict):
        return []
    elements = [e for e in payload.get("elements") or [] if isinstance(e, dict)]
    if not elements:
        return []
    root = max((_area(e.get("bounds")) for e in elements), default=0) or 1
    rows: list[tuple[int, int, str]] = []
    for e in elements:
        b = e.get("bounds")
        if not b or len(b) != 4 or _area(b) <= 0 or _area(b) > CONTAINER_SHARE * root:
            continue
        name = " ".join(str(e.get("name") or "").split())
        value = " ".join(str(e.get("value") or "").split())
        text = name if not value or value == name else (f"{name}: {value}" if name else value)
        if not text:
            continue
        role = str(e.get("role") or "")
        states = [s for s in e.get("states") or [] if s]
        line = f"{role} \"{text[:LINE_CHARS]}\"" + (f" ({', '.join(states)})" if states else "")
        rows.append((int(b[1]), int(b[0]), line, text))
    rows.sort(key=lambda r: (r[0], r[1]))
    out: list[str] = []
    last = ""
    for _y, _x, line, text in rows:
        if out and (out[-1] == line or (text and text in last)):
            continue   # the same line again, or a part of the line before it (a message's text inside its item)
        out.append(line)
        last = text
    return out[-limit:]


# ---------------------------------------------------------------------------
# Memory's recent history
# ---------------------------------------------------------------------------


def when(at: Any, now: float | None = None) -> str:
    """A time as people say it, with how long ago ("Sat 3 Oct, 6:08 PM, 1 h ago"). Deterministic formatting."""
    try:
        stamp = float(at)
    except (TypeError, ValueError):
        return "at an unknown time"
    now = time.time() if now is None else float(now)
    dt = datetime.fromtimestamp(stamp)
    clock = dt.strftime("%I:%M %p").lstrip("0")
    day = f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}"
    age = max(0.0, now - stamp)
    ago = ("just now" if age < 90 else f"{int(round(age / 60))} min ago" if age < 5400
           else f"{age / 3600:.1f} h ago" if age < 172800 else f"{int(age // 86400)} days ago")
    return f"{day}, {clock}, {ago}"


@dataclass
class Situation:
    """What one check gathered (each part may be empty: missing memory or an unreadable window is said, never
    guessed)."""

    action: str
    hwnd: int | None = None
    about: str = ""
    hours: float = DEFAULT_HOURS
    title: str = ""
    process: str = ""
    lines: list[str] = field(default_factory=list)
    read_note: str = ""
    person: dict[str, Any] | None = None
    own: list[dict[str, Any]] = field(default_factory=list)
    facts: str = ""
    errors: list[str] = field(default_factory=list)
    ms: float = 0.0
    now: float = field(default_factory=time.time)

    # -- what the model reads -------------------------------------------------------------------------------------

    def evidence_text(self) -> str:
        """The gathered evidence (to be fenced as data read from outside by the caller)."""
        parts: list[str] = []
        if self.hwnd is not None:
            head = f"The window now: [{self.hwnd}] {self.process or '?'} \"{self.title[:90]}\""
            if self.lines:
                parts.append(head + ", its latest content, top to bottom (newest last):\n"
                             + "\n".join(f"- {line}" for line in self.lines))
            else:
                parts.append(head + ": no text could be read from it" + (f" ({self.read_note})" if self.read_note
                                                                        else "") + ".")
        person = self.person or {}
        if person.get("found"):
            rows = [f"{person.get('name')}" + (f" ({person.get('relation')})" if person.get("relation") else "")
                    + ", from memory's capture of their chats:"]
            for r in (person.get("routes") or [])[:3]:
                bits = []
                if r.get("last_mine"):
                    bits.append(f"the user's last message there {when(r['last_mine'], self.now)}")
                if r.get("last_theirs"):
                    bits.append(f"theirs {when(r['last_theirs'], self.now)}")
                rows.append(f"- {r.get('label') or r.get('app') or 'a chat'}: " + ("; ".join(bits) or "no messages"))
                for s in (r.get("samples") or [])[:SAMPLES_PER_ROUTE]:
                    if isinstance(s, dict) and s.get("text"):
                        rows.append(f"  the user wrote, {when(s.get('at'), self.now)}: \"{str(s['text'])[:LINE_CHARS]}\"")
            parts.append("\n".join(rows))
        elif self.about and self.person is not None:
            parts.append(f"Memory has no one called \"{self.about}\" in its people index.")
        if self.own:
            rows = [f"The user's own messages captured in {self.process or 'that app'} in the last "
                    f"{self.hours:g} h, the ones most like what you are about to do first:"]
            for m in self.own[:OWN_MESSAGES]:
                close = m.get("similarity")
                mark = f" (similar to what you are about to send: {close:.2f})" if isinstance(close, float) \
                    and close >= SIMILAR_MARK else ""
                rows.append(f"- {when(m.get('at'), self.now)}: \"{str(m.get('text') or '')[:LINE_CHARS]}\"{mark}")
            parts.append("\n".join(rows))
        if self.facts.strip():
            parts.append(f"Memory's facts of the last {self.hours:g} h near this:\n{self.facts.strip()[:FACTS_CHARS]}")
        if not parts:
            parts.append("Nothing could be read: no window was named and memory has nothing near this.")
        if self.errors:
            parts.append("Could not be checked: " + "; ".join(self.errors[:3]) + ".")
        return "\n\n".join(parts)

    def summary(self) -> str:
        """One line for the run trace and the run card: what was looked at, never the content."""
        bits = []
        if self.hwnd is not None:
            bits.append(f"read the window ({len(self.lines)} latest lines)")
        person = self.person or {}
        if person.get("found"):
            samples = [s for r in (person.get("routes") or [])[:3] for s in (r.get("samples") or [])
                       if isinstance(s, dict)]
            newest = max((float(s.get("at") or 0) for s in samples), default=0.0)
            bits.append(f"memory: {len(samples)} of the user's own messages to {person.get('name')}"
                        + (f", newest {when(newest, self.now)}" if newest else ""))
        if self.own:
            best = max((m.get("similarity") or 0.0 for m in self.own), default=0.0)
            bits.append(f"{len(self.own)} own messages in the app in {self.hours:g} h"
                        + (f", one {best:.2f} similar to this" if best >= SIMILAR_MARK else ""))
        if self.facts.strip():
            bits.append("related facts")
        return "Checked first: " + ("; ".join(bits) if bits else "nothing could be read")

    def facts_dict(self) -> dict[str, Any]:
        """Content-free facts for the log."""
        person = self.person or {}
        return {"hwnd": self.hwnd, "lines": len(self.lines), "person": bool(person.get("found")),
                "samples": sum(len(r.get("samples") or []) for r in (person.get("routes") or [])[:3]),
                "own": len(self.own), "best_similarity": max((m.get("similarity") or 0.0 for m in self.own),
                                                             default=0.0),
                "facts_chars": len(self.facts), "errors": list(self.errors), "ms": self.ms, "hours": self.hours}


# ---------------------------------------------------------------------------
# Gathering (in parallel, bounded)
# ---------------------------------------------------------------------------


def gather(tool_input: dict[str, Any], *, read_tree: Callable[[int], Any] | None,
           process_of: Callable[[int], tuple[str, str]] | None = None,
           memory_call: Callable[..., Any] | None = None,
           recall: Callable[[str, float], str] | None = None,
           now: float | None = None, timeout_s: float = MEMORY_TIMEOUT_S + READ_TIMEOUT_S) -> Situation:
    """Gather one situation check's evidence. Never raises.

    Args:
        tool_input: The call's input (``action``, ``hwnd``, ``about``, ``hours``).
        read_tree: ``read_tree(hwnd)`` -> the window's tree as plain data (``{"title", "process_name", "elements"}``),
            read with a larger budget than an ordinary look.
        process_of: ``process_of(hwnd)`` -> ``(process name, title)`` from the window list, for when the read fails.
        memory_call: ``memory_call(method, *args, timeout_s=...)`` (``MemoryAccess.call_method``): ``contact`` and
            ``sent_messages``.
        recall: ``recall(query, since_epoch)`` -> memory's facts as text.
    """
    from yuki.agent.finish import similar

    started = time.perf_counter()
    now = time.time() if now is None else float(now)
    action = " ".join(str(tool_input.get("action") or "").split())
    about = " ".join(str(tool_input.get("about") or "").split())[:80]
    try:
        hours = float(tool_input.get("hours") or DEFAULT_HOURS)
    except (TypeError, ValueError):
        hours = DEFAULT_HOURS
    hours = min(MAX_HOURS, max(0.25, hours))
    hwnd = tool_input.get("hwnd")
    hwnd = int(hwnd) if isinstance(hwnd, int) and not isinstance(hwnd, bool) and hwnd > 0 else None
    sit = Situation(action=action, hwnd=hwnd, about=about, hours=hours, now=now)
    since = now - hours * 3600.0
    if hwnd is not None and process_of is not None:
        try:
            sit.process, sit.title = process_of(hwnd)
        except Exception:  # noqa: BLE001
            pass
    box: dict[str, Any] = {}

    def read() -> None:
        try:
            tree = read_tree(hwnd) if read_tree is not None and hwnd is not None else None
            if isinstance(tree, dict):
                box["tree"] = tree
        except Exception as exc:  # noqa: BLE001
            box["read_error"] = f"{type(exc).__name__}: {exc}"[:120]

    def person() -> None:
        if not about or memory_call is None:
            return
        try:
            box["person"] = memory_call("contact", about, timeout_s=MEMORY_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            box["person_error"] = f"{type(exc).__name__}"

    def facts() -> None:
        if recall is None or not (about or action):
            return
        try:
            box["facts"] = str(recall(" ".join(x for x in (about, action) if x)[:300], since) or "")
        except Exception as exc:  # noqa: BLE001
            box["facts_error"] = f"{type(exc).__name__}"

    threads = [threading.Thread(target=fn, name=f"yuki-situation-{fn.__name__}", daemon=True)
               for fn in (read, person, facts)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + max(0.5, float(timeout_s))
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    tree = box.get("tree")
    if isinstance(tree, dict):
        sit.lines = latest_lines(tree)
        sit.title = str(tree.get("title") or sit.title or "")
        sit.process = str(tree.get("process_name") or sit.process or "")
        sit.read_note = str(tree.get("note") or "")
    elif hwnd is not None:
        sit.errors.append("the window could not be read" + (f" ({box['read_error']})" if box.get("read_error")
                                                           else " in time"))
    found = box.get("person")
    if isinstance(found, dict):
        sit.person = found
    elif about and memory_call is not None:
        sit.errors.append("memory's people index did not answer")
    sit.facts = str(box.get("facts") or "")
    # The user's own messages in the target's app (it needs the app, known only after the read).
    if memory_call is not None and sit.process:
        try:
            rows = memory_call("sent_messages", sit.process, since, timeout_s=MEMORY_TIMEOUT_S) or []
        except Exception:  # noqa: BLE001
            rows = []
        own = []
        for r in rows:
            if isinstance(r, dict) and str(r.get("text") or "").strip():
                own.append({"text": str(r["text"]), "at": r.get("at") or r.get("first_seen"),
                            "similarity": round(similar(action, str(r["text"])), 3)})
        own.sort(key=lambda m: (-(m["similarity"] or 0.0), -float(m.get("at") or 0)))
        sit.own = own[:OWN_MESSAGES]
    sit.ms = round((time.perf_counter() - started) * 1000, 1)
    return sit


__all__ = ["CHECK_LABEL", "CheckRecord", "QUESTION", "REFUSAL", "SITUATION_TOOL", "Situation", "TOOL_NAME",
           "consequence", "covered", "gather", "latest_lines", "when"]
