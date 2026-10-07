"""Go-tos and chords per app, counted by code from the action trace (report 78 §5.1 and §6.4, phase C1b).

The user's words of 7 Oct: "so when i tell it to do something it doesnt have to guess in the pool of data, it just
knows what i usually do". This module is the counting half: for each app, which buttons and fields the user goes to
most (their element keys) and which shortcuts they press (their chords), with how often, on how many days and when
last. No model, no segmentation, no word list: counts over machine identities, decayed by age.

What counts (machine fields of ``ui_actions``, migration 38):

* only the user's own input: ``source`` ``user`` or ``user_injected`` (dictation, Deskflow, G Hub), never ``yuki``;
* an element only from a row whose identity was really taken (``resolved`` ``point``, ``event``, ``snapshot`` or
  ``focus``): never ``stale`` (a structure change between the press and the answer) nor ``unresolved`` (point-only);
* an element only for the kinds that act on one (:data:`ELEMENT_KINDS`: clicks, menus, selections, invokes, drags,
  typing and pasting into a field), never hovers or scrolls;
* a chord only when it holds Ctrl, Alt or Win (our own fixed chord format, :mod:`yuki.memory.trace_keys`): a named key
  alone (Enter, Tab, an arrow) is not a shortcut.

**Kept up to date cheaply.** :func:`update` folds only the rows written since its cursor (the last ``ui_actions.id``
it read, at most :data:`BATCH` a call) into per-app tallies: one small encrypted ``nudge_state`` value per app (named
by a keyed digest of the process) plus an index (:data:`STATE_KEY`). A tick reads, decrypts and writes again only the
apps its new rows are in (second verification V8: one value for every app cost about 0.9 MB rewritten a tick at the
caps). The practice worker calls it on its tick, so nothing scans the trace on a request. A tally's
score is an exponentially decayed count (half-life :data:`HALF_LIFE_DAYS`): folding a new use is
``score * 0.5 ** (age / half-life) + 1``, so recency needs no rescan. Tallies are capped per app
(:data:`MAX_ELEMENTS`, :data:`MAX_CHORDS`) and apps overall (:data:`MAX_APPS`); one unused for :data:`KEEP_DAYS` is
dropped (the element registry's own keep). A forget (or its undo) of the trace writes a ``memory_changelog`` line of
pass ``trace``: the next :func:`update` sees the newer line and rebuilds from the rows left, in bounded steps.

**What a request is shown** (:func:`go_tos`, read by :func:`yuki.memory.practice.relevant`): for each app in play (at
most :data:`APPS_SHOWN`, in the order given: the apps the words name come first), the top :data:`TOP_ELEMENTS`
elements and :data:`TOP_CHORDS` chords. An element's **name** comes only from the element registry (``ui_elements``),
which keeps it by the C1 rules: a command control's (Button, CheckBox ...) on first use, a menu item's, tab's or
title's only once the same one was used on 3 distinct days, a content control's (a chat, a list item, a file) never.
An element without a kept name shows as its role and position ("a ListItem in a List"). An element the registry no
longer has (forgotten, unused 60 days) is not shown, nor (capC1c, :func:`low_info`) one that would show with no name
and exposes no actionable control pattern (an unnamed Group in a Group): its tally is kept, it is just no hint. ``detail=True`` gives more of each. Read from an in-process cache
of the decrypted tallies (re-read only when the stored value changed): a request costs dictionary lookups and one
small indexed read of the registry for the names.

Content: element keys are keyed digests; names are decrypted only into the answer (the brain's prompt), never logged.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from datetime import datetime
from typing import Any, Iterable, Sequence

from yuki.memory.store import app_key

#: The kinds of action whose element is a go-to (pointer and field actions; hovers and scrolls are not).
ELEMENT_KINDS = frozenset({"click", "dclick", "rclick", "menu", "select", "invoke", "drag", "type", "paste"})
#: How an element was found when its identity counts (``stale`` and ``unresolved`` never do).
IDENTITY_RESOLVED = frozenset({"point", "event", "snapshot", "focus"})
#: The user's own input (dictation, Deskflow and G Hub macros count as theirs, report 78 §4.2).
USER_SOURCES = ("user", "user_injected")
#: The modifiers that make a key press a shortcut (Windows' own; our fixed chord format "Ctrl+Shift+T").
CHORD_MODIFIERS = frozenset({"Ctrl", "Alt", "Win"})
#: A use's weight halves every this many days (recency without a rescan).
HALF_LIFE_DAYS = 14.0
#: A tally unused this long is dropped (the element registry's own keep, report 78 §8).
KEEP_DAYS = 60.0
#: Tallies kept at most: apps, elements per app, chords per app (the weakest by decayed score go first).
MAX_APPS = 60
MAX_ELEMENTS = 60
MAX_CHORDS = 30
#: Rows folded per :func:`update` call at most (a rebuild after a forget goes on over the next ticks).
BATCH = 5_000
#: ``nudge_state`` name of the tallies' encrypted index (:func:`_empty`); each app's tallies are a value of their own
#: named :data:`APP_PREFIX` + a keyed digest of the process (second verification V8).
STATE_KEY = "trace:habits"
APP_PREFIX = STATE_KEY + ":app:"
#: What one request is shown at most: apps, elements and chords per app (``detail=True``: the second set).
APPS_SHOWN = 3
TOP_ELEMENTS = 5
TOP_CHORDS = 3
DETAIL_APPS = 6
DETAIL_ELEMENTS = 10
DETAIL_CHORDS = 8
#: An element a pitfall names (the same element key Yuki failed on) ranks this much lower; it is never left out.
PITFALL_RANK = 0.5
DAY_S = 86400.0

_COLUMNS = "id, at, process, kind, source, resolved, element_key, role, position, chord, facts"


def _col(row: Any, name: str) -> Any:
    """A column of a row that may predate it (a dict built by hand, or a row read before capC1c)."""
    try:
        return row[name] if name in row.keys() else None
    except Exception:  # noqa: BLE001
        return None


def _actionable(row: Any) -> bool | None:
    """capC1c: whether the row's element exposes an actionable control pattern (``facts.patterns``, read from UI
    Automation with the element); None when the row did not record it (older rows)."""
    raw = _col(row, "facts")
    if not raw:
        return None
    try:
        facts = json.loads(raw)
    except (TypeError, ValueError):
        return None
    patterns = facts.get("patterns") if isinstance(facts, dict) else None
    return None if patterns is None else bool(patterns)


def low_info(tally: dict[str, Any], name: str | None) -> bool:
    """capC1c: an element that would be shown with no name and that exposes no actionable pattern (an unnamed Group
    in a Group, an unnamed Document in a Window): it tells the brain nothing about what to press, so it is never shown
    as a usual control. Its tally is still kept (other uses, later phases). Patterns not recorded (older rows) are
    judged by the control type: a container type (Group, Pane, Document, Custom, Window) counts as not actionable."""
    if name:
        return False
    actionable = tally.get("a")
    if actionable is None:
        from yuki.memory.trace_uia import CONTAINER_ROLES

        actionable = str(tally.get("r") or "") not in CONTAINER_ROLES
    return not actionable


def _decay(score: float, since: float, now: float) -> float:
    return float(score) * 0.5 ** (max(0.0, float(now) - float(since)) / (HALF_LIFE_DAYS * DAY_S))


def _day(at: float) -> str:
    return datetime.fromtimestamp(float(at)).strftime("%Y-%m-%d")


def is_chord(chord: Any) -> bool:
    """Whether a keys row's chord is a shortcut: it holds Ctrl, Alt or Win (parsing our own fixed format)."""
    parts = str(chord or "").split("+")
    return len(parts) >= 2 and any(p in CHORD_MODIFIERS for p in parts[:-1])


