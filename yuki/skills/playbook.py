"""The Playbook page's data and the user's say over it (audit 17 Oct, learning.md §9.4; contracts C4, C5).

Three sections, all from data, said plainly (:func:`page`, through ``MemoryClient.playbook()``):

1. **What Yuki can do** - the capabilities (the shipped files), one line each, with how often the user used each
   (its record's counts: contract C5);
2. **Your routines** - the routine miner's routines (active first) with their time, days, how sure, and the card they
   lead to;
3. **Your own ways** - lane kWays' learned ways (contract C4: ``origin = 'learned_way'`` rows, as the route reads
   them) with how they were learned and how often Yuki ran them, and their buttons (Rename; Delete: ``forget``, a
   tombstone the way learner never brings back); a plain empty state until they exist.

The page's standing rules and requests come from memory's own rules and standing requests
(``yuki.ui.web_data.build_playbook``), not from here.

21 Oct, by the user's ask: the page no longer lists the user's own things (the sites, meeting tabs and playlists of
each capability's record, the channels and playlists taste knows, a way's projects). That is Yuki's own understanding,
not a catalog for the user to look through: it stays in memory, untouched, for the you-card, the meanings, the menu
and the routines (``yuki.skills.learn.values``, ``yuki.skills.taste``, ``yuki.memory.youcard``), and marks the user
already set on a thing keep being honoured there.

The old practice sections (procedures, proposals, workflows, lessons) are not on the page any more: the old learners'
entries are retired by code (lane kClean). Nothing here writes a routine or a way except the buttons: one small write
each, no model call, never a hand edit (:func:`skill_edit`). Names are decrypted in RAM for the user's own window only;
nothing here logs; the MCP never reads these tables.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

#: The page's ref of a learned row (a way, a routine's anchor): ``skill:<skills.id>``.
REF_PREFIX = "skill:"
#: The buttons on a learned row (Rename, Forget this, Not mine).
EDIT_ACTIONS: tuple[str, ...] = ("rename", "forget", "not_mine")
#: The tombstone state each row button leaves.
TOMBSTONES: dict[str, str] = {"forget": "deleted", "not_mine": "not_mine"}
TOMBSTONE_STATES: tuple[str, ...] = tuple(TOMBSTONES.values())
#: The provenance stamp of a Rename (the learner reads ``named_by`` and never names such a row).
NAMED_BY = "named_by"
NAMED_BY_USER = "user"
NAME_CHARS = 60
#: The way learner's rows (contract C4).
WAY_ORIGIN = "learned_way"


def _clip(text: Any, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _text(store: Any, ciphertext: Any) -> str:
    if not ciphertext:
        return ""
    try:
        return " ".join(str(store._dec(ciphertext) or "").split())
    except Exception:  # noqa: BLE001
        return ""


def _json(store: Any, ciphertext: Any) -> dict[str, Any]:
    if not ciphertext:
        return {}
    try:
        got = json.loads(store._dec(ciphertext) or "{}")
    except Exception:  # noqa: BLE001
        return {}
    return got if isinstance(got, dict) else {}


def _prov(raw: Any) -> dict[str, Any]:
    try:
        got = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _day_words(at: Any, now: float) -> str:
    try:
        d = datetime.fromtimestamp(float(at))
    except (TypeError, ValueError):
        return ""
    gap = (datetime.fromtimestamp(now).date() - d.date()).days
    return "today" if gap == 0 else "yesterday" if gap == 1 else f"{d:%a} {d.day} {d:%b}"


def _hm(minute: Any) -> str:
    m = int(float(minute) % 1440.0)
    return f"{m // 60:02d}:{m % 60:02d}"


# ---------------------------------------------------------------------------------------------------------------------
# The sections
# ---------------------------------------------------------------------------------------------------------------------


def _records(store: Any) -> dict[str, dict[str, Any]]:
    """Per capability key, its record's content-free provenance (``n``, ``days``, ``last_at``)."""
    try:
        rows = store._query("SELECT c.key, s.provenance FROM skills s JOIN skills c ON c.id = s.capability_id WHERE"
                            " s.origin = 'learned' AND s.deleted_at IS NULL AND s.retired_at IS NULL AND s.key LIKE"
                            " '%#all'")
    except Exception:  # noqa: BLE001
        return {}
    return {str(k): _prov(p) for k, p in rows}


