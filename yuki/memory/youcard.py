"""The you-card: one compact, code-built page of what Yuki knows about the user, for the deciders (audit 17 Oct,
contract C1; reasoning.md §8.2 item 1).

The voice talker, the brain's planner and the card judge each decided blind to the user (reasoning.md §4): the talker
got "tone and names, never news", the planner a menu with no names, nobody "what does he usually do at this hour".
This module builds ONE card from the stores that already hold the answer, by code, with no model call:

* **Who** - the portrait's work and interests, first sentence each (planner and judge);
* **Right now** - the you-now record (``yuki.memory.younow``: focus, the day, what just changed, the thread now);
* **Usually now** - the active routines due around this hour, the sites, channels and playlists the user picks most
  at this hour (``yuki.skills.learn.values`` hour histograms, ``yuki.skills.taste.top``);
* **Your things** - sites with their usual browser and a work mark, YouTube channels, Spotify playlists with what the
  user calls them, meeting tabs, Discord voice channels, the user's own ways (contract C4 rows), usual means
  (``prefs``), and the people block (``yuki.memory.contacts.directory``: names, how each is heard, their app);
* **Open** - the brief's live items (dated goals, loops, plans), top 3;
* **Lately** - the last two day digests, one short line each.

Every line says how sure it is ("often", "some days", "once", "you said", "dated"). Sizes: talker at most
:data:`TALKER_CHARS`, planner and judge at most :data:`WIDE_CHARS`; parts give way from the end (lately, then open,
then who). The slow part (everything but "right now") is cached in RAM per database and purpose for
:data:`CACHE_S` and rebuilt sooner when the learned rows, routines, tastes, prefs, contacts or portrait change (one
stamp query); "right now" is read fresh on every call (the you-now record is in RAM). Never written to disk, never
logged (logs carry sizes only), never in the MCP.

Public::

    build_you_card(store, *, purpose="talker", now=None) -> str     # "" when nothing is known
    PURPOSES = ("talker", "planner", "judge")
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

PURPOSES: tuple[str, ...] = ("talker", "planner", "judge")
TALKER_CHARS = 1_800
WIDE_CHARS = 3_000
CACHE_S = 60.0
#: How many of each kind of thing a card names (talker, wide).
SITES_N = (8, 10)
CHANNELS_N = (6, 10)
PLAYLISTS_N = (5, 8)
VOICE_N = (4, 6)
PEOPLE_CHARS = (480, 700)
#: A routine is "due around now" within this many minutes of its usual time.
ROUTINE_NEAR_MIN = 90.0
#: A value counts as "usual at this hour" when this share of its use falls within an hour of now.
HOUR_SHARE = 0.12

_lock = threading.Lock()
#: ``(db path, purpose) -> (built_at, stamp, text)``
_cache: dict[tuple[str, str], tuple[float, tuple[Any, ...], str]] = {}


def _clip(text: Any, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip(" ,;") + "…"


def _first_sentence(text: Any, n: int) -> str:
    text = " ".join(str(text or "").split())
    for stop in (". ", "; "):
        i = text.find(stop)
        if 0 < i < n:
            return text[: i + 1].rstrip(";") if stop == ". " else text[:i]
    return _clip(text, n)


def _sure_days(days: int) -> str:
    """How sure, from how many distinct days it was seen."""
    if days >= 8:
        return "often"
    if days >= 3:
        return "some days"
    if days >= 2:
        return "a couple of times"
    return "once"


def _hm(minute: float) -> str:
    m = int(float(minute) % 1440.0)
    return f"{m // 60:02d}:{m % 60:02d}"


def _stamp(store: Any) -> tuple[Any, ...]:
    """What the slow part depends on (one query of maxima; content-free)."""
    try:
        row = store._query(
            "SELECT (SELECT count(*) || ':' || coalesce(max(updated_at), 0) FROM skills WHERE origin != 'capability'),"
            " (SELECT coalesce(max(updated_at), 0) FROM routines), (SELECT coalesce(max(updated_at), 0) FROM tastes),"
            " (SELECT coalesce(max(updated_at), 0) FROM prefs), (SELECT coalesce(max(updated_at), 0) FROM contacts),"
            " (SELECT coalesce(max(id), 0) FROM portraits), (SELECT coalesce(max(updated_at), 0) FROM day_digests)")
        return tuple(row[0]) if row else ()
    except Exception:  # noqa: BLE001 - an older database: the time bound alone
        return ()


# ---------------------------------------------------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------------------------------------------------


def _who(store: Any) -> list[str]:
    try:
        portrait = store.latest_portrait()
        sections = dict(portrait.sections) if portrait and portrait.sections else {}
        at = float(portrait.at) if portrait else None
    except Exception:  # noqa: BLE001
        return []
    parts = [_first_sentence(sections.get(k), 170) for k in ("work", "interests") if sections.get(k)]
    if not parts:
        return []
    when = f", portrait of {datetime.fromtimestamp(at):%d %b}" if at else ""
    return [f"Who: {'; '.join(p.rstrip('.') for p in parts)} (from memory{when})"]


def _you_record(store: Any) -> Any:
    """The you-now record: this process's builder when it runs here (the memory service), else the service's over
    the Now pipe (a short wait). ``None`` when neither answers."""
    try:
        from yuki.memory.younow import live_builder

        b = live_builder()
        if b is not None:
            return b.snapshot()
    except Exception:  # noqa: BLE001
        pass
    try:
        from yuki.memory.now import shared_client

        client = shared_client(store.path)
        return client.you(wait_s=0.05) if client is not None else None
    except Exception:  # noqa: BLE001
        return None


def _right_now(store: Any, purpose: str, now: float) -> list[str]:
    you = _you_record(store)
    if not you:
        return []
    try:
        from yuki.memory.younow import now_lines

        lines = now_lines(you, now, wide=purpose != "talker")
    except Exception:  # noqa: BLE001
        return []
    return [f"Right now (live): {_clip(' '.join(lines), 420 if purpose == 'talker' else 700)}"] if lines else []


def _routines_near(store: Any, now: float) -> list[str]:
    try:
        rows = store.routines(states=["active"])
    except Exception:  # noqa: BLE001
        return []
    t = datetime.fromtimestamp(now)
    minute = t.hour * 60 + t.minute
    weekend = t.weekday() >= 5
    out = []
    for r in rows:
        at = r.get("at_min")
        if at is None:
            continue
        dayset = str(r.get("dayset") or "")
        if (dayset == "weekdays" and weekend) or (dayset == "weekends" and not weekend):
            continue
        gap = abs(((float(at) - minute + 720.0) % 1440.0) - 720.0)
        try:
            label = store.routine_label(r) or ""
        except Exception:  # noqa: BLE001
            label = ""
        label = label or _skill_name(store, str(r.get("skill_id") or "")) or "their usual"
        hits, eligible = int(r.get("hits") or 0), int(r.get("eligible") or 0)
        sure = f"{hits} of {eligible} {dayset or 'days'}" if eligible else "seen"
        when = "now" if gap <= ROUTINE_NEAR_MIN else "later"
        out.append((gap, f"{_clip(label, 50)} around {_hm(float(at))} {dayset} ({sure}; {when})"))
    out.sort()
    return [line for _gap, line in out[:3]]


def _skill_name(store: Any, key: str) -> str:
    if not key:
        return ""
    try:
        rows = store._query("SELECT name_ciphertext FROM skills WHERE key = ? LIMIT 1", (key,))
        return " ".join(str(store._dec(rows[0][0]) or "").split()) if rows and rows[0][0] else ""
    except Exception:  # noqa: BLE001
        return ""


def _values(store: Any) -> dict[str, list[dict[str, Any]]]:
    try:
        from yuki.skills.learn import values as V

        return V.known(store)
    except Exception:  # noqa: BLE001
        return {}


def _usually_now(store: Any, vals: dict[str, list[dict[str, Any]]], now: float, wide: bool) -> list[str]:
    from yuki.skills.learn import values as V

    parts: list[str] = []
    routines = _routines_near(store, now)
    if routines:
        parts.append("Routines: " + "; ".join(routines))
    hour_vals = []
    for cap_key, items in vals.items():
        for v in items:
            if v.get("everyday") is False:
                continue
            w = V.hour_weight(v, now)
            if w >= HOUR_SHARE and int(v.get("days") or 0) >= 2:
                hour_vals.append((w * float(v.get("score") or 0.0), v))
    hour_vals.sort(key=lambda x: -x[0])
    if hour_vals:
        parts.append("Their things at this hour: " + ", ".join(
            f"{_clip(v.get('name'), 30)} ({v.get('slot')}, {_sure_days(int(v.get('days') or 0))})"
            for _s, v in hour_vals[:4]))
    try:
        from yuki.skills import taste

        for kind, label in (("youtube_handle", "YouTube channels picked at this hour"),
                            ("spotify_playlist", "Playlists played at this hour")):
            got = [t for t in taste.top(store, kind, now=now, n=3 if wide else 2) if t.name and t.score > 0]
            if got:
                parts.append(f"{label}: " + ", ".join(_clip(t.name, 30) for t in got) + " (from what they watched or"
                             " played at this hour)")
    except Exception:  # noqa: BLE001
        pass
    if not parts:
        return []
    return ["Usually around now: " + " | ".join(parts)]


def _browser_name(store: Any, key: str, names: dict[str, str]) -> str:
    if not key:
        return ""
    if key not in names:
        try:
            names[key] = str(store.app_display_name(key) or key)
        except Exception:  # noqa: BLE001
            names[key] = key
    return names[key]


def _things(store: Any, vals: dict[str, list[dict[str, Any]]], now: float, wide: bool) -> list[str]:
    i = 1 if wide else 0
    out: list[str] = []
    names: dict[str, str] = {}
    sites = vals.get("web.open_page") or []
    if sites:
        bits = []
        for v in sites[:SITES_N[i]]:
            usual = _browser_name(store, str((v.get("usual") or {}).get("browser") or ""), names)
            work = " [work]" if v.get("everyday") is False else ""
            label = v.get("label") or v.get("value") or v.get("name")
            called = (f" (they call it {_clip(v.get('name'), 24)})" if v.get("named_by") == "user"
                      and v.get("name") != label else "")
            bits.append(f"{_clip(label, 28)}{called}{work}" + (f" in {usual}" if usual else "")
                        + f" ({_sure_days(int(v.get('days') or 0))})")
        out.append("Sites they open: " + ", ".join(bits))
    tabs = vals.get("browser.site_tab") or []
    if tabs:
        out.append("Meeting tabs they bring up: " + ", ".join(
            f"{_clip(v.get('label') or v.get('name'), 28)}" + (f" in {_browser_name(store, str(v['usual']['browser']), names)}"
                                             if (v.get("usual") or {}).get("browser") else "")
            + f" ({_sure_days(int(v.get('days') or 0))})" for v in tabs[:4]))
    try:
        from yuki.skills import taste

        chans = [n for n in taste.candidates(store, "youtube_handle", now=now, by="n_obs", limit=CHANNELS_N[i])
                 if str(n or "").strip()][:CHANNELS_N[i]]
        if chans:
            out.append("YouTube channels they watch (most first): " + ", ".join(_clip(n, 26) for n in chans))
        lists = taste.described(store, "spotify_playlist", now=now, limit=PLAYLISTS_N[i])
        if lists:
            bits = []
            for d in lists[:PLAYLISTS_N[i]]:
                called = [c for c in (d.get("called") or []) if c][:2]
                plays = d.get("plays")
                bits.append(_clip(d.get("name"), 28) + (f" (they call it {' / '.join(called)})" if called else "")
                            + (" (played often)" if isinstance(plays, (int, float)) and plays >= 3 else ""))
            out.append("Spotify playlists: " + ", ".join(bits))
        voice = [n for n in taste.candidates(store, "discord_voice", now=now, by="n_obs", limit=VOICE_N[i])
                 if str(n or "").strip()][:VOICE_N[i]]
        if voice:
            out.append("Discord voice channels they join: " + ", ".join(_clip(n, 30) for n in voice))
    except Exception:  # noqa: BLE001
        pass
    out += _places(store, 4 if not wide else 6)
    ways = _ways(store, 3 if not wide else 6)
    if ways:
        out.append("Their own ways (learned): " + "; ".join(ways))
    usuals = _usuals(store, 4 if not wide else 8)
    if usuals:
        out.append("Usual means: " + "; ".join(usuals))
    return out


def _places(store: Any, n: int) -> list[str]:
    """The servers of the apps that join calls (catalog data: ``end = "call_join"``; their processes from the chat
    data, as ``yuki.brain.context.read_places``): the part before ": " of their chats' scopes (the capture's own
    "<server>: <channel>" form), most recently seen first. RAM only."""
    try:
        from yuki.memory.store import app_key
        from yuki.skills.catalog import capabilities
        from yuki.skills.chat import app_data

        apps = sorted({str(s.app) for s in capabilities().skills if str(getattr(s, "end", "")) == "call_join" and
                       s.app})
    except Exception:  # noqa: BLE001
        return []
    out = []
    for app in apps:
        try:
            keys = sorted({app_key(str(p)) or "" for p in app_data(app).get("processes") or []} - {""})
            if not keys:
                continue
            rows = store._query(f"SELECT scope_ciphertext FROM threads WHERE process IN ({','.join('?' * len(keys))})"
                                " AND merged_into IS NULL AND kind = 'conversation' AND scope_ciphertext IS NOT NULL"
                                " ORDER BY last_seen DESC LIMIT 200", keys)
        except Exception:  # noqa: BLE001
            continue
        servers: list[str] = []
        for (ct,) in rows:
            try:
                scope = " ".join(str(store._dec(ct) or "").split())
            except Exception:  # noqa: BLE001
                continue
            head, sep, _rest = scope.partition(": ")
            if sep and head and head not in servers:
                servers.append(head)
            if len(servers) >= n:
                break
        if servers:
            out.append(f"{app.capitalize()} servers they chat in (recent first): " + ", ".join(_clip(x, 30)
                                                                                           for x in servers))
    return out


def _ways(store: Any, n: int) -> list[str]:
    """Contract C4: the learned ways (lane kWays' rows, ``origin = 'learned_way'``), name and how often, with the
    items they were used with when the row keeps them (``spec["values"]`` or ``spec["items"]``: names)."""
    try:
        rows = store._query("SELECT name_ciphertext, spec_ciphertext, provenance FROM skills WHERE origin ="
                            " 'learned_way' AND deleted_at IS NULL AND retired_at IS NULL ORDER BY uses DESC,"
                            " updated_at DESC LIMIT ?", (int(n),))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for name_ct, spec_ct, prov in rows:
        try:
            name = " ".join(str(store._dec(name_ct) or "").split()) if name_ct else ""
            spec = json.loads(store._dec(spec_ct) or "{}") if spec_ct else {}
            prov = json.loads(prov or "{}")
        except Exception:  # noqa: BLE001
            continue
        if not name:
            continue
        items = []
        for it in (spec.get("values") or spec.get("items") or []) if isinstance(spec, dict) else []:
            label = it.get("name") if isinstance(it, dict) else it
            if str(label or "").strip():
                items.append(_clip(label, 24))
        days = int(prov.get("days") or 0) if isinstance(prov, dict) else 0
        out.append(_clip(name, 50) + (f" (with {', '.join(items[:4])})" if items else "")
                   + (f" ({_sure_days(days)})" if days else ""))
    return out


def _usuals(store: Any, n: int) -> list[str]:
    """The confirmed usuals (``prefs`` rows the user kept): slot and option, "you said"."""
    try:
        rows = store._query("SELECT slot, option FROM prefs WHERE state = 'usual' AND ended_at IS NULL ORDER BY"
                            " coalesce(confirmed_at, last_at) DESC LIMIT ?", (int(n),))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for slot, option in rows:
        cap, _, rest = str(slot).partition(":")
        what, _, scope = rest.partition("@")
        out.append(f"{cap.rpartition('.')[2].replace('_', ' ')} {what}" + (f" for {scope}" if scope else "")
                   + f": {option} (they said)")
    return out


def _people(store: Any, wide: bool) -> list[str]:
    try:
        from yuki.memory.contacts import DIRECTORY_HEAD, directory

        text = directory(store, chars=PEOPLE_CHARS[1 if wide else 0]).strip()
        if text.startswith(DIRECTORY_HEAD):
            text = text[len(DIRECTORY_HEAD):].strip()     # one header: this card's own
    except Exception:  # noqa: BLE001
        return []
    return [f"People (who, how their name is heard, where they talk):\n{text}"] if text else []


def _open(store: Any, n: int) -> list[str]:
    try:
        portrait = store.latest_portrait()
        brief = dict(portrait.brief) if portrait and portrait.brief else {}
    except Exception:  # noqa: BLE001
        return []
    items = [x for x in brief.get("now") or [] if isinstance(x, dict) and x.get("text")][:n]
    if not items:
        return []
    return ["Open for them (dated): " + "; ".join(f"{x.get('date') or ''}: {_clip(x['text'], 110)}".strip(": ")
                                                  for x in items)]


def _lately(store: Any) -> list[str]:
    try:
        days = store.day_digests(limit=2)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for d in days[:2]:
        text = d.get("shown") or d.get("text") or ""
        if text:
            out.append(f"{d.get('day')}: {_first_sentence(text, 150)}")
    return ["Lately: " + " | ".join(out)] if out else []


# ---------------------------------------------------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------------------------------------------------

HEAD = ("[What Yuki knows about the user (code-built from memory; each line says how sure). Use it to choose for them "
        "and to read hints: their usual, their things, what they do at this hour. Their own words always win.]")


def _slow(store: Any, purpose: str, now: float) -> list[tuple[int, str]]:
    """``(priority, line)`` of everything but "right now" (lower priority gives way last)."""
    wide = purpose != "talker"
    vals = _values(store)
    lines: list[tuple[int, str]] = []
    if wide:
        lines += [(3, x) for x in _who(store)]
    lines += [(1, x) for x in _usually_now(store, vals, now, wide)]
    lines += [(2, x) for x in _things(store, vals, now, wide)]
    lines += [(3, x) for x in _people(store, wide)]
    if wide:
        # the planner and the judge only: the talker answers "what's due / what did I do" by a lookup (25 Sept), so a
        # nightly slice of open items is never on its card
        lines += [(4, x) for x in _open(store, 3)]
        lines += [(4, x) for x in _lately(store)]
    return lines


def _fit(lines: Sequence[tuple[int, str]], cap: int) -> str:
    keep: set[int] = set()
    total = len(HEAD) + 1
    for i in sorted(range(len(lines)), key=lambda i: (lines[i][0], i)):
        text = lines[i][1]
        if total + len(text) + 1 > cap:
            continue
        keep.add(i)
        total += len(text) + 1
    body = [lines[i][1] for i in range(len(lines)) if i in keep]
    return (HEAD + "\n" + "\n".join(body))[:cap] if body else ""


def build_you_card(store: Any, *, purpose: str = "talker", now: float | None = None) -> str:
    """The you-card for one decider (``purpose`` in :data:`PURPOSES`): ``""`` when nothing is known. Model-free; the
    slow part cached in RAM (:data:`CACHE_S`, or until a store it reads changes); "right now" read fresh. Raises
    ``ValueError`` for an unknown purpose only."""
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {', '.join(PURPOSES)}, not {purpose!r}")
    now = time.time() if now is None else float(now)
    cap = TALKER_CHARS if purpose == "talker" else WIDE_CHARS
    key = (str(getattr(store, "path", "")), purpose)
    stamp = _stamp(store)
    with _lock:
        hit = _cache.get(key)
    if hit is not None and now - hit[0] < CACHE_S and hit[1] == stamp:
        slow = json.loads(hit[2])
    else:
        slow = _slow(store, purpose, now)
        with _lock:
            _cache[key] = (now, stamp, json.dumps(slow, ensure_ascii=False))
    live = [(0, x) for x in _right_now(store, purpose, now)]
    return _fit([*live, *[(int(p), str(t)) for p, t in slow]], cap)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


__all__ = ["PURPOSES", "TALKER_CHARS", "WIDE_CHARS", "CACHE_S", "build_you_card", "clear_cache"]
