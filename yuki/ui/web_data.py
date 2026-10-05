"""What the Lattice window's pages show, as JSON: built from memory in the window's own process. No Qt.

Each ``build_*`` takes an open :class:`~yuki.memory.api.MemoryClient` (or a stand-in with the same methods) and
returns a plain dict the page renders (``yuki/ui/web/pages/*.js``; the invented preview data in
``yuki/ui/web/fixtures/window.js``, written by ``scripts/make_web_fixture.py``, has exactly these shapes). They reuse
the Qt window's builders (:mod:`yuki.ui.window.data`) where those exist, and read memory only through its public
client: nothing here writes, calls a model, or reads what a message says to decide anything (no keyword logic).
Every builder runs on a worker thread and is safe to call again; a failing part leaves its section empty.

Times are epoch seconds (the page formats them); words the page shows are plain and honest about what memory does
not have (a memory without the people index, practice memory or goals gets the older views, probed with getattr).
E11-E14 (32-build-E11-E14.md): contact rhythm (``rhythm_view``), the Playbook from ``workflows()``
(``build_playbook``), the ledger from the run traces with why, result and Undo (``build_activity``), today's goals on
Home's Now strip (``goals_today``). appTodo (4 Oct): the to-do list on Home and Work (``todo_lists``), and no threads
anywhere in the window: Work is the list and today's sessions, Home's Now tile the current session (``now_session``).
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
    """:func:`call_now` from a ``call_status()`` already read (the tray forwards each change: no memory read).

    ``{"app", "kind", "label", "where", "started_at", "recording", "status", "status_words", "line", "title",
    "sentence"}``: what is kept is said from the status's ``mic`` and ``tracks`` (:mod:`yuki.ui.call_words`):
    ``line`` "Recording · Discord + you", ``title`` "Remembering the Discord call", ``sentence`` "Your mic and
    Discord’s sound."; ``recording`` is the top-level flag (Stop is offered while it is True)."""
    from yuki.ui import call_words as CW

    cs = cs if isinstance(cs, dict) else {}
    call = cs.get("call") if isinstance(cs.get("call"), dict) else None
    view = CW.live_view(cs)
    if not call and view is None:
        return None
    row = view or call or {}   # the row being recorded (a game recording while a new call's card is up), else the call
    base = {"app": row.get("app") or "", "kind": "game" if CW.is_game(row) else "call", "label": CW.head_label(row),
            "where": CW.session_name(row)}
    status = str(row.get("status") or "")
    if status in ("declined", "unanswered", "stopped", "not_asked") and not cs.get("recording"):
        started = epoch_of(row.get("session_started_at")) or epoch_of(row.get("detected_at"))
        return {**base, "started_at": started, "recording": False, "status": status,
                "status_words": {"declined": "not recording: you said no", "unanswered": "not recording",
                                 "stopped": "stopped remembering", "not_asked": "not recording"}.get(status, ""),
                "line": "", "title": "", "sentence": ""}
    started = epoch_of(row.get("session_started_at")) or epoch_of(row.get("recording_since"))
    line = (view or {}).get("line") or ""
    words = {"asking": "asking if I may record it", "waiting_headphones": "waiting for headphones"}
    recording = bool(cs.get("recording"))
    return {**base, "started_at": started, "recording": recording, "status": status,
            "status_words": line if (recording and line) else words.get(status, line),
            "line": line, "title": CW.remembering_title(cs), "sentence": (view or {}).get("sentence") or ""}


# -- Home --------------------------------------------------------------------------------------------------------


#: Home's Now tile shows only what is happening now (review F1): the timeline's newest row reaches this close to now
#: (the recorder writes the open stretch every 30 s, ``timeline.FLUSH_EVERY_S``; longer silence is the PC asleep,
#: memory paused or the session locked) ...
NOW_FRESH_S = 120.0
#: ... and the time without input after your last stretch at the PC is under this (a few minutes reading still counts
#: as now; longer, you have left). A power row (off, asleep) after it always ends it.
NOW_AWAY_S = 5 * 60.0
#: Away from the PC this long starts a new session (``timeline.BREAK_S``, the Day tab's breaks).
NOW_BREAK_S = 10 * 60.0


def front_now(memory: Any, now: float) -> dict[str, Any] | None:
    """The app in front now: the timeline's latest stretch at the PC (app, window title, since when it has been in
    front: ``since_at``) and since when you have been at the PC without a break (``session_at``: the current
    session's start, within the 3 hours read). ``None`` unless that stretch is current (:data:`NOW_FRESH_S`,
    :data:`NOW_AWAY_S`; no power row after it) or when the app was withheld. Times are compared, nothing more."""
    rows = [r for r in (_safe(lambda: memory.day_timeline(now - 3 * 3600, now), []) or []) if isinstance(r, dict)]
    if not rows or now - max(float(r.get("end") or 0) for r in rows) > NOW_FRESH_S:
        return None   # nothing recorded lately: asleep, paused, locked
    at = None
    for i in range(len(rows) - 1, -1, -1):
        state = rows[i].get("state")
        if state == "present":
            at = i
            break
        if state != "away":
            return None   # the PC went off or to sleep after your last stretch at it
    if at is None:
        return None
    r = rows[at]
    if now - float(r.get("end") or 0) > NOW_AWAY_S or not (r.get("app") or r.get("process")):
        return None   # away since, or an app memory records without its name
    start = float(r.get("start") or now)
    # earlier rows of the same app run on into this one: since when it has been in front
    for prev in reversed(rows[:at]):
        if prev.get("state") == "present" and prev.get("app") == r.get("app") and float(prev.get("end") or 0) >= start - 60:
            start = float(prev.get("start") or start)
        else:
            break
    session = start
    for prev in reversed(rows[:at]):
        if prev.get("state") != "present":
            continue   # away a while or the PC asleep: the gap between stretches at the PC decides
        if float(prev.get("end") or 0) < session - NOW_BREAK_S:
            break
        session = min(session, float(prev.get("start") or session))
    title = "" if r.get("withheld") else (r.get("title") or "")
    return {"app": r.get("app") or r.get("process") or "", "title": " ".join(title.split()), "since_at": start,
            "session_at": session}


def now_session(memory: Any, now: float, front: dict[str, Any] | None) -> dict[str, Any] | None:
    """What you are doing right now, for Home's Now tile: the app in front (``front``: :func:`front_now`) since when
    (``since_at``: the page counts the minutes itself, so the data stays the same from one minute to the next),
    titled by the current session (review F2): the newest titled episode overlapping the time since the session
    began (``session_at``), the open one memory still rewrites included (``title``, ``summary``, ``ref``; the finer
    one when two end together); else no title (the page shows the app; never the raw window title). ``at``: where
    Work opens for it (the moment the app came to the front, inside the session holding now). ``None`` while nothing
    is in front now. Reads only (one short episodes read); times are compared, nothing judges what the work is."""
    if not front or not front.get("app"):
        return None
    since = float(front.get("since_at") or now)
    began = min(since, float(front.get("session_at") or since))
    best: tuple[float, float, dict[str, Any]] | None = None
    for e in _safe(lambda: memory.episodes(began - 3600, now + 60, limit=24), []) or []:
        if not isinstance(e, dict):
            continue
        title = " ".join(str(e.get("title") or "").split())
        s, u = epoch_of(e.get("start")), epoch_of(e.get("end"))
        if not title or s is None or u is None or u <= began or s >= now:
            continue   # untitled, or over before this session began (another session's)
        rank = (u, -(u - s))
        if best is None or rank > best[:2]:
            best = (rank[0], rank[1], e)
    ep = best[2] if best else None
    return {"app": str(front.get("app") or ""), "since_at": since, "at": round(since, 1),
            "title": " ".join(str(ep.get("title") or "").split()) if ep else None,
            "summary": (" ".join(str(ep.get("summary") or "").split()) or None) if ep else None,
            "ref": ep.get("ref") if ep else None}


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
    sentence = str((call or {}).get("sentence") or "")
    if call and (call.get("recording") or call.get("status") in ("asking", "waiting_headphones")):
        note = (" ".join(x for x in (sentence, "Turned into text when the PC is free. Cards are held.") if x)
                if call.get("recording") else "")
        now_call = {**call, "note": note}
    running: list[dict[str, Any]] = []
    if call and call.get("recording"):   # Stop remembering is on this row: there whenever anything is kept
        running.append({"kind": "call", "live": True, "recording": True, "started_at": call.get("started_at"),
                        "title": call.get("title") or f"Remembering the {call.get('where') or 'call'}",
                        "detail": " ".join(x for x in (sentence, "Turned into text later, when the PC is free.") if x)})
    waiting = waiting or {}
    if int(waiting.get("calls") or 0):
        n = int(waiting["calls"])
        running.append({"kind": "waiting", "live": False, "title": f"{n} call{'s' if n != 1 else ''} waiting to be turned into text",
                        "detail": f"{dur_words(float(waiting.get('minutes') or 0) * 60)} in all. Done when the PC is free.",
                        "meta": "waiting"})
    return now_call, running


# -- the to-do list (appTodo, 4 Oct): Home's "Your list" and Work's "To-dos" ---------------------------------------
# The live test of 4 Oct: Yuki added seven to-dos by voice, and the window showed them nowhere. The list is memory's
# merged to-do list (MemoryClient.todos: the same one the agent's todo tool lists), so what Yuki says is on it is what
# the window shows.

