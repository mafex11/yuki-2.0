"""The talker's short-term memory (vfix4, 4 Oct 2026): the last hour with the user, and a quick hand on memory.

Live 4 Oct (voice log, content-free): in one conversation the user dictated a to-do list and Yuki added seven to-dos;
that conversation ended on its idle clock, the name started a new one five minutes later, and "what were the to-dos I
told you?" got "I don't have that on record" four times. Each conversation began with no memory of the one before it
(the talker's context starts empty; only the thinker keeps the app run's chat), the grounding lookup leaves Yuki's own
exchanges out by design (``yuki.voice.grounding``), and the talker had nothing to look with, so it said it had nothing.

Two parts, both content (never logged; the voice log gets sizes and counts only):

* :func:`fetch_recent` / :func:`recent_view`: the exchanges of the last hour, across voice conversations and typed
  chat (memory's turn log, every session of the day), newest last, in a tight budget, plus what the user asked Yuki to
  keep in that hour (to-dos put on their list, rules they set). It goes in the talker's system prompt as one block
  after the cached layers (``yuki.voice.mouth.mouth_system_blocks``): fixed for the conversation, so from its second
  call on it is read from the conversation's cache, and the cached layers before it are untouched. A standby
  conversation reads it again when it is woken (it may have been built long before), see
  ``yuki.voice.pipeline.Conversation``.
* :data:`QUICK_MEMORY` / :class:`QuickMemory`: the talker's own quick memory tool: read their to-do list, put items on
  it, read today's talk, or search memory, in a moment, through the memory API calls that exist
  (``MemoryClient.todos`` / ``add_todo`` / ``turns_on`` / ``recall``). Its report says what to tell them, so "what's
  on my list" and "make a to-do list of this" are one turn each, and "I don't have that" comes only after a look.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

#: How far back the start-of-conversation view reaches (s).
RECENT_S = 3600.0
#: The whole view's size cap (characters; the oldest exchanges go first): ~500 tokens at most. Measured 4 Oct with
#: today's profile: the talker's prompt with a full view and the quick memory tool (and no marks section, the Aura
#: voice) is 17.9k tokens against main's 17.7k, the same time to first token; a step near 18k costs +0.3-0.4 s.
RECENT_CHARS = 2_000
#: The kept part (to-dos added, rules set) is cut to this.
KEPT_CHARS = 800
#: One message's longest text in the view (whole words).
USER_CHARS = 200
REPLY_CHARS = 200
#: One kept item's longest text.
KEPT_ITEM_CHARS = 110
#: At most this many of a day's exchanges are read for the view.
RECENT_TURNS = 60
#: Bound on the view's memory read (it runs off the voice loop; a conversation starts without it past this).
RECENT_TIMEOUT_S = 1.5
#: Where the to-do list lives in Yuki's window (the 4 Oct appTodo build: Home's "Your list"; all of them on Work too).
LIST_PLACE = "on Home under Your list in your window"
RECENT_LABEL = ("[Earlier with them, the last hour (voice and typed chat, oldest first): what you and they said. When "
                "they refer back to it, even half-heard (\"the total list\" for the to-do list), answer from it or look "
                "with quick_memory, never \"I don't have that\" without a look. Never bring it up yourself: \"what's "
                "up?\" gets \"Not much, you?\", not a word about it.]")
RECENT_END = "[End of earlier]"

#: The quick memory tool's reads: today's talk is cut to this, a search to this many items.
TODAY_CHARS = 3_500
TODAY_TURNS = 80
SEARCH_ITEMS = 6
SEARCH_ITEM_CHARS = 240
#: How many to-dos the read lists (the rest is counted).
LIST_ITEMS = 15
LIST_ITEM_CHARS = 100
#: Bound on one quick memory read or write.
QUICK_TIMEOUT_S = 4.0
#: The search's wait for the query's embedding (past it the search is keyword only, as the grounding lookup does).
SEARCH_EMBED_WAIT_S = 0.4

QUICK_MEMORY: dict[str, Any] = {
    "name": "quick_memory",
    "description": (
        "Your quick look into memory, alone, no words before it; its report says what to say. todos: their to-do "
        "list. add_todos: put items on it when they ask, one each, their words. today: today's talk with you. "
        "search: memory for query."
    ),
    "properties": {
        "action": {"type": "string", "enum": ["todos", "add_todos", "today", "search"]},
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"},
            "due": {"type": "string", "description": "Only if they gave one: YYYY-MM-DD or YYYY-MM-DD HH:MM"}},
            "required": ["text"]}},
        "query": {"type": "string"},
    },
    "required": ["action"],
}

TODOS_NOTE = ("Their list, read now. Tell them in a few words: how many, then the items briefly, in plain words (a "
              "long list: the first few and offer the rest on a more line). Never ids, never read it like a list.")
EMPTY_LIST_NOTE = "Their list has nothing open, read now. Say so plainly: you looked at their list and it is empty."
ADDED_NOTE = ("Saved and checked: nothing more to do. Tell them in a few words how many you added and where they are: "
              "in your window, on Home under Your list (\"Added {n}, they're on Home under Your list.\"). Never read "
              "them back unless they ask.")
NOT_SAVED_NOTE = ("Not saved: {why}. Say plainly that it did not save{part}, and offer to try again. Never say it is "
                  "on their list.")
FOUND_NOTE = ("For you, not to read out: answer what they asked from it in a few words. When it does not hold what "
              "they mean, say where you looked ({where}) and offer to dig further, which is a hand-over.")
NOTHING_NOTE = ("Nothing found in {where}. Say that plainly, naming where you looked, and offer to dig further "
                "(a hand-over); never that they did not tell you.")


def _clip(text: Any, limit: int) -> str:
    """``text`` on one line, cut after the last whole word that fits in ``limit``."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]
    return head.rstrip(",;:") + " …"


