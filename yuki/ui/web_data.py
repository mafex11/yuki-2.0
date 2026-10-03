"""What the Lattice window's pages show, as JSON: built from memory in the window's own process. No Qt.

Each ``build_*`` takes an open :class:`~yuki.memory.api.MemoryClient` (or a stand-in with the same methods) and
returns a plain dict the page renders (``yuki/ui/web/pages/*.js``; the invented preview data in
``yuki/ui/web/fixtures/window.js``, written by ``scripts/make_web_fixture.py``, has exactly these shapes). They reuse
the Qt window's builders (:mod:`yuki.ui.window.data`) where those exist, and read memory only through its public
client: nothing here writes, calls a model, or reads what a message says to decide anything (no keyword logic).
Every builder runs on a worker thread and is safe to call again; a failing part leaves its section empty.

Times are epoch seconds (the page formats them); words the page shows are plain and honest about what memory does
not have yet (no per-day message counts, no procedure steps, no thread checklists).
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from yuki.ui.cards import UNDOABLE_DOS, card_offer, card_title, epoch_of, offer_open, offer_steps

#: The page's data schema (the fixture has the same tag).
SCHEMA = "yuki-window/1"
#: Home's inbox covers this long ("last 7 days").
INBOX_DAYS = 7
#: "Since you were away" covers at most this long.
AWAY_HOURS = 12.0
#: Offers and returns kept on Home at most.
INBOX_MAX = 8
#: The day is drawn in this many slices (10 minutes each).
DAY_SLICES = 144
#: Memory's undo window for an offer that ran (yuki.memory.offers.UNDO_WITHIN_S).
UNDO_WITHIN_S = 10 * 60.0


def _safe(fn: Callable[[], Any], default: Any) -> Any:
    try:
        return fn()
    except Exception:
        return default


def hm(at: float | None) -> str:
    return datetime.fromtimestamp(at).strftime("%H:%M") if at else ""


def day_words(at: float | None, today: date | None = None) -> str:
    """``today 09:41`` / ``yesterday 23:40`` / ``2 Oct 12:19``."""
    if not at:
        return ""
    d = datetime.fromtimestamp(at)
    t = today or date.today()
    if d.date() == t:
        return f"today {d:%H:%M}"
    if d.date() == t - timedelta(days=1):
        return f"yesterday {d:%H:%M}"
    return f"{d.day} {d:%b} {d:%H:%M}"


def short_day(at: float | None) -> str:
    if not at:
        return ""
    d = datetime.fromtimestamp(at)
    return f"{d.day} {d:%b}"


def dur_words(seconds: float) -> str:
    m = int(round(max(0.0, seconds) / 60.0))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def money(usd: float | None) -> str:
    if usd is None:
        return ""
    return f"{max(1, round(usd * 100))}¢" if usd < 1 else f"${usd:.2f}"


def conf_word(confidence: float | None) -> str:
    if confidence is None:
        return ""
    return "sure" if confidence >= 0.85 else "fairly sure" if confidence >= 0.7 else \
        "still learning" if confidence >= 0.55 else "not sure"


def shift_times(value: Any, delta: float, key: str = "") -> Any:
    """``value`` with every time moved by ``delta`` s: numbers under ``at``, ``until``, ``started``, ``since`` and
    ``*_at`` keys (the fixture's times are relative to its own ``now``; bridge.js applies the same rule)."""
    if isinstance(value, list):
        return [shift_times(v, delta) for v in value]
    if isinstance(value, dict):
        return {k: shift_times(v, delta, k) for k, v in value.items()}
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 1e9 and (
            key in ("at", "until", "started", "since") or key.endswith("_at")):
        return value + delta
    return value


# -- the shell: the rail, the face, the status line ------------------------------------------------------------


def user_name() -> dict[str, str]:
    """The Windows account's display name (first word), else its user name; for the rail's avatar."""
    name = ""
    try:
        import ctypes

        size = ctypes.c_ulong(256)
        buf = ctypes.create_unicode_buffer(256)
        if ctypes.windll.secur32.GetUserNameExW(3, buf, ctypes.byref(size)):   # NameDisplay
            name = buf.value.split()[0] if buf.value.split() else ""
    except Exception:
        name = ""
    name = name or os.environ.get("USERNAME", "") or "You"
    name = name[:1].upper() + name[1:]
    return {"name": name, "initial": next((c for c in name if c.isalnum()), "Y").upper()}


def hotkey_words(combo: str) -> str:
    """``alt+shift`` -> ``Alt Shift`` (the ask bar's shortcut as the rail shows it)."""
    parts = [p.strip() for p in (combo or "alt+shift").split("+") if p.strip()]
    return " ".join(p[:1].upper() + p[1:] for p in parts)


def build_shell(memory: Any) -> dict[str, Any]:
    st = _safe(memory.status, {}) or {}
    if st.get("paused"):
        line = "memory paused"
    elif st.get("service_running") is False:
        line = "memory is off"
    else:
        line = "memory on"
    return {"user": user_name(), "memory_line": line, "call": call_now(memory)}


def call_now(memory: Any) -> dict[str, Any] | None:
    """The call going on now, for the rail's rec chip and Home's Now strip; ``None`` when there is none."""
    cs = _safe(memory.call_status, {}) or {}
    call = cs.get("call")
    if not call:
        return None
    status = str(call.get("status") or "")
    if status in ("declined", "unanswered", "stopped", "not_asked") and not cs.get("recording"):
        started = epoch_of(call.get("session_started_at")) or epoch_of(call.get("detected_at"))
        return {"app": call.get("app") or "", "started_at": started, "recording": False, "status": status,
                "status_words": {"declined": "not recording: you said no", "unanswered": "not recording",
                                 "stopped": "stopped remembering", "not_asked": "not recording"}.get(status, "")}
    started = epoch_of(call.get("session_started_at")) or epoch_of(call.get("recording_since"))
    words = {"asking": "asking if I may keep your side", "waiting_headphones": "waiting for headphones"}
    return {"app": call.get("app") or "", "started_at": started, "recording": bool(cs.get("recording")),
            "status": status, "status_words": words.get(status, "")}


# -- Home --------------------------------------------------------------------------------------------------------


def front_now(memory: Any, now: float) -> dict[str, Any] | None:
    """The app in front: the timeline's latest stretch at the PC (app, window title, since when)."""
    rows = _safe(lambda: memory.day_timeline(now - 3 * 3600, now), []) or []
    for r in reversed(rows):
        if r.get("state") == "present" and (r.get("app") or r.get("process")):
            start = float(r.get("start") or now)
            # earlier rows of the same app run on into this one: since when it has been in front
            for prev in reversed(rows[:rows.index(r)]):
                if prev.get("state") == "present" and prev.get("app") == r.get("app") and float(prev.get("end") or 0) >= start - 60:
                    start = float(prev.get("start") or start)
                else:
                    break
            title = "" if r.get("withheld") else (r.get("title") or "")
            return {"app": r.get("app") or r.get("process") or "", "title": " ".join(title.split()), "since_at": start}
    return None


#: ``GlobalSystemMediaTransportControlsSessionPlaybackStatus`` values worth showing.
_PLAYBACK = {4: "playing", 5: "paused"}


def media_now(timeout_s: float = 0.8) -> dict[str, Any] | None:
    """What is playing (the Windows media flyout's current session), read straight through WinRT (+4 MB, ~0.1 s;
    not yuki.perception, which costs +20 MB to import). ``None`` when nothing plays or it can't be read."""
    try:
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager

        op = Manager.request_async()
        if op.wait(timeout_s) != 1:   # AsyncStatus.Completed
            return None
        session = op.get_results().get_current_session()
        if session is None:
            return None
        status = _PLAYBACK.get(int(session.get_playback_info().playback_status))
        if status is None:
            return None
        props_op = session.try_get_media_properties_async()
        if props_op.wait(timeout_s) != 1:
            return None
        props = props_op.get_results()
        title = str(props.title or "").strip()
        if not title:
            return None
        app = str(session.source_app_user_model_id or "")
        app = app.rsplit("!", 1)[-1].rsplit("\\", 1)[-1]
        app = app[:-4] if app.lower().endswith(".exe") else app
        return {"status": status, "title": title, "artist": str(props.artist or "").strip(), "app": app}
    except Exception:
        return None


def question_item(q: dict[str, Any]) -> dict[str, Any]:
    at = epoch_of(q.get("asked_at")) or epoch_of(q.get("at"))
    return {"kind": "question", "ref": q.get("id"), "at_label": day_words(at), "title": q.get("question") or "",
            "why": q.get("evidence") or ""}


def offer_card(n: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """A card with a typed offer, as Home and Activity show it (06 §4.4): the observation, Will / Won't, the button
    named for the action, its state once pressed, Undo while memory still allows it."""
    now = time.time() if now is None else now
    offer = card_offer(n) or {}
    at = epoch_of(n.get("shown_at")) or epoch_of(n.get("at"))
    chips: list[list[str]] = [[card_title(n), hm(at)]]
    if offer.get("due"):
        chips.append(["due", str(offer.get("due"))])
    target = offer.get("target") if isinstance(offer.get("target"), dict) else None
    if target and (target.get("title") or target.get("url_title")):
        chips.append([str(target.get("url_title") or target.get("title")), ""])
    steps = offer_steps(n)
    state = n.get("_offer_state") or n.get("outcome") or (
        n.get("reaction") if n.get("reaction") in ("dismissed", "less") else None)
    out_at = epoch_of(n.get("outcome_at")) or n.get("_outcome_at")
    undo_until = (float(out_at) + UNDO_WITHIN_S) if (state == "done" and offer.get("do") in UNDOABLE_DOS and out_at) else None
    return {"card": str(n.get("id")), "at": at, "title": card_title(n), "text": n.get("text") or "", "chips": chips,
            "do": offer.get("do"), "label": " ".join(str(offer.get("label") or "").split()),
            "will": "; ".join(steps), "wont": offer.get("wont") or "", "undo": offer.get("do") in UNDOABLE_DOS,
            "open": offer_open(n, now), "state": state, "undo_until": undo_until}


def request_title(text: str) -> str:
    """A request as the user typed or said it, one line, quoted (cleaned of the window's own markers)."""
    from yuki.ui.chat_model import clean_request

    t = " ".join(clean_request(text or "").split())
    return f"“{t[:140] + ('…' if len(t) > 140 else '')}”" if t else "A request"


def request_row_words(r: dict[str, Any]) -> tuple[str, str]:
    """(the result in words, its cost line) for a request log row."""
    state = r.get("state") or ""
    words = {"done": "done", "failed": "failed", "stopped": "stopped", "replaced": "replaced by a newer request",
             "abandoned": "abandoned"}.get(state, state)
    n = sum((r.get("tools") or {}).values())
    bits = []
    if r.get("wall_s") is not None:
        bits.append(f"{r['wall_s']:.0f} s" if r["wall_s"] >= 10 else f"{r['wall_s']:.1f} s")
    if n:
        bits.append(f"{n} step{'s' if n != 1 else ''}")
    if r.get("cost_usd"):
        bits.append(money(r["cost_usd"]))
    return words, ", ".join(bits)


def build_home(memory: Any, *, requests_csv: Path | None = None, since: float | None = None,
               now: float | None = None, people: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Home: the Now strip, Needs you (questions), Running (the call, transcripts waiting; live runs come from the
    tray), Offers, Since you were away (requests that finished, offers that ran) and the people lately."""
    from yuki.ui.ledger import read_request_log

    now = time.time() if now is None else now
    since = since if since is not None else now - AWAY_HOURS * 3600
    call = call_now(memory)
    now_strip = {"front": front_now(memory, now), "playing": None, "call": None}
    if call and (call.get("recording") or call.get("status") in ("asking", "waiting_headphones")):
        note = "Only your mic. Turned into text when the PC is free. Cards are held." if call.get("recording") else ""
        now_strip["call"] = {**call, "note": note}
    needs = [question_item(q) for q in (_safe(lambda: memory.questions(statuses=("open",), limit=12), []) or [])]
    running: list[dict[str, Any]] = []
    if call and call.get("recording"):
        running.append({"kind": "call", "live": True, "recording": True, "started_at": call.get("started_at"),
                        "title": f"Remembering your side of the {call.get('app') or ''} call".replace("  ", " "),
                        "detail": "Only your mic. Turned into text later, when the PC is free."})
    waiting = _safe(memory.call_audio_waiting, {}) or {}
    if int(waiting.get("calls") or 0):
        n = int(waiting["calls"])
        running.append({"kind": "waiting", "live": False, "title": f"{n} call{'s' if n != 1 else ''} waiting to be turned into text",
                        "detail": f"{dur_words(float(waiting.get('minutes') or 0) * 60)} in all. Done when the PC is free.",
                        "meta": "waiting"})
    cards = _safe(lambda: memory.recent_nudges(hours=24 * INBOX_DAYS), []) or []
    cards = sorted(cards, key=lambda n: epoch_of(n.get("shown_at")) or epoch_of(n.get("at")) or 0, reverse=True)
    offers = [offer_card(n, now) for n in cards if card_offer(n) is not None and offer_open(n, now)][:INBOX_MAX]
    away: list[dict[str, Any]] = []
    for n in cards:
        at = epoch_of(n.get("shown_at")) or epoch_of(n.get("at")) or 0
        if at < since or card_offer(n) is None or n.get("outcome") not in ("done", "undone"):
            continue
        c = offer_card(n, now)
        change = {"text": n.get("text") or "", "state": "done"}
        if c["undo_until"] and c["undo_until"] > now:
            change["undo"] = {"card": c["card"], "until": c["undo_until"], "label": "Undo"}
        away.append({"kind": "card", "title": {"done": "Done from a card", "undone": "Undone"}[n["outcome"]],
                     "at": epoch_of(n.get("outcome_at")) or at, "state": n["outcome"], "changed": [change]})
    for r in (read_request_log(requests_csv, since, limit=INBOX_MAX) if requests_csv else []):
        words, cost = request_row_words(r)
        away.append({"kind": "request", "title": request_title(r.get("what") or ""), "at": r["at"], "state": r["state"],
                     "changed": [{"text": words, "state": "failed" if r["state"] == "failed" else "done"}], "cost": cost})
    away.sort(key=lambda x: x.get("at") or 0, reverse=True)
    return {"now": now_strip, "needs": needs, "running": running, "offers": offers, "away": away[:INBOX_MAX],
            "people": (people or [])[:6], "since": since}


# -- People (Me, and Home's "people lately") -----------------------------------------------------------------------


def _route(r: dict[str, Any]) -> dict[str, Any]:
    last = max(epoch_of(r.get("last_mine")) or 0, epoch_of(r.get("last_theirs")) or 0) or None
    return {"label": r.get("label") or "", "app": r.get("app") or "", "kind": r.get("kind") or "",
            "mine": r.get("mine"), "theirs": r.get("theirs"), "last_at": last, "preferred": bool(r.get("preferred")),
            "share": r.get("share")}


def person_from_index(p: dict[str, Any]) -> dict[str, Any]:
    """One row of memory's people index (``people_list()``) as Me's list and dossier show it. Message counts and the
    last contact come from the index; the contact rhythm only when the index has per-day counts (``rhythm``)."""
    routes = [_route(r) for r in p.get("routes") or [] if isinstance(r, dict)]
    count = sum(int(r["mine"] or 0) + int(r["theirs"] or 0) for r in routes if isinstance(r.get("mine"), int))
    preferred = next((r for r in routes if r["preferred"]), routes[0] if routes else None)
    apps = []
    for r in routes:
        if r["app"] and r["app"] not in apps:
            apps.append(r["app"])
    out = {"ref": str(p.get("ref") or p.get("name") or ""), "name": p.get("name") or "", "relation": p.get("relation") or "",
           "aliases": [str(a) for a in p.get("aliases") or [] if str(a).strip()][:4], "usual": p.get("usual") or "",
           "last_at": epoch_of(p.get("last_contact")), "count": count if routes else None, "routes": routes,
           "route": preferred["app"] if preferred else "", "prov": ("Seen in " + " and ".join(apps[:2])) if apps else ""}
    if isinstance(p.get("rhythm"), list):
        out["rhythm"] = p["rhythm"]
    return out


def contact_card(card: dict[str, Any]) -> dict[str, Any]:
    """``contact(ref)``'s full card trimmed for the dossier: routes with counts (never the message samples: the
    window does not need the user's own messages), the facts as lines, usual, aliases, relation."""
    facts = []
    for f in card.get("facts") or []:
        text = f.get("text") if isinstance(f, dict) else f
        if text and str(text).strip():
            facts.append(" ".join(str(text).split()))
    out = {"found": bool(card.get("found", True)), "relation": card.get("relation") or "",
           "aliases": [str(a) for a in card.get("aliases") or []][:4], "usual": card.get("usual") or "",
           "routes": [_route(r) for r in card.get("routes") or [] if isinstance(r, dict)], "facts": facts[:4]}
    if isinstance(card.get("rhythm"), list):
        out["rhythm"] = card["rhythm"]
    return out


def people_now(memory: Any, limit: int = 40) -> tuple[str, list[dict[str, Any]]]:
    """(``index`` | ``portrait``, people): memory's people index when this build has it (probed), else the portrait's
    person facts (no routes: the page says routes appear once Yuki has learned them)."""
    people_list = getattr(memory, "people_list", None)
    if callable(people_list):
        rows = _safe(lambda: people_list(limit=limit), None)
        if isinstance(rows, list):
            return "index", [person_from_index(p) for p in rows if isinstance(p, dict) and p.get("name")]
    from yuki.ui.window.data import portrait_item

    facts = [f for f in (_safe(lambda: memory.portrait_facts_with_evidence(2), []) or []) if f.get("kind") == "person"]
    out, seen = [], set()
    for f in facts:
        it = portrait_item(f)
        key = str(f.get("subject") or it.title).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"ref": it.ref, "name": f.get("subject") or it.title, "relation": it.summary or it.short,
                    "note": it.text, "prov": it.source, "routes": [], "last_at": max([e.at for e in it.evidence] or [it.since]),
                    "fact": {"ref": it.ref, "title": it.title, "text": it.text}})
    out.sort(key=lambda p: -(p.get("last_at") or 0))
    return "portrait", out


# -- Me ------------------------------------------------------------------------------------------------------------


def fact_json(it: Any, kind: str = "fact") -> dict[str, Any]:
    """A portrait item (:class:`yuki.ui.window.data.Item`) as Me shows it, with where it came from."""
    return {"ref": it.ref, "title": it.title, "text": it.text, "prov": it.source, "tags": list(it.tags or []),
            "conf_word": "" if it.origin == "user" else conf_word(it.confidence), "kind": kind}


def read_pending(path: Path, db: str) -> dict[str, dict[str, Any]]:
    """Corrections sent but not yet applied by memory (``window-pending.json``, shared with the Qt window)."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("db") not in (None, db):
        return {}
    return {str(k): {"kind": v.get("kind"), "words": v.get("words") or "", "late": bool(v.get("late"))}
            for k, v in (raw.get("items") or {}).items() if isinstance(v, dict)}


def rule_json(r: dict[str, Any]) -> dict[str, Any]:
    at = epoch_of(r.get("at"))
    prov = f"You told me, {short_day(at)}" if r.get("origin") == "user" and at else "From your preferences"
    return {"ref": r.get("ref"), "text": r.get("text") or "", "prov": prov, "kind": r.get("kind") or "",
            "since": short_day(at)}


def build_me(memory: Any, *, pending: dict[str, Any] | None = None,
             people: tuple[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    from yuki.ui.window.data import build_me as qt_build_me

    source, persons = people if people is not None else people_now(memory)
    view = _safe(lambda: qt_build_me(memory), None)
    facts: list[dict[str, Any]] = []
    routines: list[dict[str, Any]] = []
    misheard: list[dict[str, Any]] = []
    if view is not None:
        routines = [fact_json(i, "behaviour") for i in view.routines]
        for group, kind in ((view.work, "work"), (view.interests, "interest"), (view.preferences, "preference"),
                            (view.on_your_plate, "open_loop")):
            facts += [fact_json(i, kind) for i in group]
        for items in view.personality.values():
            facts += [fact_json(i, "personality") for i in items]
        misheard = [{"ref": i.ref, "title": i.title, "text": i.text, "prov": i.source} for i in view.unsure]
        if source == "index":   # the portrait fact behind a person, for the dossier's Correct / Forget / Pin
            by_name = {str(i.title).casefold(): i for i in view.people}
            for p in persons:
                it = by_name.get(str(p.get("name") or "").casefold())
                if it is not None:
                    p["fact"] = {"ref": it.ref, "title": it.title, "text": it.text}
                    p.setdefault("note", it.text)
    rules = [rule_json(r) for r in (_safe(memory.rules, []) or [])]
    return {"source": source, "people": persons, "routines": routines, "rules": rules, "facts": facts,
            "misheard": misheard, "pending": pending or {}}


# -- Work and the Day ----------------------------------------------------------------------------------------------


def build_work(memory: Any, *, week: dict[str, Any] | None = None) -> dict[str, Any]:
    """Threads of work (memory's ``threads``), most recently touched first, and the week figure."""
    rows = _safe(lambda: memory.threads(statuses=("active", "paused", "done"), limit=24), []) or []
    threads = []
    for t in rows:
        checks = t.get("checks") if isinstance(t.get("checks"), dict) else None   # E12 (thread checklists), later
        threads.append({"ref": t.get("ref"), "title": t.get("title") or "", "status": t.get("status") or "",
                        "state": " ".join(str(t.get("state") or "").split()), "open": [str(x) for x in t.get("open") or []],
                        "involves": [str(x) for x in t.get("involves") or []], "last_at": epoch_of(t.get("last_seen")),
                        "checks": checks})
    order = {"active": 0, "blocked": 0, "paused": 1, "done": 2}
    threads.sort(key=lambda t: (order.get(t["status"], 1), -(t["last_at"] or 0)))
    return {"threads": threads, "week": week if week is not None else week_figure(memory)}


def week_figure(memory: Any, now: float | None = None) -> dict[str, Any]:
    days = week_days(memory, now)
    totals = {"pc": round(sum(d["pc"] for d in days)), "a": round(sum(d["a"] for d in days)),
              "f": round(sum(d["f"] for d in days))}
    return {"days": [{k: d[k] for k in ("dow", "today", "pc", "a", "f")} for d in days], "totals": totals}


def week_days(memory: Any, now: float | None = None) -> list[dict[str, Any]]:
    """The last 7 days (today last) by hour, from the timeline: minutes at the PC, of which full screen, on a call
    (the app in front held the microphone), away; per hour the mode that held most of it."""
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now).date()
    start = datetime.combine(today - timedelta(days=6), datetime.min.time()).timestamp()
    rows = _safe(lambda: memory.day_timeline(start, now), []) or []
    hours = [[{"present": 0.0, "full": 0.0, "call": 0.0, "away": 0.0} for _ in range(24)] for _ in range(7)]
    for r in rows:
        s, e = max(float(r.get("start") or 0), start), min(float(r.get("end") or 0), now)
        if e <= s:
            continue
        span = max(1e-6, float(r.get("end") or 0) - float(r.get("start") or 0))
        present = (float(r.get("active_s") or 0) + float(r.get("passive_s") or 0)) if r.get("state") == "present" else 0.0
        t = s
        while t < e:
            day_i = int((t - start) // 86400)
            if day_i > 6:
                break
            h0 = start + day_i * 86400 + int(((t - start) % 86400) // 3600) * 3600
            seg_end = min(e, h0 + 3600)
            frac = (seg_end - t) / span
            cell = hours[day_i][int((h0 - start - day_i * 86400) // 3600)]
            if r.get("state") == "present":
                cell["present"] += present * frac
                if r.get("fullscreen"):
                    cell["full"] += present * frac
                if r.get("meeting"):
                    cell["call"] += present * frac
            elif r.get("state") == "away":
                cell["away"] += seg_end - t
            t = seg_end
    out = []
    now_h = datetime.fromtimestamp(now).hour
    for i in range(7):
        d = today - timedelta(days=6 - i)
        hrs, pc, full = [], 0.0, 0.0
        for hh, c in enumerate(hours[i]):
            pc += c["present"]
            full += c["full"]
            if i == 6 and hh > now_h:
                hrs.append({"k": "o", "v": 0, "fu": True})
                continue
            if c["present"] >= 60:
                k = "f" if c["full"] > c["present"] / 2 else "c" if c["call"] > c["present"] / 2 else "a"
                hrs.append({"k": k, "v": round(min(1.0, c["present"] / 3600), 2)})
            elif c["away"] >= 300:
                hrs.append({"k": "w", "v": round(min(1.0, c["away"] / 3600) * 0.6, 2)})
            else:
                hrs.append({"k": "o", "v": 0})
        out.append({"dow": f"{d:%a}", "today": i == 6, "pc": round(pc / 60), "a": round((pc - full) / 60),
                    "f": round(full / 60), "hours": hrs})
    return out


def build_day(memory: Any, day: date | None = None, *, now: float | None = None,
              week: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The Day tab: the day in slices (the skyline), today's totals, sessions, calls, and the week by hour."""
    from yuki.ui.window.data import build_day as qt_build_day

    now = time.time() if now is None else now
    day = day or datetime.fromtimestamp(now).date()
    v = qt_build_day(memory, day, now=now, extras=False)
    since = v.since
    is_today = day == datetime.fromtimestamp(now).date()
    slice_s = 86400.0 / DAY_SLICES
    present = [0.0] * DAY_SLICES
    full = [0.0] * DAY_SLICES
    away = [0.0] * DAY_SLICES

    def spread(arr: list[float], s: float, e: float, amount: float) -> None:
        if e <= s or amount <= 0:
            return
        per = amount / (e - s)
        i0, i1 = max(0, int((s - since) // slice_s)), min(DAY_SLICES - 1, int((e - since - 1e-6) // slice_s))
        for i in range(i0, i1 + 1):
            b0 = since + i * slice_s
            ov = min(e, b0 + slice_s) - max(s, b0)
            if ov > 0:
                arr[i] += ov * per
    for seg in v.segments:
        if seg.state == "present":
            spread(present, seg.start, seg.end, seg.present_s)
            if seg.fullscreen:
                spread(full, seg.start, seg.end, seg.present_s)
        elif seg.state == "away":
            spread(away, seg.start, seg.end, seg.end - seg.start)
    off = [(b.start, b.end) for b in v.power]
    buckets = []
    for i in range(DAY_SLICES):
        b0 = since + i * slice_s
        if b0 >= v.end:
            buckets.append({"p": 0, "m": "future", "c": 0})
            continue
        p = min(1.0, present[i] / slice_s)
        if p >= 0.02:
            mode = "full" if full[i] > present[i] / 2 else "active"
        elif away[i] > slice_s * 0.2:
            mode = "away"
        elif any(s < b0 + slice_s and e > b0 for s, e in off):
            mode = "off"
        else:
            mode = "gap"
        c = 0
        for call in v.calls:
            if call.start < b0 + slice_s and call.end > b0:
                c = 2 if call.ongoing else max(c, 1)
        buckets.append({"p": round(p, 2), "m": mode, "c": c})
    lanes = {ln.key: ln.label for ln in v.lanes}
    sessions = []
    for s in v.sessions:
        fs = sum(seg.present_s for seg in v.segments if seg.fullscreen and seg.state == "present"
                 and seg.start < s.end and seg.end > s.start)
        sessions.append({"a": int((s.start - since) // 60), "b": int((s.end - since) // 60), "start": hm(s.start),
                         "end": hm(s.end), "title": s.title, "app": lanes.get(s.lane, s.lane),
                         "mode": "full" if fs > s.present_s / 2 else "active",
                         "apps": [[name, round(secs / 60)] for name, secs in s.apps[:4]]})
    calls_rows = _safe(lambda: memory.calls_in(since=since, until=since + 86400, limit=30), []) or []
    calls = []
    for c in calls_rows:
        cs, ce = epoch_of(c.get("start")), epoch_of(c.get("end")) or now
        if cs is None:
            continue
        gists = [g.get("gist") for g in c.get("gists") or [] if isinstance(g, dict) and g.get("gist")]
        calls.append({"a": int((max(cs, since) - since) // 60), "b": int((min(ce, since + 86400) - since) // 60),
                      "start": hm(cs), "end": hm(ce), "app": c.get("app") or "", "live": bool(c.get("ongoing")),
                      "recorded": bool(c.get("recorded")), "min": round(float(c.get("seconds") or 0) / 60),
                      "gist": gists[0] if gists else "", "started_at": cs})
    t = v.totals
    longest, top_full = longest_focus(v), _top_full(v)
    lane_top = max(v.lanes, key=lambda ln: ln.present_s - ln.fullscreen_s, default=None)
    n_calls = len(calls)
    totals = [["At the PC", dur_words(t.get("present_s", 0)), f"{t.get('switches', 0)} app switches"],
              ["Active", dur_words(max(0.0, t.get("present_s", 0) - t.get("fullscreen_s", 0))),
               f"{lane_top.label} {dur_words(lane_top.present_s)}" if lane_top else ""],
              ["Full screen", dur_words(t.get("fullscreen_s", 0)), top_full],
              ["Calls", dur_words(t.get("call_s", 0)), f"{n_calls} call{'s' if n_calls != 1 else ''}" if n_calls else "none"],
              ["Longest focus", dur_words(longest[0]), f"{longest[1]}, {hm(longest[2])}" if longest[1] else ""]]
    offs = []
    for s, e in sorted(off):
        if offs and s - offs[-1][1] < 300:
            offs[-1] = (offs[-1][0], max(offs[-1][1], e))
        else:
            offs.append((s, e))
    return {"date_label": f"{day:%A} {day.day} {day:%b}", "today": is_today, "first": hm(v.first_at), "last": hm(v.last_at),
            "now_min": int((now - since) // 60) if is_today else None,
            "off": [{"from": hm(s), "to": hm(e)} for s, e in offs if e - s >= 600],
            "buckets": buckets, "sessions": sessions, "calls": calls, "totals": totals,
            "week_hours": week if week is not None else week_days(memory, now)}


def longest_focus(v: Any) -> tuple[float, str, float]:
    """(seconds, app, start) of the longest run of present stretches in one app without a switch."""
    best = (0.0, "", 0.0)
    run_lane, run_start, run_end = None, 0.0, 0.0
    labels = {ln.key: ln.label for ln in v.lanes}
    for seg in v.segments:
        if seg.state != "present":
            run_lane = None
            continue
        if seg.lane == run_lane and seg.start - run_end < 60:
            run_end = seg.end
        else:
            run_lane, run_start, run_end = seg.lane, seg.start, seg.end
        if run_end - run_start > best[0]:
            best = (run_end - run_start, labels.get(run_lane, run_lane or ""), run_start)
    return best


def _top_full(v: Any) -> str:
    lane = max(v.lanes, key=lambda ln: ln.fullscreen_s, default=None)
    return lane.label if lane is not None and lane.fullscreen_s > 60 else ""


# -- Playbook ------------------------------------------------------------------------------------------------------


def build_playbook(memory: Any) -> dict[str, Any]:
    """Procedures (Phase C writes them: probed), today's know-how, lessons (none yet), the standing rules."""
    from yuki.ui.window.data import learned_items

    workflows = getattr(memory, "workflows", None)
    procs = _safe(lambda: list(workflows() or []), []) if callable(workflows) else []
    know = [{"title": k.title, "line": k.line, "app": k.app} for k in (_safe(lambda: learned_items(memory, limit=24), []) or [])]
    rules = [rule_json(r) for r in (_safe(memory.rules, []) or [])]
    standing = []
    for r in _safe(memory.standing_requests, []) or []:
        text = " ".join(str(r.get("quote") or r.get("condition") or "").split())
        if text:
            standing.append({"ref": r.get("ref"), "text": text, "since": short_day(epoch_of(r.get("created_at")))})
    return {"proposals": [], "procedures": procs, "know_how": know, "lessons": [], "rules": rules,
            "standing": standing, "workflows_available": callable(workflows)}


# -- Activity ------------------------------------------------------------------------------------------------------


def build_activity(memory: Any, *, requests_csv: Path | None = None, now: float | None = None,
                   days: int = 7) -> dict[str, Any]:
    """The ledger (finished requests and the cards Yuki showed, newest first), what it saw today (time by app,
    counts only) and the calls of the week."""
    from yuki.ui.ledger import read_request_log

    now = time.time() if now is None else now
    since = now - days * 86400
    rows: list[dict[str, Any]] = []
    lanes = {"worker": "Typed request", "front_desk": "Quick answer", "voice": "Voice"}
    for i, r in enumerate(read_request_log(requests_csv, since, limit=120) if requests_csv else []):
        words, cost = request_row_words(r)
        status = {"done": "done", "failed": "failed", "stopped": "stopped"}.get(r["state"], "done")
        rows.append({"id": f"r{i}", "kind": "request", "at": r["at"], "what": request_title(r.get("what") or ""),
                     "where": lanes.get(r.get("where") or "", (r.get("where") or "").replace("_", " ").capitalize()),
                     "cost": cost, "because": "because", "why": "you asked", "result": words, "status": status,
                     "cant_undo": status == "done"})
    for n in _safe(lambda: memory.recent_nudges(hours=24 * days), []) or []:
        at = epoch_of(n.get("shown_at")) or epoch_of(n.get("at"))
        if not at:
            continue
        offer = card_offer(n)
        reaction = n.get("reaction") or ("missed" if n.get("missed") else "shown")
        outcome = n.get("outcome")
        result = {"done": "done", "undone": "undone", "prefilled": "put in the ask bar", "expired": "ran out",
                  "gone": "no longer there", "stopped": "stopped"}.get(outcome or "", "") or {
            "dismissed": "you said not now", "less": "you said fewer like this", "replied": "you replied",
            "accepted": "you pressed it", "missed": "missed while you were away", "snoozed": "you snoozed cards",
            "shown": "shown"}.get(reaction, reaction)
        c = offer_card(n, now) if offer else None
        row = {"id": f"c{n.get('id')}", "kind": "card", "card": str(n.get("id")), "at": at,
               "what": " ".join(str(n.get("text") or "").split())[:160], "where": f"Card · {card_title(n)}",
               "because": "because" if n.get("reason") else "", "why": " ".join(str(n.get("reason") or "").split())[:200],
               "result": result, "status": "failed" if outcome in ("expired", "gone") else "done"}
        if c and c["undo_until"] and c["undo_until"] > now:
            row["undo"] = {"card": c["card"], "until": c["undo_until"], "label": "Undo"}
        rows.append(row)
    rows.sort(key=lambda r: r["at"], reverse=True)
    midnight = datetime.combine(datetime.fromtimestamp(now).date(), datetime.min.time()).timestamp()
    did_today = sum(1 for r in rows if r["at"] >= midnight and r["kind"] == "request")
    no_reason = sum(1 for r in rows if r["at"] >= midnight and r["kind"] == "card" and not r["why"])
    act = _safe(lambda: memory.activity(since=midnight, until=now, group_by="app", limit=20), {}) or {}
    seen = [{"app": it.get("label") or it.get("app") or "", "min": round(float(it.get("present_s") or 0) / 60, 1),
             "visits": int(it.get("visits") or 0)} for it in act.get("items") or [] if float(it.get("present_s") or 0) >= 30]
    calls = []
    for c in reversed(_safe(lambda: memory.calls_in(since=since, until=now, limit=30), []) or []):
        cs = epoch_of(c.get("start"))
        if cs is None:
            continue
        gists = [g.get("gist") for g in c.get("gists") or [] if isinstance(g, dict) and g.get("gist")]
        people = [p.get("name") for p in c.get("people") or [] if isinstance(p, dict) and p.get("name")]
        status = "" if gists else ("Live now. Your side is kept; it’s turned into text when the PC is free."
                                   if c.get("ongoing") and c.get("recorded") else
                                   "Waiting to be turned into text." if c.get("recorded") else "Not recorded.")
        calls.append({"start_at": cs, "end_at": None if c.get("ongoing") else epoch_of(c.get("end")),
                      "live": bool(c.get("ongoing")), "min": round(float(c.get("seconds") or 0) / 60),
                      "spoke_min": round(float(c.get("speech_s") or 0) / 60) if c.get("recorded") else None,
                      "app": c.get("app") or "", "people": people, "gists": gists[:4], "recorded": bool(c.get("recorded")),
                      "status": status})
    return {"rows": rows[:150], "counts": {"did_today": did_today, "no_reason": no_reason}, "seen": seen,
            "calls": calls}


# -- Settings ------------------------------------------------------------------------------------------------------


VOICE_PROVIDERS = [{"key": "gemini", "label": "Gemini, Leda", "note": "The default voice"},
                   {"key": "deepgram", "label": "Deepgram, Aura", "note": "Faster replies, about 0.8 s sooner"},
                   {"key": "elevenlabs", "label": "ElevenLabs", "note": "Uses your ElevenLabs voice id"}]


def build_settings(memory: Any, *, live: dict[str, Any] | None = None, devices: dict[str, Any] | None = None,
                   policy_path: Path | None = None) -> dict[str, Any]:
    """Settings: the autonomy policy, app access (look: the privacy blocklist; act: the policy's per-app switch),
    voice (the tray's live state when it runs, else voice.json), offers and check-ins and quiet hours (the privacy
    file), pause, calls, accent and motion (ui.json)."""
    from yuki.ui import policy, ui_settings
    from yuki.ui.window import data as D
    from yuki.ui.window import settings_io

    live = live or {}
    privacy = D.read_privacy(_safe(settings_io.privacy_path, None))
    nudges, user = privacy.get("nudges", {}), privacy.get("user", {})
    prefs = D.read_voice_prefs()
    blocked = {str(a).casefold(): str(a) for a in user.get("blocked_apps", []) if str(a).strip()}
    acts = policy.app_acts(path=policy_path)
    week = time.time() - 7 * 86400
    act = _safe(lambda: memory.activity(since=week, group_by="app", limit=14), {}) or {}
    apps, seen = [], set()
    for it in act.get("items") or []:
        name = it.get("label") or it.get("app") or ""
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        apps.append({"app": name, "look": name.casefold() not in blocked, "act": acts.get(name.casefold(), True),
                     "note": ""})
    for key, name in blocked.items():
        if key not in seen:
            apps.append({"app": name, "look": False, "act": acts.get(key, False), "note": "Never looked at"})
    st = _safe(memory.status, {}) or {}
    today_act = _safe(lambda: memory.activity(group_by="app", limit=50), {}) or {}
    seen_min = round(float((today_act.get("totals") or {}).get("present_s") or 0) / 60)
    ui = ui_settings.load()
    from yuki.ui import tokens as T

    return {
        "autonomy": policy.describe(path=policy_path),
        "apps": apps[:16],
        "voice": {"wake": bool(live.get("wake", prefs.get("wake", True))),
                  "mic": live.get("mic") if "mic" in live else prefs.get("mic_device") or "",
                  "speaker": live.get("speaker") if "speaker" in live else prefs.get("speaker_device") or "",
                  "end_tone": bool(prefs.get("end_tone", True)),
                  "provider": str(prefs.get("tts_provider") or "gemini").lower(), "providers": VOICE_PROVIDERS,
                  "devices": devices, "hotkey": ""},
        "offers": {"enabled": str(nudges.get("offers", "yuki_only")) != "off"},
        "coach": {"enabled": bool(nudges.get("enabled", True)), "questions": bool(nudges.get("questions_enabled", True)),
                  "quiet_start": str(nudges.get("quiet_start", "00:30")), "quiet_end": str(nudges.get("quiet_end", "08:00"))},
        "privacy": {"paused": bool(st.get("paused")), "seen_min": seen_min,
                    "seen_apps": len([i for i in today_act.get("items") or [] if float(i.get("present_s") or 0) >= 30]),
                    "blocked_sites": len([s for s in user.get("blocked_domains", []) if str(s).strip()])},
        "calls": [{"app": c.get("name") or c.get("app") or "", "decision": c.get("decision") or "ask", "key": c.get("app")}
                  for c in _safe(memory.call_prefs, []) or []],
        "appearance": {"accent": ui.accent, "reduce_motion": bool(ui.reduce_motion),
                       "accents": [{"key": k, "label": str((v or {}).get("label") or k.capitalize())}
                                   for k, v in T.ACCENTS.items()]},
        "usage": yuki_usage(),
    }


def yuki_usage() -> dict[str, Any] | None:
    """How much memory Yuki's own processes hold right now (private bytes, tray + service + this window)."""
    try:
        import psutil

        from yuki.core.procs import is_yuki_process

        total = 0
        for p in psutil.process_iter(["pid"]):
            try:
                if is_yuki_process(p):
                    total += p.memory_full_info().private
            except Exception:
                continue
        return {"mb": total / 1e6, "cpu": None} if total else None
    except Exception:
        return None


# -- the thread (the drawer) ---------------------------------------------------------------------------------------


def chat_item(item: Any, now: float | None = None) -> dict[str, Any]:
    """One :class:`yuki.ui.chat_model.Item` as the drawer draws it: the words, the status, the fold's line and every
    real step with its time (counted as the bubble's ``step_times`` counts them, so they add up to the wait)."""
    now = time.time() if now is None else now
    out: dict[str, Any] = {"id": item.id, "kind": item.kind, "at": item.at, "text": item.text or "",
                           "source": item.source, "status": item.status, "meta": item.meta or "",
                           "running": bool(item.running), "jobs": list(item.jobs or [])}
    if item.kind == "assistant":
        times, _tail = item.step_times(now)
        out.update(work_line=item.work_line(now), thinking=" ".join((item.thinking or "").split())[:200],
                   question=item.question or "",
                   steps=[{"key": s.key, "kind": s.kind, "label": s.label, "ok": s.ok, "running": s.running,
                           "seconds": round(times.get(s.key, s.seconds(now)), 1), "started": s.started if s.running else None,
                           "result": (s.result or "")[:200] if s.ok is False else "", "depth": s.depth}
                          for s in item.real_steps()])
        if item.expanded is not None:
            out["expanded"] = item.expanded
    if item.kind == "nudge" and isinstance(item.nudge, dict):
        out["card"] = str(item.nudge.get("id"))
    return out


def runs_of(model: Any, now: float | None = None) -> list[dict[str, Any]]:
    """The requests running now, from the chat thread: the user's words, the current step, since when."""
    now = time.time() if now is None else now
    out = []
    items = list(model.items)
    for idx, item in enumerate(items):
        if item.kind != "assistant" or not item.running:
            continue
        steps = item.real_steps()
        cur = next((s for s in reversed(steps) if s.running), steps[-1] if steps else None)
        user = next((u for u in reversed(items[:idx]) if u.kind == "user"), None)
        line = (f"Step {len(steps)} · {cur.label}" if cur is not None else
                (item.question and "Waiting for your answer") or (" ".join((item.thinking or "").split())[:120] or "Thinking…"))
        title = request_title(user.text) if user is not None else "Working on your request"
        out.append({"item": item.id, "jobs": list(item.jobs or []), "title": title, "line": line,
                    "started_at": min([item.at] + [s.started for s in item.steps]), "status": item.status})
    return out


__all__ = ["SCHEMA", "build_shell", "build_home", "build_me", "build_work", "build_day", "build_playbook",
           "build_activity", "build_settings", "people_now", "person_from_index", "contact_card", "chat_item",
           "runs_of", "offer_card", "shift_times", "media_now", "read_pending", "week_days", "week_figure"]
