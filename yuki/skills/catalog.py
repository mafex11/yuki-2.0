"""The skill catalog: the capabilities (shipped files) and the user's learned skills (database rows) in one
versioned, store-aware, in-process cache (phase L design §2, §4.9, §6 "Loader"; critique L2).

Every module that loaded skills (``yuki.skills.connection.load()``) reads :func:`load` here instead. It returns a
:class:`yuki.skills.connection.Loaded`: the capabilities first (always callable on request, rung A, one neutral menu
line each), then the user's live learned skills, each a specialised capability (its bound values, its learned name,
its means fill policy, its learned rungs). A learned skill never gates its capability: the capability stays in the
catalog whatever is learned (design §2, critique H1).

**Cost** (design §9.4: request time unchanged). With a ``store`` the version check is one count over ``skills``; the
learned specs are decrypted only when that version changes, never per request. Without a store (the voice path, the
thinker's tools) it is a dict lookup: the capabilities (re-read only when a file changes) plus the learned skills the
process last read from its store. The store-owning caller (the tray's memory client when it seeds the registry, the
memory service's nightly passes) passes ``store=`` so the cache follows the database.

**What a learned skill is in the database** (written by the learner, phase L lCore; read only here):

* a ``skills`` row with ``origin = 'learned'`` and ``capability_id`` = its capability's row (``origin =
  'capability'``, seeded from the files by :func:`yuki.skills.registry.seed`), live: ``retired_at`` and ``deleted_at``
  NULL and ``state`` in :data:`LIVE_STATES` (at most :data:`yuki.skills.connection.MAX_SKILLS`, the trusted and the
  most used first);
* ``spec_ciphertext``: the encrypted JSON spec, :data:`SPEC_VERSION` ``1``::

      {"v": 1,
       "bound": {"<key or fill slot>": <value>, ...},       # the habit's constants (names: encrypted at rest)
       "means": {"<means slot>": {"policy": "<name>"}},    # the learned fill policy (web_route.BROWSER_POLICIES)
       "menu": "<the learned menu line, <= 140 chars>",    # the learner keeps it; never read at request time
       "routes": [<route tables, rung B or C, the connection-file shape>],
       "wont": ["<SKILL_WONT_LINES word>", ...],            # only adds to the capability's
       "commit": "<COMMITS word>"}                           # only stricter than the capability's

* ``name_ciphertext``: the learned name (the naming call's, validated).

**Words** (failure fix-plan Item 1): a learned skill's ``Skill.menu`` is its capability's neutral line, never the
learned menu line. That line is the naming call's free text ("open Claude (claude.ai) in Chrome when they want to chat
with Claude") and names a browser the skill does not bind (a means slot is never bound: design §4.2); it reached the
talker's and the planner's menus and, as the hand-off seed's ``goal``, the thinker doing the job by steps, and the
commit check's and "keep this as your usual?" words. What sets a learned skill apart is its name and its bound values
(the menu shows both; the seed carries the values as its slots).

A means slot is never bound (app and means are never part of a skill's identity: design §4.2); a slot is bound only
when its capability names a ``bind`` source for it (an occasion's attribute), and never when it is a slot a
``sends_when`` condition reads, a ``told`` slot or a ``bool`` slot (code word ``not_bindable``: a learned spec can't
turn a draft into a send or join a channel nobody named, review r2 M1); a learned rung is never rung A (rung A is the
capability's own); ``wont`` and ``commit`` only ever get stricter. A row that breaks any of these is skipped and
reported (``learned_error``, content-free: its row id and a code word), never guessed.

**The switch** (failure fix-plan Item 1; ``yuki.skills.learned_enabled``): the learned rows are loaded only with the
user's skills switch ``on``. With ``"capabilities"`` (or ``"off"``) :func:`load` returns the capabilities alone, under a
version of its own (a menu snapshot or a cached tool block built with the learned lines is never reused for it), and
never decrypts a learned row; the rows stay in the database. ``load(learned=True)`` reads them anyway (a caller that
must see the user's learned rows whatever the switch says: none in the request path).

**Nameless rows**: a learned row the naming call never named (``provenance.named`` false, not renamed by the user: a
night with no call budget) gets ``Skill.name = ""``, as :class:`yuki.skills.connection.Skill` documents; its stored
label is code-built ("<site> in <browser>: open page") and names things the skill does not bind. The menu leaves such
a skill off (its capability's line stands for it); it is still in the catalog, so it runs and counts as before.

A bound value runs as the skill's own: on the learned skill's copy of the slot it is the default and the slot's live
fill, taste fill and in-call question are cleared, so only the user's words replace it (review r2 L1). The contract
for the callers (lCore's run ledger, lPage's menu): a word value that differs from a bound key slot's value is the
capability's run, not the learned skill's (it never counts on the learned skill's ``skill_id``, D14).
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from yuki.skills import connection
from yuki.skills.connection import (MAX_SKILLS, MENU_LINE_CHARS, RUNGS, ConnectionError_, Loaded, Route, Skill,
                                    fill_partial, parse_route, strictest)

#: The learned spec's format version (bumped only with a reader for the old one: design §4.2, critique L3).
SPEC_VERSION = 1
#: The learned states that are live in the catalog (``seen`` is below the bar; ``dormant`` / ``retired`` / ``deleted``
#: / ``not_mine`` are out of it: design §4.7).
LIVE_STATES: tuple[str, ...] = ("candidate", "trusted")
#: A learned skill's key: the learner's versioned derivation (capability + digests of its bound values); plain text,
#: no spaces, at most this long.
KEY_CHARS = 120


@dataclass(frozen=True)
class _Row:
    """One live learned row, decrypted (RAM only). ``by_user``: its name is the user's own Rename (kept as they wrote
    it: :func:`means_free` cuts only a name Yuki made)."""

    id: int
    key: str
    capability: str
    spec: Mapping[str, Any]
    name: str
    by_user: bool = False


_lock = threading.Lock()
#: The learned rows the process last read from its store: ``(learned version, rows, errors)``.
_learned: tuple[str, tuple[_Row, ...], tuple[str, ...]] = ("", (), ())
#: vRound2: the names each means slot's values go by on this PC, as the process last read them with its store
#: (:func:`means_names`): ``{preference base ("web.open_page:browser"): (names, ...)}``. RAM only.
_means_seen: dict[str, tuple[str, ...]] = {}
#: Per folder set: ``(capabilities version, learned version, Loaded)``.
_cache: dict[tuple[str, ...], tuple[str, str, Loaded]] = {}
#: Errors already reported to a log (each once per process).
_reported: set[str] = set()


def capabilities(dirs: Iterable[str | Path] | None = None) -> Loaded:
    """The capabilities alone (the shipped files: :func:`yuki.skills.connection.load`)."""
    return connection.load(dirs)


def learned_version(store: Any) -> str:
    """What the learned rows depend on (their count, newest update and id sum): one count, no decryption. ``""``
    when the store has no learned row (or no ``skills`` table yet)."""
    try:
        rows = store._query("SELECT count(*), max(updated_at), sum(id) FROM skills WHERE origin = 'learned'")
    except Exception:  # noqa: BLE001 - a database before migration 41: nothing learned
        return ""
    if not rows:
        return ""
    n, at, ids = tuple(rows[0])[:3]
    return f"{int(n)}:{float(at or 0.0):.3f}:{int(ids or 0)}" if n else ""


def _decode(store: Any) -> tuple[tuple[_Row, ...], tuple[str, ...]]:
    """The live learned rows, decrypted in RAM, and the content-free errors of the ones skipped."""
    try:
        rows = store._query(
            "SELECT s.id, s.key, s.spec_ciphertext, s.name_ciphertext, c.key, s.provenance FROM skills s "
            "JOIN skills c ON c.id = s.capability_id WHERE s.origin = 'learned' AND s.retired_at IS NULL "
            "AND s.deleted_at IS NULL AND s.state IN (" + ",".join("?" * len(LIVE_STATES)) + ") "
            "ORDER BY CASE s.state WHEN 'trusted' THEN 0 ELSE 1 END, s.uses DESC, s.updated_at DESC, s.id LIMIT ?",
            (*LIVE_STATES, MAX_SKILLS))
    except Exception:  # noqa: BLE001 - learned rows without the learner's columns: none can be read
        return (), ("learned: unreadable",)
    out: list[_Row] = []
    errors: list[str] = []
    for rid, key, spec_ct, name_ct, cap_key, prov in rows:
        try:
            spec = json.loads(store._dec(spec_ct) or "{}") if spec_ct else {}
            name = " ".join(str(store._dec(name_ct) or "").split()) if name_ct and named(prov) else ""
        except Exception:  # noqa: BLE001
            errors.append(f"learned:{int(rid)}: undecryptable")
            continue
        if not isinstance(spec, Mapping) or int(spec.get("v") or 0) != SPEC_VERSION:
            errors.append(f"learned:{int(rid)}: spec_version")
            continue
        out.append(_Row(id=int(rid), key=str(key or ""), capability=str(cap_key or ""), spec=spec, name=name,
                        by_user=_by_user(prov)))
    return tuple(out), tuple(errors)


def _by_user(provenance: Any) -> bool:
    """Whether a learned row's name is the user's own Rename (``skills.provenance``: ``named_by = "user"``)."""
    if isinstance(provenance, (str, bytes)):
        try:
            provenance = json.loads(provenance or "{}")
        except ValueError:
            return False
    return isinstance(provenance, Mapping) and provenance.get("named_by") == "user"


# ---------------------------------------------------------------------------------------------------------------------
# vRound2: a means slot's value is never part of a learned name the talker reads
# ---------------------------------------------------------------------------------------------------------------------
#
# A means slot is never bound (design §4.2), yet the naming call was asked to name "the program when the user uses
# it nearly always", so 5 of the 12 learned talker lines on the 10 Oct DB read "Open Excalidraw in Arc", "Open Slack in
# Chrome"...: the talker took "ARC browser" in "put something on YouTube and ARC browser" for the Excalidraw skill
# (sim-e2e-1, sVoiceFix "Left"), and the thinker's note named the browser the same way. The cut below is the slot's own
# data, never a word list: the phrase its ``say`` template makes of a value ("in {browser}" -> "in Arc"), for each
# name a value of that slot goes by on this PC (the options the user's runs and occasions gave it: ``prefs``, keyed by
# the slot's preference base; the names their timeline shows each program under).


def pref_base(slot: Any, capability: str) -> str:
    """A means slot's preference base: its ``pref`` up to the scope (``"web.open_page:browser"``), else
    ``"<capability>:<slot>"``."""
    pref = str(getattr(slot, "pref", "") or "")
    return pref.split("@", 1)[0] if pref else f"{capability}:{getattr(slot, 'name', '')}"


def _means_names(store: Any, caps: Loaded) -> dict[str, tuple[str, ...]]:
    """The names each capability's means slot's values go by on this PC (see the section note): every option
    ``prefs`` holds for that slot's preference base (any scope), with an app option's names from the user's timeline
    (``yuki.skills.resolve.known_apps``), and an enum slot's own options. Two queries per means slot at most; never
    raises (``{}``)."""
    out: dict[str, tuple[str, ...]] = {}
    try:
        from yuki.skills.resolve import known_apps

        apps = known_apps(store)
    except Exception:  # noqa: BLE001 - option keys alone
        apps = {}
    for cap in caps.skills:
        for slot in cap.slots:
            if slot.role != "means":
                continue
            base = pref_base(slot, cap.key)
            if base in out:
                continue
            try:
                rows = store._query("SELECT DISTINCT option FROM prefs WHERE slot = ? OR substr(slot, 1, ?) = ?",
                                    (base, len(base) + 1, base + "@"))
            except Exception:  # noqa: BLE001 - a database before migration 41: no options seen
                rows = []
            names: list[str] = [str(o) for o in slot.options]
            for (option,) in rows:
                option = " ".join(str(option or "").split())
                if not option:
                    continue
                names.append(option)
                got = apps.get(option.casefold()) if slot.type == "app" else None
                names += [str(n) for n in (got or {}).get("names") or [] if str(n).strip()]
            out[base] = tuple(dict.fromkeys(n for n in names if n.strip()))
    return out


def means_names() -> dict[str, tuple[str, ...]]:
    """The process's last-read table of :func:`_means_names` (``{}`` before any store-aware load)."""
    with _lock:
        return dict(_means_seen)


