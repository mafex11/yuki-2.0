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
#: Bound on a whole voice add, every item together (Pipecat gives a tool call 20 s; the talker's reply follows it).
ADD_BUDGET_S = 10.0
#: The search's wait for the query's embedding (past it the search is keyword only, as the grounding lookup does).
SEARCH_EMBED_WAIT_S = 0.4

QUICK_MEMORY: dict[str, Any] = {
    "name": "quick_memory",
    "description": (
        "Look into memory, after a few words in the same reply (\"Let me look.\"). todos: their to-dos. add_todos: put "
        "items on it when they ask, one each, their words. today: today's chat. search: memory for query."
    ),
    "properties": {
        "action": {"type": "string", "enum": ["todos", "add_todos", "today", "search"]},
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"},
            "due": {"type": "string", "description": "If given: YYYY-MM-DD[ HH:MM]"}},
            "required": ["text"]}},
        "query": {"type": "string"},
    },
    "required": ["action"],
}

TODOS_NOTE = ("Their list, read now. Tell them in a few words: how many, then the items briefly, in plain words (a "
              "long list: the first few and offer the rest on a more line). Never ids, never read it like a list.")
EMPTY_LIST_NOTE = ("Their list has nothing open, read now. Say plainly you looked at their list and it is empty; never "
                   "that they did not tell you. When they say they told you something, say the list is empty and offer "
                   "to look through today's chat for it (quick_memory today) or dig further (a hand-over).")
ADDED_NOTE = ("Saved and checked: nothing more to do. Say: \"Added {n}, they're on Home under Your list.\" (that, or as "
              "short in your words, always with how many and where: Home, Your list). Never read them back unless "
              "they ask.")
#: leftVoice: a clean add (every item saved, with its due when it had one) is confirmed by code, in these words, the
#: moment memory has it, without waiting for the talker (its first token alone is ~1.2-1.5 s). Formatting: a count and
#: the place.
#: Only for an add alone in its reply (second review LVV-1: a reply's calls are answered once, after the last).
ADDED_ONE_LINE = "Added it, it's on Home under Your list."
ADDED_LINE = "Added {n}, they're on Home under Your list."
#: Third check LVV-1: the code line no longer ends the turn on its own. When the same words asked for more ("add call
#: mom, and what time is it?", judged by :func:`rest_asked`), the talker runs once more behind it (its words queue
#: after the line's audio; the line is in its context as words said before the call) and does the rest, or stays
#: quiet. Ending the turn used to rest on the talker writing the
#: rest into a ``then`` field, and live it left that field empty about one add-and-ask in three (the time never given).
#: Text replays 4 Oct: with the line before the call and this note, 25 of 25 replies to an add-and-ask gave only the
#: rest, and 20 of 20 with nothing more asked were stay_quiet; with the line only in the report, 0 of 10 and 1 of 10
#: (it said the add again, or chatted about the screen).
ADDED_SAID_NOTE = ("Done, and already said aloud: your words before this call, word for word. Nothing about the add is "
                   "left to say. Reread their last words: if they also asked something besides the add (a question, "
                   "another request) that you have not answered yet, answer only that, in a few words. If the add was "
                   "all they asked, your whole reply is the stay_quiet call alone: no words, no offer, no remark on "
                   "anything else.")
