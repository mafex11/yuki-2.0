"""The skill menu: what the talker and the thinker see of the skills, and their ``run_skill`` schemas (map-skills
S3.1, S3.5; PLAN.md B.2 "Talker cache"; phase L design §2 "The request path", §4.2, §4.9).

**What is on it** (phase L, branch lPage):

* every **capability** first, one neutral line each (at most 8, from the shipped files): always there, so any request
  runs at rung A from day one whatever is learned (design §2, critique H1, acceptance A2). Nothing learned removes or
  shortens one, and the size cap never cuts one (failure fix-plan Item 1); only a capability with no usable rung and
  no hand-off is left out (nothing it could do);
* then the user's **learned skills** that fix a key value (a site, a calling service) and carry a real name (the
  naming call's or the user's own; ``Skill.name``), each on its own named line: the talker gets the top
  :data:`TALKER_LEARNED` (12), the thinker every live one (the catalog keeps at most 20), in the catalog's order
  (trusted first, then the most used). A learned skill that fixes no key value (a usage record: messages, voice joins,
  volume) adds no line: its capability's line stands for it (design §4.2). Nor does a nameless one (a night with no
  naming budget, e.g. ``message#all``: its code-built label names a browser it doesn't bind): its capability's line
  stands for it too. With the skills switch at ``"capabilities"`` the catalog holds no learned skill at all
  (:func:`yuki.skills.catalog.load`), so only the capability lines are left.

Within ``Settings.skills_menu_chars`` (2,400) the learned lines give way (they come last); the capability lines are
always on. One line each, at most 140 characters::

    - spotify.playlist: play a playlist, album or liked songs from Spotify's Your Library, by its name [playlist]
    - <learned key>: <its name> [site = <fixed value>] [item] [browser]

A capability's line: its ``menu``, its slot names, the options of an enum slot and up to 5 taste candidates of an
entity slot (the user's own names, best at this hour first). A learned skill's line: its name (the naming call's or
the user's own), its fixed values (``[slot = value]``) and the slots still filled per request. Never its free-text
learned menu line (failure fix-plan Item 1: "open Claude (claude.ai) in Chrome" named a browser the skill does not
bind, and steered the talker and the planner away from the browser the user named; a means slot is never bound, so
the browser stays a free slot the user's words fill). Nor a means slot's value inside the name (vRound2: "Open
Excalidraw in Arc" reads "Open Excalidraw"; :func:`yuki.skills.catalog.means_free`, by the slot's own data).
A learned skill runs only for its own fixed key values: words naming another value run its capability (lSplit review
r2 L1; :func:`request_key`), and the headings say so. People are not listed (the talker has the people directory). A
skill with no usable rung and no hand-off is left out. The talker's menu is rebuilt nightly (it sits in the cached
``HANDS_PROMPT`` block; same-day changes go in its volatile ``skills_now``); the thinker's sits in its ``run_skill``
tool's description and changes only with :func:`yuki.skills.registry.menu_version`.

The new brain's planner (bSkill, brain-design 2.1 and 6) has its own audience, :data:`PLANNER`: the thinker's skills,
headed by :data:`PLANNER_HEAD` (how a line is routed, ``route = "skill:<id>"``, and filled), in the planner's cached
system block behind breakpoint 3 (:func:`planner_menu`). Skill ids never enter its tool schema: code checks the
route's id against the live catalog.
"""

from __future__ import annotations

from typing import Any, Mapping

from yuki.skills.catalog import load, means_free
from yuki.skills.connection import MENU_LINE_CHARS, Loaded, Skill, scope_value

#: The talker's and the thinker's menu size cap (``Settings.skills_menu_chars``).
MENU_CHARS = 2400
TALKER = "talker"
THINKER = "thinker"
#: The new brain's planner (bSkill, brain-design 2.1 / 6): its menu sits in the system block behind cache breakpoint 3.
PLANNER = "planner"
AUDIENCES: tuple[str, ...] = (TALKER, THINKER, PLANNER)
#: The talker's menu holds every capability line plus at most this many learned skills (design §4.9).
TALKER_LEARNED = 12
#: A fixed value on a learned skill's line is cut to this.
VALUE_CHARS = 30


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


