"""The voice talker's look at the user's days (lThink, 17 Oct 2026; the first live test, problem 9).

Live 17 Oct: "what was I doing yesterday evening", "who did I talk to on Discord today", "which VC do I usually hop in
at night" and "what's my usual playlist" took 17-29 s each, all through the Sonnet thinker, because the talker's only
look into memory (``quick_memory`` search) searches facts, and the answers live in the episodes, the calls, the chat
routes and the tastes. Two reads here, both plain assembly of stored rows (no model, no reading of words):

* :func:`digest`: what the talker gets at the start of each conversation (``yuki.voice.recall`` formats it after the
  last hour's exchanges): the last day by parts of the day (the episodes' own titles, the apps with most time and the
  calls), the people they messaged and the calls they were on in their day so far, the voice channels they were in over
  the last weeks and with whom, and what they pick most of each taste kind (their playlists, channels) with the hours
  they pick it and the words they call it by.
* :func:`day_report`: one day, or one part of it, in more detail (the episodes' summaries too), for the talker's
  ``quick_memory`` ``day`` look, so a question about another day is one quick look, not a hand-over.

Everything is the user's own data, read in the tray's process from the store memory already opened. Nothing here
logs; callers log sizes and counts only. Every part is read on its own and a failed part is left out: never raises.
"""

from __future__ import annotations

import math
import statistics
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

#: The parts of a day the digest groups by: ``(name, first hour, end hour)``, the end past 24 for the night, which runs
#: to the next morning and belongs to the date it started on. A display partition of the clock, never a reading of
#: anyone's words: the talker matches "yesterday evening" against the labels, which carry their hours.
PARTS: tuple[tuple[str, int, int], ...] = (("morning", 5, 12), ("afternoon", 12, 17), ("evening", 17, 21),
                                           ("night", 21, 29))
#: The digest shows the parts that overlap the last this many hours.
DIGEST_HOURS = 24.0
#: Per part of the digest: the longest episodes (shown in time order) and the activities with most time.
PART_EPISODES = 3
PART_APPS = 3
#: An activity under this much time in a part is not named (seconds).
PART_APP_MIN_S = 5 * 60.0
#: How far back the voice channels are counted, how many are shown, and how many of the people seen on those calls.
VOICE_DAYS = 30.0
VOICE_PLACES = 3
VOICE_PEOPLE = 5
#: The night, for "at night": a call that began from this hour on, or before :data:`NIGHT_ENDS`.
NIGHT_STARTS, NIGHT_ENDS = 21, 5
#: People and chats shown for a window, at most.
PEOPLE_N = 8
CHATS_N = 4
#: Per taste kind: the entities shown (most picked first) and the words the user called each by.
USUAL_N = 4
SAID_N = 2
#: A taste's peak hours: the hours within this share of its busiest hour, merged into at most this many ranges.
PEAK_SHARE = 0.6
PEAK_RANGES = 2
#: A day report: episodes shown with their summaries at most.
DAY_EPISODES = 10


def _parts_between(since: float, until: float) -> list[tuple[str, str, float, float]]:
    """The parts of days overlapping ``[since, until)``: ``(date label, part name, start, end)``, oldest first, each
    with its own whole span (the caller clips it)."""
    first = datetime.fromtimestamp(since).date() - timedelta(days=1)
    last = datetime.fromtimestamp(until).date()
    out: list[tuple[str, str, float, float]] = []
    day = first
    while day <= last:
        midnight = datetime(day.year, day.month, day.day)
        for name, h0, h1 in PARTS:
            start = (midnight + timedelta(hours=h0)).timestamp()
            end = (midnight + timedelta(hours=h1)).timestamp()
            if end > since and start < until:
                out.append((f"{midnight:%a} {day.day} {midnight:%b}", name, start, end))
        day += timedelta(days=1)
    return out


def _hhmm(at: float | None) -> str:
    return datetime.fromtimestamp(at).strftime("%H:%M") if at else ""


def _ts(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip()).timestamp()
        except ValueError:
            return None
    return None


def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]
    return head.rstrip(",;:") + " …"


