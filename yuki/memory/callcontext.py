"""What was around a call: the context pack the call's digest and journal read the user's own words by.

Contract: ``docs/MEMORY.md`` -> "Calls: just my side" (context from what's around the call).

Only the user's microphone is ever recorded on a call (never the call's audio, never anyone else), so their
side alone is thin: "yeah do it", "send him that link", "the thing on screen". The user chose (2026-10-01) "use
what's around the call": what Yuki ALREADY read from the screen and keeps in memory, assembled in code for a
call's span, so the model can tell who the user was talking to and what "that" was. No new capture source: no
audio, no pixels - only stored screen text (captures), links seen and threads of work.

The pack (:func:`build`), each item time-stamped (local ``HH:MM``) so it can be lined up with the utterances:

* ``CALL APP``: the call app's own windows from shortly before the call to its end - its voice panel,
  participant list or tiles, the call's text chat, captions it showed - as the watcher stored them (deltas;
  lines already shown are not repeated). Who was on it is for the model to read: code never picks names.
* ``OTHER SCREENS``: what the user had open, shared or looked at during the call (every other window's
  captures, grouped per window or page: its app, title, site, first and last time, a head of its text),
  marked when only beside the window in front, a popup, or put there by Yuki.
* ``LINKS``: pages viewed and links shared during the call.
* ``MESSAGES AROUND``: chat and mail captures in the :data:`PAD_S` before and after the call.
* ``THREADS OF WORK``: the threads the user touched during the call (``work_thread_events``).
* ``NAMES``: how memory spells names the recogniser mishears (the user's corrections, ``name_aliases``).

Every section has a character cap (the whole pack is at most ~:data:`MAX_CHARS`); what is cut is the
middle of a long call, never its first screens. Code only selects by time, window and size (plumbing): what
anything means is the model's reading. Cost: nothing but ~1-2k input tokens per model request that reads it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

#: Chat and mail around the call: this long before its start and after its end (s).
PAD_S = 30 * 60.0
#: The call app's screens from this long before the call row began (joining: the voice panel appears then).
JOIN_LOOKBACK_S = 15 * 60.0
#: Character caps per section and in all.
CALL_APP_CHARS = 2_400
SCREENS_CHARS = 1_500
LINKS_CHARS = 700
AROUND_CHARS = 1_100
WORK_CHARS = 700
NAMES_CHARS = 400
MAX_CHARS = 7_000
#: One capture's text at most (call app), one window's head at most (other screens), one message capture.
CAPTURE_CHARS = 420
HEAD_CHARS = 170
AROUND_CAPTURE_CHARS = 300
MAX_LINKS = 10
MAX_THREADS = 4
MAX_ALIASES = 12
#: Captures read from the store for one pack at most.
MAX_CAPTURES = 900
#: Extraction kinds that hold messages (the journal's MESSAGE_KINDS).
MESSAGE_KINDS = ("conversation", "email")


@dataclass
class CallContext:
    """The pack for one span of a call. ``text`` is plain (the caller fences and scrubs it)."""

    text: str = ""
    #: The capture threads (windows, pages, conversations) seen during the call, most seen first: the threads of
    #: work they belong to are offered to the journal's call batch.
    capture_thread_ids: list[int] = field(default_factory=list)
    #: Content-free counts per section (logs, evaluation).
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.text)


def _hm(at: float) -> str:
    return datetime.fromtimestamp(at).strftime("%H:%M")


def _flat(text: Any, cap: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= cap else s[: max(0, cap - 1)].rstrip() + "…"


def _attention(cap: dict[str, Any]) -> str:
    trigger = str(cap.get("trigger") or "")
    marks = []
    if trigger == "popup":
        marks.append("popup")
    elif trigger.startswith("visible:"):
        marks.append("beside the window in front")
    if cap.get("by_yuki"):
        marks.append("put on screen by Yuki")
    return f" ({', '.join(marks)})" if marks else ""


def _fit(items: Sequence[tuple[float, int, str]], cap: int) -> tuple[list[str], int]:
    """Items ``(at, priority, text)`` kept within ``cap`` characters: lowest priority number first, ties by time
    (so the first screens of each window and the call's start stay), then shown in time order. Returns the
    lines and how many were left out."""
    chosen: list[tuple[float, str]] = []
    size = 0
    left = 0
    for at, _prio, text in sorted(items, key=lambda i: (i[1], i[0])):
        if size + len(text) + 1 > cap:
            left += 1
            continue
        chosen.append((at, text))
        size += len(text) + 1
    return [t for _, t in sorted(chosen, key=lambda c: c[0])], left


def _text(cap: dict[str, Any]) -> str:
    """A capture's text for the pack: a conversation's messages (``cap["messages"]``, set by :func:`build`) as
    ``HH:MM sender: text`` lines, a terminal capture's commands (the store's own JSON) as ``$ command`` lines,
    anything else as stored."""
    msgs = cap.get("messages")
    if msgs is not None:   # a conversation read from its stored messages (none left: nothing around the call)
        return "\n".join(f"{_hm(m.at) if m.at else '--:--'} {_who(m)}: {m.text}" for m in msgs)
    if (cap.get("kind") or "") == "terminal":
        try:
            data = json.loads(cap.get("text") or "{}")
        except ValueError:
            data = {}
        cmds = data.get("commands") if isinstance(data, dict) else None
        return "\n".join(f"$ {c.get('command')}" for c in (cmds or []) if isinstance(c, dict) and c.get("command"))
    return str(cap.get("text") or "")


def _who(m: Any) -> str:
    if getattr(m, "is_me", False):
        return "the user"
    return _flat(getattr(m, "sender", "") or "(sender not shown)", 40)


def _attach_messages(store: Any, caps: Sequence[dict[str, Any]], lo: float, hi: float) -> None:
    """Each conversation capture gets ``messages``: its stored messages that are new, or dated inside ``[lo, hi]``
    (an old message only scrolled past is not "around the call"). A capture without stored messages keeps its
    text."""
    ids = [c["id"] for c in caps if (c.get("kind") or "") in MESSAGE_KINDS]
    if not ids:
        return
    try:
        by_cap = store.messages_for_captures(ids)
    except Exception:
        return
    for c in caps:
        stored = by_cap.get(c["id"])
        if stored:
            c["messages"] = [m for m in stored if m.text and (m.status == "new" or (m.at is not None and lo <= m.at <= hi))]


def _same_app(cap: dict[str, Any], process: str | None, app: str | None) -> bool:
    """Whether a capture is of the call's own app: the same process key (identity), else the same shown name."""
    if process and cap.get("process"):
        return str(cap["process"]).casefold() == str(process).casefold()
    return bool(app) and str(cap.get("app") or "").casefold() == str(app).casefold()