def _stamp(value: Any) -> float | None:
    """Epoch seconds from epoch seconds or an ISO string (memory's two shapes), else ``None``."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip()).timestamp()
        except ValueError:
            return None
    return None


def natural_when(value: Any) -> str:
    """A due date as said to the user: ``Monday 5 October, 5 PM`` (``Monday 5 October`` for a day with no time)."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return text
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone().replace(tzinfo=None)
    day = f"{stamp:%A} {stamp.day} {stamp:%B}"
    if len(text) == 10:   # a date with no time of day
        return day
    hour = stamp.hour % 12 or 12
    clock = f"{hour}:{stamp.minute:02d}" if stamp.minute else f"{hour}"
    return f"{day}, {clock} {'AM' if stamp.hour < 12 else 'PM'}"


def _exchange(turn: dict[str, Any], user_chars: int, reply_chars: int) -> str | None:
    """One exchange as the view shows it, or ``None`` when nothing was answered (talk for others, a silence)."""
    reply = _clip(turn.get("reply"), reply_chars)
    if not reply:
        return None
    at = _stamp(turn.get("at"))
    clock = f"{datetime.fromtimestamp(at):%H:%M} " if at is not None else ""
    user = _clip(turn.get("user"), user_chars)
    if not user:
        return f"{clock}You: {reply}"   # a result said on its own
    return f"{clock}They: {user}\n  You: {reply}"