def chord_keys(chord: Any) -> list[str]:
    """A chord as the skill runner's keys (``"Ctrl+K"`` -> ``["ctrl", "k"]``); ``[]`` when it is not 1-4 keys."""
    parts = [p.strip().casefold() for p in str(chord or "").split("+") if p.strip()]
    return parts if 1 <= len(parts) <= 4 else []


# -- the tallies ---------------------------------------------------------------------------------------------------


def _empty() -> dict[str, Any]:
    """The tallies' index (:data:`STATE_KEY`): the fold cursor, the trace changelog line it was built after, and per
    app its last use (``t``) and its decayed strength (``s``, as of ``a``). Each app's own tallies live in a value of
    their own (:func:`_app_name`, second verification V8)."""
    return {"v": 2, "cursor": 0, "log": 0, "at": 0.0, "apps": {}}


def _empty_app() -> dict[str, Any]:
    return {"el": {}, "ch": {}}


def _fold(entry: dict[str, Any] | None, at: float, extra: dict[str, Any]) -> dict[str, Any]:
    """One more use of a tally (decayed score, count, distinct days, last use, the latest role and position)."""
    day = _day(at)
    if entry is None:
        return {"s": 1.0, "t": at, "n": 1, "d": 1, "ld": day, **extra}
    last = float(entry.get("t") or at)
    if at >= last:
        entry["s"] = _decay(float(entry.get("s") or 0.0), last, at) + 1.0
        entry["t"] = at
        entry.update(extra)
    else:   # a row older than the last one folded (rows come in id order, nearly time order)
        entry["s"] = float(entry.get("s") or 0.0) + 0.5 ** ((last - at) / (HALF_LIFE_DAYS * DAY_S))
    entry["n"] = int(entry.get("n") or 0) + 1
    if day != entry.get("ld") and at >= last:
        entry["d"] = int(entry.get("d") or 0) + 1
        entry["ld"] = day
    return entry


