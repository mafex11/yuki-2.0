"""Writing learned skills (phase L design §4.6, §4.7, §4.9): one transaction, the cap, the trust ladder, tombstones,
content-free provenance, and the hook for the naming call (phase L ``lName``: ``yuki.skills.learn.name``).

* **Rows**: a passing capability-backed group is a ``skills`` row with ``origin = 'learned'`` and ``capability_id``
  (its capability's row, seeded from the files by ``yuki.skills.registry.seed``). ``spec_ciphertext`` holds the
  catalog's spec (``yuki.skills.catalog``, ``SPEC_VERSION`` 1: the bound key values, the menu line, the learned rungs),
  encrypted because bound values can be names; ``name_ciphertext`` the learned name; ``provenance`` content-free JSON
  (counts, days, first/last, cover, rung, means counts, the asked verbs' trust). Every spec is checked by
  ``catalog.specialise`` before it is written: a spec the catalog would refuse is never written.
* **The ledger stays by row id** (critique L3): a row keeps its id, so its trust survives a re-learn; its key is
  versioned (``group.skill_key``) and never rewritten.
* **States** (design §4.7): ``candidate`` (passed the bar), ``trusted`` (D14: ``registry.offer_ready``), ``listed``
  (below the cap of :data:`yuki.skills.connection.MAX_SKILLS`: in the Playbook only, out of the catalog), ``dormant``
  (no longer passing; :data:`DORMANT_DAYS`), ``retired`` (:data:`RETIRE_DAYS` with no occasion or use). ``deleted`` /
  ``not_mine`` with ``deleted_at``: the user's tombstone (Forget this / Not mine, written by the Playbook), never
  proposed, updated or revived here.
* **The cap** ranks by recency-weighted distinct days, uses and verified runs.
* **Names** come from one batched cheap call a night (``yuki.skills.learn.name.name_groups``, phase L lName), for new
  or changed groups only (a signature per group); a name the user gave (``provenance.named_by = "user"``) is never
  replaced. With no name the skill is nameless: it runs on request (its menu line is code-built) and is never offered.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

from yuki.skills.learn.group import KEY_VERSION, BOUND_SHARE, Group, recency_score, skill_key

#: A learned row that stops passing is ``dormant``; one with no occasion or use for this long is ``retired``.
DORMANT_DAYS = 14.0
RETIRE_DAYS = 60.0
#: The tombstone states (the Playbook's buttons write them, with ``deleted_at``).
TOMBSTONES: tuple[str, ...] = ("deleted", "not_mine")
#: Over the cap: in the Playbook only.
LISTED = "listed"
#: The naming call names at most this many groups a night (design §4.6).
NAME_BATCH = 10
NAME_WORDS = 6
NAME_CHARS = 60
DAY_S = 86400.0


@dataclass
class Planned:
    """One group on its way to a row."""

    group: Group
    cap: Any
    compiled: Any
    key: str
    score: float = 0.0
    state: str = "candidate"
    spec: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    name: str = ""
    use_when: list[str] = field(default_factory=list)
    row_id: int | None = None
    error: str = ""


@dataclass
class WriteResult:
    counts: Counter = field(default_factory=Counter)
    planned: list[Planned] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _clip(text: Any, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


def bound_values(group: Group, cap: Any) -> dict[str, Any]:
    """The values a learned skill binds (its key slots only: a means is never bound, a fill varies per request; design
    §4.2). A domain's front page binds the host the user dwelt on when one host holds :data:`BOUND_SHARE` of its
    visits, else the domain."""
    out: dict[str, Any] = {}
    for s in cap.slots:
        if s.role != "key" or s.name not in cap.bind:
            continue
        v = group.values.get(s.name)
        if v in (None, ""):
            continue
        if cap.bind.get(s.name) == "site":
            hosts = Counter(str(o.src.get("top_host")) for o in group.occasions if o.src.get("top_host"))
            total = sum(hosts.values())
            if total:
                top, n = hosts.most_common(1)[0]
                if n / total >= BOUND_SHARE:
                    v = top
        out[s.name] = v
    return out


def menu_line(cap: Any, bound: dict[str, Any], name: str = "") -> str:
    """A nameless skill's menu line, by code: the capability's line up to its first clause, then the bound values."""
    if name:
        return ""
    head = str(cap.menu).split(";")[0].split(" (")[0].strip()
    vals = ", ".join(str(v) for v in bound.values() if v not in (None, ""))
    from yuki.skills.connection import MENU_LINE_CHARS

    return _clip(f"{head}: {vals}" if vals else head, MENU_LINE_CHARS)


def provenance(group: Group, cap: Any, compiled: Any, store: Any, now: float) -> dict[str, Any]:
    """Content-free provenance (design §4.9): counts, days, first/last, weekdays, the minute it usually happens,
    cover counts, the rung and its link share, the means counts (app keys and control codes), the asked verbs."""
    user = [o for o in group.occasions if o.by == "user" and o.counts]
    timed = [o for o in user if o.context_known]
    weekdays = Counter(o.weekday for o in timed if o.weekday is not None)
    verbs: dict[str, Counter] = {}
    for o in group.occasions:
        if o.by != "asked":
            continue
        c = verbs.setdefault(str(o.facts.get("verb") or "?"), Counter())
        c["corrected" if o.facts.get("corrected") else ("ok" if o.facts.get("verified") else "unverified")] += 1
    app, share = group.top_share("app")
    label_app = ""
    if app:
        try:
            label_app = store.app_display_name(app) or app
        except Exception:  # noqa: BLE001
            label_app = app
    return {
        "v": 1, "key_v": KEY_VERSION, "capability": cap.key, "gid": group.gid, "usage": group.usage,
        "end": group.end, "exec_end": compiled.exec_end, "commit": compiled.commit, "end_commit": compiled.end_commit,
        "rung": compiled.rung, "link_share": compiled.link_share, "probe": compiled.probe,
        "offer_class": cap.offer_class, "kind": cap.kind,
        "n": group.bar.n, "days": group.bar.days, "asked": group.bar.asked, "asked_days": group.bar.asked_days,
        "first_at": round(group.bar.first_at, 1) if group.bar.first_at else None,
        "last_at": round(group.bar.last_at, 1) if group.bar.last_at else None,
        "weekdays": {str(k): v for k, v in sorted(weekdays.items())},
        "minute": round(statistics.median(o.minute for o in timed), 1) if timed else None,
        "cover": dict(Counter(o.cover for o in user)),
        "late": sum(1 for o in user if not o.context_known),
        "means": compiled.means, "app": app, "app_share": round(share, 3),
        "verbs": {k: dict(v) for k, v in verbs.items()}, "covered": compiled.covered,
        "merged": list(group.merged_from), "label": _clip(f"{label_app}: {cap.key.rpartition('.')[2].replace('_', ' ')}"
                                                          if label_app else cap.key, 60),
        "moments": [o.end_moment_id for o in sorted(user, key=lambda o: o.at)[-30:] if o.end_moment_id is not None],
        "score": None, "built_at": round(now, 1),
    }


def name_signature(planned: Planned) -> str:
    """What a name depends on (content-free): the capability, the key digests, the rung and the slots left open."""
    g = planned.group
    open_slots = sorted(s.name for s in planned.cap.slots if s.name not in planned.spec.get("bound", {}))
    raw = json.dumps([planned.cap.key, list(g.keys), planned.compiled.rung, open_slots], separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def naming_input(planned: Planned, store: Any) -> dict[str, Any]:
    """One group as the naming call reads it (design §4.6; critique L7, M8; Q13): the capability's neutral line, the
    app's display name, a calling service's product name, a site file's name and page kind or a registrable domain
    (never a path or title), the slots left open with their kinds (never a value), a time summary and the asked runs'
    keys (the request words are read by the naming module itself, encrypted at rest)."""
    from yuki.memory.moments import registrable_domain

    g, cap, prov = planned.group, planned.cap, planned.provenance
    bound = planned.spec.get("bound") or {}
    site_slot = next((s for s, src in cap.bind.items() if src == "site"), "")
    page_slot = next((s for s, src in cap.bind.items() if src == "page"), "")
    service_slot = next((s for s, src in cap.bind.items() if src == "service"), "")
    site = str(bound.get(site_slot) or "") if site_slot else ""
    try:
        from yuki.connect.templates import sites

        templated = site in sites()
    except Exception:  # noqa: BLE001
        templated = False
    means = prov.get("means") or {}
    keys = list(next(iter(means.values()), {}) or {}) or ([prov["app"]] if prov.get("app") else [])
    apps = []
    for app in keys:
        try:
            apps.append(store.app_display_name(app) or app)
        except Exception:  # noqa: BLE001
            apps.append(app)
    return {
        "signature": name_signature(planned), "capability": cap.key, "menu": cap.menu, "apps": apps[:3],
        "service": str(bound.get(service_slot) or "") if service_slot else "",
        "site": site if templated else "", "page": str(bound.get(page_slot) or "") if (page_slot and templated) else "",
        "domain": registrable_domain(site) if (site and not templated) else "",
        "slots": [{"name": s.name, "role": s.role, "kind": s.kind or s.type} for s in cap.slots
                  if s.name not in bound and not s.told],
        "weekdays": prov.get("weekdays"), "minute": prov.get("minute"), "n": prov.get("n"), "days": prov.get("days"),
        "rung": planned.compiled.rung, "asked_runs": [o.asked_run for o in g.occasions if o.by == "asked"][-3:],
        "offer_class": cap.offer_class, "usage": g.usage,
    }


def _call_naming(store: Any, inputs: list[dict[str, Any]], *, worker: Any, log: Callable[..., Any] | None
                 ) -> dict[str, dict[str, Any]]:
    """The naming call (phase L lName: ``yuki.skills.learn.name.name_groups(store, inputs, worker=, log=)`` ->
    ``{signature: {"name", "menu", "ask", "everyday", "use_when"}}``, batched, cached by signature). ``{}`` when the
    module is not there or it fails: the groups stay nameless."""
    if not inputs:
        return {}
    try:
        from yuki.skills.learn import name as naming
    except ImportError:
        return {}
    fn = getattr(naming, "name_groups", None)
    if not callable(fn):
        return {}
    try:
        got = fn(store, inputs[:NAME_BATCH], worker=worker, log=log)
    except Exception as exc:  # noqa: BLE001 - nameless this night
        if log is not None:
            try:
                log("error", where="learn.name", error=type(exc).__name__)
            except Exception:  # noqa: BLE001
                pass
        return {}
    return got if isinstance(got, dict) else {}


def _valid_name(out: Any, bound: dict[str, Any]) -> tuple[str, str, list[str], bool | None]:
    """``(name, menu, use_when, everyday)`` from one naming answer, checked by code (lName validates first): a short
    name, a menu line of at most 140 characters, a list of short strings; anything off: nameless."""
    from yuki.skills.connection import MENU_LINE_CHARS

    if not isinstance(out, dict):
        return "", "", [], None
    name = " ".join(str(out.get("name") or "").split())
    menu = " ".join(str(out.get("menu") or "").split())
    if not name or len(name) > NAME_CHARS or len(name.split()) > NAME_WORDS or len(menu) > MENU_LINE_CHARS:
        return "", "", [], None
    use_when = [_clip(u, 120) for u in out.get("use_when") or [] if isinstance(u, str) and u.strip()][:6]
    everyday = out.get("everyday") if isinstance(out.get("everyday"), bool) else None
    return name, menu, use_when, everyday


def offer_static(cap: Any, compiled: Any, everyday: bool | None) -> bool:
    """The parts of "offerable" the learner decides (design §4.7): the run's end is not a commit, the rung is A (or a
    verified B: decided at offer time), and the capability's offer class allows it (``judged``: only when the naming
    call said everyday). Trusted (D14) and an active routine are the routine lane's (phase L lWire)."""
    if compiled.commit != "none" or compiled.rung not in ("A-link", "A-capability", "B"):
        return False
    cls = str(cap.offer_class or "never")
    return cls in ("logistics", "everyday") or (cls == "judged" and everyday is True)


