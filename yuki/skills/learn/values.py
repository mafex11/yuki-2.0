"""Known values: "your things" (audit 17 Oct, contract C5; learning.md §9.1-9.2).

A capability's occasions make ONE learned record per capability (its ``<capability>#all`` row), and the values the
user used in its value slot (the sites they open, the meeting services whose tab they bring forward, the playlists
they play) are data on that record, never skills of their own. Each value carries:

* ``id`` - a keyed digest of the value (stable across passes; the Playbook's handle; never the value itself);
* ``slot`` / ``value`` / ``label`` / ``name`` - the slot it fills, the value as the occasions carried it, its label
  from data (a site's dominant host, else the value: what a request passes for it) and its name (the label, or the
  user's Rename);
* ``n`` / ``days`` / ``said`` / ``first_at`` / ``last_at`` - how often the user did it, on how many days, how often
  they asked Yuki for it, when first and last;
* ``hours`` (24 counts, local) and ``weekdays`` (7 counts, Monday first) - when;
* ``parts`` - the other key slots' values with counts (a site's page kinds: ``{"page": {"watch": 645, ...}}``);
* ``means`` / ``usual`` - the means slots' options with counts (the browser per site) and the one the user uses
  (>= :data:`USUAL_SHARE` of its uses);
* ``heard`` - the words an asked run carried for it when they differ from its name;
* ``everyday`` - ``True`` / ``False`` / ``None`` (unknown): ``False`` is work (never offered, still resolved on
  request); carried from a retired per-value row's naming verdict, and set by the user's "This is work";
* ``mark`` - the user's say: ``"not_mine"`` (left off the menu, the card and the you-card) or ``None``.

The value bar is light (values are cheap): seen on :data:`MIN_DAYS` days, or asked once. Names come from data, never a
model call. The record's spec holds them encrypted (``spec_ciphertext``); logs carry counts only.

Public::

    value_slots(cap) -> (identity slot, other key slots)
    build(group, cap, store, now, old) -> [value dicts]          # the learner's pass (write.plan)
    known(store, capability=None, *, now=None) -> {cap: [value dicts]}   # RAM-cached by the learned version
    ranked(values, now) -> [value dicts]                          # recency-weighted, this hour first
    mark(store, capability, value_id, action, words="") -> dict   # the Playbook's Rename / Not mine / This is work
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any

#: The value bar: seen on this many distinct days in the learner's window, or asked once.
MIN_DAYS = 2
MIN_ASKED = 1
#: A means option is a value's usual when it holds at least this share of its uses (and was used 2+ times).
USUAL_SHARE = 0.7
#: At most this many values kept per capability (the most recent-weighted first).
MAX_VALUES = 60
#: Recency half-life for ranking (days).
HALF_LIFE_DAYS = 10.0
#: The marks the user can set (the Playbook).
MARKS = ("not_mine",)
DAY_S = 86400.0

_lock = threading.Lock()
#: ``(learned version, {capability: [values]})`` as last read.
_cache: tuple[str, dict[str, list[dict[str, Any]]]] = ("", {})


def value_slots(cap: Any) -> tuple[Any | None, list[Any]]:
    """The slot whose values are the capability's things (its first required key slot bound from occasions, else its
    first key slot, else its first entity fill slot bound from an entity), and its other bound key slots (parts)."""
    bind = dict(getattr(cap, "bind", {}) or {})
    keys = [s for s in cap.slots if s.role == "key" and s.name in bind]
    if keys:
        ident = next((s for s in keys if s.required), keys[0])
        return ident, [s for s in keys if s is not ident]
    ent = next((s for s in cap.slots if s.role == "fill" and s.type == "entity" and bind.get(s.name) == "entity"),
               None)
    return ent, []


def _clean(v: Any) -> str:
    if isinstance(v, dict):
        v = v.get("name") or ""
    return " ".join(str(v if v is not None else "").split())


def value_id(cipher: Any, slot: str, value: Any) -> str:
    """A value's keyed digest (case-folded): its stable handle. Equal to the learner's key digest of that value."""
    from yuki.skills.learn.group import value_digest

    return value_digest(cipher, slot, _clean(value))


def _display(cap: Any, slot: Any, value: str, occasions: Sequence[Any]) -> str:
    """A value's name from data: for a site, the host the user dwelt on when one holds most of its visits (the same
    rule the learned rows bound), else the value."""
    from yuki.skills.learn.group import BOUND_SHARE

    if dict(getattr(cap, "bind", {}) or {}).get(slot.name) == "site":
        hosts = Counter(str(o.src.get("top_host")) for o in occasions if o.src.get("top_host"))
        total = sum(hosts.values())
        if total:
            top, n = hosts.most_common(1)[0]
            if n / total >= BOUND_SHARE:
                return top
    return value


def build(group: Any, cap: Any, store: Any, now: float, old: dict[str, dict[str, Any]] | None = None,
          *, extra: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """The values of one capability's occasions (``group.occasions`` plus ``extra``: occasions a routine anchor or a
    tombstone kept), most recent-weighted first, at most :data:`MAX_VALUES`. ``old``: the values the record held
    (``{id: value}``) and what retired per-value rows carried (their names, marks and everyday), so the user's say
    carries over by id. Never a model call."""
    from yuki.skills.learn.group import BAR_DAYS, day_key

    ident, others = value_slots(cap)
    if ident is None:
        return []
    old = old or {}
    means_slots = [s for s in cap.slots if s.role == "means" and s.name in (cap.bind or {})]
    lo = now - BAR_DAYS * DAY_S
    per: dict[str, list[Any]] = defaultdict(list)
    raw: dict[str, str] = {}
    every = [*group.occasions, *extra]
    # the means the user's own occasions of this capability used (their browsers): an asked run through another
    # program (a file path "opened" in an editor) gives no value
    own_means = {_clean((o.attrs or {}).get(s.name)) for o in every if o.by == "user" for s in means_slots}
    for o in every:
        v = _clean((o.attrs or {}).get(ident.name))
        if not v or not o.counts or o.at < lo or o.at > now + 1.0:
            continue
        vid = value_id(store.cipher, ident.name, v)
        per[vid].append(o)
        raw.setdefault(vid, v)
    out: list[dict[str, Any]] = []
    for vid, its in per.items():
        user = [o for o in its if o.by == "user"]
        asked = [o for o in its if o.by == "asked"]
        days = {day_key(o.at) for o in user}
        prior = old.get(vid) or {}
        # asked once counts when that run was checked done and not corrected (a misheard value never becomes a thing)
        said_ok = [o for o in asked if (o.facts or {}).get("verified") and not (o.facts or {}).get("corrected")]
        if not user and all(any(_clean((o.attrs or {}).get(s.name)) not in own_means | {""} for s in means_slots)
                            for o in asked):
            continue        # only ever asked through a program the user never used for it: not a thing of theirs
        if len(days) < MIN_DAYS and len(said_ok) < MIN_ASKED and not prior.get("named_by"):
            continue
        hours = [0] * 24
        weekdays = [0] * 7
        for o in user:
            if o.context_known:
                t = datetime.fromtimestamp(o.at)
                hours[t.hour] += 1
                weekdays[t.weekday()] += 1
        parts: dict[str, dict[str, int]] = {}
        for s in others:
            c = Counter(_clean((o.attrs or {}).get(s.name)) or "" for o in its)
            if c:
                parts[s.name] = dict(c.most_common(8))
        means: dict[str, dict[str, int]] = {}
        usual: dict[str, str] = {}
        for s in means_slots:
            c = Counter(_clean((o.attrs or {}).get(s.name)) for o in user if _clean((o.attrs or {}).get(s.name)))
            if not c:
                continue
            means[s.name] = dict(c.most_common(5))
            top, k = c.most_common(1)[0]
            if k >= 2 and k / sum(c.values()) >= USUAL_SHARE:
                usual[s.name] = top
        heard: list[str] = []
        name = _display(cap, ident, raw[vid], its)
        for o in asked:
            said = _clean((o.facts or {}).get("said") or "")
            if said and said.casefold() != name.casefold() and said not in heard:
                heard.append(said[:40])
        last = max(o.at for o in its)
        out.append({
            "id": vid, "slot": ident.name, "value": raw[vid], "label": name,
            "name": prior.get("name") if prior.get("named_by") == "user" and prior.get("name") else name,
            "named_by": prior.get("named_by"),
            "n": len(user), "days": len(days), "said": len(asked),
            "first_at": round(min(o.at for o in its), 1), "last_at": round(last, 1),
            "hours": hours, "weekdays": weekdays, "parts": parts, "means": means, "usual": usual,
            "heard": (list(prior.get("heard") or []) + heard)[:4] if prior.get("heard") else heard[:4],
            "everyday": prior.get("everyday"), "mark": prior.get("mark"),
            "score": round(recency(its, now), 3),
        })
    out.sort(key=lambda v: (-v["score"], -v["n"], v["id"]))
    return out[:MAX_VALUES]


def recency(occasions: Sequence[Any], now: float) -> float:
    """Recency-weighted distinct days (half-life :data:`HALF_LIFE_DAYS`), plus a bonus per asked occasion."""
    from yuki.skills.learn.group import day_key

    days: dict[str, float] = {}
    asked = 0
    for o in occasions:
        if o.by == "asked":
            asked += 1
        d = day_key(o.at)
        days[d] = max(days.get(d, 0.0), o.at)
    return sum(math.pow(0.5, max(0.0, now - at) / DAY_S / HALF_LIFE_DAYS) for at in days.values()) + 0.5 * asked


def hour_weight(value: dict[str, Any], now: float | None = None) -> float:
    """How much of a value's use falls within an hour of now (0..1), weekday-aware when the weekdays say."""
    now = time.time() if now is None else float(now)
    hours = list(value.get("hours") or [])
    if len(hours) != 24 or not sum(hours):
        return 0.0
    h = datetime.fromtimestamp(now).hour
    near = hours[h] + 0.5 * (hours[(h - 1) % 24] + hours[(h + 1) % 24])
    return near / float(sum(hours))


