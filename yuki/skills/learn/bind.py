"""Binding an occasion to the capability that serves it, by data only (phase L design §4.1; critique M3).

Each capability declares what it ``serves`` (an end kind, a target kind and the driver data it needs) and where each
of its slots is read from on an occasion (``bind``: a slot -> one of ``yuki.skills.connection.BIND_SOURCES``). This
module reads those declarations and the occasion's own facts; it holds no app key, site, page kind or end-state
string. An occasion no capability serves is bound to :data:`NONE`: route C only, its commit from the end kind
(``yuki.skills.engine.commit_for``), never written as a skill (no done-check), listed in the report (A3).

The driver data a serve may need (``yuki.skills.connection.SERVE_NEEDS``), each answered from data:

* ``browser``: the end's app is a measured browser (:func:`measured_browsers`: a program whose own timeline rows of
  the last :data:`BROWSER_DAYS` days showed pages of at least :data:`BROWSER_MIN_DOMAINS` registrable domains, at least
  :data:`BROWSER_MIN_REPEAT` of them on :data:`BROWSER_MIN_DAY_COUNT` days or more; critique L6: an app that draws its
  own pages shows one or two domains, a spree of one-off pages in one afternoon is not a browser);
* ``calling_site``: the end is a call join (the moments' D7 rule already judged it a meeting, by a calling site's
  verdict or a seen call) in a browser;
* ``chat``: the end's app, or its chat's host, is among a connection file's ``[chat]`` processes or services
  (``yuki.skills.chat.app_of``);
* ``chat_voice``: that ``[chat]`` table names a ``join_voice`` label;
* any other need ``x``: the connection file of the end's app has a ``[x]`` table (its driver data).
"""

from __future__ import annotations

import threading
import time
import tomllib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

#: The capability key of an occasion no capability serves.
NONE = "none"
#: An occasion's target kind by its end kind (the moments' D1 vocabulary; ``yuki.skills.connection.TARGET_KINDS``).
END_TARGETS: dict[str, str] = {"call_join": "call", "message_sent": "chat", "page": "page",
                               "media_context": "playlist", "volume_change": "level"}
#: A browser, measured (critique L6): at least this many registrable domains over this many days, at least
#: :data:`BROWSER_MIN_REPEAT` of them seen on this many distinct days. (Design section 3.3 words it "each on >= 2 days";
#: on the 10 Oct copy that reading leaves out a program that showed 7 domains, 2 of them on several days, which the
#: design's own measurement counts as a browser; this reading keeps the day bar against one-off sprees.)
BROWSER_DAYS = 28.0
BROWSER_MIN_DOMAINS = 3
BROWSER_MIN_REPEAT = 2
BROWSER_MIN_DAY_COUNT = 2


def measured_browsers(store: Any, now: float, *, days: float = BROWSER_DAYS, min_domains: int = BROWSER_MIN_DOMAINS,
                      min_repeat: int = BROWSER_MIN_REPEAT, min_days: int = BROWSER_MIN_DAY_COUNT) -> dict[str, int]:
    """``{app key: registrable domains it showed}`` for the programs that pass the browser test, from the user's own
    present timeline rows (never Yuki's). Hosts are read in clear (the timeline keeps them so); nothing else. Never
    raises (``{}``)."""
    from yuki.memory.moments import registrable_domain
    from yuki.memory.routines import day_of
    from yuki.memory.store import app_key

    try:
        rows = store._query("SELECT process, host, started_at FROM timeline WHERE host IS NOT NULL AND host != ''"
                            " AND state = 'present' AND by_yuki = 0 AND process IS NOT NULL AND started_at >= ?"
                            " AND started_at <= ?", (float(now) - days * 86400.0, float(now)))
    except Exception:  # noqa: BLE001
        return {}
    seen: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for process, host, started in rows:
        key = app_key(process)
        domain = registrable_domain(host)
        if key and domain:
            seen[key][domain].add(day_of(float(started)))
    out: dict[str, int] = {}
    for key, domains in seen.items():
        repeat = sum(1 for d in domains.values() if len(d) >= min_days)
        if len(domains) >= min_domains and repeat >= min_repeat:
            out[key] = len(domains)
    return out


_tables_lock = threading.Lock()
_tables_cache: tuple[tuple, dict[str, frozenset[str]]] | None = None


def driver_tables(folder: str | Path | None = None) -> dict[str, frozenset[str]]:
    """``{app key: the driver-data tables of its connection file}`` (every top-level table but ``connection`` and
    ``capability``: ``chat``, a player's own table...), read from the connection files (cached until one changes)."""
    global _tables_cache
    from yuki.skills.connection import CONNECTIONS_DIR

    base = Path(folder) if folder is not None else Path(CONNECTIONS_DIR)
    try:
        files = sorted(base.glob("*.toml"))
        stamp = tuple((str(p), p.stat().st_mtime, p.stat().st_size) for p in files)
    except OSError:
        return {}
    with _tables_lock:
        if _tables_cache is not None and _tables_cache[0] == stamp:
            return _tables_cache[1]
    out: dict[str, set[str]] = defaultdict(set)
    for p in files:
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        conn = data.get("connection") if isinstance(data.get("connection"), Mapping) else {}
        app = str(conn.get("app") or "").strip().casefold()
        if not app:
            continue
        out[app] |= {str(k) for k, v in data.items() if isinstance(v, Mapping) and k not in ("connection",
                                                                                               "capability")}
    frozen = {k: frozenset(v) for k, v in out.items()}
    with _tables_lock:
        _tables_cache = (stamp, frozen)
    return frozen