#: Open to-dos Home's "Your list" shows (the rest under "see all", on Work).
HOME_TODOS = 5
#: To-dos done lately in Work's fold, at most (memory keeps the ones done in the last 14 days in the list).
TODO_DONE_SHOWN = 10
#: Where an item came from, said to you (memory's source names; a to-do you added says nothing).
TODO_FROM = {"chat": "you told me", "call": "said on a call", "commitment": "I said I’d do it", "loop": "I noticed it"}
#: The refs whose Correct memory takes (``correct_item``; an open loop corrects its portrait fact).
TODO_PREFIXES = ("todo:", "commit:", "loop:")


def due_epoch(due: Any) -> tuple[float | None, bool]:
    """memory's ``due`` (``"2026-10-05"``: that day, no time; or a local ISO date and time) as ``(epoch, all_day)``."""
    s = str(due or "").strip()
    if not s:
        return None, False
    try:
        if len(s) == 10:
            return datetime.strptime(s, "%Y-%m-%d").timestamp(), True
        return datetime.fromisoformat(s).timestamp(), False
    except ValueError:
        return None, False


def due_words(at: float | None, all_day: bool, now: float) -> tuple[str, bool]:
    """When a to-do is due, as people say it, and whether that is past: ``due today, 5 PM`` / ``due tomorrow`` /
    ``due 6 October, 9 AM``; ``was due yesterday``. A deterministic format of memory's due time, nothing else."""
    from yuki.ui.window.data import nat_day, nat_time

    if at is None:
        return "", False
    today = datetime.fromtimestamp(now).date()
    day_of = datetime.fromtimestamp(at).date()
    over = day_of < today if all_day else at < now
    if day_of == today + timedelta(days=1):
        day = "tomorrow"
    elif day_of not in (today, today - timedelta(days=1)) and abs((day_of - today).days) <= 6:
        day = f"{day_of:%A}"   # within the week either way: "due Monday", "was due Wednesday" (the Work page, 4 Oct)
    else:
        day = nat_day(at, today=today)
    words = day if all_day else f"{day}, {nat_time(at)}"
    return (f"was due {words}" if over else f"due {words}"), over


def week_words(at: float | None, now: float, *, in_sentence: bool = False) -> str:
    """When something happened, the one way the Work page says it: ``8:01 PM`` today (``at 8:01 PM`` inside a
    sentence, ``in_sentence``: "I saw it done at 8:01 PM"), ``yesterday``, a weekday within the last six days
    (``Wednesday``), else ``25 September``. A deterministic format, nothing else."""
    from yuki.ui.window.data import nat_day, nat_time

    if not at:
        return ""
    today, d = datetime.fromtimestamp(now).date(), datetime.fromtimestamp(at).date()
    if d == today:
        return f"at {nat_time(at)}" if in_sentence else nat_time(at)
    if d == today - timedelta(days=1):
        return "yesterday"
    if timedelta(days=0) < today - d <= timedelta(days=6):
        return f"{d:%A}"
    return nat_day(at, today=today)


def ahead_words(at: float | None, all_day: bool, now: float) -> str:
    """When something comes up: ``today 4 PM``, ``tomorrow 11 AM``, ``Monday 4 PM`` within the week, else ``12 October
    4 PM`` (a day alone without the time)."""
    from yuki.ui.window.data import nat_day, nat_time

    if not at:
        return ""
    today, d = datetime.fromtimestamp(now).date(), datetime.fromtimestamp(at).date()
    day = ("today" if d == today else "tomorrow" if d == today + timedelta(days=1)
           else f"{d:%A}" if timedelta(days=0) < d - today <= timedelta(days=6) else nat_day(at, today=today))
    return day if all_day else f"{day} {nat_time(at)}"


def todo_view(t: dict[str, Any], now: float) -> dict[str, Any]:
    """One item of memory's to-do list as the window shows it: its words, where it came from (``from``; nothing for
    your own), when it is due in natural words (``due``, ``over`` when that is past), when it was added, an open
    loop's evidence (dates made natural; a commitment's is memory's note about you, not shown), and whether Undo can
    put it back after Done (``undo``: a to-do you added)."""
    from yuki.ui.window.data import nat_day, natural

    due_at, all_day = due_epoch(t.get("due"))
    due, over = due_words(due_at, all_day, now)
    ref, source = str(t.get("id") or ""), str(t.get("source") or "")
    since = epoch_of(t.get("since"))
    evidence = natural(" ".join(str(t.get("evidence") or "").split())) if source == "loop" else ""
    today = datetime.fromtimestamp(now).date()
    tomorrow = due_at is not None and datetime.fromtimestamp(due_at).date() == today + timedelta(days=1)
    return {"ref": ref, "text": " ".join(str(t.get("text") or "").split()),
            "title": " ".join(str(t.get("title") or "").split()) or None, "from": TODO_FROM.get(source, ""),
            "due": due, "over": over, "due_at": due_at, "all_day": all_day, "since": since,
            "added": nat_day(since, today=today) if since else "",
            "evidence": evidence or None, "undo": ref.startswith("todo:"),
            "status": "open" if t.get("status") == "open" else "done",
            # the Work page (4 Oct): which source and whose, and whether it is due tomorrow (its head's count)
            "source": source, "tomorrow": bool(tomorrow)}


