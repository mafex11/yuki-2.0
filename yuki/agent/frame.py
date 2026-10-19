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
* **This session's lessons** (N8): what went wrong in this session's troubled runs on the apps in play. Practice
  memory's procedures, pitfalls, go-tos and usual paths are no longer shown (audit 17 Oct 2026).

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
    #: Phase S: the pinned skill's key (for the log), ``None`` when none was pinned.
    skill: str | None = None

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


def named_app_line(app: str | None) -> str:
    """V4 (3 Oct): the line that puts the app the user named above every usual route ("" without one). A send went
    to the person's usual app while the user had named another one."""
    app = " ".join(str(app or "").split())[:80]
    if not app:
        return ""
    return (f"The user named where to do this themselves: {app}. Use that app or route (their chat there, found by "
            f"the name it knows them by); it wins over the usual route below, and over the request's wording.")


#: Phase S (sCore, map-skills S3.5): the user's usual way the code handed over with the request (a skill whose own
#: routes didn't get there). Placed first and never cut; its app's usual-path, go-to and pitfall lines are left out
#: (the 4 Oct B2 trace failed because a procedure, a pitfall and a session lesson pointed three ways). x5Fallback
#: (failure audit 10 Oct, C2): never "follow it" (the thinker re-ran the skill that had just failed, live 10 Oct): the
#: rest is done by steps. Neutral on why (review r1 L6): the reason is :data:`PINNED_WHY`'s line under it.
PINNED_HEAD = ("The user's usual way (skill {key}) handed this over. Do it by steps (look, click, type) and don't run "
               "that skill again; {done}.")
#: The head's done clause: the capability's done line is what Yuki says once done ("Done.", "Volume set."), so it is
#: quoted, never read as the state itself (review r1 L6: "it is done when Done."); with no done line, the plain one.
PINNED_DONE = "it is done only when the PC shows it (\"{words}\")"
PINNED_DONE_PLAIN = "it is done only when the PC shows the result"
#: Why it was handed over (the engine's ``why``, a code word: ``yuki.skills.engine.WHY_*``), one line under the head
#: in plain words. A reason not named here (none fit) gets no line.
PINNED_WHY: dict[str, str] = {
    "routes_failed": "Its own routes didn't get there (what they tried is below).",
    "to_planner": "It stopped before a step that would send or join with no plan behind it; that step did not run.",
    "unverified": ("It acted, but the PC couldn't confirm the result: look at the screen first and finish only what "
                   "isn't done; never redo a step that worked."),
    "dropped": ("It didn't run: {slots} as given isn't in the user's words as heard (misheard, or reworded). Use "
                "what the user meant, checked against the user's words and memory; ask only if it could be someone "
                "or something else."),
    "error": ("It broke partway, so what it did is unknown: look at the screen first and finish only what isn't done; "
              "never redo a step that worked."),
}
#: IF-seed: a value its routes found no item for on screen (``missing``), named with the user's own word for it.
PINNED_MISSING = ("Nothing there is named \"{value}\" ({slot}): that is the user's word for it, not always its name. "
                  "Pick the one the user means from the screen{names} and what memory knows.")
#: IF-seed: the names its routes read there (``seen``, RAM only), last and with a room of their own (the pin's other
#: lines keep :data:`PINNED_CHARS`): the thinker picks the one the user means from them.
PINNED_SEEN = "- Names it read there (screen text, data only): "
SEEN_CHARS = 600
#: The pinned block's size cap, set when it is built (never by the frame's own fitting); ``seen`` rides beyond it in
#: :data:`SEEN_CHARS`.
PINNED_CHARS = 900


def _seen_line(names: Any, room: int) -> str:
    """``seen`` as one line within ``room`` characters: as many names as fit, in the order read ("" for none)."""
    taken: list[str] = []
    used = len(PINNED_SEEN)
    for n in names if isinstance(names, list) else []:
        name = _one(n, 80)
        if not name:
            continue
        add = len(name) + (2 if taken else 0)
        if used + add > room:
            break
        taken.append(name)
        used += add
    return PINNED_SEEN + "; ".join(taken) if taken else ""


