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
# The outcome verdict (honest, report 87): one verdict per request, with its evidence level
# ---------------------------------------------------------------------------

#: The request's outcome levels, strongest first. ``verified``: a step's own check showed it on screen (what was seen is
#: ``seen``); ``likely``: the action ran and its check is weak by the nature of the target (a field with no read-back,
#: a page that does not report its scroll, a control learned to behave so) or a later step backed it; ``unverified``:
#: nothing could show whether it worked (a send not seen in the chat, a key with nothing watched); ``failed``: a step
#: failed or its check showed the effect did not happen, and no later step put it right.
LIKELY = "likely"
LEVELS = (VERIFIED, LIKELY, UNVERIFIED, FAILED)
#: No action ran: an answer, nothing done on the desktop to grade.
ANSWER = "answer"
#: The levels whose words may be said as they are written (by code, or by the talker without an outcome note).
SAYABLE = frozenset({VERIFIED, LIKELY, ANSWER})

#: The level each action class gives.
_CLASS_LEVEL = {VERIFIED: VERIFIED, EXPECTED: LIKELY, UNVERIFIED: UNVERIFIED, CONTRADICTED: FAILED, FAILED: FAILED}
#: The levels from weakest (index 0) to strongest.
_RANK = {FAILED: 0, UNVERIFIED: 1, LIKELY: 2, VERIFIED: 3}

#: The level in plain words, for the done card, the island and the window's run (second person, no tool names).
LEVEL_LABELS = {VERIFIED: "Checked on screen", LIKELY: "Likely done", UNVERIFIED: "Not confirmed",
                FAILED: "Didn’t work", ANSWER: ""}

#: The sentence said by code for an outcome below ``likely`` when no talker run rewords it (the owed ledger's
#: fallback): fixed sentences per measured fact, never a reading of the brain's words.
LINE_SEND_UNCONFIRMED = "I tried to send it, but I couldn't confirm it went."
LINE_UNVERIFIED = "I tried, but I couldn't confirm it worked."
LINE_PARTIAL = "Part of that didn't work."
LINE_FAILED = "That didn't work."
#: Said after an answer that tells the screen's state without having read it (nothing was done).
LINE_UNREAD = "I haven't checked that on screen, though."

#: Tools whose report is a command's exit status (no screen to check): one that ran without an error is ``likely``
#: (replayed 5 Oct: a request that read a fact with a command and answered it was otherwise graded "not confirmed").
EXIT_STATUS_TOOLS = frozenset({"run_powershell"})

#: What a verified step's own check saw, per tool operation (plain words: cards, the talker's ``checked``).
_SEEN_WINDOW = {"close": "its window is gone", "minimize": "its window is minimized",
                "maximize": "its window is maximized", "fullscreen": "its window is full screen",
                "restore": "its window is back to normal", "focus": "its window is in front"}
_SEEN_TOOL = {"focus_window": "its window is in front", "launch_app": "its own window is in front",
              "open_url": "the page is open at the address", "tab": "the tab shows", "type_text": "the box holds the text",
              "set_slider": "the level reads back", "app_volume": "the level reads back",
              "write_clipboard": "the clipboard holds it", "scroll": "the page moved"}


#: A send's states on the run trace (``send``): seen in memory's capture of the chat, not seen within the wait, or
#: pressed with no text known to look for (review F3: an Enter after typing that failed), which nothing could check.
SEND_CONFIRMED, SEND_UNCONFIRMED, SEND_UNCHECKED = "confirmed", "unconfirmed", "unchecked"


def step_level(step: dict[str, Any]) -> str:
    """One action step's level from its run-trace facts (``ok``, ``verdict``, ``why``, ``confirmed``, ``send``)."""
    send = step.get("send")
    if send == SEND_CONFIRMED:
        return VERIFIED
    if send in (SEND_UNCONFIRMED, SEND_UNCHECKED):
        return UNVERIFIED if step.get("ok") is not False else FAILED
    level = _CLASS_LEVEL[classify(step.get("ok") is not False, step.get("verdict"), str(step.get("why") or ""))]
    if level == UNVERIFIED and step.get("confirmed"):
        return LIKELY   # backed afterwards by something independent (a later verified step in the same window)
    if level == UNVERIFIED and step.get("tool") in EXIT_STATUS_TOOLS:
        return LIKELY   # a command's own report is its exit status: it ran without an error, a weak check
    return level


def _seen(step: dict[str, Any], level: str) -> str:
    """What the step's check saw (or could not), in plain words for the card and the talker."""
    send = step.get("send")
    if send == SEND_CONFIRMED:
        return "the message shows in the chat"
    if send == SEND_UNCONFIRMED:
        return "the message did not show up in the chat"
    if send == SEND_UNCHECKED:
        return "there was no message text known to look for in the chat"
    tool = str(step.get("tool") or "")
    if level == VERIFIED:
        if tool == "window_action":
            return _SEEN_WINDOW.get(str((step.get("input") or {}).get("action") or ""), "the window reads back that way")
        return _SEEN_TOOL.get(tool, "the window changed after it")
    if level == LIKELY:
        if step.get("confirmed"):
            return "a later step showed it worked"
        if tool in EXIT_STATUS_TOOLS:
            return "the command ran without an error"
        return "it can't be read back from here, which is normal for it"
    if level == UNVERIFIED:
        return "nothing on screen showed whether it worked"
    return "it failed" if step.get("ok") is False else "the screen showed it didn't happen"