def todo_lists(memory: Any, now: float, *, include_done: bool = False,
               rows: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(open, done)``: the open to-dos, the ones due first (soonest, so the overdue lead) then the undated newest
    first; with ``include_done``, the ones done lately (memory's order: newest first). Empty when memory can't say.
    ``rows``: memory's ``todos()`` already read."""
    if rows is None:
        rows = _safe(lambda: memory.todos(include_done=include_done), []) or []
    views = [todo_view(t, now) for t in rows if isinstance(t, dict) and t.get("id")]
    opened = sorted((v for v in views if v["status"] == "open"),
                    key=lambda v: (v["due_at"] is None, v["due_at"] or 0.0, -(v["since"] or 0.0)))
    done = [v for v in views if v["status"] != "open"][:TODO_DONE_SHOWN]
    return opened, done


def todo_moment(items: list[dict[str, Any]], now: float) -> float | None:
    """The next moment the list changes with the clock alone: a timed due passing ("due" becomes "was due"; a day's
    due turns at midnight, which every page already rebuilds at)."""
    return min((v["due_at"] for v in items if v.get("due_at") and not v.get("all_day") and v["due_at"] > now),
               default=None)


def todo_can(memory: Any) -> dict[str, bool]:
    """Which of the list's buttons this memory takes: Done / Not needed, Undo of a Done, Correct, Add."""
    return {"end": callable(getattr(memory, "end_todo", None)), "undo": callable(getattr(memory, "reopen_todo", None)),
            "correct": callable(getattr(memory, "correct_item", None)), "add": callable(getattr(memory, "add_todo", None))}


def home_list(memory: Any, now: float) -> list[dict[str, Any]]:
    """Home's Your list: the items of Work's Your list (:func:`work_columns` ``list``: the same split, the same
    order), so a loop where someone owes you (Waiting on) or a promise you made (You said you'd) never shows on Home
    as your to-do. The board is read without the chats (``work_board(replies=False)``; an older memory without that
    reads the whole board; none at all: nothing judged yet, as Work shows it then)."""
    rows = _safe(lambda: memory.todos(include_done=False), []) or []
    read = getattr(memory, "work_board", None)
    board: dict[str, Any] = {}
    if callable(read):
        try:
            board = read(now, replies=False) or {}
        except TypeError:
            board = _safe(lambda: read(now), {}) or {}
        except Exception:  # noqa: BLE001 - memory can't say: nothing judged
            board = {}
    return work_columns(rows, board if isinstance(board, dict) else {}, [], now)["list"]


def build_home(memory: Any, *, requests_csv: Path | None = None, since: float | None = None,
               now: float | None = None, people: list[dict[str, Any]] | None = None,
               call_status: dict[str, Any] | None = None, pending: dict[str, Any] | None = None) -> dict[str, Any]:
    """Home: the Now strip (what you are doing now: :func:`now_session`; playing; the call), Needs you (questions), Running (the call, transcripts waiting; live runs come from the
    tray), Offers, Your list (Work's Your list, :func:`home_list`; the page shows :data:`HOME_TODOS`; ``n`` is how many), Since
    you were away (requests that finished, offers that ran) and the people lately.
    ``call_status``: the tray's forwarded ``call_status()`` (then the call and the transcripts waiting are not read);
    ``pending``: corrections sent on a to-do and not landed yet."""
    from yuki.ui.ledger import read_request_log

    now = time.time() if now is None else now
    since = since if since is not None else now - AWAY_HOURS * 3600
    call = call_view(call_status) if call_status is not None else call_now(memory)
    waiting = (call_status.get("waiting") if call_status is not None else _safe(memory.call_audio_waiting, {})) or {}
    now_call, running = home_call(call, waiting)
    front = front_now(memory, now)
    # Now: what you are doing right now, from the current session (the 4 Oct decision: threads left the window)
    now_strip = {"session": now_session(memory, now, front), "playing": None, "call": now_call}
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
    logged = read_request_log(requests_csv, since, limit=INBOX_MAX) if requests_csv else []
    for r in request_words_from_memory(memory, logged):
        words, _cost = request_row_words(r)
        took = took_words(r.get("wall_s"))
        # how long it took, as a person says it; never the raw "7.0 s, 3 steps, 8¢" line (the 3 Oct feedback)
        away.append({"kind": "request", "title": request_title(r.get("what") or ""), "at": r["at"], "state": r["state"],
                     "changed": [{"text": words, "state": "failed" if r["state"] == "failed" else "done"}],
                     "cost": f"took {took}" if took else ""})
    away.sort(key=lambda x: x.get("at") or 0, reverse=True)
    away = away[:INBOX_MAX]
    cost_row = ai_cost_row(Path(requests_csv).parent if requests_csv else None, now)
    if cost_row is not None:
        away.append(cost_row)
    now_strip["goals"] = goals_today(memory)
    # when Home next changes with the clock alone: an open offer running out, the oldest "since you were away" row
    # leaving the window (the window's live refresh rebuilds then; 42 verification, missing 4)
    moments = [epoch_of((card_offer(n) or {}).get("expires_at")) for n in cards if offer_open(n, now)]
    moments += [float(a["at"]) + AWAY_HOURS * 3600 for a in away if a.get("at")]
    listed = home_list(memory, now)
    moments.append(todo_moment(listed, now))
    next_at = min((m for m in moments if m and m > now), default=None)
    return {"now": now_strip, "needs": needs, "running": running, "offers": offers, "away": away,
            "people": (people or [])[:6], "since": since, "next_at": next_at,
            # Your list: exactly Work's Your list (report 69), in its order; the page shows the first ``shown`` (the
            # ones you just added first, as Work does: Yk.todoOrder) and "N more on Work" for the rest
            "todos": {"open": listed, "n": len(listed), "shown": HOME_TODOS, "can": todo_can(memory)},
            "pending": pending_view(pending, TODO_PREFIXES, now)}


#: Where the day's AI spend went, in plain words (``yuki.core.cost``'s parts: memory's worker log names, the agent,
#: the voice). A part not listed is "other memory work". Display names only: nothing is decided from them.
COST_PARTS = {"journal": "daily notes", "portrait": "your profile", "nudges": "cards and check-ins",
              "calldigest": "call summaries", "transcribe": "turning calls into text",
              "conversations": "remembering our chats", "episodes": "the story of your day",
              "recheck": "double-checking notes", "titles": "short titles", "daydigest": "day summaries",
              "agent": "your requests", "voice": "talking with you"}
#: The cost ledger is incremental (each log read from where it stopped); one per logs folder, kept for the process.
_cost_ledgers: dict[str, Any] = {}


def ai_cost_row(logs: Path | None, now: float | None = None) -> dict[str, Any] | None:
    """Home's AI cost card (the 3 Oct feedback: plain words): "My AI use today: about $1.84" and where it went,
    "Daily notes $0.92, your profile $0.51, …", read from the logs Yuki already writes (``yuki.core.cost``'s ledger,
    incremental: only new lines are read). A "notice" row of Since you were away, keyed by the day (its total
    changes; its row stays). None with no logs or under a cent."""
    if logs is None:
        return None
    from yuki.core.cost import CostLedger

    try:
        ledger = _cost_ledgers.get(str(logs))
        if ledger is None:
            ledger = _cost_ledgers[str(logs)] = CostLedger(Path(logs))
        spend = ledger.refresh()
    except Exception:  # noqa: BLE001 - a cost line must never break Home
        return None
    parts: dict[str, float] = {}
    for kind, usd in (spend.memory or {}).items():
        name = COST_PARTS.get(kind, "other memory work")
        parts[name] = parts.get(name, 0.0) + float(usd or 0)
    parts[COST_PARTS["agent"]] = parts.get(COST_PARTS["agent"], 0.0) + spend.agent + spend.agent_startup
    parts[COST_PARTS["voice"]] = parts.get(COST_PARTS["voice"], 0.0) + spend.voice_mouth
    total = spend.model_total
    if total < 0.01:
        return None
    shown = [(n, v) for n, v in sorted(parts.items(), key=lambda kv: -kv[1]) if v >= 0.01]
    note = ", ".join(f"{n} ${v:.2f}" for n, v in shown[:4])
    note = note[:1].upper() + note[1:]
    rest = sum(v for _n, v in shown[4:])
    if rest >= 0.01:
        note += f", the rest ${rest:.2f}"
    return {"kind": "notice", "key": f"cost:{spend.day}", "title": f"My AI use today: about ${total:.2f}", "at": None,
            "when": "today", "state": "done", "changed": [], "note": note + "." if note else ""}


#: Goals the Now strip shows at most (today's, open first).
GOALS_SHOWN = 4


def goal_view(g: dict[str, Any]) -> dict[str, Any]:
    """One of memory's goals (``MemoryClient.goals``) as the page shows it: what it is (the user's own words when
    memory kept them), done or not."""
    text = " ".join(str(g.get("text") or g.get("words") or "").split())
    return {"ref": g.get("id"), "text": text, "status": g.get("status") or "open", "done": g.get("status") == "done"}


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
               label: str = "", now: float | None = None, kind: str = "", where: str = "") -> str:
    """Where something was said or seen, in the one format every row uses (the 3 Oct feedback item 8): "Heard in
    Discord, #general voice (Friends), 1 October, 2:07 AM, with Riya and Dev"; a call whose channel the screen never
    showed "Heard on a Discord call, …". With no call it follows the entry's source ``kind`` (memory's provenance):
    ``chat`` (said to me) "You said it to me, …"; read off the screen (``page``, ``message``, ``list``, ``command``)
    "Seen in #general in Slack, …" (``where``, else the app; a command names only the app); no source at all
    "Noted while Chrome was open, …". Never claims you said what was only seen (review F1)."""
    from yuki.ui.window.data import nat_when

    when = nat_when(at, today=datetime.fromtimestamp(time.time() if now is None else now).date())
    if call is not None or label:
        place = (call or {}).get("label") or label
        app = (call or {}).get("app") or app
        head = f"Heard in {place}" if place and channel_known else f"Heard on a {app or 'voice'} call"
        people = (call or {}).get("people") or []
        tail = f", with {_and(people)}" if people else ""
        return f"{head}{', ' + when if when else ''}{tail}"
    tail = f", {when}" if when else ""
    if kind == "call":
        return f"Heard on a {app or 'voice'} call{tail}"
    if kind == "chat":
        return f"You said it to me{tail}"
    if kind in ("page", "message", "list", "command"):
        place = app if kind == "command" else (where or app)
        return f"Seen in {place}{tail}" if place else f"Seen on your screen{tail}"
    return f"Noted while {app} was open{tail}" if app else f"Noted{tail}"


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
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now).date()
    days = evidence_days(f)
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
        out["when"] = routine_when(f)
    if f.get("kind") == "behaviour":
        out["days_seen"] = len(evidence_days(f))
    return out


def evidence_days(f: dict[str, Any]) -> set[Any]:
    """The days the evidence a fact rests on was seen (its cited entries' times)."""
    from yuki.ui.window.data import epoch

    return {datetime.fromtimestamp(t).date() for e in f.get("evidence") or [] if isinstance(e, dict)
            and (t := epoch(e.get("at"))) is not None}


#: A routine's "when" is said only from this many cited times, and a time of day only when they sit this close.
WHEN_MIN_TIMES, WHEN_SPAN_H = 3, 3.0