def _first_sentence(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    for stop in (". ", "; "):
        if stop in text:
            text = text.split(stop, 1)[0]
            break
    return _clip(text.rstrip("."), limit)


# ---------------------------------------------------------------------------------------------------------------------
# Calls (merged sessions, with where and who as the call's digest read the screen around it)
# ---------------------------------------------------------------------------------------------------------------------


def sessions(store: Any, since: float, until: float, *, now: float | None = None) -> list[dict[str, Any]]:
    """The call sessions overlapping ``[since, until)`` (``yuki.memory.callspans.merged_calls``), oldest first:
    ``{"app", "kind" (call | game), "start", "end", "ongoing", "place", "voice", "people"}``. ``place``: where it was
    as the call's digest read the screen ("Discord, #lofi lounge voice"; the older digests' free text as it is), empty
    when that says no more than the app; ``voice``: the digest marked the place a voice channel; ``people``: the names
    seen on it, strongest first. ``[]`` on any failure."""
    try:
        from yuki.memory.calldigest import merge_people
        from yuki.memory.callspans import merged_calls
        from yuki.memory.titles import place_label

        spans = merged_calls(store, since, until, now=now)
        digests = store.call_digests(i for sp in spans for i in sp["call_ids"]) if spans else {}
    except Exception:  # noqa: BLE001 - no calls known
        return []
    out = []
    for sp in spans:
        contexts = [(digests.get(c) or {}).get("context") or {} for c in sp["call_ids"]]
        labels = [place_label(c.get("place"), c.get("where") or "") for c in contexts]
        app = " ".join(str(sp.get("app") or "").split())
        place = next((x for x in labels if x and x.casefold() != app.casefold()), "")
        voice = any(isinstance(c.get("place"), dict) and c["place"].get("kind") == "voice" for c in contexts)
        try:
            people = [p["name"] for p in merge_people(*(c.get("people") or [] for c in contexts))]
        except Exception:  # noqa: BLE001
            people = []
        out.append({"app": app, "kind": str(sp.get("kind") or "call"), "start": float(sp["start"]),
                    "end": float(sp["end"]), "ongoing": bool(sp.get("ongoing")), "place": place, "voice": voice,
                    "people": people})
    return out


def voice_usual(store: Any, now: float, *, days: float = VOICE_DAYS) -> dict[str, Any]:
    """The places of the user's calls (not a game's own voice chat) over the last ``days``, most calls first:
    ``{"places": [{"place", "n", "night", "hour", "last", "voice"}], "people": [names], "calls": n}``. ``hour``: the
    median start hour; ``night``: how many began at night (:data:`NIGHT_STARTS` to :data:`NIGHT_ENDS`). ``people``:
    the names seen most on those calls. Counts of stored rows; never raises."""
    rows = [s for s in sessions(store, now - days * 86400.0, now, now=now) if s["kind"] != "game"]
    by_place: dict[str, list[dict[str, Any]]] = {}
    seen: Counter[str] = Counter()
    for s in rows:
        if s["place"]:
            by_place.setdefault(s["place"], []).append(s)
        for name in s["people"]:
            seen[name] += 1
    places = []
    for place, group in by_place.items():
        hours = [datetime.fromtimestamp(s["start"]).hour for s in group]
        places.append({"place": place, "n": len(group),
                       "night": sum(1 for h in hours if h >= NIGHT_STARTS or h < NIGHT_ENDS),
                       "hour": int(statistics.median([(h - 12) % 24 for h in hours]) + 12) % 24,
                       "last": max(s["start"] for s in group), "voice": any(s["voice"] for s in group)})
    places.sort(key=lambda p: (-p["n"], -p["last"]))
    return {"places": places[:VOICE_PLACES], "people": [n for n, _ in seen.most_common(VOICE_PEOPLE)],
            "calls": len(rows)}


# ---------------------------------------------------------------------------------------------------------------------
# Who they messaged (the people index's routes, counted in a window)
# ---------------------------------------------------------------------------------------------------------------------


def people_between(store: Any, since: float, until: float) -> dict[str, Any]:
    """Who the user messaged in ``[since, until)``, from the people index's routes and the messages memory captured:
    ``{"people": [{"name", "app", "mine", "theirs", "last"}], "chats": [{"chat", "app", "kind", "mine", "last"}]}``.
    ``people``: direct messages either way, the latest first; ``chats``: groups and channels the user wrote in.
    Counts and times only (no message text is decrypted); never raises."""
    out: dict[str, Any] = {"people": [], "chats": []}
    try:
        from yuki.memory import contacts as _c

        persons = _c.people(store)
        routes = [(p, r) for p in persons for r in p.routes if r.thread_ids]
        places = [r for r in _c.places(store) if r.thread_ids]
    except Exception:  # noqa: BLE001 - no people index yet
        return out
    ids = sorted({int(t) for _p, r in routes for t in r.thread_ids} | {int(t) for r in places for t in r.thread_ids})
    if not ids:
        return out
    counts: dict[int, dict[bool, tuple[int, float]]] = {}
    try:
        marks = ",".join("?" * len(ids))
        for row in store._query(
                f"SELECT thread_id, is_me, count(*) AS n, max({_c.MSG_TIME_SQL}) AS last FROM messages"
                f" WHERE thread_id IN ({marks}) AND status != 'reread' AND ({_c.MSG_TIME_SQL}) >= ?"
                f" AND ({_c.MSG_TIME_SQL}) < ? GROUP BY thread_id, is_me", [*ids, since, until]):
            counts.setdefault(int(row["thread_id"]), {})[bool(row["is_me"])] = (int(row["n"]), float(row["last"]))
    except Exception:  # noqa: BLE001
        return out

    def tally(route: Any) -> tuple[int, int, float]:
        mine = theirs = 0
        last = 0.0
        for t in route.thread_ids:
            got = counts.get(int(t)) or {}
            for is_me, (n, at) in got.items():
                if is_me:
                    mine += n
                else:
                    theirs += n
                last = max(last, at)
        return mine, theirs, last

    def app_of(route: Any) -> str:
        try:
            return _c.service_app(store, route.service) or route.app or route.service
        except Exception:  # noqa: BLE001
            return route.app or route.service

    # a direct-message thread the people index linked to more than one person (live 17 Oct: Darvesh's Slack DM under
    # Malini too) counts only for the person whose own name or alias is in that chat's name, when one is (identity of
    # the names memory keeps, never a reading of words); with none, it stays with each
    owners: dict[int, set[int]] = {}
    for person, route in routes:
        if route.kind == "dm":
            for t in route.thread_ids:
                owners.setdefault(int(t), set()).add(person.id)

    def named(person: Any, chat: str) -> bool:
        folded = " ".join(str(chat or "").split()).casefold()
        return any(n and " ".join(str(n).split()).casefold() in folded for n in person.names)

    by_id = {p.id: p for p, _r in routes}
    best: dict[tuple[int, str], dict[str, Any]] = {}
    for person, route in routes:
        if route.kind != "dm":
            continue
        shared = set().union(*(owners.get(int(t), set()) for t in route.thread_ids)) - {person.id}
        if shared and not named(person, route.chat) and any(named(by_id[o], route.chat) for o in shared):
            continue
        mine, theirs, last = tally(route)
        if not (mine or theirs):
            continue
        app = app_of(route)
        key = (person.id, app.casefold())
        have = best.get(key)
        if have is None:
            best[key] = {"name": person.name, "app": app, "mine": mine, "theirs": theirs, "last": last}
        else:
            have["mine"] += mine
            have["theirs"] += theirs
            have["last"] = max(have["last"], last)
    out["people"] = sorted(best.values(), key=lambda p: -p["last"])[:PEOPLE_N]
    chats: dict[str, dict[str, Any]] = {}
    for route in places:
        mine, _theirs, last = tally(route)
        if not mine:
            continue
        app = app_of(route)
        key = f"{app}|{route.chat}".casefold()
        if key not in chats:
            chats[key] = {"chat": route.chat, "app": app, "kind": route.kind, "mine": mine, "last": last}
    out["chats"] = sorted(chats.values(), key=lambda c: -c["last"])[:CHATS_N]
    return out


# ---------------------------------------------------------------------------------------------------------------------
# What they pick (tastes: every kind memory keeps, from its own data)
# ---------------------------------------------------------------------------------------------------------------------


def _peaks(weekday: list[float], weekend: list[float]) -> list[tuple[int, int]]:
    """The hours a taste is picked most, as at most :data:`PEAK_RANGES` ranges ``(first hour, last hour)``."""
    hist = [a + b for a, b in zip(weekday, weekend)]
    top = max(hist) if hist else 0.0
    if top <= 0:
        return []
    hot = [h for h in range(24) if hist[h] >= top * PEAK_SHARE]
    ranges: list[list[int]] = []
    for h in hot:
        if ranges and h == ranges[-1][1] + 1:
            ranges[-1][1] = h
        else:
            ranges.append([h, h])
    if len(ranges) > 1 and ranges[0][0] == 0 and ranges[-1][1] == 23:
        ranges[0][0] = ranges.pop()[0] - 24   # a range through midnight is one range
    ranges.sort(key=lambda r: -sum(hist[h % 24] for h in range(r[0], r[1] + 1)))
    return [(a % 24, b % 24) for a, b in ranges[:PEAK_RANGES]]


def usuals(store: Any, now: float) -> list[dict[str, Any]]:
    """What the user picks most of each taste kind memory keeps (kinds from the data: their Spotify playlists, the
    YouTube channels they watch, ...): ``[{"kind", "items": [{"name", "n", "peaks", "said"}], "now"}]``. ``n``: the
    decayed count of picks; ``peaks``: the hours they pick it most; ``said``: the words they called it by when those
    named no playlist and it then played (``yuki.skills.taste.note`` ``said_as``); ``now``: the one picked most at
    this hour. Never raises (``[]``)."""
    try:
        from yuki.skills import taste

        kinds = [str(r[0]) for r in store._query("SELECT DISTINCT kind FROM tastes ORDER BY kind")
                 if r[0] and not str(r[0]).endswith(taste.SAID_SUFFIX)]
    except Exception:  # noqa: BLE001
        return []
    out = []
    for kind in kinds:
        try:
            rows = [t for t in taste.top(store, kind, now=now, n=10_000) if t.name and t.n_obs > 0]
            if not rows:
                continue
            at_hour = rows[0].name if rows[0].score > 0 else None
            rows.sort(key=lambda t: (-t.n_obs, t.entity_key))
            hists = {str(r[0]): (taste._hist(r[1]), taste._hist(r[2])) for r in store._query(
                "SELECT entity_key, hist_weekday, hist_weekend FROM tastes WHERE kind = ?", (kind,))}
            items = []
            for t in rows[:USUAL_N]:
                wd, we = hists.get(t.entity_key, ([0.0] * 24, [0.0] * 24))
                items.append({"name": t.name, "n": round(t.n_obs, 1), "peaks": _peaks(wd, we),
                              "said": taste.said_words(store, kind, t.name)[:SAID_N]})
            out.append({"kind": kind, "items": items, "now": at_hour})
        except Exception:  # noqa: BLE001 - that kind is left out
            continue
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Parts of days (episodes, time per activity, calls)
# ---------------------------------------------------------------------------------------------------------------------


def _episode_rows(client: Any, since: float, until: float) -> list[dict[str, Any]]:
    try:
        rows = client.episodes(since, until, limit=400) or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for e in rows:
        start, end = _ts(e.get("start")), _ts(e.get("end"))
        title = " ".join(str(e.get("title") or "").split()) or _first_sentence(e.get("summary") or e.get("text"), 70)
        if start is None or end is None or not title:
            continue
        out.append({"start": start, "end": end, "title": title,
                    "summary": " ".join(str(e.get("summary") or "").split())})
    return out


def _apps_in(rows: list[Any], since: float, until: float, n: int) -> list[tuple[str, float]]:
    """The activities with most present time in ``[since, until)`` (the timeline's own aggregate, by site: a web page
    counts as its site, any other window as its app)."""
    try:
        from yuki.memory.timeline import aggregate

        agg = aggregate(rows, since, until, "site", limit=n + 2)
    except Exception:  # noqa: BLE001
        return []
    return [(str(i.get("label") or ""), float(i.get("present_s") or 0.0)) for i in agg.get("items") or []
            if i.get("label") and float(i.get("present_s") or 0.0) >= PART_APP_MIN_S][:n]


def parts(client: Any, store: Any, since: float, until: float, *, episodes: int = PART_EPISODES,
          apps: int = PART_APPS, summaries: bool = False, now: float | None = None) -> list[dict[str, Any]]:
    """The parts of days overlapping ``[since, until)`` (:data:`PARTS`), oldest first, each with what memory measured
    in it: ``{"date", "part", "start", "end", "so_far", "episodes": [{"start", "end", "title", "summary"?}],
    "apps": [(label, seconds)], "calls": [session]}``. ``episodes``: the longest that began in the part, shown in time
    order; ``summaries``: keep each episode's summary too. A part with nothing measured is left out. Never raises."""
    now = time.time() if now is None else float(now)
    spans = [(d, n, s, min(e, until), e > now) for d, n, s, e in _parts_between(since, until)]
    if not spans:
        return []
    lo, hi = spans[0][2], spans[-1][3]
    eps = _episode_rows(client, lo, hi)
    try:
        rows = store.timeline_between(lo, hi)
    except Exception:  # noqa: BLE001
        rows = []
    calls = sessions(store, lo, hi, now=now)
    out = []
    for date, name, start, end, going in spans:
        mine = [e for e in eps if start <= max(e["start"], lo) < end]
        mine.sort(key=lambda e: -(e["end"] - e["start"]))
        shown = sorted(mine[:episodes], key=lambda e: e["start"])
        if not summaries:
            shown = [{k: v for k, v in e.items() if k != "summary"} for e in shown]
        part_calls = [c for c in calls if start <= max(c["start"], lo) < end]
        used = _apps_in(rows, start, end, apps) if rows else []
        if not (shown or part_calls or used):
            continue
        out.append({"date": date, "part": name, "start": start, "end": end, "so_far": going,
                    "episodes": shown, "apps": used, "calls": part_calls})
    return out


# ---------------------------------------------------------------------------------------------------------------------
# The two reads
# ---------------------------------------------------------------------------------------------------------------------


def day_start(now: float) -> float:
    """When the user's day that ``now`` falls in began (memory's own day start: 02:00 belongs to the day before)."""
    from yuki.memory.checkins import day_boundary, user_day

    return day_boundary(user_day(now))


def digest(client: Any, *, now: float | None = None, hours: float = DIGEST_HOURS) -> dict[str, Any]:
    """The talker's digest of the user's days at ``now`` (see the module docstring): ``{"now", "day_start", "parts",
    "people", "chats", "calls_today", "voice", "usuals", "ms"}``, each read on its own (a failed one is empty). Plain
    rows; ``yuki.voice.recall.digest_view`` words it. Never raises."""
    now = time.time() if now is None else float(now)
    started = time.perf_counter()
    store = getattr(client, "store", client)
    out: dict[str, Any] = {"now": now, "day_start": None, "parts": [], "people": [], "chats": [], "calls_today": [],
                           "voice": {}, "usuals": []}
    try:
        out["day_start"] = day_start(now)
    except Exception:  # noqa: BLE001
        out["day_start"] = now - 12 * 3600.0
    for key, read in (("parts", lambda: parts(client, store, now - hours * 3600.0, now, now=now)),
                      ("voice", lambda: voice_usual(store, now)),
                      ("usuals", lambda: usuals(store, now)),
                      ("calls_today", lambda: sessions(store, out["day_start"], now, now=now))):
        try:
            out[key] = read()
        except Exception:  # noqa: BLE001 - that part is left out
            pass
    try:
        talked = people_between(store, out["day_start"], now)
        out["people"], out["chats"] = talked["people"], talked["chats"]
    except Exception:  # noqa: BLE001
        pass
    out["ms"] = round((time.perf_counter() - started) * 1000)
    return out


def day_report(client: Any, day: str, part: str | None = None, *, now: float | None = None) -> dict[str, Any]:
    """One day (``"YYYY-MM-DD"``, a calendar day) or one part of it (:data:`PARTS` names; the night runs to the next
    morning) in more detail, for the talker's ``quick_memory`` ``day`` look: ``{"day", "part", "since", "until",
    "parts", "people", "chats", "found"}``, the parts with up to :data:`DAY_EPISODES` episodes and their summaries.
    ``found`` is False when memory holds nothing for it. Raises ``ValueError`` on a day or part it can't read."""
    now = time.time() if now is None else float(now)
    date = datetime.strptime(str(day).strip()[:10], "%Y-%m-%d")
    names = [p[0] for p in PARTS]
    if part and part not in names:
        raise ValueError(f"part must be one of {', '.join(names)}")
    if part:
        _name, h0, h1 = next(p for p in PARTS if p[0] == part)
        since, until = (date + timedelta(hours=h0)).timestamp(), (date + timedelta(hours=h1)).timestamp()
    else:
        since, until = (date + timedelta(hours=PARTS[0][1])).timestamp(), \
            (date + timedelta(hours=PARTS[-1][2])).timestamp()
    until = min(until, now)
    if since >= until:
        return {"day": day, "part": part, "since": since, "until": until, "parts": [], "people": [], "chats": [],
                "found": False}
    store = getattr(client, "store", client)
    per_part = max(2, math.ceil(DAY_EPISODES / (1 if part else len(PARTS))))
    got = parts(client, store, since, until, episodes=per_part, apps=PART_APPS + 1, summaries=True, now=now)
    got = [p for p in got if p["start"] >= since - 1 and p["end"] <= until + 1]
    talked = people_between(store, since, until)
    found = bool(got or talked["people"] or talked["chats"])
    return {"day": day, "part": part, "since": since, "until": until, "parts": got, "people": talked["people"],
            "chats": talked["chats"], "found": found}


__all__ = ["DIGEST_HOURS", "PARTS", "day_report", "day_start", "digest", "parts", "people_between", "sessions",
           "usuals", "voice_usual"]