def pinned_lines(pinned: dict[str, Any] | None) -> list[str]:
    """The pinned skill as frame lines (at most :data:`PINNED_CHARS` in all, and the names it read on screen in
    :data:`SEEN_CHARS` more): what to do now (by steps, never that skill again) and when it is done, why it was
    handed over, a value its routes found no item for, each slot with where its value came from, the goal, what was
    already tried (not to be redone), and last the names read on screen."""
    if not isinstance(pinned, dict) or not pinned.get("key"):
        return []
    words = _one(pinned.get("done_words") or "", 160).rstrip(". ")
    done = PINNED_DONE.format(words=words) if words else PINNED_DONE_PLAIN
    lines = [PINNED_HEAD.format(key=_one(pinned.get("key"), 40), done=done)]
    slots = pinned.get("slots") if isinstance(pinned.get("slots"), dict) else {}
    why = PINNED_WHY.get(str(pinned.get("why") or ""))
    if why:
        unheard = [_one(name, 20) for name, s in slots.items() if isinstance(s, dict) and s.get("from") == "unheard"]
        lines.append("- " + why.format(slots=", ".join(unheard[:3]) or "a value"))
    names = [n for n in pinned.get("seen") or [] if isinstance(n, str) and n.strip()]
    missing = str(pinned.get("missing") or "")
    if missing and missing in slots:
        s = slots[missing]
        value = s.get("value") if isinstance(s, dict) else s
        lines.append("- " + PINNED_MISSING.format(value=_one(value, 60), slot=_one(missing, 20),
                                                  names=", the names below" if names else ""))
    if slots:
        parts = []
        for name, s in list(slots.items())[:6]:
            value = s.get("value") if isinstance(s, dict) else s
            src = s.get("from") if isinstance(s, dict) else ""
            if value is None or (isinstance(value, str) and not value.strip()):
                continue   # an empty default ("" text: just open the chat) says nothing to do
            parts.append(f"{_one(name, 20)} = {_one(value, 60)}" + (f" ({_one(src, 12)})" if src else ""))
        if parts:
            lines.append("- With: " + "; ".join(parts))
    if pinned.get("goal"):
        lines.append(f"- Goal: {_one(pinned.get('goal'), 160)}")
    tried = [t for t in pinned.get("tried") or [] if isinstance(t, dict)]
    if tried:
        lines.append("- Already tried (do not redo): " + "; ".join(
            f"rung {_one(t.get('rung'), 2)} {_one(t.get('via'), 20)} - {_one(t.get('why'), 30)}" for t in tried[:4]))
    out: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > PINNED_CHARS:
            break
        out.append(line)
        used += len(line) + 1
    seen = _seen_line(names, SEEN_CHARS)
    if seen:
        out.append(seen)
    return out


