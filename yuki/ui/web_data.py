"""What the Lattice window's pages show, as JSON: built from memory in the window's own process. No Qt.

Each ``build_*`` takes an open :class:`~yuki.memory.api.MemoryClient` (or a stand-in with the same methods) and
returns a plain dict the page renders (``yuki/ui/web/pages/*.js``; the invented preview data in
``yuki/ui/web/fixtures/window.js``, written by ``scripts/make_web_fixture.py``, has exactly these shapes). They reuse
the Qt window's builders (:mod:`yuki.ui.window.data`) where those exist, and read memory only through its public
client: nothing here writes, calls a model, or reads what a message says to decide anything (no keyword logic).
Every builder runs on a worker thread and is safe to call again; a failing part leaves its section empty.

Times are epoch seconds (the page formats them); words the page shows are plain and honest about what memory does
not have (a memory without the people index, practice memory or goals gets the older views, probed with getattr).
E11-E14 (32-build-E11-E14.md): contact rhythm (``rhythm_view``), thread checklists, typed "involves" and goals
(``build_work``), the Playbook from ``workflows()`` (``build_playbook``), the ledger from the run traces with why,
result and Undo (``build_activity``), today's goals on Home's Now strip (``goals_today``).
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
    """``today, 9:41 AM`` / ``yesterday, 11:40 PM`` / ``2 Oct, 12:19 PM`` (12-hour, as people say it)."""
    if not at:
        return ""
    from yuki.ui.window.data import nat_time

    d = datetime.fromtimestamp(at)
    t = today or date.today()
    if d.date() == t:
        return f"today, {nat_time(at)}"
    if d.date() == t - timedelta(days=1):
        return f"yesterday, {nat_time(at)}"
    return f"{d.day} {d:%b}, {nat_time(at)}"


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


def build_shell(memory: Any, call_status: dict[str, Any] | None = None) -> dict[str, Any]:
    """The rail: who, the memory line, the call (from the tray's forwarded status when there is one) and how many
    questions wait (Home's badge, kept fresh while another page is open)."""
    st = _safe(memory.status, {}) or {}
    if st.get("paused"):
        line = "memory paused"
    elif st.get("service_running") is False:
        line = "memory is off"
    else:
        line = "memory on"
    needs = len(_safe(lambda: memory.questions(statuses=("open",), limit=12), []) or [])
    call = call_view(call_status) if call_status is not None else call_now(memory)
    return {"user": user_name(), "memory_line": line, "call": call, "needs": needs}


def call_now(memory: Any) -> dict[str, Any] | None:
    """The call going on now, for the rail's rec chip and Home's Now strip; ``None`` when there is none."""
    return call_view(_safe(memory.call_status, {}) or {})


def call_view(cs: dict[str, Any]) -> dict[str, Any] | None:
    """:func:`call_now` from a ``call_status()`` already read (the tray forwards each change: no memory read)."""
    cs = cs if isinstance(cs, dict) else {}
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


def home_call(call: dict[str, Any] | None, waiting: dict[str, Any] | None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Home's call tile and its Running rows (the call being remembered, the transcripts waiting) from
    :func:`call_view` and the waiting audio; also how the window patches Home from the tray's forwarded status."""
    now_call = None
    if call and (call.get("recording") or call.get("status") in ("asking", "waiting_headphones")):
        note = "Only your mic. Turned into text when the PC is free. Cards are held." if call.get("recording") else ""
        now_call = {**call, "note": note}
    running: list[dict[str, Any]] = []
    if call and call.get("recording"):
        running.append({"kind": "call", "live": True, "recording": True, "started_at": call.get("started_at"),
                        "title": f"Remembering your side of the {call.get('app') or ''} call".replace("  ", " "),
                        "detail": "Only your mic. Turned into text later, when the PC is free."})
    waiting = waiting or {}
    if int(waiting.get("calls") or 0):
        n = int(waiting["calls"])
        running.append({"kind": "waiting", "live": False, "title": f"{n} call{'s' if n != 1 else ''} waiting to be turned into text",
                        "detail": f"{dur_words(float(waiting.get('minutes') or 0) * 60)} in all. Done when the PC is free.",
                        "meta": "waiting"})
    return now_call, running


def build_home(memory: Any, *, requests_csv: Path | None = None, since: float | None = None,
               now: float | None = None, people: list[dict[str, Any]] | None = None,
               call_status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Home: the Now strip, Needs you (questions), Running (the call, transcripts waiting; live runs come from the
    tray), Offers, Since you were away (requests that finished, offers that ran) and the people lately.
    ``call_status``: the tray's forwarded ``call_status()`` (then the call and the transcripts waiting are not read)."""
    from yuki.ui.ledger import read_request_log

    now = time.time() if now is None else now
    since = since if since is not None else now - AWAY_HOURS * 3600
    call = call_view(call_status) if call_status is not None else call_now(memory)
    waiting = (call_status.get("waiting") if call_status is not None else _safe(memory.call_audio_waiting, {})) or {}
    now_call, running = home_call(call, waiting)
    now_strip = {"front": front_now(memory, now), "playing": None, "call": now_call}
    needs = [question_item(q) for q in (_safe(lambda: memory.questions(statuses=("open",), limit=12), []) or [])]
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
    now_strip["goals"] = goals_today(memory)
    # when Home next changes with the clock alone: an open offer running out, the oldest "since you were away" row
    # leaving the window (the window's live refresh rebuilds then; 42 verification, missing 4)
    moments = [epoch_of((card_offer(n) or {}).get("expires_at")) for n in cards if offer_open(n, now)]
    moments += [float(a["at"]) + AWAY_HOURS * 3600 for a in away[:INBOX_MAX] if a.get("at")]
    next_at = min((m for m in moments if m and m > now), default=None)
    return {"now": now_strip, "needs": needs, "running": running, "offers": offers, "away": away[:INBOX_MAX],
            "people": (people or [])[:6], "since": since, "next_at": next_at}


#: Goals the Now strip shows at most (today's, open first).
GOALS_SHOWN = 4


def goal_view(g: dict[str, Any]) -> dict[str, Any]:
    """One of memory's goals (``MemoryClient.goals``) as the page shows it: what it is (the user's own words when
    memory kept them), done or not, its thread."""
    text = " ".join(str(g.get("text") or g.get("words") or "").split())
    return {"ref": g.get("id"), "text": text, "status": g.get("status") or "open", "done": g.get("status") == "done",
            "thread": g.get("thread")}


def goals_today(memory: Any) -> list[dict[str, Any]]:
    """Today's goals for Home's Now strip (check-ins' goals, migration 30): open and unconfirmed ones first, then the
    ones done today. Probed: ``[]`` on a memory without goals."""
    goals = getattr(memory, "goals", None)
    if not callable(goals):
        return []
    rows = _safe(lambda: goals(today=True, statuses=("open", "unknown", "done")), []) or []
    out = [goal_view(g) for g in rows if isinstance(g, dict)]
    out = [g for g in out if g["text"]]
    out.sort(key=lambda g: g["done"])
    return out[:GOALS_SHOWN]


def thread_goals(memory: Any) -> dict[str, dict[str, Any]]:
    """The open goal of each thread of work (``W12`` -> the most recently updated open or unconfirmed goal of any
    scope that names the thread), for the quiet goal line on Work's thread cards. Probed: ``{}`` without goals."""
    goals = getattr(memory, "goals", None)
    if not callable(goals):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for g in _safe(lambda: goals(statuses=("open", "unknown"), limit=60), []) or []:
        if isinstance(g, dict) and g.get("thread") and g["thread"] not in out:
            view = goal_view(g)
            if view["text"]:
                out[str(g["thread"])] = view
    return out


# -- People (Me, and Home's "people lately") -----------------------------------------------------------------------


def _route(r: dict[str, Any]) -> dict[str, Any]:
    """One route as the routes table shows it: the app; the place (``DM`` for a direct chat, else the chat's name) and
    the handle the app shows them by (or the chat's kind) small beside it; the counts both ways and the last time."""
    last = max(epoch_of(r.get("last_mine")) or 0, epoch_of(r.get("last_theirs")) or 0) or None
    kind = str(r.get("kind") or "")
    chat, handle = " ".join(str(r.get("chat") or "").split()), " ".join(str(r.get("handle") or "").split())
    if chat or handle:
        place = "DM" if kind == "dm" else chat or handle
        sub = (("@" + (handle or chat).lstrip("@")) if kind == "dm" else
               (handle if handle and handle != place else {"group": "group", "channel": "channel"}.get(kind, "")))
    else:   # an older index without the chat's own fields: the route's label as it was
        place, sub = r.get("label") or "", ""
    return {"label": place, "sub": sub, "app": r.get("app") or "", "kind": kind,
            "mine": r.get("mine"), "theirs": r.get("theirs"), "last_at": last, "preferred": bool(r.get("preferred")),
            "share": r.get("share")}


def _day_label(iso: str | None) -> str:
    """``2026-09-24`` -> ``24 Sep``."""
    try:
        d = date.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return ""
    return f"{d.day} {d:%b}"


def rhythm_view(r: Any) -> tuple[list[Any] | None, dict[str, Any] | None]:
    """Memory's contact rhythm (``yuki.memory.contacts.rhythm``) as the page draws it: the 14 daily counts (``None``:
    before Yuki read any chat on this PC, a hollow dot) and the words around the figure - the axis (first day, the day
    Yuki started watching when it falls inside, today), the sum over the days known, the peak and its day, on how many
    days there were messages. ``(None, None)`` without per-day counts."""
    if isinstance(r, list):
        return r, None
    if not isinstance(r, dict) or not isinstance(r.get("days"), list):
        return None, None
    days, dates = list(r["days"]), list(r.get("dates") or [])
    watch = next((i for i, v in enumerate(days) if v is not None), None) if r.get("since_watch") else None
    info = {"total": int(r.get("total") or 0), "known": int(r.get("known_days") or 0),
            "active": int(r.get("active_days") or 0), "peak": int(r.get("peak") or 0),
            "peak_day": _day_label(r.get("peak_date")) if r.get("peak") else "",
            "start_label": _day_label(dates[0]) if dates else "", "watch_index": watch,
            "watch_label": _day_label(dates[watch]) if watch is not None and watch < len(dates) else "",
            "mine": r.get("mine"), "theirs": r.get("theirs")}
    return days, info


def _usual_line(value: Any) -> str:
    """Memory's ``usual`` as the dossier's "Usually" line: one plain sentence (memory writes it, migration 33). An
    older memory's route ref (``route:24``, the 3 Oct feedback item 1) is never shown."""
    text = " ".join(str(value or "").split()) if isinstance(value, str) else ""
    return "" if text.startswith("route:") else text


def _summary_view(s: Any) -> dict[str, Any] | None:
    """A person's one summary (memory's ``summary``: the person fact's headline and its shown form, said to the
    user) as the dossier shows it, with its pin; ``None`` when memory has none."""
    if not isinstance(s, dict) or not s.get("ref"):
        return None
    from yuki.ui.window.data import natural, plain

    text = " ".join(str(s.get("text") or "").split())
    return {"ref": str(s["ref"]), "headline": natural(" ".join(str(s.get("headline") or "").split())),
            "text": plain(text) if text else "", "pinned": bool(s.get("pinned"))}


def person_ids(p: dict[str, Any]) -> list[str]:
    """Who a person is, by every name the people index knows them by (their name, aliases, the handles and chat
    names of their direct routes), folded for an exact lookup: a call's people list names one friend once
    (the 3 Oct feedback item 18: two handles of one person). Identity from memory's own index, no guessing."""
    ids = [p.get("name"), *(p.get("aliases") or [])]
    for r in p.get("routes") or []:
        if isinstance(r, dict) and r.get("kind") == "dm":
            ids += [r.get("handle"), r.get("chat")]
    out: list[str] = []
    for i in ids:
        key = " ".join(str(i or "").split()).lstrip("@").casefold()
        if key and key not in out:
            out.append(key)
    return out


def person_from_index(p: dict[str, Any]) -> dict[str, Any]:
    """One row of memory's people index (``people_list()``) as Me's list and dossier show it. Message counts and the
    last contact come from the index; the contact rhythm (messages a day both ways, 14 days) from its ``rhythm``;
    the one summary (``summary``), the "Usually" line (``usual``, a plain sentence) and the relation the people
    list shows (the person fact's short label, said to the user) from memory (migration 33)."""
    routes = [_route(r) for r in p.get("routes") or [] if isinstance(r, dict)]
    count = sum(int(r["mine"] or 0) + int(r["theirs"] or 0) for r in routes if isinstance(r.get("mine"), int))
    preferred = next((r for r in routes if r["preferred"]), routes[0] if routes else None)
    apps = []
    for r in routes:
        if r["app"] and r["app"] not in apps:
            apps.append(r["app"])
    out = {"ref": str(p.get("ref") or p.get("name") or ""), "name": p.get("name") or "", "relation": p.get("relation") or "",
           "aliases": [str(a) for a in p.get("aliases") or [] if str(a).strip()][:4], "usual": _usual_line(p.get("usual")),
           "last_at": epoch_of(p.get("last_contact")), "count": count if routes else None, "routes": routes,
           "route": preferred["app"] if preferred else "", "prov": ("Seen in " + " and ".join(apps[:2])) if apps else "",
           "ids": person_ids(p)}
    summary = _summary_view(p.get("summary"))
    if summary is not None:
        out["summary"] = summary
    days, info = rhythm_view(p.get("rhythm"))
    if days is not None:
        out["rhythm"] = days
        if info is not None:
            out["rhythm_info"] = info
    return out


def contact_card(card: dict[str, Any]) -> dict[str, Any]:
    """``contact(ref)``'s full card trimmed for the dossier: routes with counts (never the message samples: the
    window does not need the user's own messages), the person's other facts as key facts (``{ref, h, d}``: memory's
    headline and its shown form; the summary is not repeated here), usual, aliases, relation."""
    from yuki.ui.window.data import natural, plain

    facts = []
    for f in card.get("facts") or []:
        if isinstance(f, dict):
            text = plain(" ".join(str(f.get("text") or "").split()))
            head = natural(" ".join(str(f.get("headline") or "").split()))
            if text or head:
                facts.append({"ref": str(f.get("ref") or ""), "h": head or text, "d": text if head else ""})
        elif str(f or "").strip():
            facts.append({"ref": "", "h": plain(natural(" ".join(str(f).split()))), "d": ""})
    out = {"found": bool(card.get("found", True)), "relation": card.get("relation") or "",
           "aliases": [str(a) for a in card.get("aliases") or []][:4], "usual": _usual_line(card.get("usual")),
           "routes": [_route(r) for r in card.get("routes") or [] if isinstance(r, dict)], "facts": facts[:12]}
    days, info = rhythm_view(card.get("rhythm"))
    if days is not None:
        out["rhythm"] = days
        if info is not None:
            out["rhythm_info"] = info
    return out


def people_now(memory: Any, limit: int = 40, *, use_index: bool = True) -> tuple[str, list[dict[str, Any]]]:
    """(``index`` | ``portrait``, people): memory's people index when this build has it (probed) and it holds someone,
    else the portrait's person facts (no routes: the page says routes appear once Yuki has learned them). An empty
    index (``[]``: none built yet, or memory could not read it) is no index (review 34 M5). ``use_index=False``: the
    caller already found the index empty."""
    people_list = getattr(memory, "people_list", None) if use_index else None
    if callable(people_list):
        rows = _safe(lambda: people_list(limit=limit), None)
        people = [person_from_index(p) for p in rows if isinstance(p, dict) and p.get("name")] \
            if isinstance(rows, list) else []
        if people:
            return "index", people
    from yuki.ui.window.data import portrait_item

    facts = [f for f in (_safe(lambda: memory.portrait_facts_with_evidence(2), []) or []) if f.get("kind") == "person"]
    out, seen = [], set()
    for f in facts:
        it = portrait_item(f)
        key = str(f.get("subject") or it.title).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"ref": it.ref, "name": f.get("subject") or it.title, "relation": it.summary or "",
                    "prov": it.source, "routes": [], "last_at": max([e.at for e in it.evidence] or [it.since]),
                    "summary": {"ref": it.ref, "headline": it.summary, "text": it.text, "pinned": bool(f.get("pinned"))},
                    "fact": {"ref": it.ref, "title": it.title, "text": it.text}})
    out.sort(key=lambda p: -(p.get("last_at") or 0))
    return "portrait", out


# -- Me ------------------------------------------------------------------------------------------------------------


#: Me's portrait, by topic (the 3 Oct design, item 10): each group's memory kinds. A fact is shown once: a person's
#: fact is a link row to their page here, routines live in Routines, a fact waiting in To review only there.
FACT_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("work", "Work", ("work", "open_loop")), ("people", "People", ("person",)),
    ("interests", "Interests", ("interest",)), ("prefs", "Preferences and rules", ("preference",)),
    ("about", "About you", ("personality",)))
#: Below this (the model's own confidence) a fact Yuki wrote says "not sure" (the Qt window's UNSURE_BELOW).
UNSURE_BELOW = 0.4
#: The review queue's flags in the words the page shows.
REVIEW_FLAG = {"unsure": "not sure", "new": "new"}
#: How far back the window looks for the call an entry was said on (its place and who was on it).
CALLS_DAYS = 21


def name_lookup(people: list[dict[str, Any]] | None) -> dict[str, str]:
    """Every name the people index knows someone by (:func:`person_ids`) -> the name the page shows them by."""
    out: dict[str, str] = {}
    for p in people or []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        for key in p.get("ids") or person_ids(p):
            out.setdefault(key, str(p["name"]))
    return out


def call_people(people: list[Any], lookup: dict[str, str]) -> list[str]:
    """A call's people (the digest's ``[{name, how}]``) as the page names them: each person once, by their index
    name when a handle is theirs (two handles of one friend are one friend), those the digest only thought likely
    as "maybe <name>"."""
    best: dict[str, tuple[int, str]] = {}
    order: list[str] = []
    for p in people or []:
        raw = p.get("name") if isinstance(p, dict) else p
        how = str(p.get("how") or "on_call_screen") if isinstance(p, dict) else "on_call_screen"
        raw = " ".join(str(raw or "").split())
        if not raw:
            continue
        name = lookup.get(raw.lstrip("@").casefold(), raw)
        key = name.casefold()
        rank = 1 if how == "likely" else 0
        if key not in best:
            order.append(key)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, name)
    return [best[k][1] if best[k][0] == 0 else f"maybe {best[k][1]}" for k in order]


