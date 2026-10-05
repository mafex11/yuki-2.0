"""Rules every card writer follows, in one place, and the one list of deadlines a card may name.

Report 76 (5 Oct 2026, cards 28 Sep-5 Oct): about 23 of 58 cards relayed AI agents' chatter (a coding agent's
progress, its reports, its questions), 6-7 of them after the 3 Oct fix that taught only the questions; and one
deadline came up on 5 cards in 24 hours that contradicted each other ("due tomorrow" against "overdue since 30
September"): each writer read a date from a different place (a to-do, an open loop's words, a personal event, a
thread) and worked out "due" or "overdue" on its own.

* :data:`AGENT_WORK_RULE` - model judgement, with worked examples, shared by the coach, the card writer, the welcome
  back and the observation cards (yuki.memory.nudges, yuki.memory.presence). No word list: the model tells an AI
  tool's chatter from the user's own work.
* :data:`DEADLINE_RULE` and :func:`deadline_lines` - the DEADLINES block: every dated item memory holds (to-dos and
  commitments with a due time, goals with a due time, the user's personal events), each with its status worked out here,
  in code, from one clock ("due tomorrow, Monday 5 October", "overdue since Tuesday 30 September"; an event is never
  due: "tomorrow, Monday 5 October, 7 PM", "was on Saturday 3 October"). A card names a
  deadline only from it; a date inside a loop, a thread, a page or a journal fact is what was said then.

Examples use invented projects and names.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

AGENT_WORK_RULE = """\
AN AI TOOL'S WORK IS NOT NEWS FOR THE USER. The user runs AI coding agents and builders (T3 Code, Claude Code, \
Codex, a Warp agent, ChatGPT, "builders" working on Yuki itself). What such a tool did, is doing, plans, reported, \
finished, shipped or merged, what it is waiting on and what it asked the user is that tool's own chatter: the user \
watches it there. A card never relays or reports it, never chases the user to answer the tool, and never rests on \
it. The user's own work stays fair game: their project, their deadline, a thread of theirs, named as theirs from \
what they did, never as a tool's progress.
- none: "T3 Code wrapped the logo research and recommends option 3" - a tool's report relayed.
- none: "Those builders were still grinding on the voice fixes when you stepped away" - a tool's progress.
- none: "T3 Code is waiting on your go for the campaign text" - chasing the user on a tool's behalf.
- none: "Builder 2 merged as v3.17, want the status pulled up?" - a tool's status report, with an offer on it.
- fine: "You left the invoice export half done last night; the date column fix is still open" - the user's own \
work, from what they did."""

DEADLINE_RULE = """\
DEADLINES. A card names a deadline only from the DEADLINES block, with its status words exactly as written there \
("due tomorrow, Monday 5 October"; "overdue since Tuesday 30 September"). The user's personal events there (a \
dinner, an appointment) are things that happen, never due or overdue: "tomorrow, Monday 5 October, 7 PM"; "was on \
Saturday 3 October" (it has happened). A date inside an open loop, a thread, a page, a journal fact or a call is \
what was said then and may have moved: never call it due, tomorrow or overdue yourself. When two DEADLINES lines \
are plainly the same deadline with different dates, the one stated most recently holds; when you cannot tell, \
leave the deadline out. For example "- [t4] Send the venue deposit (overdue since Tuesday 30 September; stated \
Monday 29 September)" and "- [g2] Pay the venue deposit (due tomorrow, Tuesday 7 October; stated Saturday 4 \
October)" are one deadline: it is due tomorrow. DEADLINES says "(none)": name no deadline."""


#: Where the rules go in a card writer's prompt: before its untrusted-data paragraph (which every one of them has).
_UNTRUSTED_HEAD = "Everything between the BEGIN_UNTRUSTED_DATA"


def with_card_rules(prompt: str, *rules: str) -> str:
    """``prompt`` with ``rules`` (default: both) put in before its untrusted-data paragraph (at the end without one)."""
    block = "\n\n".join(rules or (AGENT_WORK_RULE, DEADLINE_RULE))
    head, sep, tail = prompt.partition(_UNTRUSTED_HEAD)
    if not sep:
        return f"{prompt}\n\n{block}"
    return f"{head}{block}\n\n{sep}{tail}"


def _day_words(when: datetime, *, with_time: bool) -> str:
    """``"Monday 5 October"`` (``"Monday 5 October, 9 AM"``): the user's natural dates."""
    text = f"{when:%A} {when.day} {when:%B}"
    if with_time:
        hour = when.strftime("%I").lstrip("0") or "12"
        text += f", {hour} {when:%p}" if when.minute == 0 else f", {hour}:{when:%M} {when:%p}"
    return text


