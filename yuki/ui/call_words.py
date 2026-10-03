"""What a call (or a game's voice chat) is keeping, in plain words, from memory's ``call_status()``. No Qt: the
island, the tray, the consent card, the web window and the Qt window all say it the same way.

Since 3 Oct 2026 a voice session records both sides (handbook rule 9, report 60): the user's microphone (headphones
only) and the call app's own sound through process loopback; a game's own voice chat is a session of its own
(``kind`` "game") with its own track. ``call_status()`` says what is captured right now: ``mic`` (the user's
microphone, recorded once for the session, and why it is paused) and ``tracks`` (each app's own sound). Every
sentence here is built from those fields: the app names come from the data (``app``, ``track_kind``), never from a
list, and nothing reads what anyone said.

Words used everywhere:

* a call row is "<app> call"; a game row is "<app> voice chat";
* what is being kept now: "Discord + you", "VALORANT voice + you", "Discord + VALORANT voice + you",
  "Discord (your mic waits for headphones)", "you on Discord" (the call's own sound is off);
* the line: "Recording · Discord + you"; nothing captured this moment: "Waiting for headphones · Discord" or
  "Paused · Discord call".
"""

from __future__ import annotations

from typing import Any

#: A row's statuses while it may record (said yes or always; waiting for headphones records the app's sound).
RECORDING_STATUSES = ("recording", "waiting_headphones")
#: An app track's states that count as kept: recording, or opening right now (it records within a poll; one that
#: cannot open turns ``unavailable`` and drops out of the words).
TRACK_KEPT = ("recording", "starting")


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def is_game(row: Any) -> bool:
    """A game's own voice chat (``kind`` / ``track_kind`` "game"), else a call."""
    row = _d(row)
    return (row.get("kind") or row.get("track_kind")) == "game"


def app_of(row: Any) -> str:
    return " ".join(str(_d(row).get("app") or "").split())


def session_name(row: Any) -> str:
    """``"Discord call"`` | ``"VALORANT voice chat"`` (``"a call"`` / ``"game voice chat"`` without an app)."""
    app = app_of(row)
    if is_game(row):
        return f"{app} voice chat" if app else "game voice chat"
    return f"{app} call" if app else "a call"


def head_label(row: Any) -> str:
    """A card's or a tile's label: ``"Call · Discord"`` | ``"Game voice · VALORANT"``."""
    app = app_of(row)
    word = "Game voice" if is_game(row) else "Call"
    return f"{word} · {app}" if app else word


def where_words(row: Any) -> str:
    """The tray card's head: ``"On Discord"`` for a call, ``"VALORANT voice chat"`` for a game."""
    if is_game(row):
        return session_name(row)
    app = app_of(row)
    return f"On {app}" if app else "On a call"


def track_name(track: Any, *, short: bool = False) -> str:
    """One app track in the indicator: ``"Discord"`` | ``"VALORANT voice"`` (``short``: ``"VALORANT"``)."""
    app = app_of(track)
    if is_game(track):
        if short:
            return app or "game voice"
        return f"{app} voice" if app else "game voice"
    return app or "the call"


def sound_name(track: Any) -> str:
    """One app track in a sentence: ``"Discord’s sound"`` | ``"VALORANT’s voice chat"``."""
    app = app_of(track)
    if is_game(track):
        return f"{app}’s voice chat" if app else "the game’s voice chat"
    return f"{app}’s sound" if app else "the call’s sound"


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _rows(status: dict[str, Any]) -> dict[Any, dict[str, Any]]:
    """Every row the status names, by id: the sessions, the call, the pending card's row."""
    rows: dict[Any, dict[str, Any]] = {}
    for s in status.get("sessions") or []:
        if isinstance(s, dict) and s.get("id") is not None:
            rows[s["id"]] = s
    call = _d(status.get("call"))
    if call.get("id") is not None:
        rows[call["id"]] = {**rows.get(call["id"], {}), **call}
    return rows


