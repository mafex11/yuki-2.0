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


def build() -> dict:
    call_start = ago(46 * M + 4)
    rh = {"Riya": rhythm(8, 13, 17, 6, 20, 15, 11, 23, 9), "Dev": rhythm(3, 0, 1, 4, 0, 2, 5, 3, 2),
          "Mira": rhythm(1, 3, 0, 4, 2, 5, 0, 2, 1), "Noor": rhythm(0, 2, 5, 1, 0, 2, 3, 1, 4),
          "Theo": rhythm(0, 1, 0, 0, 2, 0, 1, 3, 0), "Ines": rhythm(0, 0, 0, 2, 0, 1, 0, 0, 0)}
    run_start = ago(41)
    autonomy = policy.describe(dict(policy.DEFAULTS))
    shell = {
        "user": {"name": "Sam", "initial": "S"}, "memory_line": "memory on", "connected": True,
        "voice": {"state": "off", "wake": True, "conversation": False, "quiet_until": 0},
        "call": {"app": "Signal", "started_at": call_start, "recording": True},
        "runs": [{"item": "a41", "jobs": [31], "title": "“Rename the March invoices by client”",
                  "line": "Step 3 · Renaming a file, invoice-0312.pdf", "started_at": run_start}],
        "hotkey_words": "Alt Shift", "needs": 2,
    }
    home = {
        "now": {
            "front": {"app": "VS Code", "title": "garden-planner · beds.ts, the watering schedule", "since_at": ago(12 * M)},
            "playing": {"status": "paused", "title": "Rain Study No. 4", "artist": "The Lo-Fi Trio", "app": "Music"},
            "call": {"app": "Signal", "started_at": call_start, "recording": True,
                     "note": "Only your mic. Turned into text when the PC is free. Cards are held."},
            "goals": [{"ref": "G7", "text": "finish the watering schedule screen", "status": "open", "done": False, "thread": "W12"},
                      {"ref": "G8", "text": "reply on the seed-swap thread", "status": "unknown", "done": False, "thread": "W15"},
                      {"ref": "G6", "text": "order the seed trays", "status": "done", "done": True, "thread": None}],
        },
        "needs": [
            {"kind": "question", "ref": "q:7", "at_label": "2 Oct 12:19", "title": "Is the Friday planning call still at 10?",
             "why": "You moved it once last week. The weekly review should have the right time."},
            {"kind": "question", "ref": "q:9", "at_label": "today 09:41", "title": "The “beds v2” sketch from Thursday: keep it, or was it a test?",
             "why": "It was saved twice with the same name. It may be a scratch copy."},
        ],
        "running": [
            {"kind": "call", "live": True, "recording": True, "title": "Remembering your side of the Signal call",
             "detail": "Only your mic. Turned into text later, when the PC is free.", "started_at": call_start},
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
             "changed": [{"text": "Firefox: 6 tabs back from before the restart", "state": "done"}], "cost": "4 s, 1 step, 4¢"},
            {"kind": "card", "title": "Added to your list", "at": ago(52 * M), "state": "done",
             "changed": [{"text": "“Order more seed trays”, due Tuesday", "state": "done",
                          "undo": {"card": "39", "until": ago(-4 * M), "label": "Undo"}}]},
            {"kind": "request", "title": "Told you what you did today", "at": ago(27 * M), "state": "done", "changed": [],
             "note": "The text showed but the voice came back empty. I’ll retry speech next time instead of going quiet.", "cost": "7 s"},
            {"kind": "notice", "title": "Models cost $1.84 today", "at": ago(3 * H + 34 * M), "state": "done", "changed": [],
             "note": "Journal $0.92, portrait $0.51, cards $0.24, call text $0.17."},
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
        ("thread:2", "Old laptop for Dev", "done", 2 * D, "Wiped, fresh system installed, handed over on Thursday.", [], ["Dev"]),
    ]
    people_names = {"Riya", "Noor", "Theo", "Mira", "Dev"}
    done_counts = {"thread:12": 3, "thread:15": 2, "thread:9": 1, "thread:18": 1, "thread:7": 2, "thread:21": 0,
                   "thread:5": 1, "thread:3": 1, "thread:2": 4}
    goals = {"thread:12": "finish the watering schedule screen", "thread:15": "reply on the seed-swap thread"}

    def checks(ref: str, open_items: list[str]) -> dict | None:
        done = done_counts.get(ref, 0)
        total = done + len(open_items)
        return {"done": done, "total": total, "next": open_items[0] if open_items else None} if total else None

    work = {
        "threads": [{"ref": r, "title": t, "status": s, "last_at": ago(a), "state": st, "open": o, "involves": inv,
                     "people": [e for e in inv if e in people_names], "apps": [e for e in inv if e not in people_names],
                     "checks": checks(r, o), "goal": goals.get(r)}
                    for r, t, s, a, st, o, inv in threads],
        "week": {"days": [{"dow": d, "today": i == 6, "pc": pc, "a": a, "f": f} for i, (d, pc, a, f) in enumerate(
            [("Sun", 210, 90, 40), ("Mon", 520, 360, 80), ("Tue", 470, 330, 0), ("Wed", 560, 380, 90), ("Thu", 430, 300, 0),
             ("Fri", 610, 360, 150), ("Sat", 452, 230, 114)])],
            "totals": {"pc": 3252, "a": 2050, "f": 474}},
    }
    day = {
        "date_label": "Saturday 3 Oct", "today": True, "first": "00:00", "last": "21:49", "now_min": 21 * 60 + 52,
        "off": [{"from": "01:31", "to": "09:40"}],
        "buckets": buckets(),
        "sessions": [
            {"a": 0, "b": 90, "start": "00:00", "end": "01:30", "title": "Reading the seed-swap forum", "app": "Firefox", "mode": "active",
             "apps": [["Firefox", 61], ["Signal", 14]]},
            {"a": 580, "b": 750, "start": "09:40", "end": "12:30", "title": "Garden planner: bed sizes", "app": "VS Code", "mode": "active",
             "apps": [["VS Code", 104], ["Firefox", 31], ["Notes", 9]]},
            {"a": 800, "b": 1030, "start": "13:20", "end": "17:10", "title": "Budget sheet, then the Rust exercise", "app": "Calc", "mode": "active",
             "apps": [["Calc", 66], ["VS Code", 72], ["Firefox", 40]]},
            {"a": 1120, "b": 1230, "start": "18:40", "end": "20:30", "title": "Hollow Knight", "app": "Hollow Knight", "mode": "full",
             "apps": [["Hollow Knight", 104]]},
            {"a": 1230, "b": 1312, "start": "20:30", "end": "21:52", "title": "Garden planner: watering schedule", "app": "VS Code", "mode": "active",
             "apps": [["VS Code", 44], ["Signal", 12], ["Music", 6]]},
        ],
        "calls": [
            {"a": 1140, "b": 1210, "start": "19:00", "end": "20:10", "app": "Signal", "live": False, "recorded": True, "min": 70,
             "gist": "Planning the meetup with Noor: who brings tables, the seed list."},
            {"a": 1266, "b": 1312, "start": "21:06", "end": "now", "app": "Signal", "live": True, "recorded": True, "min": 46, "started_at": call_start},
        ],
        "totals": [["At the PC", "7h 32m", "214 app switches"], ["Active", "5h 06m", "VS Code 3h 40m"], ["Full screen", "1h 44m", "Hollow Knight"],
                   ["Calls", "1h 56m", "2 on Signal, 1 live"], ["Longest focus", "52m", "VS Code, 10:12"]],
        "week_hours": week_hours(),
    }
    me = {
        "source": "index",
        "people": [
            {"ref": "person:2", "name": "Riya", "relation": "Close friend, from the climbing gym", "aliases": ["riyaclimbs"],
             "note": "Talks most evenings. She’s helping test the garden planner on her balcony beds.", "last_at": ago(52 * M), "count": 125,
             "prov": "Seen in Signal, most days", "usual": "Signal chat. Calls on Signal when it’s long.",
             "routes": [{"label": "DM", "sub": "@riyaclimbs", "app": "Signal", "kind": "dm", "mine": 78, "theirs": 47, "last_at": ago(52 * M), "preferred": True},
                        {"label": "Climbing crew", "sub": "group", "app": "Telegram", "kind": "group", "mine": 12, "theirs": 31, "last_at": ago(1 * D + 1 * H), "preferred": False},
                        {"label": "riya@…", "sub": "", "app": "Thunderbird", "kind": "email", "mine": 2, "theirs": 1, "last_at": ago(9 * D), "preferred": False}],
             "fact": {"ref": "fact:21", "title": "Riya", "text": "Riya is a close friend from the climbing gym who is testing the garden planner."}},
            {"ref": "person:5", "name": "Dev", "relation": "Brother", "aliases": [], "note": "Got your old laptop on Thursday.", "last_at": ago(2 * H), "count": 31,
             "prov": "You told Yuki, 28 Sep", "usual": "Email, short.",
             "routes": [{"label": "dev@…", "app": "Thunderbird", "kind": "email", "mine": 18, "theirs": 13, "last_at": ago(2 * H), "preferred": True}],
             "fact": {"ref": "fact:22", "title": "Dev", "text": "Dev is the user's brother."}},
            {"ref": "person:3", "name": "Mira", "relation": "Sister", "aliases": ["mimi"], "note": "Birthday on the 14th.", "last_at": ago(1 * D + 3 * H), "count": 48,
             "prov": "Seen in Telegram", "usual": "Telegram, mornings.",
             "routes": [{"label": "DM", "sub": "@mimi", "app": "Telegram", "kind": "dm", "mine": 22, "theirs": 26, "last_at": ago(1 * D + 3 * H), "preferred": True}]},
            {"ref": "person:8", "name": "Noor", "relation": "Bandmate, runs the seed swap", "aliases": [], "note": "", "last_at": ago(1 * D + 6 * H), "count": 19,
             "prov": "Seen in Signal and on calls", "usual": "Signal; calls to plan.",
             "routes": [{"label": "DM", "sub": "@noor", "app": "Signal", "kind": "dm", "mine": 9, "theirs": 10, "last_at": ago(1 * D + 6 * H), "preferred": True}]},
            {"ref": "person:4", "name": "Theo", "relation": "Landlord", "aliases": [], "note": "", "last_at": ago(2 * D), "count": 12,
             "prov": "Seen in Thunderbird", "usual": "Email only.",
             "routes": [{"label": "theo@…", "app": "Thunderbird", "kind": "email", "mine": 5, "theirs": 7, "last_at": ago(2 * D), "preferred": True}]},
            {"ref": "person:9", "name": "Ines", "relation": "Study group", "aliases": [], "note": "", "last_at": ago(4 * D), "count": 6,
             "prov": "Seen in Telegram", "usual": "", "routes": []},
        ],
        "routines": [
            {"ref": "fact:31", "title": "Evenings", "text": "At the desk most evenings from about 20:30, often past midnight.", "prov": "Seen 6 of 7 days", "conf_word": "sure",
             "week": [True, True, True, False, True, True, True], "kind": "behaviour"},
            {"ref": "fact:32", "title": "Games", "text": "A game most weekday evenings, roughly 19:00 to 20:30, full screen.", "prov": "Seen 4 of 7 days", "conf_word": "fairly sure",
             "week": [False, True, False, True, False, True, True], "kind": "behaviour"},
            {"ref": "fact:33", "title": "Mornings", "text": "First at the PC after 09:30 at weekends.", "prov": "Seen in your activity", "conf_word": "still learning",
             "week": [True, False, False, False, False, False, True], "kind": "behaviour"},
        ],
        "rules": [
            {"ref": "rule:4", "text": "Don’t call me “buddy”.", "prov": "You told me, 25 Sep", "kind": "preference"},
            {"ref": "rule:6", "text": "Keep replies short unless I ask for detail.", "prov": "You told me, 26 Sep", "kind": "preference"},
            {"ref": "rule:9", "text": "Never cost me frames in a game or interrupt a call.", "prov": "You told me, 27 Sep", "kind": "rule"},
            {"ref": "rule:11", "text": "My to-dos are mine: bring one up only if I ask.", "prov": "You told me, 30 Sep", "kind": "rule"},
        ],
        "facts": [
            {"ref": "fact:12", "title": "Garden planner", "text": "Building a garden planner app in your spare time, in TypeScript.", "prov": "Seen in VS Code, today", "conf_word": "sure", "kind": "work"},
            {"ref": "fact:14", "title": "Climbing", "text": "Climbs twice a week at the gym near the station.", "prov": "You told Yuki, 27 Sep", "tags": ["you"], "kind": "interest"},
            {"ref": "fact:16", "title": "Plants", "text": "Says the basil “has a personality”.", "prov": "Seen in Signal, 1 Oct", "tags": ["joke"], "kind": "personality"},
            {"ref": "fact:17", "title": "Rust", "text": "Learning Rust from the book, one chapter a week.", "prov": "Seen in Firefox, 2 Oct", "conf_word": "fairly sure", "kind": "interest"},
        ],
        "misheard": [
            {"ref": "journal:812", "title": "a call", "text": "“the heirloom swap” heard as “the hair loom swap”", "prov": "Heard in a call, today"},
        ],
        "pending": {},
    }
    for p in me["people"]:
        p["rhythm"], p["rhythm_info"] = rh[p["name"]]
    playbook = {
        "workflows_available": True, "know_how": [],
        "learning": {"runs_24h": 9, "cost_today_usd": 0.021, "passes_today": 3},
        "proposals": [
            {"ref": "pb:31", "kind": "habit", "title": "Message Riya: the Signal chat first",
             "basis": "Most of your messages to her went there; Telegram only for the climbing crew. Seen 3 times, 30 Sep; I’ll use it once you say so.",
             "steps": ["Open the chat with @riyaclimbs", "Type in your style: short, lowercase", "Wait for you to say send"], "app": "Signal"},
        ],
        "procedures": [
            {"ref": "pb:12", "kind": "procedure", "title": "Put a window on the second screen, full size", "when": "",
             "steps": ["Move the window to display two", "Maximize it as its own step"], "app": "Any app", "uses": 2,
             "last": day_label(4), "conf": 0.78, "conf_word": "fairly sure", "state": "active", "disabled": False,
             "learned": f"From a time I got it wrong, {day_label(4)}"},
            {"ref": "pb:14", "kind": "procedure", "title": "Scroll a Signal chat", "when": "Not: the page keys, they jump to the newest",
             "steps": ["Scroll the chat with Windows’ own scroll, upward", "Wait for it; Signal can take 2 s"], "app": "Signal", "uses": 3,
             "last": day_label(5), "conf": 0.72, "conf_word": "fairly sure", "state": "active", "disabled": False,
             "learned": f"From a run that worked, {day_label(5)} · seen on 2 days"},
            {"ref": "pb:17", "kind": "procedure", "title": "Restore Firefox after a restart", "when": "",
             "steps": ["Open Firefox", "Click Restore in the “Restore pages?” bubble"], "app": "Firefox", "uses": 4,
             "last": day_label(0), "conf": 0.88, "conf_word": "sure", "state": "trusted", "disabled": False,
             "learned": f"From a run that worked, {day_label(6)} · seen on 3 days"},
            {"ref": "pb:19", "kind": "habit", "title": "Videos in Firefox, not the other browser",
             "when": "You watch videos in Firefox; the other browser is for work tabs.", "steps": [], "app": "Firefox", "uses": 1,
             "last": day_label(2), "conf": 0.66, "conf_word": "still learning", "state": "active", "disabled": False,
             "learned": "Seen in what you do, 28 Sep · seen on 6 days"},
        ],
        "lessons": [
            {"ref": "pb:21", "kind": "pitfall", "dont": "Pause the music", "because": "A click on the player while it was behind another window went nowhere.",
             "do": "Bring the player to the front first, then press pause.", "used": 1, "from": f"From a time I got it wrong, {day_label(2)}",
             "conf": 0.7, "disabled": False},
            {"ref": "pb:23", "kind": "pitfall", "dont": "Lower the volume", "because": "“Lower your volume” has three targets: the system, a video, and my own voice.",
             "do": "Ask which one when it isn’t clear; mine is my voice.", "used": 2, "from": f"From a time I got it wrong, {day_label(3)} · corrected 1×",
             "conf": 0.74, "disabled": False},
            {"ref": "pb:24", "kind": "style", "dont": "Messages to Dev", "because": "Short and plain, no greeting; he answers in one line.",
             "do": "", "used": 0, "from": "In your words, " + day_label(5), "conf": 0.8, "disabled": False},
        ],
        "rules": [{"ref": r["ref"], "text": r["text"], "since": r["prov"].replace("You told me, ", ""), "kind": r["kind"]} for r in me["rules"]],
        "standing": [{"ref": "request:3", "text": "Tell me when the seed-swap thread gets a reply.", "since": "1 Oct"}],
    }
    def row(id_: str, kind: str, f: str, at: float, what: str, where: str, why: str, result: str, status: str, **kw) -> dict:
        return {"id": id_, "kind": kind, "f": f, "at": at, "what": what, "where": where, "cost": kw.pop("cost", ""),
                "because": "because", "why": why, "chips": kw.pop("chips", []), "result": result, "status": status, **kw}

    rows = [
        row("run:a:0", "request", "did", ago(27 * M), "Answered “What did I do today?”", "Yuki", "you asked by voice",
            "shown; the voice came back empty", "unverified", cost="7 s, 8¢"),
        row("run:b:2", "action", "did", ago(28 * M), "Clicked Restore in “Restore pages?”", "Firefox",
            "you said “bring my tabs back”, using how you do it", "done, checked on screen", "verified", cost="4 s, 4¢",
            chips=[{"kind": "memory", "text": "Restore Firefox after a restart"}]),
        row("run:b:1", "action", "did", ago(28 * M + 3), "Opened Firefox", "Firefox", "you said “bring my tabs back”, using how you do it",
            "done, checked on screen", "verified", chips=[{"kind": "memory", "text": "Restore Firefox after a restart"}]),
        row("run:c:1", "action", "did", ago(41 * M), "Added to your list “Water the basil on Sunday”", "Yuki", "you said “remind me to water the basil sunday”",
            "done", "done", cost="5 s, 3¢", undo={"todo": "todo:12", "until": ago(-4 * M), "label": "Undo"}),
        row("c39", "card", "did", ago(52 * M), "Added “Order more seed trays” to your list", "Card · Your work",
            "you pressed Add to list on the card", "done", "done", card="39", undo={"card": "39", "until": ago(-4 * M), "label": "Undo"}),
        row("call:1", "call", "calls", call_start, "Recording your side of a Signal call", "Signal", "rule: Signal calls, your mic only",
            "live", "live"),
        row("run:d:1", "action", "did", ago(1 * H + 2 * M), "Opened Calc", "Calc", "you typed “open my budget”", "done, checked on screen",
            "verified", cost="3 s, 3¢"),
        row("run:e:2", "action", "failed", ago(2 * H + 11 * M), "Clicked Pause", "Music", "you said “pause it”",
            "didn’t work", "failed", cost="12 s, 6¢"),
        row("run:e:3", "action", "did", ago(2 * H + 10 * M), "Switched to Music", "Music", "you said “pause it”, keeping a lesson in mind",
            "done, checked on screen", "verified", chips=[{"kind": "lesson", "text": "Pause the music"}]),
        row("call:2", "call", "calls", ago(2 * H + 52 * M), "Recorded a 1h 10m Signal call", "Signal", "rule: Signal calls, your mic only",
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
    rows.sort(key=lambda r: -r["at"])
    activity = {
        "rows": rows, "counts": {"did_today": sum(1 for r in rows if r["kind"] in ("action", "request") and r["at"] > NOW - 21 * H),
                                 "no_reason": 0},
        "seen": [{"app": a, "min": m, "visits": v} for a, m, v in
                 [("VS Code", 220, 41), ("Firefox", 131, 64), ("Hollow Knight", 104, 2), ("Calc", 66, 12), ("Signal", 38, 29),
                  ("Notes", 21, 9), ("Music", 14, 6), ("Files", 11, 8), ("Thunderbird", 9, 5), ("Audacity", 6, 1)]],
        "calls": [
            {"start_at": call_start, "end_at": None, "live": True, "min": 46, "spoke_min": 18, "app": "Signal", "people": ["Riya"], "gists": [], "recorded": True,
             "status": "Live now. Your side is being kept; it’s turned into text when the PC is free."},
            {"start_at": ago(2 * H + 52 * M), "end_at": ago(1 * H + 42 * M), "live": False, "min": 70, "spoke_min": 31, "app": "Signal", "people": ["Noor"],
             "gists": ["Tables and chairs: Noor asks the community hall.", "You bring tomato and basil seeds, 20 packets each.",
                       "Next check-in Wednesday."], "recorded": True},
            {"start_at": ago(1 * D + 2 * H), "end_at": ago(1 * D + 1 * H + 20 * M), "live": False, "min": 40, "spoke_min": 0, "app": "Telegram", "people": [],
             "gists": [], "recorded": False, "status": "Not recorded: you said no for this call."},
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
        "appearance": {"accent": "green", "reduce_motion": False, "accents": [{"key": "green", "label": "Mint"}, {"key": "orange", "label": "Orange"}]},
        "usage": {"mb": 312, "cpu": 0.4},
    }
    chat_items = [
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
             {"key": "s6", "label": "Listing files, Documents › Invoices › March", "ok": True, "seconds": 2.1},
             {"key": "s7", "label": "Reading a file, invoice-0311.pdf", "ok": True, "seconds": 4.8},
             {"key": "s8", "label": "Renaming a file, invoice-0312.pdf", "ok": None, "running": True, "started": ago(6)}]},
    ]
    replies = {
        "offer": {"ok": True, "result": {"ok": True, "do": "todo_add", "state": "done", "message": "Added to your list.", "undo": True}},
        "offer_undo": {"ok": True, "result": {"ok": True, "state": "undone"}},
        "card_feedback": {"ok": True}, "answer": {"ok": True, "saved": True}, "skip_question": {"ok": True},
        "continue": {"ok": True}, "askbar": {"ok": True}, "stop": {"ok": True}, "steer": {"ok": True, "delivered": True},
        "submit": {"ok": True}, "copy": {"ok": True}, "correct": {"ok": True}, "forget": {"ok": True}, "rule_edit": {"ok": True},
        "workflow_edit": {"ok": True, "state": "active"}, "todo_undo": {"ok": True},
        "person": {"ok": True, "person": {"found": True, "facts": []}},
        "permissions_set": {"ok": True}, "app_look": {"ok": True}, "app_act": {"ok": True}, "voice": {"ok": True}, "offers": {"ok": True},
        "coach": {"ok": True}, "questions": {"ok": True}, "pause": {"ok": True}, "accent": {"ok": True}, "motion": {"ok": True},
        "call_pref": {"ok": True}, "stop_call": {"ok": True},
    }
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
