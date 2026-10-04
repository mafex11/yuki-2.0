"""Write ``yuki/ui/web/fixtures/window.js``: invented data for every page of the Lattice window.

The preview (``yuki/ui/web/preview.html`` from file://, for screenshots) and the window's ``--fixture`` mode render
it. Every name, message and number here is made up ("Sam" in Pune, a garden-planner side project, friends called
Riya and Dev); nothing comes from memory. The shapes are exactly what :mod:`yuki.ui.web_data` sends, so the preview
shows what the real window shows. Times are seconds relative to the fixture's ``now``; the preview and the window
shift them to the moment they render (``shift_times``), so "5 min ago" stays 5 minutes ago.

    python scripts/make_web_fixture.py
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yuki.ui import policy  # noqa: E402
from yuki.ui import web_data as W  # noqa: E402

OUT = ROOT / "yuki" / "ui" / "web" / "fixtures" / "window.js"
#: The fixture's own "now": Saturday 3 Oct 2026, 21:52 local (any fixed epoch works; the times are shifted).
NOW = 1791051120.0
M, H, D = 60.0, 3600.0, 86400.0


def ago(seconds: float) -> float:
    return round(NOW - seconds, 1)


def buckets() -> list[dict]:
    """The day in 144 ten-minute slices: share at the PC, the mode, calls (invented, a plausible Saturday)."""
    out = []
    now_slot = int((21 * 60 + 52) / 10)
    for i in range(144):
        m = i * 10
        b = {"p": 0.0, "m": "off", "c": 0}
        if i > now_slot:
            b["m"] = "future"
        elif m < 90:                                    # 00:00-01:30 late evening at the desk
            b.update(p=0.55 + 0.35 * math.sin(i * 1.3) ** 2, m="active")
        elif m < 9 * 60 + 40:                           # asleep: PC off
            b["m"] = "off"
        elif m < 12 * 60 + 30:                          # morning work
            b.update(p=0.62 + 0.3 * abs(math.sin(i * 0.7)), m="active")
        elif m < 13 * 60 + 20:
            b.update(p=0.18, m="away")
        elif m < 17 * 60 + 10:                          # afternoon work
            b.update(p=0.5 + 0.4 * abs(math.sin(i * 0.45)), m="active")
        elif m < 18 * 60 + 40:
            b.update(p=0.1, m="away")
        elif m < 20 * 60 + 30:                          # evening: a game, full screen
            b.update(p=0.86 + 0.1 * abs(math.sin(i)), m="full")
        else:
            b.update(p=0.48 + 0.35 * abs(math.sin(i * 0.9)), m="active")
        if 19 * 60 <= m < 20 * 60 + 10:
            b["c"] = 1
        if m >= 21 * 60 + 6 and i <= now_slot:
            b["c"] = 2
        b["p"] = round(b["p"], 2)
        out.append(b)
    return out


def week_hours() -> list[dict]:
    days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    rows = []
    for n, dow in enumerate(days):
        hours, pc = [], 0
        for hh in range(24):
            k, v = "o", 0.0
            work = (10 <= hh <= 12 or 14 <= hh <= 17) and dow not in ("Sun",)
            if work:
                k, v = "a", 0.55 + 0.4 * abs(math.sin(hh * 1.7 + n))
            elif 19 <= hh <= 20 and n in (1, 3, 5, 6):
                k, v = "f", 0.8
            elif hh in (21, 22) and n % 2 == 0:
                k, v = "c", 0.6
            elif hh in (13,) and work is False and dow != "Sun":
                k, v = "w", 0.4
            elif 21 <= hh <= 23:
                k, v = "a", 0.3 + 0.2 * abs(math.sin(n + hh))
            if n == 6 and hh > 21:
                hours.append({"k": "o", "v": 0, "fu": True})
                continue
            hours.append({"k": k, "v": round(v, 2)})
            pc += v * 60 if k in ("a", "f") else 0
        rows.append({"dow": dow, "today": n == 6, "pc": round(pc), "title": ""})
        rows[-1]["hours"] = hours
    return rows


def day_label(days_ago: int) -> str:
    from datetime import datetime, timedelta

    d = datetime.fromtimestamp(NOW).date() - timedelta(days=days_ago)
    return f"{d.day} {d:%b}"


def rhythm(*counts: int | None) -> tuple[list, dict]:
    """A person's 14 days (today last) and the words around the figure, as ``web_data.rhythm_view`` sends them: the
    invented memory began reading chats 9 days ago (the first 5 days are unknown)."""
    days = [None] * (14 - len(counts)) + list(counts)
    known = [v for v in days if v is not None]
    peak = max(known) if known else 0
    watch = next((i for i, v in enumerate(days) if v is not None), None)
    info = {"total": sum(known), "known": len(known), "active": sum(1 for v in known if v), "peak": peak,
            "peak_day": day_label(13 - days.index(peak)) if peak else "", "start_label": day_label(13),
            "watch_index": watch, "watch_label": day_label(13 - watch) if watch else "", "mine": None, "theirs": None}
    return days, info


#: The invented voice room the fixture's calls were in (never a real server).
ROOM = "Mumble, #seed-swap voice (Allotment Club)"


def _ev(at: float, app: str, call: int | None = None) -> dict:
    return {"at": at, "app": app, "text": "", "nature": "fact", "call": f"call:{call}" if call else None}


def _fact(fid: int, kind: str, subject: str, summary: str, shown: str, *, ago_s: float, ev: list[dict],
          origin: str = "model", confidence: float = 0.7, aspect: str | None = None, pinned: bool = False,
          importance: int = 4) -> dict:
    return {"id": fid, "ref": f"fact:{fid}", "kind": kind, "subject": subject, "text": shown, "summary": summary,
            "shown": shown, "aspect": aspect, "origin": origin, "confidence": confidence, "since": ago(ago_s),
            "created_at": ago(ago_s), "pinned": pinned or origin == "user", "reviewed_at": None,
            "importance": importance, "evidence": ev}


class InventedMemory:
    """A stand-in memory with invented facts, people and calls (Sam in Pune, the garden planner, friends Riya, Dev,
    Mira, Noor, Theo and Ines): the methods web_data.build_me reads, in memory's own shapes (yuki.memory.api)."""

    def people_list(self, limit: int = 40) -> list[dict]:
        def route(app, kind, chat, handle, mine, theirs, last, preferred=False):
            return {"app": app, "kind": kind, "chat": chat, "handle": handle, "mine": mine, "theirs": theirs,
                    "last_mine": last, "last_theirs": last, "preferred": preferred}

        def summary(fid, headline, text, pinned=False):
            return {"ref": f"fact:{fid}", "headline": headline, "text": text, "pinned": pinned}

        return [
            {"ref": "person:2", "name": "Riya", "relation": "Close friend, from the climbing gym", "aliases": ["riyaclimbs"],
             "usual": "Signal chat. A call on Signal when it’s long.", "last_contact": ago(52 * M),
             "routes": [route("Signal", "dm", "Riya", "riyaclimbs", 78, 47, ago(52 * M), True),
                        route("Telegram", "group", "Climbing crew", "", 12, 31, ago(1 * D + 1 * H)),
                        route("Thunderbird", "email", "", "riya@…", 2, 1, ago(9 * D))],
             "summary": summary(21, "Riya tests the garden planner", "You talk most evenings. She’s testing the garden planner on her balcony beds and sends you photos of what grew.")},
            {"ref": "person:5", "name": "Dev", "relation": "Your brother", "aliases": [], "usual": "Email, short and plain.",
             "last_contact": ago(2 * H), "routes": [route("Thunderbird", "email", "", "dev@…", 18, 13, ago(2 * H), True)],
             "summary": summary(22, "Dev has your old laptop", "You gave him your old laptop on Thursday, wiped and set up. He answers in one line.", pinned=True)},
            {"ref": "person:3", "name": "Mira", "relation": "Your sister", "aliases": ["mimi"], "usual": "Telegram, mornings.",
             "last_contact": ago(1 * D + 3 * H), "routes": [route("Telegram", "dm", "Mira", "mimi", 22, 26, ago(1 * D + 3 * H), True)],
             "summary": summary(23, "Mira’s birthday is on 14 October", "Her birthday is on 14 October. You looked at two book shops and a candle place, nothing ordered yet.")},
            {"ref": "person:8", "name": "Noor", "relation": "Bandmate, runs the seed swap", "aliases": ["noor.plays"],
             "usual": "Signal for quick things; a voice call to plan the swap.", "last_contact": ago(1 * D + 6 * H),
             "routes": [route("Signal", "dm", "Noor", "noor", 9, 10, ago(1 * D + 6 * H), True)],
             "summary": summary(24, "Noor runs the seed swap", "She runs the seed swap on the 18th, and you play in her band on Wednesdays.")},
            {"ref": "person:4", "name": "Theo", "relation": "Your landlord", "aliases": [], "usual": "Email only.",
             "last_contact": ago(2 * D), "routes": [route("Thunderbird", "email", "", "theo@…", 5, 7, ago(2 * D), True)],
             "summary": summary(25, "Theo owes you the new rent figure", "You’re waiting on his new figure for the lease before you compare it with two listings you saved.")},
            {"ref": "person:9", "name": "Ines", "relation": "Study group", "aliases": [], "usual": None,
             "last_contact": ago(4 * D), "routes": [],
             "summary": summary(26, "Ines is in your Rust study group", "You meet her online for the Rust study group, every other Sunday.")},
        ][:limit]

    def portrait_facts_with_evidence(self, evidence_per_fact: int = 6) -> list[dict]:
        today = [_ev(ago(h * H), "VS Code") for h in (1, 3)]

        def week(app: str, days: tuple[int, ...], at: tuple[int, int] | None = None) -> list[dict]:
            """Evidence on those days ago: 2 h before NOW's time of day, or at ``at`` (hour, minute) of each day."""
            if at is None:
                return [_ev(ago(d * D + 2 * H), app) for d in days]
            from datetime import datetime, timedelta

            midnight = datetime.fromtimestamp(NOW).replace(hour=0, minute=0, second=0, microsecond=0)
            return [_ev(round((midnight - timedelta(days=d)).timestamp() + at[0] * H + at[1] * M, 1), app) for d in days]

        call = [_ev(ago(1 * D + 5 * H), "call:mumble", 7)]
        return [
            _fact(12, "work", "Garden planner", "Garden planner app, in TypeScript",
                  "You’re building a garden planner app in your spare time, in TypeScript. On 2 October you finished the bed sizes screen.",
                  ago_s=2 * H, ev=today, importance=7, pinned=True),
            _fact(13, "open_loop", "Seed swap", "You’ll confirm who brings the tables",
                  "On 1 October, on a call with Noor in #seed-swap voice, you said you’d confirm who brings the tables for the swap on the 18th.",
                  ago_s=1 * D + 5 * H, ev=call, importance=6),
            _fact(15, "work", "Watering timer", "The watering timer isn’t started",
                  "The schedule screen is half built; the timer logic isn’t started yet.", ago_s=3 * H, ev=today),
            _fact(18, "work", "Budget", "October budget: rent and groceries in",
                  "Your budget sheet has September closed. October has rent and groceries in; the train tickets aren’t.",
                  ago_s=6 * H, ev=[_ev(ago(6 * H), "Calc")]),
            _fact(19, "work", "Photo backup", "Phone photos backed up to August",
                  "The phone photos up to August are on the external drive. September is still only on the phone.",
                  ago_s=1 * D + 2 * H, ev=[_ev(ago(1 * D + 2 * H), "Files")]),
            _fact(20, "work", "Chart library", "You’re choosing a chart library",
                  "For the planner’s watering chart you compared two chart libraries on 2 October and haven’t picked one.",
                  ago_s=1 * D + 1 * H, ev=[_ev(ago(1 * D + 1 * H), "Firefox")]),
            _fact(27, "work", "Old laptop", "Test the planner on the old laptop",
                  "You want to try the planner on a slow laptop before you share it.", ago_s=2 * D, ev=week("Notes", (2,))),
            _fact(28, "open_loop", "Lease", "Compare the lease with two listings",
                  "Once Theo sends the new figure, you’ll compare it with the two listings you saved.", ago_s=2 * D + 3 * H,
                  ev=week("Thunderbird", (2,))),
            _fact(21, "person", "Riya", "Riya tests the garden planner", "You talk most evenings.", ago_s=3 * D,
                  ev=week("Signal", (0, 1, 2))),
            _fact(22, "person", "Dev", "Dev has your old laptop", "He answers in one line.", ago_s=4 * D,
                  ev=week("Thunderbird", (2,)), origin="user"),
            _fact(14, "interest", "Climbing", "You climb twice a week",
                  "You climb twice a week at the gym near the station, usually with Riya.", ago_s=6 * D,
                  ev=[_ev(ago(6 * D), "Yuki")], origin="user", confidence=0.95),
            _fact(17, "interest", "Rust", "Learning Rust, a chapter a week",
                  "You’re learning Rust from the book, one chapter a week; on 3 October you were on chapter 12.", ago_s=5 * H,
                  ev=[_ev(ago(5 * H), "Firefox")]),
            _fact(29, "interest", "Music", "Lo-fi playlists while you code",
                  "You put on lo-fi playlists in Music while you code, most afternoons.", ago_s=2 * D, ev=week("Music", (0, 1, 2))),
            _fact(30, "preference", "Replies", "Plain replies, no greeting",
                  "You like replies short and plain, with no greeting.", ago_s=5 * D, ev=[_ev(ago(5 * D), "Yuki")], origin="user"),
            _fact(16, "personality", "Basil", "Your basil “has a personality”",
                  "A joke you keep making about the basil on the balcony. A joke, never a fact.", ago_s=2 * D,
                  ev=[_ev(ago(2 * D), "Signal")], aspect="joke"),
            _fact(35, "personality", "Learning", "You learn by building",
                  "You’d rather build a small thing than read a chapter first.", ago_s=4 * D, ev=week("VS Code", (4,)), aspect="cares"),
            _fact(36, "personality", "Writing", "Lowercase, short messages to friends",
                  "You write to friends in lowercase, short lines.", ago_s=3 * D, ev=week("Signal", (3,)), aspect="talk"),
            _fact(31, "routine", "Evenings", "Evenings at the desk from 8:30 PM",
                  "Most evenings you sit down at the PC from about 8:30 PM and often stay past midnight.", ago_s=5 * D,
                  ev=week("VS Code", (0, 1, 2, 4, 5, 6), at=(20, 40)), confidence=0.86),
            _fact(32, "routine", "Games", "A game most weekday evenings",
                  "On most weekday evenings you play a game full screen, roughly 7 PM to 8:30 PM.", ago_s=6 * D,
                  ev=week("Hollow Knight", (2, 3, 4, 5), at=(19, 5)), confidence=0.72),
            _fact(33, "routine", "Mornings", "Late starts at weekends",
                  "At weekends you’re first at the PC after 9:30 AM.", ago_s=6 * D, ev=week("Firefox", (0, 6)), confidence=0.58),
            _fact(34, "behaviour", "Seed packets", "Sorted seed packets on 1 October",
                  "On 1 October you spent about an hour sorting the seed packets for the swap.", ago_s=2 * D,
                  ev=[_ev(ago(2 * D), "Notes")], confidence=0.6),
            _fact(40, "work", "Moving", "You might move flats in spring",
                  "On 2 October you looked at two flat listings and said you might move in spring.", ago_s=20 * H,
                  ev=[_ev(ago(20 * H), "Firefox")], confidence=0.62),
            _fact(41, "person", "Noor", "Noor plays bass in your band",
                  "On 1 October, on a call in #seed-swap voice with Noor and maybe Dev, you said Noor plays bass in your band.",
                  ago_s=1 * D + 5 * H, ev=call, confidence=0.35),
        ]

    def review_queue(self, now: float | None = None) -> dict:
        return {"items": [{"ref": "fact:40", "headline": "You might move flats in spring", "flag": "new",
                           "text": "On 2 October you looked at two flat listings and said you might move in spring."},
                          {"ref": "fact:41", "headline": "Noor plays bass in your band", "flag": "unsure",
                           "text": "On 1 October, on a call in #seed-swap voice with Noor and maybe Dev, you said Noor plays bass in your band."}],
                "cap": 5, "reviewed_today": 1, "left_today": 4, "waiting": 9, "merged": {"count": 3, "since": ago(7 * D)}}

    def misheard(self, days: float = 7, limit: int = 8, now: float | None = None) -> dict:
        # the feedback round 3 (W6): only real mishearings, each with what was heard and the guess (memory's shapes)
        return {"items": [
            {"id": 812, "ref": "journal:812", "at": ago(1 * D + 5 * H), "app": "Mumble", "nature": "uncertain",
             "text": "The user said they would bring the hair loom seeds.", "call": "call:7",
             "source": {"quote": "the hair loom seeds", "call": "call:7"}, "heard_in": ROOM, "channel_known": True,
             "people": ["Noor"], "heard": "hair loom", "guess": "heirloom", "guess_is_name": False,
             "why": "You were planning the seed swap, where people bring heirloom seeds.",
             "shown": "You said you would bring the heirloom seeds."},
            {"id": 815, "ref": "journal:815", "at": ago(5 * H), "app": "Signal", "nature": "uncertain",
             "text": "The user said nor would send the bass part.", "call": "call:9", "source": {"quote": "nor"},
             "heard_in": "Signal", "channel_known": False, "people": [], "heard": "nor", "guess": "Noor",
             "guess_is_name": True, "why": "Noor plays bass in your band and sends you the parts.",
             "shown": "You said Noor would send the bass part."}], "total": 2}

    def resolve_heard(self, ref, verdict, *, correction_id=None) -> dict:
        return {"ok": True}

    def correct_item(self, ref, words) -> dict:
        return {"ok": True, "ref": ref, "correction_id": 91, "state": "updating"}

    def pin_fact(self, ref, on=True) -> dict:
        return {"ok": True, "pinned": on}

    def rules(self) -> list[dict]:
        def rule(rid: int, text: str, days: int, kind: str) -> dict:
            return {"ref": f"rule:{rid}", "text": text, "at": ago(days * D), "origin": "user", "kind": kind}

        return [rule(4, "Don’t call me “buddy”.", 8, "preference"),
                rule(6, "Keep replies short unless I ask for detail.", 7, "preference"),
                rule(9, "Never cost me frames in a game or interrupt a call.", 6, "rule"),
                rule(11, "My to-dos are mine: bring one up only if I ask.", 3, "rule")]

    def calls_in(self, since=None, until=None, limit=30) -> list[dict]:
        return [{"ref": "call:7", "call_ids": [7], "app": "Mumble", "places": [ROOM], "start": ago(1 * D + 6 * H),
                 "end": ago(1 * D + 5 * H), "people": [{"name": "Noor", "how": "on_call_screen"},
                                                       {"name": "noor.plays", "how": "call_chat"},
                                                       {"name": "Dev", "how": "likely"}], "gists": []}]