def _new_lines(text: str, seen: set[str]) -> list[str]:
    """The capture's lines not already shown in this section (the same voice panel read again is one line)."""
    out = []
    for line in str(text or "").splitlines():
        key = " ".join(line.split()).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(" ".join(line.split()))
    return out


def build(store: Any, *, start: float, end: float, app: str | None = None, process: str | None = None,
          before: bool = True, after: bool = True) -> CallContext:
    """The context pack for a call's span ``[start, end]`` (epoch seconds) of the app ``app``/``process``.

    ``before``/``after``: include the joining screens and the messages before the call (the first part of a
    long call) and the messages after it (its last part). Never raises: a store error leaves a section out.
    """
    ctx = CallContext()
    start, end = float(start), max(float(start), float(end))
    lo = start - (max(PAD_S, JOIN_LOOKBACK_S) if before else 0.0)
    hi = end + (PAD_S if after else 0.0)
    try:
        caps = store.captures_around(lo, hi, limit=MAX_CAPTURES)
    except Exception:
        caps = []
    _attach_messages(store, caps, lo, hi)
    sections: list[str] = []

    # -- the call app's own windows ----------------------------------------------------------------------
    join_from = start - (JOIN_LOOKBACK_S if before else 0.0)
    own = [c for c in caps if _same_app(c, process, app) and join_from <= c["at"] <= end]
    seen: set[str] = set()
    items: list[tuple[float, int, str]] = []
    first_of_window: set[int] = set()
    for c in own:
        lines = _new_lines(_text(c), seen)
        if not lines:
            continue
        prio = 0 if c["thread_id"] not in first_of_window else 1
        first_of_window.add(c["thread_id"])
        when = _hm(c["at"]) + (" (before the call began)" if c["at"] < start else "")
        head = f"- {when} window \"{_flat(c['title'], 90)}\"{_attention(c)}:"
        items.append((c["at"], prio, _flat(f"{head} {' / '.join(lines)}", CAPTURE_CHARS)))
    shown_ids = {c["id"] for c in own}
    kept, left = _fit(items, CALL_APP_CHARS)
    ctx.counts["call_app"] = len(kept)
    if kept:
        sections.append(f"CALL APP ({app or 'the call app'}): its own windows, from joining to the end of the "
                        "call - who shows in its voice panel, participant list or tiles, its chat, captions:")
        sections += kept
        if left:
            sections.append(f"({left} more screens of it left out)")

    # -- other windows during the call -------------------------------------------------------------------
    during = [c for c in caps if start <= c["at"] <= end and not _same_app(c, process, app)]
    windows: dict[int, dict[str, Any]] = {}
    for c in during:
        w = windows.setdefault(c["thread_id"], {"first": c["at"], "last": c["at"], "n": 0, "cap": c, "marks": set()})
        w["last"] = c["at"]
        w["n"] += 1
        w["marks"].add(_attention(c))
    items = []
    for tid, w in windows.items():
        c = w["cap"]
        where = f"{c['app'] or '?'}" + (f", {c['host']}" if c.get("host") else "")
        span = _hm(w["first"]) + (f"-{_hm(w['last'])}" if _hm(w["last"]) != _hm(w["first"]) else "")
        mark = "" if "" in w["marks"] else sorted(w["marks"])[0]
        text = f"- {span} {where}: \"{_flat(c['title'], 90)}\"{mark} - {_flat(_text(c), HEAD_CHARS)}"
        items.append((w["first"], -w["n"], text))    # windows seen most often first when it has to be cut
    kept, left = _fit(items, SCREENS_CHARS)
    ctx.counts["screens"] = len(kept)
    ctx.capture_thread_ids = [tid for tid, _ in sorted(windows.items(), key=lambda kv: -kv[1]["n"])]
    if kept:
        sections.append("OTHER SCREENS during the call (what the user had open, shared or looked at; per window, "
                        "its first and last time):")
        sections += kept
        if left:
            sections.append(f"({left} more windows left out)")

    # -- links -----------------------------------------------------------------------------------------------
    try:
        links = store.links(since=start, until=end, limit=60)
    except Exception:
        links = []
    items = []
    for ln in links:
        # a visit inside the call: the page first opened then, or a link shared then (a page open for days and
        # merely seen again is in OTHER SCREENS already)
        at = ln.first_seen if start <= ln.first_seen <= end else ln.last_seen
        if not start <= at <= end or (ln.how != "shared" and not start <= ln.first_seen <= end):
            continue
        who = f"shared by {_flat(ln.sender, 40)}" if ln.how == "shared" and ln.sender else ln.how
        where = f" in {_flat(ln.where, 60)}" if ln.where else ""
        items.append((at, 0 if ln.how == "shared" else 1,
                      f"- {_hm(at)} {who}{where}: {_flat(ln.title, 80) or '(no title)'} <{_flat(ln.url, 140)}>"))
    kept, _left = _fit(sorted(items, key=lambda i: (i[1], i[0]))[:MAX_LINKS], LINKS_CHARS)
    ctx.counts["links"] = len(kept)
    if kept:
        sections.append("LINKS opened or shared during the call:")
        sections += kept

    # -- messages just before and after --------------------------------------------------------------------
    items = []
    seen_around: set[str] = set()
    for c in caps:
        if (c.get("kind") or "") not in MESSAGE_KINDS or start <= c["at"] <= end or c["id"] in shown_ids:
            continue
        if c["at"] < start and not before or c["at"] > end and not after:
            continue
        lines = _new_lines(_text(c), seen_around)
        if not lines:
            continue
        side = "before" if c["at"] < start else "after"
        distance = start - c["at"] if side == "before" else c["at"] - end
        text = (f"- {_hm(c['at'])} ({side} the call) {c['app'] or '?'} \"{_flat(c['title'], 70)}\"{_attention(c)}: "
                f"{_flat(' / '.join(lines), AROUND_CAPTURE_CHARS)}")
        items.append((c["at"], int(distance // 60), text))   # the nearest to the call first when it has to be cut
    kept, left = _fit(items, AROUND_CHARS)
    ctx.counts["around"] = len(kept)
    if kept:
        sections.append(f"MESSAGES AROUND the call (chats and mail on screen within {int(PAD_S // 60)} min before "
                        "and after):")
        sections += kept

    # -- threads of work touched during the call ------------------------------------------------------------
    lines: list[str] = []
    try:
        events = store.latest_work_events(since=start - 600.0, until=end, limit=60)
        counts: dict[int, int] = {}
        for e in events:
            tid = store.resolve_work_thread(int(e["work_thread_id"]))
            if tid is not None:
                counts[tid] = counts.get(tid, 0) + 1
        for tid in sorted(counts, key=lambda t: -counts[t])[:MAX_THREADS]:
            t = store.work_thread(tid)
            if t is None or t.status in ("dismissed", "merged"):
                continue
            state = f": {_flat(t.state, 180)}" if t.state else ""
            lines.append(f"- W{t.id} \"{_flat(t.title, 80)}\" ({t.status}){state}")
    except Exception:
        lines = []
    kept, _left = _fit([(float(i), 0, ln) for i, ln in enumerate(lines)], WORK_CHARS)
    ctx.counts["threads"] = len(kept)
    if kept:
        sections.append("THREADS OF WORK the user touched during the call:")
        sections += kept

    # -- names memory spells differently from how they are heard ---------------------------------------------
    try:
        aliases = store.aliases()[-MAX_ALIASES:]
    except Exception:
        aliases = []
    items = [(float(i), 0, f"- \"{_flat(a['heard'], 40)}\" is {_flat(a['name'], 40)}")
             for i, a in enumerate(aliases) if a.get("heard") and a.get("name")]
    kept, _left = _fit(items, NAMES_CHARS)
    ctx.counts["names"] = len(kept)
    if kept:
        sections.append("NAMES as memory spells them (the user's corrections of what was heard or read):")
        sections += kept

    text = "\n".join(sections)
    ctx.text = text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1] + "…"
    return ctx


def for_call(store: Any, call_id: int, *, start: float | None = None, end: float | None = None,
             before: bool = True, after: bool = True) -> CallContext:
    """:func:`build` for one ``calls`` row (its app and process), over ``[start, end]`` (default: the row's own
    times). Never raises."""
    try:
        rec = store.call_record(int(call_id))
    except Exception:
        rec = None
    if rec is None:
        if start is None:
            return CallContext()
        return build(store, start=start, end=end or start, before=before, after=after)
    s = float(start if start is not None else (rec.recording_started_at or rec.detected_at))
    e = float(end if end is not None else (rec.ended_at or rec.recording_stopped_at or s))
    return build(store, start=s, end=e, app=rec.app, process=rec.process, before=before, after=after)


def grounded(name: str, *texts: str) -> bool:
    """Whether a name the model gave occurs in the text it read (the context or the transcript): the same
    check as a commitment's quote being a span of its utterance - a name nothing showed is not kept."""
    key = " ".join(str(name or "").split()).casefold()
    return bool(key) and any(key in " ".join(str(t or "").split()).casefold() for t in texts)


__all__ = ["CallContext", "build", "for_call", "grounded", "PAD_S", "MAX_CHARS"]