def _existing(store: Any) -> dict[str, dict[str, Any]]:
    try:
        rows = store._query("SELECT id, key, state, deleted_at, spec_ciphertext, name_ciphertext, provenance, uses,"
                            " last_used_at, updated_at, retired_at FROM skills WHERE origin = 'learned'")
    except Exception:  # noqa: BLE001 - a database before migration 43
        return {}
    out = {}
    for r in rows:
        d = dict(r)
        try:
            d["prov"] = json.loads(d.get("provenance") or "{}")
        except (TypeError, ValueError):
            d["prov"] = {}
        out[str(d["key"])] = d
    return out


def plan(store: Any, groups: Sequence[Group], caps: dict[str, Any], compiled: dict[str, Any], now: float
         ) -> tuple[list[Planned], Counter]:
    """The rows the passing capability-backed groups make, ranked and capped (no write)."""
    from yuki.skills import registry
    from yuki.skills.catalog import specialise
    from yuki.skills.connection import MAX_SKILLS, ConnectionError_

    counts: Counter = Counter()
    existing = _existing(store)
    out: list[Planned] = []
    for g in groups:
        cap = caps.get(g.capability)
        if cap is None or not g.bar.passes:
            continue
        key = skill_key(store.cipher, cap.key, g.keys)
        old = existing.get(key)
        if old is not None and (old.get("deleted_at") is not None or old.get("state") in TOMBSTONES):
            counts["tombstoned"] += 1          # Forget this / Not mine: never proposed again
            continue
        p = Planned(group=g, cap=cap, compiled=compiled[g.gid], key=key, row_id=int(old["id"]) if old else None)
        bound = bound_values(g, cap)
        p.spec = {"v": 1, "bound": bound, "menu": menu_line(cap, bound), "routes": list(p.compiled.routes)}
        p.provenance = provenance(g, cap, p.compiled, store, now)
        if old is not None:
            for k in ("name_sig", "named_by", "everyday"):
                if k in old["prov"]:
                    p.provenance[k] = old["prov"][k]
            try:
                p.name = " ".join(str(store._dec(old["name_ciphertext"]) or "").split()) if old.get(
                    "name_ciphertext") else ""
            except Exception:  # noqa: BLE001
                p.name = ""
            if p.name:
                p.spec["menu"] = _old_menu(store, old) or ""
        try:
            specialise(cap, key=key, spec=p.spec, name=p.name)
        except ConnectionError_ as exc:
            counts["refused_by_catalog"] += 1
            p.error = str(exc).rpartition(": ")[2][:60]
            continue
        verified = 0
        try:
            verified = sum(t.ok for t in registry.trust(store, key, now=now).values())
        except Exception:  # noqa: BLE001
            verified = 0
        uses = int(old.get("uses") or 0) if old else 0
        p.score = round(recency_score(g.occasions, now) + uses + verified, 3)
        p.provenance["score"] = p.score
        p.provenance["verified_runs"] = verified
        out.append(p)
    out.sort(key=lambda p: (-p.score, p.key))
    for i, p in enumerate(out):
        if i >= MAX_SKILLS:
            p.state = LISTED
            counts["listed"] += 1
            continue
        try:
            trusted = registry.offer_ready(store, p.key, kind=p.cap.kind, now=now)
        except Exception:  # noqa: BLE001
            trusted = False
        p.state = "trusted" if trusted else "candidate"
        counts[p.state] += 1
    return out, counts