def _tok(word: str) -> str:
    return word.strip(".,:;!?()[]{}\"'").casefold()


def _cut(words: list[str], phrase: list[str]) -> tuple[list[str], bool]:
    """``words`` with every run of whole words equal to ``phrase`` taken out (case and edge punctuation aside)."""
    n, out, i, cut = len(phrase), [], 0, False
    while i < len(words):
        if n and [_tok(w) for w in words[i:i + n]] == phrase:
            i, cut = i + n, True
            continue
        out.append(words[i])
        i += 1
    return out, cut


def means_free(text: str, skill: Any, names: Mapping[str, Iterable[str]] | None = None) -> str:
    """``text`` (a learned skill's name or a line) with every unbound means slot's value phrase cut out, by the
    slot's own data: its ``say`` template filled with each name a value of that slot goes by (``names``: preference
    base -> names, :func:`means_names` when ``None``), whole words, case aside ("Open Excalidraw in Arc" with ``in
    {browser}`` and "Arc" -> "Open Excalidraw"). ``""`` when a value still stands in it after the cut (a slot with no
    template, or a phrase its template does not make: code can't cut it cleanly) or nothing is left. The text as it is
    when no value of a means slot is in it. Never raises (the text as it is)."""
    text = " ".join(str(text or "").split())
    try:
        table = means_names() if names is None else names
        bound = dict(getattr(skill, "bound", {}) or {})
        cap = str(getattr(skill, "capability", "") or getattr(skill, "key", "") or "")
        words = text.split()
        for slot in getattr(skill, "slots", ()) or ():
            if slot.role != "means" or slot.name in bound:
                continue
            values = sorted({" ".join(str(n).split()) for n in table.get(pref_base(slot, cap)) or () if str(n).strip()},
                            key=lambda n: -len(n.split()))
            say = str(slot.say or "")
            holder = "{" + slot.name + "}"
            if say.count("{") == 1 and holder in say:
                for value in values:
                    words, _hit = _cut(words, [_tok(w) for w in say.replace(holder, value).split()])
            for value in values:
                if _cut(words, [_tok(w) for w in value.split()])[1]:
                    return ""   # a value its template did not cover: no clean cut by its own data
        return " ".join(words).strip(" -,:;·|")
    except Exception:  # noqa: BLE001
        return text


