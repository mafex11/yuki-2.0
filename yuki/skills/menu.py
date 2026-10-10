"""The skill menu: what the talker and the thinker see of the skills, and their ``run_skill`` schemas (map-skills
S3.1, S3.5; PLAN.md B.2 "Talker cache").

One line per skill, at most 140 characters, at most 2,400 in all (``Settings.skills_menu_chars``)::

    - spotify.playlist: play one of their Spotify playlists [playlist: Japanese | Lo-fi | Liked Songs]

Built by code from the connection's ``menu``, its slot names, the options of an enum slot and up to 5 taste
candidates of an entity slot (the user's own names, best at this hour first). People are not listed (the talker has the
people directory). A skill with no usable rung and no hand-off is left out. The talker's menu is rebuilt nightly
(it sits in the cached ``HANDS_PROMPT`` block; same-day changes go in its volatile ``skills_now``); the thinker's sits
in its ``run_skill`` tool's description and changes only with :func:`yuki.skills.registry.menu_version`.
"""

from __future__ import annotations

from typing import Any

from yuki.skills.connection import MENU_LINE_CHARS, Loaded, Skill, load

#: The talker's and the thinker's menu size cap (``Settings.skills_menu_chars``).
MENU_CHARS = 2400
TALKER = "talker"
THINKER = "thinker"


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


def skill_line(skill: Skill, *, candidates: dict[str, list[str]] | None = None) -> str:
    """One menu line (at most :data:`MENU_LINE_CHARS`): key, menu words, each slot with its options or candidates."""
    parts = [f"- {skill.key}: {skill.menu}"]
    for slot in skill.slots:
        names = list((candidates or {}).get(slot.name) or [])
        if slot.type == "enum" and slot.options:
            parts.append(f"[{slot.name}: {' | '.join(slot.options)}]")
        elif names:
            parts.append(f"[{slot.name}: {' | '.join(_clip(n, 30) for n in names[:5])}]")
        else:
            parts.append(f"[{slot.name}]")
    line = " ".join(parts)
    if len(line) > MENU_LINE_CHARS:
        # the slot lists give way first, then the words
        line = " ".join([parts[0]] + [f"[{s.name}]" for s in skill.slots])
    return _clip(line, MENU_LINE_CHARS)


def menu_lines(loaded: Loaded, *, store: Any = None, now: float | None = None, chars: int = MENU_CHARS,
               rank: str = "hour") -> list[str]:
    """The menu's lines in load order, within ``chars`` (later skills give way first). With ``store``: the taste
    candidates of entity slots (``rank``: ``hour``, best at this hour first, or ``n_obs``, most seen first: stable
    between the nightly recomputes) and only skills that have a usable rung or may hand over."""
    from yuki.skills import registry, taste

    out: list[str] = []
    used = 0
    for skill in loaded.skills:
        cands: dict[str, list[str]] = {}
        if store is not None:
            table = registry.trust(store, skill.key, now=now)
            if not skill.handoff and not any(registry.usable(r, table.get(r.key), now=now)[0] for r in skill.routes):
                continue
            for slot in skill.slots:
                if slot.taste:
                    cands[slot.name] = taste.candidates(store, slot.taste, now=now, by=rank)
        line = skill_line(skill, candidates=cands)
        if used + len(line) + 1 > chars:
            break
        out.append(line)
        used += len(line) + 1
    return out


def menu_text(store: Any = None, *, audience: str = TALKER, loaded: Loaded | None = None, now: float | None = None,
              chars: int = MENU_CHARS, rank: str | None = None) -> str:
    """The menu block (``""`` when no skill is loaded). The talker's starts with its heading; the thinker's is the
    bare lines (its tool description carries the heading) with taste candidates most seen first, so its tool block
    stays byte-stable through the day (review L2: an hour-ranked list changed the cached block every hour).
    ``rank``: the taste order (``hour`` / ``n_obs``) when not the audience's own (the requests suite's eval prompt
    takes ``n_obs``, so its cached prefix holds across the fixtures' hours)."""
    loaded = loaded if loaded is not None else load()
    rank = rank or ("n_obs" if audience == THINKER else "hour")
    lines = menu_lines(loaded, store=store, now=now, chars=chars, rank=rank)
    if not lines:
        return ""
    if audience == TALKER:
        return "Skills (the user's usual ways; run_skill with the key, slots only from their words or a name listed):\n" \
            + "\n".join(lines)
    return "\n".join(lines)


def talker_tool(keys: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """The talker's ``run_skill`` hand (Pipecat standard function schema fields; map-skills S3.1): no steps, no
    "say when done" (code says the done line once the check saw it). ``say_first`` comes first (it is said while the
    rest is still being written). The voice builder registers it (``voice/hands.py``)."""
    return {
        "name": "run_skill",
        "description": "Run one of the user's usual ways from the Skills list; it says its own done line.",
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
THINKER_HEAD = ("Run one of the user's usual ways (a skill): their own app, the route that works on this PC, checked "
                "by the PC before it counts as done. Give slots only from their words or a name listed; leave the rest "
                "out (Yuki fills them from their usuals). Done only when its check saw the result. Skills:")


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


__all__ = ["MENU_CHARS", "TALKER", "THINKER", "skill_line", "menu_lines", "menu_text", "talker_tool", "THINKER_HEAD",
           "thinker_tool"]