def ranked(values: Sequence[dict[str, Any]], now: float | None = None, *, everyday_only: bool = False
           ) -> list[dict[str, Any]]:
    """The values best first: recency-weighted use, lifted by how much of it falls at this hour; marked not-mine left
    out; ``everyday_only``: work (``everyday`` False) left out too."""
    now = time.time() if now is None else float(now)
    live = [v for v in values if v.get("mark") not in MARKS and not (everyday_only and v.get("everyday") is False)]
    return sorted(live, key=lambda v: (-(float(v.get("score") or 0.0) * (1.0 + 2.0 * hour_weight(v, now))),
                                       -int(v.get("n") or 0), str(v.get("id"))))


def _records(store: Any) -> list[tuple[int, str, str, dict[str, Any], str]]:
    """``(row id, key, capability, spec, provenance json)`` of the live values records (decrypted, RAM only)."""
    rows = store._query(
        "SELECT s.id, s.key, c.key, s.spec_ciphertext, s.provenance FROM skills s JOIN skills c ON c.id ="
        " s.capability_id WHERE s.origin = 'learned' AND s.deleted_at IS NULL AND s.retired_at IS NULL AND"
        " s.key LIKE '%#all'")
    out = []
    for rid, key, cap_key, spec_ct, prov in rows:
        try:
            spec = json.loads(store._dec(spec_ct) or "{}") if spec_ct else {}
        except Exception:  # noqa: BLE001 - a row the key can't read
            continue
        if isinstance(spec, dict) and isinstance(spec.get("values"), list):
            out.append((int(rid), str(key), str(cap_key), spec, str(prov or "{}")))
    return out


