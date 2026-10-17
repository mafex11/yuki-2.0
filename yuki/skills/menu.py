"""The skill menu: what the talker, the thinker and the planner see of the skills, and their ``run_skill`` schemas
(map-skills S3.1, S3.5; PLAN.md B.2 "Talker cache"; audit 17 Oct contracts C4, C5).

**What is on it**:

* every **capability**, one line each (from the shipped files): always there, so any request runs at rung A whatever
  is learned; only a capability with no usable rung and no hand-off is left out (nothing it could do). After its words
  and slots, the line carries **the user's own values for it** in a compact budget of their own
  (:data:`VALUES_CHARS`; contract C5): the things the learner knows they use with it (their sites, with the browser
  each usually opens in and a work mark; their meeting tabs), an entity slot's taste names (their playlists), and for
  a capability that opens a site's pages the names of each site page kind (their YouTube channels). Best first: by
  recency-weighted use (``rank="n_obs"``: stable through the day for a cached prompt block) or lifted by this hour
  (``rank="hour"``). A value never has a line of its own: the per-value learned lines are gone (one skill, many
  values; a value the user marked Not mine is never shown);
* then **the user's own ways** (contract C4: lane kWays' ``origin = 'learned_way'`` rows), one short line each, run
  through the ``app.way`` capability with ``way`` set to the line's value.

Within ``Settings.skills_menu_chars`` (2,400) the values give way first (half their budget, then none); a capability's
words are never cut for size. One line each::

    - spotify.playlist: play a playlist, album or liked songs from Spotify's Your Library, by its name [playlist] · theirs: [playlist: lumen | 3AM]
    - web.open_page: open a site's front page ... [site] [page] [item] [browser] · theirs: youtube, claude.ai (work) ... · usually in Chrome
    - app.way: <the way's name> [way = <its key>] [item] [text]

People are not listed (the talker has the people directory). The talker's menu is rebuilt nightly (it sits in the
cached ``HANDS_PROMPT`` block; same-day changes go in its volatile ``skills_now``); the thinker's sits in its
``run_skill`` tool's description and changes only with :func:`yuki.skills.registry.menu_version`. The new brain's
planner has its own audience, :data:`PLANNER`, headed by :data:`PLANNER_HEAD`, in the planner's cached system block
behind breakpoint 3 (:func:`planner_menu`).
"""

from __future__ import annotations

from typing import Any, Mapping

from yuki.skills.catalog import load
from yuki.skills.connection import MENU_LINE_CHARS, Loaded, Skill, scope_value

#: The talker's and the thinker's menu size cap (``Settings.skills_menu_chars``).
MENU_CHARS = 2400
TALKER = "talker"
THINKER = "thinker"
#: The new brain's planner (bSkill, brain-design 2.1 / 6): its menu sits in the system block behind cache breakpoint 3.
PLANNER = "planner"
AUDIENCES: tuple[str, ...] = (TALKER, THINKER, PLANNER)
#: A capability line's own room for the user's values (contract C5), on top of its words (MENU_LINE_CHARS).
VALUES_CHARS = 420
#: Values named per capability at most; names per entity slot or site page kind at most.
VALUES_N = 10
NAMES_N = 5
#: A value or a name on a line is cut to this.
VALUE_CHARS = 30
#: Lane kWays' learned ways (contract C4): their rows' origin, and the capability they run through.
WAY_ORIGIN = "learned_way"
WAY_CAPABILITY = "app.way"
#: The talker lists at most this many learned ways.
WAYS_N = 10


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


def menu_skills(loaded: Loaded, audience: str = TALKER) -> list[Skill]:
    """The skills a menu lists, in order: every capability (always). A learned row is never a line of its own (its
    values ride on its capability's line, contract C5); the learned ways come from the store (:func:`way_lines`)."""
    return [s for s in loaded.skills if not s.learned]