def kept(status: Any) -> dict[str, Any]:
    """What is being kept now, from a ``call_status()``.

    ``{"known", "recording", "you", "mic_waits", "tracks", "words", "short", "line", "short_line", "sentence",
    "kinds"}`` (``short``: the words for a narrow pill, a game's name without "voice" beside a call, the headphones
    said in four words): ``known`` is False for a status without ``mic`` / ``tracks`` (an older memory, or a bare call row),
    then the words fall back to the row's own status; ``recording`` is the status's top-level flag (anything
    captured: a row, the microphone or any app's own sound); ``kinds`` the kinds of the rows that may record now
    (the island says so again when a game joins a call)."""
    status = _d(status)
    call = _d(status.get("call"))
    rows = _rows(status)
    mic = status.get("mic") if isinstance(status.get("mic"), dict) else None
    tracks = [t for t in status.get("tracks") or [] if isinstance(t, dict)]
    known = mic is not None or "tracks" in status
    live = sorted((t for t in tracks if t.get("state") in TRACK_KEPT), key=is_game)   # the call's sound first
    you = bool(mic) and mic.get("state") == "recording"
    waits = bool(mic) and mic.get("state") == "paused" and mic.get("reason") == "not_headphones"
    owner = rows.get((mic or {}).get("call_id")) or call
    recording = bool(status.get("recording")) if "recording" in status else call.get("status") == "recording"
    words = short = line = short_line = ""
    if live:
        words = " + ".join([track_name(t) for t in live] + (["you"] if you else []))
        short = " + ".join([track_name(t, short=len(live) > 1) for t in live] + (["you"] if you else []))
        if waits:
            words += " (your mic waits for headphones)"
            short += " · mic waits for headphones"
    elif you:
        app = app_of(owner)
        words = short = (f"you in {app}" if is_game(owner) else f"you on {app}") if app else "your mic"
    if words:
        line, short_line = f"Recording · {words}", f"Recording · {short}"
    elif not known:   # an older status, or a bare row: its own status, without claiming whose sound
        st = call.get("status")
        if st == "waiting_headphones":
            line = f"Waiting for headphones · {app_of(call)}" if app_of(call) else "Waiting for headphones"
        elif st == "recording":
            line = f"Recording · {session_name(call)}"
        short_line = line
    else:              # nothing is captured this moment
        consented = [r for r in rows.values() if r.get("status") in RECORDING_STATUSES and not r.get("ended")]
        if waits or call.get("status") == "waiting_headphones":
            app = app_of(owner) or app_of(call)
            line = f"Waiting for headphones · {app}" if app else "Waiting for headphones"
        elif consented:
            line = f"Paused · {session_name(consented[0])}"
        short_line = line
    sources = (["your mic"] if you else []) + [sound_name(t) for t in live]
    sentence = f"{_cap(_join(sources))}." if sources else ""
    if waits and sentence:
        sentence += " Your mic waits for headphones."
    kinds = tuple(sorted({("game" if is_game(r) else "call") for r in rows.values()
                          if r.get("status") in RECORDING_STATUSES and not r.get("ended")}))
    if not kinds and call.get("status") in RECORDING_STATUSES:
        kinds = ("game" if is_game(call) else "call",)
    return {"known": known, "recording": recording, "you": you, "mic_waits": waits,
            "tracks": [track_name(t) for t in live], "words": words, "short": short, "line": line,
            "short_line": short_line, "sentence": sentence, "kinds": kinds}


def remembering_title(status: Any) -> str:
    """The window's Running row: ``"Remembering the Discord call"``, ``"Remembering VALORANT voice chat"``,
    ``"Remembering the Discord call and VALORANT voice chat"`` (the rows that may record now)."""
    status = _d(status)
    rows = [r for r in _rows(status).values() if r.get("status") in RECORDING_STATUSES and not r.get("ended")]
    if not rows and _d(status.get("call")):
        rows = [_d(status.get("call"))]
    rows.sort(key=is_game)
    names = [session_name(r) if is_game(r) else f"the {session_name(r)}" for r in rows]
    return f"Remembering {_join(names)}" if names else "Remembering a call"


def call_rec(call: Any) -> bool:
    """Something is being kept now (the rec dot, the tray's rec badge): a :func:`live_view`'s ``recording`` (the mic
    or any app's own sound), or, for a bare row, its own status."""
    call = _d(call)
    if not call:
        return False
    return bool(call.get("recording")) if "recording" in call else call.get("status") == "recording"


def call_line(call: Any) -> str:
    """The indicator's words for a :func:`live_view` (its ``line``), or a bare call row's own status."""
    call = _d(call)
    if call.get("line"):
        return str(call["line"])
    return kept({"call": call})["line"] or (f"Recording · {session_name(call)}" if call else "")


def recording_row(status: Any) -> dict[str, Any]:
    """The row the indicators are about: ``call`` while it may record (memory's own pick: the indicator and Stop
    follow it), else a row that may record (a game recording while a new call's card is up), else the row of the
    first app track that records, else ``call``. A session row carries no times (no timer then)."""
    status = _d(status)
    call = _d(status.get("call"))
    if call.get("status") in RECORDING_STATUSES:
        return dict(call)
    row = next((r for r in _rows(status).values() if r.get("status") in RECORDING_STATUSES and not r.get("ended")),
               None)
    if row is not None:
        return {"id": row.get("id"), "app": row.get("app"), "kind": row.get("kind") or "call", "status": row.get("status")}
    track = next((t for t in status.get("tracks") or [] if isinstance(t, dict) and t.get("state") == "recording"), None)
    if track is not None:
        return {"id": track.get("call_id"), "app": track.get("app"), "kind": track.get("track_kind") or "call",
                "status": "recording"}
    return dict(call)


