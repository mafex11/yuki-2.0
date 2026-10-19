"""The you-card: one compact, code-built page of what Yuki knows about the user, for the deciders (audit 17 Oct,
contract C1; reasoning.md §8.2 item 1).

The voice talker, the brain's planner and the card judge each decided blind to the user (reasoning.md §4): the talker
got "tone and names, never news" (the 25 Sept rule; lifted for choosing by decision 3, 18 Oct), the planner a menu
with no names, nobody "what does he usually do at this hour". This module builds ONE card from the stores that
already hold the answer, by code, with no model call. Phase 3.1 (audit 18 Oct, dots.md R3): a page a model can
reason from, not a catalog, in this order (the situation on the screens stays separate: ``yuki.perception.situation``):

1. **What they're in the middle of and waiting for** - **Day** and **In the middle of** (every purpose; decision 3):
   from the you-now record (``yuki.memory.younow``), when their day at the PC began and whether they are at it, away
   or just back (``younow.day_words``); the thread now by its title, up to three items due soon, who they wait on
   (``younow.middle_words``, labelled ``younow.MIDDLE_LABEL``: to choose well, never recited unasked). The focus and
   what just changed are not here: the talker's own per-turn line carries them, and the planner's and the judge's
   you-now renders (``render(you, "thinker" | "cards")``) sit beside the card; **Open** - the brief's live items
   (dated goals, loops, plans; a call promise only once known what it is about: ``cardjudge.about_known_items``),
   top 3 (the talker's top 2); **Lately** (planner and judge) - the last two day digests;
2. **Their habits, each with how often** - how they keep their desk (``yuki.memory.deskhabits.lines``, contract D1:
   "when ..., he keeps X on screen 2 (9 of 11)"); **Usually now** - the routines due around this hour, the picks for
   now (phase 2.2: ``yuki.skills.taste.now_picks``, each with a code-made why; the talker and the planner may choose
   from them by name), their other things at this hour (``yuki.skills.learn.values`` hour histograms: seen on
   :data:`HOUR_MIN_DAYS` days or more: "a couple of times" never leads the card); their weekly routines with days and
   times (every active one, whatever the day: "what time's my meeting tomorrow"); their routine as the nightly
   portrait writes it (final-B: sleep, work hours, the usual meeting; the brief leaves it to the card; the planner's
   and the judge's in the first priority, :data:`ROUTINE_PRIORITY`); their own ways (contract C4: run only when asked, never offered);
3. **Their things, each with what it means to them** (``yuki.memory.meanings.meaning_of``, contract M1: "mafex (the
   MacBook's projects)", next to the thing) - sites with their usual browser (the preference's own answer:
   ``values.known`` reads it from ``yuki.skills.prefs``, one truth with what Yuki opens them in) and a work mark, weak
   ones last; their browsers when one means something; meeting tabs; YouTube channels (by display name, most watched
   first); Spotify playlists with what the user calls them; the voice channels and servers of the apps that join calls
   (``yuki.skills.discord_route.voice_channels``), most days used first with the count ("4 of the last 14 days":
   final-B, a recent-first list read as the usual); the things inside their own ways' apps (their projects); usual means
   (``prefs``); **Where they left off** (planner only) - on the :data:`LEFT_OFF_N` sites they come back to most (never
   a work site), the last page they had in front in one of their browsers (an app's own embedded page is no page to
   open), with its title and address (``Store.links``);
4. **People** (planner and judge; the talker's profile layer has the directory at 900) - the people block
   (``yuki.memory.contacts.directory`` without its header: the whole :data:`PEOPLE_CHARS` is the people's, closest
   first), then **Who** - the portrait's work and interests, first sentence each.

Every line says how sure it is ("often", "some days", "n of m", "you said", "dated"). Sizes: talker under
:data:`TALKER_CHARS` (one less: the mouth's cut), planner and judge at most :data:`WIDE_CHARS`. When a card is over
(:func:`_fit`), by priority from the least: the lists give their tails first (a weak item first, one with a meaning
last, never below a few), then whole lines go; a priority gives its tails only once the lines two or more priorities
below it are gone (the first priority: Open, Lately, the desk, Usually now, Left off, People, the planner's and the
judge's routine), and the live lines never go. The slow part (everything but the live lines) is cached in RAM per database and purpose for :data:`CACHE_S` and rebuilt sooner when the learned rows,
routines, tastes, prefs, contacts, portrait, day digests, meanings or desk habits change (one stamp query, and the
meanings' own); the live lines are read fresh on every call (the you-now record is in RAM). Never written to disk,
never logged (logs carry sizes only), never in the MCP.

Public::

    build_you_card(store, *, purpose="talker", now=None) -> str     # "" when nothing is known
    PURPOSES = ("talker", "planner", "judge")
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable, Sequence
from datetime import datetime
from types import SimpleNamespace
from typing import Any

PURPOSES: tuple[str, ...] = ("talker", "planner", "judge")
#: The voice talker reads the card cut at a line boundary to 2,200 chars (kMind: ``mouth.fetch_you_card``): the card
#: fits it whole, the lines that decide most first: nothing is cut there. That cut counts a newline after every line,
#: the last one too, so the talker's card is fitted to one character less (desk19 review M2: a card of exactly 2,200
#: lost its last line there).
TALKER_CHARS = 2_200
WIDE_CHARS = 3_000
CACHE_S = 60.0
#: How many of each kind of thing a card names at most (talker, wide); :func:`_fit` takes tails off when it is over.
SITES_N = (8, 10)
CHANNELS_N = (6, 10)
PLAYLISTS_N = (5, 8)
VOICE_N = (4, 6)
SERVERS_N = (4, 6)
WAY_THINGS_N = (6, 10)
WAYS_N = (3, 6)
#: The brief's dated live items on the "Open" line (talker, wide).
OPEN_N = (2, 3)
OPEN_ITEM_CHARS = 90
#: The people block (planner and judge; the talker's profile layer has the directory at 900): all of it the people's
#: (the directory's own header is not built, so it costs nothing here).
PEOPLE_CHARS = 520
#: A routine is "due around now" within this many minutes of its usual time.
ROUTINE_NEAR_MIN = 90.0
#: A value counts as "usual at this hour" when this share of its use falls within an hour of now, and it was seen on
#: at least HOUR_MIN_DAYS days ("a couple of times" is too weak to lead the card).
HOUR_SHARE = 0.12
HOUR_MIN_DAYS = 3
#: A thing seen on fewer days than this (and never asked for) is a weak signal: named after the stronger ones of its
#: kind, and the first to go when a card is over.
WEAK_DAYS = 3
#: What a thing means to the user (contract M1), at most this many characters next to it.
MEANING_CHARS = 60
#: The desk habits line (contract D1) at most (talker, wide).
DESK_CHARS = (300, 360)
#: The weekly routines line names at most this many (talker, wide).
ROUTINES_N = (4, 6)
#: final-B (19 Oct): the "usual" lists that were recent first (voice channels, servers) rank by use over the last this
#: many days (a channel's days with a call there, a server's days with a capture in its chats), the count said next
#: to each, "n of the last 14 days", as the routines' "11 of 13 weekdays".
USE_DAYS = 14.0
#: The portrait's routine (the brief's ``routine`` field, else its Routines section) on the card at most: brief.py
#: leaves it out of the talker's slice because the card carries it, and the routines table holds only the skills'
#: clock routines (a skill's step at a time of day), not what they are for (the meeting that step is for).
ROUTINE_CHARS = (300, 400)
#: The routine line's priority per card (:func:`_fit`). The planner reads the brief only when there is no card
#: (``brain.context.read_context``) and its memory reads never carry the routine, so the card is its one way to it:
#: first priority there (desk19 review M1: at the second it always gave way on the full planner card). Integration
#: review MH3: the live lines (the day, "in the middle of": priority 0, up to MIDDLE_CHARS each) take the room first,
#: so at the second priority the line gave way on most talker cards and nearly every judge card (measured on a copy
#: with live lines of a realistic size). The judge has no other way to it (its tomorrow and rest check-ins): first
#: priority there too. The talker keeps it at the second: its memory search carries it (``voice.recall.routine_line``).
ROUTINE_PRIORITY = {"talker": 2, "planner": 1, "judge": 1}
#: "Lately": each day digest's first sentence at most.
LATELY_CHARS = 130
#: "Where they left off" (planner): the LEFT_OFF_N sites they come back to most (the most days; never fewer than
#: LEFT_OFF_MIN_N when the card is over), from pages seen within LEFT_OFF_DAYS (read whole: at most LEFT_OFF_LINKS
#: pages, measured 916 in 14 days); a site counts when its pages were seen on at least LEFT_OFF_MIN_DAYS days; only a
#: page they had in one of their browsers (learned: the programs their own site visits used) at an http(s) address:
#: an app's own embedded page (its local bundle) is no page to open; a page whose address is longer than
#: LEFT_OFF_URL_CHARS, or has a query longer than LEFT_OFF_QUERY_CHARS (a token, a tracking string, a search), is
#: passed over for that site's last one that can be given whole: a cut address opens the wrong page, and a title alone
#: is no way back. Each page's title at most LEFT_OFF_TITLE_CHARS (its address names the site).
LEFT_OFF_N = 3
LEFT_OFF_MIN_N = 2
LEFT_OFF_DAYS = 14.0
LEFT_OFF_LINKS = 2_000
LEFT_OFF_MIN_DAYS = 2
LEFT_OFF_URL_CHARS = 140
LEFT_OFF_QUERY_CHARS = 40
LEFT_OFF_TITLE_CHARS = 40
#: The day and "in the middle of" lines at most, each (a thread, three due titles, two names).
MIDDLE_CHARS = 420
#: Phase 2.2, the picks for now on the "Usually around now" line (talker, wide): how many in all (fewer when the line
#: is long, down to :data:`PICKS_MIN`), a video's title cut to, and the whole line at most.
PICKS_N = (4, 7)
PICKS_MIN = 3
TITLE_CHARS = (32, 48)
USUAL_CHARS = (360, 360)
#: An item's weight when :func:`_fit` takes a list's tail: a weak signal goes first, one with a meaning (or a name the
#: user gave it) last.
WEAK, PLAIN, MEANT = -1.0, 0.0, 1.0

_lock = threading.Lock()
#: ``yuki.memory.deskhabits.lines`` (contract D1) once imported, ``False`` when the module is absent (a failed import
#: is not cached by Python and costs a path scan each time).
_DESK_LINES: Any = None
#: The learned stores of contracts M1 and D1 in the stamp, once their modules are looked up: ``(meanings.stamp or
#: None, deskhabits.STATE_KEY or "")`` (none without them).
_MORE: Any = None
#: ``(db path, purpose) -> (built_at, stamp, entries as JSON)``
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
    """What the slow part depends on (one query of maxima, and the meanings' own stamp: contract M1; content-free):
    a nightly write of meanings or desk habits shows on the next card, not a minute later."""
    global _MORE
    if _MORE is None:
        try:
            from yuki.memory.meanings import stamp as more
        except Exception:  # noqa: BLE001 - not merged yet
            more = None
        try:
            from yuki.memory.deskhabits import STATE_KEY as desk_key
        except Exception:  # noqa: BLE001
            desk_key = ""
        _MORE = (more, str(desk_key or ""))
    more, desk_key = _MORE
    try:
        row = store._query(
            "SELECT (SELECT count(*) || ':' || coalesce(max(updated_at), 0) FROM skills WHERE origin != 'capability'),"
            " (SELECT coalesce(max(updated_at), 0) FROM routines), (SELECT coalesce(max(updated_at), 0) FROM tastes),"
            " (SELECT coalesce(max(updated_at), 0) FROM prefs), (SELECT coalesce(max(updated_at), 0) FROM contacts),"
            " (SELECT coalesce(max(id), 0) FROM portraits), (SELECT coalesce(max(updated_at), 0) FROM day_digests),"
            " (SELECT coalesce(max(updated_at), 0) FROM nudge_state WHERE name = ?)", (desk_key,))
        got = tuple(row[0]) if row else ()
    except Exception:  # noqa: BLE001 - an older database: the time bound alone
        return ()
    try:
        return got + tuple(more(store) or ()) if more is not None else got
    except Exception:  # noqa: BLE001
        return got


# ---------------------------------------------------------------------------------------------------------------------
# Entries: a line is ``[priority, text]``, or a list ``[priority, head, [[item, weight], ...], sep, min items]`` whose
# tail :func:`_fit` can take when the card is over. Plain lists (the slow part is cached as JSON).
# ---------------------------------------------------------------------------------------------------------------------


def _line(priority: int, text: str) -> list[Any]:
    return [int(priority), str(text)]


def _list(priority: int, head: str, items: Iterable[tuple[str, float]], *, sep: str = ", ",
          min_n: int = 1) -> list[list[Any]]:
    """A list entry (in a list: ``[]`` with no items, so callers can ``+=`` it)."""
    got = [[str(t), float(w)] for t, w in items if str(t or "")]
    return [[int(priority), str(head), got, str(sep), int(min_n)]] if got else []


def _text(entry: Sequence[Any]) -> str:
    return str(entry[1]) if len(entry) == 2 else str(entry[1]) + str(entry[3]).join(str(t) for t, _w in entry[2])


def _weight(v: dict[str, Any], meaning: str = "") -> float:
    """An item's weight from its value: a meaning or the user's own name for it, else weak (seen on fewer than
    :data:`WEAK_DAYS` days and never asked), else plain."""
    if meaning or v.get("named_by") == "user":
        return MEANT
    return WEAK if int(v.get("days") or 0) < WEAK_DAYS and not v.get("said") else PLAIN


# ---------------------------------------------------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------------------------------------------------


def _who(store: Any) -> list[list[Any]]:
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
    return [_line(4, f"Who: {'; '.join(p.rstrip('.') for p in parts)} (from memory{when})")]


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


def _live(store: Any, purpose: str, now: float) -> list[str]:
    """The live lines, every purpose: the day and what they're in the middle of (decision 3). The focus and what just
    changed are the deciders' own per-turn lines (the talker's you line; the planner's ``render(you, "thinker")``, the
    judge's ``render(you, "cards")``), so the card does not say them again."""
    you = _you_record(store)
    if not you:
        return []
    try:
        from yuki.memory.younow import day_words, middle_words

        return [_clip(t, MIDDLE_CHARS) for t in (day_words(you, now), middle_words(you, now)) if t]
    except Exception:  # noqa: BLE001
        return []


def _meaning(store: Any, kind: str, *names: Any) -> str:
    """What a thing means to the user (contract M1: ``yuki.memory.meanings.meaning_of(store, kind, name)``), by the
    first of ``names`` (its stored name, then another form of it) that has one, clipped to :data:`MEANING_CHARS`;
    ``""`` without (or before the meanings module exists: ``contacts._meaning`` imports it once)."""
    try:
        from yuki.memory.contacts import _meaning as meaning_of
    except Exception:  # noqa: BLE001
        return ""
    for name in dict.fromkeys(" ".join(str(n or "").split()) for n in names):
        got = meaning_of(store, kind, name) if name else ""
        if got:
            return _clip(got, MEANING_CHARS)
    return ""


def _with(name: str, *notes: str) -> str:
    """A thing with its notes next to it, its meaning first: ``mafex (the MacBook's projects; often)``."""
    notes = tuple(n for n in notes if n)
    return f"{name} ({'; '.join(notes)})" if notes else name


def _desk(store: Any, purpose: str, wide: bool) -> list[list[Any]]:
    """How they keep their desk (contract D1: ``yuki.memory.deskhabits.lines``, learned from the timeline's monitor
    per window stretch, each with its count). ``[]`` without (or before the module exists: imported once)."""
    global _DESK_LINES
    if _DESK_LINES is None:
        try:
            from yuki.memory.deskhabits import lines as _DESK_LINES
        except Exception:  # noqa: BLE001
            _DESK_LINES = False
    if not _DESK_LINES:
        return []
    cap = DESK_CHARS[1 if wide else 0]
    try:
        got = [_clip(x, cap) for x in _DESK_LINES(store, purpose=purpose, max_chars=cap) or () if str(x or "").strip()]
    except Exception:  # noqa: BLE001
        return []
    return _list(1, "How they keep their desk (learned from them): ", ((x, PLAIN) for x in got), sep="; ",
                 min_n=len(got))


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


def _routines_week(store: Any, n: int) -> list[list[Any]]:
    """fTalk (k_two_asks: "what time's my meeting tomorrow" on a Sunday): every active routine with its days and time,
    whatever today is ("Usually around now" names only those due today, within :data:`ROUTINE_NEAR_MIN`), earliest in
    the day first. One short line; RAM only."""
    try:
        rows = store.routines(states=["active"])
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        at = r.get("at_min")
        if at is None:
            continue
        try:
            label = store.routine_label(r) or ""
        except Exception:  # noqa: BLE001
            label = ""
        label = label or _skill_name(store, str(r.get("skill_id") or "")) or "their usual"
        dayset = str(r.get("dayset") or "")
        days = "every day" if dayset in ("", "all") else dayset
        hits, eligible = int(r.get("hits") or 0), int(r.get("eligible") or 0)
        sure = f" ({hits} of {eligible} {'days' if days == 'every day' else days})" if eligible else ""
        out.append((float(at) % 1440.0, f"{_clip(label, 40)} {days} around {_hm(float(at))}{sure}"))
    out.sort(key=lambda x: x[0])
    return _list(2, "Weekly routines: ", ((line, PLAIN) for _at, line in out[:n]), sep="; ")


def _sentences(text: str) -> list[str]:
    """``text`` cut after each sentence's stop (". ", "! ", "? "): plain punctuation, never a reading of words."""
    out: list[str] = []
    start = 0
    for i, ch in enumerate(text):
        if ch in ".!?" and (i + 1 == len(text) or text[i + 1] == " "):
            part = text[start:i + 1].strip()
            if part:
                out.append(part)
            start = i + 1
    rest = text[start:].strip()
    return out + ([rest] if rest else [])


def _portrait_routine(store: Any, cap: int, priority: int = 2) -> list[list[Any]]:
    """final-B (19 Oct, "when's my meeting tomorrow": the talker searched memory and found no time): their routine as
    memory's nightly portrait writes it (the brief's ``routine`` field, else the portrait's Routines section: when
    they sleep, work, eat and play, their usual meeting), dated by the portrait, whole sentences up to ``cap``. The
    brief keeps it out of the talker's slice because the card carries it (``brief.slice_for``), and the routines
    table's line names only what a skill does at a time. One plain line of ``priority``
    (:data:`ROUTINE_PRIORITY`): a full card drops it whole, after the lists of its priority are cut to their best
    few, rather than keep a fragment (its sentences carry no weight a cut could go by). ``[]`` without a portrait."""
    try:
        portrait = store.latest_portrait()
    except Exception:  # noqa: BLE001
        return []
    if portrait is None:
        return []
    text = " ".join(str((portrait.brief or {}).get("routine") or (portrait.sections or {}).get("routines")
                        or "").split())
    kept: list[str] = []
    for sentence in _sentences(text):
        if len(" ".join([*kept, sentence])) > cap:
            if not kept:
                kept.append(_clip(sentence, cap))
            break
        kept.append(sentence)
    if not kept:
        return []
    when = f", portrait of {datetime.fromtimestamp(float(portrait.at)):%d %b}" if portrait.at else ""
    return [_line(priority, f"Their routine (memory{when}): {' '.join(kept)}")]


def _left_off(store: Any, vals: dict[str, list[dict[str, Any]]], now: float, n: int,
              names: dict[str, str]) -> list[list[Any]]:
    """fBrain (k_resume_anime: the planner clicked through "continue watching" instead of opening the episode): on the
    ``n`` sites the user comes back to most (their pages seen on the most days, at least :data:`LEFT_OFF_MIN_DAYS`),
    the last page past the site's root they had in front there in one of their browsers (never one only on screen
    beside it), with its title and address. Their browsers are learned, not measured here: the programs their own
    visits to their sites used (the ``browser`` means on their values, what the sites line names; an app drawing its
    own embedded pages, e.g. a game store's local bundle, is none). Only an http(s) address; never a site of their work
    (:meth:`contacts.WorkMarks.page`, their own verdicts: picking up where they left off is for their own time).
    Planner only; RAM only. ``names``: the display names already read (:func:`_browser_name`)."""
    try:
        from yuki.core.appnames import shown
    except Exception:  # noqa: BLE001
        def shown(app: Any, process: Any = None) -> str:
            return str(app or "")
    browsers: set[str] = set()
    for items in vals.values():
        for v in items:
            for key in (v.get("means") or {}).get("browser") or {}:
                if key:
                    name = _browser_name(store, str(key), names)
                    browsers |= {str(key).casefold(), name.casefold(), str(shown(name) or "").casefold()}
    if not browsers:
        return []
    try:
        links = store.links(how="viewed", since=now - LEFT_OFF_DAYS * 86400.0, limit=LEFT_OFF_LINKS)
    except Exception:  # noqa: BLE001
        return []
    try:
        from yuki.memory.contacts import work_marks

        work = work_marks(store)
    except Exception:  # noqa: BLE001
        work = None
    from urllib.parse import urlsplit

    days: dict[str, set[Any]] = {}
    last: dict[str, Any] = {}
    for link in links:   # most recently seen first
        host = str(getattr(link, "host", "") or "").casefold()
        if not host or getattr(link, "attention", None) == "visible":
            continue
        if str(getattr(link, "app", "") or "").casefold() not in browsers:
            continue   # an app's own page (its embedded UI), not one a browser can open
        if work is not None and work.page(host):
            continue   # their work: never offered to pick up
        seen = days.setdefault(host, set())
        for at in (getattr(link, "first_seen", None), getattr(link, "last_seen", None)):
            try:
                seen.add(datetime.fromtimestamp(float(at)).date())
            except Exception:  # noqa: BLE001
                pass
        title = " ".join(str(getattr(link, "title", "") or "").split())
        if host in last or not title:
            continue
        try:
            parts = urlsplit(str(link.url or ""))
        except Exception:  # noqa: BLE001
            continue
        if parts.scheme.casefold() not in ("http", "https") or not parts.path.strip("/"):
            continue   # not a web address, or a site's root: no place to pick up from
        if len(str(link.url or "")) > LEFT_OFF_URL_CHARS or len(parts.query) > LEFT_OFF_QUERY_CHARS:
            continue   # an address that can't be given whole (a cut one opens the wrong page): no way back to it
        last[host] = (link, parts, title)
    picks = [(link, parts, title, host) for host, (link, parts, title) in last.items()
             if len(days.get(host) or ()) >= LEFT_OFF_MIN_DAYS]
    picks.sort(key=lambda x: (-len(days.get(x[3]) or ()), -float(x[0].last_seen or 0.0)))
    today = datetime.fromtimestamp(now).date()
    bits = []
    for link, _parts, title, _host in picks[:n]:
        day = datetime.fromtimestamp(float(link.last_seen)).date()
        gap = (today - day).days
        when = "today" if gap <= 0 else "yesterday" if gap == 1 else f"{day:%d %b}"
        bits.append(f'"{_clip(title, LEFT_OFF_TITLE_CHARS)}" ({link.url}), {when}')
    return _list(1, "Where they left off (the last page on the sites they return to most; open its address to pick "
                    "up): ", ((b, PLAIN) for b in bits), sep="; ", min_n=min(len(bits), LEFT_OFF_MIN_N))


def _skill_name(store: Any, key: str) -> str:
    if not key:
        return ""
    try:
        rows = store._query("SELECT name_ciphertext FROM skills WHERE key = ? LIMIT 1", (key,))
        return " ".join(str(store._dec(rows[0][0]) or "").split()) if rows and rows[0][0] else ""
    except Exception:  # noqa: BLE001
        return ""


def _values(store: Any, now: float) -> dict[str, list[dict[str, Any]]]:
    try:
        from yuki.skills.learn import values as V

        return V.known(store, now=now)
    except Exception:  # noqa: BLE001
        return {}


def _pick_text(p: Any, title_n: int) -> str:
    """One pick as the card names it: ``name (why)``; a video ``channel (its habit): "title" (how new)``, so the
    channel's habit never reads as the upload's."""
    name = _clip(p.name, 30)
    if p.kind == "video" and p.title and title_n:
        return (f"{name} ({p.why})" if p.why else name) + f': "{_clip(p.title, title_n)}"' + (
            f" ({p.age})" if p.age else "")
    why = ", ".join(b for b in (p.age, p.why) if b)
    return f"{name} ({why})" if why else name


def _picks_part(store: Any, now: float, wide: bool, n: int) -> tuple[str, list[str]]:
    """Phase 2.2: the picks for now (``yuki.skills.taste.now_picks``: code's candidates, scored like the recommender,
    each with its code-made why), for the deciders to choose from, and the names it shows: ``n`` in all, taken a
    rank at a time across the kinds (a channel's fresh unwatched upload, a playlist, a channel), the kind whose best
    one fits this hour most (time at this hour, comparable across kinds) first, so each kind's best comes before any
    kind's second; a channel with an upload among them goes by its upload. ``("", [])`` with none."""
    try:
        from yuki.skills import taste

        got = taste.now_picks(store, now=now)
    except Exception:  # noqa: BLE001
        return "", []
    title_n = TITLE_CHARS[1 if wide else 0]
    vids = (got.get("video") or [])[:n]
    kinds = []
    for i, (kind, label) in enumerate((("video", "unwatched uploads"), ("playlist", "playlists"),
                                       ("channel", "channels"))):
        items = [p for p in got.get(kind) or []
                 if kind != "channel" or not any(taste._same_name(p.name, v.name) for v in vids)][:n]
        if items:
            kinds.append((-max(p.fit for p in items), i, label, items))
    kinds.sort(key=lambda k: (k[0], k[1]))
    shown: list[list[Any]] = [[] for _ in kinds]
    left, rank = max(0, int(n)), 0
    while left > 0 and any(rank < len(k[3]) for k in kinds):
        for j, k in enumerate(kinds):
            if left > 0 and rank < len(k[3]):
                shown[j].append(k[3][rank])
                left -= 1
        rank += 1
    text = " | ".join(f"{k[2]}: " + "; ".join(_pick_text(p, title_n) for p in got_) for k, got_ in zip(kinds, shown)
                      if got_)
    return ("Picks for now, best first: " + text) if text else "", [p.name for got_ in shown for p in got_]


def _usually_now(store: Any, vals: dict[str, list[dict[str, Any]]], now: float, wide: bool) -> list[list[Any]]:
    """The "Usually around now" line, most decisive first: the routines due now, the picks for now
    (:func:`_picks_part`), their other things at this hour (seen on :data:`HOUR_MIN_DAYS` days or more; a value the
    picks name is not named twice). At most :data:`USUAL_CHARS`: the picks give way first (fewer, down to
    :data:`PICKS_MIN`), then their things at this hour."""
    from yuki.skills.learn import values as V

    try:
        from yuki.skills.taste import _same_name
    except Exception:  # noqa: BLE001
        def _same_name(a: Any, b: Any) -> bool:
            return False
    i = 1 if wide else 0
    routines = _routines_near(store, now)
    head = ["Routines: " + "; ".join(routines)] if routines else []
    hour_vals = []
    for items in vals.values():
        for v in items:
            if v.get("everyday") is False:
                continue
            w = V.hour_weight(v, now)
            if w >= HOUR_SHARE and int(v.get("days") or 0) >= HOUR_MIN_DAYS:
                hour_vals.append((w * float(v.get("score") or 0.0), v))
    hour_vals.sort(key=lambda x: -x[0])
    line = picks = ""
    things: list[str] = []
    for n in range(PICKS_N[i], min(PICKS_N[i], PICKS_MIN) - 1, -1):
        picks, names = _picks_part(store, now, wide, n)
        kept = [v for _s, v in hour_vals if not any(_same_name(v.get("name"), x) for x in names)][:4]
        things = ["Their things at this hour: " + ", ".join(
            f"{_clip(v.get('name'), 30)} ({v.get('slot')}, {_sure_days(int(v.get('days') or 0))})"
            for v in kept)] if kept else []
        parts = [*head, *([picks] if picks else []), *things]
        line = "Usually around now: " + " | ".join(parts) if parts else ""
        if len(line) <= USUAL_CHARS[i] or not picks:
            break
    if len(line) > USUAL_CHARS[i] and things:
        line = "Usually around now: " + " | ".join([*head, *([picks] if picks else [])])
    return [_line(1, _clip(line, USUAL_CHARS[i]))] if line else []


def _browser_name(store: Any, key: str, names: dict[str, str]) -> str:
    if not key:
        return ""
    if key not in names:
        try:
            names[key] = str(store.app_display_name(key) or key)
        except Exception:  # noqa: BLE001
            names[key] = key
    return names[key]


def _habits(store: Any, vals: dict[str, list[dict[str, Any]]], now: float, purpose: str,
            ways: Sequence[dict[str, Any]]) -> list[list[Any]]:
    """Their habits, most decisive first: how they keep their desk (D1), what they usually do around now, their weekly
    routines, their own ways (C4)."""
    wide = purpose != "talker"
    i = 1 if wide else 0
    out = _desk(store, purpose, wide)
    out += _usually_now(store, vals, now, wide)
    out += _routines_week(store, ROUTINES_N[i])
    out += _portrait_routine(store, ROUTINE_CHARS[i], ROUTINE_PRIORITY.get(purpose, 2))
    out += _list(3, "Their own ways (learned from them; run only when they ask, never offered): ",
                 ((x, PLAIN) for x in _ways(ways, WAYS_N[i])), sep="; ")
    return out


def _weak_last(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Values in their order, the weak ones (seen on fewer than :data:`WEAK_DAYS` days, never asked) after the rest."""
    return sorted(items, key=lambda v: int(v.get("days") or 0) < WEAK_DAYS and not v.get("said"))


def _things(store: Any, vals: dict[str, list[dict[str, Any]]], now: float, wide: bool,
            ways: Sequence[dict[str, Any]], names: dict[str, str]) -> list[list[Any]]:
    """Their things, each with what it means to them (contract M1) next to it."""
    i = 1 if wide else 0
    out: list[list[Any]] = []
    sites = []
    for v in _weak_last(vals.get("web.open_page") or [])[:SITES_N[i]]:
        # the usual browser is the preference's own answer (``values.known``): what Yuki opens the site in
        usual = _browser_name(store, str((v.get("usual") or {}).get("browser") or ""), names)
        label = v.get("label") or v.get("value") or v.get("name")
        meaning = _meaning(store, "site", label, v.get("value"), v.get("name"))
        called = f"they call it {_clip(v.get('name'), 24)}" if v.get("named_by") == "user" and \
            v.get("name") != label else ""
        sites.append((_with(_clip(label, 28), meaning, called) + (" [work]" if v.get("everyday") is False else "")
                       + (f" in {usual}" if usual else "") + f" ({_sure_days(int(v.get('days') or 0))})",
                       _weight(v, meaning)))
    out += _list(2, "Sites they open (with their usual browser; most used first): ", sites, min_n=3)
    out += _browsers(store, vals, names)
    out += _list(3, "Meeting tabs they bring up: ", (
        (_with(_clip(v.get("label") or v.get("name"), 28), _meaning(store, "site", v.get("label"), v.get("name")))
         + (f" in {_browser_name(store, str(v['usual']['browser']), names)}"
            if (v.get("usual") or {}).get("browser") else "")
         + f" ({_sure_days(int(v.get('days') or 0))})", _weight(v)) for v in (vals.get("browser.site_tab") or [])[:4]))
    try:
        from yuki.skills import taste

        out += _list(2, "YouTube channels they watch (most watched first): ", _channels(store, now, CHANNELS_N[i]),
                     min_n=3)
        bits = []
        for d in taste.described(store, "spotify_playlist", now=now, limit=PLAYLISTS_N[i])[:PLAYLISTS_N[i]]:
            called = [c for c in (d.get("called") or []) if c][:2]
            plays = d.get("plays")
            meaning = _meaning(store, "playlist", d.get("name"))
            bits.append((_with(_clip(d.get("name"), 28), meaning,
                               f"they call it {' / '.join(called)}" if called else "",
                               "played often" if isinstance(plays, (int, float)) and plays >= 3 else ""),
                         MEANT if meaning or called else PLAIN))
        out += _list(2, "Spotify playlists: ", bits, min_n=2)
    except Exception:  # noqa: BLE001
        pass
    out += _places(store, now, VOICE_N[i], SERVERS_N[i])
    out += _way_things(store, ways, WAY_THINGS_N[i])
    out += _list(3, "Usual means: ", ((x, MEANT) for x in _usuals(store, 4 if not wide else 8)), sep="; ")
    return out


def _browsers(store: Any, vals: dict[str, list[dict[str, Any]]], names: dict[str, str]) -> list[list[Any]]:
    """Their browsers, most used first (the browser counts on their sites, ``means``), each with what it means to them
    (contract M1: which is the casual one); only when at least one has a meaning (else the sites line says enough)."""
    used: dict[str, int] = {}
    for items in vals.values():
        for v in items:
            for option, k in ((v.get("means") or {}).get("browser") or {}).items():
                if option:
                    used[str(option)] = used.get(str(option), 0) + int(k or 0)
    bits = []
    for key, _k in sorted(used.items(), key=lambda kv: -kv[1])[:4]:
        shown = _browser_name(store, key, names)
        meaning = _meaning(store, "browser", shown, key)
        bits.append((_with(_clip(shown, 24), meaning), MEANT if meaning else PLAIN))
    if not any(w >= MEANT for _b, w in bits):
        return []
    return _list(2, "Their browsers (most used first): ", bits, min_n=1)


def _channels(store: Any, now: float, n: int) -> list[tuple[str, float]]:
    """The YouTube channels the user watches, most watched first (``tastes.n_obs``: decayed watch units, kTaste), each
    by its display name (``Store.yt_channels``: a feed's or oEmbed's name) with the handle the slot takes when it
    differs, and what it means to them (contract M1); then the names their own app showed with no watch yet (weak).
    ``(text, weight)``; RAM only."""
    from yuki.skills import taste

    rows = [t for t in taste.top(store, "youtube_handle", now=now, n=10_000) if str(t.name or "").strip()]
    rows.sort(key=lambda t: (-t.n_obs, t.entity_key))
    rows = rows[:n]
    try:
        known = store.yt_channels([t.entity_key for t in rows]) if rows else {}
    except Exception:  # noqa: BLE001
        known = {}
    out: list[tuple[str, float]] = []
    have: set[str] = set()
    for t in rows:
        handle = " ".join(str(t.name).split())
        name = " ".join(str((known.get(t.entity_key) or {}).get("name") or "").split()) or handle
        same = "".join(name.casefold().split()) == "".join(handle.lstrip("@").casefold().split())
        meaning = _meaning(store, "channel", name, handle)
        out.append((_with(_clip(name, 26), meaning, "" if same else _clip(handle, 24)), MEANT if meaning else PLAIN))
        have.add("".join(name.casefold().split()))
    for name in taste.seen_names(store, "youtube_handle") if len(out) < n else ():
        if len(out) >= n:
            break
        if "".join(name.casefold().split()) not in have:
            meaning = _meaning(store, "channel", name)
            out.append((_with(_clip(name, 26), meaning), MEANT if meaning else WEAK))
    return out


def _places(store: Any, now: float, n_voice: int, n_servers: int) -> list[list[Any]]:
    """Per app that joins calls (catalog data: ``end = "call_join"``): the voice channels they join (tastes
    ``<app>_voice``) and the ones memory saw them in (``yuki.skills.discord_route.voice_channels``, kHands merge note
    5) with each one's server, most days on a call there first (:data:`USE_DAYS`, the count said), then the servers
    they chat in (:func:`_servers`), each with what it means to them (contract M1). RAM only."""
    try:
        from yuki.skills.catalog import capabilities
        from yuki.skills.menu import voice_channels

        apps = sorted({str(s.app) for s in capabilities().skills if str(getattr(s, "end", "")) == "call_join" and
                       s.app})
    except Exception:  # noqa: BLE001
        return []
    out: list[list[Any]] = []
    for app in apps:
        title = app.capitalize()
        # final-B (19 Oct, "which VC do I usually hop in at night"): the list was recent first with no counts, and
        # the newest channel read as the usual. Every channel memory knows (the ones they asked Yuki to join, the
        # ones it saw them in) is now ranked by its days with a call there over the last USE_DAYS, with that count
        # next to it; one with no call there in that window follows, in the old order, weak.
        names: dict[str, tuple[str, str]] = {}
        try:
            from yuki.skills import taste

            for x in taste.candidates(store, f"{app}_voice", now=now, by="n_obs", limit=max(n_voice, 12)):
                if str(x or "").strip():
                    names.setdefault(_squash_name(x), (" ".join(str(x).split()), ""))
        except Exception:  # noqa: BLE001
            pass
        seen = voice_channels(store, app)
        for vc in seen:
            if vc.name:
                key = _squash_name(vc.name)
                got = names.get(key)
                names[key] = (got[0], got[1] or vc.place or "") if got else (" ".join(vc.name.split()),
                                                                               vc.place or "")
        use = _channel_use(store, now, [n for n, _p in names.values()], title)
        ranked = sorted(enumerate(names.items()), key=lambda e: (
            -int((use.get(e[1][0]) or {}).get("days") or 0), -int((use.get(e[1][0]) or {}).get("nights") or 0),
            -float((use.get(e[1][0]) or {}).get("last") or 0.0), e[0]))
        voice: list[tuple[str, float]] = []
        for _i, (key, (name, place)) in ranked[:n_voice]:
            u = use.get(key) or {}
            days = int(u.get("days") or 0)
            meaning = _meaning(store, "voice_channel", name)
            count = (f"{days} of {int(USE_DAYS)} days" + (f", {int(u['nights'])} at night" if u.get("nights")
                                                            else "")) if days else ""
            voice.append((_with(_clip(name, 30), meaning, f"in {_clip(place, 30)}" if place else "", count),
                          MEANT if meaning else PLAIN if days else WEAK))
        counted = any((use.get(k) or {}).get("days") for _i, (k, _v) in ranked[:n_voice])
        out += _list(2, f"{title} voice channels they join "
                        f"({'most used first' if counted else 'recent first'}): ", voice, min_n=2)
        try:
            out += _servers(store, app, title, seen, now, n_servers)
        except Exception:  # noqa: BLE001 - the voice channels stand alone
            pass
    return out


def _squash_name(name: Any) -> str:
    return " ".join(str(name or "").split()).casefold()


def _channel_use(store: Any, now: float, names: list[str], app: str) -> dict[str, dict[str, Any]]:
    """Each of ``names``' (voice channels') days with a call there over the last :data:`USE_DAYS`
    (``talker_digest.channel_use``: their days, how many at night), keyed by :func:`_squash_name`. ``{}`` without."""
    if not names:
        return {}
    try:
        from yuki.memory.talker_digest import channel_use

        got = channel_use(store, now, names, app=app, days=USE_DAYS)
    except Exception:  # noqa: BLE001
        return {}
    return {_squash_name(v.get("name")): v for v in got.values()}


def _servers(store: Any, app: str, title: str, seen: Sequence[Any], now: float, n: int) -> list[list[Any]]:
    """The servers they chat in on one call-joining app (the part before ": " of their chats' scopes: the capture's
    own "<server>: <channel>" form, as ``yuki.brain.context.read_places``), then the servers of the voice channels
    memory saw them in; ranked by their days with a capture in that server's chats over the last :data:`USE_DAYS`
    (final-B: the list was recent first), each with that count and what it means to them (contract M1). RAM only."""
    from yuki.memory.checkins import user_day
    from yuki.memory.store import app_key
    from yuki.skills.chat import app_data

    try:
        keys = sorted({app_key(str(p)) or "" for p in app_data(app).get("processes") or []} - {""})
        rows = store._query(f"SELECT id, scope_ciphertext FROM threads WHERE process IN ({','.join('?' * len(keys))})"
                            " AND merged_into IS NULL AND kind = 'conversation' AND scope_ciphertext IS NOT NULL"
                            " ORDER BY last_seen DESC LIMIT 200", keys) if keys else []
    except Exception:  # noqa: BLE001
        rows = []
    servers: list[str] = []
    threads: dict[int, str] = {}
    for tid, ct in rows:
        try:
            scope = " ".join(str(store._dec(ct) or "").split())
        except Exception:  # noqa: BLE001
            continue
        head, sep, _rest = scope.partition(": ")
        if sep and head:
            threads[int(tid)] = head
            if head not in servers:
                servers.append(head)
    for vc in seen:
        if vc.place and vc.place not in servers:
            servers.append(vc.place)
    days: dict[str, set[str]] = {}
    if threads:
        try:
            got = store._query(f"SELECT thread_id, at FROM captures WHERE thread_id IN ({','.join('?' * len(threads))})"
                               " AND at >= ?", (*threads, now - USE_DAYS * 86400.0))
        except Exception:  # noqa: BLE001
            got = []
        for tid, at in got:
            if int(tid) in threads and at:
                days.setdefault(threads[int(tid)], set()).add(user_day(float(at)))
    ranked = sorted(enumerate(servers), key=lambda e: (-len(days.get(e[1], ())), e[0]))[:n]
    bits = []
    for _i, x in ranked:
        k = len(days.get(x, ()))
        meaning = _meaning(store, "server", x)
        bits.append((_with(_clip(x, 30), meaning, f"{k} of {int(USE_DAYS)} days" if k else ""),
                     MEANT if meaning else PLAIN if k else WEAK))
    counted = any(days.get(x) for _i, x in ranked)
    return _list(3, f"{title} servers they're in ({'most used first' if counted else 'recent first'}): ",
                 bits, min_n=2)


def _live_ways(store: Any) -> list[dict[str, Any]]:
    """Contract C4: the user's own ways as the route runs them (``yuki.skills.learn.ways.live_ways``). RAM only."""
    try:
        from yuki.skills.learn.ways import live_ways

        return live_ways(store)
    except Exception:  # noqa: BLE001
        return []


def _way_things(store: Any, ways: Sequence[dict[str, Any]], n: int) -> list[list[Any]]:
    """The user's things inside their ways' apps (a way's item slot: ``spec["item"]["things"]``, the groups the rows
    they pick sit under, e.g. their projects in a coding app, by the naming call's noun), most days first, one line per
    app, each with what it means to them (contract M1, kind ``way_group``: "mafex (the MacBook's projects)"), and the
    app's own meaning when it has one."""
    try:
        from yuki.skills.learn.ways import app_title, group_words
    except Exception:  # noqa: BLE001
        return []
    by_app: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for w in ways:
        spec = w.get("spec") or {}
        things = [t for t in ((spec.get("item") or {}).get("things") or []) if isinstance(t, dict) and t.get("name")]
        if not things:
            continue
        noun, have = by_app.setdefault(str(w.get("app") or ""), (group_words(spec), []))
        names = {t["name"] for t in have}
        have.extend(t for t in things if t["name"] not in names)
    out: list[list[Any]] = []
    for app, (noun, things) in by_app.items():
        things.sort(key=lambda t: (-int(t.get("days") or 0), -int(t.get("picks") or 0)))
        bits = []
        for t in things[:n]:
            meaning = _meaning(store, "way_group", t["name"])
            bits.append((_with(_clip(t["name"], 28), meaning,
                               _sure_days(int(t.get("days") or 0)) if t.get("days") else ""),
                         MEANT if meaning else _weight(t)))
        title = app_title(app)
        out += _list(2, f"Their {noun} in {_with(title, _meaning(store, 'app', title, app))} (the ones they pick in; "
                        "most days first): ", bits, min_n=3)
    return out


def _ways(ways: Sequence[dict[str, Any]], n: int) -> list[str]:
    """Contract C4: the learned ways, name, app and how sure (the days of their demonstrations, or the user told it);
    run only when asked (a way is never offered)."""
    try:
        from yuki.skills.learn.ways import app_title
    except Exception:  # noqa: BLE001
        return []
    out = []
    for w in list(ways)[:n]:
        name = " ".join(str(w.get("name") or "").split())
        if not name:
            continue
        prov = w.get("prov") or {}
        days = int(prov.get("days") or 0)
        sure = "they told Yuki" if prov.get("told") else _sure_days(days) if days else ""
        app = app_title(str(w.get("app") or ""))
        where = "" if not app or app.split(" (")[0].casefold() in name.casefold() else f" in {app}"
        out.append(_clip(name, 50) + where + (f" ({sure})" if sure else ""))
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


def _people(store: Any) -> list[list[Any]]:
    """The people block (``contacts.directory`` without its own header: the whole :data:`PEOPLE_CHARS` is the
    people's; closest first: chats and calls outside work count most)."""
    try:
        from yuki.memory.contacts import directory

        text = directory(store, chars=PEOPLE_CHARS, head=False).strip()
    except Exception:  # noqa: BLE001
        return []
    return [_line(1, f"People (closest first; how speech hears their name; where they talk):\n{text}")] if text else []


def _about_known(store: Any, items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The brief's live items without a call promise not known to be about something (phase 3, 3.4: an unclear "we'll
    play that" never reaches a card; ``cardjudge.about_known_items``). The brief keeps an item's id only, so a
    promise's origin is read here (one content-free query); one whose origin is unread counts as a call promise
    (fails closed, as ``about_known_items`` does)."""
    ids = set()
    for x in items:
        prefix, _sep, number = str(x.get("ref") or "").partition(":")
        if prefix == "commit" and number.strip().isdigit():
            ids.add(int(number))
    if not ids:
        return list(items)
    try:
        rows = store._query("SELECT id, origin FROM conversation_facts WHERE id IN (%s)" % ",".join("?" * len(ids)),
                            tuple(ids))
        origin = {int(r[0]): str(r[1] or "") for r in rows}
    except Exception:  # noqa: BLE001 - unread: every promise counts as a call's
        origin = {}

    def source(ref: Any) -> str:
        prefix, _sep, number = str(ref or "").partition(":")
        if prefix != "commit" or not number.strip().isdigit():
            return ""
        return "call" if origin.get(int(number), "call") == "call" else "chat"

    tagged = [SimpleNamespace(ref=x.get("ref"), source=source(x.get("ref")), item=x) for x in items]
    try:
        from yuki.memory.cardjudge import about_known_items

        kept = about_known_items(store, tagged)
    except Exception:  # noqa: BLE001
        kept = [t for t in tagged if t.source != "call"]
    return [t.item for t in kept]


def _open(store: Any, n: int) -> list[list[Any]]:
    try:
        portrait = store.latest_portrait()
        brief = dict(portrait.brief) if portrait and portrait.brief else {}
    except Exception:  # noqa: BLE001
        return []
    items = _about_known(store, [x for x in brief.get("now") or [] if isinstance(x, dict) and x.get("text")])[:n]
    return _list(1, "Open for them (dated): ", (
        (f"{x.get('date') or ''}: {_clip(x['text'], OPEN_ITEM_CHARS)}".strip(": "), PLAIN) for x in items), sep="; ",
        min_n=len(items))


def _lately(store: Any) -> list[list[Any]]:
    try:
        days = store.day_digests(limit=2)
    except Exception:  # noqa: BLE001
        return []
    return _list(1, "Lately: ", ((f"{d.get('day')}: {_first_sentence(d.get('shown') or d.get('text'), LATELY_CHARS)}",
                                  PLAIN) for d in days[:2] if d.get("shown") or d.get("text")), sep=" | ")


# ---------------------------------------------------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------------------------------------------------

HEAD = ("[What Yuki knows about the user, code-built from memory, for choosing for them and reading their hints. Each "
        "line says how sure; their own words always win.]")


def _slow(store: Any, purpose: str, now: float) -> list[list[Any]]:
    """The entries of everything but the live lines, in the page's order (what they're waiting for, their habits,
    their things, their people); a lower priority gives way last (:func:`_fit`)."""
    wide = purpose != "talker"
    vals = _values(store, now)
    ways = _live_ways(store)
    out = _open(store, OPEN_N[wide])          # the talker's too (decision 3: dated, for choosing)
    if wide:
        out += _lately(store)
    out += _habits(store, vals, now, purpose, ways)
    names: dict[str, str] = {}   # app key -> display name, read once for the card
    out += _things(store, vals, now, wide, ways, names)
    if purpose == "planner":
        out += _left_off(store, vals, now, LEFT_OFF_N, names)
    if wide:
        # the talker has the people directory in its profile layer (900 chars); "what did I do" stays a lookup
        out += _people(store)
        out += _who(store)
    return out


def _fit(entries: Sequence[Sequence[Any]], cap: int) -> str:
    """The card at most ``cap`` characters, its lines in the page's order. When it is over (never the live lines,
    priority 0), by priority from the least: the lists give their tails (the longest list first and in it, past its
    first few - its minimum: those always stay - the last item of the lowest weight: a weak signal, then a plain one,
    one with a meaning last), then that priority's plain lines go (never the first priority's here). A priority gives
    its tails only once every line two or more priorities below it is gone (by priority from the least, a list holding
    a meaning after the others, the last on the page first): the first priority (Open, Lately, the desk, Usually now,
    Left off) keeps its items while a third-priority list (meeting tabs, servers, their own ways) stands, while the
    second's lists (their things) are only cut to their best few by then, never gone. Then whole lines go the same way,
    by priority from the least, the first priority's plain lines (Usually now, People) last of all. Then the room a
    dropped line left is filled back evenly (the list with the fewest items first) while items fit. Trimming before
    dropping one priority down: a list cut to its best few says more than a line gone."""
    es = [[e[0], e[1], [list(x) for x in e[2]], *e[3:]] if len(e) > 2 else list(e) for e in entries]
    alive = [True] * len(es)
    taken: list[tuple[int, int, list[Any]]] = []
    levels = sorted({int(e[0]) for e in es if int(e[0]) > 0}, reverse=True)

    def size() -> int:
        return len(HEAD) + sum(len(_text(e)) + 1 for e, a in zip(es, alive) if a)

    def trim(p: int) -> None:
        while size() > cap:
            lists = [i for i, e in enumerate(es) if alive[i] and int(e[0]) == p and len(e) > 2 and len(e[2]) > e[4]]
            if not lists:
                break
            i = max(lists, key=lambda i: (len(_text(es[i])), i))
            items, keep = es[i][2], int(es[i][4])
            low = min(w for _t, w in items[keep:])
            k = max(k for k, (_t, w) in enumerate(items) if k >= keep and w == low)
            taken.append((i, k, items.pop(k)))
        for i in range(len(es) - 1, -1, -1):   # a plain line of this priority (no tail to give) before the next
            if size() <= cap or p <= 1:
                break
            if alive[i] and int(es[i][0]) == p and len(es[i]) == 2:
                alive[i] = False

    def drop(p: int) -> None:
        # the lists first (holding no meaning, then holding one), the plain lines of the first priority last
        order = sorted((i for i, e in enumerate(es) if int(e[0]) == p),
                       key=lambda i: (len(es[i]) == 2, len(es[i]) > 2 and any(w >= MEANT for _t, w in es[i][2]), -i))
        for i in order:
            if size() <= cap:
                break
            alive[i] = False

    for p in levels:
        for q in levels:
            if q >= p + 2:
                drop(q)   # the lines two or more priorities below go before this one gives a tail
        trim(p)
    for p in levels:
        drop(p)
    # fill the room back, evenly: the item taken from the list that now has the fewest first
    left = [t for t in reversed(taken) if alive[t[0]]]
    while left:
        j = min(range(len(left)), key=lambda j: (len(es[left[j][0]][2]), j))
        i, k, item = left.pop(j)
        if size() + len(str(es[i][3])) + len(str(item[0])) <= cap:
            es[i][2].insert(k, item)
    body = [_text(e) for e, a in zip(es, alive) if a]
    return (HEAD + "\n" + "\n".join(body))[:cap] if body else ""


def build_you_card(store: Any, *, purpose: str = "talker", now: float | None = None) -> str:
    """The you-card for one decider (``purpose`` in :data:`PURPOSES`): ``""`` when nothing is known. Model-free; the
    slow part cached in RAM (:data:`CACHE_S`, or until a store it reads changes); the live lines read fresh. Raises
    ``ValueError`` for an unknown purpose only."""
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {', '.join(PURPOSES)}, not {purpose!r}")
    now = time.time() if now is None else float(now)
    # the talker's: one less than the mouth's cut, which counts a newline after the last line too (TALKER_CHARS)
    cap = TALKER_CHARS - 1 if purpose == "talker" else WIDE_CHARS
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
    return _fit([*(_line(0, x) for x in _live(store, purpose, now)), *slow], cap)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


__all__ = ["PURPOSES", "TALKER_CHARS", "WIDE_CHARS", "CACHE_S", "build_you_card", "clear_cache"]