#: Third check LVV-1, live: a talker run after every code-said add wandered when nothing more was asked (offline
#: 4 Oct, 6 of 11 plain adds: "What's up?", "The walkthrough's still playing, want me to pause it?", and once it
#: paused the video), whatever its report said. So one cheap, isolated check by the fast model (no desk, no tools, as
#: the speech guard's) decides whether the same words asked for anything the reply has not answered yet; only then
#: does the talker run. A model's judgement from worked examples, never a list of words.
ADD_REST_PROMPT = """\
You check one moment in a voice conversation. The user spoke to Yuki, their voice assistant. Yuki has just put \
what they asked on their to-do list and said so aloud. Decide whether their words ALSO asked for something else \
that Yuki has not answered or done yet in the words it already said: a question, another request, anything \
beyond putting things on the list. Answer one word: MORE (something else still waits for Yuki) or DONE (the list \
was all of it, or the rest was already answered in Yuki's words).
Their words: "Add call mom to my list, and what time is it?" Yuki said: "Added it, it's on Home under Your list." \
-> MORE
Their words: "Add call mom to my list, and what time is it?" Yuki said: "It's 11:35 PM. Adding it now. Added it, \
it's on Home under Your list." -> DONE
Their words: "Put milk and eggs on my list." Yuki said: "On it. Added 2, they're on Home under Your list." -> DONE
Their words: "Add renew the gym pass to my to-do list, then pause the music." Yuki said: "Added it, it's on Home \
under Your list." -> MORE
Their words: "Remind me to call the bank tomorrow, thanks." Yuki said: "Added it, it's on Home under Your list." \
-> DONE
Their words: "Add the plumber to my list. Also what did I tell you about the gym?" Yuki said: "Sure. Added it, \
it's on Home under Your list." -> MORE"""
#: How long the check may take; no verdict in time (or an error) runs the talker: never silent on a question.
ADD_REST_TIMEOUT_S = 2.5


def parse_rest(answer: str) -> bool:
    """Whether the check's answer says something more waits (anything but a plain DONE does: never silent)."""
    words = " ".join(str(answer or "").split()).upper()
    return not (words.startswith("DONE") and "MORE" not in words)


async def rest_asked(client: Any, model: str, *, user: str, said: str,
                     timeout_s: float = ADD_REST_TIMEOUT_S) -> tuple[bool, str]:
    """``(more, how)``: whether ``user``'s words asked for more than a clean add that ``said`` has not answered yet.

    ``how`` is ``model``, ``timeout`` or ``error`` (both of those count as more: the talker runs and decides), or
    ``no_words`` when there are no words of theirs to read (more). One short call to the fast model, no tools."""
    if not str(user or "").strip():
        return True, "no_words"
    content = f'Their words: "{" ".join(str(user).split())}" Yuki said: "{" ".join(str(said or "").split())}" ->'
    try:
        response = await asyncio.wait_for(client.messages.create(
            model=model, max_tokens=4, system=ADD_REST_PROMPT, messages=[{"role": "user", "content": content}]),
            timeout_s)
    except asyncio.TimeoutError:
        return True, "timeout"
    except Exception:  # noqa: BLE001 - a failed check runs the talker
        return True, "error"
    answer = "".join(getattr(b, "text", "") for b in getattr(response, "content", []) or [])
    return parse_rest(answer), "model"


#: An item kept without the due it was given (memory could not read the due): the talker says so, not code.
NO_TIME_MARK = " (saved without its time: it could not be read)"
NOT_SAVED_NOTE = ("Not saved: {why}. Say plainly that it did not save{part}, and offer to try again. Never say it is "
                  "on their list.")
MAYBE_SAVED_NOTE = ("Memory was too slow to answer, so {n} of them may or may not have saved. Say so plainly; before "
                    "adding anything again, read their list (quick_memory todos) so nothing goes on it twice.")
FOUND_NOTE = ("For you, not to read out: answer what they asked from it in a few words. When it does not hold what "
              "they mean, say where you looked ({where}) and offer to dig further, which is a hand-over.")
NOTHING_NOTE = ("Nothing found in {where}. Say that plainly, naming where you looked, and offer to dig further "
                "(a hand-over); never that they did not tell you.")
WHOSE_NOTE = ("A hit marked as another AI tool's text, or whose from line says someone else said it, is never theirs: "
              "never give it as their own words or plans. When none is theirs, say what you found and whose it is "
              "(\"someone on your Discord call said that, not you\"), and offer to dig further.")