def known(store: Any, capability: str | None = None, *, now: float | None = None) -> dict[str, list[dict[str, Any]]]:
    """``{capability: [values]}`` from the values records (as the learner last wrote them), each list best first by
    :func:`ranked` (marked not-mine left out). RAM-cached until the learned rows change (one count query). Never
    raises (``{}``)."""
    global _cache
    try:
        from yuki.skills.catalog import learned_version

        version = learned_version(store)
    except Exception:  # noqa: BLE001
        return {}
    with _lock:
        hit = _cache
    if hit[0] != version or not version:
        table: dict[str, list[dict[str, Any]]] = {}
        try:
            for _rid, _key, cap_key, spec, _prov in _records(store):
                table[cap_key] = [v for v in spec.get("values") or [] if isinstance(v, dict) and v.get("id")]
        except Exception:  # noqa: BLE001
            table = {}
        with _lock:
            _cache = (version, table)
        hit = _cache
    out = {k: ranked(v, now) for k, v in hit[1].items() if capability is None or k == capability}
    return out


def all_values(store: Any, capability: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Every value as stored, the marked ones too (the Playbook shows and un-marks them). Never raises."""
    try:
        return {cap_key: [v for v in spec.get("values") or [] if isinstance(v, dict) and v.get("id")]
                for _rid, _key, cap_key, spec, _prov in _records(store) if capability is None or cap_key == capability}
    except Exception:  # noqa: BLE001
        return {}


def mark(store: Any, capability: str, vid: str, action: str, words: str = "") -> dict[str, Any]:
    """The user's say over one value (the Playbook): ``rename`` (``words`` become its name, kept by every later pass),
    ``not_mine`` (left off the menu, cards and the you-card), ``work`` (``everyday`` False: resolved on request, never
    offered), ``everyday`` (``everyday`` True), ``restore`` (the mark lifted). One write on the record's spec. Never
    raises."""
    action = str(action or "").strip()
    if action not in ("rename", "not_mine", "work", "everyday", "restore"):
        return {"ok": False, "error": "That isn't something the Playbook can do to a thing of yours."}
    words = " ".join(str(words or "").split())[:60]
    if action == "rename" and not words:
        return {"ok": False, "error": "Say what to call it."}
    done: dict[str, Any] = {"ok": False, "error": "Yuki doesn't know that one any more."}

    def op(conn: Any) -> None:
        rows = conn.execute(
            "SELECT s.id, s.spec_ciphertext FROM skills s JOIN skills c ON c.id = s.capability_id WHERE s.origin ="
            " 'learned' AND s.deleted_at IS NULL AND s.key LIKE '%#all' AND c.key = ?", (str(capability),)).fetchall()
        for rid, spec_ct in rows:
            try:
                spec = json.loads(store._dec(spec_ct) or "{}") if spec_ct else {}
            except Exception:  # noqa: BLE001
                continue
            vals = spec.get("values") if isinstance(spec, dict) else None
            if not isinstance(vals, list):
                continue
            for v in vals:
                if not isinstance(v, dict) or v.get("id") != vid:
                    continue
                if action == "rename":
                    v["name"], v["named_by"] = words, "user"
                elif action == "not_mine":
                    v["mark"] = "not_mine"
                elif action == "work":
                    v["everyday"] = False
                elif action == "everyday":
                    v["everyday"] = True
                else:
                    v["mark"] = None
                spec_json = json.dumps(spec, sort_keys=True, ensure_ascii=False, default=str)
                conn.execute("UPDATE skills SET spec_ciphertext = ?, updated_at = ? WHERE id = ?",
                             (store.cipher.encrypt(spec_json), time.time(), int(rid)))
                done.clear()
                done.update({"ok": True, "action": action})
                return

    try:
        store._write(op)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Memory didn't take that ({type(exc).__name__})."}
    with _lock:
        global _cache
        _cache = ("", {})
    return done


__all__ = ["MIN_DAYS", "MIN_ASKED", "USUAL_SHARE", "MAX_VALUES", "MARKS", "value_slots", "value_id", "build",
           "recency", "hour_weight", "ranked", "known", "all_values", "mark"]