def deadline_status(due_at: float, now: float, *, all_day: bool) -> str:
    """The status words of one deadline, from one clock: ``"due today, ..."``, ``"due tomorrow, ..."``, ``"due in 3
    days, ..."``, ``"overdue since ..."``. An all-day item is overdue only once its day is over."""
    when = datetime.fromtimestamp(due_at)
    days = (when.date() - datetime.fromtimestamp(now).date()).days
    said = _day_words(when, with_time=not all_day)
    if days < 0 or (not all_day and due_at < now):
        return f"overdue since {said}"
    if days == 0:
        return f"due today, {said}"
    if days == 1:
        return f"due tomorrow, {said}"
    return f"due in {days} days, {said}"


def event_status(at: float, now: float, *, all_day: bool) -> str:
    """The words of one of the user's personal events (a dinner, an appointment): never due or overdue, it is something
    that happens. ``"today, ..."``, ``"tomorrow, ..."``, ``"in 3 days, ..."`` while it is ahead; ``"earlier today,
    ..."`` and ``"was on ..."`` once it is past (review F2)."""
    when = datetime.fromtimestamp(at)
    days = (when.date() - datetime.fromtimestamp(now).date()).days
    said = _day_words(when, with_time=not all_day)
    if days < 0:
        return f"was on {said}"
    if days == 0:
        return f"earlier today, {said}" if not all_day and at < now else f"today, {said}"
    if days == 1:
        return f"tomorrow, {said}"
    return f"in {days} days, {said}"


def deadline_lines(store: Any, items: Sequence[Any], now: float, safe: Callable[[Any, int], str], *,
                   limit: int = 12, notes: dict[str, str] | None = None) -> list[str]:
    """The DEADLINES block (with its heading): every open dated item - ``items`` (yuki.memory.nudges.todo_items: to-dos,
    commitments), the open goals with a due time, the user's personal events not yet past by a week (in their own words,
    :func:`event_status`) - soonest first, each ``- [id] what (status words; stated <day>)``. ``notes`` adds words to an
    item's line by its ref (the welcome back's "in DUE NOW above"). ``["DEADLINES:", "(none)"]`` when there is none.
    Never raises."""
    rows: list[tuple[float, str]] = []
    notes = notes or {}
    for item in items:
        due = getattr(item, "due_at", None)
        if getattr(item, "status", "") != "open" or due is None:
            continue
        stated = getattr(item, "since", None)
        status = deadline_status(float(due), now, all_day=bool(item.all_day))
        if notes.get(item.ref):
            status = f"{status}; {notes[item.ref]}"
        rows.append((float(due), _line(item.ref, item.text, status, stated, safe)))
    try:
        for g in store.goals(("open", "unknown"), limit=20):
            if g.due_at is None:   # a goal for a day is what they are going for, not a deadline
                continue
            due = float(g.due_at)
            rows.append((due, _line(g.ref, g.text, deadline_status(due, now, all_day=False),
                                    getattr(g, "created_at", None), safe)))
    except Exception:
        pass
    for ev in _events(store):
        at = ev.get("at")
        if not at or float(at) < now - 7 * 86400.0:
            continue
        rows.append((float(at), _line(f"event:{ev.get('id')}", ev.get("what"),
                                      event_status(float(at), now, all_day=bool(ev.get("all_day"))),
                                      ev.get("found_at"), safe)))
    rows.sort(key=lambda r: r[0])
    return ["DEADLINES:"] + ([line for _due, line in rows[:limit]] or ["(none)"])


def _line(ref: str, text: Any, status: str, stated: Any, safe: Callable[[Any, int], str]) -> str:
    when = ""
    try:
        if stated:
            when = f"; stated {_day_words(datetime.fromtimestamp(float(stated)), with_time=False)}"
    except (TypeError, ValueError, OSError, OverflowError):
        when = ""
    return f"- [{ref}] {safe(text, 200)} ({status}{when})"


def _events(store: Any) -> list[dict[str, Any]]:
    """The user's dated personal events the observation cards found (yuki.memory.presence, encrypted state)."""
    try:
        from yuki.memory.presence import EVENTS_STATE

        raw = store.nudge_state(EVENTS_STATE)
        data = json.loads(store.cipher.decrypt(raw)) if raw else []
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except Exception:
        return []


__all__ = ["AGENT_WORK_RULE", "DEADLINE_RULE", "deadline_lines", "deadline_status", "event_status", "with_card_rules"]
