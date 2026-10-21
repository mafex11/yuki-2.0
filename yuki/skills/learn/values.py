"""Known values: "your things" (audit 17 Oct, contract C5; learning.md §9.1-9.2).

A capability's occasions make ONE learned record per capability (its ``<capability>#all`` row), and the values the
user used in its value slot (the sites they open, the meeting services whose tab they bring forward, the playlists
they play) are data on that record, never skills of their own. Each value carries:

* ``id`` - a keyed digest of the value (stable across passes; never the value itself);
* ``slot`` / ``value`` / ``label`` / ``name`` - the slot it fills, the value as the occasions carried it, its label
  from data (a site's dominant host, else the value: what a request passes for it) and its name (the label, or the
  user's Rename);
* ``n`` / ``days`` / ``said`` / ``first_at`` / ``last_at`` - how often the user did it, on how many days, how often
  they asked Yuki for it, when first and last;
* ``hours`` (24 counts, local) and ``weekdays`` (7 counts, Monday first) - when;
* ``parts`` - the other key slots' values with counts (a site's page kinds: ``{"page": {"watch": 645, ...}}``);
* ``means`` / ``usual`` - the means slots' options with counts (the browser per site) and the one the user uses: for
  a means slot that reads a preference (a site's browser), the preference's own answer (``yuki.skills.prefs.settled``,
  read by :func:`known` and :func:`all_values`: one truth, phase 3.1), else the option with >= :data:`USUAL_SHARE` of
  its uses;
* ``everyday`` - ``True`` / ``False`` / ``None`` (unknown): ``False`` is work (never offered, still resolved on
  request); carried from a retired per-value row's naming verdict, and set by the user's "This is work";
* ``mark`` - the user's say: ``"not_mine"`` (left off the menu, the card and the you-card) or ``None``.

The value bar is light (values are cheap): seen on :data:`MIN_DAYS` days, or asked once. Names come from data, never a
model call. The record's spec holds them encrypted (``spec_ciphertext``); logs carry counts only.

The user's say outlives a value's idle days: the record's spec also keeps ``marks`` (``{value id: {name, named_by,
everyday, mark}}``, :func:`marks_of`) for every value that holds a Rename, Not mine or a work / everyday verdict, so a
value that drops out for a month comes back with its marks (a "This is work" site is never offered when it returns).

Routines on a value (the routine miner keys a record's occasions ``skill:<record id>|<value id>``, the occasions'
``key_value_digest``): :func:`routine_value` reads the value back, :func:`routine_slots` gives the card run its slot,
:func:`routine_offerable` its gate (work and not-mine never), :func:`value_onsets` when the user reached it.

Public::

    value_slots(cap) -> (identity slot, other key slots)
    build(group, cap, store, now, old) -> [value dicts]          # the learner's pass (write.plan)
    known(store, capability=None, *, now=None) -> {cap: [value dicts]}   # RAM-cached by the learned + prefs version
    with_prefs(store, {cap: [values]}) -> same, a preference slot's usual from the preference
    ranked(values, now) -> [value dicts]                          # recency-weighted, this hour first
    mark(store, capability, value_id, action, words="") -> dict   # Rename / Not mine / This is work
    routine_value(store, key) / routine_slots(store, key) / routine_offerable(value) / value_onsets(store, key, ...)
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
#: The marks a value can carry (set from the Playbook before 21 Oct; still honoured).
MARKS = ("not_mine",)
#: A value's fields that are the user's say (or the naming call's work verdict): kept in the record's ``marks``.
MARK_FIELDS = ("name", "named_by", "everyday", "mark")
#: At most this many kept marks per record (the values present first).
MAX_MARKS = 300
DAY_S = 86400.0

_lock = threading.Lock()
#: ``(learned version | preferences version, {capability: [values]})`` as last read.
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


def _means_slots(cap: Any) -> list[Any]:
    """The means slots a values record counts (``means``): the ones the capability binds from an occasion."""
    bind = getattr(cap, "bind", None) or {}
    return [s for s in getattr(cap, "slots", ()) or () if s.role == "means" and s.name in bind]


def _pref_slots(cap: Any) -> list[Any]:
    """Of :func:`_means_slots`, the ones that read a preference (a site's browser): their ``usual`` is the
    preference's own answer. One predicate for :func:`build` (which leaves their usual out) and :func:`with_prefs`
    (which fills exactly these), so no slot loses its usual between the two."""
    return [s for s in _means_slots(cap) if getattr(s, "pref", "")]


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
    means = _means_slots(cap)
    from_pref = {s.name for s in _pref_slots(cap)}
    lo = now - BAR_DAYS * DAY_S
    per: dict[str, list[Any]] = defaultdict(list)
    raw: dict[str, str] = {}
    every = [*group.occasions, *extra]
    # the means the user's own occasions of this capability used (their browsers): an asked run through another
    # program (a file path "opened" in an editor) gives no value
    own_means = {_clean((o.attrs or {}).get(s.name)) for o in every if o.by == "user" for s in means}
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
        if not user and all(any(_clean((o.attrs or {}).get(s.name)) not in own_means | {""} for s in means)
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
        used: dict[str, dict[str, int]] = {}
        usual: dict[str, str] = {}
        for s in means:
            c = Counter(_clean((o.attrs or {}).get(s.name)) for o in user if _clean((o.attrs or {}).get(s.name)))
            if not c:
                continue
            used[s.name] = dict(c.most_common(5))
            if s.name in from_pref:
                continue    # its usual is the preference's (:func:`with_prefs`, read in :func:`known`)
            top, k = c.most_common(1)[0]
            if k >= 2 and k / sum(c.values()) >= USUAL_SHARE:
                usual[s.name] = top
        name = _display(cap, ident, raw[vid], its)
        last = max(o.at for o in its)
        out.append({
            "id": vid, "slot": ident.name, "value": raw[vid], "label": name,
            "name": prior.get("name") if prior.get("named_by") == "user" and prior.get("name") else name,
            "named_by": prior.get("named_by"),
            "n": len(user), "days": len(days), "said": len(asked),
            "first_at": round(min(o.at for o in its), 1), "last_at": round(last, 1),
            "hours": hours, "weekdays": weekdays, "parts": parts, "means": used, "usual": usual,
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
    """How much of a value's use falls within an hour of now (0..1), from its hour histogram (weekdays and weekends
    together: the histogram is not split by day)."""
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


def _records(store: Any, rid: int | None = None) -> list[tuple[int, str, str, dict[str, Any], str]]:
    """``(row id, key, capability, spec, provenance json)`` of the live values records (decrypted, RAM only); ``rid``:
    that row only."""
    rows = store._query(
        "SELECT s.id, s.key, c.key, s.spec_ciphertext, s.provenance FROM skills s JOIN skills c ON c.id ="
        " s.capability_id WHERE s.origin = 'learned' AND s.deleted_at IS NULL AND s.retired_at IS NULL AND"
        " s.key LIKE '%#all'" + (" AND s.id = ?" if rid is not None else ""), (int(rid),) if rid is not None else ())
    out = []
    for rid, key, cap_key, spec_ct, prov in rows:
        try:
            spec = json.loads(store._dec(spec_ct) or "{}") if spec_ct else {}
        except Exception:  # noqa: BLE001 - a row the key can't read
            continue
        if isinstance(spec, dict) and isinstance(spec.get("values"), list):
            out.append((int(rid), str(key), str(cap_key), spec, str(prov or "{}")))
    return out


def with_prefs(store: Any, table: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """``table`` with each value's ``usual`` for a means slot that reads a preference (a site's browser) taken from
    the preference itself (:func:`yuki.skills.prefs.settled`: the user's confirmed usual, else a clear observed
    choice; none below that bar), so every place that shows "their usual" says what the engine will use (audit 18 Oct,
    phase 3.1: the you-card said one browser for a site, the preference another). The record's own counts stay in
    ``means``. New dicts; never raises (``table`` as it was)."""
    try:
        from yuki.skills import prefs
        from yuki.skills.catalog import capabilities

        caps = {c.key: c for c in capabilities().capabilities}
    except Exception:  # noqa: BLE001
        return table
    out: dict[str, list[dict[str, Any]]] = {}
    for cap_key, vals in table.items():
        cap = caps.get(cap_key)
        slots = _pref_slots(cap) if cap is not None else []
        if not slots:
            out[cap_key] = vals
            continue
        names = {s.name for s in slots}
        fresh = []
        for v in vals:
            usual = {k: o for k, o in (v.get("usual") or {}).items() if k not in names}
            for s in slots:
                try:
                    key = s.pref_key(cap_key, {str(v.get("slot") or ""): v.get("value")})
                    got = prefs.settled(store, key) if "{" not in key else None
                except Exception:  # noqa: BLE001
                    got = None
                if got is not None:
                    usual[s.name] = got.option
            fresh.append({**v, "usual": usual})
        out[cap_key] = fresh
    return out


def known(store: Any, capability: str | None = None, *, now: float | None = None) -> dict[str, list[dict[str, Any]]]:
    """``{capability: [values]}`` from the values records (as the learner last wrote them), each list best first by
    :func:`ranked` (marked not-mine left out), a preference slot's ``usual`` read from the preference
    (:func:`with_prefs`). RAM-cached until the learned rows or the preferences change (two count queries). Never
    raises (``{}``)."""
    global _cache
    try:
        from yuki.skills.catalog import learned_version
        from yuki.skills.prefs import version as prefs_version

        version = learned_version(store)
        stamp = f"{version}|{prefs_version(store)}" if version else ""
    except Exception:  # noqa: BLE001
        return {}
    with _lock:
        hit = _cache
    if hit[0] != stamp or not stamp:
        table: dict[str, list[dict[str, Any]]] = {}
        try:
            for _rid, _key, cap_key, spec, _prov in _records(store):
                table[cap_key] = [v for v in spec.get("values") or [] if isinstance(v, dict) and v.get("id")]
        except Exception:  # noqa: BLE001
            table = {}
        table = with_prefs(store, table)
        with _lock:
            _cache = (stamp, table)
        hit = _cache
    out = {k: ranked(v, now) for k, v in hit[1].items() if capability is None or k == capability}
    return out


def all_values(store: Any, capability: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Every value as stored, the marked ones too, a preference slot's ``usual`` read from the preference
    (:func:`with_prefs`). Never raises."""
    try:
        return with_prefs(store, {
            cap_key: [v for v in spec.get("values") or [] if isinstance(v, dict) and v.get("id")]
            for _rid, _key, cap_key, spec, _prov in _records(store) if capability is None or cap_key == capability})
    except Exception:  # noqa: BLE001
        return {}