def named(provenance: Any) -> bool:
    """Whether a learned row carries a real name (``skills.provenance``, content-free JSON): the naming call's
    (``named`` true) or the user's own Rename (``named_by = "user"``). Only a row the learner marked nameless
    (``named`` false: no call budget, a failed call, a refused name) is not; a row whose provenance says neither (or
    can't be read) keeps its name, as before."""
    if isinstance(provenance, (str, bytes)):
        try:
            provenance = json.loads(provenance or "{}")
        except ValueError:
            return True
    if not isinstance(provenance, Mapping):
        return True
    return provenance.get("named") is not False or provenance.get("named_by") == "user"


def _bind_route(route: Route, bound: Mapping[str, Any], policies: Mapping[str, str]) -> Route:
    """A capability's rung with the learned skill's bound values filled into its arguments and checks (the rest are
    filled per request) and its means fill policy handed to a driver (``means_policy``)."""
    args = dict(fill_partial(dict(route.args), bound)) if bound else dict(route.args)
    if policies and route.via == "driver":
        args["means_policy"] = dict(policies)
    done = replace(route.done, args=fill_partial(dict(route.done.args), bound)) if (route.done and bound) else \
        route.done
    recheck = replace(route.recheck, args=fill_partial(dict(route.recheck.args), bound)) if (
        route.recheck and bound) else route.recheck
    return replace(route, args=args, done=done, recheck=recheck)


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