def _app(step: dict[str, Any]) -> str | None:
    """The app a step acted in, comparable across steps: its window's process, else the app its input names (the
    mixer's level names its app, not a window), casefolded without ``.exe``; ``None`` unknown. Plumbing."""
    raw = str(step.get("app") or (step.get("input") or {}).get("app") or "").strip().casefold()
    raw = raw[:-4] if raw.endswith(".exe") else raw
    return raw or None


def _same_chat(step: dict[str, Any], other: dict[str, Any]) -> bool:
    """Whether a later send is a retry of an earlier one (review F2): the same known app and the same message (its
    text's fingerprint, ``send_sig``), or with no text known for the earlier one, the same window by its title."""
    app = _app(step)
    if app is None or _app(other) != app:
        return False
    sig, other_sig = step.get("send_sig"), other.get("send_sig")
    if sig and other_sig:
        return sig == other_sig
    window, other_window = step.get("window"), other.get("window")
    return bool(window) and window == other_window


def _resolved(index: int, actions: list[dict[str, Any]], levels: list[str], *, finished_at: int | None) -> bool:
    """Whether a later step put an earlier non-verified one right. Facts of the trace only, and only a later step
    toward the same effect (review F1-F3): never an unrelated verified step, never a step in another known app.

    * a send: only a later confirmed send of the same message in the same chat (:func:`_same_chat`);
    * any other step: a later try of the same tool in the same app (the retry's own level stands for it), or the
      verified action the brain marked as completing the request (``finished_at``: its index) when it acted in the same
      app, or came in the same response (every call of that response succeeded, or the loop would not finish on it).
      The slider-then-mixer case (a player's slider failed, then the mixer level for that app, marked finishing) is
      the same app by the mixer's own ``app`` field. A step's own later confirmation (``confirmed``: a later verified
      step in the same window) makes it ``likely`` in :func:`step_level`, never ``verified``."""
    step = actions[index]
    later = range(index + 1, len(actions))
    if step.get("send"):
        return any(actions[j].get("send") == SEND_CONFIRMED and _same_chat(step, actions[j]) for j in later)
    app = _app(step)
    if finished_at is not None and finished_at > index:
        done = actions[finished_at]
        same_response = step.get("call") is not None and step.get("call") == done.get("call")
        if (app is not None and _app(done) == app) or (same_response and levels[index] != FAILED):
            return True
    return any(not actions[j].get("send") and actions[j].get("tool") == step.get("tool") and _app(actions[j]) == app
               for j in later)


def _part_done(still: list[int], actions: list[dict[str, Any]], levels: list[str]) -> bool:
    """Whether some other part of a request that failed did work (review F4): a verified step that was not getting
    ready for the failed one, i.e. it acted in another known app than every open failed step, or it is itself an
    effect the user asked for (a confirmed send; a window or volume changed, ``yuki.ui.step_words.changes_state``).
    A focus or an opening in the same app before a failed close is not a part done."""
    from yuki.ui.step_words import changes_state

    failed_apps = {_app(actions[i]) for i in still if levels[i] == FAILED}
    for j, level in enumerate(levels):
        if level != VERIFIED:
            continue
        step = actions[j]
        if step.get("send") == SEND_CONFIRMED or changes_state(str(step.get("tool") or ""), step.get("input")):
            return True
        app = _app(step)
        if app is not None and None not in failed_apps and app not in failed_apps:
            return True
    return False