class Index:
    """The capabilities' serves, the browsers measured and the driver data, read once per pass."""

    def __init__(self, caps: Sequence[Any], *, browsers: Mapping[str, int], tables: Mapping[str, frozenset[str]]
                 | None = None) -> None:
        self.caps = [c for c in caps if getattr(c, "origin", "capability") == "capability"]
        self.by_key = {c.key: c for c in self.caps}
        self.browsers = dict(browsers)
        self.tables = dict(tables if tables is not None else driver_tables())
        self._chat_cache: dict[tuple[str, str], str] = {}
        self.load_ms = 0.0

    # -- needs -------------------------------------------------------------------------------------------------------

    def chat_app(self, app: str, host: str) -> str:
        """The connection-file app whose ``[chat]`` data names this program or the chat's host (``""``: none)."""
        k = (app or "", host or "")
        if k not in self._chat_cache:
            try:
                from yuki.skills.chat import app_of

                self._chat_cache[k] = app_of(host or None, [app] if app else []) or ""
            except Exception:  # noqa: BLE001
                self._chat_cache[k] = ""
        return self._chat_cache[k]

    def chat_voice(self, chat_app: str) -> bool:
        if not chat_app:
            return False
        try:
            from yuki.skills.chat import app_data

            labels = app_data(chat_app).get("labels") or {}
        except Exception:  # noqa: BLE001
            return False
        return any(isinstance(t, Mapping) and t.get("join_voice") for t in labels.values()) \
            if isinstance(labels, Mapping) else False

    def need_met(self, need: str, o: Any) -> bool:
        app = str(o.src.get("app") or "")
        if need == "browser":
            return app in self.browsers
        if need == "calling_site":
            return o.kind == "call_join" and app in self.browsers
        if need == "chat":
            return bool(self.chat_app(app, str(o.src.get("host") or "")))
        if need == "chat_voice":
            return self.chat_voice(self.chat_app(app, str(o.src.get("host") or "")))
        return need in self.tables.get(app, frozenset())

    # -- binding -----------------------------------------------------------------------------------------------------

    def serving(self, o: Any) -> Any | None:
        """The capability that serves the occasion: every serve of its end kind and target kind whose needs are all
        met; the one with the most needs wins (the most specific), then catalog order. ``None``: none serves it."""
        target = END_TARGETS.get(o.kind)
        best, best_n = None, -1
        for cap in self.caps:
            for serve in cap.serves:
                if serve.end != o.kind or serve.target not in (target, "*"):
                    continue
                if all(self.need_met(n, o) for n in serve.needs) and len(serve.needs) > best_n:
                    best, best_n = cap, len(serve.needs)
        return best


def attrs_for(cap: Any, src: Mapping[str, Any]) -> dict[str, Any]:
    """The occasion's slot values under the capability's ``bind`` (slot -> source -> the occasion's value)."""
    out = {"app": src.get("app")}
    for slot, source in (getattr(cap, "bind", None) or {}).items():
        out[slot] = src.get(source)
    return out


def bind(occasions: Iterable[Any], index: Index) -> dict[str, int]:
    """Set each occasion's ``capability`` and ``attrs``; returns counts per capability (content-free)."""
    counts: dict[str, int] = defaultdict(int)
    for o in occasions:
        cap = o.forced_capability and index.by_key.get(o.forced_capability) or index.serving(o)
        if cap is None:
            o.capability = NONE
            o.attrs = {"app": o.src.get("app")}
        else:
            o.capability = cap.key
            o.attrs = attrs_for(cap, o.src)
        counts[o.capability] += 1
    return dict(counts)


def load_index(store: Any, now: float, *, browsers: Mapping[str, int] | None = None) -> Index:
    """The capabilities (``yuki.skills.catalog.capabilities``) with this pass's browsers and driver data."""
    from yuki.skills.catalog import capabilities

    t0 = time.perf_counter()
    caps = capabilities().capabilities
    idx = Index(caps, browsers=browsers if browsers is not None else measured_browsers(store, now))
    idx.load_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    return idx


__all__ = ["NONE", "END_TARGETS", "BROWSER_DAYS", "BROWSER_MIN_DOMAINS", "BROWSER_MIN_REPEAT", "BROWSER_MIN_DAY_COUNT",
           "measured_browsers",
           "driver_tables", "Index", "attrs_for", "bind", "load_index"]