class _OutOfTime(Exception):
    """A voice add ran out of time: ``sent`` when the write was made and got no answer (it may still commit)."""

    def __init__(self, *, sent: bool) -> None:
        super().__init__("out of time")
        self.sent = sent


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
    """One exchange as the view shows it, or ``None`` when it was not part of the talk with Yuki: said to others (the
    talker stayed quiet, or memory's extractor judged so), a call line nobody has judged yet ("unanswered"), a
    silence. Words said to Yuki that got no spoken reply stay (report 77b): a request Yuki only acted on, and the first
    part of a request cut off by its own next words, which the next exchange answers."""
    reply = _clip(turn.get("reply"), reply_chars)
    at = _stamp(turn.get("at"))
    clock = f"{datetime.fromtimestamp(at):%H:%M} " if at is not None else ""
    user = _clip(turn.get("user"), user_chars)
    if not reply:
        if not user or turn.get("addressed") != "yuki":
            return None
        acts = [" ".join(str(a).split()) for a in turn.get("actions") or [] if str(a).strip()]
        if acts:
            return f"{clock}They: {user}\n  You (no words): {_clip('did ' + '; '.join(acts), reply_chars)}"
        return f"{clock}They: {user}"
    if not user:
        return f"{clock}You: {reply}"   # a result said on its own
    return f"{clock}They: {user}\n  You: {reply}"