def mark(store: Any, capability: str, vid: str, action: str, words: str = "") -> dict[str, Any]:
    """The user's say over one value (no window button since 21 Oct): ``rename`` (``words`` become its name, kept by
    every later pass), ``not_mine`` (left off the menu, cards and the you-card), ``work`` (``everyday`` False: resolved
    on request, never offered), ``everyday`` (``everyday`` True), ``restore`` (the mark lifted). One write on the
    record's spec. Never raises."""
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
                spec["marks"] = marks_of(spec.get("marks"), vals)
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


def not_mine_ids(store: Any) -> dict[int, set[str]]:
    """``{record id: the value ids the user marked Not mine}`` (its values and its kept marks): their occasions feed no
    preference (``yuki.skills.prefs.feed_observed``). Never raises (``{}``)."""
    out: dict[int, set[str]] = {}
    try:
        for rid, _key, _cap, spec, _prov in _records(store):
            ids = {str(v.get("id")) for v in spec.get("values") or [] if isinstance(v, dict) and v.get("mark") in MARKS}
            kept = spec.get("marks")
            ids |= {str(k) for k, f in (kept.items() if isinstance(kept, dict) else ())
                    if isinstance(f, dict) and f.get("mark") in MARKS}
            if ids:
                out[int(rid)] = ids
    except Exception:  # noqa: BLE001
        return {}
    return out