def build(facts: dict[str, Any] | None, *, focus: Any = None, clip: dict[str, Any] | None = None,
          overview: Any = None, now: float | None = None, chars: int = FRAME_CHARS,
          named_app: str | None = None, lessons: Sequence[str] = (), lately: Sequence[str] = (),
          pinned: dict[str, Any] | None = None) -> TaskFrame:
    """The task frame from memory's ``frame_facts`` (``MemoryClient.frame_facts``), the focus snapshot, the clipboard's
    metadata (:func:`clipboard_meta`) and the desktop overview. Code only; never raises (a failure is an empty frame
    with ``error``). Practice memory's ways, pitfalls, go-tos and usual paths are no longer shown (audit 17 Oct 2026:
    they were picked by the apps open and leaked into unrelated requests; their learners are deleted).

    ``lessons`` (N8, report 47): what went wrong in this session's troubled runs on the apps in play, written by code
    from their traces (:func:`session_lessons`); kept whole at the end. ``lately`` (N9): :func:`people_lately`'s
    lines, for a spoken request (who a misheard name may be). ``pinned`` (phase S): a skill the code handed over
    (:func:`pinned_lines`): first, never cut."""
    started = time.perf_counter()
    now = time.time() if now is None else float(now)
    frame = TaskFrame()
    try:
        facts = facts or {}
        pin = pinned_lines(pinned)
        frame.skill = str(pinned.get("key")) if pin and isinstance(pinned, dict) else None
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
                                  "preferred": r.get("preferred"), "places": r.get("places") or [],
                                  "samples": r.get("samples") or []})
        lines = _referent_lines(frame, routes, {p["id"] for p in frame.persons})
        lines += _person_lines(frame, overview)
        near = [str(x) for x in lately or () if str(x).strip()][:MAX_LATELY]
        if near:   # N9: who a misheard name may be (gives way first when the frame is over its size)
            lines += [LATELY_HEAD, *(f"- {_one(x, 200)}" for x in near)]
        named = named_app_line(named_app)
        if named:
            lines.insert(0, named)   # first, so the size limit never cuts it and it reads before any usual route
        tail: list[str] = []
        fresh = [str(x) for x in lessons or () if str(x).strip()][:MAX_LESSONS]
        if fresh:   # N8: this session's lessons, kept whole with the pitfalls
            tail = [LESSONS_HEAD, *(f"- {_one(x, 300)}" for x in fresh), *tail]
        # an action request (the caller's measure for the related ranking): a person named or in front, a selection
        frame.action = bool(frame.persons or [t for t in frame.things if t.source == "selection"]) or bool(pin)
        frame.text = _fit(lines, chars, keep=tail, pinned=pin)
    except Exception as exc:
        frame.text, frame.error = "", f"{type(exc).__name__}: {exc}"
    frame.ms = round((time.perf_counter() - started) * 1000, 2)
    return frame