def _old_menu(store: Any, old: dict[str, Any]) -> str:
    try:
        spec = json.loads(store._dec(old["spec_ciphertext"]) or "{}") if old.get("spec_ciphertext") else {}
    except Exception:  # noqa: BLE001
        return ""
    return str(spec.get("menu") or "") if isinstance(spec, dict) else ""


def name_planned(store: Any, planned: list[Planned], *, worker: Any = None,
                 log: Callable[..., Any] | None = None) -> int:
    """Names for the new or changed groups (one batched call; design §4.6). Returns how many got a name."""
    todo: list[tuple[Planned, dict[str, Any]]] = []
    for p in planned:
        sig = name_signature(p)
        if p.provenance.get("named_by") == "user":
            continue
        if p.name and p.provenance.get("name_sig") == sig:
            continue
        todo.append((p, naming_input(p, store)))
    answers = _call_naming(store, [i for _p, i in todo], worker=worker, log=log)
    named = 0
    for p, inp in todo:
        got = answers.get(inp["signature"])
        nm, menu, use_when, everyday = _valid_name(got, p.spec.get("bound") or {})
        if not nm:
            continue
        p.name, p.use_when = nm, use_when
        p.spec["menu"] = menu
        p.provenance.update(name_sig=inp["signature"], named_by="model", everyday=everyday)
        named += 1
    for p in planned:
        p.provenance["offer_static"] = offer_static(p.cap, p.compiled, p.provenance.get("everyday"))
    return named