def specialise(cap: Skill, *, key: str, spec: Mapping[str, Any], name: str = "") -> Skill:
    """The learned skill a spec makes of its capability (see the module docstring); raises
    :class:`yuki.skills.connection.ConnectionError_` (content-free: slot names and code words only) when the spec breaks
    a rule."""
    where = f"learned:{key[:24]}"
    if not key or len(key) > KEY_CHARS or any(ch.isspace() for ch in key):
        raise ConnectionError_(f"{where}: bad_key")
    by_name = {s.name: s for s in cap.slots}
    sends_slots = {str(cond.get("slot") or "") for cond in cap.sends_when}
    bound: dict[str, Any] = {}
    raw_bound = spec.get("bound") or {}
    if not isinstance(raw_bound, Mapping):
        raise ConnectionError_(f"{where}: bound is a table")
    for slot_name, value in raw_bound.items():
        slot = by_name.get(str(slot_name))
        if slot is None:
            raise ConnectionError_(f"{where}: bound {slot_name!r} names no slot")
        if slot.role == "means":
            # design §4.2: app and means are attributes, never a skill's identity (a means change moves a preference)
            raise ConnectionError_(f"{where}: bound {slot_name!r} is a means slot")
        if slot.name not in cap.bind or slot.told or slot.type == "bool" or slot.name in sends_slots:
            # review r2 M1: a value is bound only from an occasion's attributes (the capability's ``bind`` sources,
            # design §4.2). Never a slot that decides a send (rule 8's draft default stays code), a told value (the
            # user's own words, kept encrypted apart) or a yes/no switch (discord.voice's join_in_call: D10)
            raise ConnectionError_(f"{where}: bound {slot_name!r} not_bindable")
        if value is not None and value != "":
            bound[slot.name] = value
    policies: dict[str, str] = {}
    raw_means = spec.get("means") or {}
    if not isinstance(raw_means, Mapping):
        raise ConnectionError_(f"{where}: means is a table")
    for slot_name, value in raw_means.items():
        slot = by_name.get(str(slot_name))
        policy = str((value.get("policy") if isinstance(value, Mapping) else value) or "")
        if slot is None or slot.role != "means" or not policy.isidentifier():
            raise ConnectionError_(f"{where}: means {slot_name!r} is not a means slot with a policy")
        policies[slot.name] = policy
    # review r2 L1: a bound value is the skill's identity, not a guess, so nothing below the user's words replaces it
    # (resolve's order is words > again > told > live > pref > taste > default): its live fill, its taste fill and
    # its in-call question are cleared on the learned skill's copy of the slot
    slots = tuple(replace(s, default=bound[s.name], live="", taste="", ask_in_call=False) if s.name in bound else s
                  for s in cap.slots)
    slot_table = {s.name: s for s in slots}
    routes = [_bind_route(r, bound, policies) for r in cap.routes]
    for i, raw in enumerate(spec.get("routes") or []):
        r = parse_route(raw, f"{where}.route[{i}]", slot_table)
        if r.rung == "A":
            raise ConnectionError_(f"{where}.route[{i}]: a learned rung is B or C (rung A is the capability's)")
        routes.append(_bind_route(r, bound, policies))
    routes.sort(key=lambda r: RUNGS.index(r.rung))
    if len({r.key for r in routes}) != len(routes):
        raise ConnectionError_(f"{where}: two routes share rung, variant and verb")
    wont = tuple(dict.fromkeys([*cap.wont, *(str(w) for w in spec.get("wont") or [] if str(w).strip())]))
    commit = strictest(cap.commit, str(spec.get("commit") or ""))
    # Item 1: the capability's neutral words, never the learned free text (see "Words" in the module docstring)
    menu = _clip(cap.menu, MENU_LINE_CHARS)
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()
    learned = replace(cap, key=key, menu=menu, slots=slots, routes=tuple(routes), wont=wont, commit=commit,
                      origin="learned", capability=cap.key, name=_clip(name, 60), bound=bound, file="learned",
                      hash=digest[:16], warnings=())
    return _with_taste_kinds(learned)


