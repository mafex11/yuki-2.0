"""The finish gate (revamp D, S2; 01 §8.1 F, 11 §8.1): three verdicts, the finish line written by code, and the
independent confirmation of a send.

**Three verdicts per action**, from what its own report measured (``yuki.agent.loop.action_verdict``) and what Yuki
has learned about the control:

* ``verified`` - the report shows the effect;
* ``expected`` (expected-unverifiable) - the report cannot show it by the nature of the control, not because anything
  went wrong: a field with no text to read back, or a control practice memory has learned behaves so (its earlier
  unverifiable steps were each confirmed by something independent afterwards - a later verified step in the same
  window, or the chat's capture of the sent message - and none was contradicted). It never triggers a second look, and
  a done is not held for it;
* ``contradicted`` - the action ran and its report shows the effect did not happen (focus, window and page watched and
  nothing changed; the field does not hold the text; the page is not at the address; a sent message not seen).

What is none of these is ``unverified`` (the report could not tell): a finish it marks is held for a look, as before.

**The finish line** (S4 output diet): ``finishes_with`` is a boolean now; the line the request ends with is written
here from the call and its target (the window's app, the tab's name, the level asked for), never by the model, so a
finishing call costs ~40 fewer output tokens. ``finish_words`` is the brain's own wording when the report cannot name
the outcome the way the user asked (a key, a click). The line rides on the ``tool_call`` event in the
``finishes_with`` field as a string, which is what voice v3.17 reads to say a verified result by code.

**Send confirmation**: after a call the authorization judge marked as sending to a person, memory's own capture of the
chat is asked for a new message of the user's (``messages.is_me``) in that app since the call, similar to the text,
within ~4 s (``Settings.send_confirm_s``): the Now service is asked to read the window again, then memory is polled.
A send that is not seen is reported honestly ("I pressed send but could not see it arrive").

Templates are fixed sentences per tool operation (plumbing that names a measured outcome), never a reading of words.
"""

from __future__ import annotations

import time
from typing import Any, Callable

#: The verdict reason of an action whose effect its report cannot show by the nature of its target (revamp A8).
EXPECTED_UNVERIFIABLE = ("the field shows no text to read back (expected for this kind of field, not a failure; "
                         "the keys were sent)")
#: The reason when practice memory has learned the control behaves so.
LEARNED_UNVERIFIABLE = ("this control's report never shows the effect (learned: {n} earlier runs confirmed it worked "
                        "afterwards, none contradicted it)")
#: V6 (3 Oct): a scroll on a web page that takes no UI Automation scrolling: the wheel went in, and the page does not
#: report where it is (measured on each scroll: no scroll pattern, the point inside a page). Live a page scrolled and
#: was reported as "did not scroll", and Yuki told the user so while they watched it move.
PAGE_SCROLL_UNVERIFIABLE = ("the web page does not report its scroll position here (expected for a page in this app, "
                            "not a failure; the wheel went in)")

VERIFIED, EXPECTED, CONTRADICTED, UNVERIFIED, FAILED = "verified", "expected", "contradicted", "unverified", "failed"

#: A control is expected-unverifiable once this many of its earlier unverifiable steps were confirmed by something
#: independent and none of its steps was contradicted (practice memory's run traces).
LEARNED_MIN_CONFIRMED = 2


def classify(ok: bool, verdict: bool | None, why: str) -> str:
    """One action's class from its outcome and verdict (``why`` names an expected-unverifiable control)."""
    if not ok:
        return FAILED
    if verdict is True:
        return VERIFIED
    if verdict is False:
        return CONTRADICTED
    if (why in (EXPECTED_UNVERIFIABLE, PAGE_SCROLL_UNVERIFIABLE)
            or why.startswith(LEARNED_UNVERIFIABLE.split("{")[0])):
        return EXPECTED
    return UNVERIFIED


def is_expected(why: str) -> bool:
    return classify(True, None, why or "") == EXPECTED