def live_view(status: Any) -> dict[str, Any] | None:
    """The row the indicators follow (:func:`recording_row`), with what is kept now: ``recording`` (the status's
    top-level flag: Stop must be reachable while it is True), ``line`` / ``short`` / ``kept`` / ``sentence`` /
    ``kinds`` (:func:`kept`) and ``where``. ``None`` when nothing records and no row waits for headphones."""
    status = _d(status)
    call = _d(status.get("call"))
    k = kept(status)
    if not (k["recording"] or call.get("status") in RECORDING_STATUSES):
        return None
    view = recording_row(status) or {"status": "recording"}
    line = k["line"] or call_line(view)
    view.update(recording=k["recording"], line=line, short=k["short_line"] or line, short_words=k["short"],
                kept=k["words"], sentence=k["sentence"], kinds=k["kinds"], where=session_name(view))
    return view


def card_target(status: Any) -> dict[str, Any] | None:
    """The row whose consent card should be up: the live ``call`` while it asks (never an ``ask_later`` game: its
    card waits until it can be seen), else ``pending_card`` (a game session that ended before its card could be
    shown; marked ``ended``). ``keeps_sound``: False when the worker says the app's own sound is off
    (``record_others_off`` on a track), so the card never promises it."""
    status = _d(status)
    call = _d(status.get("call"))
    target: dict[str, Any] | None = None
    if call.get("asking") and call.get("status") != "ask_later" and call.get("id") is not None:
        target = dict(call)
    else:
        pending = _d(status.get("pending_card"))
        if pending.get("id") is not None and pending.get("asking", True) and pending.get("status") != "ask_later":
            target = dict(pending, ended=True)
    if target is None:
        return None
    off = any(isinstance(t, dict) and t.get("reason") == "record_others_off" for t in status.get("tracks") or [])
    target["keeps_sound"] = not off
    return target


def consent_text(call: Any) -> tuple[str, str, str]:
    """``(question, detail, headphones note or "")`` for a card's row (:func:`card_target`, or a bare call)."""
    call = _d(call)
    app = app_of(call)
    keeps = call.get("keeps_sound", True) is not False
    if is_game(call):
        name = app or "the game"
        question = f"Remember {app} voice chat?" if app else "Remember this game’s voice chat?"
        what = ("Your mic and the game’s voice chat, as their own track, turned into text after you play"
                if keeps else "Your mic only, turned into text after you play")
        if call.get("ended"):   # the session is over: nothing was kept, the answer holds only with Always
            detail = (f"{what}. Nothing was kept from the game that just ended: Always for {name} holds for the "
                      "next one.")
        else:
            detail = f"{what}, on this PC."
        sound = "the game’s voice chat"
    else:
        question = f"You’re on a {app} call." if app else "You’re on a call."
        detail = (f"Keep a private timeline of this call? Your mic and {app or 'the call'}’s sound, turned into text "
                  "later on this PC." if keeps else
                  "Keep a private timeline of what you say? Only your mic, turned into text later on this PC.")
        sound = f"{app}’s sound" if app else "the call’s sound"
    note = ""
    if call.get("headphones") is False and not call.get("ended"):
        output = call.get("form_factor") or "not headphones"
        note = (f"Your mic waits for headphones (your sound is on {output} now); {sound} is kept either way."
                if keeps else
                f"Only with headphones, so other people’s voices aren’t picked up. Your sound is on {output} now, "
                "so I’d wait for headphones.")
    return question, detail, note


def card_keys(call: Any) -> tuple[str, str]:
    """The card's two keys: ``("Record this call", "Not this call")``; a game ``("Record it", "Not this time")``;
    a game that already ended ``("Yes", "No")`` (nothing records now; Always / Never hold for the next one)."""
    call = _d(call)
    if is_game(call):
        return ("Yes", "No") if call.get("ended") else ("Record it", "Not this time")
    return "Record this call", "Not this call"


def indicator_text(call: Any) -> str:
    """The call pill's words: the live view's line ("Recording · Discord + you")."""
    return call_line(call)


__all__ = ["RECORDING_STATUSES", "TRACK_KEPT", "app_of", "call_line", "call_rec", "card_keys", "card_target", "consent_text", "head_label",
           "indicator_text", "is_game", "kept", "live_view", "recording_row", "remembering_title", "session_name",
           "sound_name",
           "track_name", "where_words"]