def can_do(store: Any, now: float) -> list[dict[str, Any]]:
    """Section 1: every capability, one line each, with how often the user used it (its record)."""
    from yuki.skills.catalog import capabilities

    recs = _records(store)
    out = []
    for cap in capabilities().skills:
        prov = recs.get(cap.key) or {}
        n, days = int(prov.get("n") or 0), int(prov.get("days") or 0)
        used = f"You did this {_plural(n, 'time')} on {_plural(days, 'day')}" if n else "Ready whenever you ask"
        if n and prov.get("last_at"):
            used += f", last {_day_words(prov['last_at'], now)}"
        words = str(cap.menu or "")
        out.append({"key": cap.key, "line": words[:1].upper() + words[1:], "used": used})
    return out


def routines(store: Any, now: float) -> list[dict[str, Any]]:
    """Section 2: the routines (active first, then candidates and dormant ones) with the card they lead to."""
    try:
        rows = store.routines(states=["active", "candidate", "dormant"])
    except Exception:  # noqa: BLE001
        return []
    from yuki.skills.catalog import capabilities

    caps = {c.key: c for c in capabilities().skills}
    order = {"active": 0, "candidate": 1, "dormant": 2}
    out = []
    for r in sorted(rows, key=lambda r: (order.get(str(r.get("state")), 3), float(r.get("at_min") or 0.0))):
        key = str(r.get("skill_id") or "")
        try:
            label = store.routine_label(r) or ""
        except Exception:  # noqa: BLE001
            label = ""
        bound: dict[str, Any] = {}
        name = ""
        try:
            got = store._query("SELECT name_ciphertext, spec_ciphertext FROM skills WHERE key = ? LIMIT 1", (key,))
            if got:
                name = _text(store, got[0][0])
                bound = dict(_json(store, got[0][1]).get("bound") or {})
            from yuki.skills.learn.values import routine_value

            one = routine_value(store, r.get("key"))
            if one is not None:
                # a routine on one value of a capability's record (a site at lunch): named by that value
                name, bound = "", {str(one[1].get("slot")): one[1].get("name") or one[1].get("label")}
        except Exception:  # noqa: BLE001
            pass
        cap = caps.get(key.split("#", 1)[0])
        verb = str(getattr(cap, "menu", "") or "").split(";")[0].split(" (")[0] if cap else ""
        what = ", ".join(str(v.get("name") if isinstance(v, dict) else v) for v in bound.values() if v)
        at = r.get("at_min")
        days = str(r.get("dayset") or "days")
        when = f"around {_hm(at)} on {days}" if at is not None else days
        hits, eligible = int(r.get("hits") or 0), int(r.get("eligible") or 0)
        mode = str(r.get("offer_mode") or "")
        card = (f"A card {when}: {verb}{' - ' + what if what else ''}?" if str(r.get("state")) == "active" and verb
                else "No card yet: Yuki is still checking it holds")
        out.append({"title": _clip(label or name or what or verb or "A usual of yours", 60), "when": when,
                    "sure": f"{hits} of the last {eligible} {days}" if eligible else "",
                    "state": str(r.get("state") or ""), "card": card, "mode": mode})
    return out


def ways(store: Any, now: float) -> list[dict[str, Any]]:
    """Section 3: the user's own ways (contract C4: ``origin = 'learned_way'`` rows, not retired, not deleted), most
    used first: the same rows the menu lists and the route runs (``yuki.skills.learn.ways.live_ways``). Each with its
    name, app, how it was learned (its provenance: demonstrations, days, first and last; or the user told it) and how
    often Yuki ran it; never offered. The user's things in a way (``spec["item"]["things"]``: their projects) are not
    listed (21 Oct): the route and the you-card read them, the page does not."""
    try:
        from yuki.skills.learn.ways import app_title, live_ways

        rows = live_ways(store)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for w in rows:
        name = " ".join(str(w.get("name") or "").split())
        p = w.get("prov") or {}
        if p.get("told"):
            seen = "You showed Yuki once"
        elif p.get("demos"):
            seen = (f"Learned from {_plural(int(p['demos']), 'time')} you did it, on {_plural(int(p.get('days') or 0), 'day')}"
                    + (f", last {_day_words(p['last'], now)}" if p.get("last") else ""))
        else:
            seen = ""
        uses, last = int(w.get("uses") or 0), w.get("last_used_at")
        used = (f"Yuki ran it {_plural(uses, 'time')}" + (f", last {_day_words(last, now)}" if last else "")
                if uses else "Runs when you ask; never offered")
        out.append({"ref": f"{REF_PREFIX}{int(w['id'])}", "name": _clip(name or "Your way", 60),
                    "app": app_title(str(w.get("app") or "")), "seen": seen, "used": used,
                    "your_name": p.get(NAMED_BY) == NAMED_BY_USER})
    return out