def fold_rows(state: dict[str, Any], rows: Iterable[Any]) -> int:
    """Fold trace rows (``ui_actions`` columns :data:`_COLUMNS`) into the tallies; returns how many counted."""
    apps: dict[str, Any] = state.setdefault("apps", {})
    counted = 0
    for r in rows:
        state["cursor"] = max(int(state.get("cursor") or 0), int(r["id"]))
        if r["source"] not in USER_SOURCES or not r["process"]:
            continue
        at = float(r["at"])
        app = apps.setdefault(str(r["process"]), {"el": {}, "ch": {}})
        if r["kind"] == "keys" and is_chord(r["chord"]):
            app["ch"][str(r["chord"])] = _fold(app["ch"].get(str(r["chord"])), at, {})
            counted += 1
        elif r["kind"] in ELEMENT_KINDS and r["element_key"] and r["resolved"] in IDENTITY_RESOLVED:
            key = str(r["element_key"])
            extra: dict[str, Any] = {"r": r["role"] or "", "p": r["position"] or ""}
            actionable = _actionable(r)
            if actionable is not None:
                extra["a"] = actionable          # capC1c: the latest row's patterns decide (:func:`low_info`)
            app["el"][key] = _fold(app["el"].get(key), at, extra)
            counted += 1
    return counted


def _prune_app(app: dict[str, Any], now: float) -> None:
    """Drop one app's tallies unused for :data:`KEEP_DAYS` and keep its strongest within the caps."""
    cut = float(now) - KEEP_DAYS * DAY_S
    for part, cap in (("el", MAX_ELEMENTS), ("ch", MAX_CHORDS)):
        items = {k: v for k, v in (app.get(part) or {}).items() if float(v.get("t") or 0.0) >= cut}
        if len(items) > cap:
            keep = sorted(items, key=lambda k: -_decay(items[k]["s"], items[k]["t"], now))[:cap]
            items = {k: items[k] for k in keep}
        app[part] = items


def _strength(app: dict[str, Any], now: float) -> float:
    return sum(_decay(v["s"], v["t"], now) for part in ("el", "ch") for v in (app.get(part) or {}).values())


def prune(state: dict[str, Any], now: float) -> None:
    """Drop tallies unused for :data:`KEEP_DAYS` and keep the strongest within the caps (a whole ``{"apps": {process:
    {"el", "ch"}}}`` held in memory)."""
    apps = state.get("apps") or {}
    for proc in list(apps):
        _prune_app(apps[proc], now)
        if not apps[proc]["el"] and not apps[proc]["ch"]:
            del apps[proc]
    if len(apps) > MAX_APPS:
        for proc in sorted(apps, key=lambda p: _strength(apps[p], now))[: len(apps) - MAX_APPS]:
            del apps[proc]