def _with_taste_kinds(skill: Skill) -> Skill:
    """lWire (open point): a learned skill's free entity slot takes its taste kind from data
    (:func:`yuki.skills.taste.slot_kind`: its bound site and page are a site file's page kind with a speakable
    variable, e.g. a channel on a site's channel pages), so the resolver and the menu offer the user's own
    candidates for it. A slot with its own kind, a bound slot and any other slot are left as they are. Never raises
    (the skill as it was)."""
    try:
        from yuki.skills import taste

        kinds = {s.name: taste.slot_kind(skill, s) for s in skill.slots
                 if s.type == "entity" and not s.taste and s.name not in skill.bound}
    except Exception:  # noqa: BLE001 - no taste kind: the slot fills from the words, as before
        return skill
    if not any(kinds.values()):
        return skill
    return replace(skill, slots=tuple(replace(s, taste=kinds[s.name]) if kinds.get(s.name) else s
                                      for s in skill.slots))


def _combine(caps: Loaded, rows: tuple[_Row, ...], row_errors: tuple[str, ...], learned_v: str,
             means: Mapping[str, Iterable[str]] | None = None) -> Loaded:
    means = means or {}
    by_key = {s.key: s for s in caps.skills}
    learned: list[Skill] = []
    errors = list(caps.errors) + list(row_errors)
    for row in rows:
        cap = by_key.get(row.capability)
        if cap is None or cap.origin != "capability":
            errors.append(f"learned:{row.id}: no_capability")
            continue
        if row.key in by_key or any(s.key == row.key for s in learned):
            errors.append(f"learned:{row.id}: key_taken")
            continue
        # vRound2: a name Yuki made never carries a means slot's value (the user's own Rename stays as they wrote it)
        name = row.name if row.by_user else means_free(row.name, cap, means)
        try:
            learned.append(specialise(cap, key=row.key, spec=row.spec, name=name))
        except ConnectionError_ as exc:
            errors.append(f"learned:{row.id}: {str(exc).rpartition(': ')[2][:60]}")
    version = caps.version if not learned_v else hashlib.sha256(
        f"{caps.version}|{learned_v}|{len(learned)}".encode()).hexdigest()[:12]
    return Loaded(tuple(caps.skills) + tuple(learned[:MAX_SKILLS]), tuple(errors), version)


