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
* **Names** come from lName's batched cheap call (``yuki.skills.learn.name.name_groups``: at most one call a night,
  the rest from its signature cache), fed by :func:`name_input` (RAM only: what may be sent, and the values that must
  never come back); a name the user gave (``provenance.named_by = "user"``) is never asked for or replaced. A nameless
  skill carries lName's code-built label and ``provenance.named = false``: it runs on request (its menu line is
  code-built); which offer classes need a name is the offer gate's call (lName's merge note 5).
"""

from __future__ import annotations

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
#: The naming facts carried over from a row's last pass (the user's rename, the model's name and its flags).
NAME_PROVENANCE: tuple[str, ...] = ("named_by", "named", "everyday", "name_source", "name_why")
#: The naming fields of a spec carried over with an earlier name (``name.Named.spec_fields``).
NAME_SPEC_FIELDS: tuple[str, ...] = ("menu", "ask", "use_when")
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


def _app_names(store: Any, keys: Sequence[str]) -> dict[str, str]:
    """``{app key: display name}`` (the timeline's names; the key itself when there is none)."""
    out: dict[str, str] = {}
    for key in keys:
        if not key or key in out:
            continue
        try:
            out[key] = str(store.app_display_name(key) or key)
        except Exception:  # noqa: BLE001
            out[key] = key
    return out


def _contact_names(store: Any, ids: Sequence[Any]) -> list[str]:
    """The names and aliases of these contacts (a ``contact`` bind source holds the people index's id), decrypted in
    RAM for the naming call's withheld check only: the whole name and each of its words."""
    wanted = sorted({int(i) for i in ids if isinstance(i, int) or str(i).isdigit()})
    if not wanted:
        return []
    out: list[str] = []
    for start in range(0, len(wanted), 200):
        part = wanted[start:start + 200]
        try:
            rows = store._query(f"SELECT name_ciphertext, aliases_ciphertext FROM contacts WHERE id IN"
                                f" ({','.join('?' * len(part))})", tuple(part))
        except Exception:  # noqa: BLE001 - no people index: nothing to add
            return out
        for name_ct, aliases_ct in rows:
            names: list[str] = []
            try:
                names.append(str(store._dec(name_ct) or ""))
                raw = store._dec(aliases_ct) if aliases_ct else ""
                got = json.loads(raw) if raw else []
                names += [str(a) for a in got if a] if isinstance(got, list) else [str(raw)]
            except Exception:  # noqa: BLE001
                pass
            for n in names:
                n = " ".join(n.split())
                if n:
                    out.append(n)
                    out += [w for w in n.split() if len(w) >= 2]
    return out


def _run_words(store: Any, run_keys: Sequence[str]) -> dict[str, str]:
    """The user's own request words of these runs (the run head's encrypted words), decrypted in RAM."""
    keys = sorted({str(k) for k in run_keys if k})
    out: dict[str, str] = {}
    for start in range(0, len(keys), 200):
        part = keys[start:start + 200]
        try:
            rows = store._query(f"SELECT run_key, input_ciphertext FROM runs WHERE step = 0 AND run_key IN"
                                f" ({','.join('?' * len(part))})", tuple(part))
        except Exception:  # noqa: BLE001
            return out
        for key, ct in rows:
            try:
                words = " ".join(str(store._dec(ct) or "").split()) if ct else ""
            except Exception:  # noqa: BLE001
                words = ""
            if words:
                out[str(key)] = words
    return out


def name_input(store: Any, p: Planned, naming: Any) -> Any:
    """One planned group as the naming call's input (``yuki.skills.learn.name.NameInput``; design §4.6, Q13; lName's
    merge note 2), built in RAM only:

    * ``bound`` (the key values; ``name.py`` sends only those its capability reads from an app, a service, a site cut
      to its registrable domain, or a page kind), ``means`` (the user's own means counts), ``apps`` (display names),
      the kept control names of a rung-B path, the user occasions' times and how often;
    * ``withhold``: every value the group's occasions carried in a slot read from a source the call never sends (an
      entity, a value, a contact: playlists, videos, levels, people, the people index's ids resolved to their names
      and aliases), decrypted in RAM;
    * ``phrasings``: the user's own words of the asked runs (newest first; a corrected run's never) and of a request he
      then did himself (``facts.after_ask``). When a slot that names a person or a thing still varies, only an asked
      run that carried a value for every such slot gives its words (those values are withheld with the rest), so a
      name in the words is always one the check knows."""
    g, cap, compiled = p.group, p.cap, p.compiled
    bind = dict(cap.bind or {})
    bound = dict(p.spec.get("bound") or {})
    sent_sources = tuple(getattr(naming, "SENT_SOURCES", ()) or ())
    naming_types = tuple(getattr(naming, "NAMING_TYPES", ()) or ())
    hide = {s.name: bind[s.name] for s in cap.slots if s.name in bind and bind[s.name] not in sent_sources}
    varies_naming = [s for s in cap.slots if s.role in ("key", "fill") and s.name not in bound and not s.told
                     and s.type != "bool" and s.type in naming_types]
    withhold: list[str] = []
    contacts: list[Any] = []

    def keep(slot: str, value: Any) -> None:
        if value in (None, "") or isinstance(value, bool):
            return
        if hide.get(slot) == "contact":
            contacts.append(value)
        else:
            withhold.append(" ".join(str(value).split()))

    for o in sorted(g.occasions, key=lambda o: -o.at):
        for slot in hide:
            keep(slot, (o.attrs or {}).get(slot))
    for slot, value in bound.items():
        if slot in hide:
            keep(slot, value)
    withhold += _contact_names(store, contacts)
    withhold = list(dict.fromkeys(v for v in withhold if v))[:400]
    # the user's own words: asked runs (never a corrected one), and a request he then did himself
    sources: list[tuple[float, str, Any]] = []
    for o in g.occasions:
        if o.by == "asked" and o.asked_run and not o.facts.get("corrected"):
            sources.append((o.at, str(o.asked_run), o))
        elif o.by == "user" and o.facts.get("after_ask"):
            sources.append((o.at, str(o.facts["after_ask"]), o))
    usable: list[str] = []
    for _at, key, o in sorted(sources, key=lambda x: -x[0]):
        if varies_naming:
            vals = [(o.attrs or {}).get(s.name) for s in varies_naming]
            if o.by != "asked" or any(v in (None, "") for v in vals):
                continue
            withhold += [" ".join(str(v).split()) for v in vals]
        if key not in usable:
            usable.append(key)
        if len(usable) >= 6:
            break
    words = _run_words(store, usable)
    phrasings = [words[k] for k in usable if words.get(k)]
    means = {str(slot): {str(v): int(n) for v, n in counts.items()} for slot, counts in (compiled.means or {}).items()}
    app_keys = [str(v) for s in cap.slots if s.type == "app" for v in (means.get(s.name) or {})]
    app_keys += [str(v) for slot, v in bound.items() if bind.get(slot) == "app" and v]
    app_keys += [str(next((o.app for o in g.occasions if o.app), "") or "")]
    steps: list[str] = []
    for route in compiled.routes or ():
        for step in ((route.get("args") or {}).get("steps") or []) if isinstance(route, dict) else ():
            if isinstance(step, dict) and step.get("do") in ("click", "open_app") and isinstance(step.get("name"), str):
                steps.append(step["name"])
    times = [o.at for o in g.occasions if o.by == "user" and o.counts and o.context_known]
    return naming.NameInput(
        key=p.key, capability=cap.key, end=g.end, bound=bound, means=means, apps=_app_names(store, app_keys),
        steps=tuple(steps), times=tuple(times), n=int(g.bar.n or 0), days=int(g.bar.days or 0),
        phrasings=tuple(phrasings), withhold=tuple(dict.fromkeys(withhold)))


def offer_static(cap: Any, compiled: Any, everyday: bool | None, *, usage: bool = False) -> bool:
    """The parts of "offerable" the learner decides (design §4.7): the run's end is not a commit, the rung is A (or a
    verified B: decided at offer time), and the capability's offer class allows it (``judged``: only when the naming
    call said everyday). A usage record (no key value bound) is offerable only through a ``routine_key`` fill slot (a
    playlist: §4.2), never as itself (review r1 L1: "volume" with nothing bound). Trusted (D14) and an active routine
    are the routine lane's (phase L lWire)."""
    if compiled.commit != "none" or compiled.rung not in ("A-link", "A-capability", "B"):
        return False
    if usage and not any(s.role == "fill" and s.routine_key for s in cap.slots):
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
    # phase L lWire: the fill-policy scorer (yuki.skills.prefs, design §5) gives each skill's program slot its policy,
    # the catalog's spec "means" (lCore's report: "the fill-policy scorer ... is lWire's")
    try:
        from yuki.skills.prefs import FillPolicyScorer

        scorer: Any = FillPolicyScorer(store, now)
    except Exception:  # noqa: BLE001 - no policy this night: "most used" at run time
        scorer = None
    for g in groups:
        cap = caps.get(g.capability)
        if cap is None:
            continue
        key = skill_key(store.cipher, cap.key, g.keys)
        old = existing.get(key)
        if old is not None and (old.get("deleted_at") is not None or old.get("state") in TOMBSTONES):
            # Forget this / Not mine: never proposed again. Its occasions keep the tombstone's id (nothing is written to
            # the row), so the preference feed and the routine miner can leave them out (lPage's tombstone contract;
            # lWire review r1 M1)
            g.skill_id = int(old["id"])
            counts["tombstoned"] += 1
            continue
        if not g.bar.passes:
            continue
        p = Planned(group=g, cap=cap, compiled=compiled[g.gid], key=key, row_id=int(old["id"]) if old else None)
        bound = bound_values(g, cap)
        p.spec = {"v": 1, "bound": bound, "menu": menu_line(cap, bound), "routes": list(p.compiled.routes)}
        p.provenance = provenance(g, cap, p.compiled, store, now)
        if scorer is not None:
            try:
                means, scored = scorer.score(g, cap)
            except Exception:  # noqa: BLE001
                means, scored = {}, {}
            if means:
                p.spec["means"] = means
                p.provenance["fill_policy"] = scored
        if old is not None:
            for k in NAME_PROVENANCE:
                if k in old["prov"]:
                    p.provenance[k] = old["prov"][k]
            try:
                p.name = " ".join(str(store._dec(old["name_ciphertext"]) or "").split()) if old.get(
                    "name_ciphertext") else ""
            except Exception:  # noqa: BLE001
                p.name = ""
            if p.name and (old["prov"].get("named") is True or old["prov"].get("named_by") == "user"):
                # a real name (the model's or the user's) keeps its menu line, questions and phrasings until replaced
                old_spec = _old_spec(store, old)
                for k in NAME_SPEC_FIELDS:
                    if old_spec.get(k):
                        p.spec[k] = old_spec[k]
        try:
            specialise(cap, key=key, spec=p.spec, name=p.name)
        except ConnectionError_ as exc:
            counts["refused_by_catalog"] += 1
            p.error = str(exc).rpartition(": ")[2][:60]
            continue
        verified = 0
        try:
            # lBrain: a live test's runs never raise a learned skill's score (registry.trust ``real_only``)
            verified = sum(t.ok for t in registry.trust(store, key, now=now, real_only=True).values())
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


def _old_spec(store: Any, old: dict[str, Any]) -> dict[str, Any]:
    try:
        spec = json.loads(store._dec(old["spec_ciphertext"]) or "{}") if old.get("spec_ciphertext") else {}
    except Exception:  # noqa: BLE001
        return {}
    return spec if isinstance(spec, dict) else {}


def name_planned(store: Any, planned: list[Planned], *, settings: Any = None, client: Any = None,
                 log: Callable[..., Any] | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Names for the planned groups (design §4.6; lName's ``name.name_groups``: one batched call a night at most, the
    rest from its signature cache), best first; a name the user gave (``provenance.named_by = "user"``) is never
    asked for or replaced. Applied to each plan: ``Named.label`` as the name, ``Named.spec_fields()`` into the spec
    (``menu``, ``ask``, ``use_when``) and ``Named.provenance()`` into the provenance (``named``, ``everyday``). A
    nameless answer keeps an earlier model name (a night with no call budget, a failed call) until a new name comes.
    ``dry_run``: no call and no write (lName's own dry run), plus ``plan`` (``name.plan``: what a real night would
    ask). The naming call logs to its own memory file (``learn-YYYYMMDD.jsonl``: the cost view counts it there);
    ``log`` gets this step's errors. Returns content-free counts (``NamingRun.as_fields()`` + ``named``)."""
    try:
        from yuki.skills.learn import name as naming
    except ImportError:
        naming = None
    out: dict[str, Any] = {"named": 0}
    todo = [p for p in planned if p.provenance.get("named_by") != "user"]
    if naming is None or not hasattr(naming, "name_groups") or not hasattr(naming, "NameInput"):
        out["error"] = "no_naming_module"
    elif todo:
        inputs = []
        for p in todo:
            try:
                inputs.append(name_input(store, p, naming))
            except Exception as exc:  # noqa: BLE001 - that group stays as it was this night
                out["input_errors"] = int(out.get("input_errors", 0)) + 1
                if log is not None:
                    try:
                        log("error", where="learn.name_input", error=type(exc).__name__)
                    except Exception:  # noqa: BLE001
                        pass
        # content-free sizes of what the call reads (the values themselves stay in RAM)
        out["inputs"] = {"groups": len(inputs), "withheld": sum(len(i.withhold) for i in inputs),
                         "phrasings": sum(len(i.phrasings) for i in inputs),
                         "with_phrasings": sum(1 for i in inputs if i.phrasings),
                         "steps": sum(len(i.steps) for i in inputs), "timed": sum(1 for i in inputs if i.times)}
        try:
            if dry_run:
                out["plan"] = naming.plan(store, inputs)
            results, run = naming.name_groups(store, inputs, settings=settings, client=client, dry_run=dry_run)
            out.update(run.as_fields())
        except Exception as exc:  # noqa: BLE001 - nameless (or as they were) this night
            results = {}
            out["error"] = type(exc).__name__
            if log is not None:
                try:
                    log("error", where="learn.name", error=type(exc).__name__)
                except Exception:  # noqa: BLE001
                    pass
        for p in todo:
            got = results.get(p.key)
            if got is None:
                continue
            if got.named:
                spec = {**p.spec, **got.spec_fields()}
                try:
                    from yuki.skills.catalog import specialise

                    specialise(p.cap, key=p.key, spec=spec, name=got.label)   # the catalog must still take it
                except Exception:  # noqa: BLE001 - the name is not used; the row stays as it was planned
                    out["refused_names"] = int(out.get("refused_names", 0)) + 1
                    continue
                p.name, p.spec = got.label, spec
                p.use_when = list(got.use_when)
                p.provenance.update(got.provenance())
                p.provenance["named_by"] = "model"
                out["named"] += 1
            elif p.name and p.provenance.get("named") is True and p.provenance.get("named_by") == "model":
                p.provenance["name_kept"] = got.why or None      # the earlier model name stays (its spec fields too)
            else:
                p.name = got.label                                 # the code-built label (the Playbook shows it)
                p.provenance.update(got.provenance())
                p.provenance.pop("named_by", None)
    for p in planned:
        p.provenance["offer_static"] = offer_static(p.cap, p.compiled, p.provenance.get("everyday"),
                                                    usage=p.group.usage)
    return out


def write(store: Any, planned: list[Planned], *, now: float, cap_ids: dict[str, int],
          last_seen: dict[str, float] | None = None) -> Counter:
    """Upsert the planned rows and move the learned rows that no longer pass, in one transaction; sets each group's
    ``skill_id``. Each existing row is read again inside the transaction (review r1 L7): a Forget / Not mine the
    Playbook wrote since :func:`plan` leaves the row alone, and a rename it wrote keeps the user's name. A row that
    stops passing keeps its state while its last occasion or use is under :data:`DORMANT_DAYS` old (design §4.7,
    review r2 L6; ``last_seen``: each key's latest occasion this pass, passing or not), then goes ``dormant``, and
    ``retired`` after :data:`RETIRE_DAYS`. Returns counts (content-free)."""
    last_seen = last_seen or {}
    counts: Counter = Counter()
    existing = _existing(store)
    keep = {p.key for p in planned}

    def op(conn: Any) -> None:
        for p in planned:
            cid = cap_ids.get(p.cap.key)
            if cid is None:
                counts["no_capability_row"] += 1
                continue
            old = existing.get(p.key)
            if old is not None:
                now_row = conn.execute("SELECT state, deleted_at, provenance, name_ciphertext FROM skills WHERE id = ?",
                                       (int(old["id"]),)).fetchone()
                if now_row is None or now_row[1] is not None or now_row[0] in TOMBSTONES:
                    counts["tombstoned_meanwhile"] += 1     # the user's button won: never revived or rewritten
                    continue
                try:
                    now_prov = json.loads(now_row[2] or "{}")
                except (TypeError, ValueError):
                    now_prov = {}
                if isinstance(now_prov, dict) and now_prov.get("named_by") == "user":
                    try:
                        user_name = " ".join(str(store._dec(now_row[3]) or "").split()) if now_row[3] else ""
                    except Exception:  # noqa: BLE001
                        user_name = ""
                    if user_name:
                        p.name = user_name
                        p.provenance["named_by"] = "user"
            spec_json = json.dumps(p.spec, sort_keys=True, ensure_ascii=False, default=str)
            prov_json = json.dumps(p.provenance, sort_keys=True, separators=(",", ":"), default=str)
            name_ct = store.cipher.encrypt(p.name) if p.name else None
            use_ct = store.cipher.encrypt(json.dumps(p.use_when, ensure_ascii=False)) if p.use_when else None
            # a keyed digest (review r1 L4): the spec holds bound values, so its plain hash could confirm a guess
            conn_text = "learned#" + str(store.cipher.digest("learn-spec", spec_json))[:16]
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
                    old_name = store._dec(now_row[3]) if now_row[3] else ""
                except Exception:  # noqa: BLE001
                    old_spec, old_name = "", ""
                changed = (old_spec != spec_json or (old_name or "") != (p.name or "") or now_row[0] != p.state
                           or old.get("retired_at") is not None)
                if changed:
                    conn.execute(
                        "UPDATE skills SET state = ?, capability_id = ?, spec_ciphertext = ?, name_ciphertext = ?,"
                        " use_when_ciphertext = coalesce(?, use_when_ciphertext), provenance = ?, connection = ?,"
                        " retired_at = NULL, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                        (p.state, cid, store.cipher.encrypt(spec_json), name_ct, use_ct, prov_json, conn_text, now,
                         p.row_id))
                    counts["updated"] += 1
                else:
                    # provenance (counts, days) only: the catalog's version is not moved (updated_at kept)
                    conn.execute("UPDATE skills SET provenance = ? WHERE id = ? AND deleted_at IS NULL",
                                 (prov_json, p.row_id))
                    counts["unchanged"] += 1
            p.group.skill_id = p.row_id
        for key, old in existing.items():
            if key in keep or old.get("deleted_at") is not None or old.get("state") in TOMBSTONES:
                continue
            last = max(float(old["prov"].get("last_at") or 0.0), float(old.get("last_used_at") or 0.0),
                       float(last_seen.get(key) or 0.0))
            idle_days = (now - last) / DAY_S if last else RETIRE_DAYS + 1
            if idle_days < DORMANT_DAYS:
                counts["kept_recent"] += 1      # used or seen lately: it keeps its state until 14 days go by
                continue
            state = "retired" if idle_days >= RETIRE_DAYS else "dormant"
            if old.get("state") == state:
                continue
            cur = conn.execute("UPDATE skills SET state = ?, retired_at = CASE WHEN ? = 'retired' THEN ? ELSE retired_at"
                               " END, updated_at = ? WHERE id = ? AND deleted_at IS NULL AND state NOT IN (?, ?)",
                               (state, state, now, now, int(old["id"]), *TOMBSTONES))
            if cur.rowcount:
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


__all__ = ["DORMANT_DAYS", "RETIRE_DAYS", "TOMBSTONES", "LISTED", "NAME_PROVENANCE", "NAME_SPEC_FIELDS", "Planned",
           "WriteResult", "bound_values", "menu_line", "provenance", "name_input", "offer_static", "plan",
           "name_planned", "write", "capability_ids"]