def outcome_verdict(steps: list[dict[str, Any]] | None, *, unbacked: list[str] | tuple[str, ...] = (),
                    finished_by_action: bool = False) -> dict[str, Any]:
    """The request's one outcome verdict from its run trace (``yuki.agent.loop.Agent._trace``), used by the thinker's
    log, the voice, the done card, the island and the window's run. Data only: the steps' measured verdicts, a send's
    confirmation in memory's capture of the chat, and the claims of the closing message the claim check could not back
    (``unbacked``, its gaps after it asked once); never a reading of the words.

    The level is the weakest of the action steps no later step put right (:func:`_resolved`), and no stronger than
    ``unverified`` while a claim stays unbacked. Returns ``{"level", "label", "acted", "seen", "send", "partial",
    "line", "steps", "open"}``: ``seen`` what the deciding step's check saw (or could not), ``send`` ``confirmed`` /
    ``unconfirmed`` / ``None``, ``line`` the sentence said by code when nothing rewords an outcome below ``likely``
    (``""`` otherwise)."""
    actions = [s for s in (steps or []) if isinstance(s, dict) and s.get("kind") == "action"]
    if not actions:
        level = UNVERIFIED if unbacked else ANSWER
        return {"level": level, "label": LEVEL_LABELS[level], "acted": False,
                "seen": "what the reply says about the screen was not read in this request" if unbacked else "",
                "send": None, "partial": False, "line": LINE_UNREAD if unbacked else "", "step": "", "steps": 0,
                "open": 0}
    levels = [step_level(s) for s in actions]
    # the request ended on the action the brain marked as completing it, and that action verified (the loop finishes
    # only then): the last verified action
    finished_at = (max((i for i, lv in enumerate(levels) if lv == VERIFIED), default=None)
                   if finished_by_action else None)
    still = [i for i, lv in enumerate(levels)
             if lv != VERIFIED and not _resolved(i, actions, levels, finished_at=finished_at)]
    # review F2: the request's send is unconfirmed while any send is still open, not only the last one
    sends = [i for i, s in enumerate(actions) if s.get("send")]
    send = None if not sends else ("unconfirmed" if any(i in still for i in sends) else "confirmed")
    if still:
        decider = min(still, key=lambda i: (_RANK[levels[i]], -i))   # the weakest; the latest of equals
        level = levels[decider]
    else:
        decider = max(range(len(actions)), key=lambda i: (actions[i].get("send") == "confirmed", i))
        level = VERIFIED
    if unbacked and _RANK[level] > _RANK[UNVERIFIED]:
        level = UNVERIFIED
        seen = "what the reply says about the screen was not read in this request"
    else:
        seen = _seen(actions[decider], level)
    partial = level == FAILED and _part_done(still, actions, levels)
    if level == FAILED:
        line = LINE_PARTIAL if partial else LINE_FAILED
    elif level == UNVERIFIED:
        line = LINE_SEND_UNCONFIRMED if send == "unconfirmed" else LINE_UNVERIFIED
    else:
        line = ""
    # the deciding step in the brain's own plain words for it (its ``what``, as the window shows the step)
    step = " ".join(str((actions[decider].get("input") or {}).get("what") or "").split())[:80]
    return {"level": level, "label": LEVEL_LABELS[level], "acted": True, "seen": seen, "send": send,
            "partial": partial, "line": line, "step": step, "steps": len(actions), "open": len(still)}


def sayable(verdict: dict[str, Any] | None) -> bool:
    """Whether a request's closing words may be said as written: no verdict (an older agent, a stand-in), or one at
    ``likely`` or stronger, or an answer with nothing done."""
    return not isinstance(verdict, dict) or verdict.get("level") in SAYABLE or not verdict.get("level")


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
        if str(tool_input.get("action")) == "close":
            # honest (87): a close is measured on the window (gone, hidden or cloaked), not the app: live, Slack was
            # said as "closed" while it still ran in the tray
            return f"{app}'s window is closed." if process else "The window is closed."
        state = {"minimize": "minimized", "maximize": "maximized", "fullscreen": "full screen",
                 "restore": "restored", "focus": "in front"}.get(str(tool_input.get("action")))
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
    if name == "set_slider":
        # vfix3: live 4 Oct a verified in-app volume change was said as "Done."
        where = f" in {app}" if process else ""
        to, by = tool_input.get("to"), tool_input.get("by")
        if isinstance(to, (int, float)) and not isinstance(to, bool):
            return f"Set it to {int(round(float(to)))}%{where}."
        if isinstance(by, (int, float)) and not isinstance(by, bool) and by:
            return f"Turned it {'down' if by < 0 else 'up'}{where}."
    return "Done."


def noop_line(name: str, details: dict[str, Any] | None, *, process: str | None = None) -> str | None:
    """N1 (report 47): the finish line of an action whose own report says nothing moved (``details["noop"]``: the
    level was already where it was asked to go, or at its end), in place of the line written before it ran ("Turned
    Spotify up."), or ``None`` for an action that did move. Fixed sentences over the measured numbers."""
    details = details if isinstance(details, dict) else {}
    if name == "set_slider" and details.get("level_unread") and not details.get("noop"):
        # vfix3 review F2: a shortcut whose effect showed only as the bar's look changing; the level itself was never
        # read, so no percent and no "turned it down" is said for it
        where = app_name(str(process)) if process else "the app"
        return f"I pressed {where}'s own shortcut for it and the bar changed, but I can't read the new level."
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


__all__ = ["ANSWER", "CONTRADICTED", "EXPECTED", "EXPECTED_UNVERIFIABLE", "FAILED", "LEARNED_MIN_CONFIRMED",
           "LEARNED_UNVERIFIABLE", "LEVELS", "LEVEL_LABELS", "LIKELY", "PAGE_SCROLL_UNVERIFIABLE", "SAYABLE",
           "SEND_CONFIRMED", "SEND_UNCHECKED", "SEND_UNCONFIRMED", "UNVERIFIED", "VERIFIED", "app_name", "classify", "confirm_send", "finish_line", "finish_request",
           "is_expected", "noop_line", "outcome_verdict", "sayable", "similar", "step_level"]