def _code(error: str) -> str:
    """A content-free code word for an error line (``connection_error`` / ``learned_error`` log fields)."""
    low = error.casefold()
    for word in ("habit_key", "zero setup", "undecryptable", "spec_version", "no_capability", "key_taken",
                 "unreadable", "not_bindable"):
        if word in low:
            return word.replace(" ", "_")
    return "invalid"


def _report(errors: Iterable[str], log: Callable[..., Any] | None) -> None:
    if log is None:
        return
    for error in errors:
        if error in _reported:
            continue
        _reported.add(error)
        head = error.split(":", 1)[0].strip()
        try:
            if head == "learned":
                log("learned_error", row=error.split(":")[1].strip()[:12], why=_code(error))
            else:
                log("connection_error", file=head[:60], why=_code(error))
        except Exception:  # noqa: BLE001 - a log never breaks a load
            pass


def _folders(dirs: Iterable[str | Path] | None) -> tuple[str, ...]:
    return tuple(str(Path(d)) for d in dirs) if dirs is not None else (str(connection.CONNECTIONS_DIR),)


#: The capabilities-only catalog's version part (the switch's ``capabilities`` / ``off``): never a version the full
#: catalog has, so nothing keyed on a catalog version (the talker's menu snapshot, the thinker's tool block, the
#: registry's menu version) carries learned lines over a switch.
CAPABILITIES_ONLY = "capabilities-only"


def learned_on() -> bool:
    """Whether the learned rows are loaded (the user's skills switch is ``on``: :func:`yuki.skills.learned_enabled`,
    read as a fresh ``Settings()`` would). On when the switch can't be read (the default)."""
    try:
        from yuki.skills import learned_enabled

        return learned_enabled(None)
    except Exception:  # noqa: BLE001
        return True


def _capabilities_only(caps: Loaded, dirs: Iterable[str | Path] | None) -> Loaded:
    """The capabilities alone under their own version (:data:`CAPABILITIES_ONLY`), cached per folder set."""
    key = (*_folders(dirs), CAPABILITIES_ONLY)
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == caps.version:
            return hit[2]
        loaded = Loaded(tuple(caps.skills), tuple(caps.errors),
                        hashlib.sha256(f"{caps.version}|{CAPABILITIES_ONLY}".encode()).hexdigest()[:12])
        _cache[key] = (caps.version, CAPABILITIES_ONLY, loaded)
    return loaded


def _tell_hidden(store: Any, log: Callable[..., Any] | None) -> None:
    """Tell ``log`` once per process that the switch leaves the learned rows out (``skills_switch``, content-free:
    the switch word and how many learned rows the database holds: one count, no decryption)."""
    if log is None:
        return
    with _lock:
        if "switch:hidden" in _reported:
            return
        _reported.add("switch:hidden")
    n = 0
    if store is not None:
        version = learned_version(store)
        try:
            n = int(version.split(":", 1)[0]) if version else 0
        except ValueError:
            n = 0
    try:
        from yuki.skills import mode

        word = mode(None)
    except Exception:  # noqa: BLE001
        word = "capabilities"
    try:
        log("skills_switch", mode=word, learned="hidden", learned_rows=n)
    except Exception:  # noqa: BLE001 - a log never breaks a load
        pass