def _value(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("name") or ""
    return _clip(str(value or ""), VALUE_CHARS)


def _slot_part(slot: Any) -> str:
    if slot.type == "enum" and slot.options:
        return f"[{slot.name}: {' | '.join(slot.options)}]"
    return f"[{slot.name}]"


def skill_line(skill: Skill, *, values: str = "") -> str:
    """One menu line: key, menu words and each slot (an enum slot with its options), at most
    :data:`MENU_LINE_CHARS` (the options give way first), then the user's values for it (``values``, from
    :func:`values_part`, in a budget of their own)."""
    head = f"- {skill.key}: {skill.menu}"
    tries = ([head, *(_slot_part(s) for s in skill.slots)], [head, *(f"[{s.name}]" for s in skill.slots)], [head])
    line = _clip(" ".join(tries[-1]), MENU_LINE_CHARS)
    for parts in tries:
        if len(" ".join(parts)) <= MENU_LINE_CHARS:
            line = " ".join(parts)
            break
    return f"{line} · {values}" if values else line


def _app_name(store: Any, key: str, names: dict[str, str]) -> str:
    if key and key not in names:
        try:
            names[key] = str(store.app_display_name(key) or key)
        except Exception:  # noqa: BLE001
            names[key] = key
    return names.get(key, "")


def values_part(skill: Skill, store: Any, *, now: float | None = None, rank: str = "hour",
                chars: int = VALUES_CHARS) -> str:
    """The user's own values for one capability, in at most ``chars`` (``""`` when none): the learner's known values
    (contract C5; their usual means when one is shared by most: "usually in Chrome", others named per value; a work
    value marked), each entity slot's taste names (``[playlist: a | b]``), and for a capability that opens site pages,
    each site page kind's taste names ("youtube handle: Sidemen | NDL"). Never raises."""
    from collections import Counter

    try:
        from yuki.skills import taste
        from yuki.skills.learn import values as V
    except Exception:  # noqa: BLE001
        return ""
    parts: list[str] = []
    taste_slots = {s.name for s in skill.slots if s.taste}
    try:
        vals = V.known(store, skill.key, now=now).get(skill.key) or []
        if rank == "n_obs":
            vals = sorted(vals, key=lambda v: (-float(v.get("score") or 0.0), str(v.get("id"))))
        vals = [v for v in vals if v.get("slot") not in taste_slots][:VALUES_N]
    except Exception:  # noqa: BLE001
        vals = []
    if vals:
        names: dict[str, str] = {}
        usual_of = {str(v.get("id")): next(iter((v.get("usual") or {}).values()), "") for v in vals}
        common, n = (Counter(u for u in usual_of.values() if u).most_common(1) or [("", 0)])[0]
        shared = common if n >= max(2, len(vals) // 2) else ""
        bits = []
        for v in vals:
            notes = []
            if v.get("everyday") is False:
                notes.append("work")
            u = usual_of.get(str(v.get("id")))
            if u and u != shared:
                notes.append(f"in {_app_name(store, u, names)}")
            bits.append(_value(v.get("name")) + (f" ({', '.join(notes)})" if notes else ""))
        parts.append("theirs: " + ", ".join(bits) + (f" · usually in {_app_name(store, shared, names)}" if shared
                                                       else ""))
    try:
        for slot in skill.slots:
            if slot.taste:
                got = [_value(n) for n in taste.candidates(store, slot.taste, now=now, by=rank)[:NAMES_N] if n]
                if got:
                    parts.append(f"[{slot.name}: {' | '.join(got)}]")
        if str(getattr(skill, "end", "") or "") == "call_join" and skill.app:
            # the voice channels a join was noted for on this app (tastes ``<app>_voice``, as the planner's places)
            got = [_value(n) for n in taste.candidates(store, f"{skill.app}_voice", now=now, by=rank)[:NAMES_N] if n]
            if got:
                parts.append(f"voice channels: {' | '.join(got)}")
        if any(src == "site" for src in (skill.bind or {}).values()):
            for kind, site, page, _var in taste.site_kinds():
                got = [_value(n) for n in taste.candidates(store, kind, now=now, by=rank)[:NAMES_N] if n]
                if got:
                    parts.append(f"{site} {page}: {' | '.join(got)}")
    except Exception:  # noqa: BLE001
        pass
    return _clip(" · ".join(parts), chars) if parts else ""


def way_lines(store: Any, *, limit: int = WAYS_N) -> list[str]:
    """Contract C4: one short line per learned way of the user's (lane kWays' rows: ``origin = 'learned_way'``, live,
    most used first): ``- app.way: <its name> [way = <its key>] [item] [text]``. ``[]`` with none (or before lane kWays'
    rows exist). Never raises."""
    if store is None:
        return []
    try:
        rows = store._query("SELECT key, name_ciphertext FROM skills WHERE origin = ? AND deleted_at IS NULL AND"
                            " retired_at IS NULL AND state IN ('candidate', 'trusted') ORDER BY uses DESC,"
                            " updated_at DESC LIMIT ?", (WAY_ORIGIN, int(limit)))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for key, name_ct in rows:
        try:
            name = " ".join(str(store._dec(name_ct) or "").split()) if name_ct else ""
        except Exception:  # noqa: BLE001
            name = ""
        if key and name:
            out.append(_clip(f"- {WAY_CAPABILITY}: {name} [way = {key}] [item] [text]", MENU_LINE_CHARS))
    return out


def menu_lines(loaded: Loaded, *, store: Any = None, now: float | None = None, chars: int = MENU_CHARS,
               rank: str = "hour", audience: str = TALKER) -> list[str]:
    """The menu's lines (:func:`menu_skills` for ``audience``): every capability line with the user's values (with
    ``store``: only skills that have a usable rung or may hand over), then the learned ways, within ``chars``: the
    values give way first (half their budget, then none), then the ways; a capability's own words are never cut."""
    from yuki.skills import registry

    skills: list[Skill] = []
    for skill in menu_skills(loaded, audience):
        if store is not None:
            table = registry.trust(store, skill.key, now=now)
            if not skill.handoff and not any(registry.usable(r, table.get(r.key), now=now)[0] for r in skill.routes):
                continue
        skills.append(skill)
    ways = way_lines(store) if store is not None else []
    out: list[str] = []
    for budget in (VALUES_CHARS, VALUES_CHARS // 2, 0):
        out = [skill_line(s, values=values_part(s, store, now=now, rank=rank, chars=budget)
                          if store is not None and budget else "") for s in skills]
        if sum(len(x) + 1 for x in out) + sum(len(x) + 1 for x in ways) <= chars or not budget:
            break
    used = sum(len(x) + 1 for x in out)
    for line in ways:
        if used + len(line) + 1 > chars:
            break
        out.append(line)
        used += len(line) + 1
    return out


#: The talker's heading (its menu follows).
TALKER_HEAD = ("Skills (run_skill with the key; slots only from their words or a name listed). After a line, "
               "\"theirs\" lists the user's own values for it, best first: match their words to one, or pick one "
               "when they ask you to choose. An app.way line is a way of theirs: run app.way with its way value:")


#: The planner's heading (bSkill): what a line is and how its slots are filled. Generic: no app, no skill, no case.
#: The route (``skill:<id>``) and the step value's shape are in the core prompt and the plan tool; a send slot left
#: out is refused by the consent rule (bExec merge note 3), so the heading says to fill it.
PLANNER_HEAD = ("Each line: - <id>: what it does [slot] or [slot: option | option], then \"theirs\": the user's own "
                "values for it, best first (an app.way line is a way of theirs: route app.way with its way value). "
                "Slots only from their words or a listed name or option; leave the rest out (Yuki fills them). A skill that "
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
THINKER_HEAD = ("Run a skill: a built-in way to do something on this PC (\"theirs\" after a line lists the user's "
                "own values for it, best first), or a way of the user's own (an app.way line: run app.way with its "
                "way value). It takes the route that works on this PC, checked by the PC before it counts as done. Give slots only from "
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


__all__ = ["MENU_CHARS", "TALKER", "THINKER", "PLANNER", "AUDIENCES", "VALUES_CHARS", "TALKER_HEAD", "PLANNER_HEAD",
           "names_other_key", "menu_skills", "skill_line", "values_part", "way_lines", "menu_lines", "menu_text",
           "planner_menu", "request_key", "talker_tool", "THINKER_HEAD", "thinker_tool"]