def _app_name(store: Any, proc: str) -> str:
    """``nudge_state`` name of one app's tallies (second verification V8: one small value per app, so a tick rewrites
    only the apps that had new rows): a keyed digest of the process, never its name in the clear."""
    return APP_PREFIX + str(store.cipher.digest("trace-habits-app", proc))


def _decode(store: Any, raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(store._dec(raw) or "{}")
    except Exception:  # noqa: BLE001 - unreadable (another key): treated as absent
        return None
    return data if isinstance(data, dict) else None


def _load(store: Any) -> dict[str, Any]:
    """The index, read fresh (a copy the caller may change); an older single-value form (``v`` 1) or an unreadable
    one starts again from the rows."""
    state = _decode(store, store.nudge_state(STATE_KEY))
    return state if state is not None and state.get("v") == 2 else _empty()


def _load_app(store: Any, proc: str) -> dict[str, Any]:
    data = _decode(store, store.nudge_state(_app_name(store, proc))) or {}
    return {"el": dict(data.get("el") or {}), "ch": dict(data.get("ch") or {})}


def _put(store: Any, values: dict[str, str | None], *, drop_apps: bool, now: float) -> None:
    """Write (or with ``None`` delete) these ``nudge_state`` values in one transaction; ``drop_apps``: first delete
    every app's tallies (a rebuild)."""
    def tx(conn: Any) -> None:
        if drop_apps:
            conn.execute("DELETE FROM nudge_state WHERE name LIKE ?", (APP_PREFIX + "%",))
        for name, value in values.items():
            if value is None:
                conn.execute("DELETE FROM nudge_state WHERE name=?", (name,))
            else:
                conn.execute("INSERT INTO nudge_state(name, value, updated_at) VALUES (?,?,?) ON CONFLICT(name) DO"
                             " UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (name, value, now))

    store._write(tx)


def _trace_log_id(store: Any) -> int:
    """The newest ``memory_changelog`` line of pass ``trace`` (a forget or its undo), 0 when none."""
    try:
        row = store._query("SELECT max(id) FROM memory_changelog WHERE pass = 'trace'")
        return int(row[0][0] or 0) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def update(store: Any, *, now: float | None = None, batch: int = BATCH) -> dict[str, Any]:
    """Fold the trace rows written since the last call into the tallies (at most ``batch``); rebuild from the rows
    left after a forget or its undo. Cheap when nothing is new: one indexed read. Second verification V8: only the
    apps the new rows are in are read, decrypted and written again (plus the small index), never every app's
    tallies. Returns counts (content-free)."""
    now = time.time() if now is None else float(now)
    try:
        newest = store._query("SELECT max(id) FROM ui_actions")
    except Exception:  # noqa: BLE001 - a database before migration 38
        return {"skipped": "no trace"}
    newest_id = int(newest[0][0] or 0) if newest else 0
    log_id = _trace_log_id(store)
    if _seen.get(_cache_key(store)) == (newest_id, log_id):
        return {"rows": 0}     # nothing new since this process last folded: no decrypt, no write
    state = _load(store)
    rebuilt = False
    if log_id != int(state.get("log") or 0):
        state = _empty()
        state["log"] = log_id
        rebuilt = True
    if newest_id <= int(state.get("cursor") or 0) and not rebuilt:
        _seen[_cache_key(store)] = (newest_id, log_id)
        return {"rows": 0}
    rows = store._query(f"SELECT {_COLUMNS} FROM ui_actions WHERE id > ? ORDER BY id LIMIT ?",
                        (int(state.get("cursor") or 0), int(batch)))
    procs = sorted({str(r["process"]) for r in rows if r["process"] and r["source"] in USER_SOURCES})
    # a rebuild starts every app from nothing; otherwise only the apps these rows are in are read
    work: dict[str, Any] = {"cursor": int(state.get("cursor") or 0),
                            "apps": {p: (_empty_app() if rebuilt else _load_app(store, p)) for p in procs}}
    counted = fold_rows(work, rows)
    state["cursor"] = int(work["cursor"])
    if not rows:
        state["cursor"] = max(int(state.get("cursor") or 0), newest_id)
    index: dict[str, Any] = state.setdefault("apps", {})
    values: dict[str, str | None] = {}
    for proc, app in work["apps"].items():
        _prune_app(app, now)
        if app["el"] or app["ch"]:
            last = max(float(v.get("t") or 0.0) for part in ("el", "ch") for v in app[part].values())
            index[proc] = {"t": last, "s": round(_strength(app, now), 4), "a": now}
            values[_app_name(store, proc)] = store._enc(json.dumps(app, separators=(",", ":")))
        else:
            index.pop(proc, None)
            values[_app_name(store, proc)] = None
    # apps unused for KEEP_DAYS go; past MAX_APPS the weakest go (by the strength the index keeps for each)
    cut = now - KEEP_DAYS * DAY_S
    gone = [p for p, v in index.items() if float(v.get("t") or 0.0) < cut]
    if len(index) - len(gone) > MAX_APPS:
        rest = sorted((p for p in index if p not in gone),
                      key=lambda p: _decay(float(index[p].get("s") or 0.0), float(index[p].get("a") or now), now))
        gone += rest[: len(index) - len(gone) - MAX_APPS]
    for proc in gone:
        index.pop(proc, None)
        values[_app_name(store, proc)] = None
    state["at"] = now
    values[STATE_KEY] = store._enc(json.dumps(state, separators=(",", ":")))
    _put(store, values, drop_apps=rebuilt, now=now)
    with _lock:
        _cache.pop(_cache_key(store), None)
        for k in [k for k in _app_cache if k[0] == _cache_key(store) and (rebuilt or k[1] in work["apps"]
                                                                           or k[1] in gone)]:
            _app_cache.pop(k, None)
    if int(state.get("cursor") or 0) >= newest_id:
        _seen[_cache_key(store)] = (newest_id, log_id)
    return {"rows": len(rows), "counted": counted, "rebuilt": rebuilt, "apps": len(index),
            "written": len(work["apps"]), "more": int(state.get("cursor") or 0) < newest_id}


# -- reading -------------------------------------------------------------------------------------------------------

#: ``{store key: (stored ciphertext, decoded index)}``: the decrypted index, re-read when the stored value changed.
_cache: dict[Any, tuple[str, dict[str, Any]]] = {}
#: ``{(store key, process): (stored ciphertext, decoded tallies)}``: one app's decrypted tallies, likewise.
_app_cache: dict[tuple[Any, str], tuple[str, dict[str, Any]]] = {}
#: ``{store key: (newest action id, newest trace changelog id)}`` this process has folded up to.
_seen: dict[Any, tuple[int, int]] = {}
_lock = threading.Lock()
#: Go-tos served to requests by this process, per local day, not yet added to the shared count (review F5: go-tos are
#: served in the Yuki app's process, the doctor's health is written by the memory service - the count both see is
#: the ``nudge_state`` value :data:`SERVED_KEY`).
SERVED: Counter[str] = Counter()
#: ``nudge_state`` name of the shared count: ``{"day": "YYYY-MM-DD", "n": requests served go-tos that day}``.
SERVED_KEY = "trace:go_tos_served"
#: This process adds its count to the shared one at most this often (s): one small write, not one per request.
SERVED_FLUSH_S = 30.0
#: ``{store key: when this process last added its count}``.
_flushed: dict[Any, float] = {}


def _cache_key(store: Any) -> Any:
    return getattr(store, "path", None) or id(store)


def tallies(store: Any) -> dict[str, Any]:
    """The current index of the tallies (decrypted, cached in memory until the stored value changes): ``{"cursor",
    "apps": {process: {"t", "s", "a"}}}``. One app's own tallies: :func:`app_tallies`."""
    raw = store.nudge_state(STATE_KEY) or ""
    key = _cache_key(store)
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == raw:
            return hit[1]
    state = _load(store) if raw else _empty()
    with _lock:
        _cache[key] = (raw, state)
    return state


def app_tallies(store: Any, proc: str) -> dict[str, Any]:
    """One app's tallies ``{"el": {element key: tally}, "ch": {chord: tally}}`` (decrypted, cached in memory until its
    stored value changes; read only - never changed by the caller)."""
    raw = store.nudge_state(_app_name(store, proc)) or ""
    key = (_cache_key(store), proc)
    with _lock:
        hit = _app_cache.get(key)
        if hit is not None and hit[0] == raw:
            return hit[1]
    data = _decode(store, raw) or {}
    app = {"el": dict(data.get("el") or {}), "ch": dict(data.get("ch") or {})}
    with _lock:
        _app_cache[key] = (raw, app)
    return app


def _label(name: str | None, role: str, position: str) -> str:
    """How an element is shown: its kept name with its role, else its role and position (never a name the registry
    did not keep)."""
    if name:
        return f'{role or "control"} "{name}"'
    where = ""
    if position:
        index, sep, parent = position.partition("/")
        if sep and index.isdigit():
            where = f" (item {int(index) + 1} in a {parent})" if parent else f" (item {int(index) + 1})"
        elif not sep:
            where = f" in a {position}"
    return f"an unnamed {role or 'control'}{where}"


def go_tos(store: Any, apps: Sequence[str], *, now: float | None = None, detail: bool = False,
           lower: Iterable[str] = (), count: bool = True) -> list[dict[str, Any]]:
    """For each app in play (at most :data:`APPS_SHOWN`, :data:`DETAIL_APPS` with ``detail``; in the order given),
    its top elements and chords: ``[{"app", "elements": [{"label", "role", "named", "n", "days", "last_at",
    "score"}], "chords": [{"chord", "keys", "n", "days", "last_at", "score"}]}]``. Elements named only as the
    registry keeps them (:func:`_label`). ``lower``: element keys a pitfall names (ranked lower, never left out).
    ``count``: a request is shown them (counted as served, :func:`note_served`); a plain read is not. Never raises
    (``[]``)."""
    try:
        now = time.time() if now is None else float(now)
        index = tallies(store).get("apps") or {}
        wanted = [k for k in dict.fromkeys(app_key(str(a)) for a in apps or () if a) if k and k in index]
        wanted = wanted[: DETAIL_APPS if detail else APPS_SHOWN]
        if not wanted:
            return []
        table = {proc: app_tallies(store, proc) for proc in wanted}
        n_el, n_ch = (DETAIL_ELEMENTS, DETAIL_CHORDS) if detail else (TOP_ELEMENTS, TOP_CHORDS)
        lowered = set(lower or ())
        picked: dict[str, list[tuple[float, str, dict[str, Any]]]] = {}
        keys: list[str] = []
        for proc in wanted:
            els = []
            for k, v in (table[proc].get("el") or {}).items():
                score = _decay(v["s"], v["t"], now) * (PITFALL_RANK if k in lowered else 1.0)
                els.append((score, k, v))
            els.sort(key=lambda x: -x[0])
            # every tally is a candidate (at most MAX_ELEMENTS an app): some are gone from the registry and the
            # low-information ones (capC1c) are never shown, so the top ones may sit far down the list
            picked[proc] = els
            keys += [k for _s, k, _v in picked[proc]]
        names: dict[str, str | None] = {}
        for start in range(0, len(keys), 400):
            part = keys[start:start + 400]
            for r in store._query("SELECT element_key, name_ciphertext FROM ui_elements WHERE element_key IN (%s)"
                                  % ",".join("?" * len(part)), part):
                try:
                    names[r["element_key"]] = store._dec(r["name_ciphertext"]) if r["name_ciphertext"] else None
                except Exception:  # noqa: BLE001
                    names[r["element_key"]] = None
        out = []
        for proc in wanted:
            elements = []
            for score, k, v in picked[proc]:
                if k not in names:
                    continue            # forgotten or unused 60 days: the registry no longer has it
                name = names[k]
                if low_info(v, name):
                    continue            # capC1c: no name, nothing to press: no hint for the brain
                elements.append({"label": _label(name, str(v.get("r") or ""), str(v.get("p") or "")),
                                 "role": v.get("r") or "", "named": bool(name), "n": int(v.get("n") or 0),
                                 "days": int(v.get("d") or 0), "last_at": v.get("t"), "score": round(score, 3),
                                 "lowered": k in lowered})
                if len(elements) >= n_el:
                    break
            chords = sorted(((_decay(v["s"], v["t"], now), c, v) for c, v in (table[proc].get("ch") or {}).items()),
                            key=lambda x: -x[0])[:n_ch]
            entry = {"app": proc, "elements": elements,
                     "chords": [{"chord": c, "keys": chord_keys(c), "n": int(v.get("n") or 0),
                                 "days": int(v.get("d") or 0), "last_at": v.get("t"), "score": round(s, 3)}
                                for s, c, v in chords]}
            if entry["elements"] or entry["chords"]:
                out.append(entry)
        if out and count:
            note_served(store, now)
        return out
    except Exception:  # noqa: BLE001 - a hint is never worth an error on the request
        return []


def note_served(store: Any, now: float | None = None) -> None:
    """Count one request that was served go-tos: in this process, added to the shared count (:data:`SERVED_KEY`) at
    once the first time and then at most every :data:`SERVED_FLUSH_S`. Never raises."""
    now = time.time() if now is None else float(now)
    with _lock:
        SERVED[_day(now)] += 1
        due = now - _flushed.get(_cache_key(store), float("-inf")) >= SERVED_FLUSH_S
    if due:
        flush_served(store, now)


def flush_served(store: Any, now: float | None = None) -> None:
    """Add this process's uncounted go-tos to the shared count, in one statement (another process may add its own at
    the same time); a new day starts the count again. Never raises (the count is kept for the next try)."""
    now = time.time() if now is None else float(now)
    with _lock:
        pending = {d: n for d, n in SERVED.items() if n}
        SERVED.clear()
        _flushed[_cache_key(store)] = now
    if not pending:
        return
    day = max(pending)        # an older day's count left over is past: only the newest day is kept
    n = int(pending[day])
    try:
        store._write(lambda conn: conn.execute(
            "INSERT INTO nudge_state(name, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET"
            " value = CASE WHEN json_valid(value) AND json_extract(value, '$.day') = ?"
            " THEN json_object('day', ?, 'n', coalesce(json_extract(value, '$.n'), 0) + ?)"
            " WHEN json_valid(value) AND json_extract(value, '$.day') > ? THEN value ELSE excluded.value END,"
            " updated_at = excluded.updated_at",
            (SERVED_KEY, json.dumps({"day": day, "n": n}), now, day, day, n, day)))
    except Exception:  # noqa: BLE001 - kept for the next try
        with _lock:
            for d, c in pending.items():
                SERVED[d] += c


def served_today(store: Any, now: float | None = None) -> int:
    """Requests served go-tos today, by every process (the shared count, :data:`SERVED_KEY`). Never raises."""
    day = _day(time.time() if now is None else float(now))
    try:
        data = json.loads(store.nudge_state(SERVED_KEY) or "{}")
    except Exception:  # noqa: BLE001
        return 0
    return int(data.get("n") or 0) if isinstance(data, dict) and data.get("day") == day else 0


__all__ = ["APPS_SHOWN", "BATCH", "CHORD_MODIFIERS", "ELEMENT_KINDS", "HALF_LIFE_DAYS", "IDENTITY_RESOLVED",
           "KEEP_DAYS", "MAX_APPS", "MAX_CHORDS", "MAX_ELEMENTS", "PITFALL_RANK", "STATE_KEY", "TOP_CHORDS", "low_info",
           "APP_PREFIX", "app_tallies",
           "SERVED_KEY", "TOP_ELEMENTS", "USER_SOURCES", "chord_keys", "flush_served", "fold_rows", "go_tos", "is_chord",
           "note_served", "prune", "served_today",
           "tallies", "update"]