def _same_exchange(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two logged turns are one exchange recorded twice: the same words (whitespace and case aside) and one
    reply carrying the other's opening, or only one of them with a reply (record bookkeeping, never a judgement of
    what was said)."""
    def norm(value: Any) -> str:
        return " ".join(str(value or "").split()).casefold()

    words_a, words_b = norm(a.get("user")), norm(b.get("user"))
    reply_a, reply_b = norm(a.get("reply")), norm(b.get("reply"))
    if not words_a or words_a != words_b:
        return False
    if bool(reply_a) != bool(reply_b):
        return True   # the same words twice, once with no reply (a hand-off with no words, then the worker's turn; 77b)
    if not reply_a:
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


def fetch_recent(memory: Any, *, now: float | None = None, timeout_s: float = RECENT_TIMEOUT_S,
                 failed: Any = None) -> Any:
    """:func:`recent_view` read from memory now (blocking, bounded by ``timeout_s``; call it off the voice loop).

    ``memory`` is :class:`~yuki.agent.memory.MemoryAccess` (its ``call``). Never raises: ``None`` when the hour holds
    nothing; ``failed`` (default ``None``) without memory, on a failure or past the bound, so a caller that already has
    a view can keep it rather than take a failed read for an empty hour (review F4)."""
    call = getattr(memory, "call", None)
    if not callable(call):
        return failed
    now = time.time() if now is None else float(now)

    def read(client: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        turns = _turns(client, now - RECENT_S, now, RECENT_TURNS)
        todos = client.todos() if callable(getattr(client, "todos", None)) else []
        rules = client.rules() if callable(getattr(client, "rules", None)) else []
        return turns, list(todos or []), list(rules or [])

    try:
        turns, todos, rules = call("recent talk", read, timeout_s=timeout_s)
    except Exception:  # noqa: BLE001 - the talker starts without it
        return failed
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


#: Marks a hit that is text typed into, or read from, another AI tool (``recall``'s ``other_ai``), as the thinker's
#: formatter and the MCP server mark it.
OTHER_AI_MARK = " [text for or from another AI tool - not a fact about the user]"
#: One hit's source line (where, when, whose words; the quote last, so a cut takes the quote first) is cut to this.
SOURCE_CHARS = 160


def _search_lines(hits: list[dict[str, Any]]) -> list[str]:
    """A memory search's hits for the talker: dated, a joke or an uncertain item marked, text for or from another AI
    tool marked, and where each came from (``recall``'s ``source``: whose words, which app), so someone else's words
    on a call or another AI's text are never given as the user's own (review F1)."""
    out = []
    for hit in hits or []:
        if not isinstance(hit, dict) or not str(hit.get("fact") or hit.get("text") or "").strip():
            continue
        at = _stamp(hit.get("at"))
        when = f"{datetime.fromtimestamp(at):%a %d %b %H:%M}" if at is not None else "undated"
        note = " (a joke, not a fact)" if hit.get("nature") == "humor" else (
            " (maybe misheard)" if hit.get("nature") == "uncertain" else "")
        mark = OTHER_AI_MARK if hit.get("other_ai") else ""
        source = _clip(hit.get("source"), SOURCE_CHARS)
        out.append(f"- ({hit.get('kind') or 'memory'}, {when}){note} "
                   f"{_clip(hit.get('fact') or hit.get('text'), SEARCH_ITEM_CHARS)}{mark}"
                   + (f"\n  from: {source}" if source else ""))
        if len(out) >= SEARCH_ITEMS:
            break
    return out


def added_line(report: dict[str, Any]) -> str | None:
    """The words that confirm an add by code, or ``None`` when the talker should say it (leftVoice).

    Only a clean add: every item saved, none kept without the due it was given, nothing unsure or unsaved. Anything
    else needs the talker's words (what did not save, what to do next). Facts of the report's own bookkeeping."""
    items = report.get("items") if isinstance(report, dict) else None
    n = report.get("added") if isinstance(report, dict) else None
    if not isinstance(n, int) or n < 1 or not isinstance(items, list) or len(items) != n:
        return None
    if report.get("may_have_saved") or report.get("not_saved") or report.get("failed"):
        return None
    if any(str(item).endswith(NO_TIME_MARK) for item in items):
        return None
    return ADDED_ONE_LINE if n == 1 else ADDED_LINE.format(n=n)


class QuickMemory:
    """The ``quick_memory`` tool's handler: one memory read or write off the voice loop, its report for the talker.

    ``memory`` is :class:`~yuki.agent.memory.MemoryAccess`. ``log`` (the voice log) gets one content-free event per
    use: the action, whether it worked, how long it took and how many items."""

    def __init__(self, memory: Any, *, log: Any = None, session: str | None = None,
                 alone: Any = None, mark_spoken: Any = None) -> None:
        self.memory = memory
        self.log = log
        self.session = session
        #: ``alone(tool_call_id)``: the call was the only one of its reply (the talker service's ``_yuki_solo``);
        #: ``None`` takes every call as alone.
        self.alone = alone
        #: ``mark_spoken(tool_call_id)``: words were said for this call (a clean add's line, said by code), as the
        #: bridge's ``mark_spoken``: the talker service then keeps them before the call in the conversation.
        self.mark_spoken = mark_spoken

    async def handle(self, params: Any) -> None:
        args = params.arguments if isinstance(params.arguments, dict) else {}
        started = time.monotonic()
        try:
            payload, facts = await asyncio.to_thread(self.run, args)
        except Exception as exc:  # noqa: BLE001 - a failed look is said as one
            payload = {"failed": f"memory could not be read ({type(exc).__name__})",
                       "how_to_handle_it": NOTHING_NOTE.format(where="memory")}
            facts = {"ok": False}
        adding = str(args.get("action") or "").strip() == "add_todos"
        line = added_line(payload) if adding else None
        push = getattr(getattr(params, "llm", None), "push_frame", None)
        # Second review LVV-1: said by code only for an add alone in its reply. A reply with another call beside it is
        # answered once, after its last report, by Pipecat; the talker says the add in that answer.
        call_id = str(getattr(params, "tool_call_id", "") or "")
        alone = self.alone is None or self.alone(call_id)
        said = bool(line and callable(push) and alone)
        if self.log is not None:
            self.log.event("quick_memory", session=self.session, op=str(args.get("action") or ""),
                           ms=round((time.monotonic() - started) * 1000), call=getattr(params, "tool_call_id", None),
                           **facts, **({"said_by": "code"} if said else {}), **({} if alone else {"alone": False}))
        if said:
            # leftVoice: a clean add is confirmed by code in Yuki's voice the moment memory has it, as a hand's done
            # line is (waiting for the talker's words was ~1.5 s of silence before "Added ..."); its words go into the
            # conversation as Yuki's own. Third check LVV-1: the call settles as any report does, so what else the
            # same words asked is answered; the conversation (``_quick_memory_call``) first asks :func:`rest_asked`
            # whether there is more, and runs the talker on its own copy only then (its words queue behind the line).
            from pipecat.frames.frames import TTSSpeakFrame

            await push(TTSSpeakFrame(text=line))
            if self.mark_spoken is not None:
                self.mark_spoken(call_id)   # the line goes before the call in the conversation (``chronological``)
            await params.result_callback({**payload, "said": line, "how_to_handle_it": ADDED_SAID_NOTE})
            return
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
        # Review F5: the whole add has one budget (:data:`ADD_BUDGET_S`, well inside the tool's own timeout), and it
        # stops at the first write that ran out of time: that write may still finish on memory's own thread, so it
        # is reported as "may have saved" (check the list before adding again), never as "not saved".
        deadline = time.monotonic() + ADD_BUDGET_S
        added, why, unsure, untried = [], "", 0, 0

        def write(text: str, due: str | None) -> None:
            bound = min(QUICK_TIMEOUT_S, deadline - time.monotonic())
            if bound <= 0.05:
                raise _OutOfTime(sent=False)
            started = time.monotonic()
            try:
                self.memory.add_todo(text, due, timeout_s=bound)
            except Exception as exc:  # noqa: BLE001
                # no answer within the bound (memory's own error carries no cause then): it may yet commit
                if getattr(exc, "__cause__", None) is None and time.monotonic() - started >= bound * 0.95:
                    raise _OutOfTime(sent=True) from None
                raise

        for i, (text, due) in enumerate(wanted):
            try:
                try:
                    write(text, due)
                    added.append(text)
                except _OutOfTime:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if not (due and isinstance(getattr(exc, "__cause__", None), ValueError)):
                        raise
                    # a due memory cannot read: the to-do is kept without it, and the report says so
                    write(text, None)
                    added.append(text + NO_TIME_MARK)
            except _OutOfTime as out:
                # the write that ran out may finish on its own; one that never started did not
                unsure = 1 if out.sent else 0
                untried = len(wanted) - i - unsure
                why = why or "memory was too slow to answer"
                break
            except Exception as exc:  # noqa: BLE001
                why = why or f"memory did not take it ({type(exc).__name__})"
        not_saved = len(wanted) - len(added) - unsure
        facts = {"ok": bool(added), "items": len(added), "asked": len(wanted), "unsure": unsure, "untried": untried}
        notes = []
        if unsure:
            notes.append(MAYBE_SAVED_NOTE.format(n=unsure))
        if not_saved:
            notes.append(NOT_SAVED_NOTE.format(why=why, part=f" for {not_saved} of them" if added or unsure else ""))
        if not added:
            return {"failed": why, **({"may_have_saved": unsure} if unsure else {}),
                    **({"not_saved": not_saved} if not_saved else {}), "how_to_handle_it": " ".join(notes)}, facts
        report: dict[str, Any] = {"added": len(added), "where": LIST_PLACE, "items": added,
                                  "how_to_handle_it": " ".join([ADDED_NOTE.format(n=len(added)), *notes])}
        if unsure:
            report["may_have_saved"] = unsure
        if not_saved:
            report["not_saved"] = not_saved
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
        return ({"found": "\n".join(lines),
                 "how_to_handle_it": FOUND_NOTE.format(where="a memory search") + " " + WHOSE_NOTE},
                {"ok": True, "items": len(lines)})


__all__ = ["QUICK_MEMORY", "QuickMemory", "RECENT_LABEL", "fetch_recent", "natural_when", "recent_view",
           "todo_report"]