def day_name(days_ago: int) -> str:
    """Memory's name for a user day (``YYYY-MM-DD``) some days before the fixture's now."""
    from datetime import datetime, timedelta

    return f"{datetime.fromtimestamp(NOW).date() - timedelta(days=days_ago):%Y-%m-%d}"


def call_status(call_start: float) -> dict:
    """An invented ``call_status()`` (memory's shape since report 60): a Signal call recorded on both sides, the
    mic and Signal's own sound. The window's call words are built from it by the real builders."""
    row = {"id": 12, "app": "Signal", "kind": "call", "status": "recording", "asking": False, "consent": "always",
           "headphones": True, "session_started_at": call_start, "detected_at": call_start,
           "recording_since": call_start}
    return {"call": row, "recording": True, "waiting": {"calls": 2, "minutes": 94.0},
            "sessions": [{"id": 12, "app": "Signal", "kind": "call", "status": "recording", "asking": False,
                          "ended": False}],
            "mic": {"state": "recording", "call_id": 12, "reason": None, "speech_s": 1080.0},
            "tracks": [{"call_id": 12, "app": "Signal", "process": "signal.exe", "track_kind": "call",
                        "state": "recording", "reason": None, "speech_s": 940.0}],
            "pending_card": None}


def build() -> dict:
    call_start = ago(46 * M + 4)
    shell_call = W.call_view(call_status(call_start))
    now_call, call_running = W.home_call(shell_call, None)
    rh = {"Riya": rhythm(8, 13, 17, 6, 20, 15, 11, 23, 9), "Dev": rhythm(3, 0, 1, 4, 0, 2, 5, 3, 2),
          "Mira": rhythm(1, 3, 0, 4, 2, 5, 0, 2, 1), "Noor": rhythm(0, 2, 5, 1, 0, 2, 3, 1, 4),
          "Theo": rhythm(0, 1, 0, 0, 2, 0, 1, 3, 0), "Ines": rhythm(0, 0, 0, 2, 0, 1, 0, 0, 0)}
    run_start = ago(41)
    autonomy = policy.describe(dict(policy.DEFAULTS))
    shell = {
        "user": {"name": "Sam", "initial": "S"}, "memory_line": "memory on", "connected": True,
        "voice": {"state": "off", "wake": True, "conversation": False, "quiet_until": 0},
        "call": shell_call,
        "runs": [{"item": "a41", "jobs": [31], "title": "“Rename the March invoices by client”",
                  "line": "Step 3 · Renaming an invoice", "started_at": run_start}],
        "hotkey_words": "Alt Shift", "needs": 2,
    }
    home = {
        "now": {
            # the current work thread (the 4 Oct decision: it replaced "In front")
            "thread": {"ref": "thread:12", "title": "Garden planner: watering schedule", "app": "VS Code", "since_at": ago(42 * M),
                       "min": None, "in_front": True, "next": "Write the timer that waters each bed on its own days",
                       "shown": "The schedule screen is half built; the timer logic isn’t started."},
            "playing": {"status": "paused", "title": "Rain Study No. 4", "artist": "The Lo-Fi Trio", "app": "Music"},
            "call": now_call,
            "goals": [{"ref": "G7", "text": "finish the watering schedule screen", "status": "open", "done": False, "thread": "W12"},
                      {"ref": "G8", "text": "reply on the seed-swap thread", "status": "unknown", "done": False, "thread": "W15"},
                      {"ref": "G6", "text": "order the seed trays", "status": "done", "done": True, "thread": None}],
        },
        "needs": [
            {"kind": "question", "ref": "q:7", "at_label": "2 Oct, 12:19 PM", "title": "Is the Friday planning call still at 10?",
             "why": "You moved it once last week. The weekly review should have the right time."},
            {"kind": "question", "ref": "q:9", "at_label": "today, 9:41 AM", "title": "The “beds v2” sketch from Thursday: keep it, or was it a test?",
             "why": "It was saved twice with the same name. It may be a scratch copy."},
        ],
        "running": [
            *call_running,
            {"kind": "waiting", "live": False, "title": "2 calls waiting to be turned into text",
             "detail": "1h 34m in all. A full-screen game had the GPU, so they wait.", "meta": "waiting for the GPU"},
        ],
        "offers": [
            {"card": "41", "at": ago(25 * M), "title": "Your work", "do": "todo_add", "label": "Add to list", "open": True, "undo": True,
             "text": "Next on the garden planner is the watering schedule: the bed sizes are in, the timings aren’t. Due Monday.",
             "chips": [["VS Code", "21:27"], ["Notes", "1 Oct"]],
             "will": "Add “Write the watering schedule” to your list, due Mon 5 Oct.", "wont": "Won’t open, type into or send anything."},
            {"card": "43", "at": ago(18 * M), "title": "Noticed", "do": "bring_back", "label": "Bring back the forum", "open": True, "undo": True,
             "text": "The seed-swap forum thread you were reading at 20:07 has two new replies to your question.",
             "chips": [["Firefox", "20:07"], ["2 replies", "since then"]],
             "will": "Open the thread again where you left it.", "wont": "Won’t type into or send anything."},
            {"card": "44", "at": ago(9 * M), "title": "From memory", "do": "ask", "label": "Edit in ask bar", "open": True, "undo": False,
             "text": "Your budget sheet hasn’t been saved since the PC restarted at 06:26. Last time you asked me to check it.",
             "chips": [["PC stopped", "06:26"], ["Calc", "yesterday"]],
             "will": "Put “check the budget sheet is saved” in the ask bar for you to send.", "wont": "Won’t open, type into or send anything."},
        ],
        "away": [
            {"kind": "request", "title": "Pages restored", "at": ago(28 * M), "state": "done",
             "changed": [{"text": "Firefox: 6 tabs back from before the restart", "state": "done"}], "cost": "took 4 s"},
            {"kind": "card", "title": "Added to your list", "at": ago(52 * M), "state": "done",
             "changed": [{"text": "“Order more seed trays”, due Tuesday", "state": "done",
                          "undo": {"card": "39", "until": ago(-4 * M), "label": "Undo"}}]},
            {"kind": "request", "title": "Told you what you did today", "at": ago(27 * M), "state": "done", "changed": [],
             "note": "The text showed but the voice came back empty. I’ll retry speech next time instead of going quiet.", "cost": "took 7 s"},
            {"kind": "notice", "key": "cost:today", "title": "My AI use today: about $1.84", "at": None, "when": "today",
             "state": "done", "changed": [],
             "note": "Daily notes $0.92, your profile $0.51, cards and check-ins $0.24, turning calls into text $0.17."},
        ],
        "people": [
            {"ref": "person:2", "name": "Riya", "route": "Signal", "last_at": ago(52 * M), "count": 125, "rhythm": rh["Riya"][0]},
            {"ref": "person:5", "name": "Dev", "route": "Thunderbird", "last_at": ago(2 * H), "count": 31, "rhythm": rh["Dev"][0]},
            {"ref": "person:3", "name": "Mira", "route": "Telegram", "last_at": ago(1 * D + 3 * H), "count": 48, "rhythm": rh["Mira"][0]},
            {"ref": "person:8", "name": "Noor", "route": "Signal", "last_at": ago(1 * D + 6 * H), "count": 19, "rhythm": rh["Noor"][0]},
            {"ref": "person:4", "name": "Theo", "route": "Thunderbird", "last_at": ago(2 * D), "count": 12, "rhythm": rh["Theo"][0]},
            {"ref": "person:9", "name": "Ines", "route": "Telegram", "last_at": ago(4 * D), "count": 6, "rhythm": rh["Ines"][0]},
        ],
        "since": ago(1 * H + 49 * M),
    }
    threads = [
        ("thread:12", "Garden planner: watering schedule", "active", 5 * M, "Bed sizes and soil types are in. The schedule screen is half built; the timer logic isn’t started.",
         ["Write the watering schedule", "Pick a chart library", "Test on the old laptop"], ["Riya", "Noor", "VS Code", "Firefox"]),
        ("thread:15", "Seed-swap meetup on the 18th", "active", 4 * M, "You offered to bring tomato and basil seeds. The venue is confirmed; the list of who brings what isn’t.",
         ["Reply to the forum thread", "Count the seed packets"], ["Noor", "Firefox"]),
        ("thread:9", "Flat lease renewal", "blocked", 24 * M, "Waiting on the landlord’s new figure before you can compare it with the two listings you saved.",
         ["Ask Theo for the new figure"], ["Theo", "Thunderbird"]),
        ("thread:18", "Budget for October", "active", 22 * M, "The sheet has September closed. October has rent and groceries in; the trip isn’t.",
         ["Add the train tickets", "Check the sheet is saved"], ["Calc"]),
        ("thread:7", "Learning Rust: the CLI exercise", "active", 1 * H, "Chapter 12 done. The grep clone parses arguments; the tests fail on case-insensitive search.",
         ["Fix the case-insensitive test"], ["VS Code", "Firefox"]),
        ("thread:21", "Mira’s birthday", "paused", 3 * H, "You looked at two book shops and one candle place. Nothing ordered yet; it’s on the 14th.",
         ["Pick a gift", "Order by the 10th", "Card"], ["Mira", "Firefox"]),
        ("thread:5", "Band practice recordings", "paused", 3 * H, "Three takes of the new song are in the Projects folder. You marked take 2 as the keeper.",
         ["Send take 2 to Noor"], ["Noor", "Audacity"]),
        ("thread:3", "Photo backup", "active", 2 * H, "The phone photos up to August are on the external drive. September is still only on the phone.",
         ["Copy September", "Check the drive’s free space"], ["Files"]),
    ]
    # memory's next step per thread (the feedback round 3, W1c); one not written yet (the checklist's next shows)
    nexts = {"thread:12": "Write the timer that waters each bed on its own days",
             "thread:15": "Reply on the forum with who brings what", "thread:9": "Ask Theo for the new rent figure",
             "thread:18": "Add the train tickets for the 24th", "thread:7": "Fix the case-insensitive search test",
             "thread:21": "Pick between the two books you saved", "thread:5": "Send take 2 to Noor"}
    people_names = {"Riya", "Noor", "Theo", "Mira", "Dev"}
    done_counts = {"thread:12": 3, "thread:15": 2, "thread:9": 1, "thread:18": 1, "thread:7": 2, "thread:21": 0,
                   "thread:5": 1, "thread:3": 1, "thread:2": 4}
    goals = {"thread:12": "finish the watering schedule screen", "thread:15": "reply on the seed-swap thread"}

    def checks(ref: str, open_items: list[str]) -> dict | None:
        done = done_counts.get(ref, 0)
        total = done + len(open_items)
        return {"done": done, "total": total, "next": open_items[0] if open_items else None} if total else None

    work = {
        "threads": [{"ref": r, "title": t, "status": s, "last_at": ago(a), "state": st, "next": nexts.get(r), "open": o, "involves": inv,
                     "people": [e for e in inv if e in people_names], "apps": [e for e in inv if e not in people_names],
                     "checks": checks(r, o), "goal": goals.get(r)}
                    for r, t, s, a, st, o, inv in threads],
        "closed": [{"ref": "thread:2", "title": "Old laptop for Dev", "status": "done", "closed_at": ago(2 * D), "by_you": True},
                   {"ref": "thread:4", "title": "Watching a cooking video", "status": "dismissed", "closed_at": ago(3 * D), "by_you": False}],
        "pending": {}, "can": {"close": True, "cont": True, "correct": True},
        "week": {"days": [{"dow": d, "today": i == 6, "pc": pc, "a": a, "f": f} for i, (d, pc, a, f) in enumerate(
            [("Sun", 210, 90, 40), ("Mon", 520, 360, 80), ("Tue", 470, 330, 0), ("Wed", 560, 380, 90), ("Thu", 430, 300, 0),
             ("Fri", 610, 360, 150), ("Sat", 452, 230, 114)])],
            "totals": {"pc": 3252, "a": 2050, "f": 474}},
    }
    day = {
        "date_label": "Saturday 3 Oct", "today": True, "first": "12 AM", "last": "9:49 PM", "now_min": 21 * 60 + 52,
        "off": [{"from": "1:31 AM", "to": "9:40 AM"}],
        "buckets": buckets(),
        # the feedback round 3 (W2): memory's short distinct title per session and its one- or two-sentence summary
        # (the last one not written yet: the app is the title, the narrative's first sentence the detail)
        "sessions": [
            {"a": 0, "b": 90, "start": "00:00", "end": "01:30", "title": "Seed-swap forum", "app": "Firefox", "mode": "active",
             "apps": [["Firefox", 61], ["Signal", 14]], "ref": "episode:301",
             "summary": "You read the seed-swap forum and asked who has spare basil seeds."},
            {"a": 580, "b": 750, "start": "09:40", "end": "12:30", "title": "Bed sizes screen", "app": "VS Code", "mode": "active",
             "apps": [["VS Code", 104], ["Firefox", 31], ["Notes", 9], ["Signal", 4]], "ref": "episode:305",
             "summary": "You finished the bed sizes screen of the garden planner and checked it against the beds Riya measured."},
            {"a": 800, "b": 1030, "start": "13:20", "end": "17:10", "title": "October budget and Rust", "app": "Calc", "mode": "active",
             "apps": [["VS Code", 72], ["Calc", 66], ["Firefox", 40], ["Music", 12]], "ref": "episode:309",
             "summary": "You put October\u2019s rent and groceries in the budget, then worked on the Rust exercise\u2019s search."},
            {"a": 1120, "b": 1230, "start": "18:40", "end": "20:30", "title": "Hollow Knight evening", "app": "Hollow Knight", "mode": "full",
             "apps": [["Hollow Knight", 104]], "ref": "episode:312", "summary": "You played Hollow Knight, full screen."},
            {"a": 1230, "b": 1312, "start": "20:30", "end": "21:52", "title": "VS Code", "app": "VS Code", "mode": "active",
             "apps": [["VS Code", 44], ["Signal", 12], ["Music", 6]], "ref": "episode:315",
             "full": "Garden planner: you started on the watering schedule screen."},
        ],
        "pending": {}, "can": {"correct": True},
        "calls": [
            {"a": 1140, "b": 1210, "start": "19:00", "end": "20:10", "app": "Mumble", "live": False, "recorded": True, "min": 70,
             "where": "Mumble, #seed-swap voice (Allotment Club)", "people": ["Noor", "maybe Dev"],
             "gists": ["You and Noor planned the meetup: she asks the community hall about tables.",
                       "You bring tomato and basil seeds, 20 packets each."]},
            {"a": 1266, "b": 1312, "start": "21:06", "end": "now", "app": "Signal", "live": True, "recorded": True, "min": 46,
             "where": "Signal", "people": ["Riya"], "gists": [], "started_at": call_start},
        ],
        "totals": [["At the PC", "7h 32m", "214 app switches"], ["Active", "5h 06m", "VS Code 3h 40m"], ["Full screen", "1h 44m", "Hollow Knight"],
                   ["Calls", "1h 56m", "1 on Mumble, 1 live"], ["Longest focus", "52m", "VS Code, 10:12 AM"]],
        "week_hours": week_hours(),
    }
    # Me is built by the real builder (web_data.build_me) over an invented memory, so the preview has exactly the
    # page's shapes: one summary per person, the review queue, topic groups, routines, misheard with its place
    me = W.build_me(InventedMemory(), now=NOW)
    for p in me["people"]:
        p["rhythm"], p["rhythm_info"] = rh[p["name"]]
    def proc(ref: str, title: str, steps: list[str], app: str, uses: int, last: int, conf: float, word: str, learned: str,
             when: str = "") -> dict:
        return {"ref": ref, "kind": "procedure", "title": title, "when": when, "steps": steps, "app": app, "uses": uses,
                "last": day_label(last), "conf": conf, "conf_word": word, "state": "active", "disabled": False,
                "learned": learned}

    def lesson(ref: str, dont: str, because: str, used: int, days: int, do: str = "") -> dict:
        return {"ref": ref, "kind": "style", "dont": dont, "because": because, "do": do, "used": used,
                "from": "In your words, " + day_label(days), "conf": 0.8, "disabled": False}

    # the 4 Oct item W7: procedures with numbered steps, app choices said plainly (never their counts), lessons that
    # fold to a title and one line, so the page's two columns balance
    playbook = {
        "workflows_available": True, "know_how": [],
        "learning": {"runs_24h": 9, "cost_today_usd": 0.021, "passes_today": 3},
        "proposals": [
            {"ref": "pb:31", "kind": "habit", "title": "Which chat for Riya",
             "basis": "Riya: usually the Signal chat. Seen 3 times, 30 Sep; I’ll use it once you say so.",
             "steps": [], "app": "Signal"},
        ],
        "procedures": [
            proc("pb:11", "Open a video site", ["Open Firefox", "Open a new tab", "Go to the video site"], "Firefox", 6, 0, 0.9,
                 "sure", f"From a run that worked, {day_label(6)} · seen on 4 days"),
            proc("pb:12", "Put a window on the second screen, full size", ["Move the window to display two", "Maximize it as its own step"],
                 "Any app", 2, 4, 0.78, "fairly sure", f"From a run that worked, {day_label(4)}"),
            proc("pb:13", "Message a friend", ["Ask which app: Signal or Telegram", "Open that chat", "Type the message in your style",
                                               "Wait for you to say send"], "Signal · Telegram", 5, 1, 0.82, "sure",
                 f"From a run that worked, {day_label(5)} · seen on 3 days"),
            proc("pb:14", "Scroll a Signal chat", ["Scroll the chat with Windows’ own scroll, upward", "Wait for it; Signal can take 2 s"],
                 "Signal", 3, 5, 0.72, "fairly sure", f"From a run that worked, {day_label(5)} · seen on 2 days",
                 when="Not: the page keys, they jump to the newest"),
            proc("pb:15", "Join the club meeting", ["Open the calendar", "Open today’s meetings", "Ask you: join now?",
                                                    "Join with the mic muted"], "Calendar", 2, 2, 0.7, "fairly sure",
                 f"From a run that worked, {day_label(3)}"),
            proc("pb:17", "Restore Firefox after a restart", ["Open Firefox", "Click Restore in the “Restore pages?” bubble"], "Firefox",
                 4, 0, 0.88, "sure", f"From a run that worked, {day_label(6)} · seen on 3 days"),
        ],
        "choices": [
            {"ref": "pb:19", "kind": "habit", "title": "which browser for videos", "subject": "Videos", "default": "Firefox",
             "ask": False, "line": "Videos: usually Firefox.", "app": "Firefox", "uses": 1, "last": day_label(2), "conf": 0.66,
             "conf_word": "still learning", "disabled": False, "learned": "Seen in what you do, 28 Sep · seen on 6 days"},
            {"ref": "pb:20", "kind": "habit", "title": "where the club chat is open", "subject": "The club chat", "default": "Firefox",
             "ask": False, "line": "The club chat: usually Firefox.", "app": "Firefox", "uses": 0, "last": "", "conf": 0.7,
             "conf_word": "fairly sure", "disabled": False, "learned": "Seen in what you do, 29 Sep · seen on 5 days"},
            {"ref": "pb:22", "kind": "habit", "title": "which app for Dev", "subject": "Dev", "default": "", "ask": True,
             "line": "Dev: I ask which one.", "app": "Signal · Telegram", "uses": 0, "last": "", "conf": 0.5,
             "conf_word": "still learning", "disabled": False, "learned": "Seen in what you do, 30 Sep · seen on 3 days"},
            {"ref": "pb:25", "kind": "habit", "title": "which editor for the garden planner", "subject": "The garden planner",
             "default": "VS Code", "ask": False, "line": "The garden planner: usually VS Code.", "app": "VS Code", "uses": 2,
             "last": day_label(1), "conf": 0.84, "conf_word": "sure", "disabled": False,
             "learned": "Seen in what you do, 27 Sep · seen on 7 days"},
        ],
        "lessons": [
            lesson("pb:24", "Messages to Dev", "Short and plain, no greeting; he answers in one line, so a long message just sits there.", 0, 5),
            lesson("pb:26", "The weekly seed-swap post", "Keep the list as bullet points with the packet count first, then the variety; you paste it into the forum as it is and the forum keeps line breaks.", 2, 4,
                   do="Bullets, count first, no emoji."),
            lesson("pb:27", "Music while you work", "Lo-fi or nothing with words; songs with lyrics pull you out of the code, so a playlist you didn’t name should be an instrumental one.", 3, 6),
            lesson("pb:28", "Reminders", "Say the day and the time, never “later”; a reminder without a time is one you said you never act on.", 1, 3),
            lesson("pb:29", "Notes for the allotment club", "In the shared notes, under the month heading, newest at the top.", 1, 2),
            lesson("pb:30", "Calling you by name", "Sam, not Samuel; Samuel is only for forms.", 0, 7),
            lesson("pb:32", "Screenshots", "Into the Screenshots folder, named by what they show and the day, never left on the desktop where they pile up.", 2, 5),
            lesson("pb:33", "Late at night", "Keep replies short after midnight; you read them in bed and a wall of text keeps you up.", 0, 1),
        ],
        "rules": [{"ref": r["ref"], "text": r["text"], "since": r["prov"].replace("You told me, ", ""), "kind": r["kind"]} for r in me["rules"]],
        "standing": [{"ref": "request:3", "text": "Tell me when the seed-swap thread gets a reply.", "since": "1 Oct"}],
    }
    def row(id_: str, kind: str, f: str, at: float, what: str, where: str, why: str, result: str, status: str, **kw) -> dict:
        return {"id": id_, "kind": kind, "f": f, "at": at, "what": what, "where": where, "cost": kw.pop("cost", ""),
                "because": "because", "why": why, "chips": kw.pop("chips", []), "result": result, "status": status, **kw}

    # what I did, in plain words only (the 3 Oct asks A-C: no Undo, no command or typed text), grouped by what you
    # asked through the real grouping (web_data.ledger_groups), from invented runs
    runs = [{"key": "a", "lane": "voice", "words": "What did I do today?", "outcome": "final", "ms": 7000, "cost_usd": 0.08, "at": ago(27 * M)},
            {"key": "b", "lane": "voice", "words": "bring my tabs back", "outcome": "final", "ms": 4000, "cost_usd": 0.04, "at": ago(28 * M + 5)},
            {"key": "c", "lane": "voice", "words": "remind me to water the basil sunday", "outcome": "final", "ms": 5000, "cost_usd": 0.03, "at": ago(41 * M + 3)},
            {"key": "d", "lane": "worker", "words": "open my budget", "outcome": "final", "ms": 3000, "cost_usd": 0.03, "at": ago(1 * H + 2 * M + 2)},
            {"key": "e", "lane": "voice", "words": "pause it", "outcome": "final", "ms": 12000, "cost_usd": 0.06, "at": ago(2 * H + 11 * M + 4)}]
    rows = [
        row("run:a", "request", "did", ago(27 * M), "Answered “What did I do today?”", "Yuki", "you said “What did I do today?”",
            "answered", "done", cost="7 s, 8¢"),
        row("run:b:2", "action", "did", ago(28 * M), "Clicked Restore", "Firefox",
            "you said “bring my tabs back”, using how you do it", "done, checked on screen", "verified", cost="4 s, 4¢",
            chips=[{"kind": "memory", "text": "Restore Firefox after a restart"}]),
        row("run:b:1", "action", "did", ago(28 * M + 3), "Opened Firefox", "Firefox", "you said “bring my tabs back”, using how you do it",
            "done, checked on screen", "verified", chips=[{"kind": "memory", "text": "Restore Firefox after a restart"}]),
        row("run:c:1", "action", "did", ago(41 * M), "Added to your list “Water the basil on Sunday”", "Yuki", "you said “remind me to water the basil sunday”",
            "done", "done", cost="5 s, 3¢"),
        row("c39", "card", "did", ago(52 * M), "Added “Order more seed trays” to your list", "Card · Your work",
            "you pressed Add to list on the card", "done", "done", card="39"),
        row("call:1", "call", "calls", call_start, "Recording a Signal call", "Signal", "rule: always for Signal calls",
            "live", "live"),
        row("run:d:1", "action", "did", ago(1 * H + 2 * M), "Opened Calc", "Calc", "you typed “open my budget”", "done, checked on screen",
            "verified", cost="3 s, 3¢"),
        row("run:e:2", "action", "failed", ago(2 * H + 11 * M), "Clicked Pause", "Music", "you said “pause it”",
            "didn’t work", "failed", cost="12 s, 6¢"),
        row("run:e:3", "action", "did", ago(2 * H + 10 * M), "Switched to Music", "Music", "you said “pause it”, keeping a lesson in mind",
            "done, checked on screen", "verified", chips=[{"kind": "lesson", "text": "Pause the music"}]),
        row("run:e:4", "action", "did", ago(2 * H + 10 * M - 2), "Pressed Pause", "Music", "you said “pause it”, keeping a lesson in mind",
            "done, checked on screen", "verified", chips=[{"kind": "lesson", "text": "Pause the music"}]),
        row("call:2", "call", "calls", ago(26 * H + 52 * M), "Recorded a 1h 10m Mumble call", "Mumble", "rule: always for Mumble calls",
            "turned into text", "verified"),
        row("c35", "card", "did", ago(3 * H), "Asked if the Friday call moved", "Card · Question", "your calendar and a message disagreed",
            "you said skip", "done", card="35"),
        row("c33", "card", "did", ago(5 * H), "The seed-swap thread has a new reply", "Card · Noticed",
            "rule: “Tell me when the seed-swap thread gets a reply.”", "you pressed it", "done", card="33"),
        row("r5", "request", "did", ago(1 * D + 1 * H), "“where’s last year’s seed list”", "Typed request", "you asked", "done",
            "done", cost="21 s, 5 steps, 11¢"),
        row("r6", "request", "did", ago(1 * D + 3 * H), "“rename the photos by date”", "Typed request", "you asked",
            "stopped", "stopped", cost="9 s, 2 steps, 5¢"),
    ]
    for r in rows:
        if r["id"].startswith("run:"):
            r["wrong_text"] = "That wasn’t right: “" + next(x["words"] for x in runs if r["id"].split(":")[1] == x["key"]) + "”. "
    rows.sort(key=lambda r: -r["at"])
    rows, groups = W.ledger_groups(rows, runs, NOW)
    activity = {
        "rows": rows, "groups": groups,
        "counts": {"did_today": sum(1 for r in rows if r["kind"] in ("action", "request") and r["at"] > NOW - 21 * H), "no_reason": 0},
        "seen": [{"app": a, "min": m, "visits": v} for a, m, v in
                 [("VS Code", 220, 41), ("Firefox", 131, 64), ("Hollow Knight", 104, 2), ("Calc", 66, 12), ("Signal", 38, 29),
                  ("Notes", 21, 9), ("Music", 14, 6), ("Files", 11, 8), ("Thunderbird", 9, 5), ("Audacity", 6, 1)]],
        "calls": [
            {"start_at": call_start, "end_at": None, "live": True, "min": 46, "spoke_min": 18, "app": "Signal", "where": "Signal", "people": ["Riya"],
             "gists": [], "recorded": True, "status": "Live now. Being recorded; it’s turned into text when the PC is free."},
            {"start_at": ago(26 * H + 52 * M), "end_at": ago(25 * H + 42 * M), "live": False, "min": 70, "spoke_min": 31, "app": "Mumble",
             "where": ROOM, "people": ["Noor", "maybe Dev"],
             "gists": ["You and Noor planned the meetup: she asks the community hall about tables and chairs.",
                       "You bring tomato and basil seeds, 20 packets each.",
                       "Then a long game where your friends picked the hardest route on purpose, which annoyed you."], "recorded": True},
            {"start_at": ago(1 * D + 2 * H), "end_at": ago(1 * D + 1 * H + 20 * M), "live": False, "min": 40, "spoke_min": None, "app": "Telegram",
             "where": "Telegram", "people": [], "gists": [], "recorded": False, "status": "Not recorded: you said no for this call."},
        ],
    }
    settings = {
        "autonomy": autonomy,
        "apps": [{"app": a, "look": lk, "act": ac, "note": n} for a, lk, ac, n in
                 [("VS Code", True, False, "That’s your work: I look, I don’t type"), ("Firefox", True, True, ""), ("Signal", True, True, "Sends still need your words"),
                  ("Calc", True, True, ""), ("Thunderbird", True, False, ""), ("Notes", True, True, ""), ("Banking app", False, False, "Never looked at")]],
        "voice": {"wake": True, "mic": "Microphone (USB Audio)", "speaker": "Headphones (Studio)", "end_tone": True, "provider": "gemini", "hotkey": "Ctrl+Alt+Y",
                  "devices": {"capture": ["Microphone (USB Audio)", "Microphone (Webcam)"], "render": ["Headphones (Studio)", "Speakers (Realtek)"],
                              "default_capture": "Microphone (USB Audio)", "default_render": "Speakers (Realtek)"},
                  "providers": [{"key": "gemini", "label": "Gemini, Leda", "note": "The default voice"},
                                {"key": "deepgram", "label": "Deepgram, Aura", "note": "Faster replies, about 0.8 s sooner"},
                                {"key": "elevenlabs", "label": "ElevenLabs", "note": "Uses your ElevenLabs voice id"}]},
        "offers": {"enabled": True}, "coach": {"enabled": True, "questions": True, "quiet_start": "00:30", "quiet_end": "08:00"},
        "privacy": {"paused": False, "seen_min": 642, "seen_apps": 14, "blocked_sites": 3},
        "calls": [{"app": "Signal", "decision": "always"}, {"app": "Telegram", "decision": "ask"}, {"app": "Meet", "decision": "never"}],
        "call_knobs": W.call_knobs({}),
        "appearance": {"accent": "green", "reduce_motion": False, "accents": [{"key": "green", "label": "Mint"}, {"key": "orange", "label": "Orange"}]},
        "usage": {"mb": 312, "cpu": 0.4},
    }
    chat_items = [
        {"id": "u0", "kind": "user", "at": ago(1 * D + 2 * H), "text": "how far is the community hall from the station?", "source": "text"},
        {"id": "a0", "kind": "assistant", "at": ago(1 * D + 2 * H), "text": "About a 12-minute walk, along the canal path.", "source": "text",
         "status": "done", "meta": "2¢", "steps": []},
        {"id": "u1", "kind": "user", "at": ago(5 * H), "text": "Open the seed-swap forum.", "source": "voice"},
        {"id": "a1", "kind": "assistant", "at": ago(5 * H), "text": "The forum’s up, on your question thread.", "source": "voice", "status": "done",
         "work_line": "worked for 6 s, 2 steps", "meta": "3¢", "steps": [
             {"key": "s1", "label": "Opening an app, Firefox", "ok": True, "seconds": 1.2},
             {"key": "s2", "label": "Going to a page, the seed-swap thread", "ok": True, "seconds": 2.9}]},
        {"id": "u2", "kind": "user", "at": ago(3 * H), "text": "pause it", "source": "voice"},
        {"id": "a2", "kind": "assistant", "at": ago(3 * H), "text": "I couldn’t pause it: the player wasn’t in front when I clicked. Next time I’ll bring it forward first.",
         "source": "voice", "status": "failed", "work_line": "worked for 12 s, 3 steps", "meta": "6¢", "steps": [
             {"key": "s3", "label": "Looking at the screen", "ok": True, "seconds": 0.4},
             {"key": "s4", "label": "Clicking Pause in Music", "ok": False, "seconds": 3.1, "result": "Didn’t work"},
             {"key": "s5", "label": "Checking it paused", "ok": True, "seconds": 0.9}]},
        {"id": "n1", "kind": "nudge", "at": ago(52 * M), "text": "Next on the garden planner is the watering schedule. Add it to your list?"},
        {"id": "u3", "kind": "user", "at": ago(45), "text": "Rename the March invoices by client", "source": "text"},
        {"id": "a41", "kind": "assistant", "at": ago(41), "text": "", "source": "text", "status": "running", "running": True, "jobs": [31],
         "work_line": "working 41 s, 3 steps", "thinking": "Reading the client names from each PDF", "steps": [
             {"key": "s6", "label": "Listing the March invoices", "ok": True, "seconds": 2.1},
             {"key": "s7", "label": "Reading an invoice", "ok": True, "seconds": 4.8},
             {"key": "s8", "label": "Renaming an invoice", "ok": None, "running": True, "started": ago(6)}]},
    ]
    replies = {
        "offer": {"ok": True, "result": {"ok": True, "do": "todo_add", "state": "done", "message": "Added to your list.", "undo": True}},
        "offer_undo": {"ok": True, "result": {"ok": True, "state": "undone"}},
        "card_feedback": {"ok": True}, "answer": {"ok": True, "saved": True}, "skip_question": {"ok": True},
        "continue": {"ok": True}, "askbar": {"ok": True}, "stop": {"ok": True}, "steer": {"ok": True, "delivered": True},
        "submit": {"ok": True}, "copy": {"ok": True}, "correct": {"ok": True}, "forget": {"ok": True}, "rule_edit": {"ok": True},
        "workflow_edit": {"ok": True, "state": "active"},
        "person": {"ok": True, "person": {"found": True, "facts": []}},
        "permissions_set": {"ok": True}, "app_look": {"ok": True}, "app_act": {"ok": True}, "voice": {"ok": True}, "offers": {"ok": True},
        "coach": {"ok": True}, "questions": {"ok": True}, "pause": {"ok": True}, "accent": {"ok": True}, "motion": {"ok": True},
        "call_pref": {"ok": True}, "stop_call": {"ok": True},
        "pin": {"ok": True, "pinned": True}, "resolve_heard": {"ok": True, "correction_id": None},
        "review_mark": {"ok": True, "correction_id": None}, "day_answer": {"ok": True, "sent": True},
        # the feedback round 3: a thread's close and Undo / Reopen, Correct from your words and its Undo, Continue
        "close_thread": {"ok": True, "status": "done"}, "correct_item": {"ok": True, "correction_id": 91, "ref": "fact:21"},
        "cancel_correction": {"ok": True}, "open_page": {"ok": True},
        "thread_continue": {"ok": True, "cont": {
            "ref": "thread:15", "title": "Seed-swap meetup on the 18th", "shown": "The venue is confirmed; the list of who brings what isn’t.",
            "next": "Reply on the forum with who brings what", "open": ["Reply to the forum thread"], "last_at": ago(4 * M),
            "ask": "Help me continue Seed-swap meetup on the 18th. Where it stands: The venue is confirmed; the list of who brings what "
                   "isn’t. Next: Reply on the forum with who brings what",
            "places": [{"kind": "page", "app": "Firefox", "title": "Seed swap on the 18th, the forum thread", "page": True, "last_at": ago(20 * M)}]},
            "by_ref": {"thread:12": {"ok": True, "cont": {
                "ref": "thread:12", "title": "Garden planner: watering schedule",
                "shown": "Bed sizes and soil types are in. The schedule screen is half built; the timer logic isn’t started.",
                "next": "Write the timer that waters each bed on its own days", "open": ["Write the watering schedule"],
                "last_at": ago(5 * M),
                "ask": "Help me continue Garden planner: watering schedule. Where it stands: Bed sizes and soil types are in. The "
                       "schedule screen is half built; the timer logic isn’t started. Next: Write the timer that waters each bed on "
                       "its own days",
                "places": [{"kind": "window", "app": "VS Code", "title": "garden-planner, schedule.ts", "page": False, "last_at": ago(5 * M)},
                           {"kind": "page", "app": "Firefox", "title": "Watering needs by plant, a gardening guide", "page": True,
                            "last_at": ago(31 * M)},
                           {"kind": "window", "app": "Notes", "title": "Balcony beds", "page": False, "last_at": ago(1 * D)}]}}}},
        "thread_days": {"ok": True, "connector": {
            "day": day_name(1), "day_at": ago(1 * D), "label": "Yesterday",
            "text": "Yesterday you finished the bed sizes screen and planned the seed swap with Noor. The watering timer is still open.",
            "open": ["Write the watering timer"]},
            "days": [{"day": day_name(1), "count": 1, "first_at": ago(1 * D + 2 * H), "last_at": ago(1 * D + 2 * H)},
                     {"day": day_name(3), "count": 2, "first_at": ago(3 * D + 3 * H), "last_at": ago(3 * D + 2 * H)}]},
        "chat_day": {"ok": True, "turns": [
            {"id": "t91", "at": ago(3 * D + 3 * H), "user": "what time does the climbing gym close on Sundays?",
             "reply": "8 PM on Sundays, an hour earlier than weekdays.", "outcome": "final"},
            {"id": "t92", "at": ago(3 * D + 2 * H), "user": "add chalk to my list", "reply": "Added “chalk” to your list.",
             "outcome": "final"}]},
    }
    # each person's key facts on their page (the person reply, by the person asked for)
    replies["person"] = {"ok": True, "person": {"found": True, "facts": []}, "by_ref": {
        "person:2": {"ok": True, "person": {"found": True, "facts": [
            {"ref": "fact:51", "h": "Riya climbs with you on Tuesdays", "d": ""},
            {"ref": "fact:52", "h": "She grows chillies and tomatoes on her balcony", "d": ""},
            {"ref": "fact:53", "h": "She asked for a dark mode in the planner", "d": ""},
            {"ref": "fact:54", "h": "You owe her the climbing photos from 27 September", "d": ""}]}},
        "person:8": {"ok": True, "person": {"found": True, "facts": [
            {"ref": "fact:41", "h": "Noor plays bass in your band", "d": ""},
            {"ref": "fact:55", "h": "Band practice is on Wednesdays at 7 PM", "d": ""}]}}}}
    return {"schema": "yuki-window/1",
            "note": "Invented data for the Lattice window's preview and tests. No real person, message or app use: nothing here comes from memory.",
            "now": NOW, "shell": shell,
            "pages": {"home": home, "work": work, "day": day, "me": me, "playbook": playbook, "activity": activity, "settings": settings},
            "chat": {"items": chat_items, "connected": True}, "replies": replies}


PREFIX = "window.YUKI_FIXTURE = "


def main() -> int:
    data = build()
    text = json.dumps(data, ensure_ascii=False, indent=None, separators=(",", ":"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("/* GENERATED by scripts/make_web_fixture.py: invented data only (see its docstring). */\n" + PREFIX + text + ";\n",
                   encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