def holds_mark(v: Any) -> bool:
    """A value (or a kept mark) that holds the user's say: a Rename, Not mine, or a work / everyday verdict."""
    return isinstance(v, dict) and bool(v.get("mark") or v.get("everyday") is not None or v.get("named_by") == "user")


def marks_of(kept: Any, vals: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The record's ``marks``: the kept marks (``{value id: fields}``) updated by the current values (a value present
    now says its own say: a restore lifts its kept mark), at most :data:`MAX_MARKS`, the values present first."""
    out: dict[str, dict[str, Any]] = {}
    present = {str(v.get("id")): v for v in vals if isinstance(v, dict) and v.get("id")}
    for vid, v in present.items():
        if holds_mark(v):
            out[vid] = {k: v.get(k) for k in MARK_FIELDS}
    for vid, f in (kept.items() if isinstance(kept, dict) else ()):
        if len(out) >= MAX_MARKS:
            break
        if str(vid) not in present and holds_mark(f):
            out[str(vid)] = {k: f.get(k) for k in MARK_FIELDS}
    return out


def _split(key: Any) -> tuple[int, str] | None:
    """``(record id, value id)`` of a routine key ``skill:<id>|<value id>``, else ``None``."""
    parts = str(key or "").split("|")
    if len(parts) != 2 or not parts[0].startswith("skill:") or not parts[1]:
        return None
    try:
        return int(parts[0][6:]), parts[1]
    except ValueError:
        return None


def routine_value(store: Any, key: Any) -> tuple[str, dict[str, Any]] | None:
    """For a routine on one value of a capability's record (``skill:<record id>|<value id>``, as
    ``yuki.skills.learn.routine_inputs`` keys it): ``(capability key, the value as stored)``, marked ones too; ``None``
    for any other key (a playlist routine's ``|<entity key>``, a per-value row's ``skill:<id>``). Never raises."""
    got = _split(key)
    if got is None:
        return None
    rid, vid = got
    try:
        for _rid, _key, cap_key, spec, _prov in _records(store, rid):
            for v in spec.get("values") or []:
                if isinstance(v, dict) and v.get("id") == vid:
                    return cap_key, v
    except Exception:  # noqa: BLE001
        return None
    return None


def routine_slots(store: Any, key: Any) -> dict[str, Any]:
    """The card run's slot for a value routine (:func:`routine_value`): ``{value slot: its label}`` (what a request
    passes for it, the menu's "theirs"), RAM only; ``{}`` for any other key."""
    got = routine_value(store, key)
    if got is None:
        return {}
    _cap, v = got
    value = v.get("label") or v.get("value")
    return {str(v.get("slot")): value} if v.get("slot") and value else {}