def binds_key(skill: Skill) -> bool:
    """Whether a learned skill fixes a key value (its own menu line, design §4.2); ``False`` for a capability and for
    a usage record (it fixes no key value)."""
    return skill.learned and any(s.role == "key" and s.name in skill.bound for s in skill.slots)


def listed(skill: Skill) -> bool:
    """Whether a learned skill gets a menu line of its own: it fixes a key value (:func:`binds_key`) and carries a real
    name (the catalog blanks a nameless row's code-built label: ``yuki.skills.catalog.named``). ``False`` for a
    capability (its line is always there: :func:`menu_skills`)."""
    return binds_key(skill) and bool(" ".join(str(skill.name or "").split()))


def menu_skills(loaded: Loaded, audience: str = TALKER) -> list[Skill]:
    """The skills a menu lists, in order: every capability (always), then the learned skills with a line of their own
    (:func:`listed`: a fixed key value and a real name; the talker's top :data:`TALKER_LEARNED`; the thinker's and the
    planner's all, at most the catalog's 20), in the catalog's order."""
    caps = [s for s in loaded.skills if not s.learned]
    named = [s for s in loaded.skills if s.learned and listed(s)]
    return caps + (named if audience in (THINKER, PLANNER) else named[:TALKER_LEARNED])


def _value(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("name") or ""
    return _clip(str(value or ""), VALUE_CHARS)


def _label(skill: Skill) -> str:
    """A learned skill's words on its line: its name alone (failure fix-plan Item 1). Its learned menu line is free
    text from the naming call and named browsers the skill does not bind ("in Chrome"); the fixed values that follow
    the name say what the skill is.

    vRound2: never a means slot's value either (sim-e2e-1: the talker read "ARC browser" as "Open Excalidraw in Arc"):
    the name with each unbound means slot's value phrase cut, by the slot's own data
    (:func:`yuki.skills.catalog.means_free`; the catalog already cuts the names Yuki made, so this cuts the user's own
    Rename on this line only). When no clean cut is possible, the line takes its capability's neutral words instead."""
    name = " ".join(str(skill.name or "").split())
    return means_free(name, skill) or " ".join(str(skill.menu or "").split()) if name else name


def _slot_part(slot: Any, names: list[str]) -> str:
    if slot.type == "enum" and slot.options:
        return f"[{slot.name}: {' | '.join(slot.options)}]"
    if names:
        return f"[{slot.name}: {' | '.join(_clip(n, 30) for n in names[:5])}]"
    return f"[{slot.name}]"


def skill_line(skill: Skill, *, candidates: dict[str, list[str]] | None = None) -> str:
    """One menu line (at most :data:`MENU_LINE_CHARS`): key, menu words, each slot with its options or candidates. A
    learned skill's line names it (its name only, never its learned menu text), shows its fixed values (``[slot =
    value]``) and lists only the slots still filled per request."""
    cands = candidates or {}
    bound = dict(skill.bound) if skill.learned else {}
    head = f"- {skill.key}: {_label(skill) if skill.learned else skill.menu}"
    fixed = [f"[{s.name} = {_value(bound[s.name])}]" for s in skill.slots if s.name in bound and _value(bound[s.name])]
    free = [s for s in skill.slots if s.name not in bound]
    tries = (
        [head, *fixed, *(_slot_part(s, list(cands.get(s.name) or [])) for s in free)],
        # the slot lists give way first, then the free slots, then the words
        [head, *fixed, *(f"[{s.name}]" for s in free)],
        [head, *fixed],
    )
    for parts in tries:
        line = " ".join(parts)
        if len(line) <= MENU_LINE_CHARS:
            return line
    return _clip(" ".join(tries[-1]), MENU_LINE_CHARS)


def menu_lines(loaded: Loaded, *, store: Any = None, now: float | None = None, chars: int = MENU_CHARS,
               rank: str = "hour", audience: str = TALKER) -> list[str]:
    """The menu's lines (:func:`menu_skills` for ``audience``): every capability line, then the learned lines within
    ``chars`` (they give way; a capability line is never cut for size: Item 1). With ``store``: the taste candidates of
    entity slots (``rank``: ``hour``, best at this hour first, or ``n_obs``, most seen first: stable between the
    nightly recomputes) and only skills that have a usable rung or may hand over."""
    from yuki.skills import registry, taste

    out: list[str] = []
    used = 0
    for skill in menu_skills(loaded, audience):
        cands: dict[str, list[str]] = {}
        if store is not None:
            table = registry.trust(store, skill.key, now=now)
            if not skill.handoff and not any(registry.usable(r, table.get(r.key), now=now)[0] for r in skill.routes):
                continue
            for slot in skill.slots:
                if slot.taste and slot.name not in skill.bound:
                    cands[slot.name] = taste.candidates(store, slot.taste, now=now, by=rank)
        line = skill_line(skill, candidates=cands)
        if skill.learned and used + len(line) + 1 > chars:
            break
        out.append(line)
        used += len(line) + 1
    return out


#: The talker's heading (its menu follows).
TALKER_HEAD = ("Skills (run_skill with the key; slots only from their words or a name listed). A line with a name is "
               "one the user does often, its = values fixed; when they name another value, use the plain line:")


#: The planner's heading (bSkill): what a line is and how its slots are filled. Generic: no app, no skill, no case.
#: The route (``skill:<id>``) and the step value's shape are in the core prompt and the plan tool; a send slot left
#: out is refused by the consent rule (bExec merge note 3), so the heading says to fill it.
PLANNER_HEAD = ("Each line: - <id>: what it does [slot] or [slot: option | option]. A named line is a habit learned "
                "from the user, its = values fixed: when their words name another value, use the plain line. Slots "
                "only from their words or a listed name or option; leave the rest out (Yuki fills them). A skill that "
                "sends: its send slot takes the sending option only with consent.commit send, else the other.")


def menu_text(store: Any = None, *, audience: str = TALKER, loaded: Loaded | None = None, now: float | None = None,
              chars: int = MENU_CHARS, rank: str | None = None) -> str:
    """The menu block (``""`` when no skill is loaded). The talker's starts with its heading; the thinker's is the
    bare lines (its tool description carries the heading) with taste candidates most seen first, so its tool block
    stays byte-stable through the day (review L2: an hour-ranked list changed the cached block every hour). The
    planner's (bSkill) starts with :data:`PLANNER_HEAD` and lists what the thinker's lists, taste most seen first: it
    sits in the planner's cached system block behind breakpoint 3, so it changes only when the catalog, the trust
    ledger or the nightly taste does; ``chars`` covers the heading too.
    ``rank``: the taste order (``hour`` / ``n_obs``) when not the audience's own (the requests suite's eval prompt
    takes ``n_obs``, so its cached prefix holds across the fixtures' hours)."""
    loaded = loaded if loaded is not None else load()
    rank = rank or ("n_obs" if audience in (THINKER, PLANNER) else "hour")
    room = max(0, chars - len(PLANNER_HEAD) - 1) if audience == PLANNER else chars
    lines = menu_lines(loaded, store=store, now=now, chars=room, rank=rank, audience=audience)
    if not lines:
        return ""
    if audience == TALKER:
        return TALKER_HEAD + "\n" + "\n".join(lines)
    if audience == PLANNER:
        return PLANNER_HEAD + "\n" + "\n".join(lines)
    return "\n".join(lines)


def planner_menu(client: Any = None, *, loaded: Loaded | None = None, now: float | None = None,
                 chars: int = MENU_CHARS) -> str:
    """The new brain's menu for its breakpoint-3 block (bSkill; :func:`menu_text` with :data:`PLANNER`). ``client``:
    memory's client, as ``MemoryAccess.call`` hands it (``yuki.memory.api.MemoryClient``: its store-aware catalog,
    the trust ledger and the user's taste; the learned skills come from there), or ``None`` for the shipped files
    alone (no candidates; every skill with a rung). ``""`` when no skill is loaded. Never raises::

        lines = memory.call("skill_menu", planner_menu, timeout_s=...)     # yuki.brain.context.read_menu
    """
    store = getattr(client, "store", None) if client is not None else None
    if loaded is None and client is not None:
        seeded = getattr(client, "_skills_seeded", None)
        try:
            got = seeded() if callable(seeded) else None
        except Exception:  # noqa: BLE001 - the files alone
            got = None
        loaded = got if got is not None and hasattr(got, "skills") else None
    try:
        return menu_text(store, audience=PLANNER, loaded=loaded, now=now, chars=chars)
    except Exception:  # noqa: BLE001 - a menu that can't be built is no menu (the planner routes steps)
        return ""


def request_key(loaded: Loaded, key: str, slots: Mapping[str, Any] | None) -> str:
    """The skill a request runs (lSplit review r2 L1): a learned skill runs only for its own fixed key values, so when
    the request's slots name another value for one of them it is its capability's run (and never counts on the
    learned skill's ledger, D14). Values compare as one identity (:func:`yuki.skills.connection.scope_value`:
    "Example" and "example.com" are the same site). Any other key comes back as it is."""
    skill = loaded.get(str(key or ""))
    if skill is None or not skill.learned or not skill.capability:
        return str(key or "")
    return skill.capability if names_other_key(skill, slots) else skill.key


def names_other_key(skill: Any, slots: Mapping[str, Any] | None) -> bool:
    """The one rule behind :func:`request_key` and the engine's ledger (``engine.ledger_skill``; lCore review L11):
    whether ``slots`` name another value for one of a learned skill's bound key slots (compared as one identity,
    :func:`yuki.skills.connection.scope_value`). False for a capability or a skill with nothing bound."""
    bound = getattr(skill, "bound", None) or {}
    if not bound or not callable(getattr(skill, "slot", None)):
        return False
    for name, value in (slots or {}).items():
        slot = skill.slot(str(name))
        if slot is None or slot.role != "key" or slot.name not in bound or value in (None, ""):
            continue
        if scope_value(value) != scope_value(bound[slot.name]):
            return True
    return False


def talker_tool(keys: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """The talker's ``run_skill`` hand (Pipecat standard function schema fields; map-skills S3.1): no steps, no
    "say when done" (code says the done line once the check saw it). ``say_first`` comes first (it is said while the
    rest is still being written). The voice builder registers it (``voice/hands.py``)."""
    return {
        "name": "run_skill",
        "description": "Run one of the skills on the Skills list; it says its own done line.",
        "properties": {
            "say_first": {"type": "string", "description": "Written first: words said as it starts, or \"\"."},
            "skill": {"type": "string", "enum": list(keys)},
            "slots": {"type": "object", "description": "Only from their words, exactly as said, or a name the Skills "
                                                       "line lists; leave the rest out (Yuki fills them)."},
            "again": {"type": "boolean", "description": "They asked for the same again (\"a bit more\")."},
            "corrects": {"type": "boolean", "description": "They corrected the last run of this skill."},
        },
        "required": ["say_first", "skill", "slots"],
    }


#: The thinker's tool's fixed head (its menu follows).
THINKER_HEAD = ("Run a skill: a built-in way to do something on this PC, or one Yuki learned from the user (a line "
                "with a name; its = values are fixed, so when they name another value use the plain line). It takes "
                "the route that works on this PC, checked by the PC before it counts as done. Give slots only from "
                "their words or a name listed; leave the rest out (Yuki fills them from their usuals). Done only when "
                "its check saw the result. Skills:")


def thinker_tool(menu: str, keys: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """The thinker's ``run_skill`` tool (Anthropic tool shape, ``agent/tools.py``): the talker's fields minus
    ``say_first``; its description carries the thinker menu (cached; it changes only with the menu's version)."""
    return {
        "name": "run_skill",
        "label": "Running your usual way",
        "description": f"{THINKER_HEAD}\n{menu}" if menu else THINKER_HEAD,
        "input_schema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "enum": list(keys)} if keys else {"type": "string"},
                "slots": {"type": "object"},
                "again": {"type": "boolean"},
                "corrects": {"type": "boolean"},
            },
            "required": ["skill"],
        },
    }


__all__ = ["MENU_CHARS", "TALKER", "THINKER", "PLANNER", "AUDIENCES", "TALKER_LEARNED", "TALKER_HEAD", "PLANNER_HEAD",
           "names_other_key", "binds_key", "listed", "menu_skills", "skill_line", "menu_lines", "menu_text",
           "planner_menu", "request_key", "talker_tool", "THINKER_HEAD", "thinker_tool"]
