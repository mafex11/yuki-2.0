"""The task frame: who and what a request may refer to, and what acting on it involves (revamp Phase B3-B4).

"message him this" needs three things no similarity search gives (docs/research/revamp-2026-10-02/01 §3, 02 §4.2):
who "him" is, what "this" is, and where the user talks with that person. The brain had a 1.8k-character memory dump
and had to guess. This module builds, **in code only** (no model call, a few ms), a small structured frame for each
request, the same for typed and voice:

* **Referents** (the register, B3): candidates for him / her / they / the other one, each with its source and age -
  the chat in front (the Now board's window, through memory's people index), the people named in this conversation
  (Yuki's replies and the user's words), the people around the current call, the chats in front in the last minutes;
  and for this / that / it: the selection or the focus line, and the clipboard's metadata (kind, length, how long ago
  it changed, which app put it there, a preview of at most :data:`CLIP_PREVIEW` characters; content an app marked
  private is never read). Each candidate's strength is its source's weight halved every :data:`SOURCES` half-life of
  its age; when the first two are within :data:`REFERENT_MARGIN` of each other the frame says it is a close call.
* **The person and route** (B4): a person the request names (memory's people index, whole words, misheard forms the
  user corrected included), else the strongest person referent: their routes most used lately first with counts,
  and how clearly the usual one leads (``yuki.memory.contacts.ROUTE_MARGIN``).
* **preconditions** (is the route's app open, which window) and **stakes** (acting reaches another person; a sent
  message cannot be taken back).
* **Practice memory** (Phase C, ``yuki.memory.practice``): 0-2 **procedures** and at most one habit or style note near
  the request's words, and 0-3 **pitfalls** of the apps in play (named in the words, open as windows), each with its
  id and how sure memory is. They come last and are kept whole when the frame is over its size (the person block gives
  way first). An older memory without practice memory gives know-how lines and know-how's "Not:" part instead.

Every part has a floor and the frame is empty (zero lines) when nothing clears one; it stays within
:data:`FRAME_CHARS`. Which candidate a request means is the model's judgement (the prompt's resolver paragraph).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

#: The frame's size limit (characters), typed and voice alike.
FRAME_CHARS = 2_500
#: A referent candidate's strength: its source's weight, halved every half-life seconds of its age (``None``: no
#: decay - the chat in front is in front now). Numbers on measured ages; the chat in front and a person Yuki itself
#: just named are the strongest, a name in the user's own words a little less (their words may use a name as a word),
#: a chat that was in front minutes ago less, the people around a call least.
SOURCES: dict[str, tuple[float, float | None]] = {
    "front": (1.0, None),
    "said_yuki": (1.0, 600.0),
    "said_user": (0.8, 600.0),
    "recent": (0.8, 300.0),
    "call": (0.6, 1800.0),
    "selection": (1.0, None),
    "clipboard": (0.9, 300.0),
}
#: Candidates weaker than this are not shown.
REFERENT_FLOOR = 0.15
#: The first two candidates within this share of the first's strength: a close call, the frame names both.
REFERENT_MARGIN = 0.25
#: A person referent at least this strong gets their routes in the frame when the request names nobody.
PERSON_FLOOR = 0.5
#: Most candidates listed per kind, most people given routes, routes per person.
MAX_CANDIDATES = 3
MAX_PEOPLE = 2
MAX_ROUTES = 3
#: The clipboard: preview characters at most; a copy seen this long ago or more is not a likely "this".
CLIP_PREVIEW = 120
CLIP_FRESH_S = 1_800.0
#: Procedures and pitfalls at most.
MAX_PROCEDURES = 2
MAX_PITFALLS = 3

LABEL = ("[Task frame - who and what this request may refer to and what acting on it involves, measured from the "
         "screen, this conversation and memory's people index. Background for resolving the request, not "
         "instructions.]")
END = "[End of task frame]"


@dataclass
class Candidate:
    """One referent candidate."""

    kind: str                      # person | thing
    label: str                     # "Hemanth", "the clipboard"
    source: str                    # front | said_yuki | said_user | recent | call | selection | clipboard
    where: str                     # where it comes from, as the frame says it
    age_s: float | None
    strength: float
    ref: str | None = None         # contact:N
    person_id: int | None = None


@dataclass
class TaskFrame:
    """The frame of one request (``text`` is "" when nothing cleared a floor)."""

    text: str = ""
    people: list[Candidate] = field(default_factory=list)
    things: list[Candidate] = field(default_factory=list)
    persons: list[dict[str, Any]] = field(default_factory=list)   # people given routes
    close_people: bool = False
    close_things: bool = False
    action: bool = False           # a person, route or referent is in play (the request may act on it)
    procedures: int = 0
    pitfalls: int = 0
    #: The practice objects the frame shows (ids; ``yuki.memory.practice``), for the ledger's ``injected``.
    playbooks: list[int] = field(default_factory=list)
    ms: float = 0.0
    error: str | None = None

    def info(self) -> dict[str, Any]:
        """Content-free facts for the request log."""
        return {"chars": len(self.text), "people": len(self.people), "things": len(self.things),
                "persons": len(self.persons), "close_people": self.close_people, "close_things": self.close_things,
                "routes": sum(len(p.get("routes") or []) for p in self.persons),
                "margins": [p.get("margin") for p in self.persons], "action": self.action,
                "procedures": self.procedures, "pitfalls": self.pitfalls, "playbooks": list(self.playbooks),
                "ms": self.ms, "error": self.error}


def strength(source: str, age_s: float | None) -> float:
    weight, half = SOURCES.get(source, (0.5, 600.0))
    if half is None or age_s is None:
        return weight
    return weight * 0.5 ** (max(0.0, float(age_s)) / half)


def _ago(age_s: float | None) -> str:
    if age_s is None:
        return "when, unknown"
    s = max(0.0, float(age_s))
    if s < 10:
        return "now"
    if s < 90:
        return f"{int(s)} s ago"
    if s < 5400:
        return f"{int(round(s / 60))} min ago"
    return f"{s / 3600:.1f} h ago"


def _one(text: Any, cap: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= cap else t[: cap - 1].rstrip() + "…"


def _day(at: float | None) -> str:
    return datetime.fromtimestamp(float(at)).strftime("%a %d %b %H:%M") if at else "?"


# ---------------------------------------------------------------------------
# The clipboard: metadata and how long ago it changed
# ---------------------------------------------------------------------------

_CLIP_LOCK = threading.Lock()
#: The clipboard's sequence number as last seen, when it was first seen, and when Yuki last looked (per process).
_CLIP_STATE: dict[str, Any] = {"seq": None, "first_seen": None, "looked": None}


def clipboard_meta(backend: Any, *, now: float | None = None) -> dict[str, Any] | None:
    """The clipboard's metadata for "this": ``{"kind", "chars", "owner", "preview", "private", "changed", "since",
    "age_s"}``, or ``None`` (no reader, an error, an empty clipboard). Windows keeps no time for a copy: the age is
    measured from Yuki's own looks (the sequence number changes on every copy) - ``age_s`` is how long ago it was first
    seen when it was already there at the previous look, else ``None`` with ``since`` the previous look (it changed
    after that). Private content (a password manager's marks) is never read: no preview. Never raises."""
    now = time.time() if now is None else float(now)
    read = getattr(backend, "read_clipboard", None)
    if not callable(read):
        return None
    try:
        clip = read()
    except Exception:
        return None
    if not getattr(clip, "ok", False):
        return None
    seq = getattr(clip, "sequence", None)
    with _CLIP_LOCK:
        state = _CLIP_STATE
        looked = state["looked"]
        if seq is not None and seq == state["seq"] and state["first_seen"] is not None:
            age, since, changed = now - float(state["first_seen"]), None, False
        else:
            changed = state["seq"] is not None
            since = looked if changed else None
            age = None
            state["seq"], state["first_seen"] = seq, now
        state["looked"] = now
    private = bool(getattr(clip, "private", False))
    text = getattr(clip, "text", None)
    formats = list(getattr(clip, "formats", []) or [])
    if text is None and not formats:
        return None
    kind = "private" if private else ("text" if text is not None else ", ".join(formats[:3]) or "data")
    preview = None
    if not private and text is not None:
        head = str(text)[: CLIP_PREVIEW * 4]
        try:   # a key or token copied as plain text never reaches the model (memory's own secret filter)
            from yuki.memory.secret_filter import redact_text

            head = redact_text(head)
        except Exception:
            head = ""
        preview = _one(head, CLIP_PREVIEW) or None
    return {"kind": kind, "chars": int(getattr(clip, "chars", 0) or 0), "owner": getattr(clip, "owner_process", "") or "",
            "preview": preview, "private": private, "changed": changed, "since": since, "age_s": age}


# ---------------------------------------------------------------------------
# The register
# ---------------------------------------------------------------------------


def referents(facts: dict[str, Any] | None, *, focus: Any = None, clip: dict[str, Any] | None = None,
              now: float | None = None) -> tuple[list[Candidate], list[Candidate]]:
    """``(people, things)``: the candidates for him / her / they / the other one, and for this / that / it, strongest
    first, each person once (its strongest source), weaker than :data:`REFERENT_FLOOR` left out."""
    now = time.time() if now is None else float(now)
    facts = facts or {}
    people: dict[int, Candidate] = {}

    def add(p: dict[str, Any], source: str, where: str, age: float | None) -> None:
        pid = p.get("id")
        if not isinstance(pid, int):
            return
        c = Candidate("person", str(p.get("name") or ""), source, where, age, round(strength(source, age), 3),
                      ref=p.get("ref"), person_id=pid)
        if pid not in people or c.strength > people[pid].strength:
            people[pid] = c

    front = facts.get("front") or None
    if front and front.get("kind") == "dm":
        for m in (front.get("people") or [])[:1]:
            add(m, "front", f"their {m.get('route') or 'chat'} is in front", 0.0)
    elif front and front.get("people"):   # a group in front: its people, the latest to write first
        for m in (front.get("people") or [])[:MAX_CANDIDATES]:
            last = m.get("last_theirs")
            add(m, "recent", f"wrote in the group in front ({front.get('chat') or 'a group'})",
                (now - float(last)) if last else None)
    for p in facts.get("said") or []:
        who = "said_yuki" if p.get("who") == "yuki" else "said_user"
        whose = "Yuki's reply" if who == "said_yuki" else "the user's words"
        add(p, who, f"named in {whose} earlier in this conversation (\"{_one(p.get('words'), 40)}\")",
            now - float(p.get("at") or now))
    for p in facts.get("recent") or []:
        add(p, "recent", f"their {p.get('route') or 'chat'} was in front", now - float(p.get("at") or now))
    call = facts.get("call") or None
    for p in (call or {}).get("people") or []:
        add(p, "call", f"around the {call.get('app') or ''} call going on: {p.get('how') or ''}".replace("  ", " "),
            now - float(p.get("at") or now))
    people_list = sorted((c for c in people.values() if c.strength >= REFERENT_FLOOR), key=lambda c: -c.strength)
    things: list[Candidate] = []
    if focus is not None and getattr(focus, "ok", False) and getattr(focus, "selection", ""):
        sel = str(focus.selection)
        where = f"selected in [{getattr(focus, 'window', 0)}] \"{_one(getattr(focus, 'window_title', ''), 50)}\""
        things.append(Candidate("thing", f"the selection: \"{_one(sel, CLIP_PREVIEW)}\" ({len(sel)} characters)",
                                "selection", where, 0.0, strength("selection", 0.0)))
    elif focus is not None and getattr(focus, "ok", False) and getattr(focus, "role", ""):
        things.append(Candidate("thing", "what has the keyboard: " + _one(focus.line(limit=120), 200), "selection",
                                "the focus line", 0.0, round(strength("selection", 0.0) * 0.5, 3)))
    if clip:
        if clip.get("age_s") is not None:
            age, when = float(clip["age_s"]), f"copied {_ago(clip['age_s'])} or earlier"
        elif clip.get("since") is not None:
            age = now - float(clip["since"])
            when = f"copied in the last {_ago(age).replace(' ago', '')} (since Yuki last looked)"
            age = age / 2.0   # somewhere in that span: its middle
        else:
            age, when = None, "copied before Yuki first looked (when, unknown)"
        fresh = age is None or age <= CLIP_FRESH_S
        if fresh:
            body = ("something an app marked private (not read)" if clip.get("private") else
                    f"{clip.get('kind')}, {clip.get('chars')} characters"
                    + (f": \"{clip['preview']}\"" if clip.get("preview") else ""))
            owner = f" from {clip['owner']}" if clip.get("owner") else ""
            things.append(Candidate("thing", f"the clipboard: {body}", "clipboard", f"{when}{owner}", age,
                                    round(strength("clipboard", age) * (0.6 if age is None else 1.0), 3)))
    things = sorted((t for t in things if t.strength >= REFERENT_FLOOR), key=lambda t: -t.strength)
    return people_list, things


def close_call(cands: Sequence[Candidate]) -> bool:
    """The first two candidates are within :data:`REFERENT_MARGIN` of each other."""
    if len(cands) < 2 or cands[0].strength <= 0:
        return False
    return (cands[0].strength - cands[1].strength) / cands[0].strength < REFERENT_MARGIN


# ---------------------------------------------------------------------------
# The frame
# ---------------------------------------------------------------------------


def _windows(overview: Any) -> list[dict[str, Any]]:
    if isinstance(overview, dict):
        return [w for w in overview.get("windows") or [] if isinstance(w, dict)]
    return []


def _app_key(name: Any) -> str:
    from yuki.memory.store import app_key

    return app_key(str(name or "")) or ""


def preconditions(person: dict[str, Any], overview: Any) -> list[str]:
    """Whether the usual route's app is open, and its window (from the desktop overview: process names, ids)."""
    routes = person.get("routes") or []
    if not routes or not _windows(overview):   # no overview: what is open is unknown, say nothing
        return []
    usual = routes[0]
    procs = {_app_key(p) for p in usual.get("processes") or [] if p}
    if not procs:
        return []
    wins = [w for w in _windows(overview) if _app_key(w.get("process_name")) in procs]
    app = usual.get("app") or usual.get("label") or "its app"
    if wins:
        w = next((x for x in wins if x.get("is_foreground")), wins[0])
        state = "in front" if w.get("is_foreground") else ("minimized" if w.get("is_minimized") else "open")
        return [f"{app} is open: window [{w.get('hwnd')}] \"{_one(w.get('title'), 60)}\" ({state})."]
    return [f"No {app} window is open now ({', '.join(sorted(procs))} not running or no window)."]


def build(facts: dict[str, Any] | None, *, focus: Any = None, clip: dict[str, Any] | None = None,
          knowhow: Sequence[str] = (), overview: Any = None, now: float | None = None,
          chars: int = FRAME_CHARS, practice: dict[str, Any] | None = None) -> TaskFrame:
    """The task frame from memory's ``frame_facts`` (``MemoryClient.frame_facts``), the focus snapshot, the clipboard's
    metadata (:func:`clipboard_meta`), practice memory for the request (``MemoryClient.practice``: procedures near the
    words and pitfalls of the apps in play; it replaces the know-how lines and know-how's "Not:" pitfalls, which an
    older memory without it still gives) and the desktop overview. Code only; never raises (a failure is an empty
    frame with ``error``)."""
    started = time.perf_counter()
    now = time.time() if now is None else float(now)
    frame = TaskFrame()
    try:
        facts = facts or {}
        people, things = referents(facts, focus=focus, clip=clip, now=now)
        frame.people, frame.things = people[:MAX_CANDIDATES], things[:MAX_CANDIDATES]
        frame.close_people, frame.close_things = close_call(people), close_call(things)
        routes = facts.get("routes") or {}
        named = [p for p in facts.get("named") or [] if isinstance(p, dict)]
        chosen: list[tuple[dict[str, Any], str]] = []
        for p in named[:MAX_PEOPLE]:
            if isinstance(p.get("id"), int):
                chosen.append((p, f"named in the request (\"{_one(p.get('words'), 30)}\")"))
        # nobody named: the person of the chat in front (and a close second) gets the whole block; a person only
        # remembered from this conversation, the recent chats or the call keeps a one-line route in the register
        if not chosen and people and people[0].source == "front" and people[0].strength >= PERSON_FLOOR:
            top = people[:2] if frame.close_people else people[:1]
            for c in top:
                chosen.append(({"id": c.person_id, "name": c.label}, "the strongest referent"))
        for p, why in chosen:
            r = routes.get(p.get("id")) or routes.get(str(p.get("id"))) or {}
            frame.persons.append({"id": p.get("id"), "name": p.get("name") or r.get("name"), "why": why,
                                  "relation": r.get("relation") or p.get("relation"), "routes": r.get("routes") or [],
                                  "margin": r.get("margin"), "close": bool(r.get("close")),
                                  "preferred": r.get("preferred"), "places": r.get("places") or []})
        lines = _referent_lines(frame, routes, {p["id"] for p in frame.persons})
        lines += _person_lines(frame, overview)
        tail: list[str] = []
        if isinstance(practice, dict):
            # last in the frame, and kept whole when the frame is over its size: the pitfalls of the apps in play
            # always reach the brain (the person block gives way first)
            tail = _practice_lines(frame, practice)
        else:   # an older memory: know-how lines and their "Not:" part
            proc = [k for k in (str(x).strip() for x in knowhow or ()) if k][:MAX_PROCEDURES]
            if proc:
                lines.append("How it worked before on this PC (know-how near the request):")
                lines += [f"- {_one(k.split(' Not: ', 1)[0], 300)}" for k in proc]
            frame.procedures = len(proc)
            pits = [p for p in facts.get("pitfalls") or [] if isinstance(p, dict)][:MAX_PITFALLS]
            if pits:
                lines.append("What did not work before in the apps in play:")
                lines += [f"- [{p.get('app')}] {_one(p.get('text'), 260)}" for p in pits]
            frame.pitfalls = len(pits)
        # an action request (the caller's measure for the related ranking): a person named or in front, a selection
        frame.action = bool(frame.persons or [t for t in frame.things if t.source == "selection"])
        frame.text = _fit(lines, chars, keep=tail)
        # what was shown, as the ledger records it (our own "[pb:N]" marks in the text that survived the size limit)
        frame.playbooks = [i for i in frame.playbooks if f"[pb:{i}]" in frame.text]
    except Exception as exc:
        frame.text, frame.error = "", f"{type(exc).__name__}: {exc}"
    frame.ms = round((time.perf_counter() - started) * 1000, 2)
    return frame


def _referent_lines(frame: TaskFrame, routes: dict[Any, Any] | None = None, shown: set[Any] | None = None) -> list[str]:
    lines: list[str] = []
    routes = routes or {}
    shown = shown or set()
    if frame.people:
        lines.append("Who \"him\", \"her\", \"them\" or \"the other one\" may be, strongest first:")
        for c in frame.people:
            line = f"- {c.label}: {c.where} ({_ago(c.age_s)})"
            r = routes.get(c.person_id) or routes.get(str(c.person_id)) or {}
            if c.person_id not in shown and r.get("routes"):
                usual = r["routes"][0]
                how = "the user's choice" if usual.get("preferred") else (
                    "a close call" if r.get("close") and len(r["routes"]) > 1 else
                    f"margin {float(r.get('margin') or 0):.2f}")
                line += f"; usually [{usual.get('ref')}] {usual.get('label')} ({how})"
            lines.append(line)
        if frame.close_people:
            lines.append(f"  The first two are a close call (within {REFERENT_MARGIN:.2f} of each other): if the request "
                         "says neither, ask naming both.")
    if frame.things:
        lines.append("What \"this\", \"that\" or \"it\" may be, strongest first:")
        for t in frame.things:
            lines.append(f"- {t.label} ({t.where})")
        if frame.close_things:
            lines.append("  The first two are a close call: if the request says neither, ask naming both.")
    return lines


def _person_lines(frame: TaskFrame, overview: Any) -> list[str]:
    lines: list[str] = []
    for p in frame.persons:
        head = p["name"] + (f" ({p['relation']})" if p.get("relation") else "")
        routes = p.get("routes") or []
        if not routes:
            lines.append(f"{head} - {p['why']}: no chat with them in memory (about({p['name']!r}) for what memory has).")
            continue
        lines.append(f"{head} - {p['why']}. Where the user talks with them, most used lately first:")
        for i, r in enumerate(routes[:MAX_ROUTES], 1):
            notes = []
            if r.get("known_as"):
                notes.append(f"{r.get('app') or 'the app'} knows them as \"{r['known_as']}\", not \"{p['name']}\"")
            if r.get("preferred"):
                notes.append("the user chose this one")
            if r.get("in_front"):
                notes.append("in front now")
            last = f", last yours {_day(r.get('last_mine'))}" if r.get("last_mine") else ""
            lines.append(f"  {i}. [{r.get('ref')}] {r.get('label')}: the user {r.get('mine')} / them {r.get('theirs')}"
                         f"{last}" + (f" ({'; '.join(notes)})" if notes else ""))
        if p.get("preferred") and routes[0].get("preferred"):
            lines.append("  Usual: the one the user chose (\"" + _one((p.get("preferred") or {}).get("quote"), 60) + "\").")
        elif len(routes) > 1 and p.get("close"):
            lines.append(f"  Close call between the first two (margin {float(p.get('margin') or 0):.2f}): unless the "
                         "user named one, ask naming both.")
        elif len(routes) > 1:
            lines.append(f"  Usual: the first, clearly (margin {float(p.get('margin') or 0):.2f}).")
        places = [x for x in p.get("places") or [] if isinstance(x, dict)]
        if places:
            lines.append("  Group chats on the same apps the user writes in most: "
                         + "; ".join(f"[{x.get('ref')}] {x.get('label')} (the user {x.get('mine')})" for x in places))
        lines += ["  " + x for x in preconditions(p, overview)]
        whom = "everyone in that group" if routes[0].get("kind") in ("group", "channel") else p["name"]
        lines.append(f"  Stakes: a message sent on the usual route reaches {whom}; once sent it cannot be taken back.")
    return lines


#: Practice memory in the frame (Phase C): the procedures and habits shown, then the pitfalls of the apps in play.
PLAYBOOK_HEAD = ("How this was done before on this PC (playbooks - your first attempt: check its first step against "
                 "the screen, adapt when the screen differs, ask when it names no single default; put its id in the "
                 "playbook field of the actions that follow it):")
PITFALL_HEAD = "Your own earlier mistakes in the apps in play (never repeat one; do what it says instead):"


def _practice_lines(frame: TaskFrame, practice: dict[str, Any]) -> list[str]:
    """The practice part of the frame: at most :data:`MAX_PROCEDURES` procedures plus one habit or style note, then
    at most :data:`MAX_PITFALLS` pitfalls, each with its id, how sure memory is and the apps it is about."""
    lines: list[str] = []
    ways = [w for w in practice.get("ways") or [] if isinstance(w, dict) and w.get("text")][:MAX_PROCEDURES + 1]
    pits = [p for p in practice.get("pitfalls") or [] if isinstance(p, dict) and p.get("text")][:MAX_PITFALLS]
    if ways:
        lines.append(PLAYBOOK_HEAD)
        for w in ways:
            apps = f" [{', '.join(w.get('apps') or [])}]" if w.get("apps") else ""
            lines.append(f"- [{w.get('ref')}] {w.get('kind')} \"{_one(w.get('intent'), 80)}\"{apps} "
                         f"({w.get('label')}): {_one(w.get('text'), 420)}")
    if pits:
        lines.append(PITFALL_HEAD)
        for p in pits:
            apps = ", ".join(p.get("apps") or []) or "any app"
            lines.append(f"- [{p.get('ref')}] [{apps}] \"{_one(p.get('intent'), 80)}\" ({p.get('label')}): "
                         f"{_one(p.get('text'), 440)}")
    frame.procedures = len(ways)
    frame.pitfalls = len(pits)
    frame.playbooks = [int(x["id"]) for x in [*ways, *pits] if isinstance(x.get("id"), int)]
    return lines


def _fit(lines: list[str], chars: int, keep: Sequence[str] = ()) -> str:
    """The frame within ``chars`` (the label and end line included); when it is over, the last lines of ``lines`` go
    first and ``keep`` (placed after them) only once ``lines`` are gone, its own last lines first. ``""`` for no lines."""
    body, tail = list(lines), list(keep)
    if not body and not tail:
        return ""

    def trim(xs: list[str]) -> None:
        while xs and xs[-1].rstrip().endswith(":"):   # a heading whose items were cut goes with them
            xs.pop()

    while body or tail:
        trim(body)
        trim(tail)
        if not body and not tail:
            break
        text = LABEL + "\n" + "\n".join(body + tail) + "\n" + END
        if len(text) <= chars:
            return text
        (body if body else tail).pop()
    return ""