def routine_when(f: dict[str, Any]) -> str:
    """When a routine happens, measured from the times of the entries it rests on (memory keeps no "when" field):
    "weekdays, around 9 PM", "around 7 AM", "weekends", or "" when the times don't agree. Arithmetic on its own
    evidence: the hours' circular spread (a routine across midnight stays one), the weekdays of distinct days."""
    import math

    from yuki.ui.window.data import epoch

    times = sorted(t for e in f.get("evidence") or [] if isinstance(e, dict) and (t := epoch(e.get("at"))) is not None)
    if len(times) < WHEN_MIN_TIMES:
        return ""
    stamps = [datetime.fromtimestamp(t) for t in times]
    hours = [s.hour + s.minute / 60 for s in stamps]
    ang = [h / 24 * 2 * math.pi for h in hours]
    mean = (math.degrees(math.atan2(sum(map(math.sin, ang)), sum(map(math.cos, ang)))) / 15) % 24
    spread = max(min(abs(h - mean), 24 - abs(h - mean)) for h in hours) * 2
    bits = []
    days = {s.date() for s in stamps}
    if len(days) >= WHEN_MIN_TIMES:
        if all(d.weekday() < 5 for d in days):
            bits.append("weekdays")
        elif all(d.weekday() >= 5 for d in days):
            bits.append("weekends")
    if spread <= WHEN_SPAN_H:
        half = int(round(mean * 2)) % 48          # to the nearest half hour
        hour, mins = divmod(half * 30, 60)
        bits.append(f"around {hour % 12 or 12}{':30' if mins else ''} {'AM' if hour < 12 else 'PM'}")
    return ", ".join(bits)


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
    """"Maybe misheard" (memory's ``misheard()``: real mishearings of the last week, newest first; the feedback round
    3, W6): what was heard and Yuki's guess (``heard``, ``guess``, ``why`` who or what makes it fit, ``guess_is_name``),
    the entry said in English (``text``: memory's ``shown`` when written, else its text with only formats changed) and
    where it was heard, in the one format (:func:`heard_line`). The raw transcript (``source.quote``) is provenance
    and never reaches the page. An item with ``heard`` and ``guess`` takes ``guess_right`` (Yes = Yuki's guess is
    right; only a memory that writes guesses has them); ``correct``: True when memory takes ``correct_item`` ("It
    was…" in your words). An older memory: the Qt view's uncertain entries (``fallback``)."""
    from yuki.ui.window.data import natural, plain

    fn = getattr(memory, "misheard", None)
    got = _safe(fn, None) if callable(fn) else None
    if not isinstance(got, dict):
        items = [{"ref": i.ref, "text": i.text, "prov": heard_line(i.title, None, i.since, now=now)} for i in fallback]
        return {"items": items, "total": len(items), "direct": False, "correct": False}
    items = []
    for m in got.get("items") or []:
        if not isinstance(m, dict) or not m.get("ref"):
            continue
        src = m.get("source") if isinstance(m.get("source"), dict) else {}
        cid = _call_id(m.get("call"))
        call = calls.get(cid) if cid is not None else None
        if cid is not None and call is None:   # memory's own label for it (its digest's place) and who was on it
            call = {"label": m.get("heard_in") or "", "app": m.get("app") or "",
                    "people": call_people(m.get("people") or [], lookup or {})}
        heard, guess = " ".join(str(m.get("heard") or "").split()), " ".join(str(m.get("guess") or "").split())
        text = " ".join(str(m.get("shown") or "").split()) or plain(natural(str(m.get("text") or "")))
        items.append({"ref": str(m["ref"]), "text": text,
                      **({"heard": heard, "guess": guess, "guess_is_name": bool(m.get("guess_is_name")),
                          "why": " ".join(str(m.get("why") or "").split()) or None} if heard and guess else {}),
                      "prov": heard_line(str(m.get("app") or ""), call, m.get("at"),
                                         channel_known=bool(m.get("channel_known", True)), now=now,
                                         kind=str(src.get("kind") or ""), where=str(src.get("where") or ""))})
    return {"items": items, "total": int(got.get("total") or len(items)), "direct": callable(getattr(memory, "resolve_heard", None)),
            "correct": callable(getattr(memory, "correct_item", None))}


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


#: How long a correction that landed says "Fixed" on its row.
FIXED_SHOWN_S = 10 * 60.0


def pending_view(pending: dict[str, Any] | None, prefixes: tuple[str, ...] | None = None,
                 now: float | None = None) -> dict[str, dict[str, Any]]:
    """The corrections a page shows on their rows (the feedback round 3, W3): ``{ref: {"kind", "words", "state",
    "cid", "at"}}`` for the refs starting with one of ``prefixes`` (all with None). ``state``: ``fixing`` (sent, the
    correction pass has not landed: "Got it, fixing it"), ``fixed`` (landed within :data:`FIXED_SHOWN_S`; the row
    already shows the corrected text), ``moved`` (the words only moved a due date) or ``failed``; ``cid`` is the
    correction's id, for its Undo while it waits; ``was``, the due date the words moved it from (its Undo)."""
    now = time.time() if now is None else now
    out: dict[str, dict[str, Any]] = {}
    for ref, p in (pending or {}).items():
        if not isinstance(p, dict) or (prefixes and not str(ref).startswith(prefixes)):
            continue
        state = str(p.get("state") or "fixing")
        if state != "fixing" and now - float(p.get("done_at") or 0) > FIXED_SHOWN_S:
            continue
        out[str(ref)] = {"kind": p.get("kind") or "correct", "words": p.get("words") or "", "state": state,
                         "cid": p.get("fid"), "at": p.get("at"), "late": bool(p.get("late")),
                         # a due date the words moved (``moved``: the date was all of it): Undo puts ``was`` back
                         **({"was": p["was"]} if "was" in p and state in ("fixing", "moved") else {})}
    return out


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
            "misheard": misheard_view(memory, calls, fallback, now, lookup), "pending": pending_view(pending, None, now),
            "can": {"pin": callable(getattr(memory, "pin_fact", None)), "review": callable(rq),
                    "heard": callable(getattr(memory, "resolve_heard", None)),
                    "correct": callable(getattr(memory, "correct_item", None))}}


# -- Work and the Day ----------------------------------------------------------------------------------------------
# Work (the user, 4 Oct: "a to-do list and other things that can help me track work and help me with work"; threads
# left the window, memory keeps them for the agent and the Continue run): the week's hours in the head, then Today
# (focus against your usual, the longest stretch, what you worked on, what's coming up), then two columns: Your list
# and Waiting on; Needs your reply and You said you'd. The judged parts (who waits on a reply, who owes what, the
# meetings) are memory's (yuki.memory.workboard); everything else is a query and a deterministic format.

#: Today's "Worked on today": the threads with the most time today, at most.
WORKED_SHOWN = 3
#: "Your usual by now": the same weekday this many weeks back (at least USUAL_MIN_DAYS with time at the PC), else
#: every day of the last USUAL_FALLBACK_DAYS.
USUAL_WEEKS = 4
USUAL_MIN_DAYS = 2
USUAL_FALLBACK_DAYS = 7


def _quote_of(evidence: Any) -> str:
    """A commitment's own words from memory's to-do evidence (``the user said in a call on 2026-10-02: "…"``, the
    format :func:`yuki.memory.nudges.todo_items` writes): the quoted part, or ``""``."""
    t = " ".join(str(evidence or "").split())
    i = t.find(': "')
    return t[i + 3:-1] if i >= 0 and t.endswith('"') and len(t) > i + 4 else ""


def active_minutes_by_day(memory: Any, days: list[date], seconds_into_day: float) -> dict[date, float]:
    """Minutes at the PC, not full screen, on each of ``days`` from its midnight up to ``seconds_into_day`` (the
    timeline, as the week figure reads it: active and passive time of present stretches, the full-screen part taken
    off). One read for all the days."""
    out = {d: 0.0 for d in days}
    for d in days:   # each day's own stretch only (midnight to this time of day): a few small indexed reads
        d0 = datetime.combine(d, datetime.min.time()).timestamp()
        for r in _safe(lambda: memory.day_timeline(d0, d0 + seconds_into_day), []) or []:
            if r.get("state") != "present" or r.get("fullscreen"):
                continue
            s0, e0 = float(r.get("start") or 0), float(r.get("end") or 0)
            ov = min(e0, d0 + seconds_into_day) - max(s0, d0)
            if ov > 0:
                present = float(r.get("active_s") or 0) + float(r.get("passive_s") or 0)
                out[d] += present * ov / max(1e-6, e0 - s0) / 60.0
    return out


#: "Your usual by now" moves slowly: read again at most this often (seconds), per memory file and day.
USUAL_CACHE_S = 600.0
_usual_cache: dict[Any, tuple[float, float | None]] = {}


def usual_by_now(memory: Any, now: float) -> float | None:
    """Your usual minutes at the PC (not full screen) by this time of day: the median over the same weekday of the
    last :data:`USUAL_WEEKS` weeks (days with any such time), or, with fewer than :data:`USUAL_MIN_DAYS` of them, over
    the last :data:`USUAL_FALLBACK_DAYS` days. ``None`` with too little history."""
    import statistics

    today = datetime.fromtimestamp(now).date()
    key = (str(getattr(getattr(memory, "store", None), "path", id(memory))), today)
    hit = _usual_cache.get(key)
    if hit is not None and 0 <= now - hit[0] < USUAL_CACHE_S:
        return hit[1]
    value = _usual(memory, now, today)
    _usual_cache.clear()
    _usual_cache[key] = (now, value)
    return value


def _usual(memory: Any, now: float, today: date) -> float | None:
    import statistics

    into = now - datetime.combine(today, datetime.min.time()).timestamp()
    same = [today - timedelta(days=7 * k) for k in range(1, USUAL_WEEKS + 1)]
    recent = [today - timedelta(days=k) for k in range(1, USUAL_FALLBACK_DAYS + 1)]
    mins = active_minutes_by_day(memory, sorted(set(same) | set(recent)), into)
    used = [mins[d] for d in same if mins.get(d, 0) >= 1]
    if len(used) < USUAL_MIN_DAYS:
        used = [mins[d] for d in recent if mins.get(d, 0) >= 1]
        if len(used) < 3:
            return None
    return float(statistics.median(used))


