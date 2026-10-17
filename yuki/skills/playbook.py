"""The Playbook page's data and the user's say over it (audit 17 Oct, learning.md §9.4; contracts C4, C5).

Four sections, all from data, said plainly (:func:`page`, through ``MemoryClient.playbook()``):

1. **What Yuki can do** - the capabilities (the shipped files), one line each, with how often the user used each
   (its record's counts: contract C5);
2. **What Yuki knows you use** - the user's things by kind: each known value of a capability's record (a site, a
   meeting service, a playlist) with how often, on how many days, when (the hours it falls in), which program it
   usually opens in and the work mark; plus what taste knows read-only (the YouTube channels and Spotify playlists
   the user picks). Buttons on a value: **Rename** (their words become its name, kept by every later pass), **Not
   mine** (left off the menu, cards and the you-card) and **This is work** (resolved on request, never offered; a
   work value shows **Not work** instead);
3. **Your routines** - the routine miner's routines (active first) with their time, days, how sure, and the card they
   lead to;
4. **Your own ways** - lane kWays' learned ways (contract C4: ``origin = 'learned_way'`` rows, as the route reads
   them) with how they were learned, how often Yuki ran them and the user's things in them (their projects), and their
   buttons (Rename; Delete: ``forget``, a tombstone the way learner never brings back); a plain empty state until
   they exist.

The old practice sections (procedures, proposals, workflows, lessons) are not on the page any more: the old learners'
entries are retired by code (lane kClean). Nothing here writes a value, a routine or a way except the buttons: one
small write each, no model call, never a hand edit (:func:`skill_edit`). Names and values are decrypted in RAM for
the user's own window only; nothing here logs; the MCP never reads these tables.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

#: The page's ref of a learned row (a way, a routine's anchor): ``skill:<skills.id>``; of a known value:
#: ``value:<capability>:<value id>``.
REF_PREFIX = "skill:"
VALUE_PREFIX = "value:"
#: The buttons on a learned row (Rename, Forget this, Not mine) and on a value (Rename, Not mine, This is work, Not
#: work, Restore).
EDIT_ACTIONS: tuple[str, ...] = ("rename", "forget", "not_mine")
VALUE_ACTIONS: tuple[str, ...] = ("rename", "not_mine", "work", "everyday", "restore")
#: The tombstone state each row button leaves.
TOMBSTONES: dict[str, str] = {"forget": "deleted", "not_mine": "not_mine"}
TOMBSTONE_STATES: tuple[str, ...] = tuple(TOMBSTONES.values())
#: The provenance stamp of a Rename (the learner reads ``named_by`` and never names such a row).
NAMED_BY = "named_by"
NAMED_BY_USER = "user"
NAME_CHARS = 60
#: Known values shown per kind at most; taste names per kind at most.
VALUES_SHOWN = 30
TASTE_SHOWN = 12
#: The parts of a day the page names a value's hours by (start hour, words); data for words only.
DAY_PARTS: tuple[tuple[int, str], ...] = ((5, "mornings"), (12, "afternoons"), (17, "evenings"), (21, "late evenings"),
                                          (0, "nights"))
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


def when_words(hours: Any) -> str:
    """A value's hours (24 local counts) as the parts of the day most of its use falls in ("evenings and nights")."""
    hours = list(hours or [])
    if len(hours) != 24 or not sum(hours):
        return ""
    parts: dict[str, int] = {}
    bounds = sorted(DAY_PARTS)
    for h, n in enumerate(hours):
        word = next(w for start, w in reversed(bounds) if h >= start)
        parts[word] = parts.get(word, 0) + int(n)
    total = sum(parts.values())
    top = [w for w, n in sorted(parts.items(), key=lambda kv: -kv[1]) if n / total >= 0.25][:2]
    return " and ".join(top) if top else "at any hour"


def _app_names(store: Any) -> Any:
    names: dict[str, str] = {}

    def name(key: str) -> str:
        if key and key not in names:
            try:
                names[key] = str(store.app_display_name(key) or key)
            except Exception:  # noqa: BLE001
                names[key] = key
        return names.get(key, "")

    return name


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


def _kind_title(cap: Any, slot: str) -> str:
    """A kind's heading from the capability's own data: its slot's name and its menu's first words ("site: open a
    site's front page...")."""
    words = str(getattr(cap, "menu", "") or "").split(";")[0].split(" (")[0]
    return f"{slot.replace('_', ' ').capitalize()}s · {words}" if slot else words