def _same_exchange(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two logged turns are one exchange recorded twice: the same words (whitespace and case aside) and one
    reply carrying the other's opening (record bookkeeping, never a judgement of what was said)."""
    def norm(value: Any) -> str:
        return " ".join(str(value or "").split()).casefold()

    words_a, words_b = norm(a.get("user")), norm(b.get("user"))
    reply_a, reply_b = norm(a.get("reply")), norm(b.get("reply"))
    if not words_a or words_a != words_b or not reply_a or not reply_b:
        return False
    short, long_ = sorted((reply_a, reply_b), key=len)
    return short[:60] in long_


def _exchanges(turns: list[dict[str, Any]], since: float, until: float, budget: int, *, user_chars: int,
               reply_chars: int) -> list[str]:
    """The exchanges between ``since`` and ``until``, oldest first, the oldest dropped whole past ``budget``."""
    seen: set[Any] = set()
    rows: list[tuple[float, dict[str, Any]]] = []
    for turn in turns or []:
        if not isinstance(turn, dict):
            continue
        at = _stamp(turn.get("at"))
        key = turn.get("id") if turn.get("id") is not None else (at, turn.get("user"))
        if at is None or at < since or at > until or key in seen:
            continue
        seen.add(key)
        rows.append((at, turn))
    rows.sort(key=lambda r: r[0])
    kept: list[dict[str, Any]] = []
    for _at, turn in rows:
        if kept and _same_exchange(kept[-1], turn):
            # One exchange logged twice: the voice turn that handed it over (whose reply already said the outcome)
            # and the thinker's own turn with the same words. Kept once: the fuller record.
            if len(str(turn.get("reply") or "")) > len(str(kept[-1].get("reply") or "")):
                kept[-1] = turn
            continue
        kept.append(turn)
    lines = [line for line in (_exchange(t, user_chars, reply_chars) for t in kept) if line]
    while lines and sum(len(x) + 1 for x in lines) > budget:
        lines.pop(0)
    return lines


def _kept(todos: list[dict[str, Any]], rules: list[dict[str, Any]], since: float, until: float) -> str:
    """What the user asked Yuki to keep between ``since`` and ``until``: to-dos put on their list (not memory's own open loops) and
    rules they set, cut to :data:`KEPT_CHARS`."""
    fresh = [t for t in todos or [] if isinstance(t, dict) and str(t.get("status") or "open") == "open"
             and since <= (_stamp(t.get("since")) or 0.0) <= until and str(t.get("text") or t.get("title") or "").strip()]
    # their own to-dos (added, or told to Yuki), then Yuki's promises; memory's own open loops are not something
    # they asked Yuki to keep
    added = [_clip(t.get("text") or t.get("title"), KEPT_ITEM_CHARS) for t in fresh
             if t.get("source") not in ("loop", "commitment")]
    promised = [_clip(t.get("title") or t.get("text"), KEPT_ITEM_CHARS) for t in fresh
                if t.get("source") == "commitment"]
    set_rules = [_clip(r.get("text") or r.get("quote"), KEPT_ITEM_CHARS) for r in rules or []
                 if isinstance(r, dict) and since <= (_stamp(r.get("at")) or 0.0) <= until
                 and str(r.get("text") or r.get("quote") or "").strip()]
    parts = []
    if added:
        parts.append(f"to-dos put on their list ({len(added)}, {LIST_PLACE}): " + "; ".join(added) + ".")
    if set_rules:
        parts.append("Rules they set: " + "; ".join(set_rules) + ".")
    if promised:
        parts.append("Your promises to them: " + "; ".join(promised) + ".")
    if not parts:
        return ""
    text = _clip("Kept in that hour: " + parts[0], KEPT_CHARS)
    for part in parts[1:]:   # the rest only whole, while it fits
        if len(text) + 1 + len(part) <= KEPT_CHARS:
            text += " " + part
    return text


def recent_view(turns: list[dict[str, Any]], todos: list[dict[str, Any]] | None = None,
                rules: list[dict[str, Any]] | None = None, *, now: float | None = None) -> str | None:
    """The talker's view of the last hour (:data:`RECENT_S`): the exchanges (memory's ``recent_turns`` shape), oldest
    first, and what was kept, at most :data:`RECENT_CHARS`; ``None`` when the hour holds nothing."""
    now = time.time() if now is None else float(now)
    since = now - RECENT_S
    kept = _kept(list(todos or []), list(rules or []), since, now + 60.0)
    budget = RECENT_CHARS - len(RECENT_LABEL) - len(RECENT_END) - len(kept) - 4
    lines = _exchanges(list(turns or []), since, now + 60.0, budget, user_chars=USER_CHARS, reply_chars=REPLY_CHARS)
    if not lines and not kept:
        return None
    return "\n".join([RECENT_LABEL, *lines, *([kept] if kept else []), RECENT_END])


def _days(since: float, until: float) -> list[str]:
    """The user's days (memory's, from its day start) that ``since``..``until`` touches, oldest first."""
    from yuki.memory.checkins import user_day

    out: list[str] = []
    for at in (since, until):
        day = user_day(at)
        if day not in out:
            out.append(day)
    return out


def _turns(client: Any, since: float, until: float, limit: int) -> list[dict[str, Any]]:
    """Memory's exchanges of the days ``since``..``until`` touches (``MemoryClient.turns_on``), ``[]`` without it."""
    fn = getattr(client, "turns_on", None)
    if not callable(fn):
        return []
    out: list[dict[str, Any]] = []
    for day in _days(since, until):
        out.extend(row for row in (fn(day, limit=limit) or []) if isinstance(row, dict))
    return out


def fetch_recent(memory: Any, *, now: float | None = None, timeout_s: float = RECENT_TIMEOUT_S) -> str | None:
    """:func:`recent_view` read from memory now (blocking, bounded by ``timeout_s``; call it off the voice loop).

    ``memory`` is :class:`~yuki.agent.memory.MemoryAccess` (its ``call``). Never raises: ``None`` without memory, on a
    failure, past the bound, or when the hour holds nothing."""
    call = getattr(memory, "call", None)
    if not callable(call):
        return None
    now = time.time() if now is None else float(now)

    def read(client: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        turns = _turns(client, now - RECENT_S, now, RECENT_TURNS)
        todos = client.todos() if callable(getattr(client, "todos", None)) else []
        rules = client.rules() if callable(getattr(client, "rules", None)) else []
        return turns, list(todos or []), list(rules or [])

    try:
        turns, todos, rules = call("recent talk", read, timeout_s=timeout_s)
    except Exception:  # noqa: BLE001 - the talker starts without it
        return None
    return recent_view(turns, todos, rules, now=now)


# ---------------------------------------------------------------------------------------------------------------------
# The quick memory tool
# ---------------------------------------------------------------------------------------------------------------------


def todo_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The report of a to-do list read: its open items (the title memory wrote when there is one), due dates said
    naturally, at most :data:`LIST_ITEMS` (the rest counted)."""
    open_rows = [r for r in rows or [] if isinstance(r, dict) and str(r.get("status") or "open") == "open"
                 and str(r.get("text") or r.get("title") or "").strip()]
    if not open_rows:
        return {"list": "Nothing open.", "how_to_handle_it": EMPTY_LIST_NOTE}
    lines = []
    for row in open_rows[:LIST_ITEMS]:
        words = _clip(row.get("title") or row.get("text"), LIST_ITEM_CHARS)
        notes = [f"due {natural_when(row['due'])}"] if row.get("due") else []
        if row.get("source") in ("commitment",):
            notes.append("your promise to them")
        lines.append(f"- {words}" + (f" ({'; '.join(notes)})" if notes else ""))
    more = len(open_rows) - len(lines)
    head = f"{len(open_rows)} open, {LIST_PLACE}:"
    tail = [f"(and {more} more)"] if more > 0 else []
    return {"list": "\n".join([head, *lines, *tail]), "how_to_handle_it": TODOS_NOTE}


def _search_lines(hits: list[dict[str, Any]]) -> list[str]:
    out = []
    for hit in hits or []:
        if not isinstance(hit, dict) or not str(hit.get("fact") or hit.get("text") or "").strip():
            continue
        at = _stamp(hit.get("at"))
        when = f"{datetime.fromtimestamp(at):%a %d %b %H:%M}" if at is not None else "undated"
        note = " (a joke, not a fact)" if hit.get("nature") == "humor" else (
            " (maybe misheard)" if hit.get("nature") == "uncertain" else "")
        out.append(f"- ({hit.get('kind') or 'memory'}, {when}){note} "
                   f"{_clip(hit.get('fact') or hit.get('text'), SEARCH_ITEM_CHARS)}")
        if len(out) >= SEARCH_ITEMS:
            break
    return out


class QuickMemory:
    """The ``quick_memory`` tool's handler: one memory read or write off the voice loop, its report for the talker.

    ``memory`` is :class:`~yuki.agent.memory.MemoryAccess`. ``log`` (the voice log) gets one content-free event per
    use: the action, whether it worked, how long it took and how many items."""

    def __init__(self, memory: Any, *, log: Any = None, session: str | None = None) -> None:
        self.memory = memory
        self.log = log
        self.session = session

    async def handle(self, params: Any) -> None:
        args = params.arguments if isinstance(params.arguments, dict) else {}
        started = time.monotonic()
        try:
            payload, facts = await asyncio.to_thread(self.run, args)
        except Exception as exc:  # noqa: BLE001 - a failed look is said as one
            payload = {"failed": f"memory could not be read ({type(exc).__name__})",
                       "how_to_handle_it": NOTHING_NOTE.format(where="memory")}
            facts = {"ok": False}
        if self.log is not None:
            self.log.event("quick_memory", session=self.session, op=str(args.get("action") or ""),
                           ms=round((time.monotonic() - started) * 1000), call=getattr(params, "tool_call_id", None),
                           **facts)
        await params.result_callback(payload)

    def run(self, args: dict[str, Any], *, now: float | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """``(report for the talker, content-free facts)`` for one use (blocking)."""
        action = str(args.get("action") or "").strip()
        if action == "todos":
            return self._todos()
        if action == "add_todos":
            return self._add(args.get("items"))
        if action == "today":
            return self._today(time.time() if now is None else float(now))
        if action == "search":
            return self._search(str(args.get("query") or "").strip())
        return ({"failed": f"no such action {action!r}", "how_to_handle_it": "Call it again with one of its actions."},
                {"ok": False})

    def _todos(self) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            rows = self.memory.todos(include_done=False, timeout_s=QUICK_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            return ({"failed": f"their list could not be read ({type(exc).__name__})",
                     "how_to_handle_it": "Say plainly you couldn't read their list just now, and offer to try again."},
                    {"ok": False})
        report = todo_report(rows)
        return report, {"ok": True, "items": sum(1 for r in rows if str(r.get("status") or "open") == "open")}

    def _add(self, items: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        wanted = []
        for item in items if isinstance(items, list) else []:
            text = item.get("text") if isinstance(item, dict) else item
            text = " ".join(str(text or "").split())
            if text:
                due = item.get("due") if isinstance(item, dict) else None
                wanted.append((text, str(due).strip() if due and str(due).strip() else None))
        if not wanted:
            return ({"failed": "no items were given", "how_to_handle_it": (
                "Nothing was added. Call it again with each to-do as one item, or ask them what to add.")},
                    {"ok": False, "items": 0})
        added, why = [], ""
        for text, due in wanted:
            try:
                self.memory.add_todo(text, due, timeout_s=QUICK_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                cause = getattr(exc, "__cause__", None)
                if due and isinstance(cause, ValueError):
                    try:   # a due memory cannot read: the to-do is kept without it, and the report says so
                        self.memory.add_todo(text, None, timeout_s=QUICK_TIMEOUT_S)
                        added.append(f"{text} (saved without its time: it could not be read)")
                        continue
                    except Exception as again:  # noqa: BLE001
                        exc = again
                why = why or f"memory did not take it ({type(exc).__name__})"
                continue
            added.append(text)
        facts = {"ok": bool(added), "items": len(added), "asked": len(wanted)}
        if not added:
            return {"failed": why, "how_to_handle_it": NOT_SAVED_NOTE.format(why=why, part="")}, facts
        report: dict[str, Any] = {"added": len(added), "where": LIST_PLACE, "items": added,
                                  "how_to_handle_it": ADDED_NOTE.format(n=len(added))}
        if len(added) < len(wanted):
            report["not_saved"] = len(wanted) - len(added)
            report["how_to_handle_it"] += " " + NOT_SAVED_NOTE.format(
                why=why, part=f" for {len(wanted) - len(added)} of them")
        return report, facts

    def _today(self, now: float) -> tuple[dict[str, Any], dict[str, Any]]:
        from yuki.memory.checkins import day_boundary, user_day

        call = getattr(self.memory, "call", None)
        day = user_day(now)
        try:
            turns = call("today's talk", lambda c: _turns(c, day_boundary(day), now, TODAY_TURNS),
                         timeout_s=QUICK_TIMEOUT_S) if callable(call) else []
        except Exception as exc:  # noqa: BLE001
            return ({"failed": f"today's talk could not be read ({type(exc).__name__})",
                     "how_to_handle_it": NOTHING_NOTE.format(where="today's chat")}, {"ok": False})
        lines = _exchanges(turns, day_boundary(day), now + 60.0, TODAY_CHARS, user_chars=USER_CHARS,
                           reply_chars=REPLY_CHARS)
        if not lines:
            return {"found": "", "how_to_handle_it": NOTHING_NOTE.format(where="today's chat")}, {"ok": True, "items": 0}
        return ({"found": "Today with you, oldest first:\n" + "\n".join(lines),
                 "how_to_handle_it": FOUND_NOTE.format(where="today's chat")}, {"ok": True, "items": len(lines)})

    def _search(self, query: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not query:
            return ({"failed": "no query", "how_to_handle_it": "Call it again with what to look for."}, {"ok": False})
        try:
            hits = self.memory.call_method("recall", query, limit=SEARCH_ITEMS * 2, embed_wait_s=SEARCH_EMBED_WAIT_S,
                                           timeout_s=QUICK_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            return ({"failed": f"memory could not be searched ({type(exc).__name__})",
                     "how_to_handle_it": NOTHING_NOTE.format(where="memory")}, {"ok": False})
        lines = _search_lines(list(hits or []))
        if not lines:
            return {"found": "", "how_to_handle_it": NOTHING_NOTE.format(where="a memory search")}, {"ok": True,
                                                                                                    "items": 0}
        return ({"found": "\n".join(lines), "how_to_handle_it": FOUND_NOTE.format(where="a memory search")},
                {"ok": True, "items": len(lines)})


__all__ = ["QUICK_MEMORY", "QuickMemory", "RECENT_LABEL", "fetch_recent", "natural_when", "recent_view",
           "todo_report"]