# ---------------------------------------------------------------------------
# The finish line
# ---------------------------------------------------------------------------


def app_name(process: str | None) -> str:
    """A window's app as people say it ("chrome.exe" -> "Chrome"), structurally (no list of apps)."""
    from yuki.core.appnames import prettify

    return prettify(process or "") or "It"


def finish_line(name: str, tool_input: dict[str, Any], *, process: str | None = None,
                tab_name: str | None = None) -> str:
    """The sentence a finishing call ends the request with, from the call and its target alone.

    ``process``: the target window's process (from the window list); ``tab_name``: the tab's name from the latest
    read of its strip. Tools whose report cannot name the outcome get a plain "Done." (the brain gives
    ``finish_words`` there when the user's words need more)."""
    app = app_name(process)
    tool_input = tool_input or {}
    if name == "window_action":
        state = {"minimize": "minimized", "maximize": "maximized", "fullscreen": "full screen",
                 "restore": "restored", "focus": "in front", "close": "closed"}.get(str(tool_input.get("action")))
        return f"{app} is {state}." if state else "Done."
    if name == "focus_window":
        return f"{app} is in front."
    if name == "launch_app":
        query = " ".join(str(tool_input.get("query") or "").split())
        return f"{query[:1].upper() + query[1:]} is open." if query else "It's open."
    if name == "open_url":
        target = str(tool_input.get("app") or "").strip()
        return f"It's open in {target}." if target else "It's open."
    if name == "tab":
        if str(tool_input.get("action")) == "close":
            return f"Closed {tab_name}." if tab_name else "Closed the tab."
        return f"{tab_name} is showing." if tab_name else "Switched tabs."
    if name == "app_volume":
        target = app_name(str(tool_input.get("app") or "")) if tool_input.get("app") else "Its"
        if isinstance(tool_input.get("mute"), bool):
            return f"{target} is {'muted' if tool_input['mute'] else 'unmuted'}."
        level = tool_input.get("level")
        if isinstance(level, (int, float)) and not isinstance(level, bool):
            return f"{target} is at {int(round(float(level)))}."
        change = tool_input.get("change")
        if isinstance(change, (int, float)) and not isinstance(change, bool) and change:
            return f"Turned {target} {'down' if change < 0 else 'up'}."
        return "Done."
    if name == "write_clipboard":
        return "Copied."
    return "Done."


def noop_line(name: str, details: dict[str, Any] | None, *, process: str | None = None) -> str | None:
    """N1 (report 47): the finish line of an action whose own report says nothing moved (``details["noop"]``: the
    level was already where it was asked to go, or at its end), in place of the line written before it ran ("Turned
    Spotify up."), or ``None`` for an action that did move. Fixed sentences over the measured numbers."""
    details = details if isinstance(details, dict) else {}
    if not details.get("noop"):
        return None
    if name == "app_volume":
        app = app_name(str(details.get("process") or process or ""))
        before = details.get("before")
        at = f"already at {float(before):g}%" if isinstance(before, (int, float)) else "already there"
        change = (details.get("asked") or {}).get("change")
        verb = "raise" if isinstance(change, (int, float)) and change > 0 else (
            "lower" if isinstance(change, (int, float)) and change < 0 else "change")
        if details.get("muted_before") and verb != "lower":
            # review F5: muted in the mixer is the real reason it is quiet; unmuting is the fix to offer
            return (f"{app} is {at} in Windows' volume mixer but muted there, so nothing changed. Want me to unmute "
                    f"it?")
        system = details.get("system_volume")
        system_part = f"the system volume, at {float(system):g}%," if isinstance(system, (int, float)) else \
            "the system volume"
        return (f"{app} is {at} in Windows' volume mixer, so nothing changed. Its own player volume and "
                f"{system_part} are separate. Want me to {verb} one of those?")
    if name == "set_slider":
        at = details.get("before_percent")
        return f"It's already at {float(at):g}%, so nothing changed." if isinstance(at, (int, float)) else \
            "It's already there, so nothing changed."
    return "It was already that way, so nothing changed."


