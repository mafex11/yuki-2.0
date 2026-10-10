"""The Playbook's "Skills Yuki learned from you" and the user's say over them (phase L design §4.7, §4.9; PLAN.md
Phase L, Q12; branch lPage).

The learner (phase L, lCore) writes one ``skills`` row per learned skill (``origin = 'learned'``). This module reads
them for the Yuki window's Playbook page (:func:`learned_section`, through ``MemoryClient.workflows()``) and applies the
page's three buttons (:func:`skill_edit`, through ``MemoryClient.skill_edit``). Nothing here writes a skill, a route, a
preference or a routine: the learner does, from what the user does.

**What an entry shows** (design §4.9), all from data, said plainly:

* its name: the naming call's (lName), the user's own after a Rename, else a nameless line built from its capability
  and its fixed values (design §4.6: nameless skills run on request and are never offered);
* where it came from: "Seen 5 times on 3 weekdays, last Mon 08:30" (the learner's ``occasions`` rows of the user,
  else the row's content-free ``provenance``);
* how it runs: its capability's neutral line, what stays the same each time (its bound values), the program the user
  used for it (the occasions' means), what changes each time (its free fill slots), and the last step that stays the
  user's (the capability's ``wont`` words through the card's fixed copy, else its commit word, D24);
* when: the routine miner's active routine for it, if any;
* trust: the checked runs when the user asked (``skill_runs``: one per request, not per rung tried);
* whether it is offered, and if not why: the learner's ``provenance.offer_static`` (commit, rung, ``offer_class``,
  the naming call's ``everyday``), a name, the card's Won't words, D14 (``registry.offer_ready``), the routine, the
  20-skill cap. The routine lane decides each card itself; the page only says what it knows.

**The buttons** (design §4.7 table, Q12), each one small write on the row (no model, never a hand edit):

* **Rename**: the user's own words become the skill's name (``name_ciphertext``), marked ``named_by = "user"`` in
  its content-free ``provenance`` (the learner carries that forward and its naming pass skips the row) and kept as
  told (``told_ciphertext`` key :data:`TOLD_NAME`), so a later naming pass never replaces them (:func:`named_by_user`);
* **Forget this**: a tombstone, ``state = 'deleted'``: never in the menu, never offered, never proposed again;
* **Not mine**: a tombstone, ``state = 'not_mine'``: as Forget, and what it was learned from is not the user's.

**The tombstone contract** (for the learner, lCore, and its naming pass, lName). A tombstone is the learned row itself
with ``deleted_at`` set and ``state`` in :data:`TOMBSTONE_STATES`, keyed by its ``key`` (the learner's versioned group
key: capability + the digests of its bound key values) under its ``capability_id``, so it survives a means change
(design §4.7). The learner's write never clears ``deleted_at``, never changes a tombstone's ``state`` and never makes a
new row for the same group key. A ``not_mine`` group's occasions feed no preference, routine or taste. A row the user
renamed (``provenance.named_by == "user"``, or ``@name`` in its told values: :func:`named_by_user`) keeps its name:
the naming pass never writes ``name_ciphertext`` for it, and the learner carries ``named_by`` forward.

The catalog (:mod:`yuki.skills.catalog`) leaves tombstones out (``deleted_at IS NULL``), and every edit stamps
``updated_at``, so the next store-aware load (one count query) reads the change and the menu follows it.

Privacy (A10): names and bound values are decrypted in RAM for the user's own window only; nothing here logs; the
MCP never reads these tables.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Mapping, Sequence

#: The section's heading on the Playbook page (design §4.9).
SECTION = "Skills Yuki learned from you"
#: The page's ref of a learned skill: ``skill:<skills.id>``.
REF_PREFIX = "skill:"
#: The three buttons (design §4.9): Rename, Forget this, Not mine.
EDIT_ACTIONS: tuple[str, ...] = ("rename", "forget", "not_mine")
#: The tombstone state each button leaves (design §4.7 table).
TOMBSTONES: dict[str, str] = {"forget": "deleted", "not_mine": "not_mine"}
TOMBSTONE_STATES: tuple[str, ...] = tuple(TOMBSTONES.values())
#: The learned states the page shows: live (``candidate``, ``trusted``), over the 20 cap (``listed``: "in the Playbook
#: only", design §4.9) and resting (``dormant``: out of the menu, back when it is seen again). ``seen`` (below the
#: bar) is the learner's report only; ``retired`` is history.
SHOWN_STATES: tuple[str, ...] = ("candidate", "trusted", "listed", "dormant")
#: The learner's state for a row that passed the bar but ranks past the cap (lCore ``learn.write.LISTED``).
LISTED = "listed"
#: The provenance stamp of a Rename (lCore's ``learn.write`` reads ``named_by`` and never names such a row).
NAMED_BY = "named_by"
NAMED_BY_USER = "user"
#: The rungs an offered run may use (design §4.7: rung A, or a B verified at offer time).
OFFER_RUNGS: tuple[str, ...] = ("A-link", "A-capability", "B")
#: The user's own name for a skill: at most this long (the catalog shows at most 60).
NAME_CHARS = 60
#: Keys of the learned row's told values (``told_ciphertext``, encrypted JSON) that hold the user's Rename. Not slot
#: names (a slot name never starts with ``@``), so the resolver's told slots never read them.
TOLD_NAME = "@name"
TOLD_NAMED_AT = "@named_at"
#: A program is "usually" the one used when it took at least this share of the occasions.
USUAL_SHARE = 0.8
#: At most this many entries on the page (the learner keeps at most 20 live; the resting ones follow).
MAX_SHOWN = 60


# ---------------------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------------------


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def _clip(text: Any, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


def _sentence(text: Any) -> str:
    """``text`` with a capital first letter and a full stop (a data line said as a sentence)."""
    t = " ".join(str(text or "").split())
    if not t:
        return ""
    t = t[:1].upper() + t[1:]
    return t if t[-1] in ".!?…" else t + "."


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def parse_ref(ref: Any) -> int | None:
    """``skill:12`` (or ``12``) -> 12; ``None`` for anything else."""
    text = str(ref or "").strip()
    if text.lower().startswith(REF_PREFIX):
        text = text[len(REF_PREFIX):]
    return int(text) if text.isdigit() else None


def _json(store: Any, ciphertext: Any) -> dict[str, Any]:
    """An encrypted JSON column, decrypted in RAM (``{}`` when empty or unreadable)."""
    if not ciphertext:
        return {}
    try:
        got = json.loads(store._dec(ciphertext) or "{}")
    except Exception:  # noqa: BLE001
        return {}
    return got if isinstance(got, dict) else {}


def _text(store: Any, ciphertext: Any) -> str:
    if not ciphertext:
        return ""
    try:
        return " ".join(str(store._dec(ciphertext) or "").split())
    except Exception:  # noqa: BLE001
        return ""


def _value_words(value: Any) -> str:
    """A bound value as the page says it (an entity's name, else the value)."""
    if isinstance(value, Mapping):
        value = value.get("name") or value.get("key") or ""
    return _clip(value, 40)


def _app_name(app: Any) -> str:
    """An app key as people read it (``someapp`` -> ``Someapp``), without a database read."""
    from yuki.core.appnames import prettify

    key = str(app or "").strip()
    return prettify(key) or key


def _when_words(at: float, now: float, *, clock: bool) -> str:
    """``Mon 5 Oct 08:30`` (``today 08:30``, ``yesterday 08:30``); the time only when it is known (``clock``)."""
    d = datetime.fromtimestamp(float(at))
    today = datetime.fromtimestamp(now).date()
    gap = (today - d.date()).days
    day = "today" if gap == 0 else "yesterday" if gap == 1 else f"{d:%a} {d.day} {d:%b}"
    return f"{day} {d:%H:%M}" if clock else day


def _prov(raw: Any) -> dict[str, Any]:
    """The row's content-free ``provenance`` JSON as a dict (``{}`` when empty or unreadable)."""
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        got = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _renamed(prov: Mapping[str, Any], told: Mapping[str, Any]) -> bool:
    """A Rename's two marks: ``provenance.named_by == "user"`` (the one the learner reads) or ``@name`` told."""
    return prov.get(NAMED_BY) == NAMED_BY_USER or bool(str(told.get(TOLD_NAME) or "").strip())


def named_by_user(store: Any, skill_id: int) -> bool:
    """Whether the user renamed this learned skill (``provenance.named_by == "user"``, or its told values hold
    :data:`TOLD_NAME`). The naming pass asks before it writes a name, and never replaces the user's."""
    try:
        rows = store._query("SELECT provenance, told_ciphertext FROM skills WHERE id = ?", (int(skill_id),))
    except Exception:  # noqa: BLE001
        return False
    return bool(rows) and _renamed(_prov(rows[0][0]), _json(store, rows[0][1]))


# ---------------------------------------------------------------------------------------------------------------------
# Reading the rows
# ---------------------------------------------------------------------------------------------------------------------


def _rows(store: Any) -> list[dict[str, Any]]:
    """The learned rows the page shows (no tombstone, no retired row), with their capability's key. Raises when the
    learner's columns are not there yet (a database before migration 43)."""
    marks = ",".join("?" * len(SHOWN_STATES))
    rows = store._query(
        "SELECT s.id, s.key, s.state, s.uses, s.last_used_at, s.created_at, s.updated_at, s.name_ciphertext, "
        "s.spec_ciphertext, s.provenance, s.told_ciphertext, c.key FROM skills s LEFT JOIN skills c ON "
        "c.id = s.capability_id WHERE s.origin = 'learned' AND s.deleted_at IS NULL AND s.retired_at IS NULL AND "
        f"s.state IN ({marks}) ORDER BY s.updated_at DESC, s.id DESC LIMIT ?", (*SHOWN_STATES, MAX_SHOWN))
    names = ("id", "key", "state", "uses", "last_used_at", "created_at", "updated_at", "name_ct", "spec_ct",
             "provenance", "told_ct", "capability")
    return [dict(zip(names, tuple(r))) for r in rows]


#: A local day and "is it a weekend day" of an occasion's time, in SQL (the page's words only; no bar uses them).
_DAY = "date(at, 'unixepoch', 'localtime')"
_WEEKEND = "strftime('%w', at, 'unixepoch', 'localtime') IN ('0', '6')"


def _blank_seen() -> dict[str, Any]:
    return {"n": 0, "days": 0, "weekend_days": None, "first": None, "last": None, "last_known": None, "means": {},
            "asked": 0}


def _seen(store: Any, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Per skill id, from the learner's ``occasions`` (the user's own: no asked run): how many, on how many local
    days and how many of those were weekend days, the first and last time, the last one whose time is known (a late
    chat capture's is not: ``context_known``), the programs used (``means``) and the asked occasions. ``{}`` when the
    table can't be read (the row's ``provenance`` is used then)."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out: dict[int, dict[str, Any]] = defaultdict(_blank_seen)
    try:
        for sid, n, days, weekend_days, first, last, last_known in store._query(
                f"SELECT skill_id, count(*), count(DISTINCT {_DAY}), count(DISTINCT CASE WHEN {_WEEKEND} THEN {_DAY} "
                "END), min(at), max(at), max(CASE WHEN context_known THEN at END) FROM occasions "
                f"WHERE asked_run IS NULL AND skill_id IN ({marks}) GROUP BY skill_id", tuple(ids)):
            out[int(sid)].update(n=int(n or 0), days=int(days or 0), weekend_days=int(weekend_days or 0),
                                 first=first, last=last, last_known=last_known)
        for sid, means, n in store._query(
                "SELECT skill_id, means, count(*) FROM occasions WHERE asked_run IS NULL AND means IS NOT NULL AND "
                f"means != '' AND skill_id IN ({marks}) GROUP BY skill_id, means", tuple(ids)):
            out[int(sid)]["means"][str(means)] = int(n or 0)
        for sid, n in store._query(
                f"SELECT skill_id, count(*) FROM occasions WHERE asked_run IS NOT NULL AND skill_id IN ({marks}) "
                "GROUP BY skill_id", tuple(ids)):
            out[int(sid)]["asked"] = int(n or 0)
    except Exception:  # noqa: BLE001 - no occasions table yet: the provenance column says it
        return {}
    return dict(out)


def _from_provenance(got: Mapping[str, Any]) -> dict[str, Any]:
    """The row's content-free provenance (the learner's JSON: ``n``, ``days``, ``first_at`` / ``last_at``, older
    ``first`` / ``last``) as :func:`_seen` gives it; ``{}`` when it holds none."""
    if not got.get("n"):
        return {}

    def num(*keys: str) -> float | None:
        for key in keys:
            try:
                if got.get(key) is not None:
                    return float(got[key])
            except (TypeError, ValueError):
                continue
        return None

    last = num("last_at", "last")
    return {**_blank_seen(), "n": int(num("n") or 0), "days": int(num("days") or 0),
            "first": num("first_at", "first"), "last": last, "last_known": last}


def _runs(store: Any, keys: Sequence[str], now: float) -> dict[str, tuple[int, int]]:
    """Per skill key, ``(worked, didn't)`` over its ``skill_runs`` rows: one per request (a rung tried before the last
    one is in ``skill_route_tries`` and is not a request), decided the way ``registry.trust`` decides them (same
    window, outcomes and recheck rule). ``{}`` when the ledger can't be read."""
    if not keys:
        return {}
    from yuki.skills import registry

    marks = ",".join("?" * len(keys))
    try:
        rows = store._query(
            "SELECT skill, sum(CASE WHEN checked = 1 AND (recheck IS NULL OR recheck != 0) THEN 1 ELSE 0 END), "
            "sum(CASE WHEN checked = 0 OR (checked = 1 AND recheck = 0) THEN 1 ELSE 0 END) FROM skill_runs "
            f"WHERE skill IN ({marks}) AND rung IS NOT NULL AND checked IS NOT NULL AND t0 >= ? AND t0 < ? AND "
            "outcome NOT IN ('running', 'held_game', 'asked', 'refused', 'cancelled', 'other_device') GROUP BY skill",
            (*keys, now - registry.TRUST_DAYS * 86400.0, now + 1.0))
    except Exception:  # noqa: BLE001 - no ledger yet
        return {}
    return {str(k): (int(ok or 0), int(bad or 0)) for k, ok, bad in rows}


def _skipped_ids(errors: Sequence[str]) -> set[int]:
    """The learned rows the catalog skipped (``learned:<id>: <why>``: an invalid spec, key taken, not bindable)."""
    out: set[int] = set()
    for error in errors:
        head, _sep, rest = str(error).partition(":")
        rid = rest.split(":", 1)[0].strip()
        if head.strip() == "learned" and rid.isdigit():
            out.add(int(rid))
    return out


def _routines(store: Any, rows: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    """Per skill id, the routine miner's best active routine for it (its ``skill_id`` is the skill's key or, once the
    miner reads learned rows, its id: either is matched)."""
    by_ref: dict[str, int] = {}
    for r in rows:
        by_ref[str(r["key"])] = int(r["id"])
        by_ref[str(r["id"])] = int(r["id"])
    if not by_ref:
        return {}
    marks = ",".join("?" * len(by_ref))
    out: dict[int, dict[str, Any]] = {}
    try:
        got = store._query(
            "SELECT skill_id, dayset, at_min, hits, eligible, offer_mode FROM routines WHERE state = 'active' AND "
            f"skill_id IN ({marks}) ORDER BY hits DESC, id", tuple(by_ref))
    except Exception:  # noqa: BLE001 - no routines table: no time is known
        return {}
    for ref, dayset, at_min, hits, eligible, mode in got:
        sid = by_ref.get(str(ref))
        if sid is not None and sid not in out and at_min is not None:
            out[sid] = {"dayset": str(dayset or "all"), "at_min": float(at_min), "hits": int(hits or 0),
                        "eligible": int(eligible or 0), "offer_mode": str(mode or "early")}
    return out


# ---------------------------------------------------------------------------------------------------------------------
# The entry's lines
# ---------------------------------------------------------------------------------------------------------------------


def seen_line(seen: Mapping[str, Any], now: float) -> str:
    """``Seen 5 times on 3 weekdays, last Mon 5 Oct 08:30`` (``""`` with nothing seen)."""
    n = int(seen.get("n") or 0)
    if n <= 0:
        asked = int(seen.get("asked") or 0)
        return f"Learned from {_plural(asked, 'time')} you asked" if asked else ""
    days = int(seen.get("days") or 0)
    weekend_days = seen.get("weekend_days")
    what = "day"
    if weekend_days is not None and days:
        what = "weekday" if int(weekend_days) == 0 else "weekend day" if int(weekend_days) == days else "day"
    head = "Seen once" if n == 1 else f"Seen {n} times"
    if days > 1 or (days == 1 and n > 1):
        head += f" on {_plural(days, what)}"
    last = seen.get("last")
    if last:
        known = seen.get("last_known")
        head += f", last {_when_words(float(last), now, clock=bool(known) and float(known) == float(last))}"
    return head


def _usual_means(means: Mapping[str, int]) -> str:
    """``usually in <app>`` / ``in <app> or <other app>`` from the programs the occasions used."""
    if not means:
        return ""
    ranked = sorted(means.items(), key=lambda kv: (-kv[1], kv[0]))
    total = sum(n for _a, n in ranked) or 1
    top = _app_name(ranked[0][0])
    if ranked[0][1] / total >= USUAL_SHARE or len(ranked) == 1:
        return f"usually in {top}"
    return f"in {top} or {_app_name(ranked[1][0])}"


def _wont_line(cap: Any) -> str:
    """The last step that stays the user's: the capability's ``wont`` words through the routine card's fixed copy
    (``yuki.memory.offers.SKILL_WONT_LINES``), else its commit word (D24)."""
    words = [str(w) for w in getattr(cap, "wont", ()) or ()]
    if words:
        try:
            from yuki.memory.offers import SKILL_WONT_LINES

            lines = [SKILL_WONT_LINES.get(w) for w in words]
            if all(lines):
                return " ".join(dict.fromkeys(lines))   # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            pass
    commit = str(getattr(cap, "commit", "") or "")
    if commit == "user":
        return "The last step is yours: Yuki stops before it."
    if commit == "words":
        return "It sends or joins only on your own words."
    return ""


def _varies(cap: Any, bound: Mapping[str, Any]) -> str:
    """What changes each time: the capability's fill slots the skill does not fix (told and yes/no slots left out),
    by their kind or name."""
    out = []
    for slot in getattr(cap, "slots", ()) or ():
        if slot.role != "fill" or slot.name in bound or slot.told or slot.type == "bool":
            continue
        if not (slot.required or slot.ask or slot.kind or slot.type in ("entity", "person", "text")):
            continue
        out.append((slot.kind or slot.name).replace("_", " "))
    return ", ".join(dict.fromkeys(out))


def _nameless(cap: Any, bound: Mapping[str, Any]) -> str:
    """A nameless skill's line (design §4.6): its fixed values, then its capability's line."""
    fixed = " · ".join(v for v in (_value_words(x) for x in bound.values()) if v)
    words = str(getattr(cap, "menu", "") or "skill")
    return _clip(f"{fixed}: {words}" if fixed else _sentence(words).rstrip("."), NAME_CHARS)


def _static_offer(cap: Any, prov: Mapping[str, Any], spec: Mapping[str, Any]) -> str:
    """The learner's part of "offerable" (design §4.7): ``""`` when it allows an offer, else why not, in the page's
    words. Reads ``provenance.offer_static`` (lCore: the executable end is not a commit, the rung is A or B, the
    ``offer_class`` allows it, ``judged`` only with the naming call's ``everyday``); without it, the same parts from
    the provenance keys that are there (``everyday`` from the provenance, else the spec)."""
    offer_class = str(getattr(cap, "offer_class", "") or "never")
    if offer_class == "never":
        return "Never offered: it runs when you ask."
    everyday = prov.get("everyday") if isinstance(prov.get("everyday"), bool) else spec.get("everyday")
    may_be_work = offer_class == "judged" and everyday is not True
    commit = prov.get("commit")
    is_commit = commit not in (None, "", "none")
    rung = prov.get("rung")
    unsure = rung not in (None, "") and str(rung) not in OFFER_RUNGS
    static = prov.get("offer_static")
    if static is True or (static is None and not (may_be_work or is_commit or unsure)):
        return ""
    if may_be_work:
        return "Not offered (it may be work): it runs when you ask."
    if is_commit:
        return "Not offered: its last step is yours, so it runs when you ask."
    if unsure:
        return "Not offered: Yuki has no sure way to do it alone yet, so it runs when you ask."
    return "Not offered: it runs when you ask."


def _offer_line(*, cap: Any, skill: Any, spec: Mapping[str, Any], prov: Mapping[str, Any], state: str,
                routine: Mapping[str, Any] | None, nameless: bool, skipped: bool, ok: int, store: Any,
                now: float) -> tuple[str, bool]:
    """Whether it may be offered on a card, and if not why (design §4.6, §4.7): never a nameless skill; the learner's
    static part (:func:`_static_offer`); the card's Won't words known (``offers.skill_wont_line``); live; trusted by
    D14 (``registry.offer_ready``, called only when everything before it holds); an active routine. ``skill``: the
    catalog's live entry, ``None`` when it is not live (resting, over the cap, or skipped by the catalog:
    ``skipped``). ``ok``: its requests that worked. ``(line, maybe)``; the routine lane decides each card itself
    (offers.py), this only says what the page knows."""
    from yuki.skills import registry

    if str(getattr(cap, "offer_class", "") or "never") == "never":
        return "Never offered: it runs when you ask.", False
    if nameless:
        return "Not offered: it has no name yet, so it runs when you ask.", False
    why = _static_offer(cap, prov, spec)
    if why:
        return why, False
    try:
        from yuki.memory.offers import skill_wont_line

        wont_known = skill_wont_line(skill or cap) is not None
    except Exception:  # noqa: BLE001 - fail closed, as the lane does
        wont_known = False
    if not wont_known:
        return "Not offered: a card can't yet say what Yuki won't do for it.", False
    if state == LISTED or (skill is None and state != "dormant" and not skipped):
        return f"Over the limit of {_max_skills()} skills, so not in use now.", False
    if state == "dormant":
        return "Not seen lately, so not offered.", False
    if skill is None:
        return "Not in use now, so not offered.", False
    kind = str(skill.kind or "change")
    if not registry.offer_ready(store, skill, kind=kind, now=now):
        if kind == "open":
            return (f"Not offered yet: first it has to work {registry.OPEN_RUNS} times when you ask ({ok} so far).",
                    False)
        if ok >= registry.CHANGE_RUNS:
            return (f"Not offered yet: one of its last {registry.CHANGE_WINDOW} tries didn't work.", False)
        return (f"Not offered yet: first it has to work {registry.CHANGE_RUNS} times when you ask ({ok} so far).",
                False)
    if not routine:
        return "Not offered yet: no steady time found.", False
    if routine.get("offer_mode") == "off":
        return "Offers off: you asked for fewer.", False
    from yuki.memory.routines import DAYSET_WORDS, twelve_hour

    return (f"May be offered around {twelve_hour(float(routine['at_min']))} on "
            f"{DAYSET_WORDS.get(str(routine.get('dayset')), 'days')}.", True)


def _when_line(routine: Mapping[str, Any] | None) -> str:
    if not routine:
        return ""
    from yuki.memory.routines import DAYSET_WORDS, twelve_hour

    line = (f"Usually around {twelve_hour(float(routine['at_min']))} on most "
            f"{DAYSET_WORDS.get(str(routine.get('dayset')), 'days')}")
    if routine.get("eligible"):
        line += f" ({routine.get('hits', 0)} of the last {routine['eligible']})"
    return line


def _max_skills() -> int:
    from yuki.skills.connection import MAX_SKILLS

    return MAX_SKILLS


# ---------------------------------------------------------------------------------------------------------------------
# The section
# ---------------------------------------------------------------------------------------------------------------------


def learned_section(store: Any, *, now: float | None = None) -> dict[str, Any]:
    """The Playbook's "Skills Yuki learned from you": ``{"available", "title", "items": [entry, ...], "live", "cap",
    "menu"}``. ``available`` is False before the learner's columns exist (migration 43). Each entry::

        {"ref": "skill:12", "name", "your_name": bool, "nameless": bool, "seen", "how", "same", "means",
         "varies", "when", "trust", "offer", "offered": bool, "state", "live": bool, "quick": bool, "usage": bool}

    ``live``: in the catalog now (at most 20); ``quick``: on the talker's menu (capability lines + the top 12 that fix
    a key value); ``usage``: it fixes no key value (a usage record: messages, voice joins, volume), so it adds no
    menu line of its own. Live ones first, in the catalog's order, then the ones over the cap, then the resting
    ones, newest first. Never raises."""
    now = _now(now)
    out: dict[str, Any] = {"available": False, "title": SECTION, "items": [], "live": 0, "cap": _max_skills(),
                           "menu": 0}
    try:
        rows = _rows(store)
    except Exception:  # noqa: BLE001 - before migration 43: nothing learned yet
        return out
    out["available"] = True
    try:
        from yuki.skills import catalog
        from yuki.skills.menu import menu_skills

        loaded = catalog.load(store=store)
        caps = {s.key: s for s in loaded.capabilities}
        live = {s.key: s for s in loaded.learned}
        order = {k: i for i, k in enumerate(live)}
        quick = {s.key for s in menu_skills(loaded) if s.learned}
        out["menu"], out["live"] = len(quick), len(live)
        skipped = _skipped_ids(loaded.errors)
        seen = _seen(store, [int(r["id"]) for r in rows])
        routines = _routines(store, rows)
        runs = _runs(store, [str(r["key"]) for r in rows], now)
        ranked: list[tuple[tuple[int, float], dict[str, Any]]] = []
        for r in rows:
            sid, key = int(r["id"]), str(r["key"])
            cap = caps.get(str(r["capability"] or ""))
            if cap is None:
                continue   # a capability no shipped file has any more: nothing it could run
            spec = _json(store, r["spec_ct"])
            skill = live.get(key)
            if skill is not None:
                bound = dict(skill.bound)
            else:   # resting or over the cap: its spec's bound values, the ones that name a slot of its capability
                raw = spec.get("bound") if isinstance(spec.get("bound"), Mapping) else {}
                bound = {k: v for k, v in raw.items() if cap.slot(str(k)) is not None and v not in (None, "")}
            name = _text(store, r["name_ct"])
            prov = _prov(r["provenance"])
            got = seen.get(sid) or _from_provenance(prov)
            ok, failed = runs.get(key, (0, 0))
            routine = routines.get(sid)
            state = str(r["state"] or "")
            offer, offered = _offer_line(cap=cap, skill=skill, spec=spec, prov=prov, state=state, routine=routine,
                                         nameless=not name, skipped=sid in skipped, ok=ok, store=store, now=now)
            entry = {
                "ref": f"{REF_PREFIX}{sid}", "name": _clip(name, NAME_CHARS) or _nameless(cap, bound),
                "your_name": bool(name) and _renamed(prov, _json(store, r["told_ct"])),
                "nameless": not name, "seen": seen_line(got, now),
                "how": " ".join(x for x in (_sentence(cap.menu), _wont_line(skill or cap)) if x),
                "same": " · ".join(v for v in (_value_words(bound[s.name]) for s in cap.slots if s.name in bound) if v),
                "means": _usual_means(got.get("means") or {}), "varies": _varies(cap, bound),
                "when": _when_line(routine),
                "trust": (f"Worked {ok} of {ok + failed} times when you asked" if ok + failed
                          else "Yuki hasn't run it for you yet"),
                "offer": offer, "offered": offered, "state": state, "live": skill is not None, "quick": key in quick,
                # a usage record fixes no key value: its capability's line stands for it in the menu (design §4.2)
                "usage": not any(s.role == "key" and s.name in bound for s in cap.slots),
            }
            rank = ((0, float(order[key])) if key in order else
                    (2 if state == "dormant" else 1, -float(got.get("last") or r["updated_at"] or 0)))
            ranked.append((rank, entry))
        out["items"] = [entry for _rank, entry in sorted(ranked, key=lambda pair: pair[0])]
    except Exception as exc:  # noqa: BLE001 - the page shows the rest; the section says it couldn't be read
        out["error"] = type(exc).__name__
    return out


# ---------------------------------------------------------------------------------------------------------------------
# The buttons
# ---------------------------------------------------------------------------------------------------------------------


def _clean_name(words: Any) -> str:
    """The user's words as a name: one line, memory's secret filter applied, at most :data:`NAME_CHARS`."""
    from yuki.memory import secret_filter

    text = " ".join(str(words or "").split())
    try:
        text = secret_filter.redact_text(text)
    except Exception:  # noqa: BLE001 - the filter must never let a secret through: then no name
        return ""
    return _clip(text, NAME_CHARS)


def skill_edit(store: Any, ref: Any, action: str, words: str = "", *, now: float | None = None) -> dict[str, Any]:
    """The user's say over one learned skill on the Playbook page (design §4.9), one write on its row, never a hand
    edit and never a model call:

    * ``rename`` (``words`` needed): their words become its name, stamped ``named_by = "user"`` in its provenance
      and kept as told, so no naming pass replaces them;
    * ``forget``: a tombstone (``deleted``): never in the menu, never offered, never proposed again (Q12);
    * ``not_mine``: a tombstone (``not_mine``): as forget, and what it was learned from is not the user's.

    Returns ``{"ok": True, "id", "action", "state", "name"?}`` or ``{"ok": False, "error"}`` (plain words for the
    page). Never raises."""
    now = _now(now)
    action = str(action or "").strip().lower().replace(" ", "_").replace("-", "_")
    if action not in EDIT_ACTIONS:
        return {"ok": False, "error": f"action must be one of {', '.join(EDIT_ACTIONS)}"}
    sid = parse_ref(ref)
    if sid is None:
        return {"ok": False, "error": "That isn't one of the skills I learned."}
    name = ""
    if action == "rename":
        name = _clean_name(words)
        if not name:
            return {"ok": False, "error": "Say the name you want first."}
    result: dict[str, Any] = {}

    def write(conn: Any) -> None:
        row = conn.execute("SELECT origin, state, deleted_at, told_ciphertext, provenance FROM skills WHERE id = ?",
                           (sid,)).fetchone()
        if row is None or str(row[0] or "") != "learned":
            result["error"] = "That isn't one of the skills I learned."
            return
        if row[2] is not None:
            result["error"] = "That one is already gone."
            return
        if action == "rename":
            told = _json(store, row[3])
            told[TOLD_NAME], told[TOLD_NAMED_AT] = name, now
            # the mark the learner reads (content-free): it carries named_by forward and its naming pass skips the row
            prov = _prov(row[4])
            prov[NAMED_BY], prov["named_at"] = NAMED_BY_USER, round(now, 1)
            conn.execute("UPDATE skills SET name_ciphertext = ?, told_ciphertext = ?, provenance = ?, updated_at = ? "
                         "WHERE id = ?",
                         (store._enc(name), store._enc(json.dumps(told, ensure_ascii=False, sort_keys=True)),
                          json.dumps(prov, sort_keys=True, separators=(",", ":"), default=str), now, sid))
            result["state"] = str(row[1] or "")
            return
        state = TOMBSTONES[action]
        conn.execute("UPDATE skills SET state = ?, deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                     (state, now, now, sid))
        result["state"] = state

    try:
        store._write(write)
    except Exception as exc:  # noqa: BLE001 - before migration 43, or the database is busy
        return {"ok": False, "error": f"Memory didn't take that ({type(exc).__name__})."}
    if "error" in result:
        return {"ok": False, "error": result["error"]}
    out = {"ok": True, "id": f"{REF_PREFIX}{sid}", "action": action, "state": result.get("state")}
    if name:
        out["name"] = name
    return out


__all__ = ["SECTION", "REF_PREFIX", "EDIT_ACTIONS", "TOMBSTONES", "TOMBSTONE_STATES", "SHOWN_STATES", "NAME_CHARS",
           "TOLD_NAME", "TOLD_NAMED_AT", "LISTED", "NAMED_BY", "NAMED_BY_USER", "OFFER_RUNGS", "parse_ref",
           "named_by_user", "seen_line", "learned_section", "skill_edit"]
