"""Grouping occasions into habits (phase L design §4.2; critique H2), and the route-token helpers ported from
``yuki.memory.trace_families`` (design §7: ``step_token`` and the LCS overlap move here before phase P deletes that
module).

* **The key** of a group is ``(capability, end kind, key-slot values)``. The app, the means (the browser, the chat app,
  the control) and the fill values (a person, a video, a level) are attributes, never part of a skill's identity.
* **Split**: a key value splits off its own group only when its own occasions pass the candidate bar
  (:func:`passes`); the rest stay the capability's slot (``seen``, in the report only). An occasion missing a required
  key value joins the one passing group of its capability with the same means; with two or more, it counts for none.
* **Usage records**: a capability with no key slot gives one group for all its occasions (the messages, the voice
  joins, the volume changes): it adds no menu line; it exists for the Playbook, provenance, the means preference,
  routines on a ``routine_key`` fill slot, and trust.
* **The bar** (design §4.7): at least :data:`MIN_OCCASIONS` user occasions on :data:`MIN_DAYS` distinct days in the
  last :data:`BAR_DAYS`, the last within :data:`RECENT_DAYS`; or :data:`MIN_ASKED` asked occasions on as many days.
* **Skill keys** (:func:`skill_key`) are derived by a versioned function of the capability and the keyed digests of the
  bound key values (critique L3): a means change never moves the key, and a rule bump never rewrites a live key (the
  writer matches a row by every key version it ever had, :data:`KEY_VERSION`).

No app key, site, page kind or end-state string is written here: the capability data (``serves``, ``bind``, slot
roles) and the occasions say everything.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The candidate bar (design §4.7; D21). Starting values, tuned from the user's Playbook review (design §11.4).
MIN_OCCASIONS = 3
MIN_DAYS = 3
BAR_DAYS = 28.0
RECENT_DAYS = 14.0
MIN_ASKED = 2
#: A skill key's derivation version (critique L3). Bump only with the old derivation kept in :func:`skill_key`.
KEY_VERSION = 1
#: The share of occasions one value must hold to count as the group's own value in its provenance (design §4.2's 80%).
BOUND_SHARE = 0.8
DAY_S = 86400.0

# ---------------------------------------------------------------------------------------------------------------------
# Route tokens (ported from yuki.memory.trace_families: step_token, lcs_pairs, lcs_ratio, build_path)
# ---------------------------------------------------------------------------------------------------------------------

#: The kinds of action that are a step of a route (pointer and field actions on an element).
ELEMENT_STEP_KINDS = frozenset({"click", "dclick", "rclick", "menu", "select", "invoke", "drag", "type", "paste"})
#: The identities a step may rest on (the trace's ``resolved``; never ``stale`` or ``unresolved``).
IDENTITY_RESOLVED = frozenset({"point", "event", "snapshot", "focus"})
#: Steps of one route compared at most.
MAX_KEYS = 80


def step_token(row: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """One trace action as a route step: ``(token, attrs)``. The token is an element key, or a machine form
    (``app|keys|<chord>``, ``app|switch``, ``app|file_save|.ext``, ``app|command``, ``app|copy``); ``attrs`` hold the
    kind, role, position and app for wording. Scrolls, hovers and page arrivals are no step. (Ported unchanged from
    ``trace_families.step_token``.)"""
    kind, proc = row.get("kind") or "", str(row.get("process") or "")
    attrs = {"kind": kind, "app": proc}
    if kind == "keys" and row.get("chord"):
        return f"{proc}|keys|{row['chord']}", attrs
    if kind in ELEMENT_STEP_KINDS and row.get("element_key") and row.get("resolved") in IDENTITY_RESOLVED:
        return str(row["element_key"]), {**attrs, "role": row.get("role") or "", "pos": row.get("position") or ""}
    if kind == "switch":
        return f"{proc}|switch", attrs
    if kind in ("file_save", "file_open"):
        return f"{proc}|{kind}|{row.get('file_ext') or ''}", {**attrs, "ext": row.get("file_ext") or ""}
    if kind in ("command", "copy"):
        return f"{proc}|{kind}", attrs
    return None


def lcs_pairs(a: Sequence[str], b: Sequence[str]) -> list[tuple[int, int]]:
    """The index pairs of a longest common subsequence of two token lists."""
    n, m = len(a), len(b)
    if not n or not m:
        return []
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        ai, row, nxt = a[i], dp[i], dp[i + 1]
        for j in range(m - 1, -1, -1):
            row[j] = nxt[j + 1] + 1 if ai == b[j] else max(nxt[j], row[j + 1])
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        if a[i] == b[j]:
            pairs.append((i, j))
            i, j = i + 1, j + 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def lcs_ratio(a: Sequence[str], b: Sequence[str]) -> float:
    """The longest common subsequence's length over the shorter route's (0 when either is empty)."""
    if not a or not b:
        return 0.0
    return len(lcs_pairs(a, b)) / min(len(a), len(b))


def common_path(routes: Sequence[Sequence[str]]) -> list[str]:
    """The tokens every route shares in order (the LCS folded over the routes, most typical route first): the
    constant identity steps a recorded path is built from (design §4.4 rung B). ``[]`` for fewer than two routes."""
    routes = [list(r)[:MAX_KEYS] for r in routes if r]
    if len(routes) < 2:
        return []
    scores = [sum(len(lcs_pairs(routes[i], routes[j])) for j in range(len(routes)) if j != i)
              for i in range(len(routes))]
    best = max(range(len(routes)), key=lambda i: (scores[i], -len(routes[i]), -i))
    path = list(routes[best])
    for k, other in enumerate(routes):
        if k == best:
            continue
        path = [path[i] for i, _j in lcs_pairs(path, other)]
        if not path:
            return []
    return path


# ---------------------------------------------------------------------------------------------------------------------
# Days and the bar
# ---------------------------------------------------------------------------------------------------------------------


def day_key(at: float) -> str:
    """The 04:00-based day an occasion belongs to (the routine miner's day: a 01:00 send counts for the evening
    before), as ``YYYY-MM-DD``."""
    from yuki.memory.routines import day_of

    return day_of(float(at)).isoformat()


@dataclass
class Bar:
    """What the candidate bar saw of a set of occasions (content-free numbers)."""

    n: int = 0
    days: int = 0
    asked: int = 0
    asked_days: int = 0
    last_at: float | None = None
    first_at: float | None = None
    passes: bool = False
    why: str = ""


def bar(occasions: Iterable[Any], now: float) -> Bar:
    """The candidate bar (design §4.7) over ``occasions`` (each with ``at``, ``by`` and ``counts``): user occasions
    that count (never Yuki's, a late send counts for demand) in the last :data:`BAR_DAYS`, or asked occasions."""
    lo = now - BAR_DAYS * DAY_S
    user = [o for o in occasions if o.by == "user" and o.counts and lo <= o.at <= now]
    asked = [o for o in occasions if o.by == "asked" and o.counts and lo <= o.at <= now]
    out = Bar(n=len(user), days=len({day_key(o.at) for o in user}), asked=len(asked),
              asked_days=len({day_key(o.at) for o in asked}))
    every = user + asked
    if every:
        out.last_at = max(o.at for o in every)
        out.first_at = min(o.at for o in every)
    recent = out.last_at is not None and now - out.last_at <= RECENT_DAYS * DAY_S
    if out.n >= MIN_OCCASIONS and out.days >= MIN_DAYS and recent:
        out.passes, out.why = True, "seen"
    elif out.asked >= MIN_ASKED and out.asked_days >= MIN_ASKED and recent:
        out.passes, out.why = True, "asked"
    else:
        out.why = "few" if out.n < MIN_OCCASIONS else ("days" if out.days < MIN_DAYS else "stale")
    return out


def passes(occasions: Iterable[Any], now: float) -> bool:
    return bar(occasions, now).passes


# ---------------------------------------------------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(eq=False)
class Group:
    """One habit: a capability, the end kind its occasions end on, and its key-slot values (digests for identity, the
    values themselves in RAM only), with the occasions in it and what the bar saw."""

    capability: str
    end: str
    keys: tuple[tuple[str, str], ...]                  # ((slot, value digest), ...), sorted by slot
    values: dict[str, Any] = field(default_factory=dict)   # slot -> value (RAM only; encrypted when written)
    occasions: list[Any] = field(default_factory=list)
    usage: bool = False                                # binds no key value (a usage record)
    bar: Bar = field(default_factory=Bar)
    merged_from: list[str] = field(default_factory=list)
    skill_id: int | None = None                        # the learned row it was written to (the writer sets it)

    @property
    def gid(self) -> str:
        """A content-free id for logs and the report (12 hex)."""
        raw = json.dumps([self.capability, self.end, list(self.keys)], separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def shares(self, attr: str) -> Counter:
        """How often each value of one attribute (``app``, a means value) shows on the group's user occasions."""
        return Counter(str(v) for v in (o.attrs.get(attr) for o in self.occasions if o.by == "user") if v)

    def top_share(self, attr: str) -> tuple[str | None, float]:
        counts = self.shares(attr)
        total = sum(counts.values())
        if not total:
            return None, 0.0
        value, n = counts.most_common(1)[0]
        return value, n / total


def skill_key(cipher: Any, capability: str, keys: Sequence[tuple[str, str]], *, version: int = KEY_VERSION) -> str:
    """A learned skill's key: the capability and a keyed digest of its bound key values (critique L3). A usage record
    (no key value) is ``<capability>#all``. Versioned: :data:`KEY_VERSION` 1 is the only derivation so far."""
    if version != 1:
        raise ValueError(f"no skill key derivation {version}")
    if not keys:
        return f"{capability}#all"
    blob = json.dumps(sorted([str(s), str(d)] for s, d in keys), separators=(",", ":"))
    return f"{capability}#{str(cipher.digest('learn-skill-key', 'v1', capability, blob))[:16]}"


def value_digest(cipher: Any, slot: str, value: Any) -> str:
    """A key value's keyed digest (case-folded, spaced once): equal values group together; never the value itself."""
    text = " ".join(str(value if value is not None else "").split()).casefold()
    return str(cipher.digest("learn-key", str(slot), text))[:16]


def build(occasions: Sequence[Any], caps: dict[str, Any], cipher: Any, now: float) -> tuple[list[Group], list[Group]]:
    """``(groups, seen)``: the occasions of each capability grouped by its key-slot values with the split rule; the
    groups whose key values did not pass alone are ``seen`` (report only). Occasions bound to no capability are grouped
    by ``(none, end kind, app)``: route C only, never written (no done-check), listed in the report (design §4.1 A3).
    Each occasion's ``group`` is set to the group it counts for (``None``: none)."""
    by_cap: dict[str, list[Any]] = defaultdict(list)
    for o in occasions:
        o.group = None
        by_cap[o.capability].append(o)
    groups: list[Group] = []
    seen: list[Group] = []
    for cap_key, items in sorted(by_cap.items()):
        cap = caps.get(cap_key)
        if cap is None:
            # capability "none": one group per (end kind, app); route C only
            per: dict[tuple[str, str], list[Any]] = defaultdict(list)
            for o in items:
                per[(o.kind, str(o.attrs.get("app") or ""))].append(o)
            for (end, app), its in sorted(per.items()):
                g = Group(capability=cap_key, end=end, keys=(("app", app),), values={"app": app}, occasions=its)
                g.bar = bar(its, now)
                if g.bar.passes:
                    for o in its:
                        o.group = g
                (groups if g.bar.passes else seen).append(g)
            continue
        key_slots = [s for s in cap.slots if s.role == "key" and s.name in cap.bind]
        end = Counter(o.kind for o in items).most_common(1)[0][0]
        if not key_slots:
            g = Group(capability=cap_key, end=end, keys=(), occasions=list(items), usage=True)
            g.bar = bar(items, now)
            if g.bar.passes:
                for o in items:
                    o.group = g
            (groups if g.bar.passes else seen).append(g)
            continue
        complete: dict[tuple[tuple[str, str], ...], list[Any]] = defaultdict(list)
        values: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}
        missing: list[Any] = []
        for o in items:
            parts: list[tuple[str, str]] = []
            vals: dict[str, Any] = {}
            lost = False
            for s in key_slots:
                v = o.attrs.get(s.name)
                if v in (None, ""):
                    if s.required:
                        lost = True
                        break
                    continue      # an optional key slot with no value (a site's front page): a value of its own
                parts.append((s.name, value_digest(cipher, s.name, v)))
                vals[s.name] = v
            if lost:
                missing.append(o)
                continue
            k = tuple(sorted(parts))
            complete[k].append(o)
            values.setdefault(k, vals)
        passing: list[Group] = []
        for k, its in sorted(complete.items()):
            g = Group(capability=cap_key, end=end, keys=k, values=dict(values[k]), occasions=list(its))
            g.bar = bar(its, now)
            (passing if g.bar.passes else seen).append(g)
        for o in missing:
            # a required key value missing (a hold with no service): the one passing group with the same means, else
            # none (two or more: never guessed)
            means = _means_of(o, cap)
            fits = [g for g in passing if means is None or means in {_means_of(x, cap) for x in g.occasions}]
            if len(fits) == 1:
                fits[0].occasions.append(o)
        for g in passing:
            g.bar = bar(g.occasions, now)
            for o in g.occasions:
                o.group = g
        groups.extend(passing)
    return groups, seen


def _means_of(o: Any, cap: Any) -> str | None:
    """The occasion's value of the capability's first means slot that binds from it (the browser, the chat app)."""
    for s in cap.slots:
        if s.role == "means" and s.name in cap.bind:
            v = o.attrs.get(s.name)
            return str(v) if v else None
    return None


def recency_score(occasions: Iterable[Any], now: float, *, half_life_days: float = RECENT_DAYS) -> float:
    """Recency-weighted distinct days (design §4.9's cap ranking): each distinct day weighs ``0.5 ** (age / 14 d)``."""
    days: dict[str, float] = {}
    for o in occasions:
        if not o.counts:
            continue
        d = day_key(o.at)
        days[d] = max(days.get(d, 0.0), o.at)
    return sum(math.pow(0.5, max(0.0, now - at) / DAY_S / half_life_days) for at in days.values())


__all__ = ["MIN_OCCASIONS", "MIN_DAYS", "BAR_DAYS", "RECENT_DAYS", "MIN_ASKED", "KEY_VERSION", "BOUND_SHARE",
           "ELEMENT_STEP_KINDS", "IDENTITY_RESOLVED", "step_token", "lcs_pairs", "lcs_ratio", "common_path", "day_key",
           "Bar", "bar", "passes", "Group", "skill_key", "value_digest", "build", "recency_score"]