def page(store: Any, *, now: float | None = None) -> dict[str, Any]:
    """The Playbook's three sections (see the module docstring). Each section reads on its own; one that can't be read
    is empty, never an error. Never raises."""
    now = time.time() if now is None else float(now)
    out: dict[str, Any] = {"available": True}
    for name, fn in (("can_do", can_do), ("routines", routines), ("ways", ways)):
        try:
            out[name] = fn(store, now)
        except Exception as exc:  # noqa: BLE001
            out[name] = []
            out.setdefault("errors", []).append(f"{name}:{type(exc).__name__}")
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


def parse_ref(ref: Any) -> int | None:
    """``skill:12`` (or ``12``) -> 12; ``None`` for anything else."""
    text = str(ref or "").strip()
    if text.lower().startswith(REF_PREFIX):
        text = text[len(REF_PREFIX):]
    return int(text) if text.isdigit() else None


def skill_edit(store: Any, ref: Any, action: str, words: str = "", *, now: float | None = None) -> dict[str, Any]:
    """The user's say on the Playbook page, one small write, never a model call: on a learned row (``skill:<id>``: a
    way of the user's, a routine's anchor) ``rename`` (their words, stamped ``named_by = "user"`` so no naming pass
    replaces them), ``forget`` / ``not_mine`` (a tombstone: never in the menu, never offered, never proposed again).

    Anything else is refused in plain words, the old ``value:<capability>:<id>`` refs too: the page no longer lists
    the user's things (21 Oct), so it has no buttons on them (:func:`yuki.skills.learn.values.mark` stays memory's own).

    Returns ``{"ok": True, "action", "state"?, "name"?}`` or ``{"ok": False, "error"}`` (plain words). Never raises."""
    now = time.time() if now is None else float(now)
    action = str(action or "").strip().lower().replace(" ", "_").replace("-", "_")
    sid = parse_ref(ref)
    if sid is None:
        return {"ok": False, "error": "That isn't one of the things I learned."}
    if action not in EDIT_ACTIONS:
        return {"ok": False, "error": f"action must be one of {', '.join(EDIT_ACTIONS)}"}
    name = ""
    if action == "rename":
        name = _clean_name(words)
        if not name:
            return {"ok": False, "error": "Say the name you want first."}
    result: dict[str, Any] = {}

    def write(conn: Any) -> None:
        row = conn.execute("SELECT origin, state, deleted_at, provenance FROM skills WHERE id = ?", (sid,)).fetchone()
        if row is None or str(row[0] or "") not in ("learned", WAY_ORIGIN):
            result["error"] = "That isn't one of the things I learned."
            return
        if row[2] is not None:
            result["error"] = "That one is already gone."
            return
        if action == "rename":
            prov = _prov(row[3])
            prov[NAMED_BY], prov["named_at"] = NAMED_BY_USER, round(now, 1)
            conn.execute("UPDATE skills SET name_ciphertext = ?, provenance = ?, updated_at = ? WHERE id = ?",
                         (store._enc(name), json.dumps(prov, sort_keys=True, separators=(",", ":"), default=str),
                          now, sid))
            result["state"] = str(row[1] or "")
            return
        state = TOMBSTONES[action]
        conn.execute("UPDATE skills SET state = ?, deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                     (state, now, now, sid))
        result["state"] = state

    try:
        store._write(write)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Memory didn't take that ({type(exc).__name__})."}
    if "error" in result:
        return {"ok": False, "error": result["error"]}
    out = {"ok": True, "id": f"{REF_PREFIX}{sid}", "action": action, "state": result.get("state")}
    if name:
        out["name"] = name
    return out


__all__ = ["REF_PREFIX", "EDIT_ACTIONS", "TOMBSTONES", "TOMBSTONE_STATES", "NAMED_BY", "NAMED_BY_USER", "NAME_CHARS",
           "can_do", "routines", "ways", "page", "parse_ref", "skill_edit"]