def routine_offerable(value: dict[str, Any] | None) -> tuple[bool, str]:
    """A value routine's own gate: never a value the user marked Not mine, never work (``everyday`` False), and only
    one known to be everyday (``None``: unknown, maybe work). ``(ok, why)``."""
    if not isinstance(value, dict):
        return False, "no_value"
    if value.get("mark") in MARKS:
        return False, "not_mine"
    if value.get("everyday") is False:
        return False, "work"
    if value.get("everyday") is not True:
        return False, "may_be_work"
    return True, "ok"


def value_onsets(store: Any, key: Any, since: float, until: float) -> list[float]:
    """When the user reached a value routine's end in ``[since, until]``, oldest first, never Yuki's own (D18): the
    record's user occasions of that value (``occasions.key_value_digest``), and the user's own end moments whose bound
    value is that one (the occasions lag the moments by up to the incremental pass). ``[]`` for any other key. Never
    raises."""
    got = _split(key)
    if got is None:
        return []
    rid, vid = got
    out: set[float] = set()
    try:
        for (at,) in store._query("SELECT at FROM occasions WHERE skill_id = ? AND key_value_digest = ? AND by = 'user'"
                                  " AND at >= ? AND at <= ?", (rid, vid, float(since), float(until))):
            out.add(float(at))
    except Exception:  # noqa: BLE001
        pass
    try:
        from yuki.skills.catalog import capabilities
        from yuki.skills.learn.bind import attrs_for
        from yuki.skills.learn.occasions import moment_src

        row = store._query("SELECT c.key FROM skills s JOIN skills c ON c.id = s.capability_id WHERE s.id = ?", (rid,))
        cap = capabilities().get(str(row[0][0])) if row else None
        ident = value_slots(cap)[0] if cap is not None else None
        kind = str(getattr(cap, "end", "") or "") if cap is not None else ""
        if ident is not None and kind:
            for m in store.moments_between(float(since), float(until) + 1.0, kinds=[kind]):
                if str(m.get("by") or "user") == "yuki":
                    continue
                raw = attrs_for(cap, moment_src(m)).get(ident.name)
                if raw not in (None, "") and value_id(store.cipher, ident.name, raw) == vid:
                    out.add(float(m["at"]))
    except Exception:  # noqa: BLE001
        pass
    return sorted(out)


__all__ = ["MIN_DAYS", "MIN_ASKED", "USUAL_SHARE", "MAX_VALUES", "MARKS", "MARK_FIELDS", "value_slots", "value_id",
           "build", "recency", "hour_weight", "ranked", "known", "with_prefs", "all_values", "mark", "holds_mark",
           "marks_of", "not_mine_ids", "routine_value", "routine_slots", "routine_offerable", "value_onsets"]