def things(store: Any, now: float) -> list[dict[str, Any]]:
    """Section 2: the known values by kind (each capability's record, contract C5), then the taste kinds read-only."""
    from yuki.skills.catalog import capabilities
    from yuki.skills.learn import values as V

    caps = {c.key: c for c in capabilities().skills}
    app = _app_names(store)
    groups: list[dict[str, Any]] = []
    for cap_key, vals in sorted(V.all_values(store).items()):
        cap = caps.get(cap_key)
        if cap is None or not vals:
            continue
        taste_slot = {s.name for s in cap.slots if s.taste}
        items = []
        for v in V.ranked(vals, now)[:VALUES_SHOWN] + [v for v in vals if v.get("mark") in V.MARKS]:
            if v.get("slot") in taste_slot and v.get("mark") not in V.MARKS:
                continue        # the taste list below names these
            usual = next(iter((v.get("usual") or {}).values()), "")
            seen = f"{_plural(int(v.get('n') or 0), 'time')} on {_plural(int(v.get('days') or 0), 'day')}"
            if v.get("said"):
                seen += f", asked {_plural(int(v['said']), 'time')}"
            items.append({
                "ref": f"{VALUE_PREFIX}{cap_key}:{v.get('id')}", "name": _clip(v.get("name"), 60),
                "seen": seen, "when": when_words(v.get("hours")), "last": _day_words(v.get("last_at"), now),
                "app": app(str(usual)) if usual else "", "work": v.get("everyday") is False,
                "not_mine": v.get("mark") in V.MARKS, "your_name": v.get("named_by") == NAMED_BY_USER,
            })
        if items:
            groups.append({"key": cap_key, "title": _kind_title(cap, str(vals[0].get("slot") or "")), "items": items,
                           "editable": True})
    try:
        from yuki.skills import taste

        kinds: list[tuple[str, str]] = []
        for cap in caps.values():
            kinds += [(s.taste, f"{s.name.capitalize()}s you play or pick") for s in cap.slots if s.taste]
        kinds += [(kind, f"{site.capitalize()} {page} pages you watch") for kind, site, page, _v in taste.site_kinds()]
        for kind, title in dict.fromkeys(kinds):
            rows = taste.top(store, kind, now=now, n=10_000)
            rows.sort(key=lambda t: -t.n_obs)
            names = [t for t in rows if t.name][:TASTE_SHOWN]
            if names:
                groups.append({"key": kind, "title": title, "editable": False,
                               "items": [{"ref": "", "name": _clip(t.name, 60),
                                          "seen": f"picked about {_plural(max(1, round(t.n_obs)), 'time')}"}
                                         for t in names]})
    except Exception:  # noqa: BLE001 - the learner's values still show
        pass
    return groups


def routines(store: Any, now: float) -> list[dict[str, Any]]:
    """Section 3: the routines (active first, then candidates and dormant ones) with the card they lead to."""
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
    """Section 4: the user's own ways (contract C4: ``origin = 'learned_way'`` rows, not retired, not deleted), most
    used first: the same rows the menu lists and the route runs (``yuki.skills.learn.ways.live_ways``). Each with its
    name, app, how it was learned (its provenance: demonstrations, days, first and last; or the user told it), how
    often Yuki ran it, and the user's things in it (``spec["item"]["things"]``: their projects); never offered."""
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
        spec = w.get("spec") or {}
        noun = str(spec.get("item_noun") or p.get("item_noun") or "thing")
        things = [t for t in ((spec.get("item") or {}).get("things") or []) if isinstance(t, dict) and t.get("name")]
        out.append({"ref": f"{REF_PREFIX}{int(w['id'])}", "name": _clip(name or "Your way", 60),
                    "app": app_title(str(w.get("app") or "")), "seen": seen, "used": used,
                    "things_title": f"Your {noun} groups" if things else "",
                    "things": [_clip(t["name"], 40) + (f" · {_plural(int(t['days']), 'day')}" if t.get("days") else "")
                               for t in things[:12]],
                    "your_name": p.get(NAMED_BY) == NAMED_BY_USER})
    return out


def page(store: Any, *, now: float | None = None) -> dict[str, Any]:
    """The Playbook's four sections (see the module docstring). Each section reads on its own; one that can't be read
    is empty, never an error. Never raises."""
    now = time.time() if now is None else float(now)
    out: dict[str, Any] = {"available": True}
    for name, fn in (("can_do", can_do), ("things", things), ("routines", routines), ("ways", ways)):
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
    """The user's say on the Playbook page, one small write, never a model call:

    * a known value (``value:<capability>:<id>``): ``rename``, ``not_mine``, ``work`` (``everyday`` False: never
      offered), ``everyday`` (Not work) or ``restore`` (:func:`yuki.skills.learn.values.mark`);
    * a learned row (``skill:<id>``: a way of the user's, a routine's anchor): ``rename`` (their words, stamped
      ``named_by = "user"`` so no naming pass replaces them), ``forget`` / ``not_mine`` (a tombstone: never in the
      menu, never offered, never proposed again).

    Returns ``{"ok": True, "action", "state"?, "name"?}`` or ``{"ok": False, "error"}`` (plain words). Never raises."""
    now = time.time() if now is None else float(now)
    action = str(action or "").strip().lower().replace(" ", "_").replace("-", "_")
    text = str(ref or "").strip()
    if text.startswith(VALUE_PREFIX):
        from yuki.skills.learn import values as V

        cap, _sep, vid = text[len(VALUE_PREFIX):].rpartition(":")
        if not cap or not vid or action not in VALUE_ACTIONS:
            return {"ok": False, "error": "That isn't something the Playbook can do to a thing of yours."}
        name = _clean_name(words) if action == "rename" else ""
        if action == "rename" and not name:
            return {"ok": False, "error": "Say the name you want first."}
        got = V.mark(store, cap, vid, action, name)
        if got.get("ok") and name:
            got["name"] = name
        return got
    if action not in EDIT_ACTIONS:
        return {"ok": False, "error": f"action must be one of {', '.join(EDIT_ACTIONS)}"}
    sid = parse_ref(text)
    if sid is None:
        return {"ok": False, "error": "That isn't one of the things I learned."}
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


__all__ = ["REF_PREFIX", "VALUE_PREFIX", "EDIT_ACTIONS", "VALUE_ACTIONS", "TOMBSTONES", "TOMBSTONE_STATES",
           "NAMED_BY", "NAMED_BY_USER", "NAME_CHARS", "when_words", "can_do", "things", "routines", "ways", "page",
           "parse_ref", "skill_edit"]
