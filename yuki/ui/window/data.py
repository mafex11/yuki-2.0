"""What the Yuki window shows, read from memory: plain view models, no Qt.

Everything here only reads (``MemoryClient`` and its ``Store``, the privacy
file, ``voice.json``, the Run key). Nothing calls a model or writes. Each
builder is called on a worker thread when a page opens and hands the page a
dataclass; the page only paints it.

Phase-1 note: a few reads go one level below ``MemoryClient`` (the day's raw
timeline rows, journal entries by id, conversation facts, the latest session's
turns, a 14-day per-app ranking for stable lane colours). Phase 2 moves them
behind ``MemoryClient`` methods (see docs/UI-window.md, "Phase 2").
"""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# -- small formatting helpers -------------------------------------------------------


def dur(seconds: float, *, short: bool = False) -> str:
    """``4h 06m`` / ``35m`` / ``40s`` (``short``: ``4h`` when minutes round away)."""
    s = int(round(max(0.0, seconds)))
    if s < 60:
        return f"{s}s"
    h, m = divmod(s // 60, 60)
    if h and short and m == 0:
        return f"{h}h"
    return f"{h}h {m:02d}m" if h else f"{m}m"


def hm(at: float) -> str:
    return datetime.fromtimestamp(at).strftime("%H:%M")


def day_label(at: float, *, today: date | None = None) -> str:
    d = datetime.fromtimestamp(at).date()
    today = today or date.today()
    if d == today:
        return "today"
    if d == today - timedelta(days=1):
        return "yesterday"
    return f"{d.day} {d:%b}"


def epoch(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def first_sentence(text: str, limit: int = 160) -> str:
    """The first sentence of ``text``, cut at a word under ``limit`` characters."""
    text = " ".join((text or "").split())
    m = re.search(r"(?<=[.!?])\s+(?=[A-Z\"'(])", text)
    s = text[: m.start()] if m else text
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "…"


_EPISODE_TIME = re.compile(r"^\s*\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}\s*(\([^)]*\))?\s*:\s*", re.UNICODE)


def episode_gist(text: str, limit: int = 90) -> str:
    """An episode's first sentence without its ``HH:MM-HH:MM:`` prefix, capitalised."""
    body = _EPISODE_TIME.sub("", text or "").strip()
    s = first_sentence(body, limit)
    return s[:1].upper() + s[1:] if s else s


# Portrait and journal text is written about "the user" (third person). The
# window speaks to the user: a sentence that starts with the user becomes a note
# ("The user works at X" -> "Works at X"), a possessive becomes "your", and the user
# further in becomes "you" (with the verb turned: "the user was" -> "you were").
# A fixed grammar pass, presentation only. Phase 2: the portrait render returns a
# second-person line itself.
_ADVERBS = r"(?:also |often |sometimes |currently |usually |still |now |then |recently |regularly |frequently |mostly |has |had |is |was )?"
_LEAD = re.compile(r"(^|(?<=[.!?;]\s))The user (" + _ADVERBS + r")([a-z]+)\b")
_MID = re.compile(r"\bthe user (" + _ADVERBS.replace("has |had |is |was ", "") + r")([a-z]+)\b")


def _base_verb(word: str) -> str | None:
    irregular = {"is": "are", "was": "were", "has": "have", "does": "do", "says": "say"}
    if word in irregular:
        return irregular[word]
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if re.search(r"(ss|sh|ch|x|z|o)es$", word):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return None


def second_person(text: str) -> str:
    """"The user works at X" -> "Works at X"; "the user's brother" -> "your brother"; "owes the user" -> "owes you"."""
    t = text or ""
    t = re.sub(r"\bThe user's\b", "Your", t)
    t = re.sub(r"\bthe user's\b", "your", t)
    t = _LEAD.sub(lambda m: m.group(1) + (m.group(2) + m.group(3))[:1].upper() + (m.group(2) + m.group(3))[1:], t)

    def mid(m: re.Match) -> str:
        verb = _base_verb(m.group(2)) if not m.group(1) else None
        if m.group(2) in ("is", "was", "has", "does", "says"):
            verb = _base_verb(m.group(2))
        return "you " + m.group(1) + (verb or m.group(2))

    t = _MID.sub(mid, t)
    t = re.sub(r"\bthe user\b", "you", t)
    t = re.sub(r"\bThe user\b", "You", t)
    return t


# -- config files (read only) ------------------------------------------------------


def _localappdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def read_privacy(path: Path | None = None) -> dict[str, Any]:
    path = path or _localappdata() / "Yuki" / "memory" / "privacy.toml"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def read_voice_prefs(path: Path | None = None) -> dict[str, Any]:
    path = path or _localappdata() / "Yuki" / "voice.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# -- Today: the day's timeline -----------------------------------------------------


@dataclass
class Segment:
    """One stretch of the day (a timeline row clipped to the day)."""

    start: float
    end: float
    lane: str
    state: str                 # present | away
    present_s: float
    fullscreen: bool = False
    voice: bool = False        # the app in front held the microphone (a meeting row)
    private: bool = False      # blocked by privacy: no title, no page
    by_yuki: bool = False
    title: str | None = None
    host: str | None = None


@dataclass
class Lane:
    key: str
    label: str
    present_s: float = 0.0
    fullscreen_s: float = 0.0
    voice_s: float = 0.0
    visits: int = 0
    color: int | None = None   # index into the theme's categorical series


@dataclass
class Band:
    """A span drawn across the chart: a call, the PC off/asleep, or nothing recorded."""

    kind: str                  # call | off | asleep | unknown | gap | away
    start: float
    end: float
    label: str = ""
    app: str = ""
    real_start: float | None = None   # a call's true start/end, when outside the day
    real_end: float | None = None
    detail: str = ""
    drops: list[tuple[float, float]] = field(default_factory=list)
    seconds: float = 0.0       # a call's measured length (microphone held), whole call


@dataclass
class Session:
    """A stretch with one dominant activity (the episode worker's windows, joined when the same app led)."""

    start: float
    end: float
    lane: str
    title: str
    present_s: float
    apps: list[tuple[str, float]]
    episodes: list[str]
    facts: list[dict[str, Any]]
    longest: float = 0.0       # the longest episode inside (its gist is the title)


@dataclass
class DayView:
    day: date
    since: float
    until: float
    end: float
    lanes: list[Lane]
    segments: list[Segment]
    calls: list[Band]
    power: list[Band]
    gaps: list[Band]
    sessions: list[Session]
    totals: dict[str, float]
    summary: str
    milestones: list[dict[str, Any]]
    facts: list[dict[str, Any]]
    first_at: float | None
    last_at: float | None
    lane_of_other: str = "other"


MAX_LANES = 7


def _lane_key(row: Any) -> tuple[str, str]:
    if row.withheld and not row.app:
        return "other", "Other apps"
    key = row.process or (row.app or "").casefold() or "other"
    return key, row.app or row.process or "Other apps"


def _color_ranks(store: Any, until: float, days: int = 14) -> dict[str, int]:
    """Lane colour = the app's rank by time over the last ``days`` (stable across days)."""
    try:
        rows = store._query(
            "SELECT COALESCE(process, app) k, SUM(COALESCE(active_s,0)+COALESCE(passive_s,0)) t FROM timeline "
            "WHERE started_at >= ? AND state = 'present' GROUP BY k ORDER BY t DESC LIMIT 8",
            (until - days * 86400.0,))
        return {str(r["k"]).casefold(): i for i, r in enumerate(rows) if r["k"]}
    except Exception:
        return {}


def build_day(memory: Any, day: date, *, now: float | None = None) -> DayView:
    """The whole Today page for ``day`` (read-only)."""
    from yuki.memory.store import POWER_STATES

    store = memory.store
    now = time.time() if now is None else now
    since = datetime.combine(day, datetime.min.time()).timestamp()
    until = since + 86400.0
    end = min(until, now)
    rows = sorted(store.timeline_between(since, until), key=lambda r: r.started_at)
    act = memory.activity(since=since, until=until, group_by="app", limit=50)

    segments: list[Segment] = []
    lanes: dict[str, Lane] = {}
    covered: list[tuple[float, float]] = []
    for r in rows:
        s, e = max(r.started_at, since), min(r.ended_at, end)
        if e <= s:
            continue
        covered.append((s, e))
        if r.state in POWER_STATES:
            continue
        frac = (e - s) / max(1e-6, r.ended_at - r.started_at)
        present = ((r.active_s or 0.0) + (r.passive_s or 0.0)) * frac if r.state == "present" else 0.0
        key, label = _lane_key(r)
        lane = lanes.setdefault(key, Lane(key, label))
        if r.state == "present":
            lane.present_s += present
            lane.fullscreen_s += (e - s) if r.fullscreen else 0.0
            lane.voice_s += (e - s) if r.meeting else 0.0
        segments.append(Segment(s, e, key, r.state, present, bool(r.fullscreen), bool(r.meeting),
                                bool(r.withheld), bool(getattr(r, "by_yuki", False)), r.title, r.host))

    ranked = sorted((ln for ln in lanes.values() if ln.key != "other"), key=lambda ln: -ln.present_s)
    top = ranked[:MAX_LANES]
    top_keys = {ln.key for ln in top}
    other = Lane("other", "Other apps")
    for ln in ranked[MAX_LANES:] + ([lanes["other"]] if "other" in lanes else []):
        other.present_s += ln.present_s
    for seg in segments:
        if seg.lane not in top_keys:
            seg.lane = "other"
    # visits: runs of consecutive present segments per lane
    prev = None
    for seg in segments:
        if seg.state == "present" and seg.lane != prev:
            (lanes.get(seg.lane) or other).visits += 1 if seg.lane in top_keys else 0
        prev = seg.lane if seg.state == "present" else prev
    ranks = _color_ranks(store, until)
    for ln in top:
        ln.color = ranks.get(ln.key.casefold())
    # an app outside the 14-day top 8 takes a free slot rather than grey, while slots remain
    free = [i for i in range(8) if i not in {ln.color for ln in top}]
    for ln in top:
        if ln.color is None and free:
            ln.color = free.pop(0)
    lane_list = top + ([other] if other.present_s > 0 else [])

    power = [Band(p["kind"], epoch(p["start"]) or since, epoch(p["end"]) or since,
                  {"off": "PC off", "asleep": "Asleep", "unknown": "PC stopped"}.get(p["kind"], p["kind"]))
             for p in act.get("power", [])]
    # nothing recorded while the PC was on: holes between rows of a minute or more
    gaps: list[Band] = []
    cursor = None
    for s, e in sorted(covered):
        if cursor is not None and s - cursor >= 60.0:
            gaps.append(Band("gap", cursor, s, "Not recorded"))
        cursor = e if cursor is None else max(cursor, e)

    calls = []
    for c in act.get("calls", []):
        rs, re_ = epoch(c.get("start")), epoch(c.get("end"))
        if rs is None or re_ is None:
            continue
        calls.append(Band("call", max(rs, since), min(re_, end), f"{c.get('app')} call", c.get("app") or "",
                          rs, re_, c.get("text") or "",
                          [(epoch(d["start"]) or 0.0, epoch(d["end"]) or 0.0) for d in c.get("drops", [])],
                          float(c.get("seconds") or 0.0)))

    journal = store.journal_between(since, until)
    facts = [{"id": j.id, "at": j.at, "app": j.app, "text": j.fact, "nature": j.nature, "importance": j.importance,
              "by_yuki": j.by_yuki, "milestone": j.milestone} for j in journal]
    milestones = [f for f in facts if f["milestone"]]
    sessions = _sessions(memory, since, until, segments, facts, {ln.key: ln.label for ln in lane_list})

    t = act["totals"]
    first_at, last_at = epoch(t.get("first_at")), epoch(t.get("last_at"))
    call_s = sum(c.end - c.start for c in calls)
    totals = {"present_s": t.get("present_s", 0.0), "away_s": t.get("away_s", 0.0),
              "switches": t.get("switches", 0), "call_s": call_s, "off_s": t.get("off_s", 0.0),
              "asleep_s": t.get("asleep_s", 0.0), "fullscreen_s": t.get("fullscreen_s", 0.0),
              "gap_s": sum(g.end - g.start for g in gaps)}
    top3 = ", ".join(f"{ln.label} {dur(ln.present_s)}" for ln in top[:3])
    summary = (f"At your PC {dur(totals['present_s'])}, away {dur(totals['away_s'])}. "
               f"Most time in {top3}.")
    longest = max(calls, key=lambda c: c.seconds, default=None)
    if longest is not None:
        summary += f" Longest call: {longest.app}, {dur(longest.seconds)}."
    return DayView(day, since, until, end, lane_list, segments, calls, power, gaps, sessions, totals, summary,
                   milestones, facts, first_at, last_at)


def _sessions(memory: Any, since: float, until: float, segments: list[Segment], facts: list[dict[str, Any]],
              labels: dict[str, str]) -> list[Session]:
    """Sessions: the episode worker's windows (the finest that tile the day), each led by the app
    that held most of it; neighbours led by the same app, less than a break apart, read as one."""
    from yuki.memory.timeline import BREAK_S

    eps = [(epoch(e["start"]) or 0.0, epoch(e["end"]) or 0.0, e["text"]) for e in memory.episodes(since, until)]
    # drop an episode that contains finer ones (a "full window" summary over its parts)
    fine = [e for e in eps if not any(o is not e and e[0] <= o[0] and o[1] <= e[1] and (o[1] - o[0]) < (e[1] - e[0])
                                      for o in eps)]
    fine.sort(key=lambda e: e[0])

    def lead(s: float, e: float) -> tuple[str, float, list[tuple[str, float]]]:
        per: Counter = Counter()
        for seg in segments:
            if seg.state != "present" or seg.end <= s or seg.start >= e:
                continue
            overlap = min(seg.end, e) - max(seg.start, s)
            per[seg.lane] += seg.present_s * overlap / max(1e-6, seg.end - seg.start)
        if not per:
            return "", 0.0, []
        top = per.most_common()
        return top[0][0], sum(per.values()), [(labels.get(k, k), v) for k, v in top]

    out: list[Session] = []
    for s, e, text in fine:
        s, e = max(s, since), min(e, until)
        lane, present, apps = lead(s, e)
        if not lane:
            continue
        if out and out[-1].lane == lane and s - out[-1].end < BREAK_S:
            last = out[-1]
            last.end = e
            last.episodes.append(text)
            _, last.present_s, last.apps = lead(last.start, e)
            if e - s > last.longest:
                last.title, last.longest = episode_gist(text), e - s
            continue
        out.append(Session(s, e, lane, episode_gist(text), present, apps, [text], [], e - s))
    for sess in out:
        sess.facts = [f for f in facts if sess.start <= f["at"] < sess.end and f["nature"] != "uncertain"]
    return out


# -- Me: the portrait, with where each item came from ------------------------------


@dataclass
class Evidence:
    at: float
    app: str
    text: str
    nature: str


@dataclass
class Item:
    """One thing Yuki believes about the user, with where it came from."""

    ref: str                   # "fact:69" (portrait fact) or "journal:512"
    title: str                 # the subject ("Bro", "Corvana")
    text: str                  # second person, full
    short: str                 # first sentence
    kind: str
    aspect: str | None
    origin: str                # user | model
    since: float
    confidence: float | None
    source: str                # "You told Yuki · 25 Sep" / "From Discord · 25 Sep"
    tags: list[str] = field(default_factory=list)   # joke | story | unsure | you
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class MeView:
    work: list[Item]
    interests: list[Item]
    routines: list[Item]
    people: list[Item]
    personality: dict[str, list[Item]]
    unsure: list[Item]
    on_your_plate: list[Item]
    preferences: list[Item]
    portrait_at: float | None
    questions_open: int


#: The portrait's personality aspects, as the page names them.
ASPECT_TITLES = {"talk": "How you talk", "cares": "What you care about", "joke": "Running jokes",
                 "story": "Stories you tell", "people": "How you are with people"}

#: Below this (the model's own confidence, 0-1) an inferred item says "Yuki isn't sure".
UNSURE_BELOW = 0.4


def _source_line(origin: str, since: float, evidence: list[Evidence], today: date | None) -> str:
    when = day_label(max([since] + [e.at for e in evidence]), today=today)
    if origin == "user":
        return f"You told Yuki · {when}"
    apps = Counter(e.app for e in evidence if e.app)
    if apps:
        where = apps.most_common(1)[0][0]
        where = "a call" if where.startswith("call") else where
        return f"Seen in {where} · {when}"
    return f"Yuki's reading of your week · {when}"


def build_me(memory: Any, *, today: date | None = None) -> MeView:
    store = memory.store
    facts = store.portrait_facts(include_corrections=False)
    ids = sorted({i for f in facts for i in f.source_ids})
    entries = {j.id: j for j in store.journal_by_ids(ids)} if ids else {}

    def item(f: Any) -> Item:
        ev = [Evidence(entries[i].at, entries[i].app, entries[i].fact, entries[i].nature)
              for i in f.source_ids if i in entries]
        ev.sort(key=lambda e: -e.at)
        tags = []
        if f.aspect == "joke":
            tags.append("joke")
        if f.aspect == "story":
            tags.append("story")
        if f.origin == "user":
            tags.append("you")
        elif f.confidence is not None and f.confidence < UNSURE_BELOW:
            tags.append("unsure")
        text = second_person(f.text)
        return Item(f"fact:{f.id}", f.subject, text, first_sentence(text, 180), f.kind, f.aspect, f.origin,
                    f.valid_from, f.confidence, _source_line(f.origin, f.valid_from, ev, today), tags, ev)

    by_kind: dict[str, list[Item]] = defaultdict(list)
    for f in facts:
        by_kind[f.kind].append(item(f))
    for items in by_kind.values():
        items.sort(key=lambda i: (i.origin != "user", -(i.confidence or 0.5), -i.since))
    personality: dict[str, list[Item]] = {k: [] for k in ASPECT_TITLES}
    for it in by_kind.get("personality", []):
        personality.setdefault(it.aspect or "talk", []).append(it)
    # things said that may be misheard: the journal's "uncertain" items of the last week
    week = time.time() - 7 * 86400.0
    unsure = []
    for j in store.journal_between(week, None):
        if j.nature == "uncertain":
            text = second_person(j.fact)
            unsure.append(Item(f"journal:{j.id}", j.app, text, first_sentence(text, 180), "journal", None, "model",
                               j.at, None, f"Heard in {'a call' if j.app.startswith('call') else j.app} · "
                               f"{day_label(j.at, today=today)}", ["unclear"],
                               [Evidence(j.at, j.app, j.fact, j.nature)]))
    portrait = store.latest_portrait()
    open_q = len(memory.questions(statuses=("open", "asked"), limit=50))
    return MeView(by_kind.get("work", []), by_kind.get("interest", []), by_kind.get("behaviour", []),
                  by_kind.get("person", []), personality, unsure[-4:], by_kind.get("open_loop", []),
                  by_kind.get("preference", []), portrait.at if portrait else None, open_q)


# -- Yuki: its side ------------------------------------------------------------------


@dataclass
class YukiView:
    persona: list[str]
    talk: list[str]
    rules: list[dict[str, Any]]
    knowhow: list[str]
    promised: list[dict[str, Any]]      # Yuki's own commitments
    reminding: list[dict[str, Any]]     # things the user said they'd do, that Yuki will remind about
    waiting: list[dict[str, Any]]       # questions waiting to be asked
    asked: list[dict[str, Any]]         # asked, with the answer if any


#: Who Yuki is, in the words of its own system prompt (yuki/agent/prompt.py).
PERSONA = [
    "Lives on your PC and gets things done on it: your hands and eyes on this machine, not a chatbot.",
    "Quick, uses its own judgement, and talks like a capable friend rather than a service desk.",
    "Reaches for Windows' own tools (PowerShell, shortcuts, an app's own commands) before the mouse.",
]
#: How it uses what it knows about you (the same prompt's Personality paragraph).
TALK = [
    "Replies the way a friend texts back: usually one sentence, leading with the answer.",
    "Meets banter with banter in your register, lightly and not often; a task always comes first.",
    "Teases only with your own jokes, never about health, body, family or money.",
    "Never treats a joke, or anything it isn't sure it heard right, as a fact.",
]


def build_yuki(memory: Any) -> YukiView:
    store = memory.store
    rules = []
    for f in store.conversation_facts():
        if f.kind in ("rule", "preference") and f.status == "active":
            rules.append({"ref": f"rule:{f.id}", "kind": f.kind, "text": f.text, "quote": f.quote,
                          "at": f.valid_from, "origin": f.origin})
    rules.sort(key=lambda r: r["at"])
    todos = memory.todos(include_done=False)
    promised = [t for t in todos if t["source"] == "commitment"]
    reminding = [t for t in todos if t["source"] == "call"]
    qs = memory.questions(limit=30)
    waiting = [q for q in qs if q["status"] in ("open",)]
    asked = [q for q in qs if q["status"] in ("asked", "answered", "skipped", "never")]
    return YukiView(PERSONA, TALK, rules, memory.knowhow(limit=20), promised, reminding, waiting, asked[:6])


# -- Status ---------------------------------------------------------------------------


@dataclass
class StatusView:
    service_running: bool
    paused: bool
    captures_today: int
    facts_today: int
    last_capture_at: float | None
    portrait_at: float | None
    cost_today: float
    calls_waiting: dict[str, Any] | None
    oldest_wait_at: float | None
    questions_waiting: list[dict[str, Any]]
    quiet_until: float | None
    quiet_hours: tuple[str, str]
    nudges_today: dict[str, int]
    last_nudge_at: float | None
    wake_on: bool
    mic: str
    speaker: str
    start_with_windows: bool | None
    call_now: dict[str, Any] | None


def build_status(memory: Any, *, real_db: Path | None = None) -> StatusView:
    from yuki.memory.api import service_running

    st = memory.status()
    # the service's health is read from its mutex for the *real* database (a read: nothing changes)
    running = service_running(real_db) if real_db is not None else st["service_running"]
    ns = memory.nudge_status()
    privacy = read_privacy()
    nudges = privacy.get("nudges", {})
    prefs = read_voice_prefs()
    try:
        from yuki.ui import startup

        sww: bool | None = startup.is_enabled()
    except Exception:
        sww = None
    waiting = memory.call_audio_waiting()
    call = memory.call_status().get("call")
    return StatusView(
        running, st["paused"], st["captures_today"], st["facts_today"], epoch(st["last_capture_at"]),
        epoch(st["portrait_updated_at"]), st["memory_cost_today_usd"], waiting,
        epoch(waiting.get("oldest_at")) if waiting else None,
        memory.questions(statuses=("open",), limit=10), epoch(ns.get("quiet_until")),
        (nudges.get("quiet_start", "00:30"), nudges.get("quiet_end", "08:00")), ns.get("today", {}),
        epoch(ns.get("last_nudge_at")), bool(prefs.get("wake", True)),
        prefs.get("mic_device") or "System default", prefs.get("speaker_device") or "System default", sww, call,
    )


# -- Chat -------------------------------------------------------------------------------


@dataclass
class ChatView:
    turns: list[dict[str, Any]]
    nudges: list[dict[str, Any]]
    session_started: float | None


def build_chat(memory: Any, *, limit: int = 6) -> ChatView:
    store = memory.store
    latest = store.latest_turn()
    turns: list[dict[str, Any]] = []
    started = None
    if latest is not None:
        for t in store.turns_for_session(latest.session_id, limit=limit):
            turns.append({"at": t.at, "user": t.user_text, "reply": t.reply_text or "", "outcome": t.outcome,
                          "actions": t.actions})
        started = turns[0]["at"] if turns else None
    since = (started or time.time()) - 3600.0
    nudges = [n for n in memory.recent_nudges(hours=48) if (epoch(n.get("shown_at")) or 0) >= since]
    return ChatView(turns, nudges, started)


# -- Settings -----------------------------------------------------------------------------


@dataclass
class SettingsView:
    mic: str
    speaker: str
    voice_id: str
    wake_on: bool
    coach_on: bool
    quiet_start: str
    quiet_end: str
    questions_on: bool
    call_prefs: list[dict[str, Any]]
    blocked_apps: list[str]
    blocked_sites: list[str]
    paused: bool
    start_with_windows: bool | None
    model: str
    effort: str


def build_settings(memory: Any) -> SettingsView:
    from yuki.config import DEFAULT_EFFORT, DEFAULT_MODEL, MODEL_ALIASES

    privacy = read_privacy()
    nudges = privacy.get("nudges", {})
    user = privacy.get("user", {})
    prefs = read_voice_prefs()
    try:
        from yuki.ui import startup

        sww: bool | None = startup.is_enabled()
    except Exception:
        sww = None
    alias = next((a for a, m in MODEL_ALIASES.items() if m == DEFAULT_MODEL), DEFAULT_MODEL)
    return SettingsView(
        prefs.get("mic_device") or "System default", prefs.get("speaker_device") or "System default",
        prefs.get("tts_voice") or "", bool(prefs.get("wake", True)), bool(nudges.get("enabled", True)),
        nudges.get("quiet_start", "00:30"), nudges.get("quiet_end", "08:00"),
        bool(nudges.get("questions_enabled", True)), memory.call_prefs(), list(user.get("blocked_apps", [])),
        list(user.get("blocked_domains", [])), memory.status()["paused"], sww, alias.capitalize(),
        DEFAULT_EFFORT.capitalize(),
    )