def finish_request(tool_input: dict[str, Any]) -> tuple[bool, str | None]:
    """``(finishes, own words)`` from a call's finish fields: ``finishes_with`` true (or a string, an older habit:
    taken as the words), ``finish_words`` the brain's own wording."""
    from yuki.agent.tools import FINISH_WORDS_FIELD, FINISHES_FIELD

    flag = (tool_input or {}).get(FINISHES_FIELD)
    words = (tool_input or {}).get(FINISH_WORDS_FIELD)
    words = " ".join(str(words).split()) if isinstance(words, str) and words.strip() else None
    if isinstance(flag, str) and flag.strip():
        return True, words or " ".join(flag.split())
    return (flag is True or words is not None), words


# ---------------------------------------------------------------------------
# Send confirmation
# ---------------------------------------------------------------------------

#: How similar the captured message must be to what was typed (difflib's ratio over the two texts, casefolded and
#: whitespace-squashed: a measured similarity; a captured text containing the typed one also counts).
SIMILAR = 0.6
#: How often memory is polled while waiting, and when the Now service is asked to read the window again (s after
#: the send; the watcher reads a window at most once in 2 s).
POLL_S = 0.35
REFRESH_AT_S = (0.25, 2.3)


def similar(a: str, b: str) -> float:
    """How alike two message texts are (0-1), casefolded and whitespace-squashed."""
    import difflib

    x, y = " ".join((a or "").split()).casefold(), " ".join((b or "").split()).casefold()
    if not x or not y:
        return 0.0
    if x in y or y in x:
        return 1.0
    return difflib.SequenceMatcher(None, x, y).ratio()


def confirm_send(lookup: Callable[[float], list[dict[str, Any]]], text: str, *, since: float, within_s: float = 4.0,
                 refresh: Callable[[], Any] | None = None, cancelled: Callable[[], bool] | None = None,
                 now: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep
                 ) -> dict[str, Any]:
    """Wait (at most ``within_s``) for memory to show the user's new message similar to ``text``.

    ``lookup(since)`` returns the user's own messages first seen since then (``{"text", "at", "thread", ...}``);
    ``refresh()`` asks the Now service to read the window again. Returns ``{"confirmed": bool, "similarity",
    "waited_ms", "polls", "match"?}``; never raises."""
    started = now()
    refreshes = list(REFRESH_AT_S)
    best = 0.0
    polls = 0
    while True:
        elapsed = now() - started
        while refreshes and elapsed >= refreshes[0]:
            refreshes.pop(0)
            if refresh is not None:
                try:
                    refresh()
                except Exception:  # noqa: BLE001
                    pass
        try:
            rows = lookup(since) or []
        except Exception:  # noqa: BLE001 - memory missing: not confirmed
            rows = []
        polls += 1
        for row in rows:
            score = similar(text, str(row.get("text") or ""))
            best = max(best, score)
            if score >= SIMILAR:
                return {"confirmed": True, "similarity": round(score, 3), "waited_ms": round(elapsed * 1000),
                        "polls": polls, "match": {k: row.get(k) for k in ("thread", "at", "status")}}
        if elapsed >= within_s or (cancelled is not None and cancelled()):
            return {"confirmed": False, "similarity": round(best, 3), "waited_ms": round(elapsed * 1000),
                    "polls": polls}
        sleep(min(POLL_S, max(0.0, within_s - elapsed)))


__all__ = ["CONTRADICTED", "EXPECTED", "EXPECTED_UNVERIFIABLE", "FAILED", "LEARNED_MIN_CONFIRMED",
           "LEARNED_UNVERIFIABLE", "PAGE_SCROLL_UNVERIFIABLE", "UNVERIFIED", "VERIFIED", "app_name", "classify",
           "confirm_send", "finish_line", "finish_request", "is_expected", "noop_line", "similar"]