def write(store: Any, planned: list[Planned], *, now: float, cap_ids: dict[str, int]) -> Counter:
    """Upsert the planned rows and move the learned rows that no longer pass, in one transaction; sets each group's
    ``skill_id``. Returns counts (content-free)."""
    counts: Counter = Counter()
    existing = _existing(store)
    keep = {p.key for p in planned}

    def op(conn: Any) -> None:
        for p in planned:
            cid = cap_ids.get(p.cap.key)
            if cid is None:
                counts["no_capability_row"] += 1
                continue
            spec_json = json.dumps(p.spec, sort_keys=True, ensure_ascii=False, default=str)
            prov_json = json.dumps(p.provenance, sort_keys=True, separators=(",", ":"), default=str)
            name_ct = store.cipher.encrypt(p.name) if p.name else None
            use_ct = store.cipher.encrypt(json.dumps(p.use_when, ensure_ascii=False)) if p.use_when else None
            old = existing.get(p.key)
            conn_text = "learned#" + hashlib.sha256(spec_json.encode("utf-8")).hexdigest()[:16]
            if old is None:
                cur = conn.execute(
                    "INSERT INTO skills (key, app, end_kind, connection, origin, state, capability_id, spec_ciphertext,"
                    " name_ciphertext, use_when_ciphertext, provenance, created_at, updated_at) VALUES"
                    " (?, ?, ?, ?, 'learned', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (p.key, p.cap.app, p.cap.end, conn_text, p.state, cid, store.cipher.encrypt(spec_json), name_ct,
                     use_ct, prov_json, now, now))
                p.row_id = int(cur.lastrowid)
                counts["new"] += 1
            else:
                p.row_id = int(old["id"])
                try:
                    old_spec = store._dec(old["spec_ciphertext"]) if old.get("spec_ciphertext") else ""
                    old_name = store._dec(old["name_ciphertext"]) if old.get("name_ciphertext") else ""
                except Exception:  # noqa: BLE001
                    old_spec, old_name = "", ""
                changed = (old_spec != spec_json or (old_name or "") != (p.name or "") or old.get("state") != p.state
                           or old.get("retired_at") is not None)
                if changed:
                    conn.execute(
                        "UPDATE skills SET state = ?, capability_id = ?, spec_ciphertext = ?, name_ciphertext = ?,"
                        " use_when_ciphertext = coalesce(?, use_when_ciphertext), provenance = ?, connection = ?,"
                        " retired_at = NULL, updated_at = ? WHERE id = ?",
                        (p.state, cid, store.cipher.encrypt(spec_json), name_ct, use_ct, prov_json, conn_text, now,
                         p.row_id))
                    counts["updated"] += 1
                else:
                    # provenance (counts, days) only: the catalog's version is not moved (updated_at kept)
                    conn.execute("UPDATE skills SET provenance = ? WHERE id = ?", (prov_json, p.row_id))
                    counts["unchanged"] += 1
            p.group.skill_id = p.row_id
        for key, old in existing.items():
            if key in keep or old.get("deleted_at") is not None or old.get("state") in TOMBSTONES:
                continue
            last = max(float(old["prov"].get("last_at") or 0.0), float(old.get("last_used_at") or 0.0))
            idle_days = (now - last) / DAY_S if last else RETIRE_DAYS + 1
            state = "retired" if idle_days >= RETIRE_DAYS else "dormant"
            if old.get("state") == state:
                continue
            conn.execute("UPDATE skills SET state = ?, retired_at = CASE WHEN ? = 'retired' THEN ? ELSE retired_at END,"
                         " updated_at = ? WHERE id = ?", (state, state, now, now, int(old["id"])))
            counts[state] += 1

    store._write(op)
    return counts


def capability_ids(store: Any) -> dict[str, int]:
    """``{capability key: skills.id}`` of the live capability rows (seeded from the files first: idempotent)."""
    from yuki.skills import registry
    from yuki.skills.catalog import capabilities

    try:
        registry.seed(store, capabilities())
    except Exception:  # noqa: BLE001 - the rows that exist still serve
        pass
    try:
        return {str(k): int(i) for i, k in store._query("SELECT id, key FROM skills WHERE origin = 'capability'"
                                                         " AND retired_at IS NULL")}
    except Exception:  # noqa: BLE001
        return {}


__all__ = ["DORMANT_DAYS", "RETIRE_DAYS", "TOMBSTONES", "LISTED", "NAME_BATCH", "Planned", "WriteResult",
           "bound_values", "menu_line", "provenance", "name_signature", "naming_input", "offer_static", "plan",
           "name_planned", "write", "capability_ids"]