def work_today(memory: Any, v: Any, sessions: list[dict[str, Any]], board: dict[str, Any], now: float) -> dict[str, Any]:
    """Today on the Work page, the same figures as the Day tab (``v``: the day view both read): ``focus`` minutes
    (the Day's Active: at the PC, not full screen) and your ``usual`` by now; the ``longest`` stretch in one app
    (the Day's Longest focus) with the session it was in; ``worked``: the threads of work with the most time today
    (``[title, minutes, thread ref]``, from today's sessions grouped by the thread their journal entries belong to);
    ``coming``: memory's meetings ahead (``[when words, title]``) and where they were seen."""
    from yuki.ui.window.data import nat_time

    t = v.totals if v is not None else {}
    focus = round(max(0.0, float(t.get("present_s", 0) or 0) - float(t.get("fullscreen_s", 0) or 0)) / 60.0)
    usual = usual_by_now(memory, now)
    longest = None
    if v is not None:
        secs, app, start = longest_focus(v)
        if secs >= 60:
            inside = next((s for s in sessions if s.get("start_at", 0) - 60 <= start <= s.get("end_at", 0)), None)
            title = inside.get("title") if inside and inside.get("title") and inside.get("title") != app else None
            longest = {"min": round(secs / 60.0), "app": app, "title": title, "at": start, "from": nat_time(start)}
    worked: list[list[Any]] = []
    spans = [(s["start_at"], s["end_at"]) for s in sessions if s.get("start_at") and s.get("end_at")]
    threads = _safe(lambda: memory.work_threads_for(spans), None) if spans else None
    if threads:
        mins: dict[str, list[Any]] = {}
        for s, th in zip([s for s in sessions if s.get("start_at") and s.get("end_at")], threads):
            if not th:
                continue
            m = sum(int(x[1] or 0) for x in s.get("apps") or [])
            row = mins.setdefault(th["ref"], [th["title"], 0, th["ref"]])
            row[1] += m
        worked = sorted((r for r in mins.values() if r[1] >= 1), key=lambda r: -r[1])[:WORKED_SHOWN]
    coming = [[ahead_words(m["at"], bool(m.get("all_day")), now), m.get("title") or ""] for m in board.get("meetings") or []][:3]
    seen = list(dict.fromkeys(a for m in (board.get("meetings") or [])[:3] for a in m.get("seen") or []))
    return {"focus": focus, "usual": round(usual) if usual is not None else None, "longest": longest,
            "worked": worked, "coming": coming,
            "coming_seen": f"seen on your screen in {_and(seen)}" if seen else ""}


def dates_said(text: str, now: float) -> str:
    """``text`` with any machine date in it ("2026-10-09", "2026-10-09 17:00") said the page's one way ("Friday",
    "tomorrow 5 PM", "25 September"): a model's words can carry one though asked not to (second review M2). A format
    read token by token (a date parses or it doesn't), never a guess about what the words mean."""
    words = str(text or "").split(" ")
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        core = w.rstrip(".,;:)")
        tail = w[len(core):]
        try:
            day = datetime.strptime(core, "%Y-%m-%d") if len(core) == 10 else None
        except ValueError:
            day = None
        if day is None:
            out.append(w)
            i += 1
            continue
        at, all_day = day.timestamp(), True
        if not tail and i + 1 < len(words):   # "2026-10-09 17:00": the time after it
            nxt = words[i + 1].rstrip(".,;:)")
            try:
                hm = datetime.strptime(nxt, "%H:%M") if len(nxt) == 5 else None
            except ValueError:
                hm = None
            if hm is not None:
                at, all_day = at + hm.hour * 3600 + hm.minute * 60, False
                tail = words[i + 1][len(nxt):]
                i += 1
        today = datetime.fromtimestamp(now).date()
        said = ahead_words(at, all_day, now) if day.date() >= today else week_words(at, now)
        out.append(said + tail)
        i += 1
    return " ".join(out)


def _loop_title(loop: dict[str, Any] | None, now: float) -> str:
    """An open loop's own words for a row with no memory title: memory's judged ``what`` (said to the user, a verb
    first) for something asked of or promised by them, its dates said the page's way; ``""`` for anything else."""
    if not loop or loop.get("kind") not in ("asked", "promised"):
        return ""
    return dates_said(" ".join(str(loop.get("what") or "").split()), now)


def _from_words(t: dict[str, Any], loop: dict[str, Any] | None, now: float) -> str:
    """Where a row of Your list came from, in the Work page's words (Home keeps ``from``): ``added Wednesday``, ``you
    told me, yesterday``, ``you said it on a call, Tuesday``, ``Mira asked in Slack, Wednesday``."""
    src, day = t.get("source"), week_words(t.get("since"), now, in_sentence=True)
    tail = (f" {day}" if day.startswith("at ") else f", {day}") if day else ""   # "you told me at 3 PM", "..., yesterday"
    if src == "user":
        return f"added {day}" if day else ""
    if src == "chat":
        return "you told me" + tail
    if src == "call":
        return "you said it on a call" + tail
    if src == "commitment":
        return "I said I’d do it"
    if src == "loop" and loop and loop.get("who") and loop.get("kind") in ("asked", "promised"):
        where = f" in {loop['where']}" if loop.get("where") else ""
        if loop["kind"] == "asked":
            return f"{loop['who']} asked{where}{tail}"
        return f"you promised {loop['who']}{where}{tail}"
    return "I noticed it" + tail


def work_columns(rows: list[dict[str, Any]], board: dict[str, Any], kept: list[dict[str, Any]],
                 now: float) -> dict[str, Any]:
    """memory's merged to-do list split over the page so each thing shows once: ``list`` (Your list: your to-dos,
    Yuki's own promises, loops asked of you or not judged yet, and whatever you moved there), ``done`` (Recently
    done), ``said`` (You said you'd: what you said on a call or told me, and loops where you promised someone; one
    memory saw kept stays a while, struck through), ``waiting`` (loops where someone owes you)."""
    loops = {int(k): v for k, v in (board.get("loops") or {}).items()}
    listed = set(board.get("listed") or [])
    raw = {str(r.get("id")): r for r in rows if isinstance(r, dict)}
    open_todos, done_todos = todo_lists(None, now, include_done=True, rows=rows)
    lst, said, waiting = [], [], []

    def column(t: dict[str, Any], loop: dict[str, Any] | None) -> str:
        """Which block a row of memory's list belongs in: ``waiting``, ``said`` or ``list`` (one rule for the open rows
        and for where Put it back returns a done one)."""
        kind = t["ref"].partition(":")[0]
        if t["ref"] in listed:
            return "list"
        if kind == "loop" and loop and loop.get("kind") == "waiting":
            return "waiting"
        if ((kind == "commit" and t.get("source") in ("chat", "call"))
                or (kind == "loop" and loop and loop.get("kind") == "promised")):
            return "said"
        return "list"

    for t in open_todos:
        ref = t["ref"]
        kind, _, number = ref.partition(":")
        loop = loops.get(int(number)) if kind == "loop" and number.isdigit() else None
        moved = ref in listed
        col = column(t, loop)
        if col == "waiting":
            waiting.append({"ref": ref, "who": loop.get("who") or "",
                            "what": dates_said(loop.get("what") or "", now) or t["text"],
                            "where": loop.get("where") or "", "since": t.get("since"),
                            "day": week_words(t.get("since"), now), "draft": loop.get("draft") or "",
                            # what the wait rests on (second review V5): they offered, or you asked
                            "basis": loop.get("basis") if loop.get("basis") in ("offered", "asked") else "",
                            "text": t["text"]})
            continue
        if col == "said":
            quote = _quote_of(raw.get(ref, {}).get("evidence")) if kind == "commit" else ""
            if kind == "commit":
                where = ("on a call" if t.get("source") == "call" else "said to me")
            else:
                where = loop.get("where") or ""
            said.append({"ref": ref, "title": dates_said((loop or {}).get("what") or "", now) or t.get("title") or t["text"],
                         "text": t["text"],
                         "where": where, "day": week_words(t.get("since"), now), "quote": quote,
                         "who": (loop or {}).get("who") or "", "due": t.get("due") or "", "over": t.get("over"),
                         "due_at": t.get("due_at"), "all_day": t.get("all_day"), "since": t.get("since"),
                         "tomorrow": t.get("tomorrow")})
            continue
        # a row memory hasn't titled yet: a loop's judged words, never its raw third-person text (second review V4)
        lst.append({**t, "title": t.get("title") or _loop_title(loop, now) or None, "meta": _from_words(t, loop, now),
                    "moved": moved})
    for k in kept:
        said.append({"ref": k["ref"], "title": k.get("title") or k["text"], "text": k["text"],
                     "closed": f"closed itself: I saw it done {week_words(k['closed_at'], now, in_sentence=True)}",
                     "closed_at": k["closed_at"], "since": k.get("since")})
    kept_refs = {k["ref"] for k in kept}

    def loop_of(ref: str) -> dict[str, Any] | None:
        kind, _, number = ref.partition(":")
        return loops.get(int(number)) if kind == "loop" and number.isdigit() else None

    # a promise memory saw kept shows once: struck through in You said you'd (Not done yet), not again in Recently
    # done (review F3). Every row there can be put back (memory's reopen_item at any age; review F7); ``goes`` is the
    # block it goes back to, so only a row going back to Your list is drawn there at once (leftUI review 1); "" for a
    # loop the board holds no judgement of (it only judges open loops), which memory's next build places
    def goes(t: dict[str, Any]) -> str:
        loop = loop_of(t["ref"])
        return "" if t["ref"].startswith("loop:") and loop is None and t["ref"] not in listed else column(t, loop)

    done = [{**t, "title": t.get("title") or _loop_title(loop_of(t["ref"]), now) or None,
             "meta": _from_words(t, loop_of(t["ref"]), now), "back": t["ref"].startswith(("todo:", "commit:", "loop:")),
             "goes": goes(t)}
            for t in done_todos if t["ref"] not in kept_refs]
    return {"list": lst, "done": done, "said": said, "waiting": waiting}