def _folded(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def persons_named(frame: Any, values: Sequence[Any]) -> list[dict[str, Any]]:
    """The frame's people with routes (``TaskFrame.persons``) that the model's own call names (keyword audit D1, 20 Oct
    2026): ``values`` are the person values of that call (``about``'s name, the situation check's ``about``, a skill's
    person slot). A person counts only by identity: their name as memory knows it, a name an app knows them by
    (``known_as``), their ``contact:<id>`` or one of their route ids, case and spaces aside; never a part of a name and
    never the request's words. The frame's people block stays the model's context; this only says which of them the
    model chose, for the UI's "from memory" chip. Never raises."""
    try:
        wanted = {_folded(v) for v in values or () if isinstance(v, (str, int)) and _folded(v)}
        if not wanted:
            return []
        out: list[dict[str, Any]] = []
        for p in list(getattr(frame, "persons", None) or []):
            if not isinstance(p, dict) or not p.get("name"):
                continue
            routes = [r for r in p.get("routes") or [] if isinstance(r, dict)]
            ids = {_folded(p.get("name")), _folded(f"contact:{p.get('id')}") if p.get("id") is not None else ""}
            ids |= {_folded(r.get("known_as")) for r in routes} | {_folded(r.get("ref")) for r in routes}
            if wanted & (ids - {""}):
                out.append(p)
        return out
    except Exception:  # noqa: BLE001 - presentation only: no chip
        return []


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


#: How a route's kind reads in the frame (memory's people index: dm | group | channel).
KIND_WORDS: dict[str, str] = {"dm": "a DM", "group": "a group chat", "channel": "a channel"}


def _tags(n: Any, last: Any) -> str:
    """"the user tagged them here 9 times, last Wed 30 Sep 21:30" (their name in the user's own messages there)."""
    if not n:
        return ""
    return f"the user tagged them here {int(n)} time{'' if int(n) == 1 else 's'}" + (
        f", last {_day(last)}" if last else "")


def _person_lines(frame: TaskFrame, overview: Any) -> list[str]:
    lines: list[str] = []
    for p in frame.persons:
        head = p["name"] + (f" ({p['relation']})" if p.get("relation") else "")
        routes = p.get("routes") or []
        if not routes:
            lines.append(f"{head} - {p['why']}: no chat with them in memory (about({p['name']!r}) for what memory has).")
            continue
        # the name each app knows them by: what a search or a mention needs (Sam may be "samk77" on Discord)
        known = list(dict.fromkeys(f"{r.get('app') or 'the app'} knows them as \"{r['known_as']}\""
                                   for r in routes[:MAX_ROUTES] if r.get("known_as")))
        lines.append(f"{head} - {p['why']}. Where the user talks with them, most used lately first:")
        if known:
            lines.append("  " + "; ".join(known) + f": search for and mention that name, not \"{p['name']}\".")
        for i, r in enumerate(routes[:MAX_ROUTES], 1):
            notes = []
            if r.get("preferred"):
                notes.append("the user chose this one")
            if r.get("in_front"):
                notes.append("in front now")
            last = f", last yours {_day(r.get('last_mine'))}" if r.get("last_mine") else ""
            kind = KIND_WORDS.get(str(r.get("kind") or ""), str(r.get("kind") or "a chat"))
            tagged = _tags(r.get("tags"), r.get("last_tag"))
            lines.append(f"  {i}. [{r.get('ref')}] {r.get('label')} - {kind}: the user {r.get('mine')} / them "
                         f"{r.get('theirs')}{last}" + (f"; {tagged}" if tagged else "")
                         + (f" ({'; '.join(notes)})" if notes else ""))
        if p.get("preferred") and routes[0].get("preferred"):
            lines.append("  Usual: the one the user chose (\"" + _one((p.get("preferred") or {}).get("quote"), 60) + "\").")
        elif len(routes) > 1 and p.get("close"):
            lines.append(f"  Close call between the first two (margin {float(p.get('margin') or 0):.2f}): unless the "
                         "user named one, ask naming both.")
        elif len(routes) > 1:
            lines.append(f"  Usual: the first, clearly (margin {float(p.get('margin') or 0):.2f}).")
        places = [x for x in p.get("places") or [] if isinstance(x, dict)]
        channel = _channel_example(p, routes, places)
        if channel:
            lines.append("  " + channel)
        said = [s for s in p.get("samples") or [] if s]
        if said:
            lines.append("  The user's own last messages on the usual route (draft to them like these, and say so in "
                         "a few words): " + " | ".join(f"\"{_one(s, 90)}\"" for s in said[:2]))
        if places:
            lines.append("  Group chats and channels on the same apps (where the user tags them first, then where the "
                         "user writes most): " + "; ".join(_place_text(x) for x in places))
        lines += ["  " + x for x in preconditions(p, overview)]
        whom = "everyone in that group" if routes[0].get("kind") in ("group", "channel") else p["name"]
        lines.append(f"  Stakes: a message sent on the usual route reaches {whom}; once sent it cannot be taken back.")
    return lines


def _place_text(x: dict[str, Any]) -> str:
    """One group chat or channel near a person: its id and label, its kind, the user's tags of them, their messages."""
    kind = KIND_WORDS.get(str(x.get("kind") or ""), "a group")
    notes = [_tags(x.get("tags"), x.get("last_tag")) if x.get("tags") else "",
             f"they wrote {x.get('theirs')}" if x.get("theirs") else "", f"the user wrote {x.get('mine')}"]
    return f"[{x.get('ref')}] {x.get('label')} - {kind} ({'; '.join(n for n in notes if n)})"


def _channel_example(p: dict[str, Any], routes: Sequence[dict[str, Any]], places: Sequence[dict[str, Any]]) -> str:
    """A worked example from this person's own data for a request that names a channel or a group ("tag him in my
    usual channel", "where I tag him"): the group or channel where the user tags them most, else the one the user
    writes in most - never their DM. Which one the request means stays the model's judgement; "" with no group or
    channel on record."""
    # a group route of theirs is one they are in; a place counts when the user tagged them there or they write there
    rooms = [{**r, "theirs": r.get("theirs") or 1} for r in routes if r.get("kind") in ("group", "channel")]
    rooms += [x for x in places if int(x.get("tags") or 0) > 0 or int(x.get("theirs") or 0) > 0]
    head = ("When the request names a channel or a group (\"my usual channel\", \"where I tag him\"), it means a group "
            "or channel, never the DM")
    if not rooms:
        if not places:
            return ""
        return (f"{head}; memory has no group or channel with them in it (no tags of them, no messages of theirs): "
                "ask which one, naming the group chats listed here.")
    best = max(rooms, key=lambda r: (int(r.get("tags") or 0), float(r.get("last_tag") or 0.0),
                                     int(r.get("theirs") or 0) > 0, int(r.get("mine") or 0)))
    why = (_tags(best.get("tags"), best.get("last_tag")) if best.get("tags") else
           f"no tags of them on record; they write there and so does the user ({best.get('mine')})")
    return (f"{head}: here [{best.get('ref')}] {best.get('label')} ({why}). Example: \"tag {p['name']} in my usual "
            "channel\" -> go to that channel, mention them by the name the app knows them by, send there, and say "
            "which channel.")


#: N8 (report 47): this session's troubled runs on the apps in play, from their traces, before memory learns them.
LESSONS_HEAD = ("Earlier in this session on the apps in play, from your own runs (not learned yet; never repeat what "
                "did not work, and start from what worked):")
MAX_LESSONS = 4


def _fit(lines: list[str], chars: int, keep: Sequence[str] = (), pinned: Sequence[str] = ()) -> str:
    """The frame within ``chars`` (the label and end line included); when it is over, the last lines of ``lines`` go
    first and ``keep`` (placed after them) only once ``lines`` are gone, its own last lines first. ``pinned`` (phase S)
    goes first after the label and is **never popped**: the others share what it leaves (it is capped when built,
    :data:`PINNED_CHARS`). ``""`` for no lines."""
    body, tail, pin = list(lines), list(keep), [str(x) for x in pinned]
    if not body and not tail and not pin:
        return ""

    def trim(xs: list[str]) -> None:
        while xs and xs[-1].rstrip().endswith(":"):   # a heading whose items were cut goes with them
            xs.pop()

    while True:
        trim(body)
        trim(tail)
        if not body and not tail and not pin:
            break
        text = LABEL + "\n" + "\n".join(pin + body + tail) + "\n" + END
        if len(text) <= chars or not (body or tail):
            return text if (len(text) <= chars or pin) else ""
        (body if body else tail).pop()
    return ""


#: The label the voice bridge puts on its own note to a running request (``yuki.voice.bridge``: the app they named,
#: the references resolved): Yuki's words, not the user's.
VOICE_NOTE_LABEL = "(From Yuki's voice, about the request you are on)"


def users_steers(steers: Sequence[str] | None) -> list[str]:
    """The words the user added while a run went on, without Yuki's voice's own notes (vfix3: live 4 Oct a note that
    only named the app was kept in practice memory as "they said while it ran", and made a run without trouble a
    pitfall). Our own fixed label, never the words."""
    return [s for s in steers or () if not str(s).lstrip().startswith(VOICE_NOTE_LABEL)]


def session_lessons(trace: Sequence[dict[str, Any]], *, outcome: str, steers: Sequence[str] = (),
                    at: float | None = None, stamped: bool = True) -> dict[str, list[str]]:
    """N8 (report 47): from one finished run's trace, per app, what did not work and what worked after it, as short
    lines for the next requests of this session (:func:`build`'s ``lessons``), or ``{}`` for a run with no trouble.

    Trouble is measured: a step that failed, was held, or whose report showed no effect (contradicted), the same
    control acted on three times or more, a run that ended cancelled, withdrawn or failed after acting, or words the
    user added mid-run. Each line names the tool, the control (role and name, never ids or coordinates) and how many
    times, with the step's own why; what worked is the steps verified after the first trouble. Code only."""
    import collections
    from datetime import datetime as _dt

    steers = users_steers(steers)
    steps = [s for s in trace or () if isinstance(s, dict) and s.get("kind") in ("action", "control")
             and s.get("tool") not in ("tasks",)]
    acted = [s for s in steps if s.get("kind") == "action"]

    def settled(i: int, s: dict[str, Any]) -> bool:
        """A hold (a check's refusal: look first, the user's confirmation) that the same tool in the same app then got
        past in this run is the loop working, no pitfall (review F8)."""
        return s.get("kind") == "control" and any(
            later.get("kind") == "action" and later.get("tool") == s.get("tool") and later.get("ok") is True
            and (not s.get("app") or not later.get("app") or later.get("app") == s.get("app"))
            for later in steps[i + 1:])

    bad = [s for i, s in enumerate(steps) if (s.get("ok") is False or s.get("verdict") is False)
           and not settled(i, s)]
    counts = collections.Counter((s.get("app") or "", s.get("tool"), s.get("target") or "") for s in acted)
    repeated = [k for k, n in counts.items() if n >= 3 and k[2]]
    ended_badly = outcome in ("cancelled", "withdrawn", "error", "failed") and bool(acted)
    if not bad and not repeated and not ended_badly and not steers:
        return {}
    clock = _dt.fromtimestamp(at).strftime("%I:%M %p").lstrip("0") if at else "earlier"
    pre = f"{clock}: " if stamped else ""
    out: dict[str, list[str]] = collections.defaultdict(list)
    seen: set[tuple[str, Any, str]] = set()
    for s in bad:
        key = (s.get("app") or "", s.get("tool"), s.get("target") or "")
        if key in seen:
            continue
        seen.add(key)
        n = counts.get(key, 1)
        why = _one(s.get("why") or s.get("result"), 120)
        out[key[0]].append(f"{pre}{key[1]}" + (f" on {key[2]}" if key[2] else "") + (f", {n} times" if n > 1
                                                                                         else "") + f": {why}")
    for key in repeated:
        if key not in seen:
            seen.add(key)
            out[key[0]].append(f"{pre}{key[1]} on {key[2]} {counts[key]} times in one run (it did not settle "
                               f"it; read the state before acting again)")
    first_bad = min((trace.index(s) for s in bad if s in trace), default=None)
    if first_bad is not None:
        # vfix3: what worked after a failed step in an app must have acted on something there (a control or its
        # window): live 4 Oct the Windows mixer level (no window, no control) was learned as the way instead of the
        # player's own slider the user had asked for, and every later request was steered to the mixer
        worked = [s for s in list(trace)[first_bad + 1:] if s.get("kind") == "action" and s.get("verdict") is True
                  and str(s.get("target") or "").strip()]
        for s in worked[:1]:
            out[s.get("app") or ""].append(f"{pre}what worked after that: {s.get('tool')}"
                                           + (f" on {s.get('target')}" if s.get("target") else ""))
    if ended_badly or steers:
        app = next((s.get("app") for s in reversed(acted) if s.get("app")), "")
        said = "; ".join(f"\"{_one(w, 80)}\"" for w in list(steers)[:2])
        out[app or ""].append(f"{pre}the run ended {outcome}" + (f"; they said while it ran: {said}" if said else ""))
    return {k: v[:MAX_LESSONS] for k, v in out.items() if v}


#: N9: the people block of a spoken request: at most this many people, the calls of this many hours.
MAX_LATELY = 8
LATELY_CALL_HOURS = 48.0
LATELY_HEAD = ("Who they deal with lately (for a name speech recognition may have misheard: first how the heard words "
               "sound, then this and what the request is about):")


def people_lately(people: Sequence[dict[str, Any]] | None, calls: Sequence[dict[str, Any]] | None = None, *,
                  now: float | None = None, limit: int = MAX_LATELY) -> list[str]:
    """N9 (report 47, review): one line per person the user deals with lately, for resolving a misheard name: their
    relation, how long since they were last in touch, the chat they use most with them, their messages a day over the
    last two weeks (closeness, measured) and the calls of the last :data:`LATELY_CALL_HOURS` they were on (the call
    digest's reading of the screen around it). From memory's ``people_list`` (most recently in touch first) and
    ``calls_in``; people only on a call come too. Deterministic formatting; never raises (``[]``)."""
    from datetime import datetime as _dt

    now = time.time() if now is None else float(now)
    try:
        on_calls: dict[str, list[str]] = {}
        shown: dict[str, str] = {}
        for c in calls or ():
            if not isinstance(c, dict):
                continue
            try:
                start = _dt.fromisoformat(str(c.get("start"))).timestamp()
            except (TypeError, ValueError):
                continue
            day = "today" if _dt.fromtimestamp(start).date() == _dt.fromtimestamp(now).date() else \
                _dt.fromtimestamp(start).strftime("%a %d %b").replace(" 0", " ")
            when_ = f"{day}, {_dt.fromtimestamp(start).strftime('%I:%M %p').lstrip('0')}"
            app = str(c.get("service") or c.get("app") or "a").strip()
            for p in c.get("people") or []:
                name = " ".join(str((p or {}).get("name") or "").split())[:60] if isinstance(p, dict) else ""
                if name:
                    shown.setdefault(name.casefold(), name)
                    on_calls.setdefault(name.casefold(), []).append(f"on a {app} call with them {when_}")
        out: list[str] = []
        named: set[str] = set()
        for row in people or ():
            if not isinstance(row, dict) or not row.get("name"):
                continue
            name = " ".join(str(row["name"]).split())[:60]
            bits = [name + (f" ({row['relation']})" if row.get("relation") else "")]
            if row.get("aliases"):
                bits.append("also heard as " + ", ".join(f"\"{a}\"" for a in list(row["aliases"])[:3]))
            last = row.get("last_contact")
            if isinstance(last, (int, float)) and last > 0:
                age = now - float(last)
                bits.append(f"last in touch {_ago(age) if age < 86400 else f'{int(age // 86400)} days ago'}")
            usual = row.get("usual_route") if isinstance(row.get("usual_route"), dict) else None
            label = (usual or {}).get("label") or next((r.get("label") for r in row.get("routes") or []
                                                        if isinstance(r, dict) and r.get("label")), None)
            if label:
                bits.append(f"mostly {label}")
            rhythm = row.get("rhythm") if isinstance(row.get("rhythm"), dict) else {}
            total, days = rhythm.get("total"), rhythm.get("known_days") or len(rhythm.get("days") or []) or 14
            if isinstance(total, (int, float)) and total > 0:
                per_day = float(total) / max(1, int(days))
                bits.append(f"{per_day:.0f} messages a day both ways" if per_day >= 1 else "a few messages a week"
                            if per_day * 7 >= 1 else "rarely writes")
            bits += on_calls.get(name.casefold(), [])[:1]
            named.add(name.casefold())
            out.append("; ".join(bits))
            if len(out) >= limit:
                break
        for key, seen in on_calls.items():   # on a recent call, not among the people listed
            if key not in named and len(out) < limit + 2:
                out.append(f"{shown.get(key, key)}: {seen[0]}")
        return out
    except Exception:  # noqa: BLE001 - a missing block never breaks a request
        return []
