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
    if level == UNVERIFIED and step.get("confirmed") and not step.get("confirmed_by_ready"):
        # backed afterwards by something independent (a later verified step in the same window); second review V2-7:
        # never by a step that only brought that window to front or opened it (``confirmed_by_ready``)
        return LIKELY
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
        if step.get("confirmed") and not step.get("confirmed_by_ready"):
            return "a later step showed it worked"
        if tool in EXIT_STATUS_TOOLS:
            return "the command ran without an error"
        return "it can't be read back from here, which is normal for it"
    if level == UNVERIFIED:
        return "nothing on screen showed whether it worked"
    return "it failed" if step.get("ok") is False else "the screen showed it didn't happen"


def _app(step: dict[str, Any]) -> str | None:
    """The app a step acted in, comparable across steps: the app its input names (the mixer's level names its app;
    the trace's ``app`` for it is only the window in front), else its window's process, casefolded without ``.exe``;
    ``None`` unknown. Plumbing."""
    raw = str((step.get("input") or {}).get("app") or step.get("app") or "").strip().casefold()
    raw = raw[:-4] if raw.endswith(".exe") else raw
    return raw or None


def readies(name: str, tool_input: dict[str, Any] | None) -> bool:
    """A step whose effect is only a window brought to front or an app opened (``focus_window``, ``launch_app``, a
    ``window_action`` focus): getting there, never another step's effect (second review V2-1, V2-7). Machine fields
    only (the tool's name and its fixed ``action``)."""
    if name in ("focus_window", "launch_app"):
        return True
    return name == "window_action" and str((tool_input or {}).get("action") or "") == "focus"


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _squashed(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "+".join(_squashed(v) for v in value)
    return " ".join(str(value).split()).casefold() if value is not None and value != "" else ""


#: The input fields that name what a step aims at, per tool that takes no element (a retry aims at the same): fixed
#: schema fields, plumbing.
_AIM_FIELDS = {"window_action": ("action",), "tab": ("action", "tab"), "hotkey": ("keys",), "press": ("key",),
               "open_url": ("url",), "launch_app": ("query",), "run_powershell": ("command",),
               "write_clipboard": ("text",), "focus_window": ()}
#: Tools that aim at an element of the window: a retry is the same element (its role and name).
_ELEMENT_TOOLS = frozenset({"click", "type_text", "hover", "scroll", "set_slider"})


def _element(step: dict[str, Any]) -> str:
    """The element a step aimed at: the loop's ``control`` (role|name), else the role and name its ``target`` starts
    with (``button "Send", window "..."``); ``""`` unknown."""
    control = _squashed(step.get("control"))
    if control.strip("|"):
        return control
    head = str(step.get("target") or "").split(", window ", 1)[0].strip()
    return "" if not head or head.startswith("window ") else _squashed(head)


def _aim(step: dict[str, Any]) -> tuple[str, ...] | None:
    """What a step aims at (second review V2-1): its tool and the operation or element it is for, or ``None`` when that
    is not known (then no other step counts as its retry)."""
    tool = str(step.get("tool") or "")
    args = step.get("input") or {}
    if tool in _ELEMENT_TOOLS:
        element = _element(step)
        if element:
            return (tool, element)
        if tool == "type_text" and _squashed(args.get("text")):
            return (tool, "text", _squashed(args.get("text")))   # typed into the focused field: the same text again
        if tool == "scroll":
            # at a point, no element named: the same way again (the signs of its fixed dy / dx fields)
            signs = tuple("+" if _number(args.get(k)) > 0 else "-" if _number(args.get(k)) < 0 else "0"
                          for k in ("dy", "dx"))
            return None if signs == ("0", "0") else (tool, *signs)
        return None
    if tool == "app_volume":
        return (tool, "mute" if isinstance(args.get("mute"), bool) else "level")   # its app is compared by _app
    if tool in _AIM_FIELDS:
        fields = tuple(_squashed(args.get(k)) for k in _AIM_FIELDS[tool])
        return None if fields and not any(fields) else (tool, *fields)
    return None


def _aims_same(step: dict[str, Any], other: dict[str, Any]) -> bool:
    """Whether ``other`` is a retry of ``step``: the same known app and the same aim (the same tool and the same
    operation or element). A focus is never a retry of a close; an unknown app is never the same app."""
    app = _app(step)
    if app is None or _app(other) != app:
        return False
    aim = _aim(step)
    return aim is not None and aim == _aim(other)


def _chat(step: dict[str, Any]) -> str | None:
    """Which chat a send went to, as far as its window's title tells: the title when it names more than the app
    (``Ana - Discord``), else ``None`` (second review V2-2, V2-3: WhatsApp's title is only ``WhatsApp``, and an unread
    count ``(3) WhatsApp`` names no chat either). Character tests on the title and the process name, plumbing."""
    title = " ".join(str(step.get("window") or "").split()).casefold()
    if not title:
        return None
    rest = title
    app = _app(step) or ""
    for part in {app, app_name(app).casefold(), *app.replace("-", ".").split(".")}:
        if len(part) >= 3:
            rest = rest.replace(part, " ")
    return title if any(ch.isalpha() for ch in rest) else None


def _same_chat(step: dict[str, Any], other: dict[str, Any]) -> bool:
    """Whether a later send is a retry of an earlier one (review F2, second review V2-2/V2-3): the same known app, the
    same chat by a window title that names it (:func:`_chat`), and with a text known for both, the same message (its
    fingerprint, ``send_sig``). The same text to two chats of one app, or any send in an app whose title never names
    the chat, stays open."""
    app = _app(step)
    if app is None or _app(other) != app:
        return False
    chat = _chat(step)
    if chat is None or chat != _chat(other):
        return False
    sig, other_sig = step.get("send_sig"), other.get("send_sig")
    if sig and other_sig:
        return sig == other_sig
    return not sig   # no text known for the earlier one: the chat is all there is to match


def _resolved(index: int, actions: list[dict[str, Any]], levels: list[str], *, finished_at: int | None) -> bool:
    """Whether a later step put an earlier non-verified one right. Facts of the trace only, and only a later step
    toward the same effect (review F1-F3, second review V2-1/V2-6): never an unrelated verified step, never a step in
    another or an unknown app.

    * a send: only a later confirmed send of the same message in the same chat (:func:`_same_chat`);
    * any other step: a later try at the same aim in the same app (:func:`_aims_same`; the retry's own level stands
      for it), or the verified action the brain marked as completing the request (``finished_at``: its index) when it
      acted in the same app (and is not a mere focus or opening, unless the step was one too), or came in the same
      response while the step is already ``likely`` (a weak check by nature). The slider-then-mixer case (a player's
      slider failed, then the mixer level for that app, marked finishing) is the same app by the mixer's own ``app``
      field. A step's own later confirmation (``confirmed``) makes it ``likely`` in :func:`step_level`, never
      ``verified``."""
    step = actions[index]
    later = range(index + 1, len(actions))
    if step.get("send"):
        return any(actions[j].get("send") == SEND_CONFIRMED and _same_chat(step, actions[j]) for j in later)
    app = _app(step)
    if finished_at is not None and finished_at > index:
        done = actions[finished_at]
        same_app = app is not None and _app(done) == app and (
            not readies(str(done.get("tool") or ""), done.get("input"))
            or readies(str(step.get("tool") or ""), step.get("input")))
        same_response = step.get("call") is not None and step.get("call") == done.get("call")
        if same_app or (same_response and levels[index] == LIKELY):
            return True
    return any(not actions[j].get("send") and _aims_same(step, actions[j]) for j in later)


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


#: small8: the ``seen`` of a request held at likely because a step of it was refused before it ran (plain words).
SEEN_REFUSED = "one step couldn't run, so not all of it was checked"

#: small8 review F1: the tools whose effect is the content or the view itself (what was typed, the keys' effect, where
#: the view went, a slider's value), by kind (``press`` and ``hotkey`` are both keys). A refused one of these is set
#: aside only when the same kind of step did work earlier in the same app: then what it aimed at was reached that way.
#: Fixed schema tool names, plumbing.
_CONTENT_KINDS = {"type_text": "type", "press": "keys", "hotkey": "keys", "scroll": "scroll", "set_slider": "slider"}


def _reached_before(index: int, actions: list[dict[str, Any]], levels: list[str]) -> bool:
    """small8 review F1: whether something of the request already worked in the app a refused step was for, before
    it: an earlier step in the same known app that ran and was not shown to fail (verified, likely, or not confirmed:
    that step's own level still bounds the request while it stays open), that did more than bring the window to front
    or open the app (:func:`readies`), and, for a refused step of a content kind (:data:`_CONTENT_KINDS`), one of that
    same kind. The live shape: a verified scroll, then refused clicks that only tried to put the caret by a line
    number. Never: a focus or an opening before a refused scroll, press or typing (that refused step was the attempt
    itself); an unknown app."""
    step = actions[index]
    app = _app(step)
    if app is None:
        return False
    kind = _CONTENT_KINDS.get(str(step.get("tool") or ""))
    for j in range(index):
        done = actions[j]
        tool = str(done.get("tool") or "")
        if (levels[j] == FAILED or done.get("refused") or _app(done) != app
                or readies(tool, done.get("input"))):
            continue
        if kind is None or _CONTENT_KINDS.get(tool) == kind:
            return True
    return False


def _set_aside_refused(still: list[int], actions: list[dict[str, Any]], levels: list[str],
                       claims: str | None) -> list[int]:
    """small8 (report 73, release 7): the open steps that were refused before anything was sent (the trace's
    ``refused``: a guard's "not clicked: nothing you can click is at ...", the loop breaker, a bad argument) and that
    the brain's own closing claim says did not matter: it classes its message as done, checked or an answer, not as
    what it tried and that failed (``claims``: judgement with worked examples in its schema, never a reading of the
    words). Live, three refused clicks that only tried to put the caret where a line number shows graded a scroll that
    worked "Didn't work".

    Never set aside: anything when the claim is ``tried`` or unknown, or every action was refused (nothing reached the
    screen); a refused step that is itself an effect the user asks for (a send, a close, a volume or window change:
    :func:`yuki.ui.step_words.is_effect`), whatever the claim; a refused step when nothing of the request worked
    before it in that app (:func:`_reached_before`, review F1: a focus, then a refused scroll, is a scroll that never
    happened, and stays failed for the claim check to ask about). The request is then held at ``likely`` at most,
    never ``verified``: what was refused did not happen."""
    if claims not in (CLAIM_DONE, CLAIM_CHECKED, CLAIM_ANSWER):
        return []
    if all(s.get("refused") for s in actions):
        return []
    from yuki.ui.step_words import is_effect

    return [i for i in still if actions[i].get("refused") and not actions[i].get("send")
            and not is_effect(str(actions[i].get("tool") or ""), actions[i].get("input"))
            and _reached_before(i, actions, levels)]


def outcome_verdict(steps: list[dict[str, Any]] | None, *, unbacked: list[str] | tuple[str, ...] = (),
                    finished_by_action: bool = False, claims: str | None = None) -> dict[str, Any]:
    """The request's one outcome verdict from its run trace (``yuki.agent.loop.Agent._trace``), used by the thinker's
    log, the voice, the done card, the island and the window's run. Data only: the steps' measured verdicts, a send's
    confirmation in memory's capture of the chat, and the claims of the closing message the claim check could not back
    (``unbacked``, its gaps after it asked once); never a reading of the words.

    The level is the weakest of the action steps no later step put right (:func:`_resolved`), and no stronger than
    ``unverified`` while a claim stays unbacked. ``claims``: the closing ``done``'s own class of its message
    (:func:`claim`), which alone may set aside steps refused before they ran (:func:`_set_aside_refused`; the request
    is then ``likely`` at most). Returns ``{"level", "label", "acted", "seen", "send", "partial",
    "line", "steps", "open"}``: ``seen`` what the deciding step's check saw (or could not), ``send`` ``confirmed`` /
    ``unconfirmed`` / ``None``, ``line`` the sentence said by code when nothing rewords an outcome below ``likely``
    (``""`` otherwise)."""
    actions = [s for s in (steps or []) if isinstance(s, dict) and s.get("kind") == "action"]
    if not actions:
        level = UNVERIFIED if unbacked else ANSWER
        return {"level": level, "label": LEVEL_LABELS[level], "acted": False,
                "seen": "what the reply says about the screen was not read in this request" if unbacked else "",
                "send": None, "partial": False, "line": LINE_UNREAD if unbacked else "", "step": "", "steps": 0,
                "open": 0, "unbacked": len(unbacked), "did": did_other(steps)}
    levels = [step_level(s) for s in actions]
    # the request ended on the action the brain marked as completing it, and that action verified (the loop finishes
    # only then): the last verified action
    finished_at = (max((i for i, lv in enumerate(levels) if lv == VERIFIED), default=None)
                   if finished_by_action else None)
    still = [i for i, lv in enumerate(levels)
             if lv != VERIFIED and not _resolved(i, actions, levels, finished_at=finished_at)]
    aside = _set_aside_refused(still, actions, levels, claims)
    still = [i for i in still if i not in aside]
    # review F2: the request's send is unconfirmed while any send is still open, not only the last one
    sends = [i for i, s in enumerate(actions) if s.get("send")]
    send = None if not sends else ("unconfirmed" if any(i in still for i in sends) else "confirmed")
    if still:
        decider = min(still, key=lambda i: (_RANK[levels[i]], -i))   # the weakest; the latest of equals
        level = levels[decider]
    else:
        decider = max(range(len(actions)), key=lambda i: (actions[i].get("send") == "confirmed", i))
        level = VERIFIED
    held_by_refused = bool(aside) and _RANK[level] > _RANK[LIKELY]
    if held_by_refused:
        level = LIKELY   # small8: what was refused did not happen, so the request is never "checked" past it
    if unbacked and _RANK[level] > _RANK[UNVERIFIED]:
        level = UNVERIFIED
        seen = "what the reply says about the screen was not read in this request"
    elif held_by_refused:
        seen = SEEN_REFUSED
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
    # second review V2-4: the effects whose own check showed them, by machine key (below likely the done card draws a
    # check mark only for these)
    from yuki.ui.step_words import effect_key

    checked = sorted({effect_key(str(actions[i].get("tool") or ""), actions[i].get("input"))
                      for i, lv in enumerate(levels) if lv == VERIFIED})
    return {"level": level, "label": LEVEL_LABELS[level], "acted": True, "seen": seen, "send": send,
            "partial": partial, "line": line, "step": step, "steps": len(actions), "open": len(still),
            "checked": checked, "unbacked": len(unbacked), "did": did_other(steps), "refused": len(aside)}


#: fix6 review F1: steps that change something without acting on the screen, beyond the to-do and memory writes
#: (:func:`yuki.ui.step_words.is_effect`): Yuki's own window brought up.
_OTHER_EFFECT_TOOLS = frozenset({"open_yuki"})


def _changed(step: dict[str, Any]) -> bool:
    """Whether one successful step that is not an on-screen action changed something. Second review V1/V2: a memory
    write's own result says whether it was kept (its payload's ``saved``, carried on the trace step by the loop): a
    write memory could not keep (memory down, nothing to cancel) changed nothing, and every write counts (an answer to
    a getting-to-know-you question, a route kept for a person), not only the ones a tool-name list knows. A step whose
    result carries no ``saved`` (a read, an older stand-in) falls back to ``yuki.ui.step_words.is_effect``."""
    saved = step.get("saved")
    if isinstance(saved, bool):
        return saved
    from yuki.ui.step_words import is_effect

    tool = str(step.get("tool") or "")
    return tool in _OTHER_EFFECT_TOOLS or is_effect(tool, step.get("input"))


def did_other(steps: list[dict[str, Any]] | None) -> bool:
    """fix6 review F1: a step that is not an on-screen action succeeded and changed something (a to-do added or ticked,
    a rule or know-how kept, a memory corrected, a watch set, a question answered, a route kept, Yuki's window opened:
    :func:`_changed`). A closing message that says it got done is then backed by that step's own result, never
    "nothing was done". Machine fields only."""
    return any(isinstance(s, dict) and s.get("kind") != "action" and s.get("ok") is True and _changed(s)
               for s in (steps or []))


def read_back(steps: list[dict[str, Any]] | None) -> bool:
    """fix6 review F3: a successful read of the window the last action acted in (the same app), made after that action:
    what the closing message says it "checked" was then looked at, even when the action's own report cannot show its
    effect (a ``likely`` step). Trace facts only."""
    steps = [s for s in (steps or []) if isinstance(s, dict)]
    last = max((i for i, s in enumerate(steps) if s.get("kind") == "action"), default=None)
    if last is None or steps[last].get("windowless"):
        # second review V6: an action with no window of its own (a shell command, a media key) has nothing to read
        # back; its step's app is only the window that was in front, so reading that is not a read-back
        return False
    app = steps[last].get("app")
    return any(s.get("kind") == "read" and s.get("ok") is True and s.get("app") == app for s in steps[last + 1:])


# ---------------------------------------------------------------------------
# What the closing message claims (fix6, report 73 release 6: "honest wording")
# ---------------------------------------------------------------------------

#: What a ``done`` message claims, as the brain itself classes it (``done.claims``; judgement with worked examples in
#: its schema, never a reading of the words here): ``checked`` (it says the result was seen or confirmed), ``done``
#: (it says something got done), ``tried`` (what was tried, and that it failed or could not be confirmed), ``answer``
#: (only what was found; nothing done). Strongest first.
CLAIM_CHECKED, CLAIM_DONE, CLAIM_TRIED, CLAIM_ANSWER = "checked", "done", "tried", "answer"
CLAIMS = (CLAIM_CHECKED, CLAIM_DONE, CLAIM_TRIED, CLAIM_ANSWER)

#: Said in place of a closing message that claims something got done (or was checked) when no action ran in the
#: request (live 5 Oct run 19: no action, yet "I opened and closed the queue, confirmed").
LINE_NOTHING_DONE = "I haven't done that: nothing was done on screen for it."
LINE_NOT_CHECKED = "I haven't checked that on screen."


def last_windowless(steps: list[dict[str, Any]] | None) -> bool:
    """Second review V6: the request's last action has no window of its own (a shell command, a media key: the loop
    marks its trace step ``windowless``). Trace facts only."""
    actions = [s for s in (steps or []) if isinstance(s, dict) and s.get("kind") == "action"]
    return bool(actions and actions[-1].get("windowless"))


def claim(value: Any) -> str | None:
    """A ``done``'s declared claim (:data:`CLAIMS`), or ``None`` when it gave none or something else."""
    word = " ".join(str(value or "").split()).casefold()
    return word if word in CLAIMS else None


def claim_gap(claims: str | None, verdict: dict[str, Any] | None, *, read: bool = False,
              looked_back: bool = False, windowless: bool = False) -> str | None:
    """What a closing message claims beyond what this request's steps showed, in words for the brain, or ``None``.

    ``claims``: the brain's own class of its message (:func:`claim`); ``verdict``: :func:`outcome_verdict` of the run
    so far (with no unbacked claims); ``read``: a read of the screen or memory ran in this request; ``looked_back``: the
    window the last action acted in was read after it (:func:`read_back`); ``windowless``: that action has no window
    of its own (a shell command: :func:`last_windowless`), so the gap never asks to read one back. Facts only:

    * nothing was done (no action, and no other step that changed something: ``did``) and it says something got done,
      or that something was tried (second review V3: with no action nothing was tried), or that it was checked with
      nothing read either;
    * it says checked or confirmed while no step's own check showed it (the level is below verified), unless the
      result is likely and the window was read back after the action (review F3: a read never raises the level, so a
      read-back is what "checked" can rest on there);
    * it says it got done while the level is below likely (not confirmed, or it did not work).

    Each gap says what clears it (review F3: never "look first" where looking cannot). ``None`` claims (none declared,
    an older brain) are not checked here; the closing text obeys the level anyway (:func:`honest_text`)."""
    if claims is None or not isinstance(verdict, dict):
        return None
    level = str(verdict.get("level") or "")
    seen = " ".join(str(verdict.get("seen") or "").split())
    because = f" ({seen})" if seen else ""
    if not verdict.get("acted"):
        if claims in (CLAIM_DONE, CLAIM_TRIED) and not verdict.get("did"):
            # review F1: never "do it now" blindly: nothing at all changed in this request, so nothing would repeat
            return (f"it says you {'did' if claims == CLAIM_DONE else 'tried'} something, but nothing in this request "
                    f"did anything (no step ran that changes something): say plainly that it was not done, or, if "
                    f"they asked for it, do it first and then finish")
        if claims == CLAIM_CHECKED and not read:
            return ("it says you checked something, but nothing was read in this request: look first and say what "
                    "it shows, or say plainly that you have not checked")
        return None
    if claims == CLAIM_CHECKED and level != VERIFIED and not (level == LIKELY and looked_back):
        fix = ("say what you did and what its output showed, without checked or confirmed" if level == LIKELY
               and windowless else
               "read the window back now and say what it shows, or say what you did without checked or confirmed"
               if level == LIKELY else "say what you did and what the check showed, never checked or confirmed")
        return (f"it says you checked or confirmed it, but no step's own check showed it: the result is "
                f"\"{str(verdict.get('label') or level).lower()}\"{because}; {fix}")
    if claims == CLAIM_DONE and level in (UNVERIFIED, FAILED):
        return (f"it says it got done, but the result is \"{str(verdict.get('label') or level).lower()}\"{because}; "
                f"say what you tried and that it {'did not work' if level == FAILED else 'could not be confirmed'}")
    return None


def honest_text(text: str, verdict: dict[str, Any] | None, claims: str | None) -> tuple[str, str | None]:
    """The closing text the user gets, obeying the request's outcome level: ``(text, why replaced or None)``.

    At ``likely`` or stronger, or an answer with nothing done and nothing unbacked, the brain's words stand. Below
    likely the user gets the verdict's own fixed line alone (second review V3: the brain labels flat success words
    "tried" too, so "I tried, but I couldn't confirm it worked. Shuffle's on now." put a false claim right after the
    line; the done card's level row already says what the check saw, and the brain's words ride on the verdict as
    ``words`` for the expanded run: :meth:`yuki.agent.loop.Agent._honest_final`). With nothing done: a message that
    claims something got done or was tried becomes :data:`LINE_NOTHING_DONE` (unless another step changed something,
    ``did``: a to-do added), one that claims a check becomes :data:`LINE_NOT_CHECKED`; any other unread answer keeps
    its words (the surfaces add "not checked" after them). Fixed sentences over measured facts and the brain's own
    class; the words are never read."""
    if not isinstance(verdict, dict) or verdict.get("level") in SAYABLE or not verdict.get("level"):
        return text, None
    if not verdict.get("acted"):
        if claims in (CLAIM_DONE, CLAIM_TRIED) and not verdict.get("did"):
            return LINE_NOTHING_DONE, "nothing_done"
        if claims == CLAIM_CHECKED:
            return LINE_NOT_CHECKED, "not_checked"
        return text, None
    line = " ".join(str(verdict.get("line") or "").split())
    if not line:
        return text, None
    return line, "below_likely_tried" if claims == CLAIM_TRIED else "below_likely"


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



# ---------------------------------------------------------------------------------------------------------------------
# Phase S (sCore): the finish gate of a pinned skill (map-skills S3.5, PLAN.md D19)
# ---------------------------------------------------------------------------------------------------------------------

#: How many times a done is sent back for a pinned skill whose own check doesn't see the result yet; then the request
#: finishes as "couldn't confirm" (never as done).
SKILL_BOUNCES = 2
#: The gap a pinned skill's unconfirmed check leaves on the outcome verdict (it caps it at "not confirmed").
SKILL_UNCONFIRMED = "the skill's own check didn't see the result"


def skill_gate(pinned: dict[str, Any] | None, *, since: float, bounces: int,
               check: Callable[..., Any] | None = None) -> tuple[str, str | None]:
    """A done on a request with a pinned skill: ``("ok", None)`` when its done-check sees the result (or the skill has
    no machine check the thinker's own steps could raise: the outcome verdict of its trace decides), ``("hold",
    note)`` to send the done back ("not done yet", at most :data:`SKILL_BOUNCES` times), ``("unconfirmed", gap)``
    after that: the request ends, said as "couldn't confirm". Data only (the probe's verdict), never the words."""
    if not isinstance(pinned, dict) or not pinned.get("key"):
        return "ok", None
    done = pinned.get("done")
    if not isinstance(done, dict) or not done.get("probe"):
        return "ok", None
    try:
        if check is None:
            from yuki.skills.donecheck import check as check_fn

            got = check_fn(done, since=float(since))
        else:
            got = check(done, since=float(since))
        verdict = getattr(got, "verdict", got)
        evidence = getattr(got, "evidence", {}) or {}
    except Exception:  # noqa: BLE001 - a check that broke couldn't read
        verdict, evidence = None, {}
    if verdict is True:
        return "ok", None
    seen = "the PC shows otherwise" if verdict is False else "the PC couldn't read it"
    status = str(evidence.get("status") or "") if isinstance(evidence, dict) else ""
    if int(bounces) < SKILL_BOUNCES:
        words = str(pinned.get("done_words") or "what they asked")
        return "hold", (f"not done yet: {seen}" + (f" ({status})" if status.replace("_", "").isalpha() else "")
                        + f". The usual way ({pinned.get('key')}) is done only when: {words}. Check the screen and "
                        f"finish it, or say plainly that it couldn't be confirmed.")
    return "unconfirmed", SKILL_UNCONFIRMED

__all__ = ["ANSWER", "CONTRADICTED", "EXPECTED", "EXPECTED_UNVERIFIABLE", "FAILED", "LEARNED_MIN_CONFIRMED",
           "LEARNED_UNVERIFIABLE", "LEVELS", "LEVEL_LABELS", "LIKELY", "PAGE_SCROLL_UNVERIFIABLE", "SAYABLE",
           "SEND_CONFIRMED", "SEND_UNCHECKED", "SEND_UNCONFIRMED", "UNVERIFIED", "VERIFIED", "app_name", "classify", "confirm_send", "finish_line", "finish_request",
           "is_expected", "noop_line", "outcome_verdict", "readies", "sayable", "similar", "step_level",
           "CLAIMS", "LINE_NOTHING_DONE", "LINE_NOT_CHECKED", "claim", "claim_gap", "did_other", "honest_text",
           "last_windowless", "read_back", "SKILL_BOUNCES", "SKILL_UNCONFIRMED", "skill_gate"]