#: Calls read for provenance are kept this long (a call's place and people don't change; ``calls_in`` over days
#: costs about half a second on a real memory, and Me is rebuilt as the journal grows).
CALLS_CACHE_S = 600.0
_calls_cache: dict[tuple[int, int, int], tuple[float, list[Any]]] = {}


def call_places(memory: Any, lookup: dict[str, str] | None = None, now: float | None = None,
                days: float = CALLS_DAYS, since: float | None = None) -> dict[int, dict[str, Any]]:
    """call id -> ``{"label", "app", "people"}`` for the calls since ``since`` (default: the last ``days``): the
    place in one format ("Discord, #general voice (Friends)", memory's place label; an older digest's own words;
    "" when the screen never showed it), the app, and who was on it (:func:`call_people`). One ``calls_in`` read,
    kept :data:`CALLS_CACHE_S`; ``{}`` without calls."""
    now = time.time() if now is None else now
    since = max(now - days * 86400, since if since is not None else now - days * 86400)
    key = (id(memory), int(since // 3600), int(now // CALLS_CACHE_S))
    hit = _calls_cache.get(key)
    if hit is None:
        rows = _safe(lambda: memory.calls_in(since=since, until=now, limit=400), []) or []
        _calls_cache.clear()
        _calls_cache[key] = hit = (now, rows)
    out: dict[int, dict[str, Any]] = {}
    for c in hit[1]:
        if not isinstance(c, dict):
            continue
        view = {"label": call_where(c, default=""), "app": str(c.get("app") or ""),
                "people": call_people(c.get("people") or [], lookup or {})}
        for cid in c.get("call_ids") or []:
            if isinstance(cid, int):
                out[cid] = view
    return out


def _call_id(ref: Any) -> int | None:
    t = str(ref or "")
    return int(t[5:]) if t.startswith("call:") and t[5:].isdigit() else None


def heard_line(app: str, call: dict[str, Any] | None, at: float | None, *, channel_known: bool = True,
               label: str = "", now: float | None = None) -> str:
    """Where something was said, in the one format every row uses (the 3 Oct feedback item 8): "Heard in Discord,
    #general voice (Friends), 1 October, 2:07 AM, with Riya and Dev"; a call whose channel the screen never showed
    "Heard on a Discord call, …"; said with no call, "You said it to me while WhatsApp was open, …"."""
    from yuki.ui.window.data import nat_when

    when = nat_when(at, today=datetime.fromtimestamp(time.time() if now is None else now).date())
    if call is not None or label:
        place = (call or {}).get("label") or label
        app = (call or {}).get("app") or app
        head = f"Heard in {place}" if place and channel_known else f"Heard on a {app or 'voice'} call"
        people = (call or {}).get("people") or []
        tail = f", with {_and(people)}" if people else ""
        return f"{head}{', ' + when if when else ''}{tail}"
    return f"You said it to me while {app} was open{', ' + when if when else ''}" if app else (
        f"You said it to me, {when}" if when else "You said it to me")


def _and(names: list[str]) -> str:
    names = [n for n in names if n]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def fact_prov(f: dict[str, Any], calls: dict[int, dict[str, Any]]) -> str:
    """Where a fact came from, in plain words: "You told me, 25 Sep", "Heard in Discord, #general voice (Friends),
    30 Sep, with Riya", "Seen in Slack, 1 Oct", or "From my reading of your week, 1 Oct"."""
    from yuki.ui.window.data import epoch

    ev = [e for e in f.get("evidence") or [] if isinstance(e, dict)]
    last = max([epoch(e.get("at")) or 0.0 for e in ev] + [epoch(f.get("since")) or 0.0]) or None
    day = short_day(last)
    if f.get("origin") == "user":
        return f"You told me{', ' + day if day else ''}"
    for e in ev:
        cid = _call_id(e.get("call"))
        if cid is not None and cid in calls:
            c = calls[cid]
            head = f"Heard in {c['label']}" if c["label"] else f"Heard on a {c['app'] or 'voice'} call"
            return f"{head}, {short_day(epoch(e.get('at')))}" + (f", with {_and(c['people'])}" if c["people"] else "")
    apps: dict[str, int] = {}
    for e in ev:
        app = str(e.get("app") or "")
        if app and not app.startswith("call"):
            apps[app] = apps.get(app, 0) + 1
    if apps:
        return f"Seen in {max(apps, key=lambda a: apps[a])}{', ' + day if day else ''}"
    if any(str(e.get("app") or "").startswith("call") for e in ev):
        return f"Heard on a call{', ' + day if day else ''}"
    return f"From my reading of your week{', ' + day if day else ''}"


def week_seen(f: dict[str, Any], now: float | None = None) -> list[bool]:
    """The last 7 days (today last): on which of them the evidence a fact rests on was seen (the routine's week
    dots: "days seen in the last 7", measured from its cited entries)."""
    from yuki.ui.window.data import epoch

    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now).date()
    days = {datetime.fromtimestamp(t).date() for e in f.get("evidence") or [] if isinstance(e, dict)
            and (t := epoch(e.get("at"))) is not None}
    return [today - timedelta(days=6 - i) in days for i in range(7)]


def fact_view(f: dict[str, Any], calls: dict[int, dict[str, Any]], now: float | None = None) -> dict[str, Any]:
    """One portrait fact as Me shows it: a one-line headline (memory's short ``summary``), the detail (its
    ``shown`` form: second person, natural dates; until memory writes it, the fact's text with its dates in natural
    form), where it came from, its own pin, and the flags (a joke, not sure, you told me)."""
    from yuki.ui.window.data import epoch, first_sentence, natural, plain

    shown = " ".join(str(f.get("shown") or "").split())
    detail = plain(shown) if shown else plain(natural(str(f.get("text") or "")))
    head = natural(" ".join(str(f.get("summary") or "").split())) or first_sentence(detail, 90)
    conf = f.get("confidence")
    out = {"ref": f.get("ref"), "kind": f.get("kind") or "", "h": head, "d": detail if detail != head else "",
           "prov": fact_prov(f, calls), "pinned": bool(f.get("pinned")), "you": f.get("origin") == "user",
           "joke": f.get("aspect") == "joke", "subject": f.get("subject") or "",
           "unsure": f.get("origin") != "user" and conf is not None and conf < UNSURE_BELOW,
           "importance": f.get("importance") or 3, "at": epoch(f.get("created_at")) or epoch(f.get("since")) or 0}
    if f.get("kind") == "routine":
        out["week"] = week_seen(f, now)
        out["conf_word"] = conf_word(conf) or ("you told me" if f.get("origin") == "user" else "")
    return out


def review_view(q: dict[str, Any], facts_by_ref: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Memory's "To review" queue (``review_queue()``, at most 5 a day) as the page shows it: each fact's headline,
    its detail, its flag ("new" / "not sure") and where it came from; the day's cap, what's left, the merges."""
    from yuki.ui.window.data import natural, plain

    items = []
    for it in q.get("items") or []:
        if not isinstance(it, dict) or not it.get("ref"):
            continue
        known = facts_by_ref.get(str(it["ref"])) or {}
        text = plain(" ".join(str(it.get("text") or "").split())) or known.get("d") or ""
        head = natural(" ".join(str(it.get("headline") or "").split())) or known.get("h") or text
        items.append({"ref": str(it["ref"]), "h": head, "d": text if text != head else "",
                      "flag": REVIEW_FLAG.get(str(it.get("flag") or ""), str(it.get("flag") or "")),
                      "prov": known.get("prov") or ""})
    merged = q.get("merged") if isinstance(q.get("merged"), dict) else {}
    return {"items": items, "cap": int(q.get("cap") or 5), "left": int(q.get("left_today") or 0),
            "done": int(q.get("reviewed_today") or 0), "waiting": int(q.get("waiting") or 0),
            "merged": int(merged.get("count") or 0)}


def misheard_view(memory: Any, calls: dict[int, dict[str, Any]], fallback: list[Any],
                  now: float | None = None, lookup: dict[str, str] | None = None) -> dict[str, Any]:
    """"Maybe misheard" (memory's ``misheard()``: unanswered uncertain entries of the last week, newest first): the
    words as heard (the entry's own quote when memory kept it, else its text) and where they were heard, in the one
    format (:func:`heard_line`). An older memory: the Qt view's uncertain entries (``fallback``)."""
    from yuki.ui.window.data import natural, plain

    fn = getattr(memory, "misheard", None)
    got = _safe(fn, None) if callable(fn) else None
    if not isinstance(got, dict):
        items = [{"ref": i.ref, "text": i.text, "quote": "", "prov": heard_line(i.title, None, i.since, now=now)} for i in fallback]
        return {"items": items, "total": len(items), "direct": False}
    items = []
    for m in got.get("items") or []:
        if not isinstance(m, dict) or not m.get("ref"):
            continue
        src = m.get("source") if isinstance(m.get("source"), dict) else {}
        quote = " ".join(str(src.get("quote") or "").split())
        cid = _call_id(m.get("call"))
        call = calls.get(cid) if cid is not None else None
        if cid is not None and call is None:   # memory's own label for it (its digest's place) and who was on it
            call = {"label": m.get("heard_in") or "", "app": m.get("app") or "",
                    "people": call_people(m.get("people") or [], lookup or {})}
        items.append({"ref": str(m["ref"]), "text": plain(natural(str(m.get("text") or ""))), "quote": quote,
                      "prov": heard_line(str(m.get("app") or ""), call, m.get("at"),
                                         channel_known=bool(m.get("channel_known", True)), now=now)})
    return {"items": items, "total": int(got.get("total") or len(items)), "direct": callable(getattr(memory, "resolve_heard", None))}


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
             people: tuple[str, list[dict[str, Any]]] | None = None, now: float | None = None) -> dict[str, Any]:
    """Me (the 3 Oct design): the people and their dossiers (one summary each, the "Usually" line, key facts on
    open); your portrait as a "To review" queue (at most 5 a day) and topic groups of one-line headlines (each fact
    once; a person's fact links to their page); Routines (title, week dots, how sure; detail on click) with the
    day-specific observations under "Not routines"; Maybe misheard with where it was heard. Every fact carries its own
    pin. Built from memory's shown forms (second person, natural dates); nothing here rewrites a word."""
    now = time.time() if now is None else now
    source, persons = people if people is not None else people_now(memory)
    lookup = name_lookup(persons)
    raw = [f for f in (_safe(memory.portrait_facts_with_evidence, []) or []) if isinstance(f, dict) and f.get("ref")]
    # the calls a fact was heard on (their place and people): read only for the span those entries cover
    heard = [t for f in raw for e in f.get("evidence") or [] if isinstance(e, dict) and _call_id(e.get("call")) is not None
             and (t := epoch_of(e.get("at"))) is not None]
    misheard_fn = getattr(memory, "misheard", None)
    calls = call_places(memory, lookup, now, since=min(heard) - 6 * 3600) if heard else {}
    views = [fact_view(f, calls, now) for f in raw]
    by_ref = {str(v["ref"]): v for v in views}
    rq = getattr(memory, "review_queue", None)
    review = review_view(_safe(rq, {}) or {}, by_ref) if callable(rq) else None
    in_review = {it["ref"] for it in (review or {}).get("items") or []}
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for v in views:
        by_kind.setdefault(v["kind"], []).append(v)
    for items in by_kind.values():
        items.sort(key=lambda v: -(v["at"] or 0))
    # a person's fact is their dossier's summary: here it is a link row to their page, never the detail again
    person_ref = {str(p.get("name") or "").casefold(): p.get("ref") for p in persons}
    groups = []
    for key, name, kinds in FACT_GROUPS:
        facts = [v for k in kinds for v in by_kind.get(k, []) if v["ref"] not in in_review]
        if key == "people":
            facts = [{"ref": v["ref"], "kind": "person", "h": v["h"], "person": person_ref.get(v["subject"].casefold()) or v["subject"],
                      "pinned": v["pinned"], "at": v["at"], "importance": v["importance"]} for v in facts]
        facts.sort(key=lambda v: -(v["at"] or 0))
        groups.append({"key": key, "name": name, "facts": facts})
    rules = [rule_json(r) for r in (_safe(memory.rules, []) or [])]
    routines = [v for v in by_kind.get("routine", []) if v["ref"] not in in_review]
    # the day-specific observations memory keeps as "behaviour" facts: not routines, said apart (report 38 §12.5)
    not_routines = [v for v in by_kind.get("behaviour", []) if v["ref"] not in in_review]
    fallback: list[Any] = []
    if not callable(misheard_fn):
        from yuki.ui.window.data import build_me as qt_build_me

        view = _safe(lambda: qt_build_me(memory), None)
        fallback = list(view.unsure) if view is not None else []
    total = sum(len(g["facts"]) for g in groups) + len(rules) + len(routines) + len(not_routines) + len(in_review)
    return {"source": source, "people": persons, "groups": groups, "rules": rules, "routines": routines,
            "not_routines": not_routines, "review": review, "total": total,
            "misheard": misheard_view(memory, calls, fallback, now, lookup), "pending": pending or {},
            "can": {"pin": callable(getattr(memory, "pin_fact", None)), "review": callable(rq),
                    "heard": callable(getattr(memory, "resolve_heard", None))}}


# -- Work and the Day ----------------------------------------------------------------------------------------------


def thread_checks(t: dict[str, Any]) -> dict[str, Any] | None:
    """A thread's checklist as Work's dot progress draws it: ``{"done", "total", "next"}`` from memory's ``checks``
    (milestones that landed on it / its open items, E12); ``None`` when there is nothing to count."""
    c = t.get("checks")
    if not isinstance(c, dict) or not int(c.get("total") or 0):
        return None
    return {"done": int(c.get("done") or 0), "total": int(c.get("total") or 0), "next": c.get("next") or None}


def thread_involves(t: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(people, apps and things) a thread involves, typed by memory (``involves_typed``: a person is a name in the
    people index, matched by identity) - people as their index name, once each; without the typing every name goes
    to the second list (the page shows those as the mono apps line, never as people)."""
    typed = t.get("involves_typed")
    if not isinstance(typed, list):
        return [], [str(x) for x in t.get("involves") or [] if str(x).strip()]
    people: list[str] = []
    things: list[str] = []
    for e in typed:
        if not isinstance(e, dict) or not str(e.get("name") or "").strip():
            continue
        if e.get("type") == "person":
            name = str(e.get("person") or e["name"])
            if name not in people:
                people.append(name)
        elif e["name"] not in things:
            things.append(str(e["name"]))
    return people, things


def build_work(memory: Any, *, week: dict[str, Any] | None = None) -> dict[str, Any]:
    """Threads of work (memory's ``threads``), most recently touched first, with their checklists, the people and
    the apps they involve, their open goal; and the week figure."""
    from yuki.ui.window.data import natural

    rows = _safe(lambda: memory.threads(statuses=("active", "paused", "done"), limit=24), []) or []
    goals = thread_goals(memory)
    threads = []
    for t in rows:
        people, things = thread_involves(t)
        goal = goals.get(f"W{t.get('id')}") if t.get("id") is not None else None
        threads.append({"ref": t.get("ref"), "title": t.get("title") or "", "status": t.get("status") or "",
                        # where it stands, said to you (memory's shown form, migration 33); until it is written, the
                        # state with only its date and time formats changed
                        "state": " ".join(str(t.get("shown") or "").split()) or natural(" ".join(str(t.get("state") or "").split())),
                        "open": [str(x) for x in t.get("open") or []],
                        "involves": [str(x) for x in t.get("involves") or []], "people": people, "apps": things,
                        "last_at": epoch_of(t.get("last_seen")), "checks": thread_checks(t),
                        "goal": goal["text"] if goal else None})
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


def call_gists(c: dict[str, Any]) -> list[str]:
    """A call's gists said to the user (memory's ``shown`` form, migration 33: "your friends chose …, which shocked
    you"), the stored gist with its dates in natural form until that is written."""
    from yuki.ui.window.data import natural

    out = []
    for g in c.get("gists") or []:
        if isinstance(g, dict):
            text = " ".join(str(g.get("shown") or "").split()) or natural(" ".join(str(g.get("gist") or "").split()))
            if text:
                out.append(text)
    return out


def call_where(c: dict[str, Any], default: str | None = None) -> str:
    """Where a call was, in the one format every row uses: "Discord, #general voice (Friends)" (memory's place
    label); a digest written before the structured place, its own words as they are; else the app."""
    for key in ("places", "where"):
        for p in c.get(key) or []:
            if str(p).strip():
                return " ".join(str(p).split())
    return str(c.get("app") or "") if default is None else default


def build_day(memory: Any, day: date | None = None, *, now: float | None = None,
              week: list[dict[str, Any]] | None = None, people: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The Day tab: the day in slices (the skyline), today's totals, sessions (each opens to its whole title and the
    time in every app), calls (where, who, the gists said to you), and the week by hour."""
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
    from yuki.ui.window.data import nat_time, natural

    lanes = {ln.key: ln.label for ln in v.lanes}
    sessions = []
    for s in v.sessions:
        fs = sum(seg.present_s for seg in v.segments if seg.fullscreen and seg.state == "present"
                 and seg.start < s.end and seg.end > s.start)
        full = natural(getattr(s, "title_full", "") or s.title)
        # the row shows the title and its first apps; opening it shows the whole title and the time in every app
        # (the 3 Oct ask D: "clicking on each session should show the time spent per app and the entire session title")
        sessions.append({"a": int((s.start - since) // 60), "b": int((s.end - since) // 60), "start": hm(s.start),
                         "end": hm(s.end), "title": natural(s.title), "app": lanes.get(s.lane, s.lane),
                         "mode": "full" if fs > s.present_s / 2 else "active",
                         "apps": [[name, round(secs / 60)] for name, secs in s.apps if secs >= 30],
                         **({"full": full} if full != natural(s.title) else {})})
    calls_rows = _safe(lambda: memory.calls_in(since=since, until=since + 86400, limit=30), []) or []
    lookup = name_lookup(people)
    calls = []
    for c in calls_rows:
        cs, ce = epoch_of(c.get("start")), epoch_of(c.get("end")) or now
        if cs is None:
            continue
        calls.append({"a": int((max(cs, since) - since) // 60), "b": int((min(ce, since + 86400) - since) // 60),
                      "start": hm(cs), "end": hm(ce), "app": c.get("app") or "", "live": bool(c.get("ongoing")),
                      "recorded": bool(c.get("recorded")), "min": round(float(c.get("seconds") or 0) / 60),
                      "gists": call_gists(c), "where": call_where(c),
                      "people": call_people(c.get("people") or [], lookup), "started_at": cs})
    t = v.totals
    longest, top_full = longest_focus(v), _top_full(v)
    lane_top = max(v.lanes, key=lambda ln: ln.present_s - ln.fullscreen_s, default=None)
    n_calls = len(calls)
    totals = [["At the PC", dur_words(t.get("present_s", 0)), f"{t.get('switches', 0)} app switches"],
              ["Active", dur_words(max(0.0, t.get("present_s", 0) - t.get("fullscreen_s", 0))),
               f"{lane_top.label} {dur_words(lane_top.present_s)}" if lane_top else ""],
              ["Full screen", dur_words(t.get("fullscreen_s", 0)), top_full],
              ["Calls", dur_words(t.get("call_s", 0)), f"{n_calls} call{'s' if n_calls != 1 else ''}" if n_calls else "none"],
              ["Longest focus", dur_words(longest[0]), f"{longest[1]}, {nat_time(longest[2])}" if longest[1] else ""]]
    offs = []
    for s, e in sorted(off):
        if offs and s - offs[-1][1] < 300:
            offs[-1] = (offs[-1][0], max(offs[-1][1], e))
        else:
            offs.append((s, e))
    return {"date_label": f"{day:%A} {day.day} {day:%b}", "today": is_today, "first": nat_time(v.first_at), "last": nat_time(v.last_at),
            "now_min": int((now - since) // 60) if is_today else None,
            "off": [{"from": nat_time(s), "to": nat_time(e)} for s, e in offs if e - s >= 600],
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


#: NEW blocks on the Playbook page at most (the design shows one); more tentative items go to their lists.
PROPOSALS_SHOWN = 2
#: How each practice object came to be (``playbooks.origin``), in the words the Playbook page's side line uses.
ORIGIN_WORDS = {"distill": "From a run that worked", "run": "From a run that worked",
                "postmortem": "From a time I got it wrong", "user": "In your words", "taught": "You taught me",
                "miner": "Seen in what you do", "knowhow": "From what I learned before"}


def app_words(apps: list[Any]) -> str:
    """The apps a playbook is for, as the page names them (``chrome`` -> ``Chrome``; none: ``Any app``)."""
    from yuki.core.appnames import prettify

    names = [prettify(str(a)) or str(a) for a in apps or [] if str(a).strip()]
    return " · ".join(dict.fromkeys(names)) if names else "Any app"


def _learned_line(c: dict[str, Any]) -> str:
    """Where a playbook came from and how much evidence it has, e.g. ``From a run that worked, 29 Sep · seen on 3
    days``."""
    head = ORIGIN_WORDS.get(str(c.get("origin") or ""), "Learned")
    since = short_day(epoch_of(c.get("since")))
    ev = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    bits = []
    if int(ev.get("days") or 0) > 1:
        bits.append(f"seen on {ev['days']} days")
    if int(ev.get("corrected") or 0):
        bits.append(f"corrected {ev['corrected']}×")
    return f"{head}{', ' + since if since else ''}" + (" · " + ", ".join(bits) if bits else "")


def procedure_view(c: dict[str, Any]) -> dict[str, Any]:
    """A procedure or habit from ``workflows()`` as the Playbook page's "How you do things" row: title, the line under
    it (a habit's rule, or what it avoids), its steps, the app, uses and last use, confidence (meter and word) and where
    it came from. Disabled ones say so."""
    steps = [str(s) for s in c.get("steps") or [] if str(s).strip()]
    when = ""
    if c.get("kind") == "habit":
        when = " ".join(str(c.get("text") or "").split())
    elif c.get("avoid"):
        when = f"Not: {' '.join(str(c['avoid']).split())}"
    uses = int(c.get("uses") or 0)
    return {"ref": c.get("id"), "kind": c.get("kind"), "title": c.get("title") or "", "when": when, "steps": steps,
            "app": app_words(c.get("apps") or []), "uses": uses, "last": short_day(epoch_of(c.get("last_used"))),
            "conf": c.get("confidence"), "conf_word": conf_word(c.get("confidence")) or str(c.get("label") or ""),
            "state": c.get("state"), "disabled": bool(c.get("disabled")), "learned": _learned_line(c)}


def lesson_view(c: dict[str, Any]) -> dict[str, Any]:
    """A pitfall or style note as a Lessons row: what it is about (bold), what went wrong, what to do instead, how
    often it was used and where it came from."""
    # practice memory's own text() appends " Instead: ..." and " Not: ..." to what happened; those two parts have
    # their own fields here (``instead``, ``avoid``), so the "because" line is the part before them
    text = str(c.get("text") or "").split(" Instead: ")[0].split(" Not: ")[0]
    return {"ref": c.get("id"), "kind": c.get("kind"), "dont": c.get("title") or "",
            "because": " ".join(text.split()),
            "do": " ".join(str(c.get("instead") or "").split()), "used": int(c.get("uses") or 0),
            "from": _learned_line(c), "conf": c.get("confidence"), "disabled": bool(c.get("disabled"))}


def proposal_view(c: dict[str, Any]) -> dict[str, Any]:
    """What Yuki is not sure of yet - a playbook seen once, or a question it would like to ask - as the NEW proposal
    block: title, the basis line, its steps; ``question`` ones are answered in the user's words."""
    if c.get("kind") == "question":
        return {"ref": c.get("id"), "kind": "question", "title": " ".join(str(c.get("text") or c.get("title") or "").split()),
                "basis": "I’d like to know, so I stop guessing.", "steps": []}
    steps = [str(s) for s in c.get("steps") or [] if str(s).strip()]
    ev = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    seen = int(ev.get("seen") or 0)
    basis = " ".join(str(c.get("text") or "").split()) if not steps else ""
    when = short_day(epoch_of(c.get("since")))
    tail = f"Seen {seen} time{'s' if seen != 1 else ''}" if seen else "Seen once"
    return {"ref": c.get("id"), "kind": c.get("kind"), "title": c.get("title") or "",
            "basis": (basis + (" " if basis else "") + f"{tail}{', ' + when if when else ''}; I’ll use it once you say so.").strip(),
            "steps": steps, "app": app_words(c.get("apps") or [])}


def build_playbook(memory: Any) -> dict[str, Any]:
    """The Playbook page from practice memory (``workflows()``, Phase C): NEW proposals (seen once, or a question),
    procedures and habits ("How you do things"), lessons (pitfalls and style notes), with uses, last use and
    confidence; then the standing rules and requests. Without practice memory, today's know-how instead (probed)."""
    from yuki.ui.window.data import learned_items

    workflows = getattr(memory, "workflows", None)
    wf = _safe(workflows, {}) if callable(workflows) else {}
    wf = wf if isinstance(wf, dict) else {}
    available = bool(wf.get("available"))
    procs = [procedure_view(c) for c in wf.get("procedures") or [] if isinstance(c, dict)] if available else []
    lessons = [lesson_view(c) for c in wf.get("lessons") or [] if isinstance(c, dict)] if available else []
    # what Yuki is not sure of yet: questions first, at most PROPOSALS_SHOWN as the NEW blocks (the design shows one);
    # the other tentative ways and lessons stay visible in their own lists, marked as seen once
    pending = [c for c in wf.get("proposals") or [] if isinstance(c, dict)] if available else []
    pending.sort(key=lambda c: c.get("kind") != "question")
    props = [proposal_view(c) for c in pending[:PROPOSALS_SHOWN]]
    for c in pending[PROPOSALS_SHOWN:]:
        if c.get("kind") in WAY_KINDS:
            procs.append({**procedure_view(c), "conf_word": "tentative, seen once", "tentative": True})
        elif c.get("kind") in ("pitfall", "style"):
            lessons.append({**lesson_view(c), "tentative": True})
    know = [] if available else [{"title": k.title, "line": k.line, "app": k.app}
                                 for k in (_safe(lambda: learned_items(memory, limit=24), []) or [])]
    rules = [rule_json(r) for r in (_safe(memory.rules, []) or [])]
    standing = []
    for r in _safe(memory.standing_requests, []) or []:
        text = " ".join(str(r.get("quote") or r.get("condition") or "").split())
        if text:
            standing.append({"ref": r.get("ref"), "text": text, "since": short_day(epoch_of(r.get("created_at")))})
    return {"proposals": props, "procedures": procs, "know_how": know, "lessons": lessons, "rules": rules,
            "standing": standing, "workflows_available": available, "learning": wf.get("learning") or None}


# -- Activity ------------------------------------------------------------------------------------------------------


#: Practice memory's kinds: ways of doing (shown as "using …") and lessons (shown as "lesson …").
WAY_KINDS = ("procedure", "habit")
#: Memory's Undo window for something Yuki added (offers: yuki.memory.offers.UNDO_WITHIN_S; to-dos alike).
LEDGER_UNDO_S = UNDO_WITHIN_S
#: A request log row and a run trace are the same request when they started this close (the log keeps seconds).
SAME_REQUEST_S = 2.5


def _clip(text: Any, n: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def step_what(step: dict[str, Any]) -> str:
    """One acting step in plain past-tense words, from the one label function of every run surface
    (:func:`yuki.ui.step_words.describe_step`): the brain's own words for the step when it gave them, else the
    control's name and the app the trace kept; never the command, typed text, address or keys (revamp 38 §8c). A
    voice hand's step (its own tool names: ``window``, ``open_app``, ``media_key``) reads as the thinker's would."""
    from yuki.ui.step_words import ABOUT, describe_step, hand_input

    try:
        args = json.loads(step.get("input") or "{}")
    except (TypeError, ValueError):
        args = {}
    args = args if isinstance(args, dict) else {}
    about = {"element": args.get("name"), "role": args.get("role"), "app": step.get("app_name") or step.get("app")}
    tool, words_from = hand_input(str(step.get("tool") or ""), args)
    if tool != str(step.get("tool") or ""):
        about.pop("element")   # a hand's "name" is the app it opened (now the query), not a control
    return describe_step(tool, {**words_from, ABOUT: about}, done=True, limit=90)


def step_result(step: dict[str, Any], *, stopped: bool = False) -> tuple[str, str]:
    """(the result in plain words, its status for the glyph): done and checked on screen, done, ran but not shown,
    didn't work, or stopped before it finished (``stopped``: the run was cancelled, a structured outcome). Never the
    tool's own result text or a timing (the user's 3 Oct feedback: plain words only); those stay in the trace."""
    if step.get("ok") is False:
        return ("stopped before it finished", "stopped") if stopped else ("didn’t work", "failed")
    if step.get("verdict") is True:
        return "done, checked on screen", "verified"
    if step.get("verdict") is False:
        return "ran, but the screen didn’t show it", "unverified"
    if step.get("expected") or step.get("kind") == "memory":
        return "done", "done"
    return "done; I couldn’t check it", "unverified"


def _todo_for(step: dict[str, Any], todos: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The to-do a ``todo`` step added (its own words, added within two minutes of the step), still open."""
    try:
        args = json.loads(step.get("input") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(args, dict) or args.get("action") != "add" or not str(args.get("text") or "").strip():
        return None
    words = " ".join(str(args["text"]).split())
    at = float(step.get("at") or 0)
    for t in todos:
        since = epoch_of(t.get("since"))
        if t.get("source") == "user" and t.get("status") == "open" and " ".join(str(t.get("text") or "").split()) == words \
                and since is not None and abs(since - at) <= 120:
            return t
    return None


def _rule_for(step: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The rule a ``remember_rule`` step kept: the user's rule written closest to the step, within two minutes
    (matched by time, a structured field; never by the step's input)."""
    at = float(step.get("at") or 0)
    near = [(abs(since - at), r) for r in rules
            if isinstance(r, dict) and (since := epoch_of(r.get("at"))) is not None and abs(since - at) <= 120]
    return min(near, key=lambda p: p[0])[1] if near else None


def _kept_words(record: dict[str, Any] | None) -> str:
    """The words of the to-do or rule a step kept, from memory's own record, quoted ("“Water the basil”")."""
    words = _clip((record or {}).get("text"), 80)
    return f" “{words}”" if words else ""


def run_rows(run: dict[str, Any], todos: list[dict[str, Any]], now: float,
             rules: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One request's rows for the ledger: each acting step (time, what, where, why: the user's words and any
    procedure or lesson it used, result in plain words, Undo where memory can take it back: a to-do it added), or
    one "Answered" row when nothing acted. A to-do or rule Yuki kept says which, from memory's record of it. No
    "can't undo" text (the user, 3 Oct: no undo talk on the run surfaces)."""
    voice = run.get("lane") == "voice" or run.get("source") == "hand"
    words = _clip(run.get("words"), 120)
    why = (f"you said “{words}”" if voice else f"you typed “{words}”") if words else (
        "you asked by voice" if voice else "you asked")
    cost_bits = []
    if run.get("ms"):
        cost_bits.append(f"{run['ms'] / 1000:.0f} s" if run["ms"] >= 10000 else f"{run['ms'] / 1000:.1f} s")
    if run.get("cost_usd"):
        cost_bits.append(money(run["cost_usd"]))
    cost = ", ".join(cost_bits)
    wrong = f"That wasn’t right: “{words}”. " if words else ""
    steps = run.get("steps") or []
    if not steps:
        result = {"final": "answered", "error": "failed", "cancelled": "stopped", "withdrawn": "replaced by a newer request",
                  "abandoned": "abandoned"}.get(str(run.get("outcome") or ""), str(run.get("outcome") or "answered"))
        status = "failed" if run.get("outcome") == "error" else "stopped" if run.get("outcome") in (
            "cancelled", "withdrawn", "abandoned") else "done"
        return [{"id": f"run:{run.get('key')}", "kind": "request", "f": "failed" if status == "failed" else "did",
                 "at": float(run.get("at") or now), "what": f"Answered “{words}”" if words else "Answered you",
                 "where": "Yuki", "cost": cost, "because": "because", "why": why, "chips": [], "result": result,
                 "status": status, "wrong_text": wrong}]
    rows = []
    stopped = str(run.get("outcome") or "") in ("cancelled", "withdrawn", "abandoned")
    for i, s in enumerate(steps):
        result, status = step_result(s, stopped=stopped)
        chips = [{"kind": "memory" if c.get("kind") in WAY_KINDS else "lesson", "text": c.get("title") or ""}
                 for c in s.get("cited") or [] if c.get("title")]
        row = {"id": f"run:{run.get('key')}:{s.get('step')}", "kind": "action", "f": "failed" if status == "failed" else "did",
               "at": float(s.get("at") or run.get("at") or now), "what": step_what(s),
               "where": s.get("app_name") or ("Yuki" if s.get("kind") == "memory" else "Windows"),
               "cost": cost if i == len(steps) - 1 else "", "because": "because",
               "why": why + (", using how you do it" if any(c["kind"] == "memory" for c in chips) else "")
               + (", keeping a lesson in mind" if any(c["kind"] == "lesson" for c in chips) else ""),
               "chips": chips, "result": result, "status": status, "wrong_text": wrong}
        todo = _todo_for(s, todos) if s.get("tool") == "todo" else None
        rule = _rule_for(s, rules or []) if s.get("tool") == "remember_rule" and s.get("ok") is not False else None
        if todo is not None or rule is not None:
            row["what"] += _kept_words(todo or rule)
        if todo is not None and row["at"] + LEDGER_UNDO_S > now:
            row["undo"] = {"todo": todo.get("id"), "until": row["at"] + LEDGER_UNDO_S, "label": "Undo"}
        rows.append(row)
    return rows


def call_rows(calls: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """The calls whose side Yuki kept, as ledger rows: what (recording or recorded), where, why (the user's standing
    rule for that app, or their yes on the call's card), and what came of it."""
    out = []
    for c in calls:
        if not c.get("recorded"):
            continue
        app = c.get("app") or "a"
        consent = c.get("consent")
        why = (f"rule: {app} calls, your mic only" if consent == "always" else
               "you said yes on the call’s card" if consent == "yes" else "you said yes to keeping your side")
        live = bool(c.get("live"))
        mins = int(c.get("min") or 0)
        result = ("live" if live else "turned into text" if c.get("gists") else
                  c.get("status") or "waiting to be turned into text")
        out.append({"id": f"call:{c.get('start_at')}", "kind": "call", "f": "calls", "at": c.get("start_at") or now,
                    "what": f"Recording your side of a {app} call" if live else
                    f"Recorded a {dur_words(mins * 60)} {app} call", "where": c.get("where") or app, "cost": "", "because": "because",
                    "why": why, "chips": [], "result": result, "status": "live" if live else "verified"})
    return out


def build_activity(memory: Any, *, requests_csv: Path | None = None, now: float | None = None,
                   days: int = 7, people: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The ledger (E14), newest first: every acting step of Yuki's requests from the run traces (practice memory) with
    why - the user's words, a procedure or lesson it used - its result and an Undo window where memory can take it
    back (a to-do it added; an offer that ran); requests the traces don't have, from the request log; the cards Yuki
    showed (why: the card's reason, or the user's standing request); the calls it kept (why: the user's call rule).
    Filters by ``f``: did / failed / calls. Plus what it saw today (time by app, counts only) and the week's calls."""
    from yuki.ui.ledger import read_request_log

    now = time.time() if now is None else now
    since = now - days * 86400
    rows: list[dict[str, Any]] = []
    ledger = getattr(memory, "run_ledger", None)
    runs = (_safe(lambda: ledger(since=since, until=now, limit=150), []) or []) if callable(ledger) else []
    todos = (_safe(lambda: memory.todos(include_done=False), []) or []) if any(
        s.get("tool") == "todo" for r in runs for s in r.get("steps") or []) else []
    rules = (_safe(lambda: memory.rules(), []) or []) if callable(getattr(memory, "rules", None)) and any(
        s.get("tool") == "remember_rule" for r in runs for s in r.get("steps") or []) else []
    for run in runs:
        rows.extend(run_rows(run, todos, now, rules))
    run_starts = sorted(float(r.get("at") or 0) for r in runs)
    lanes = {"worker": "Typed request", "front_desk": "Quick answer", "voice": "Voice"}
    for i, r in enumerate(read_request_log(requests_csv, since, limit=120) if requests_csv else []):
        if any(abs(r["at"] - a) <= SAME_REQUEST_S for a in run_starts):
            continue   # the run trace above has this request, step by step
        words, cost = request_row_words(r)
        status = {"done": "done", "failed": "failed", "stopped": "stopped"}.get(r["state"], "done")
        voice = r.get("where") == "voice"
        rows.append({"id": f"r{i}", "kind": "request", "f": "failed" if status == "failed" else "did", "at": r["at"],
                     "what": request_title(r.get("what") or ""),
                     "where": lanes.get(r.get("where") or "", (r.get("where") or "").replace("_", " ").capitalize()),
                     "cost": cost, "because": "because", "why": "you asked by voice" if voice else "you asked",
                     "chips": [], "result": words, "status": status})
    standing = {}
    for s in _safe(lambda: memory.standing_requests(include_ended=True), []) or []:
        if isinstance(s, dict) and s.get("ref"):
            standing[str(s["ref"])] = " ".join(str(s.get("quote") or s.get("condition") or "").split())
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
        rule = standing.get(str(n.get("ref") or "")) if str(n.get("ref") or "").startswith("request:") else None
        why = ((f"rule: “{_clip(rule, 140)}”" if rule else "rule: something you asked me to watch for")
               if rule is not None else " ".join(str(n.get("reason") or "").split())[:200])
        row = {"id": f"c{n.get('id')}", "kind": "card", "f": "failed" if outcome in ("expired", "gone") else "did",
               "card": str(n.get("id")), "at": at, "what": " ".join(str(n.get("text") or "").split())[:160],
               "where": f"Card · {card_title(n)}", "because": "because" if why else "", "why": why, "chips": [],
               "result": result, "status": "failed" if outcome in ("expired", "gone") else "done"}
        if c and c["undo_until"] and c["undo_until"] > now:
            row["undo"] = {"card": c["card"], "until": c["undo_until"], "label": "Undo"}
        rows.append(row)
    midnight = datetime.combine(datetime.fromtimestamp(now).date(), datetime.min.time()).timestamp()
    act = _safe(lambda: memory.activity(since=midnight, until=now, group_by="app", limit=20), {}) or {}
    seen = [{"app": it.get("label") or it.get("app") or "", "min": round(float(it.get("present_s") or 0) / 60, 1),
             "visits": int(it.get("visits") or 0)} for it in act.get("items") or [] if float(it.get("present_s") or 0) >= 30]
    calls = []
    lookup = name_lookup(people)
    for c in reversed(_safe(lambda: memory.calls_in(since=since, until=now, limit=30), []) or []):
        cs = epoch_of(c.get("start"))
        if cs is None:
            continue
        gists = call_gists(c)
        status = "" if gists else ("Live now. Your side is kept; it’s turned into text when the PC is free."
                                   if c.get("ongoing") and c.get("recorded") else
                                   "Waiting to be turned into text." if c.get("recorded") else "Not recorded.")
        calls.append({"start_at": cs, "end_at": None if c.get("ongoing") else epoch_of(c.get("end")),
                      "live": bool(c.get("ongoing")), "min": round(float(c.get("seconds") or 0) / 60),
                      "spoke_min": round(float(c.get("speech_s") or 0) / 60) if c.get("recorded") else None,
                      "app": c.get("app") or "", "where": call_where(c),
                      "people": call_people(c.get("people") or [], lookup), "gists": gists[:6],
                      "recorded": bool(c.get("recorded")), "status": status, "consent": c.get("consent")})
    rows.extend(call_rows(calls, now))
    rows.sort(key=lambda r: r["at"], reverse=True)
    did_today = sum(1 for r in rows if r["at"] >= midnight and r["kind"] in ("action", "request"))
    no_reason = sum(1 for r in rows if r["at"] >= midnight and not r.get("why"))
    rows, groups = ledger_groups(rows[:150], runs, now)
    return {"rows": rows, "groups": groups, "counts": {"did_today": did_today, "no_reason": no_reason}, "seen": seen,
            "calls": calls}


#: A request's outcome (practice memory's run ``outcome``) in the words the Activity group's header shows.
RUN_OUTCOME = {"final": "Done", "error": "Couldn’t finish", "cancelled": "Stopped",
               "withdrawn": "Replaced by a newer request", "abandoned": "Left unfinished"}


def took_words(seconds: float | None) -> str:
    """``4 s`` / ``1 min 9 s`` / ``12 min``: how long something took, as a person says it."""
    if seconds is None or seconds <= 0:
        return ""
    s = int(round(seconds))
    if s < 60:
        return f"{max(1, s)} s"
    m, s = divmod(s, 60)
    return f"{m} min {s} s" if m < 10 and s else f"{m} min"


def ledger_groups(rows: list[dict[str, Any]], runs: list[dict[str, Any]],
                  now: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The ledger grouped by the request each action belonged to (the 3 Oct ask C): one group per request (the run's
    key, which every step of it already carries), card and call; the group says what you asked in your words, how it
    went, when and how long; its rows are the steps, opened on a click. No Undo on any row (the 3 Oct ask A: the
    Activity tab has no Undo section). Returns (rows, each with its ``group``, and the groups, newest first)."""
    by_run = {f"run:{r.get('key')}": r for r in runs}
    groups: dict[str, dict[str, Any]] = {}
    out_rows = []
    for row in rows:
        row = {k: v for k, v in row.items() if k not in ("undo", "cant_undo")}
        rid = str(row.get("id") or "")
        gid = next((g for g in (rid, rid.rsplit(":", 1)[0]) if g in by_run), rid) if rid.startswith("run:") else rid
        row["group"] = gid
        out_rows.append(row)
        g = groups.get(gid)
        if g is None:
            run = by_run.get(gid)
            if run is not None:
                voice = run.get("lane") == "voice" or run.get("source") == "hand"
                words = _clip(run.get("words"), 160)
                outcome = RUN_OUTCOME.get(str(run.get("outcome") or ""), "Done")
                g = {"id": gid, "kind": "request", "at": float(run.get("at") or row["at"]),
                     "title": request_title(words) if words else ("Something you asked by voice" if voice else "Something you asked"),
                     "said": "you said" if voice else "you typed", "outcome": outcome,
                     "took": took_words((run.get("ms") or 0) / 1000), "cost": money(run.get("cost_usd")) if run.get("cost_usd") else "",
                     "status": "failed" if run.get("outcome") == "error" else "stopped" if run.get("outcome") in (
                         "cancelled", "withdrawn", "abandoned") else "done",
                     "steps": 0, "failed": 0, "wrong_text": row.get("wrong_text") or ""}
            else:
                g = {"id": gid, "kind": row.get("kind") or "request", "at": row["at"], "title": row.get("what") or "",
                     "said": "", "outcome": (row.get("result") or "")[:1].upper() + (row.get("result") or "")[1:],
                     "took": "", "cost": row.get("cost") or "", "status": row.get("status") or "", "steps": 0, "failed": 0,
                     "where": row.get("where") or "", "why": row.get("why") or "", "card": row.get("card"),
                     "wrong_text": row.get("wrong_text") or ""}
            groups[gid] = g
        if row.get("kind") == "action":
            g["steps"] += 1
            g["failed"] += row.get("status") == "failed"
            g["at"] = min(g["at"], row["at"])
    for g in groups.values():
        if g["kind"] == "request" and g["steps"] and g["failed"] and g["outcome"] == "Done":
            g["outcome"] = f"Done, but {'one step' if g['failed'] == 1 else str(g['failed']) + ' steps'} didn’t work"
        g["f"] = "calls" if g["kind"] == "call" else "failed" if g["status"] == "failed" or (
            g["failed"] and g["failed"] == g["steps"]) else "did"
    return out_rows, sorted(groups.values(), key=lambda g: -(g["at"] or 0))


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


# -- change stamps (the window's live refresh: 42-window-live-refresh.md §6.2) -------------------------------------

#: Each page's memory tables (the ones its builder reads). Work's week figure and Activity's "Seen today, by app"
#: read the timeline, so it is in their stamps (review F3): a revisit after STALE_S, or the open page's slow fallback,
#: rebuilds them while you are at the PC; the hash still skips a build that shows nothing new.
PAGE_TABLES: dict[str, tuple[str, ...]] = {
    "home": ("questions", "nudges", "timeline", "goals", "contacts", "calls"),
    "work": ("work_threads", "goals", "timeline"),
    "day": ("timeline", "calls"),
    "me": ("contacts", "portraits", "portrait_facts", "conversation_facts", "journal"),
    "playbook": ("playbooks", "questions", "standing_requests", "knowhow", "conversation_facts"),
    "activity": ("runs", "nudges", "todos", "calls", "timeline"),
    "shell": ("questions",),
}


def page_stamps(memory: Any, page: str, *, requests_csv: Path | None = None) -> Any:
    """A page's change stamps: memory's per-table stamps (:func:`yuki.memory.api.change_stamps`) and, for Home and
    Activity, the request log's size and time. Equal stamps: nothing the page shows from memory moved, so it is not
    built. ``None`` (build it) for a page without stamps (Settings: files and the tray's state) or an older memory."""
    tables = PAGE_TABLES.get(page)
    if not tables:
        return None
    try:
        from yuki.memory.api import change_stamps
    except ImportError:
        return None
    stamps = change_stamps(memory, tables)
    if not stamps or any(v is None for v in stamps.values()):
        return None
    out: list[Any] = [[t, stamps.get(t)] for t in tables]
    if page in ("home", "activity") and requests_csv is not None:
        try:
            st = Path(requests_csv).stat()
            out.append(["requests", st.st_size, st.st_mtime_ns])
        except OSError:
            out.append(["requests", None])
    return json.dumps(out, default=str)


__all__ = ["SCHEMA", "build_shell", "page_stamps", "build_home", "build_me", "build_work", "build_day", "build_playbook",
           "build_activity", "build_settings", "people_now", "person_from_index", "contact_card", "chat_item",
           "rhythm_view", "goals_today", "thread_goals", "run_rows", "call_rows", "step_what", "step_result",
           "runs_of", "offer_card", "shift_times", "media_now", "read_pending", "week_days", "week_figure"]