def load(dirs: Iterable[str | Path] | None = None, *, store: Any = None,
         log: Callable[..., Any] | None = None, learned: bool | None = None) -> Loaded:
    """The catalog: the capabilities of ``dirs`` (default: the bundled folder), then the live learned skills. With
    ``store``: its learned rows, decrypted only when their version changed since the process last read them. Without
    one: the learned skills the process last read (none before the first store-aware load). ``log``: ``log(type,
    **fields)``, told each invalid file (``connection_error``) and skipped learned row (``learned_error``) once,
    content-free. Never raises for the learned part (the capabilities alone then).
    ``learned``: whether the learned rows are loaded; ``None`` (every caller today) follows the user's skills switch
    (:func:`learned_on`): with ``"capabilities"`` or ``"off"`` this is the capabilities alone, no learned row is read,
    and ``log`` is told so once (``skills_switch``)."""
    global _learned
    caps = connection.load(dirs)
    if not (learned_on() if learned is None else bool(learned)):
        loaded = _capabilities_only(caps, dirs)
        _tell_hidden(store, log)
        _report(loaded.errors, log)
        return loaded
    if store is not None:
        learned_v = learned_version(store)
        with _lock:
            stale = learned_v != _learned[0]
        if stale:
            rows, row_errors = _decode(store) if learned_v else ((), ())
            seen = _means_names(store, caps) if rows else {}   # vRound2: read with the rows (the same nightly pass)
            with _lock:
                _learned = (learned_v, rows, row_errors)
                _means_seen.clear()
                _means_seen.update(seen)
    with _lock:
        learned_v, rows, row_errors = _learned
        key = _folders(dirs)
        hit = _cache.get(key)
        if hit is not None and hit[0] == caps.version and hit[1] == learned_v:
            loaded = hit[2]
        else:
            loaded = _combine(caps, rows, row_errors, learned_v, dict(_means_seen))
            _cache[key] = (caps.version, learned_v, loaded)
    _report(loaded.errors, log)
    return loaded


def get(key: str, *, store: Any = None) -> Skill | None:
    """One catalog entry by key (a capability's or a learned skill's; no learned key with the switch's
    ``capabilities`` or ``off``)."""
    return load(store=store).get(str(key or ""))


def clear_cache() -> None:
    """Forget the cached catalog and learned rows (the next load re-reads them)."""
    global _learned
    with _lock:
        _learned = ("", (), ())
        _means_seen.clear()
        _cache.clear()
        _reported.clear()
    connection.clear_cache()


# ---------------------------------------------------------------------------------------------------------------------
# The generic means question (a driver route any capability may name: ``driver = "catalog"``, ``call = "ask_means"``)
# ---------------------------------------------------------------------------------------------------------------------


def route_ask_means(call: Any) -> Any:
    """A means slot's one question, as a rung (design §5: "with no usual and no observation, it asks once"). A means
    slot ships no default, so when nothing filled it (not the user's words, a usual, an observed preference or a learned
    fill policy) and the rungs that would act need it, this rung asks the slot's own ``ask`` line with its options and
    acts on nothing. The answer runs the request again with the slot filled, and the resolver's "Keep this as your
    usual?" follows; once that usual is kept, this rung's ``when`` no longer holds. ``args.slot`` names the means
    slot; the route's ``when`` says where it applies (data). No app, target or answer is written here."""
    from yuki.skills.engine import RouteResult

    name = str(call.args.get("slot") or "")
    slot = call.skill.slot(name) if name else None
    if slot is None or slot.role != "means":
        return RouteResult(ran=False, outcome="not_a_means_slot")
    if call.values.get(name) not in (None, ""):
        return RouteResult(ran=False, outcome="means_given")
    return RouteResult(ran=False, ask=slot.ask or f"Which {name}?", options=list(slot.options), outcome="ask_means",
                       facts={"slot": name})


#: Driver routes (``yuki.skills.engine.v_driver`` reads this table).
ROUTES: dict[str, Callable[[Any], Any]] = {"ask_means": route_ask_means}


__all__ = ["SPEC_VERSION", "LIVE_STATES", "KEY_CHARS", "CAPABILITIES_ONLY", "capabilities", "learned_version",
           "named", "specialise", "learned_on", "load", "get", "clear_cache", "route_ask_means", "ROUTES",
           "pref_base", "means_names", "means_free"]