def build_work(memory: Any, *, week: dict[str, Any] | None = None, pending: dict[str, Any] | None = None,
               now: float | None = None) -> dict[str, Any]:
    """Work: ``todos`` (Your list: ``open`` in the list's order, each with its ``meta`` line; ``done``, Recently
    done, with ``back`` where memory can put it back; ``can``: its buttons; ``n``), ``waiting`` (Waiting on),
    ``replies`` (Needs your reply: ``ready`` false until memory has read the chats once), ``said`` (You said you'd),
    ``today`` (:func:`work_today`), the week figure, ``pending`` (corrections sent and not landed) and ``can``."""
    from yuki.ui.window.data import build_day as qt_build_day

    now = time.time() if now is None else now
    v = _safe(lambda: qt_build_day(memory, datetime.fromtimestamp(now).date(), now=now, extras=False), None)
    sessions = day_sessions(memory, v) if v is not None else []
    rows = _safe(lambda: memory.todos(include_done=True), []) or []
    board = _safe(lambda: memory.work_board(now), {}) or {}
    kept = _safe(lambda: memory.work_kept(now), []) or []
    cols = work_columns(rows, board, kept, now)
    replies = [{"key": r["key"], "who": r.get("who") or "", "msg": r.get("msg") or "", "app": r.get("app") or "",
                "place": r.get("place") or "", "at": r.get("at"), "opened": bool(r.get("opened"))}
               for r in board.get("replies") or []]
    can = todo_can(memory)
    can.update({"mark": callable(getattr(memory, "work_mark", None)), "reopen": callable(getattr(memory, "reopen_item", None))})
    return {"todos": {"open": cols["list"], "done": cols["done"], "n": len(cols["list"]), "can": can},
            "waiting": cols["waiting"], "said": cols["said"],
            "replies": {"items": replies, "ready": bool(board.get("ready"))},
            # whether memory has judged the chats and open loops at all (second review V3): until then Waiting on
            # says it is still reading instead of "nobody owes you anything"
            "ready": bool(board.get("ready")),
            "today": work_today(memory, v, sessions, board, now),
            "week": week if week is not None else week_figure(memory),
            "pending": pending_view(pending, ("episode:", *TODO_PREFIXES), now),
            "next_at": todo_moment(cols["list"], now),
            "can": {"correct": callable(getattr(memory, "correct_item", None))}}


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
              week: list[dict[str, Any]] | None = None, people: list[dict[str, Any]] | None = None,
              pending: dict[str, Any] | None = None) -> dict[str, Any]:
    """The Day tab: the day in slices (the skyline), today's totals, sessions (a short title each; opened, the
    summary and the time in every app; ``ref`` for Correct), calls (where, who, the gists said to you), the week by
    hour, and the corrections sent on a session and not landed yet (``pending``)."""
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
    from yuki.ui.window.data import nat_time

    sessions = day_sessions(memory, v)
    calls_rows = _safe(lambda: memory.calls_in(since=since, until=since + 86400, limit=30), []) or []
    lookup = name_lookup(people)
    calls = []
    for c in calls_rows:
        cs, ce = epoch_of(c.get("start")), epoch_of(c.get("end")) or now
        if cs is None:
            continue
        calls.append({"a": int((max(cs, since) - since) // 60), "b": int((min(ce, since + 86400) - since) // 60),
                      "start": hm(cs), "end": hm(ce), "app": c.get("app") or "", "kind": c.get("kind") or "call",
                      "live": bool(c.get("ongoing")),
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
            "week_hours": week if week is not None else week_days(memory, now),
            "pending": pending_view(pending, ("episode:",), now),
            "can": {"correct": callable(getattr(memory, "correct_item", None))}}


def day_sessions(memory: Any, v: Any) -> list[dict[str, Any]]:
    """The day's sessions (``v``: the Qt builder's day view) as the Day tab and Work show them, oldest first: the
    minutes into the day (``a``, ``b``), the clock times, the moments (``start_at``, ``end_at``), the leading app,
    full screen or not, the time in every app, and memory's short title, summary and episode ref (Correct)."""
    from yuki.ui.window.data import natural

    since = v.since
    lanes = {ln.key: ln.label for ln in v.lanes}
    labels = session_labels(memory, v.sessions, since)
    sessions = []
    for s in v.sessions:
        fs = sum(seg.present_s for seg in v.segments if seg.fullscreen and seg.state == "present"
                 and seg.start < s.end and seg.end > s.start)
        app = lanes.get(s.lane, s.lane)
        lab = labels.get(id(s))
        # the feedback round 3, W2: the row is the session's short distinct title (memory's, never its summary cut
        # short), opened its one- or two-sentence summary and the time in every app. Until memory has written them:
        # the app as the title and the narrative's first sentence, whole, as the detail
        if lab is not None and lab["title"]:
            extra = {"title": lab["title"], "ref": lab["ref"], **({"summary": lab["summary"]} if lab["summary"] else {})}
        else:   # still correctable: the ref of its longest episode (correct_item takes any current one)
            extra = {"title": app, "full": natural(getattr(s, "title_full", "") or s.title),
                     **({"ref": lab["ref"]} if lab is not None and lab.get("ref") else {})}
        sessions.append({"a": int((s.start - since) // 60), "b": int((s.end - since) // 60), "start": hm(s.start),
                         "end": hm(s.end), "start_at": round(s.start, 1), "end_at": round(s.end, 1), "app": app,
                         "mode": "full" if fs > s.present_s / 2 else "active",
                         "apps": [[name, round(secs / 60)] for name, secs in s.apps if secs >= 30], **extra})
    return sessions


def session_labels(memory: Any, sessions: list[Any], since: float) -> dict[int, dict[str, Any]]:
    """Each Day session's label from memory's episodes (migration 35, the feedback round 3, W2): ``{id(session):
    {"title", "summary", "ref"}}`` from the longest titled episode inside it (an episode belongs to the session
    holding most of it; a session joins neighbouring episodes led by the same app, as its title already did). A
    session with no titled episode yet gets ``{"title": None, "summary": None, "ref"}`` from its longest episode, so
    it can still be corrected (the page keeps the app as its title); one with no episode is left out. Reads only
    (``episodes``), no model."""
    eps = []
    for e in _safe(lambda: memory.episodes(since, since + 86400.0, limit=200), []) or []:
        if not isinstance(e, dict):
            continue
        title = " ".join(str(e.get("title") or "").split()) or None
        s, u = epoch_of(e.get("start")), epoch_of(e.get("end"))
        if s is not None and u is not None and u > s:
            eps.append((s, u, title, " ".join(str(e.get("summary") or "").split()) or None, e.get("ref")))
    out: dict[int, dict[str, Any]] = {}
    for sess in sessions:
        inside = [e for e in eps if min(e[1], sess.end) - max(e[0], sess.start) > (e[1] - e[0]) / 2]
        titled = [e for e in inside if e[2]]
        if titled:
            s, u, title, summary, ref = max(titled, key=lambda e: e[1] - e[0])
            out[id(sess)] = {"title": title, "summary": summary, "ref": ref}
        elif inside:
            ref = max(inside, key=lambda e: e[1] - e[0])[4]
            if ref:
                out[id(sess)] = {"title": None, "summary": None, "ref": ref}
    return out


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
#: How each practice object came to be (``playbooks.origin``), in the words the Playbook page's side line uses. Every
#: way on the page rests on a success (appLearn: a post-mortem's way is the one that worked in the end); a style note
#: is the user's own words.
ORIGIN_WORDS = {"distill": "From a run that worked", "run": "From a run that worked",
                "postmortem": "From a run that worked in the end", "slider": "From a run that worked",
                "user": "In your words", "taught": "You taught me",
                "miner": "Seen in what you do", "knowhow": "From what I learned before"}
#: The kinds the Playbook page and the Activity rows may show (appLearn): ways and the user's style notes. A pitfall
#: is a quiet note for Yuki's own use, never shown as something learned.
SHOWN_KINDS = ("procedure", "habit", "style", "question")


def app_words(apps: list[Any]) -> str:
    """The apps a playbook is for, as the page names them (``chrome`` -> ``Chrome``; none: ``Any app``)."""
    from yuki.core.appnames import prettify

    names = [prettify(str(a)) or str(a) for a in apps or [] if str(a).strip()]
    return " · ".join(dict.fromkeys(names)) if names else "Any app"


def _learned_line(c: dict[str, Any]) -> str:
    """Where a playbook came from and how much evidence it has, e.g. ``From a run that worked, 29 Sep · seen on 3
    days``."""
    head = "In your words" if c.get("kind") == "style" else ORIGIN_WORDS.get(str(c.get("origin") or ""), "Learned")
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


def choice_view(c: dict[str, Any]) -> dict[str, Any]:
    """A habit from ``workflows()`` as an app choice on the Playbook page (the 4 Oct item W7b): what the choice is about
    (the first thing it names - a site, an app, a person - else its title), the default it settles on, or that Yuki
    asks; never the counts behind it (the miner's text, "39.3 minutes by 39 visits over 3 days", stays memory's). The
    page says it as "YouTube: usually Chrome"."""
    about = [str(e).strip() for e in c.get("people") or [] if str(e).strip()]
    title = " ".join(str(c.get("title") or "").split())
    default = " ".join(str(c.get("default") or "").split())
    uses = int(c.get("uses") or 0)
    out = {"ref": c.get("id"), "kind": "habit", "title": title, "subject": about[0] if about else title,
           "default": default, "ask": bool(c.get("ask")) and not default, "app": app_words(c.get("apps") or []),
           "uses": uses, "last": short_day(epoch_of(c.get("last_used"))), "conf": c.get("confidence"),
           "conf_word": conf_word(c.get("confidence")) or str(c.get("label") or ""), "disabled": bool(c.get("disabled")),
           "learned": _learned_line(c)}
    out["line"] = choice_words(out)
    return out


def choice_words(ch: dict[str, Any]) -> str:
    """An app choice in one plain sentence: "YouTube: usually Chrome.", "Riya: I ask which one.", or just what it is
    about when there is no default yet."""
    subject = str(ch.get("subject") or ch.get("title") or "")
    subject = subject[:1].upper() + subject[1:]
    if ch.get("default"):
        return f"{subject}: usually {ch['default']}."
    if ch.get("ask"):
        return f"{subject}: I ask which one."
    return f"{subject}." if subject else ""


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
    if c.get("kind") == "habit":   # an app choice: said plainly, never the counts behind it (the 4 Oct item W7b)
        basis = choice_words(choice_view(c))
    when = short_day(epoch_of(c.get("since")))
    tail = f"Seen {seen} time{'s' if seen != 1 else ''}" if seen else "Seen once"
    return {"ref": c.get("id"), "kind": c.get("kind"), "title": c.get("title") or "",
            "basis": (basis + (" " if basis else "") + f"{tail}{', ' + when if when else ''}; I’ll use it once you say so.").strip(),
            "steps": steps, "app": app_words(c.get("apps") or [])}


def build_playbook(memory: Any) -> dict[str, Any]:
    """The Playbook page from practice memory (``workflows()``, Phase C): NEW proposals (seen once, or a question),
    procedures and habits ("How you do things"), lessons (the user's style notes), with uses, last use and
    confidence; then the standing rules and requests. Without practice memory, today's know-how instead (probed).
    Only what Yuki learned from a success shows (appLearn): memory sends no pitfall, and none is drawn if one came."""
    from yuki.ui.window.data import learned_items

    workflows = getattr(memory, "workflows", None)
    wf = _safe(workflows, {}) if callable(workflows) else {}
    wf = wf if isinstance(wf, dict) else {}
    available = bool(wf.get("available"))

    def shown(key: str) -> list[dict[str, Any]]:
        return [c for c in wf.get(key) or [] if isinstance(c, dict) and c.get("kind") in SHOWN_KINDS] if available \
            else []

    # the 4 Oct item W7: procedures are a title and numbered steps; habits are app choices said plainly ("Slack:
    # usually Chrome"), never their counts
    procs = [procedure_view(c) for c in shown("procedures") if c.get("kind") == "procedure"]
    choices = [choice_view(c) for c in shown("procedures") if c.get("kind") == "habit"]
    lessons = [lesson_view(c) for c in shown("lessons") if c.get("kind") == "style"]
    # what Yuki is not sure of yet: questions first, at most PROPOSALS_SHOWN as the NEW blocks (the design shows one);
    # the other tentative ways and lessons stay visible in their own lists, marked as seen once
    pending = shown("proposals")
    pending.sort(key=lambda c: c.get("kind") != "question")
    props = [proposal_view(c) for c in pending[:PROPOSALS_SHOWN]]
    for c in pending[PROPOSALS_SHOWN:]:
        if c.get("kind") == "procedure":
            procs.append({**procedure_view(c), "conf_word": "tentative, seen once", "tentative": True})
        elif c.get("kind") == "habit":
            choices.append({**choice_view(c), "conf_word": "tentative, seen once", "tentative": True})
        elif c.get("kind") == "style":
            lessons.append({**lesson_view(c), "tentative": True})
    know = [] if available else [{"title": k.title, "line": k.line, "app": k.app}
                                 for k in (_safe(lambda: learned_items(memory, limit=24), []) or [])]
    rules = [rule_json(r) for r in (_safe(memory.rules, []) or [])]
    standing = []
    for r in _safe(memory.standing_requests, []) or []:
        text = " ".join(str(r.get("quote") or r.get("condition") or "").split())
        if text:
            standing.append({"ref": r.get("ref"), "text": text, "since": short_day(epoch_of(r.get("created_at")))})
    return {"proposals": props, "procedures": procs, "choices": choices, "know_how": know, "lessons": lessons, "rules": rules,
            "standing": standing, "workflows_available": available, "learning": wf.get("learning") or None}


# -- Activity ------------------------------------------------------------------------------------------------------


#: Practice memory's kinds: ways of doing (shown as "using …") and lessons (shown as "lesson …").
WAY_KINDS = ("procedure", "habit")
#: Memory's Undo window for something Yuki added (offers: yuki.memory.offers.UNDO_WITHIN_S; to-dos alike).
LEDGER_UNDO_S = UNDO_WITHIN_S
#: A request log row and a run trace are the same request when they started this close (the log keeps seconds).
SAME_REQUEST_S = 2.5


def request_words_from_memory(memory: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``rows`` (:func:`yuki.ui.ledger.read_request_log`) with the user's words filled in where the log has none:
    ``requests.csv`` no longer keeps them (report 76), memory's encrypted exchanges do. A row and an exchange are the
    same request when they started within :data:`SAME_REQUEST_S` (both are stamped when the request began). Only the
    days of rows without words are read (``turns_on``); a row with no match keeps "" ("A request"). Never raises
    (review F7)."""
    need = [r for r in rows if not str(r.get("what") or "").strip() and r.get("at")]
    fetch = getattr(memory, "turns_on", None)
    if not need or not callable(fetch):
        return rows
    from yuki.memory.checkins import user_day

    turns: list[dict[str, Any]] = []
    for day in sorted({user_day(float(r["at"])) for r in need}):
        turns += [t for t in (_safe(lambda day=day: fetch(day), []) or [])
                  if isinstance(t, dict) and t.get("at") and str(t.get("user") or "").strip()]
    for r in need:
        best = min(turns, key=lambda t: abs(float(t["at"]) - float(r["at"])), default=None)
        if best is not None and abs(float(best["at"]) - float(r["at"])) <= SAME_REQUEST_S:
            r["what"] = str(best["user"])
    return rows


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
        word, status = run_outcome(run.get("outcome"))
        result = "answered" if status == "done" else word[:1].lower() + word[1:]
        return [{"id": f"run:{run.get('key')}", "kind": "request",
                 "f": "failed" if status == "failed" else "waiting" if status == "waiting" else "did",
                 "at": float(run.get("at") or now), "what": f"Answered “{words}”" if words else "Answered you",
                 "where": "Yuki", "cost": cost, "because": "because", "why": why, "chips": [], "result": result,
                 "status": status, "wrong_text": wrong}]
    rows = []
    stopped = str(run.get("outcome") or "") in ("cancelled", "withdrawn", "abandoned")
    for i, s in enumerate(steps):
        result, status = step_result(s, stopped=stopped)
        # a way it followed, or the user's style note; a pitfall it kept clear of is Yuki's own quiet note (appLearn)
        chips = [{"kind": "memory" if c.get("kind") in WAY_KINDS else "lesson", "text": c.get("title") or ""}
                 for c in s.get("cited") or [] if c.get("title") and c.get("kind") in SHOWN_KINDS]
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
    """The calls (and games' voice chats) Yuki recorded, as ledger rows: what (recording or recorded), where, why
    (the user's standing rule for that app, or their yes on the card), and what came of it."""
    from yuki.ui.call_words import is_game

    out = []
    for c in calls:
        if not c.get("recorded"):
            continue
        app = c.get("app") or ""
        game = is_game(c)
        name = (f"{app} voice chat" if app else "game voice chat") if game else (f"{app} calls" if app else "calls")
        consent = c.get("consent")
        why = (f"rule: always for {name}" if consent == "always" else
               f"you said yes on the {'game' if game else 'call'}’s card" if consent == "yes" else
               "you said yes to recording it")
        live = bool(c.get("live"))
        mins = int(c.get("min") or 0)
        result = ("live" if live else "turned into text" if c.get("gists") else
                  c.get("status") or "waiting to be turned into text")
        if game:
            what = (f"Recording {app or 'game'} voice chat" if live else
                    f"Recorded {dur_words(mins * 60)} of {app or 'game'} voice chat")
        else:
            what = (f"Recording a {app or 'voice'} call" if live else
                    f"Recorded a {dur_words(mins * 60)} {app or 'voice'} call")
        out.append({"id": f"call:{c.get('start_at')}", "kind": "call", "f": "calls", "at": c.get("start_at") or now,
                    "what": what, "where": c.get("where") or app, "cost": "", "because": "because",
                    "why": why, "chips": [], "result": result, "status": "live" if live else "verified"})
    return out


def build_activity(memory: Any, *, requests_csv: Path | None = None, now: float | None = None,
                   days: int = 7, people: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The ledger (E14), newest first: every acting step of Yuki's requests from the run traces (practice memory) with
    why - the user's words, a procedure or lesson it used - its result and an Undo window where memory can take it
    back (a to-do it added; an offer that ran); requests the traces don't have, from the request log; the cards Yuki
    showed (why: the card's reason, or the user's standing request); the calls it kept (why: the user's call rule).
    Filters by ``f``: did / failed / calls. Plus what it saw today (time by app, counts only) and the week's calls."""
    from yuki.ui.chat_model import clean_request
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
    logged = [r for r in (read_request_log(requests_csv, since, limit=120) if requests_csv else [])
              if not any(abs(r["at"] - a) <= SAME_REQUEST_S for a in run_starts)]   # else the run trace has it
    for r in request_words_from_memory(memory, logged):
        words, cost = request_row_words(r)
        status = {"done": "done", "failed": "failed", "stopped": "stopped"}.get(r["state"], "done")
        voice = r.get("where") == "voice"
        said = _clip(" ".join(clean_request(r.get("what") or "").split()), 120)
        # the id is the request's own time (and session), never its place in the log: it is the fold's key, and a
        # newer request must not move an opened group onto another one (review F3)
        rows.append({"id": f"req:{r.get('session') or ''}:{float(r['at']):.3f}", "kind": "request",
                     "f": "failed" if status == "failed" else "did", "at": r["at"],
                     "wrong_text": f"That wasn’t right: “{said}”. " if said else "",
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
        status = "" if gists else ("Live now. Being recorded; it’s turned into text when the PC is free."
                                   if c.get("ongoing") and c.get("recorded") else
                                   "Waiting to be turned into text." if c.get("recorded") else "Not recorded.")
        calls.append({"start_at": cs, "end_at": None if c.get("ongoing") else epoch_of(c.get("end")),
                      "live": bool(c.get("ongoing")), "min": round(float(c.get("seconds") or 0) / 60),
                      "spoke_min": round(float(c.get("speech_s") or 0) / 60) if c.get("recorded") else None,
                      "app": c.get("app") or "", "kind": c.get("kind") or "call", "where": call_where(c),
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
               "withdrawn": "Replaced by a newer request", "abandoned": "Left unfinished",
               "parked": "Waiting on your answer"}
#: Each run outcome's status (the glyph and the filter): a parked request is still waiting on you, never "done".
RUN_STATUS = {"final": "done", "error": "failed", "cancelled": "stopped", "withdrawn": "stopped",
              "abandoned": "stopped", "parked": "waiting"}


def run_outcome(outcome: Any) -> tuple[str, str]:
    """(the outcome in plain words, its status) for a run's stored outcome (the agent's ``final``/``error``/
    ``cancelled``/``withdrawn``/``abandoned``/``parked``; none stored is ``final``, practice memory's own default).
    An outcome this window doesn't know reads "Ended" with no status: never "Done" (review F2)."""
    key = str(outcome or "final")
    return RUN_OUTCOME.get(key, "Ended"), RUN_STATUS.get(key, "")


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
                outcome, status = run_outcome(run.get("outcome"))
                g = {"id": gid, "kind": "request", "at": float(run.get("at") or row["at"]),
                     "title": request_title(words) if words else ("Something you asked by voice" if voice else "Something you asked"),
                     "said": "you said" if voice else "you typed", "outcome": outcome,
                     "took": took_words((run.get("ms") or 0) / 1000), "cost": money(run.get("cost_usd")) if run.get("cost_usd") else "",
                     "status": status, "steps": 0, "failed": 0, "wrong_text": row.get("wrong_text") or ""}
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
            g["failed"] and g["failed"] == g["steps"]) else "waiting" if g["status"] == "waiting" else "did"
    return out_rows, sorted(groups.values(), key=lambda g: -(g["at"] or 0))


# -- Settings ------------------------------------------------------------------------------------------------------


VOICE_PROVIDERS = [{"key": "gemini", "label": "Gemini, Leda", "note": "The default voice"},
                   {"key": "deepgram", "label": "Deepgram, Aura", "note": "Faster replies, about 0.8 s sooner"},
                   {"key": "elevenlabs", "label": "ElevenLabs", "note": "Uses your ElevenLabs voice id"}]


def takeover_view(px: Any) -> dict[str, Any]:
    """Settings → While I work: how far the mouse must move while Yuki works for Yuki to pause (``ui.json``
    ``takeover_move_px``, read by the takeover watch each time a task starts acting). ``px``: the saved distance; the
    choices in plain words, the saved one among them (a distance set by hand that is none of them shows as itself)."""
    from yuki.ui import ui_settings as U

    try:
        px = int(px)
    except (TypeError, ValueError):
        px = U.UiSettings().takeover_move_px
    choices = [{"px": p, "words": U.TAKEOVER_MOVE_WORDS.get(p, U.TAKEOVER_MOVE_OWN)} for p in U.TAKEOVER_MOVE_CHOICES]
    if px not in U.TAKEOVER_MOVE_CHOICES:
        choices.append({"px": px, "words": U.TAKEOVER_MOVE_OWN})
        choices.sort(key=lambda c: c["px"])
    return {"px": px, "label": U.TAKEOVER_MOVE_LABEL, "hint": U.TAKEOVER_MOVE_HINT, "choices": choices}


def build_settings(memory: Any, *, live: dict[str, Any] | None = None, devices: dict[str, Any] | None = None,
                   policy_path: Path | None = None, ui_path: Path | None = None) -> dict[str, Any]:
    """Settings: the autonomy policy, app access (look: the privacy blocklist; act: the policy's per-app switch),
    voice (the tray's live state when it runs, else voice.json), offers and check-ins and quiet hours (the privacy
    file), pause, calls, accent and motion and the takeover distance (ui.json; ``ui_path``: a test's own)."""
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
    ui = ui_settings.load(ui_path)
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
                  "quiet_start": str(nudges.get("quiet_start", "00:30")), "quiet_end": str(nudges.get("quiet_end", "08:00")),
                  # the hours an on-switch brings back after they were turned off (equal start and end = none)
                  "quiet_last": list(ui.quiet_last) if ui.quiet_last else None},
        "privacy": {"paused": bool(st.get("paused")), "seen_min": seen_min,
                    "seen_apps": len([i for i in today_act.get("items") or [] if float(i.get("present_s") or 0) >= 30]),
                    "blocked_sites": len([s for s in user.get("blocked_domains", []) if str(s).strip()])},
        "calls": [{"app": c.get("name") or c.get("app") or "", "decision": c.get("decision") or "ask", "key": c.get("app")}
                  for c in _safe(memory.call_prefs, []) or []],
        # what a recorded call keeps (read only: the privacy file's [calls] knobs, shown as text)
        "call_knobs": call_knobs(privacy),
        "appearance": {"accent": ui.accent, "reduce_motion": bool(ui.reduce_motion),
                       "accents": [{"key": k, "label": str((v or {}).get("label") or k.capitalize())}
                                   for k, v in T.ACCENTS.items()]},
        # While I work: how far the mouse must move for me to pause (the takeover watch reads it as a task starts)
        "takeover": takeover_view(ui.takeover_move_px),
        "usage": yuki_usage(),
        # Settings → AI (report 67): each job's service and model, each service's key status (never a key); no
        # network here (the page asks for model lists with the ai_models action, off the UI thread)
        "ai": _safe(_ai_page, None),
    }


def _ai_page() -> dict[str, Any]:
    from yuki.agent.providers.summary import page

    return page()


def call_knobs(privacy: dict[str, Any] | None) -> dict[str, bool]:
    """``{"record_others", "game_voice"}``: the privacy file's ``[calls]`` knobs as the memory service reads them
    (a key the file doesn't set takes memory's own default, :class:`yuki.memory.calls.CallConfig`). Read only: the
    Settings text says what a recorded call keeps."""
    table = (privacy or {}).get("calls") if isinstance((privacy or {}).get("calls"), dict) else {}
    try:
        from yuki.memory.calls import CallConfig

        cfg = CallConfig.from_dict(table or {})
        return {"record_others": bool(cfg.record_others), "game_voice": bool(cfg.game_voice)}
    except Exception:
        return {"record_others": bool(table.get("record_others", True)), "game_voice": bool(table.get("game_voice", True))}


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
                    "started_at": min([item.at] + [s.started for s in item.steps]), "status": item.status,
                    "step": len(steps)})   # the step it is on, no total: the face's and field's figures (appPT)
    return out


# -- change stamps (the window's live refresh: 42-window-live-refresh.md §6.2) -------------------------------------

#: Each page's memory tables (the ones its builder reads). Work's week figure and Activity's "Seen today, by app"
#: read the timeline, so it is in their stamps (review F3): a revisit after STALE_S, or the open page's slow fallback,
#: rebuilds them while you are at the PC; the hash still skips a build that shows nothing new. Home and Work show the
#: to-do list (appTodo): your to-dos, the commitments (conversation facts) and the open loops.
TODO_TABLES = ("todos", "conversation_facts", "open_loops")
PAGE_TABLES: dict[str, tuple[str, ...]] = {
    # Home's Your list follows Work's split (report 69): who owes what and what you moved to the list are work_items
    "home": ("questions", "nudges", "timeline", "goals", "contacts", "calls", *TODO_TABLES, "work_items"),
    "work": ("timeline", *TODO_TABLES, "work_items", "messages"),   # the Work page (4 Oct): replies, loops, meetings
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
           "rhythm_view", "goals_today", "run_rows", "call_rows", "step_what", "step_result",
           "runs_of", "offer_card", "shift_times", "media_now", "read_pending", "week_days", "week_figure",
           "todo_view", "todo_lists", "due_words", "due_epoch", "now_session", "day_sessions"]
