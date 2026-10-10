"""The talker's hands: single desktop steps the mouth does itself (docs/VOICE.md §5.6).

gradient-bang's voice LLM calls its quick tools itself and hands only
multi-step work to task agents (docs/research/voice-parallel.md §1). Yuki's
mouth gets the same: a few one-step desktop tools (a key or shortcut in one
window, a media or volume key, scrolling, a window's state, one tab of a
window's tab strip, opening a URL or an app), each backed by the existing
:mod:`yuki.actions` code through the brain's own backend, with the brain's
guards: keys and scrolling go to the named window only while it is in front
(``expect_hwnd``); and only a window (or tab) the mouth was shown in this
turn's desktop summary can be acted on (a hand never guesses).

**Instant hands act at once (v3.3).** Live on 2026-09-25 the reply's words
streamed first and its tool call came after them, so "scroll this window" was
heard in full ("okay, I'm scrolling this window...") before anything moved. An
instant hand (``press_keys``, ``media_key``, ``scroll``, ``window``, ``tab``) is
now called with no words before it: the reply is the tool call alone, and the
action runs the moment the call is complete. What is heard is the hand's done
line (``say_when_done``, written with the call), said by code the moment the
hand's own report *verifies* the effect, with no second model call. The slow
hands (``open_url``, ``open_app``: seconds) and hand-offs to the thinker keep a
few words first.

**"Done" only from a verified effect (v3.3).** Live, "done" was said three times
for effects that had not happened (a window focused for a tab switch; two
scrolls whose wheel went to the tab bar; YouTube "up" before it had loaded).
Each hand now reports ``verified`` True / False / None from what it read back:
a window's state, the selected tab, the scroll position, the media session's
status, the output volume, the page's address *and* title. The done line is
said only on True; otherwise the report goes to the mouth, which says honestly
what happened and hands it to the thinker (``press_keys`` always reports back:
a key may be meant for anything, so it has no verdict).

**What the hands did is shared (v3.3).** Live, the hands opened a YouTube tab and
the thinker, asked for "the new tab we just opened", clicked another one. Every
hand action and its verified report, and every desktop action of the thinker's,
goes into :class:`ActionLog`; its lines are in the mouth's desktop block and in
every request handed to the thinker (:func:`yuki.voice.mouth.brain_request`).
An opened page records which tab it is (place, name, window).

**No blind keys, one actor, stop on a wrong effect (v3.5).** Live 2026-09-26 21:25-21:31 the
talker pressed Ctrl+W to close "the Claude tabs" on whatever tab was showing, closed the
user's video three times and Gmail once, "undid" with Ctrl+Shift+T into the wrong tabs, and
repeated, while a thinker job worked in the same Chrome window. Now: the talker has no
free keyboard hand; a tab is closed only through the ``tab`` hand, by the identity of the
tab it was shown (UIA RuntimeId), verified gone; a closed tab comes back only through the
browser's own reopen, verified by title against the tab that was closed; after a hand whose
report does not show the effect, the talker's hands stop until the user speaks again; and
while a thinker job runs on the worker (which has hands), only the audio hands (media keys,
an app's volume) act, since they touch no window.

The desktop summary (:meth:`Hands.snapshot`) is the brain's own overview
(``format_overview``) plus the media sessions, the foreground window's tabs and
the action log, captured off the event loop for every mouth call and put in the
volatile block after every cache breakpoint. Sizes and times go to the voice log
(content-free).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from types import SimpleNamespace
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from yuki.ui.own_window import PAGES as YUKI_PAGES

#: Media and volume keys (pyautogui names, as :func:`yuki.actions.input.normalize_key` takes them).
MEDIA_KEYS = ("playpause", "nexttrack", "prevtrack", "stop", "volumeup", "volumedown", "volumemute")
#: The media keys whose effect the media session shows (its status), watched after sending.
_TRANSPORT_KEYS = ("playpause", "stop", "nexttrack", "prevtrack")
#: The keys whose effect the output volume shows.
_VOLUME_KEYS = ("volumeup", "volumedown", "volumemute")
_WATCH_S = 0.8
_POLL_S = 0.05
#: How long an opened page may take to get its title (the page is at the address before it has one).
_TITLE_WAIT_S = 6.0
#: The foreground window's tabs are read within this when the summary needs them now.
_TABS_BUDGET_S = 0.15
#: A tab strip read ahead (when the user starts talking) serves a summary for this long, while the
#: window's title is the same and nothing was done on the desktop since.
_TABS_FRESH_S = 20.0
#: How many action-log lines the mouth's desktop block carries (the thinker gets more).
_MOUTH_ACTION_LINES = 4
#: A summary read while the user was talking serves the reply if it is this recent and nothing was done on the
#: desktop since (v3.7: waiting for a fresh one cost the reply 66 ms p50, 190 ms p90 live) ...
_DESK_FRESH_S = 1.5
#: ... and one is read ahead at most this often while they talk.
_DESK_AHEAD_EVERY_S = 0.5

#: Hands that act at once, with no words before them.
INSTANT_HANDS = ("media_key", "app_volume", "scroll", "window", "tab", "click", "type", "open_yuki")
#: Hands that touch no window (sound only): they may act while the thinker works on the desktop.
AUDIO_HANDS = ("media_key", "app_volume")
#: How long a reopened tab may take to show its title (the browser restores it at once, the page after).
_REOPEN_WAIT_S = 3.0
#: What the talker reads when Yuki's own authorization check held a hand back (v3.18, revamp D S1): nothing was done;
#: the check's one plain question is asked, and the hand runs only after a yes.
_JUDGE_HELD_NOTE = (
    "Not done: Yuki's own check held it back, because their words did not plainly ask for this. Nothing was done. Ask "
    "them in a few words, the question alone with no call: {question} If they say yes, call it again."
)
#: Review r2 L3: a volume skill run on unsure words (never held) that would need a window for the rest.
_UNSURE_FOCUS_NOTE = (
    "Not done: recognition was unsure of their words, and the rest of this would take a window, so nothing was done. "
    "Ask them in a few words whether they meant it, the question alone with no call. If they say yes, call it again."
)
#: What the talker reads when the app's "Act" switch is off in Settings (v3.18, 34-review H5).
_ACT_OFF_NOTE = (
    "Not done: in Yuki's settings Act is switched off for {app}, so Yuki does not click, type or press keys there; "
    "nothing was done and the thinker will not do it either. Say so in a few words (they can switch it on in "
    "Settings); no other hand, no hand-off."
)

_WINDOW = {"type": "integer", "description": "The window's [hwnd] from the desktop block."}
_SAID_NOTE = "It worked, verified, and your done line was said. Do not say it again."
#: A verified hand whose done line was left empty (v3.12: "on every scroll it says done, it's very repetitive").
_SILENT_NOTE = "It worked, verified, and, as you chose, nothing was said: they can see it. Say nothing now."
_UNCONFIRMED_NOTE = (
    "The report does not show that it took effect. Say so honestly in a few words from the report, never "
    "that it is done. If the report says why nothing more can happen (already at the end, already showing), "
    "say that and stop. Otherwise hand it to the thinker in this same reply: tell_yuki with the user's "
    "request as they asked it and this report as context."
)
_FAILED_NOTE = (
    "It did not work. Say so in a few words and hand it to the thinker in this same reply: tell_yuki "
    "with the user's request as they asked it and this report as context. If the report says why nothing "
    "more can happen (already at the end), say that and stop."
)
#: N1 (report 47): a level asked to move that cannot (already at its top or bottom, or already there). Live 3 Oct
#: "increase the Spotify volume" at 100% was said as "Turned it up" twice while nothing changed.
_NOOP_NOTE = (
    "Nothing changed: it was already there, as the report says. Say that in a few words, never that you turned it up "
    "or down, and offer the volume that may be the low one (the app's own player volume or the system volume, as the "
    "report names them). No other hand and no hand-off unless they say which."
)
#: What the talker reads when an app was handed to its launcher and its own window is not up yet (appOpen, 5 Oct).
_STARTING_NOTE = (
    "It is starting: its launcher took it and its own window is not up yet (a game takes a minute or two). Say that "
    "in a few words from the report (\"It's starting, give it a minute.\"), naming what is in front instead when the "
    "report says so; never say it is open. Do not hand it over and do not open it again: Yuki watches for its window, "
    "and your desktop block shows it once it is up."
)
#: V6 (3 Oct): a hand whose effect cannot be read back by the nature of its target (a web page that does not report
#: its scroll): done, nothing said, nothing handed over.
_EXPECTED_NOTE = (
    "It was done; this page does not report its scroll position, so it cannot be checked from here (normal for this "
    "page, not a failure). Say nothing now. If they say it did not move, hand it to the thinker."
)
#: What the talker reads when a hand's try did not take effect and code handed it to the thinker (v3.9).
_PASSED_NOTE = (
    "It did not take effect, so it was handed to the thinker with this report and the window already read; "
    "the thinker is doing it now and its outcome comes back to you. Say nothing now: no retry, no other hand, no "
    "\"let me try another way\"."
)
#: The same, when nothing has been said for their words yet (v3.14: offline 29 Sept a click refused on Warp was passed on
#: by code and they heard nothing for 12 s): two or three words now, so they know Yuki is on it.
_PASSED_ACK_NOTE = (
    "It did not take effect, so it was handed to the thinker with this report and the window already read; its outcome "
    "comes back to you. They have heard nothing yet for their words: say two or three words that you are on it (\"On "
    "it.\", \"One sec.\"), nothing about what did not work, never that it is done; no retry, no other hand."
)
#: What the talker reads when the same step was called again while it ran, or an opening it just did (v3.10:
#: live 2026-09-28 09:07:13 two open_url calls for one "open YouTube" said "Opening YouTube. Opening YouTube now.
#: YouTube's up. YouTube's up.").
_DUPLICATE_NOTE = (
    "This exact step was already running or was just done for these words, so it was not done twice and nothing "
    "more was said. Say nothing about it now."
)
#: A slow hand's opening (open_url, open_app) called again this soon after it was verified is the same request.
_OPEN_REPEAT_S = 12.0
#: A done line a hand already said this recently, word for word, is not said again (v3.14: live 27-29 Sept "Done." x5
#: in 38 s): the effect was verified and they watch it happen; the words' identity, never their meaning.
_REPEAT_LINE_S = 60.0
_KEYS_NOTE = (
    "The keys went out; this is what the report saw. Say in a few words what it shows happened, never "
    "more than it shows. If it shows the keys did nothing they wanted, say so and hand it to the thinker "
    "with tell_yuki (their request, and this report as context)."
)
#: Every hand's done line: said the moment its report verifies the step, with no second model call. v3.14b: the
#: schemas are the hands' contracts only; when to leave it empty and every worked example are in the prompt's "# Your
#: hands" (the same words in nine schemas were ~1.3k tokens of the talker's prefix, measured +250-300 ms of first token
#: once the whole prompt passed ~18k tokens).
_DONE_LINE = {
    "type": "string",
    "description": "Said once its report verifies it (\"Done, it's minimized.\"), or \"\" to say nothing.",
}
_NOW = " Instant: call it alone, no words before it."
#: The spoken acknowledgement of a slow hand (open_url, open_app), said the moment it is written (v3.5).
_SAY_FIRST = {
    "type": "string",
    "description": "Written first: the few words said as it starts (\"Opening it.\"). Call it alone.",
}
#: fx2: the rest of a request an opening hand does not do. "Open Chrome and restore the pages." went 0 for 5 in 3b's
#: replays (29-build §4), and 0 for 5 at 756ba4a with no learned way (37-build-fx2 §3): the talker opened Chrome alone,
#: and a verified hand ends the talker's turn (its done line, or silence, and no second call), so the second part was
#: never done by anyone. The talker says what is left (its judgement, as it judges say_when_done); with it, the
#: opening's verified result goes back to the talker with the screen as it is once it settled, and the talker does the
#: rest with one hand or hands it over.
OPEN_HANDS = ("open_app", "open_url")
_THEN = {
    "type": "string",
    "description": "What else their request asks once it is open, in their words (\"restore the pages\"), else \"\". "
                   "Then you get the new screen to do it.",
}
#: What the talker reads when an opening with a rest verified (fx2).
_THEN_NOTE = (
    "It is open, verified, and nothing was said for it. Still to do from their request: \"{rest}\". Your desktop block "
    "now shows the screen as it is: when one of your hands does that on what it shows (an item of a popup or of the "
    "window in front, a tab, a learned way's remaining steps), do it now, alone, with say_when_done for the whole; "
    "otherwise hand it to the thinker now with tell_yuki (screen true, that part as they said it, and in context what "
    "you opened and what the block shows now for it, or that it shows nothing for it). Never say again that it is "
    "open, never say it is done before a hand verified it."
)
#: After an opening with a rest, the screen is read again once it has settled: its windows, the window in front and
#: the popups unchanged for SETTLE_QUIET_S, at most SETTLE_MAX_S (a launch is verified once its window takes input; a
#: crash bubble or a page's dialog can come a moment later). Polled every SETTLE_POLL_S. Not measured live yet: every
#: settle is logged (``hands op=open_settle``: ms, changes, popups) for that.
SETTLE_QUIET_S = 0.6
SETTLE_MAX_S = 2.5
SETTLE_POLL_S = 0.15

#: The hands' schemas (Pipecat standard function schema fields).
HAND_TOOLS: list[dict[str, Any]] = [
    {
        "name": "media_key",
        "description": (
            "Your hand: a media key, or the SYSTEM volume (every app at once, 2% per press). playpause, nexttrack, "
            "prevtrack and stop act on the media session that is playing, else the current one (through Windows' "
            "media controls; never a game's). One app's volume is app_volume. "
            "Verified from the session's status or the volume." + _NOW
        ),
        "properties": {
            "key": {"type": "string", "enum": list(MEDIA_KEYS)},
            "want": {"type": "string", "enum": ["playing", "paused"],
                     "description": "playpause: the state they want; already that way, nothing is pressed."},
            "times": {"type": "integer", "description": "Presses (default 1)."},
            "say_when_done": _DONE_LINE,
        },
        "required": ["key", "say_when_done"],
    },
    {
        "name": "app_volume",
        "description": (
            "Your hand: ONE APP's own mixer volume (that app only: Chrome's video, Spotify, your own voice), the app "
            "as the app volumes line names it; nothing on the screen is touched. Verified by reading its level back. "
            "Never for a player's in-app volume (its slider in the window): that is the thinker's, and this is no "
            "stand-in for it." + _NOW
        ),
        "properties": {
            "app": {"type": "string", "description": "As the app volumes line names it, e.g. chrome.exe."},
            "change": {"type": "integer", "description": "Points to move it by, e.g. -20."},
            "level": {"type": "integer", "description": "Or the level, 0-100."},
            "mute": {"type": "boolean", "description": "true mutes, false unmutes."},
            "say_when_done": _DONE_LINE,
        },
        "required": ["app", "say_when_done"],
    },
    {
        "name": "scroll",
        "description": (
            "Your hand: scroll one window's page or list up or down (brought to the front first), read back: "
            "verified only if it moved." + _NOW
        ),
        "properties": {
            "window": _WINDOW,
            "direction": {"type": "string", "enum": ["up", "down"]},
            "amount": {"type": "integer", "description": "Notches (default 5; a screenful is about 10)."},
            "keep": {"type": "string", "enum": ["start", "stop"], "description": (
                "start: go on scrolling in steps by itself until they say stop; stop: end that.")},
            "pace": {"type": "string", "enum": ["slow", "normal", "fast"], "description": "With keep start."},
            "say_when_done": _DONE_LINE,
        },
        "required": ["window", "direction", "say_when_done"],
    },
    {
        "name": "window",
        "description": (
            "Your hand: focus, minimize, maximize, fullscreen, restore or close one whole window, its state read back "
            "from Windows. Maximized keeps the title bar and taskbar; full screen is fullscreen (F11), never "
            "maximize. A window is not a tab. close only when they asked to close that window." + _NOW
        ),
        "properties": {
            "window": _WINDOW,
            "action": {"type": "string", "enum": ["focus", "minimize", "maximize", "fullscreen", "restore", "close"]},
            "named": {"type": "string", "description": "close: their own words asking to close it."},
            "say_when_done": _DONE_LINE,
        },
        "required": ["window", "action", "say_when_done"],
    },
    {
        "name": "tab",
        "description": (
            "Your hand for the tabs line. show: make one tab the one showing. close: exactly the tabs you name by "
            "number, each found by its identity and verified gone. reopen: the tab most recently closed, verified "
            "by expect. What a page holds is the thinker's." + _NOW
        ),
        "properties": {
            "window": _WINDOW,
            "action": {"type": "string", "enum": ["show", "close", "reopen"], "description": "Default show."},
            "tab": {"type": "integer", "description": "Its number on the tabs line."},
            "tabs": {"type": "array", "items": {"type": "integer"}, "description": "close: several at once."},
            "named": {"type": "string", "description": "close: their own words naming each tab."},
            "expect": {"type": "string", "description": "reopen: the closed tab's title."},
            "say_when_done": _DONE_LINE,
        },
        "required": ["window", "say_when_done"],
    },
    {
        "name": "open_url",
        "description": (
            "Your hand: open an address in the app they named or their default browser (a new tab of its own: never "
            "press a new-tab key first), or a Windows address (ms-settings:bluetooth). Verified once at the address "
            "and titled, or its window in front (seconds)."
        ),
        "properties": {
            "say_first": _SAY_FIRST,
            "url": {"type": "string", "description": "The full address."},
            "app": {"type": "string", "description": "Optional: the app, as the Start menu names it."},
            "then": _THEN,
            "say_when_done": _DONE_LINE,
        },
        "required": ["say_first", "url", "then", "say_when_done"],
    },
    {
        "name": "open_app",
        "description": "Your hand: open or bring up an app by its Start menu name. Verified once its window is in front.",
        "properties": {"say_first": _SAY_FIRST,
                       "name": {"type": "string", "description": "The app's name as they said it."},
                       "then": _THEN,
                       "say_when_done": _DONE_LINE},
        "required": ["say_first", "name", "then", "say_when_done"],
    },
    {
        "name": "click",
        "description": (
            "Your hand: click one item of the items line by its number there. Only an item on that line: a name, a "
            "tab or a title is not one (hand those over). Refused if the window changed since. Verified when the "
            "page, focus or window visibly changes." + _NOW
        ),
        "properties": {
            "window": _WINDOW,
            "item": {"type": "integer", "description": "Its number on the items line."},
            "named": {"type": "string", "description": "Their own words naming it."},
            "say_when_done": _DONE_LINE,
        },
        "required": ["window", "item", "say_when_done"],
    },
    {
        "name": "type",
        "description": (
            "Your hand: type exactly their words into the field the focus line names, nothing pressed after (Enter "
            "and shortcuts are the thinker's). Verified by reading the field back." + _NOW
        ),
        "properties": {
            "window": _WINDOW,
            "text": {"type": "string", "description": "The text, as they said it."},
            "say_when_done": _DONE_LINE,
        },
        "required": ["window", "text", "say_when_done"],
    },
    {
        "name": "open_yuki",
        "description": "Your hand: open your own window (Yuki's) in front, on one page. Verified once it is open." + _NOW,
        "properties": {"page": {"type": "string", "enum": list(YUKI_PAGES)}, "say_when_done": _DONE_LINE},
        "required": ["page", "say_when_done"],
    },
]
# v3.18: a learned way from practice memory, run as the talker's hand (yuki.voice.skills; offered on the volatile
# block's "Learned ways" line only when it qualifies, refused for any way that was not offered).
from yuki.voice.skills import RUN_SKILL_TOOL  # noqa: E402

HAND_TOOLS.append(RUN_SKILL_TOOL)
HAND_NAMES = tuple(t["name"] for t in HAND_TOOLS)
#: The hand that runs a learned way (it may take seconds: a say_first, like the openings). Phase S: with the skill path
#: on (a Skills menu in this conversation) the same name runs one of the user's skills instead
#: (:meth:`Hands._skill_hand`, ``yuki.skills.engine``), its schema built from the menu's keys
#: (:func:`yuki.skills.menu.talker_tool`).
SKILL_HAND = "run_skill"
#: Phase S (map-skills S4.2): the talker's answer to "Keep this as your usual?" (asked by code after a skill's done
#: line), bound to the conversation's one pending question: no ids pass through the model.
KEEP_USUAL_HAND = "keep_usual"
KEEP_USUAL_TOOL: dict[str, Any] = {
    "name": KEEP_USUAL_HAND,
    "description": "Their answer to \"Keep this as your usual?\"." + _NOW,
    "properties": {"answer": {"type": "string", "enum": ["yes", "just_this_time"]}},
    "required": ["answer"],
}
#: The talker's skills-now line's cap (PLAN.md B.2: ``skills_now`` <= 300 characters, after every cache breakpoint).
SKILLS_NOW_CHARS = 300
from yuki.skills.prefs import CORRECTION_S as _CORRECTION_S, KEEP_PENDING_S as _KEEP_PENDING_S  # noqa: E402
#: The skill that takes over every volume request (map-skills S5): with it on the menu the talker's own app_volume hand
#: and the media_key volume keys are off (back with ``skills = false``); media_key keeps playpause, next, previous, stop.
VOLUME_SKILL = "volume"
#: media_key with the volume keys gone (its transport goes through the media session first, fGate): the volume is the
#: volume skill's. The session wording is fWire's (review r2 L4), so the two descriptions agree after the merge.
MEDIA_KEY_TRANSPORT_DESCRIPTION = (
    "Your hand: a media key (playpause, nexttrack, prevtrack, stop) for the media session that is playing, else the "
    "current one (through Windows' media controls; never a game's). Volume is run_skill volume. Verified from the "
    "session's status." + _NOW
)


def talker_hand_tools(skill_keys: Any = (), *, learned_ways: bool = False) -> list[dict[str, Any]]:
    """The talker's hands for one conversation (fixed for it: the tools head the cached prefix).

    ``skill_keys``: the keys of this conversation's Skills menu (``yuki.skills``; empty: the skill path is off or no
    skill loaded). With keys, ``run_skill`` is the skill hand (its enum the keys) plus ``keep_usual``; with ``volume``
    among them, ``app_volume`` and the media_key volume keys are left out. Without keys the hands are as before phase
    S, the step-list ``run_skill`` only with ``learned_ways`` (the old learning path, ``skills = false``)."""
    keys = [str(k) for k in (skill_keys or ()) if str(k or "").strip()]
    volume = VOLUME_SKILL in keys
    out: list[dict[str, Any]] = []
    for tool in HAND_TOOLS:
        name = tool["name"]
        if name == SKILL_HAND:
            continue
        if volume and name == "app_volume":
            continue
        if volume and name == "media_key":
            props = dict(tool["properties"])
            props["key"] = {**props["key"], "enum": [k for k in MEDIA_KEYS if k not in _VOLUME_KEYS]}
            tool = {**tool, "description": MEDIA_KEY_TRANSPORT_DESCRIPTION, "properties": props}
        out.append(tool)
    if keys:
        from yuki.skills.menu import talker_tool

        out.append(talker_tool(keys))
        out.append(KEEP_USUAL_TOOL)
    elif learned_ways:
        out.append(RUN_SKILL_TOOL)
    return out
#: UI Automation control types (the platform's fixed vocabulary) whose click can send, delete, buy or sign out: such a
#: click always passes Yuki's own authorization check (34-review H1/H2: a machine fact, never the control's words).
#: Links in a page (a video, a chat in a list) stay fast, as on the thinker's side (34-review H2): only controls that
#: commit something - buttons and menu items - are judged.
RISKY_CLICK_ROLES = frozenset({"Button", "SplitButton", "MenuItem"})

#: The items line (v3.14): at most this many items of the window in front, each name cut to this many characters.
ITEMS_CAP = 20
ITEM_NAME_CHARS = 60
#: A click on an item of a read at most this old, with the window's title and the action log unchanged, goes by that
#: read; an older one is found again in a fresh read by its role and name, or refused.
ITEMS_FRESH_S = 4.0
#: The user starting to talk reads the items again when the last read is older than this.
ITEMS_AHEAD_S = 3.0
#: A window whose own tree read took at least this long is heavy (v3.15, measured): its names and items then come
#: from memory's Now picture (the watcher already reads the window in front on every settle), not from a second
#: read here. Live 2026-09-30 the talker read Discord 53 times in 15 minutes, ~730 ms each, one of them 6.4 s.
HEAVY_READ_MS = 500.0
#: D7 (v3.17): memory's Now picture keeps at most this many control names of a window (``yuki.memory.now``'s
#: ``CONTROLS_KEPT``), in the order the tree has them (the window's own bar first). An entry with fewer is the whole
#: window, and serves the items line with no read here; one at the cap may have lost the page's own items, so a light
#: window is read here as before (a heavy one still comes from Now).
NOW_CONTROLS_CAP = 30
#: F1 (v3.17): popups (a "Restore pages?" bubble, a toast, a dialog) and other visible windows from memory's Now
#: picture go on the items line too, so the talker's click hand reaches them: at most this many popups and other
#: windows, and this many items each.
EXTRA_POPUPS = 3
EXTRA_WINDOWS = 2
EXTRA_ITEMS = 8
#: A window not in front is listed only when at least this share of it can be seen (the Now layout's own measure).
EXTRA_MIN_SHARE = 0.25


def _is_real_backend(backend: Any) -> bool:
    """Whether ``backend`` is the real desktop's (only then does memory's Now picture describe what it acts on)."""
    try:
        from yuki.agent.tools import _LazyRealBackend
    except Exception:  # noqa: BLE001
        return False
    inner = getattr(backend, "_inner", None) if isinstance(backend, _AppVolumeBackend) else backend
    return isinstance(inner, _LazyRealBackend)


def _window_shown(hwnd: Any) -> bool:
    """Whether a window still exists and is shown (two OS facts; a popup the board saw may be gone)."""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        handle = int(hwnd or 0)
        return bool(handle and user32.IsWindow(handle) and user32.IsWindowVisible(handle))
    except Exception:  # noqa: BLE001
        return False


def real_now_board(wait_s: float = 0.05) -> dict[str, Any] | None:
    """Memory's Now picture of the real screen (``yuki.memory.now``), popups no longer shown left out; ``None`` without
    a service or an answer within ``wait_s`` (p50 2-5 ms measured)."""
    try:
        from yuki.memory.now import shared_client

        client = shared_client()
        board = client.now(wait_s=wait_s) if client is not None else None
    except Exception:  # noqa: BLE001 - then the windows are read here
        return None
    if board and board.get("popups"):
        board = dict(board, popups=[p for p in board["popups"] if isinstance(p, dict) and _window_shown(p.get("hwnd"))])
    return board


def now_source_for(backend: Any) -> Any:
    """Where the hands take memory's Now picture from: the backend's own (a test desk's ``now_board``), memory's service
    for the real desktop, else nowhere (a stand-in desk never reaches the real screen's picture, rule 7)."""
    own = getattr(backend, "now_board", None)
    if callable(own):
        return own
    if _is_real_backend(backend):
        return real_now_board
    return None


@dataclass
class ScreenItems:
    """One read of the window in front's clickable items (content: RAM only).

    ``read`` is the hands dispatcher's own read number for that tree (its element ids are only good for it);
    ``items`` are ``{"id", "role", "name"}`` in the order shown (page content first, then the window's own).
    """

    hwnd: int
    title: str
    read: int
    items: list[dict[str, Any]]
    at: float
    version: int


def items_from_tree(tree: Any, cap: int = ITEMS_CAP) -> list[dict[str, Any]]:
    """The named interactive items of a window tree, page content first, each (role, name) once.

    By structure only (yuki.voice.screennames: UI Automation control types are the platform's fixed vocabulary, and
    whether an element sits inside a page Document is where it is), never by what the names say.
    """
    from yuki.voice.screennames import ITEM_ROLES

    elements = list((tree.get("elements") if isinstance(tree, dict) else getattr(tree, "elements", None)) or [])

    def get(e: Any, key: str, default: Any = None) -> Any:
        return e.get(key, default) if isinstance(e, dict) else getattr(e, key, default)

    by_id = {get(e, "id"): e for e in elements}

    def in_page(e: Any) -> bool:
        hops = 0
        parent = get(e, "parent", -1)
        while parent not in (None, -1) and hops < 64:
            up = by_id.get(parent)
            if up is None:
                return False
            if str(get(up, "role", "")) == "Document":
                return True
            parent, hops = get(up, "parent", -1), hops + 1
        return False

    picked = [e for e in elements if get(e, "is_interactive", True) and str(get(e, "role", "")) in ITEM_ROLES
              and " ".join(str(get(e, "name", "") or "").split()) and isinstance(get(e, "id"), int)]
    ordered = [e for e in picked if in_page(e)] + [e for e in picked if not in_page(e)]
    out: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for e in ordered:
        name = " ".join(str(get(e, "name", "")).split())
        key = (str(get(e, "role", "")), name.casefold())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append({"id": int(get(e, "id")), "role": key[0], "name": name})
        if len(out) >= cap:
            break
    return out


#: How the items line says a control type (the platform's fixed vocabulary, in words).
_ROLE_WORDS = {"Hyperlink": "link", "Button": "button", "ListItem": "item", "DataItem": "row", "TreeItem": "item",
               "TabItem": "tab", "MenuItem": "menu item"}


def items_line(items: ScreenItems, *, popup: str | None = None) -> str:
    """The desktop block's items line (``""`` without items). ``popup``: the window in front is a popup of that app (no
    window of the list), said so, so its own [hwnd] is the one a click takes (v3.17, F1)."""
    if not items.items:
        return ""
    parts = []
    for n, item in enumerate(items.items, 1):
        name = item["name"] if len(item["name"]) <= ITEM_NAME_CHARS else item["name"][:ITEM_NAME_CHARS - 1] + "…"
        parts.append(f'{n} "{name}" {_ROLE_WORDS.get(item["role"], item["role"].casefold())}'.rstrip())
    if popup is not None:
        return (f"popup [{items.hwnd}] {popup} in front (it has the keyboard; no window of the list), numbered for your "
                f"click hand, click it with window {items.hwnd}: " + "; ".join(parts))
    return (f"items on the screen in [{items.hwnd}], numbered for your click hand (page content first): "
            + "; ".join(parts))


@dataclass
class Snapshot:
    """One desktop summary for the mouth: the text, the windows and tabs it names, and its cost."""

    text: str = ""
    hwnds: set[int] = field(default_factory=set)
    foreground: int | None = None
    cursor: tuple[int, int] | None = None
    bounds: dict[int, tuple[int, int, int, int]] = field(default_factory=dict)
    #: The tabs shown: ``{hwnd: [name of tab 1, name of tab 2, ...]}``.
    tabs: dict[int, list[str]] = field(default_factory=dict)
    #: Their identities (UIA RuntimeId text; "" when the provider gives none), in the same order.
    tab_ids: dict[int, list[str]] = field(default_factory=dict)
    tabs_ms: float | None = None
    at: float = 0.0
    ms: float = 0.0
    error: str | None = None
    #: The action log's version when it was read (anything done on the desktop since makes it stale).
    version: int = -1
    #: v3.18: each window's process (``{hwnd: process name}``): the apps in play for practice memory, and a hand's
    #: trace names the app it acted in.
    apps: dict[int, str] = field(default_factory=dict)
    #: v3.18: the overview as read (the dispatcher's text and payload) and memory's Now picture of the same moment,
    #: kept for the thinker's request pack (read while the user talked; RAM only).
    overview_text: str = ""
    overview: dict[str, Any] | None = None
    board: dict[str, Any] | None = None
    #: Wall-clock time it was read (``at`` is monotonic).
    wall: float = 0.0


def media_line(facts: dict[str, Any]) -> str:
    """The media sessions as one line of facts (``""`` when there are none)."""
    parts = []
    for s in facts.get("media") or []:
        if not isinstance(s, dict):
            continue
        what = " by ".join(x for x in (f"\"{s.get('title')}\"" if s.get("title") else "", s.get("artist") or "") if x)
        app = str(s.get("app_id") or "?").split("!")[-1]
        parts.append(f"{app}{' ' + what if what else ''} {s.get('status') or 'unknown'}"
                     + (" (media keys act on this one)" if s.get("current") else ""))
    return ("media: " + "; ".join(parts)) if parts else ""


#: How the app volumes line marks Yuki's own voice (v3.18).
OWN_VOICE = "(your own voice)"


def audio_line_own(apps: list[dict[str, Any]]) -> str:
    """The app volumes line (``yuki.actions.app_volume.audio_line``'s facts) with Yuki's own voice marked: an entry
    whose ``own`` is true is this process's own audio session (an identity of processes, never a name read)."""
    if not apps:
        return ""
    parts = [f"{a['app']} {float(a.get('level') or 0):g}%{' muted' if a.get('muted') else ''}"
             f"{' playing' if a.get('playing') else ''}{' ' + OWN_VOICE if a.get('own') else ''}"
             for a in apps if isinstance(a, dict) and a.get("app")]
    return ("app volumes (Windows volume mixer, each app's own level): " + "; ".join(parts)) if parts else ""


@dataclass
class ActionRecord:
    """One desktop action done in this conversation, and what was verified about it (content: RAM only)."""

    at: float
    who: str        # "your hand" | "the thinker"
    what: str       # the action, e.g. "open_url https://www.youtube.com in Google Chrome"
    result: str     # its report
    verified: bool | None
    ok: bool

    def line(self, limit: int = 320) -> str:
        mark = "verified" if self.verified is True else ("failed" if not self.ok else "not verified")
        text = f"{datetime.fromtimestamp(self.at):%H:%M:%S} {self.who}: {self.what} -> {mark}: {self.result}"
        return text if len(text) <= limit else text[: limit - 1] + "…"


class ActionLog:
    """What was done on the desktop in this conversation, newest last (the hands' and the thinker's).

    Shared context, not a transcript: the mouth sees it in its desktop block, and the
    thinker is handed it with every voice request, so "the tab we just opened" has a
    referent. In RAM only (it holds titles and addresses: content), never in the
    voice log.
    """

    def __init__(self, keep: int = 12, max_age_s: float = 900.0) -> None:
        self._items: deque[ActionRecord] = deque(maxlen=keep)
        self._lock = threading.Lock()
        self.max_age_s = max_age_s
        #: Bumped on every action (what was read of the desktop before it may be out of date).
        self.version = 0

    def add(self, who: str, what: str, result: str, *, ok: bool, verified: bool | None) -> None:
        with self._lock:
            self._items.append(ActionRecord(time.time(), who, what, " ".join(str(result).split()), verified, ok))
            self.version += 1

    def lines(self, count: int = 6, limit: int = 320) -> list[str]:
        now = time.time()
        with self._lock:
            items = [r for r in self._items if now - r.at <= self.max_age_s][-count:]
        return [r.line(limit) for r in items]

    def text(self, count: int = 6) -> str:
        lines = self.lines(count)
        return "\n".join(lines)


class Hands:
    """The mouth's desktop tools over a backend (the brain's), with the summary they act on.

    Args:
        backend: A :class:`yuki.agent.tools.Backend` (the real desktop, or a stub in tests).
        log: The voice log (content-free ``event``), or ``None``.
        actions: The conversation's shared :class:`ActionLog` (a new one when ``None``).
    """

    def __init__(self, backend: Any, log: Any = None, actions: ActionLog | None = None) -> None:
        from yuki.agent.tools import Dispatcher

        self.backend = backend
        self.log = log
        self.actions = actions if actions is not None else ActionLog()
        # One tree read of the window in front serves the names line and the items line (v3.14); its element ids
        # are this dispatcher's own, so a click by item resolves against the very read the talker was shown.
        # appPerception (report 58): the voice's quick hands keep their speed; the before/after account of an action
        # is the thinker's (turn it on here with verify_outcomes=True once measured live)
        self._dispatcher = Dispatcher(backend, max_tree_elements=300, tree_timeout_s=0.8, verify_outcomes=False)
        from yuki.voice.workers import shared

        #: Where the background reads run (yuki.voice.workers): started here, off every loop.
        self._workers = shared()
        #: The window in front's clickable items (``ScreenItems``, v3.14), read with the names; ``None`` before.
        self.items: ScreenItems | None = None
        #: F1 (v3.17): the items of popups and other visible windows the last summary listed, by hwnd (from memory's
        #: Now picture: no element ids, a click finds its item in a read of its own).
        self.extra_items: dict[int, ScreenItems] = {}
        #: ``() -> board | None``: memory's Now picture of the screen (:func:`now_source_for`; ``None``: none).
        self.now_source: Any = now_source_for(backend)
        self._items_lock = threading.Lock()
        #: The time of each window's last own tree read here (ms), to tell a heavy window (v3.15).
        self._read_ms: dict[int, float] = {}
        self._lock = threading.Lock()
        self.last = Snapshot()
        #: Hands in flight (the narrator waits for them, like the mouth's other quick tools).
        self.busy = 0
        self.bridge: Any = None
        #: ``(sent_at)``: a hand's action went out (the turn log's end of speech -> action).
        self.on_action: Any = None
        #: ``(name, what, ok, report, ms)``: a hand finished (the chat UI's tool step; set by the conversation).
        self.on_step: Any = None
        #: ``{hwnd: (window title, action log version, strip, read at)}``: tabs read ahead.
        self._tabs: dict[int, tuple[str, int, Any, float]] = {}
        self._prefetching = False
        #: N7 (report 47): the text field of another app that had the keyboard as the user started talking, and
        #: ``{"at", "since", "app", "title", "field"}`` when its text changed while they spoke (typing or dictation).
        self._focus_mark: dict[str, Any] | None = None
        self.dictating: dict[str, Any] | None = None
        self._desk_ahead = False
        self._desk_ahead_at = 0.0
        #: Set when a hand that touches a window did not show its effect: the talker's window hands then
        #: stop until the user speaks again (re-read, tell them, or hand it over; never repeat blindly).
        self.halted: str | None = None
        #: The names on the screen in front (yuki.voice.screennames, v3.10): its app, its hwnd and the names, read
        #: once per change of the window in front, its title or the action log (``refresh_screen_names``).
        self.screen_names: list[str] = []
        #: Of those, the window's recent titles (its tabs), when its tree gave almost nothing (v3.13).
        self.screen_titles: list[str] = []
        self.screen_app = ""
        self.screen_hwnd: int | None = None
        self._names_key: tuple | None = None
        self._names_at = 0.0
        self._names_busy = False
        #: Hands running now and the slow ones done lately, by what they act on (v3.10: one step runs once).
        self._inflight: dict[str, float] = {}
        self._opened: dict[str, tuple[float, str]] = {}
        #: Done lines said lately, by their words (case and spacing aside): ``{words: said at}`` (v3.14).
        self._lines_said: dict[str, float] = {}
        #: H1 (v3.17): when a hand last did not take effect or was refused, and when a tab or window was last closed
        #: (monotonic): measured states that bring their guidance into the talker's next calls.
        self.trouble_at: float | None = None
        self.closed_at: float | None = None
        # -- v3.18: practice memory (hand traces, learned ways) ---------------------------------------------------
        #: :class:`~yuki.agent.memory.MemoryAccess` the hands' traces go to (``record_hand_trace`` / ``record_run``),
        #: set by the conversation; ``None``: no traces.
        self.memory: Any = None
        #: ``() -> [str]``: the user's latest words (oldest first) a hand acts on; ``() -> str``: the voice turn's id
        #: (``<session>-<n>``); ``() -> [int]``: the playbook ids shown to the talker for this call; ``() -> int``: the
        #: user turns so far. All set by the conversation.
        self.words: Any = None
        self.turn_key: Any = None
        self.shown: Any = None
        self.user_turn: Any = None
        #: ``() -> bool``: recognition was unsure of the user's latest words (a Speech line), set by the conversation:
        #: a volume skill run then takes no focus (review r2 L3: it is never held on unsure words, as the old sound
        #: hands were not, so its focus rungs and a hand-off are left out on that run).
        self.unsure: Any = None
        #: :class:`yuki.voice.practice.PracticeLookup`: the learned ways the talker was offered (run_skill runs only
        #: those), set by the conversation.
        self.practice: Any = None
        #: appOpen: the watches of openings still starting (their results, newest last) and the stop of the running one.
        self.watched: list[dict[str, Any]] = []
        self._watch_stop: threading.Event | None = None
        #: Traces kept (one per hand, and one per skill run), for the tests and the log.
        self.traces_queued = 0
        self._trace_n = 0
        #: Phase F (fPrivacy R1.3): each hand's id (``voice-<turn>-h<n>``) by tool call, minted as the hand starts so
        #: memory's acting marker and the hand's run trace carry the same one (``python -m yuki.log.follow``).
        self._hand_ids: dict[Any, str] = {}
        #: The authorization check for the talker's hands (``yuki.agent.judge.Authorizer``, revamp D S1): made on first
        #: use from the thinker's agent (its client, settings and policy), ``False`` when there is none (no judge in
        #: this build, no agent, or the judge turned off); a user turn is one request for it.
        self._authorizer: Any = None
        #: The judge's model client (the agent's), its verdicts on calls judged although their gate let them through
        #: (per user turn), and the stored policy's per-app switches as last read (34-review H1, H5).
        self._judge_client: Any = None
        self._forced_memo: dict[str, Any] = {}
        self._acts_cache: tuple[Any, dict[str, bool]] | None = None
        # -- Phase S: the skill path (yuki.skills; PLAN.md Phase S, map-skills S3.4) --------------------------------
        #: The keys of this conversation's Skills menu, set by the conversation with its tools (empty: the skill path
        #: is off, and run_skill is the old learned-way hand).
        self.skill_keys: tuple[str, ...] = ()
        #: The memory the skill engine resolves and keeps its ledger with (a MemoryAccess, set by the conversation,
        #: whatever ``voice_hand_traces`` says), and the settings it reads (``Settings.skills*``).
        self.skill_memory: Any = None
        self.skill_settings: Any = None
        #: The last skill run in this conversation (RAM only, never logged): skill, values, outcome, options, run key,
        #: when. For a correction's "the one used" and the talker's skills-now line.
        self.last_skill: dict[str, Any] | None = None
        #: The one "Keep this as your usual?" waiting for its answer (RAM only): pref, option, run key, when.
        self.pending_keep: dict[str, Any] | None = None

    @property
    def skills_on(self) -> bool:
        """Whether run_skill is the skill hand in this conversation (a Skills menu with keys)."""
        return bool(self.skill_keys)

    def new_user_turn(self) -> None:
        """The user spoke: the talker may use its hands again (after telling them what happened)."""
        self.halted = None
        self._forced_memo = {}
        judge = self._authorizer
        if judge:
            try:
                judge.begin_request()   # a user turn is one request: its memo, window grants and denial counts
            except Exception:  # noqa: BLE001
                pass

    # -- v3.18: the authorization check (revamp D S1) -----------------------------------------------------------

    def _judge(self) -> Any:
        """The talker's hands' own :class:`yuki.agent.judge.Authorizer` (probed, never assumed: the judge landed with
        revamp D), built from the thinker's agent: its client, its settings, its policy reader. ``None`` when there
        is no judge, no agent behind the voice, or the agent's judge is turned off (``Settings.judge``)."""
        if self._authorizer is None:
            self._authorizer = False
            try:
                from yuki.agent.judge import Authorizer
            except Exception:  # noqa: BLE001 - an older brain: no judge
                return None
            runtime = getattr(self.bridge, "runtime", None)
            agent = getattr(getattr(runtime, "worker", None), "agent", None)
            if agent is None or getattr(agent, "authorizer", None) is None:
                return None
            log = self.log

            def judge_log(kind: str, **fields: Any) -> None:
                if log is not None:   # the voice log is content-free: the check's why and question stay out
                    log.event(kind, who="voice_hand", **{k: v for k, v in fields.items() if k not in ("why", "question")})

            try:
                self._authorizer = Authorizer(lambda: agent.client, agent.settings, log=judge_log,
                                              policy=getattr(agent.authorizer, "policy", None))
                self._judge_client = self._authorizer.model_client   # always Claude (the check's own client)
            except Exception:  # noqa: BLE001
                self._authorizer = False
        return self._authorizer or None

    def authorize(self, tool: str, tool_input: dict[str, Any], target: dict[str, Any] | None = None,
                  earlier: list[str] | None = None, *, force: bool = False) -> Any:
        """The check's verdict for one call as the thinker's tool it is (``None``: not judged - its metadata and the
        user's policy do not send it there, or there is no judge). It reads only the user's own heard words in this
        conversation (speech recognition's final transcripts, never the talker's wording) and the proposed call (the
        target window's app and title, the element). ``force``: judged even when the metadata and the policy would
        not send it there (34-review H1/H2: a click on a button, menu item or link can send, delete or buy; typing puts
        words in a field) - by the judge's own request and two-stage decision, memoized per user turn by call and
        window, never with a window grant. Blocking (~1 s when it asks the model); off every loop. A check that
        raises or cannot run holds the call (fails closed)."""
        judge = self._judge()
        if judge is None:
            return None
        words = [{"how": "voice", "text": w} for w in (self._call_safely(self.words, []) or []) if str(w).strip()]
        import json

        signature = json.dumps(tool_input, sort_keys=True, default=str)
        try:
            verdict = judge.check(tool, tool_input, words=words, target=target, signature=signature,
                                  earlier=list(earlier or []))
            if verdict is None and force:
                verdict = self._force_judge(judge, tool, tool_input, target or {}, words, list(earlier or []),
                                            signature)
            return verdict
        except Exception as exc:  # noqa: BLE001 - fail closed
            return SimpleNamespace(allowed=False, decision="ask_user", question="", source="error",
                                   why=f"the check could not run ({type(exc).__name__})")

    def _force_judge(self, judge: Any, tool: str, tool_input: dict[str, Any], target: dict[str, Any],
                     words: list[dict[str, Any]], earlier: list[str], signature: str) -> Any:
        """The judge's verdict for a call its gate lets through at this policy (``yuki.agent.judge``'s public request
        and decision: the quick verdict, and the careful review when it holds; fail closed on an error)."""
        from yuki.agent import judge as J
        from yuki.agent.tools import tool_traits

        key = f"{tool}:{target.get('hwnd')}:{signature}"
        memo = self._forced_memo.get(key)
        if memo is not None:
            return memo
        settings = getattr(judge, "settings", None)
        model = str(getattr(settings, "judge_model", "") or "")
        timeout = float(getattr(settings, "judge_timeout_s", 8.0) or 8.0)
        levels = judge.policy.levels() if getattr(judge, "policy", None) is not None else {}
        traits = tool_traits(tool, tool_input)
        client = self._judge_client() if self._judge_client is not None else None
        if client is None:
            return SimpleNamespace(allowed=False, decision="ask_user", question="", source="error",
                                   why="the authorization check could not run")
        verdict = J.decide(client, J.judge_request(words, tool, tool_input, target=target, levels=levels,
                                                   traits=traits, model=model, earlier=earlier),
                           timeout_s=timeout, need=())
        if verdict.error is None and not verdict.allowed:
            first = verdict
            again = J.decide(client, J.judge_request(words, tool, tool_input, target=target, levels=levels,
                                                     traits=traits, model=model, review=True, earlier=earlier),
                             timeout_s=timeout + 2.0, need=())
            if again.error is None:
                again.question = again.question or first.question
                again.why = again.why or first.why
                again.source = "review"
                again.ms = round(first.ms + again.ms, 1)
                verdict = again
        if verdict.error is not None:
            verdict.decision, verdict.source = "ask_user", "error"
            verdict.why = "the authorization check could not run"
        else:
            self._forced_memo[key] = verdict
        if self.log is not None:
            self.log.event("judge", who="voice_hand", forced=True, tool=tool, decision=verdict.decision,
                           source=verdict.source, ms=verdict.ms, error=verdict.error)
        return verdict

    # -- 34-review H5: the per-app "Act" switch in Settings ----------------------------------------------------

    #: Where the stored autonomy policy lives (``None``: ``%LOCALAPPDATA%\\Yuki\\autonomy.json``, as the judge reads it).
    policy_path: Any = None

    def act_off(self, app: str | None) -> str | None:
        """The name Settings switched "Act" off for, when ``app`` (a window's process, or a popup's app name) is that
        app: Yuki may not click, type or press keys there. Read like the judge's policy reader (the ``apps`` of the
        stored policy file, re-read when it changes), by names only: the process, its stem and its display name are
        compared with the switched-off names, case aside. ``None``: allowed (or nothing switched off)."""
        if not app:
            return None
        acts = self._app_acts()
        if not acts or all(acts.values()):
            return None
        names = {str(app), str(app).removesuffix(".exe"), str(app).rsplit(".", 1)[0]}
        try:
            from yuki.core import appnames

            names.add(str(appnames.name_for_key(str(app)) or ""))
        except Exception:  # noqa: BLE001 - the process names are enough
            pass
        for name in names:
            if name and acts.get(name.casefold()) is False:
                return name
        return None

    def _app_acts(self) -> dict[str, bool]:
        """``{app name casefolded: allowed}`` from the stored policy (the judge's reader when it has ``apps``)."""
        if self.policy_path is None and not _is_real_backend(self.backend):
            return {}   # a stand-in desk never reads the user's real settings (tests set policy_path)
        judge = self._judge() if self.policy_path is None else None
        reader = getattr(getattr(judge, "policy", None), "apps", None) if judge else None
        if callable(reader):
            try:
                got = reader()
                if isinstance(got, dict):
                    return {str(k).casefold(): bool(v) for k, v in got.items()}
            except Exception:  # noqa: BLE001 - then the file is read here
                pass
        path = self.policy_path
        if path is None:
            try:
                from yuki.agent.judge import policy_path

                path = policy_path()
            except Exception:  # noqa: BLE001 - an older brain: the same file, by its documented place
                import os
                from pathlib import Path

                path = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")) / "Yuki" / \
                    "autonomy.json"
        from pathlib import Path

        try:
            st = Path(path).stat()
            stamp = (str(path), st.st_mtime, st.st_size)
        except OSError:
            return {}
        if self._acts_cache is not None and self._acts_cache[0] == stamp:
            return self._acts_cache[1]
        import json

        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        apps = raw.get("apps") if isinstance(raw, dict) else None
        acts = {str(k).casefold(): bool(v.get("act", True)) for k, v in (apps or {}).items()
                if isinstance(v, dict) and str(k).strip()} if isinstance(apps, dict) else {}
        self._acts_cache = (stamp, acts)
        return acts

    def _refuse_if_act_off(self, app: str | None) -> None:
        off = self.act_off(app)
        if off:
            raise _Refused(f"Act is switched off for {off} in Yuki's settings, so Yuki does not click, type or press "
                           f"keys there; nothing was done", act_off=off)

    def _check_hand(self, name: str, args: dict[str, Any]) -> None:
        """Hold a hand the check holds (raises :class:`_Refused` with ``held``): a window close, a tab close, or a kind
        the user's autonomy policy holds back (the hand's fixed name and operation map to the thinker's tool, whose
        metadata decides; the media keys are a fixed set of sound keys that can close or send nothing)."""
        if name in ("click", "type"):
            return   # judged inside, once the control they act on is known (its role decides for a click)
        hwnd = args.get("window")
        names: list[str] = []
        if name == "tab":
            with self._lock:
                shown = list(self.last.tabs.get(int(hwnd), []) if str(hwnd).lstrip("-").isdigit() else [])
            wanted = ([args.get("tab")] if args.get("tab") is not None else []) + list(args.get("tabs") or [])
            for n in wanted:
                try:
                    names.append(shown[int(n) - 1])
                except (TypeError, ValueError, IndexError):
                    continue
        call = judged_call(name, args, tab_names=names)
        if call is None:
            return
        tool, tool_input = call
        target: dict[str, Any] = {"hwnd": hwnd, "app": self._app_at(hwnd), "title": self._title_shown(hwnd)}
        if name == "tab" and str(args.get("action") or "") == "close":
            target["element"] = "tabs " + ", ".join(f'"{n}"' for n in tool_input.get("tabs") or [])
        verdict = self.authorize(tool, tool_input, target)
        if verdict is not None and not getattr(verdict, "allowed", False):
            raise _Refused(f"held by Yuki's own check: {getattr(verdict, 'why', '') or 'not plainly asked for'}",
                           held=str(getattr(verdict, "question", "") or ""))

    # -- the desktop summary ------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """Capture the summary now (blocking, a few ms; call it off the event loop). Never raises."""
        started = time.perf_counter()
        snap = Snapshot(at=time.monotonic(), version=self.actions.version, wall=time.time())
        try:
            text, payload, _ms = self._dispatcher.overview_text()
            windows = [w for w in (payload.get("windows") or []) if isinstance(w, dict)]
            snap.hwnds = {int(w["hwnd"]) for w in windows if w.get("hwnd") is not None}
            snap.apps = {int(w["hwnd"]): str(w.get("process_name") or w.get("app") or "") for w in windows
                         if w.get("hwnd") is not None}
            snap.overview_text, snap.overview = text, payload
            snap.bounds = {int(w["hwnd"]): tuple(w.get("bounds") or (0, 0, 0, 0)) for w in windows
                           if w.get("hwnd") is not None and not w.get("is_minimized")}
            snap.foreground = int(payload["foreground_hwnd"]) if payload.get("foreground_hwnd") else None
            cursor = payload.get("cursor")
            snap.cursor = (int(cursor[0]), int(cursor[1])) if cursor else None
            parts = [text]
            try:
                media = media_line(self._dispatcher.activity_facts())
            except Exception:  # noqa: BLE001 - the windows are the part that matters
                media = ""
            if media:
                parts.append(media)
            audio = self._audio_line()
            if audio:
                parts.append(audio)
            titles = {int(w["hwnd"]): str(w.get("title") or "") for w in windows if w.get("hwnd") is not None}
            tabs = self._tabs_line(snap, titles.get(snap.foreground or 0, ""))
            if tabs:
                parts.append(tabs)
            with self._lock:
                names, names_app, names_hwnd = list(self.screen_names), self.screen_app, self.screen_hwnd
            if names and names_hwnd == snap.foreground:
                from yuki.voice.screennames import screen_line

                parts.append(screen_line(names_app, names))
            with self._lock:
                items = self.items
            board = self._now_board()
            snap.board = board
            for p in (board or {}).get("popups") or []:
                if isinstance(p, dict) and p.get("hwnd") and int(p["hwnd"]) not in snap.apps:
                    snap.apps[int(p["hwnd"])] = str(p.get("app") or "")
            if (items is not None and items.hwnd == snap.foreground
                    and items.title == titles.get(snap.foreground or 0, "")):
                popup = None
                if snap.foreground is not None and snap.foreground not in titles:
                    # a popup in front, no window of the list (Chrome's "Restore pages?" bubble had the keyboard on
                    # 2 Oct): said so, with its app, so a click takes its own hwnd (F1)
                    seen = next((p for p in (board or {}).get("popups") or []
                                 if isinstance(p, dict) and int(p.get("hwnd") or 0) == snap.foreground), None)
                    popup = str((seen or {}).get("app") or "a popup")
                line = items_line(items, popup=popup)
                if line:
                    parts.append(line)
                    if snap.foreground is not None:
                        snap.hwnds.add(snap.foreground)   # a popup in front (no user window) is clickable too (F1)
            parts.extend(self._extra_lines(snap, board))   # F1: popups and other visible windows
            focus = self._focus_line(snap.foreground)
            if focus:
                parts.append(focus)
            done = self.actions.lines(_MOUTH_ACTION_LINES, 240)
            if done:
                parts.append("done on the desktop in this conversation (newest last):\n" + "\n".join(done))
            snap.text = "\n".join(parts)
        except Exception as exc:  # noqa: BLE001 - the mouth goes without, and its hands refuse
            snap.error = f"{type(exc).__name__}: {exc}"
        snap.ms = (time.perf_counter() - started) * 1000.0
        with self._lock:
            if not snap.error:
                self.last = snap
        return snap

    def _audio_line(self) -> str:
        """The apps with sound on the output and their own (mixer) volume, one line ("" without it).

        v3.18: the entry that is Yuki's own voice (an entry the backend marks ``own``: this process's audio session) is
        said so, so "your volume" has something to act on (live 2 Oct 21:27 the talker pressed the system media keys
        for it: the line never said which app was its own voice)."""
        read = getattr(self.backend, "audio_apps", None)
        if not callable(read):
            return ""
        try:
            apps = [dict(a) for a in read() or [] if isinstance(a, dict)]
            if _is_real_backend(self.backend):
                # the real mixer: the voice plays from this very process, so its session is the one with this
                # process's own name (a stand-in desk marks its own entries)
                own = _own_process_name().casefold()
                for a in apps:
                    if own and str(a.get("app") or "").casefold() == own:
                        a["own"] = True
            return audio_line_own(apps)
        except Exception:  # noqa: BLE001 - the windows are the part that matters
            return ""

    def _tabs_line(self, snap: Snapshot, title: str) -> str:
        """The foreground window's tabs, recorded on ``snap``: read ahead when still good, else now (bounded)."""
        read = getattr(self.backend, "read_tabs", None)
        if not callable(read) or snap.foreground is None or snap.foreground not in snap.bounds:
            return ""
        from yuki.perception.tabs import tabs_line

        started = time.perf_counter()
        with self._lock:
            ahead = self._tabs.get(snap.foreground)
        if (ahead is not None and ahead[0] == title and ahead[1] == self.actions.version
                and time.monotonic() - ahead[3] < _TABS_FRESH_S):
            strip = ahead[2]
        else:
            try:
                strip = read(snap.foreground, budget_s=_TABS_BUDGET_S)
            except Exception:  # noqa: BLE001
                return ""
            self._keep_tabs(snap.foreground, title, strip)
        snap.tabs_ms = round((time.perf_counter() - started) * 1000.0, 1)
        if not getattr(strip, "tabs", None):
            return ""
        snap.tabs[snap.foreground] = [t.name for t in strip.tabs]
        snap.tab_ids[snap.foreground] = [getattr(t, "tab_id", "") or "" for t in strip.tabs]
        return tabs_line(strip)

    def _keep_tabs(self, hwnd: int, title: str, strip: Any) -> None:
        if getattr(strip, "error", None) is None:
            with self._lock:
                self._tabs = {hwnd: (title, self.actions.version, strip, time.monotonic())}

    #: A window whose tree gives fewer names than this draws its own interface (a GPU terminal): its recent titles
    #: (its tabs) are what can be named in it (v3.13).
    FEW_NAMES = 3

    def refresh_screen_names(self, on_change: Any = None, *, max_age_s: float = 30.0,
                             titles: Any = None) -> None:
        """Read the names on the screen in front again when it changed (a thread; returns at once).

        Changed means another window in front, another title, or something done on the desktop since the last read
        (or that read is older than ``max_age_s``). One bounded UI Automation read of the window in front (a few
        hundred ms at most, off every loop), never on a reply's path. ``on_change(names)`` is called (on that
        thread) when the list is different.
        """
        if self._names_busy:
            return
        read = getattr(self.backend, "get_window_tree", None)
        if not callable(read):
            return
        self._names_busy = True

        def run() -> None:
            try:
                _text, payload, _ms = self._dispatcher.overview_text()
                front = int(payload["foreground_hwnd"]) if payload.get("foreground_hwnd") else None
                if front is None:
                    return
                win = next((w for w in payload.get("windows") or [] if isinstance(w, dict) and w.get("hwnd") == front),
                           {})
                key = (front, str(win.get("title") or ""), self.actions.version)
                if key == self._names_key and time.monotonic() - self._names_at < max_age_s:
                    return
                from yuki.voice.screennames import LIST_ROLES, names_from_tree

                tree = self._read_items(front, str(win.get("title") or ""))
                if tree is None:
                    # a backend the dispatcher cannot read through (a minimal stand-in): the tree alone, for the names
                    try:
                        tree = read(front, max_elements=300, timeout_s=0.8)
                    except TypeError:
                        tree = read(front)
                names = names_from_tree(tree)
                image = str(win.get("process_name") or win.get("app") or "")
                recent: list[str] = []
                # No list, tree or tab items at all (a browser shows its tabs as TabItems: its titles are pages, not
                # tabs), and next to no names: the app draws its own interface.
                drawn = not any(str((e.get("role") if isinstance(e, dict) else getattr(e, "role", "")) or "")
                                in LIST_ROLES for e in ((tree.get("elements") if isinstance(tree, dict)
                                                         else getattr(tree, "elements", None)) or []))
                if len(names) < self.FEW_NAMES and drawn and titles is not None and image:
                    # Live 2026-09-29: Warp's sidebar tab "LLM OS agent harness revamp" was heard as "LLM OS rigid
                    # hardness" and never found: Warp exposes no items, but its tabs were its titles all evening.
                    try:
                        recent = [t for t in (titles(image) or []) if t][:12]
                    except Exception:  # noqa: BLE001 - a hint only
                        recent = []
                    names = list(dict.fromkeys([*names, *recent]))
                try:   # the app's own name, as it may be spoken ("WhatsApp", not "WhatsApp.Root")
                    from yuki.core import appnames

                    app = (appnames.name_for_pid(int(win["pid"]), image) if win.get("pid")
                           else appnames.name_for_key(image))
                except Exception:  # noqa: BLE001 - a hint only
                    app = image.removesuffix(".exe")
                with self._lock:
                    changed = names != self.screen_names or front != self.screen_hwnd
                    self.screen_names, self.screen_app, self.screen_hwnd = names, app, front
                    self.screen_titles = recent
                self._names_key, self._names_at = key, time.monotonic()
                if self.log is not None:
                    self.log.event("screen_names", names=len(names), changed=changed)
                if changed and on_change is not None:
                    on_change(list(names))
            except Exception as exc:  # noqa: BLE001 - only a hint for recognition and the talker
                if self.log is not None:
                    self.log.event("screen_names", error=type(exc).__name__)
            finally:
                self._names_busy = False

        self._workers.submit(run)   # a thread that already exists: never started from the loop (v3.14)

    def prefetch(self) -> None:
        """Read the foreground window's tabs ahead, off every thread that matters (the user started talking).

        Returns at once; one read at a time. The summary for the reply then takes them from
        here instead of reading them on the reply's path (a UIA walk of Chrome's window: tens
        of milliseconds).
        """
        read = getattr(self.backend, "read_tabs", None)
        if not callable(read) or self._prefetching:
            return
        self._prefetching = True

        def run() -> None:
            try:
                _text, payload, _ms = self._dispatcher.overview_text()
                front = int(payload["foreground_hwnd"]) if payload.get("foreground_hwnd") else None
                if front is None:
                    return
                title = next((str(w.get("title") or "") for w in payload.get("windows") or []
                              if isinstance(w, dict) and w.get("hwnd") == front), "")
                self._mark_focus(front, payload)   # N7: the text at the caret as they start talking
                self._keep_tabs(front, title, read(front, budget_s=0.4))
                with self._lock:
                    items = self.items
                if (items is None or items.hwnd != front or items.title != title or items.version != self.actions.version
                        or time.monotonic() - items.at > ITEMS_AHEAD_S):
                    self._read_items(front, title)   # the items line for the reply, read while they talk (v3.14)
            except Exception:  # noqa: BLE001 - only a head start
                pass
            finally:
                self._prefetching = False

        self._workers.submit(run)   # a thread that already exists: never started from the loop (v3.14)

    def prefetch_desk(self) -> None:
        """Read the whole summary ahead while the user talks (a thread; at most every ``_DESK_AHEAD_EVERY_S``).

        Returns at once. The reply's call then takes it (:meth:`for_call`) instead of waiting for a read.
        """
        now = time.monotonic()
        if self._desk_ahead or now - self._desk_ahead_at < _DESK_AHEAD_EVERY_S:
            return
        self._desk_ahead, self._desk_ahead_at = True, now

        def run() -> None:
            try:
                self.snapshot()
            except Exception:  # noqa: BLE001 - only a head start
                pass
            finally:
                self._desk_ahead = False

        self._workers.submit(run)   # a thread that already exists: never started from the loop (v3.14)

    async def for_call(self, wait_ms: float) -> tuple[Snapshot | None, str]:
        """A fresh summary for one mouth call, waiting at most ``wait_ms``; else the last one (``how``).

        A summary read ahead while the user talked is used at once when it is recent and nothing was done on
        the desktop since (``"ahead"``).
        """
        with self._lock:
            last = self.last
        if (last.text and not last.error and last.version == self.actions.version
                and time.monotonic() - last.at < _DESK_FRESH_S):
            return last, "ahead"
        task = asyncio.ensure_future(asyncio.to_thread(self.snapshot))
        try:
            snap = await asyncio.wait_for(asyncio.shield(task), timeout=max(wait_ms, 1.0) / 1000.0)
            return (snap, "fresh") if not snap.error else (self._last_or_none(), "previous")
        except asyncio.TimeoutError:
            return self._last_or_none(), "previous"

    def _last_or_none(self) -> Snapshot | None:
        with self._lock:
            return self.last if self.last.text else None

    # -- the tools (Pipecat handlers, on the voice loop) --------------------------

    async def handle(self, params: Any) -> None:
        """One hand: run it off the loop, then its report goes back as the call's result."""
        name = params.function_name
        if self.skills_on and name in (SKILL_HAND, KEEP_USUAL_HAND):
            await self._handle_skill(params)   # Phase S: one of the user's skills, or the answer to its question
            return
        args = params.arguments if isinstance(params.arguments, dict) else {}
        started = time.monotonic()
        key = _step_key(name, args)
        recent = self._opened.get(key) if name in ("open_url", "open_app") else None
        if key in self._inflight or (recent is not None and started - recent[0] < _OPEN_REPEAT_S):
            # One step runs once (v3.10): the same hand on the same target while it runs (a second mouth run on the
            # same words), or an opening just verified, is that step, not a second one with a second done line.
            from pipecat.frames.frames import FunctionCallResultProperties

            if self.log is not None:
                self.log.event("hands", op=name, deduped=True, running=key in self._inflight,
                               call=params.tool_call_id)
            await params.result_callback({"already": (recent[1][:400] if recent else "the same step is running now"),
                                          "how_to_handle_it": _DUPLICATE_NOTE},
                                         properties=FunctionCallResultProperties(run_llm=False))
            return
        self._inflight[key] = started
        if args.get("say_first") and self.bridge is not None:
            await self.bridge._ack_if_silent(params, args)  # said already while it was written, as a rule
        self.busy += 1
        if self.bridge is not None:
            self.bridge.busy_tools += 1
        hand_id = self._mint_hand_id(params.tool_call_id)
        try:
            ok, report, facts = await asyncio.to_thread(self._acting_run, name, args, hand_id)
        except Exception as exc:  # noqa: BLE001 - a hand's crash is a failed hand
            ok, report, facts = False, f"{type(exc).__name__}: {exc}", {}
        finally:
            self._inflight.pop(key, None)
            self.busy = max(0, self.busy - 1)
            if self.bridge is not None:
                self.bridge.busy_tools = max(0, self.bridge.busy_tools - 1)
                if self.bridge.gate is not None:
                    self.bridge.gate.poke()
        ms = round((time.monotonic() - started) * 1000)
        if self.on_action is not None and facts.get("sent_at") is not None:
            self.on_action(facts["sent_at"])
        line = str(args.get("say_when_done") or "").strip()
        # The done line goes out at once, with no second model call, only when the hand's own
        # report verified the effect (a window's state, the selected tab, the scroll position,
        # the media status or volume, the page's address and title). Otherwise the mouth reads
        # the report and says what happened (or hands it over).
        verified = facts.get("verified")
        confirmed = ok and verified is True and name != "press_keys"
        if not confirmed and not facts.get("settled") and not facts.get("held") and not facts.get("act_off"):
            self.trouble_at = time.monotonic()   # its guidance rides with the talker's next calls (v3.17)
        if ok and name in ("tab", "window") and str(args.get("action") or "") == "close":
            self.closed_at = time.monotonic()
        if confirmed and name in ("open_url", "open_app"):
            self._opened = {k: v for k, v in self._opened.items() if started - v[0] < _OPEN_REPEAT_S}
            self._opened[key] = (time.monotonic(), report)
        # fx2: an opening that leaves part of their request to do (the talker's own "then") is not the whole request
        rest = " ".join(str(args.get("then") or "").split()) if name in OPEN_HANDS else ""
        speak = bool(line) and confirmed and not rest
        if speak:
            words = " ".join(line.casefold().split())
            now = time.monotonic()
            self._lines_said = {k: at for k, at in self._lines_said.items() if now - at < _REPEAT_LINE_S}
            if words in self._lines_said:
                speak, line = False, ""   # said a moment ago for the same kind of step: this time it is silent
                if self.log is not None:
                    self.log.event("hands", op=name, repeat_line_silent=True, call=params.tool_call_id)
            else:
                self._lines_said[words] = now
        self.actions.add("your hand", _what(name, args), report[:400], ok=ok, verified=verified)
        if self.on_step is not None:
            try:
                self.on_step(name, hand_words(name, args, facts.get("app")), bool(ok and verified is not False),
                             report[:400], ms)
            except Exception:  # noqa: BLE001 - the screen never breaks a hand
                pass
        if self.log is not None:
            # request_id: the hand's id (its marker's and trace's), so follow finds its outcome (fWire r1 L2)
            self.log.event("hands", op=name, ok=ok, ms=ms, report_chars=len(report), call=params.tool_call_id,
                           done_line=speak, request_id=hand_id,
                           **{k: v for k, v in facts.items()
                              if (isinstance(v, (int, float, bool)) or v is None)
                              and k not in ("question", "request_id")})
        # v3.18: every hand that acted is kept as a run trace for practice memory (off the loop, in order)
        if name == SKILL_HAND:
            self._trace_skill(args, ok=ok, report=report, facts=facts, ms=ms, call=params.tool_call_id)
        elif facts.get("starting"):
            # appOpen: the trace waits for the watch, so the learner reads the opening together with its outcome
            self._watch_open(name, args, report=report, facts=facts, ms=ms, call=params.tool_call_id)
        elif not facts.get("refused"):
            self._trace(name, args, ok=ok, verified=verified, report=report, facts=facts, ms=ms,
                        call=params.tool_call_id)
        self._hand_ids.pop(params.tool_call_id, None)   # a hand that kept no trace leaves no id behind
        if ok and facts.get("starting"):
            await params.result_callback({"starting": report[:700], "how_to_say_it": _STARTING_NOTE})
            return
        if confirmed and rest:
            # fx2: the rest of their request is still to do: the talker gets its turn back (run_llm) with the screen
            # as it is once it settled (the new window's items, a popup that came with it), and does the rest with
            # one hand or hands it over - never a done line for half of it
            settle = await asyncio.to_thread(self.settle_after_open)
            if self.log is not None:
                self.log.event("hands", op="open_settle", hand=name, rest_chars=len(rest), call=params.tool_call_id,
                               **settle)
            await params.result_callback({"done": report[:700], "then": rest[:200],
                                          "how_to_handle_it": _THEN_NOTE.format(rest=rest[:200])})
            return
        if confirmed and not line:
            from pipecat.frames.frames import FunctionCallResultProperties

            await params.result_callback({"done": report[:700], "said": "", "how_to_handle_it": _SILENT_NOTE},
                                         properties=FunctionCallResultProperties(run_llm=False))
            return
        if speak:
            from pipecat.frames.frames import FunctionCallResultProperties, TTSSpeakFrame

            await params.llm.push_frame(TTSSpeakFrame(text=line))
            await params.result_callback({"done": report[:700], "said": line, "how_to_handle_it": _SAID_NOTE},
                                         properties=FunctionCallResultProperties(run_llm=False))
            return
        if facts.get("held"):
            # v3.18: Yuki's own check held it (revamp D S1): nothing more is done; the talker asks its question
            question = str(facts.get("question") or "").strip() or "Ask whether they want exactly this."
            await params.result_callback({"held": report[:700],
                                          "how_to_handle_it": _JUDGE_HELD_NOTE.format(question=question)})
            return
        if facts.get("act_off"):
            # v3.18 (34-review H5): Act is off for that app in Settings: nothing is done there, by anyone
            await params.result_callback({"refused": report[:700], "how_to_handle_it": _ACT_OFF_NOTE.format(
                app=facts["act_off"])})
            return
        if ok and facts.get("expected"):
            # V6: done, and its effect cannot be read back by the nature of the target (a web page's scroll): no
            # second talker run, no hand-off; they watch it happen
            from pipecat.frames.frames import FunctionCallResultProperties

            await params.result_callback({"done": report[:700], "how_to_handle_it": _EXPECTED_NOTE},
                                         properties=FunctionCallResultProperties(run_llm=False))
            return
        if ok and facts.get("noop"):
            await params.result_callback({"no_change": report[:700], "how_to_say_it": _NOOP_NOTE})
            return
        if name == "press_keys" and ok:
            note, key = _KEYS_NOTE, "report"
        elif ok:
            note, key = _UNCONFIRMED_NOTE, "not_verified"
        else:
            note, key = _FAILED_NOTE, "failed"
        if ((((name in INSTANT_HANDS or name == SKILL_HAND) and not facts.get("refused")) or facts.get("stale"))
                and not facts.get("settled")
                and self.bridge is not None and callable(getattr(self.bridge, "escalate", None))):
            # Tried and did not take effect (not refused, not "already so"): the thinker gets the same request
            # now, by code, with this report and the window read; no talker run narrates the failure (v3.9). A learned
            # way that deviated (v3.18) goes the same way, with what it did and where it stopped.
            ack = False
            try:
                ack = bool(self.bridge.may_ack()) if callable(getattr(self.bridge, "may_ack", None)) else False
            except Exception:  # noqa: BLE001
                ack = False
            context = facts.get("skill_context") if name == SKILL_HAND else None
            try:
                try:
                    if context:
                        passed = await self.bridge.escalate(params, name, args, report, acked=ack, context=context)
                    else:
                        passed = await self.bridge.escalate(params, name, args, report, acked=ack)
                except TypeError:   # a stand-in bridge without the v3.14 / v3.18 arguments
                    passed = await self.bridge.escalate(params, name, args, report)
            except Exception as exc:  # noqa: BLE001 - then the talker handles the report as before
                passed = False
                if self.log is not None:
                    self.log.event("hands", op=name, escalate_error=f"{type(exc).__name__}: {exc}")
            if passed:
                from pipecat.frames.frames import FunctionCallResultProperties

                if self.log is not None:
                    self.log.event("hands", op=name, escalated=True, acked=ack, call=params.tool_call_id)
                if ack:
                    self.bridge.acked()
                await params.result_callback({key: report[:700], "passed_on": True,
                                              "how_to_handle_it": _PASSED_ACK_NOTE if ack else _PASSED_NOTE},
                                             properties=FunctionCallResultProperties(run_llm=ack))
                return
        await params.result_callback({key: report[:700], "how_to_say_it": note})

    def settle_after_open(self) -> dict[str, Any]:
        """Blocking (off the loop): wait until the screen settles after an opening - the windows, the one in front and
        memory's popups unchanged for :data:`SETTLE_QUIET_S`, at most :data:`SETTLE_MAX_S` - then read the window in
        front's items, so the talker's next call sees what the opening brought up (fx2). Measured facts only:
        ``{"settle_ms", "changes", "popups", "settled", "items"}``."""
        started = time.monotonic()
        last, quiet_since, changes, popups = None, started, 0, 0
        front, title = None, ""
        while True:
            try:
                _text, payload, _ms = self._dispatcher.overview_text()
            except Exception:  # noqa: BLE001 - then the next call reads what it can
                payload = {}
            board = self._now_board() or {}
            pops = sorted(int(p["hwnd"]) for p in board.get("popups") or [] if isinstance(p, dict) and p.get("hwnd"))
            wins = sorted(int(w["hwnd"]) for w in payload.get("windows") or [] if isinstance(w, dict) and w.get("hwnd"))
            front = int(payload.get("foreground_hwnd") or 0) or None
            title = next((str(w.get("title") or "") for w in payload.get("windows") or []
                          if isinstance(w, dict) and w.get("hwnd") == front), "")
            now = time.monotonic()
            sig = (front, title, tuple(wins), tuple(pops))
            if sig != last:
                if last is not None:
                    changes += 1
                last, quiet_since = sig, now
            popups = len(pops)
            settled = now - quiet_since >= SETTLE_QUIET_S
            if settled or now - started >= SETTLE_MAX_S:
                break
            time.sleep(SETTLE_POLL_S)
        items = 0
        if front:
            try:
                self._read_items(front, title)   # the new window's items line for the talker's next call
                with self._lock:
                    items = len(self.items.items) if self.items is not None and self.items.hwnd == front else 0
            except Exception:  # noqa: BLE001 - a window with no tree (an app that draws itself)
                items = 0
        return {"settle_ms": round((time.monotonic() - started) * 1000), "changes": changes, "popups": popups,
                "settled": settled, "items": items}

    # -- v3.18: run traces for practice memory --------------------------------------------------------------------

    def _call_safely(self, fn: Any, default: Any) -> Any:
        try:
            return fn() if callable(fn) else default
        except Exception:  # noqa: BLE001 - a trace's context is a hint
            return default

    def _mint_hand_id(self, call: Any = None) -> str:
        """A new hand id, ``voice-<turn key>-h<n>``, kept for ``call`` so the hand's trace reuses it."""
        self._trace_n += 1
        key = str(self._call_safely(self.turn_key, "") or "voice")
        hand_id = f"voice-{key}-h{self._trace_n}"
        if call is not None:
            self._hand_ids[call] = hand_id
            while len(self._hand_ids) > 64:   # bounded: a hand whose end never came
                self._hand_ids.pop(next(iter(self._hand_ids)))
        return hand_id

    def _trace_ids(self, call: Any) -> tuple[str, list[str], list[int]]:
        """``(request id, the user's words, playbook ids shown)`` for a hand's trace: the id minted when the hand
        started (the acting marker's), else a new one."""
        hand_id = self._hand_ids.pop(call, None) if call is not None else None
        words = [str(w) for w in (self._call_safely(self.words, []) or []) if str(w).strip()]
        shown = [int(i) for i in (self._call_safely(self.shown, []) or []) if isinstance(i, int)]
        return hand_id or self._mint_hand_id(), words, shown

    def _queue(self, write: Any) -> None:
        """A trace write, off the voice loop, in order with memory's turn log (no disk on the loop)."""
        from yuki.voice.workers import ordered

        self.traces_queued += 1
        ordered().submit(write)

    def _trace(self, name: str, args: dict[str, Any], *, ok: bool, verified: bool | None, report: str,
               facts: dict[str, Any], ms: float, call: Any = None) -> None:
        """Keep one hand as a run trace (03 §7.2, 27-build-C §6): ``MemoryAccess.record_hand_trace`` - the tool, the
        target by control role and name, the app it acted in, the verdict and why, the result - so practice memory's
        learners see the talker's hands too (the 2 Oct volume mistake was a hand's). When practice objects were shown
        to the talker for this call, the run goes through ``record_run`` with them as ``injected`` (the same run, one
        step), so the ledger can weigh what was shown against what happened."""
        memory = self.memory
        if memory is None:
            return
        request_id, words, shown = self._trace_ids(call)
        at = time.time() - float(ms or 0) / 1000.0
        why = facts.get("why") or ("its own report verified it" if verified is True else
                                   "its own report did not verify it" if verified is False else
                                   "its report could not tell")
        target = facts.get("target")
        app = facts.get("app") or None
        payload = {"name": name, "args": dict(args or {}), "ok": bool(ok), "verified": verified,
                   "report": str(report or "")[:600], "ms": float(ms or 0.0), "at": at, "app": app,
                   "target": target, "words": " / ".join(words[-2:]) or None, "why": why, "request_id": request_id}

        control = facts.get("control")

        def write() -> None:
            try:
                record = getattr(memory, "record_hand_trace", None)
                extra = {"control": control} if control and _takes_kw(record, "control") else {}
                if shown and _takes_kw(record, "injected"):
                    # fx2: memory takes the playbooks shown with the hand itself (29-build §3 item 2)
                    memory.record_hand_trace(**payload, injected=shown, **extra)
                elif shown and callable(getattr(memory, "record_run", None)):
                    # the same one-step run record_hand_trace keeps, with the playbooks shown (it has no such field)
                    memory.record_run(hand_run(payload, session_id=getattr(memory, "session_id", None),
                                               injected=shown))
                else:
                    memory.record_hand_trace(**payload, **extra)
            except Exception as exc:  # noqa: BLE001 - memory never breaks a hand
                if self.log is not None:
                    self.log.event("hand_trace", op="error", name=name, error=type(exc).__name__)

        self._queue(write)
        if self.log is not None:
            self.log.event("hand_trace", op="queued", name=name, ok=bool(ok), verified=verified, ms=round(ms),
                           target=bool(target), app=bool(app), injected=len(shown), call=call)

    def _watch_open(self, name: str, args: dict[str, Any], *, report: str, facts: dict[str, Any], ms: float,
                    call: Any = None) -> None:
        """appOpen F1/F3: an opening handed to a launcher whose app has no window yet. A light, bounded watch
        (``backend.watch_launch``: one window list a second, at most ``yuki.actions.launch.WATCH_MAX_S``) on its own
        thread reports the app's window when it shows - or what came up instead - to the shared action log, the
        screen's step line and the log. It never speaks and never acts; a newer watch replaces it. Then the opening is
        kept as one run of two steps (the hand, then the watch with its verdict), so practice memory's learner reads
        the opening together with its outcome."""
        request_id, words, shown = self._trace_ids(call)
        started_at = time.time() - float(ms or 0) / 1000.0
        watch = getattr(self.backend, "watch_launch", None)
        launcher = dict(facts.get("launcher") or {})
        previous = getattr(self, "_watch_stop", None)
        if previous is not None:
            previous.set()
        stop = threading.Event()
        self._watch_stop = stop
        app_name = str(launcher.get("name") or args.get("name") or "it")

        def run() -> None:
            began = time.monotonic()
            try:
                result = watch(launcher, stop=stop) if callable(watch) else {
                    "verified": None, "summary": "no watch on this desktop"}
            except Exception as exc:  # noqa: BLE001 - a watch that failed says so
                result = {"verified": None, "summary": f"the watch failed: {type(exc).__name__}"}
            result = result if isinstance(result, dict) else {"verified": None, "summary": str(result)}
            verdict = result.get("verified")
            summary = str(result.get("summary") or "")
            watch_ms = round((time.monotonic() - began) * 1000)
            try:
                self.actions.add("your hand", f"watched {app_name} start", summary[:400], ok=verdict is not False,
                                 verified=verdict)
            except Exception:  # noqa: BLE001
                pass
            if self.on_step is not None:
                try:
                    self.on_step("open_app", hand_words(name, args, result.get("process_name") or facts.get("app")),
                                 verdict is True, summary[:400], watch_ms)
                except Exception:  # noqa: BLE001 - the screen never breaks a hand
                    pass
            if self.log is not None:
                self.log.event("hands", op="open_watch", verified=verdict, ms=watch_ms,
                               in_front=bool(result.get("in_front")), fullscreen=bool(result.get("fullscreen")),
                               launcher_front=bool(result.get("launcher_front")), call=call)
            self._trace_opening(name, args, report=report, facts=facts, ms=ms, started_at=started_at,
                                request_id=request_id, words=words, shown=shown, watch=result, watch_ms=watch_ms)
            self.watched.append(result)

        threading.Thread(target=run, name="yuki-open-watch", daemon=True).start()

    def _trace_opening(self, name: str, args: dict[str, Any], *, report: str, facts: dict[str, Any], ms: float,
                       started_at: float, request_id: str, words: list[str], shown: list[int], watch: dict[str, Any],
                       watch_ms: float) -> None:
        """One opening and its watch as a run of two steps (``MemoryAccess.record_run``): the hand (could not tell
        yet: still starting) and the watch, verified when the app's own window showed, not when it did not."""
        memory = self.memory
        if memory is None or not callable(getattr(memory, "record_run", None)):
            return
        verdict = watch.get("verified")
        shown_app = str(watch.get("process_name") or "") or None
        launcher = facts.get("launcher") if isinstance(facts.get("launcher"), dict) else {}
        # review F1: which launcher took it and when, so the learner tells a game this launch started from the user's
        handed = {k: launcher[k] for k in ("handed_to", "handed_at") if launcher.get(k)}
        steps = [
            {"tool": "launch_app", "kind": "action", "app": None, "target": f'app "{args.get("name")}"',
             "input": {"name": args.get("name"), **handed}, "ok": True, "verdict": None,
             "why": "still starting: no window of it yet when the hand returned", "result": str(report or "")[:600],
             "ms": float(ms or 0.0), "at": started_at},
            {"tool": "launch_app", "kind": "action", "app": shown_app if verdict is True else None,
             "target": f'app "{args.get("name")}", watched', "input": {"name": args.get("name"), "watch": True},
             "ok": verdict is not False, "verdict": verdict,
             "why": ("its own window showed" if verdict is True else "its own window did not show" if verdict is False
                     else "the watch stopped before it could tell"),
             "result": str(watch.get("summary") or "")[:600], "ms": float(watch_ms or 0.0),
             "at": started_at + float(ms or 0) / 1000.0},
        ]
        ended = started_at + (float(ms or 0) + float(watch_ms or 0)) / 1000.0
        run = {"source": "hand", "session_id": getattr(memory, "session_id", None), "request_id": request_id,
               "lane": "voice", "at": started_at, "ended_at": ended, "words": " / ".join(words[-2:]),
               "reply": str(watch.get("summary") or "")[:300],
               "outcome": "final" if verdict is True else "unverified", "steps": steps,
               "wall_s": max(0.0, ended - started_at), "app": shown_app, "injected": list(shown), "cited": []}

        def write() -> None:
            try:
                memory.record_run(run)
            except Exception as exc:  # noqa: BLE001 - memory never breaks a hand
                if self.log is not None:
                    self.log.event("hand_trace", op="error", name=name, error=type(exc).__name__)

        self._queue(write)
        if self.log is not None:
            self.log.event("hand_trace", op="queued", name=name, ok=True, verified=verdict, ms=round(ms), steps=2,
                           injected=len(shown))

    def _trace_skill(self, args: dict[str, Any], *, ok: bool, report: str, facts: dict[str, Any], ms: float,
                     call: Any = None) -> None:
        """Keep one learned way's run as a run trace: one step per action (and the look that found the screen did not
        fit), each citing the way (``playbook``), the ways and lessons shown as ``injected``. Content-free log."""
        from yuki.voice.skills import trace_steps

        results = facts.get("results") or []
        item = facts.get("item") or {}
        pid = None
        try:
            pid = int(item.get("id")) if item.get("id") is not None else None
        except (TypeError, ValueError):
            pid = None
        if self.log is not None:
            self.log.event("skill", op="run", skill=pid, ok=bool(ok), verified=facts.get("verified"),
                           steps=facts.get("steps"), steps_done=facts.get("steps_done"),
                           deviated_at=facts.get("deviated_at"), deviation=facts.get("deviation"),
                           refused=bool(facts.get("refused")), ms=round(ms), call=call)
        memory = self.memory
        if memory is None or not callable(getattr(memory, "record_run", None)) or not results:
            return
        request_id, words, shown = self._trace_ids(call)
        steps = trace_steps(results, pid)
        started = min((float(s.get("at") or time.time()) for s in steps), default=time.time())
        outcome = ("final" if ok and facts.get("verified") is True else "held" if facts.get("held") else
                   "refused" if facts.get("act_off") else
                   "handed_over" if facts.get("deviated_at") is not None else "unverified")
        run = {"source": "hand", "session_id": getattr(memory, "session_id", None), "request_id": request_id,
               "lane": "voice", "at": started, "ended_at": started + float(ms or 0) / 1000.0,
               "words": " / ".join(words[-2:]), "reply": str(report or "")[:300], "outcome": outcome,
               "steps": steps, "wall_s": float(ms or 0) / 1000.0, "app": (item.get("apps") or [None])[0],
               "injected": list(dict.fromkeys([*shown, *([pid] if pid is not None else [])])),
               "cited": [pid] if pid is not None else []}

        def write() -> None:
            try:
                memory.record_run(run)
            except Exception as exc:  # noqa: BLE001
                if self.log is not None:
                    self.log.event("hand_trace", op="error", name=SKILL_HAND, error=type(exc).__name__)

        self._queue(write)
        if self.log is not None:
            self.log.event("hand_trace", op="queued", name=SKILL_HAND, ok=bool(ok), steps=len(steps),
                           injected=len(run["injected"]), cited=len(run["cited"]), call=call)

    # -- v3.18: a learned way as a hand ---------------------------------------------------------------------------

    def _skill(self, args: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """Run a learned way the talker was offered (:mod:`yuki.voice.skills`): refused (and handed to the thinker by
        code) when it was not offered, or when its call cannot run as written."""
        from yuki.voice.skills import SkillRefused, SkillRunner

        ref = str(args.get("skill") or "").strip()
        turn = int(self._call_safely(self.user_turn, 0) or 0)
        practice = self.practice
        item = practice.offered(ref, turn) if practice is not None else None
        words = [str(w) for w in (self._call_safely(self.words, []) or []) if str(w).strip()]
        if item is None:
            facts["skill_context"] = (f"I was about to run a learned way ({ref or 'none named'}) with my own hands, but "
                                      f"it was not one offered for these words, so nothing was done.")
            raise _Refused(f"{ref or 'that way'} is not on the Learned ways line you were shown; nothing was done",
                           stale=True)
        facts["item"] = item
        try:
            ok, report, more = SkillRunner(self).run(args, item=item, words=words)
        except SkillRefused as exc:
            facts["skill_context"] = (f"I was about to run my learned way {item.get('ref')} (\"{item.get('intent')}\") "
                                      f"with my own hands, but did not: {exc}. Nothing was done.")
            raise _Refused(f"the learned way was not run: {exc}; nothing was done", stale=True) from None
        facts.update(more)
        return ok, report, facts

    # -- Phase S: one of the user's skills as the talker's hand (yuki.skills; map-skills S3.4, S4.2, S4.3) ----------

    #: The outcomes whose line code says itself (it settles the call: no talker run, no hand-off): a game in front,
    #: Spotify playing on another device, "couldn't confirm", a plain failure, and the resolver's one question.
    SKILL_SAID = ("held_game", "other_device", "unverified", "failed", "asked")
    #: What the talker reads after a skill's line was said by code (nothing more to say).
    _SKILL_SAID_NOTE = ("Said aloud just now by code, word for word: {line} Say nothing more about it now. When they "
                        "answer a question in it, act on their answer (run_skill again with that slot, or keep_usual).")

    async def _handle_skill(self, params: Any) -> None:
        """Phase S: ``run_skill`` (one of the user's skills, :mod:`yuki.skills.engine`) or ``keep_usual``, off the loop.

        "Done" only on a seen result (D19): the done line (the connection's, filled by code) is said by code only when
        the skill's done-check saw the result, with "Keep this as your usual?" after it when the user's words named a
        new usual. A game in front ("after the round?"), Spotify on another device, "couldn't confirm", a failure and
        the resolver's question are said by code too, plainly; a hand-off goes to the thinker by code with the skill
        pinned. The talker gets the report, never a second run to narrate it."""
        from pipecat.frames.frames import FunctionCallResultProperties, TTSSpeakFrame

        name = params.function_name
        args = params.arguments if isinstance(params.arguments, dict) else {}
        started = time.monotonic()
        if name == KEEP_USUAL_HAND:
            ok, line, facts = await asyncio.to_thread(self._keep_usual, args)
            if self.log is not None:
                answer = str(args.get("answer") or "")
                self.log.event("hands", op=name, ok=ok, answer=answer if answer in ("yes", "just_this_time") else "other",
                               pending=bool(facts.get("pending")), unsure=bool(facts.get("unsure")),
                               call=params.tool_call_id)
            if facts.get("unsure") and line:
                await params.result_callback({"not_sure": line, "how_to_say_it": (
                    f"Memory didn't answer in time, so it may or may not be kept. Say this plainly, nothing more: "
                    f"{line}")})
                return
            if ok and line:
                await params.llm.push_frame(TTSSpeakFrame(text=line))
                await params.result_callback({"done": line, "said": line, "how_to_handle_it": _SAID_NOTE},
                                             properties=FunctionCallResultProperties(run_llm=False))
                return
            await params.result_callback({"no_change": line or "nothing was waiting for that answer",
                                          "how_to_say_it": "Nothing was kept. Answer their words as usual."})
            return
        key = _step_key(name, args)
        if key in self._inflight:
            if self.log is not None:
                self.log.event("hands", op=name, deduped=True, running=True, call=params.tool_call_id)
            await params.result_callback({"already": "the same step is running now",
                                          "how_to_handle_it": _DUPLICATE_NOTE},
                                         properties=FunctionCallResultProperties(run_llm=False))
            return
        self._inflight[key] = started
        if args.get("say_first") and self.bridge is not None:
            await self.bridge._ack_if_silent(params, args)
        self.busy += 1
        if self.bridge is not None:
            self.bridge.busy_tools += 1
        try:
            ok, report, facts = await asyncio.to_thread(self._skill_hand, args)
        except Exception as exc:  # noqa: BLE001 - a crash is a failed run
            ok, report, facts = False, f"{type(exc).__name__}: {exc}", {"outcome": "error", "verified": None}
        finally:
            self._inflight.pop(key, None)
            self.busy = max(0, self.busy - 1)
            if self.bridge is not None:
                self.bridge.busy_tools = max(0, self.bridge.busy_tools - 1)
                if self.bridge.gate is not None:
                    self.bridge.gate.poke()
        ms = round((time.monotonic() - started) * 1000)
        prefs = facts.pop("_prefs", None)
        if self.on_action is not None and facts.get("sent_at") is not None:
            self.on_action(facts["sent_at"])   # review r1 L8: a rung acted (the turn log's speech -> action)
        if str(facts.get("outcome") or "") == "handed_over" and facts.get("handoff_skill"):
            # review r1 L2: D17(2) read again right before the hand-off goes out (the engine read it before the
            # ledger and preference work); a game in front now holds it, and a read that fails holds it too
            game = await asyncio.to_thread(self._game_closed)
            if game:
                result = prefs[3] if prefs else None
                if result is not None:
                    memory = self.skill_memory if self.skill_memory is not None else self.memory
                    await asyncio.to_thread(self._refinish, memory, result, "held_game")
                from yuki.skills.resolve import HELD_LINE

                facts.update(outcome="held_game", held="game", refused="game", say=HELD_LINE, handoff_skill=None,
                             settled=True, game_front=True)
                if self.last_skill and self.last_skill.get("run_key") == getattr(result, "run_key", None):
                    self.last_skill["outcome"] = "held_game"
                report = f"Held: a game is in front, nothing was sent. Say: {HELD_LINE}"
        try:
            await self._skill_reply(params, name, args, ok, report, facts, ms)
        finally:
            if prefs is not None:
                # review r1 L4: the preference side runs after the line was said (a bounded memory call)
                try:
                    learned = await asyncio.to_thread(self._learn_from_run, *prefs)
                except Exception:  # noqa: BLE001 - the preference side never breaks a hand
                    learned = {}
                if self.log is not None and learned:
                    self.log.event("hands", op=name, prefs=True, corrected=int(learned.get("corrected") or 0),
                                   still_card=bool(learned.get("still_card")), call=params.tool_call_id)

    async def _skill_reply(self, params: Any, name: str, args: dict[str, Any], ok: bool, report: str,
                           facts: dict[str, Any], ms: float) -> None:
        """The result of one ``run_skill``: the line code says (done only when verified), the hand-off with the skill
        pinned, or the report for the talker."""
        from pipecat.frames.frames import FunctionCallResultProperties, TTSSpeakFrame

        outcome = str(facts.get("outcome") or "")
        verified = facts.get("verified") is True
        line = ""
        if verified:
            line = str(facts.get("done_line") or "")
            if facts.get("keep_question"):
                from yuki.skills.prefs import keep_line

                line = keep_line(line)
        elif outcome in self.SKILL_SAID or facts.get("held") or facts.get("act_off") or facts.get("memory_slow"):
            line = str(facts.get("say") or "")
        if outcome in ("failed", "error") or (facts.get("refused") and not facts.get("held")
                                               and not facts.get("act_off") and not facts.get("memory_slow")
                                               and not facts.get("unsure_focus")):
            self.trouble_at = time.monotonic()   # its guidance rides with the talker's next calls (v3.17)
        self.actions.add("your hand", _what(name, args), report[:400], ok=ok, verified=facts.get("verified"))
        if self.on_step is not None:
            try:
                self.on_step(name, hand_words(name, args, facts.get("app")), bool(ok and verified), report[:400], ms)
            except Exception:  # noqa: BLE001 - the screen never breaks a hand
                pass
        if self.log is not None:
            self.log.event("hands", op=name, ok=ok, ms=ms, report_chars=len(report), call=params.tool_call_id,
                           skill_outcome=outcome or None, verified=facts.get("verified"), done_line=bool(line),
                           keep_question=bool(facts.get("keep_question")), focus_taken=bool(facts.get("focus_taken")),
                           act_off=bool(facts.get("act_off")), memory_slow=bool(facts.get("memory_slow")),
                           unsure_focus=bool(facts.get("unsure_focus")))
        if outcome == "handed_over" and facts.get("handoff_skill") and self.bridge is not None \
                and callable(getattr(self.bridge, "escalate", None)):
            # rung C: its routes didn't get there; the thinker does it with the skill pinned (never with a game in
            # front: read again just before this, D17(2))
            ack = False
            try:
                ack = bool(self.bridge.may_ack()) if callable(getattr(self.bridge, "may_ack", None)) else False
            except Exception:  # noqa: BLE001
                ack = False
            try:
                try:
                    passed = await self.bridge.escalate(params, name, args, report, acked=ack,
                                                        skill=facts["handoff_skill"])
                except TypeError:   # a stand-in bridge without the skill argument
                    passed = await self.bridge.escalate(params, name, args, report)
            except Exception as exc:  # noqa: BLE001
                passed = False
                if self.log is not None:
                    self.log.event("hands", op=name, escalate_error=type(exc).__name__)
            if passed:
                if self.log is not None:
                    self.log.event("hands", op=name, escalated=True, acked=ack, call=params.tool_call_id)
                if ack:
                    self.bridge.acked()
                await params.result_callback({"not_verified": report[:700], "passed_on": True,
                                              "how_to_handle_it": _PASSED_ACK_NOTE if ack else _PASSED_NOTE},
                                             properties=FunctionCallResultProperties(run_llm=ack))
                return
        if line:
            await params.llm.push_frame(TTSSpeakFrame(text=line))
            await params.result_callback({"done" if verified else "result": report[:700], "said": line,
                                          "how_to_handle_it": self._SKILL_SAID_NOTE.format(line=line)},
                                         properties=FunctionCallResultProperties(run_llm=False))
            return
        if facts.get("unsure_focus"):
            await params.result_callback({"held": report[:700], "how_to_handle_it": _UNSURE_FOCUS_NOTE})
            return
        if facts.get("held") and not line:
            question = str(facts.get("question") or "").strip() or "Ask whether they want exactly this."
            await params.result_callback({"held": report[:700],
                                          "how_to_handle_it": _JUDGE_HELD_NOTE.format(question=question)})
            return
        note = _FAILED_NOTE if not ok else _UNCONFIRMED_NOTE
        await params.result_callback({"failed" if not ok else "not_verified": report[:700], "how_to_say_it": note})

    def _skill_request_id(self) -> str | None:
        """The voice turn's id (``<session>-<n>``): one request per user turn, so a model calling a skill twice in one
        turn is never "the user repeating it" (D19)."""
        got = self._call_safely(self.turn_key, None)
        return str(got) if got not in (None, "") else None

    def _skill_log(self, event: str, **fields: Any) -> None:
        if self.log is not None:
            try:
                self.log.event(event, **fields)   # the engine's own content-free D12 lines
            except Exception:  # noqa: BLE001
                pass

    #: The skill verbs that touch an app's window (press a control in it, set its own slider, bring it forward, pick
    #: its tab): refused in an app whose Settings "Act" switch is off, as the old click and type hands are (34-review
    #: H5, review r1 M2). A driver may do any of these, so it is refused there too, unless its route's data marks it
    #: window-free (:meth:`_window_free`). The sound-only verbs (the mixer, the PC's level, the media session) and
    #: openings stay free, as the old sound and open hands were; ``uia_steps`` checks each of its own steps.
    ACT_VIAS = ("uia_invoke", "slider", "focus", "tab_select", "driver")

    @staticmethod
    def _window_free(route: Any) -> bool:
        """Review r2 L1: a route whose data says it neither takes focus nor sends input (``game = "always"`` with
        ``needs_focus = false``: the connection format's own marker, the same one the engine's D17 check reads), e.g.
        sSpotify's mixer, system and Spotify-mixer driver rungs. The Act switch is about clicking and typing in an app,
        so such a rung runs with it off, as the old app_volume did."""
        return getattr(route, "game", None) == "always" and not getattr(route, "needs_focus", True)

    def _skill_verbs(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        """The engine's verbs, each refusing a focus-taking rung while the thinker works on the desktop (two hands at
        once would collide): a no-focus rung (a pattern, the media session, Core Audio) still runs beside it (map-skills
        S3.4). A window-touching rung (:data:`ACT_VIAS`) in an app whose "Act" switch is off is refused
        (``act_off``, the app's name kept in ``state["act_off"]``). ``state["sent_at"]``: when the first rung that
        acted went out (the turn log's speech-to-action time). Looked up when called, so the engine's own table stays
        the one source."""
        from yuki.skills import engine

        state = state if state is not None else {}

        def wrap(via: str) -> Any:
            def run(call: Any) -> Any:
                if state.get("sound_only") and getattr(call.route, "needs_focus", False):
                    # review r2 L3: unsure words and an unheld volume run: no focus rung on this run
                    state["unsure_refused"] = True
                    return engine.RouteResult(ran=False, outcome="unsure_words")
                if via in self.ACT_VIAS and not self._window_free(call.route):
                    try:
                        off = self.act_off(engine.acted_app(call.skill, call.route, call.args))
                    except Exception:  # noqa: BLE001 - an unreadable policy is the judge's and the backstop's
                        off = None
                    if off:
                        state.setdefault("act_off", off)
                        return engine.RouteResult(ran=False, outcome="act_off")
                if getattr(call.route, "needs_focus", False):
                    busy = self.bridge.desk_job() if self.bridge is not None and hasattr(self.bridge, "desk_job") \
                        else None
                    if busy:
                        return engine.RouteResult(ran=False, outcome="desk_busy")
                fn = engine.VERB_FUNCS.get(via)
                if fn is None:
                    return engine.RouteResult(ran=False, outcome="no_verb")
                res = fn(call)
                if getattr(res, "ran", False) is not False and state.get("sent_at") is None:
                    state["sent_at"] = time.monotonic()
                return res
            return run

        return {via: wrap(via) for via in list(engine.VERB_FUNCS)}

    def _judge_skill(self, skill_key: str, slots: dict[str, Any]) -> None:
        """Rule 8 for a skill that sends (its data's ``sends`` / ``sends_when``) or joins a call (its end state is
        ``call_join``, a voice channel): Yuki's own check reads the user's heard words and the policy, as for the
        thinker's send (a join is judged even where the policy would let it through); held, nothing runs (raises
        :class:`_Refused` with ``held``). Any other skill is not judged here (its routes are the user's own usual ways).
        Data only, never the words."""
        try:
            from yuki.skills.connection import load, sends_with

            skill = load().get(skill_key)
            sends = bool(skill is not None and sends_with(skill, slots, partial=True))
            joins = bool(skill is not None and skill.end == "call_join")
        except Exception:  # noqa: BLE001
            return
        if not (sends or joins):
            return
        verdict = self.authorize(SKILL_HAND, {"skill": skill_key, "slots": dict(slots)}, {"app": skill.app},
                                 force=joins and not sends)
        if verdict is not None and not getattr(verdict, "allowed", False):
            raise _Refused(f"held by Yuki's own check: {getattr(verdict, 'why', '') or 'not plainly asked for'}",
                           held=str(getattr(verdict, "question", "") or ""))

    def _skill_hand(self, args: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """Blocking: resolve and run one skill (:func:`yuki.skills.engine.resolve_and_run`), then learn from the user's
        words: a correction naming another option (:func:`yuki.skills.prefs.note_correction`), the pending "Keep this
        as your usual?", a "still your usual?" card. The acting marker goes up only right before a rung that takes
        focus (D18: sound-only runs publish none). ``(ok, report, facts)`` as :meth:`SkillResult.as_voice` gives."""
        facts: dict[str, Any] = {"sent_at": None, "verified": None}
        skill_key = str(args.get("skill") or "").strip()
        slots = dict(args.get("slots")) if isinstance(args.get("slots"), dict) else {}
        # review r2 L3: the volume skill is never held on unsure words (pipeline._sound_skill), so on that run it
        # takes no focus
        sound_only = skill_key == VOLUME_SKILL and bool(self._call_safely(self.unsure, False))
        if self.halted:
            facts.update(refused=True, outcome="refused")
            return False, (f"your last hand did not show the effect it was for ({self.halted}); your hands stay still "
                           f"until the user speaks again. Nothing was done"), facts
        try:
            self._judge_skill(skill_key, slots)
        except _Refused as exc:
            facts.update(refused=True, outcome="refused", held=exc.held is not None, question=exc.held or "")
            return False, str(exc), facts
        from yuki.skills import engine

        memory = self.skill_memory if self.skill_memory is not None else self.memory
        words = [str(w) for w in (self._call_safely(self.words, []) or []) if str(w).strip()]
        marker: dict[str, Any] = {"token": None}
        begin = getattr(memory, "acting_begin", None) if memory is not None else None

        def on_focus() -> None:
            if marker["token"] is not None or not callable(begin) or sound_only:
                return   # sound_only: the focus rung is refused by its verb, nothing takes focus (D18)
            token = f"voice-skill-{id(self)}-{time.monotonic_ns()}"
            try:
                if begin(token, " / ".join(words[-2:]) or skill_key, lane="voice"):
                    marker["token"] = token
            except Exception:  # noqa: BLE001 - the marker never breaks a run
                pass

        previous = self.last_skill if (self.last_skill and self.last_skill.get("skill") == skill_key and
                                       time.time() - float(self.last_skill.get("at") or 0.0) <= _CORRECTION_S) else None
        state: dict[str, Any] = {}
        if sound_only:
            state["sound_only"] = True
        # review r2 M3: a resolve memory doesn't answer in time refuses the run (no hand moves before the user's
        # usuals are known); a memory reached directly (it holds the store) has no wait to miss
        resolve_wait = _ResolveWait(memory, skill_key) if memory is not None and callable(getattr(memory, "call", None)) \
            and not hasattr(memory, "store") else None
        # review r1 L2: the engine's own D9 read fails closed ("unknown" holds), so its reader may raise
        ctx = engine.RunContext(origin="voice", words=words, memory=resolve_wait or memory, hands=self,
                                log=self._skill_log, request_id=self._skill_request_id(),
                                session_id=getattr(memory, "session_id", None), on_focus=on_focus,
                                verbs=self._skill_verbs(state), game=self._game_strict)
        try:
            result = engine.resolve_and_run(skill_key, slots, words, ctx, again=bool(args.get("again")),
                                            settings=self.skill_settings, corrects=bool(args.get("corrects")),
                                            game_answer=ctx.game_now())
        finally:
            if marker["token"] is not None:
                try:
                    memory.acting_end(marker["token"])
                except Exception:  # noqa: BLE001
                    pass
        ok, report, more = result.as_voice()
        facts.update(more)
        facts["focus_taken"] = bool(result.focus_taken)
        facts["sent_at"] = state.get("sent_at")
        if facts.get("held") == "game":
            facts["game_front"] = True
        if resolve_wait is not None and resolve_wait.slow:
            # review r2 M3: memory didn't answer the resolve in time: nothing ran (no rung, no ledger row), said plainly
            facts.update(outcome="refused", refused=True, memory_slow=True, say=MEMORY_SLOW_LINE, settled=True,
                         verified=False, handoff_skill=None)
            return False, (f"Not done: Yuki's memory didn't answer in time, so the skill was not run and nothing "
                           f"moved. Said aloud by code: {MEMORY_SLOW_LINE}"), facts
        off = state.get("act_off")
        acted = any(t.get("outcome") == "contradicted" for t in result.tried or [])
        if off and (result.outcome == "handed_over" or (result.outcome == "failed" and not acted)):
            # review r1 M2: a rung would have touched an app whose "Act" switch is off and nothing else got there:
            # said plainly (the judge's own line), never handed to the thinker (it would not act there either). A
            # failure after a rung that did act keeps its own plain line
            report = self._skill_act_off(result, off, facts, memory)
        elif sound_only and (result.outcome == "handed_over"
                             or (state.get("unsure_refused") and result.outcome == "failed" and not acted)):
            # review r2 L3: on unsure words the volume skill runs unheld, so only its no-focus rungs ran; what is left
            # would take a window (a focus rung or the thinker, whose tell_yuki would be held on these words too):
            # nothing more runs, the talker asks first
            self._refinish(memory, result, "refused")
            facts.update(outcome="refused", refused=True, unsure_focus=True, handoff_skill=None, settled=True,
                         verified=False, say="")
            report = ("Not done: recognition was unsure of their words, and the rest of this would take a window, so "
                      "nothing was done.")
        facts.update(self._keep_from_run(skill_key, result))
        # review r1 L4: the preference side (a bounded memory call) runs after the line is said (_handle_skill)
        facts["_prefs"] = (skill_key, dict(args), dict(slots), result, previous, list(words))
        if result.outcome not in ("refused",):
            self.last_skill = {"skill": skill_key, "values": dict(result.values or {}),
                               "outcome": str(facts.get("outcome") or result.outcome),
                               "options": list(result.options or []), "run_key": result.run_key, "at": time.time()}
        return ok, report, facts

    def _skill_act_off(self, result: Any, off: str, facts: dict[str, Any], memory: Any) -> str:
        """A run refused in an app whose Settings "Act" switch is off (review r1 M2): the judge's plain line, no
        hand-off, and the ledger row says ``refused`` (no hand-off went out; a rung that acted before keeps it).
        Returns the report for the talker."""
        try:
            from yuki.agent.judge import app_off_line

            line = app_off_line(off)
        except Exception:  # noqa: BLE001 - an older brain: the same words
            line = (f"You turned off clicking and typing in {off} (Settings, App access), so I won't click or type "
                    f"there.")
        self._refinish(memory, result, "refused")
        facts.update(outcome="refused", act_off=off, refused=True, say=line, handoff_skill=None, settled=True,
                     verified=False)
        return (f"Not done: Act is switched off for {off} in Yuki's settings, so Yuki does not click, type or press "
                f"keys there; nothing was done there and the thinker will not do it either.")

    def _refinish(self, memory: Any, result: Any, outcome: str) -> None:
        """The run's ledger row rewritten when the voice did not do what the engine ended with (a hand-off that never
        went out: Act off, or a game came to the front). Content-free; never raises."""
        run_key = getattr(result, "run_key", None)
        if not run_key or memory is None:
            return
        acted = any(t.get("outcome") == "contradicted" for t in getattr(result, "tried", None) or [])

        def go(client: Any) -> Any:
            from yuki.skills import registry

            return registry.finish_run(getattr(client, "store", client), run_key, outcome=outcome, checked=None,
                                       acted=acted)

        self._memory_call(memory, "skill_run_finish", go, timeout_s=0.5)

    def _keep_from_run(self, skill_key: str, result: Any) -> dict[str, Any]:
        """The pending "Keep this as your usual?" when the user's words named a new usual and the PC saw it done (no
        memory call: the engine's ``ask_keep``). A newer run lets an older question lapse."""
        out: dict[str, Any] = {}
        keep = result.ask_keep if isinstance(getattr(result, "ask_keep", None), dict) else None
        if keep and result.done and result.outcome == "done":
            self.pending_keep = {"pref": str(keep.get("pref") or ""), "option": str(keep.get("option") or ""),
                                 "skill": skill_key, "run_key": result.run_key, "at": time.time()}
            out["keep_question"] = True
        elif result.outcome not in ("asked",):
            self.pending_keep = None   # a newer run: an older question lapses
        return out

    def _learn_from_run(self, skill_key: str, args: dict[str, Any], slots: dict[str, Any], result: Any,
                        previous: dict[str, Any] | None, words: list[str]) -> dict[str, Any]:
        """The preference side of a run (S4.2, S4.3), in one bounded memory call, after its line was said: a
        correction naming another option writes corrected / rejected; a usual three later choices went against gets
        its one "still your usual?" card. Content-free counts back."""
        out: dict[str, Any] = {}
        memory = self.skill_memory if self.skill_memory is not None else self.memory
        corrects = bool(args.get("corrects"))
        if memory is not None and result.outcome not in ("refused", "asked"):
            quote = " / ".join(words[-2:])[:300] or None

            def go(client: Any) -> Any:
                from yuki.skills.prefs import after_voice_run

                return after_voice_run(getattr(client, "store", client), skill_key, values=dict(result.values or {}),
                                       previous=(previous or {}).get("values"), run_key=result.run_key,
                                       given=list(slots), corrects=corrects, quote=quote)

            got = self._memory_call(memory, "skill_prefs", go)
            if isinstance(got, dict):
                out["corrected"] = len(got.get("corrected") or [])
                out["still_card"] = bool(got.get("still_card"))
        return out

    @staticmethod
    def _memory_call(memory: Any, what: str, fn: Any, timeout_s: float = 1.0) -> Any:
        """``fn(client)`` through a MemoryAccess (bounded) or directly on a client; ``None`` on failure."""
        try:
            call = getattr(memory, "call", None)
            if callable(call) and not hasattr(memory, "store"):
                return call(what, fn, timeout_s=timeout_s)
            return fn(memory)
        except Exception:  # noqa: BLE001
            return None

    def _keep_usual(self, args: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """Blocking: the answer to the pending "Keep this as your usual?" (``yes`` confirms it as the usual;
        ``just_this_time`` keeps it as one choice). ``(ok, the line said, facts)``."""
        pending = self.pending_keep
        answer = str(args.get("answer") or "")
        if not pending or time.time() - float(pending.get("at") or 0.0) > _KEEP_PENDING_S:
            self.pending_keep = None
            return False, "", {"pending": False}
        if answer not in ("yes", "just_this_time"):
            return False, "", {"pending": True}
        memory = self.skill_memory if self.skill_memory is not None else self.memory
        words = [str(w) for w in (self._call_safely(self.words, []) or []) if str(w).strip()]
        quote = " / ".join(words[-2:])[:300] or None
        source = f"skill_run:{pending.get('run_key') or ''}"

        def go(client: Any) -> Any:
            from yuki.skills.prefs import keep_usual

            return keep_usual(getattr(client, "store", client), pending["pref"], pending["option"], answer,
                              quote=quote, source=source)

        got = self._memory_call(memory, "keep_usual", go)
        self.pending_keep = None
        if got is None:
            # review r1 L4: no answer in time (or memory failed): the write may still land on memory's own thread
            return False, "I'm not sure that saved.", {"pending": True, "unsure": True}
        ok = isinstance(got, dict) and bool(got.get("ok"))
        if not ok:
            return False, "I couldn't save that.", {"pending": True}
        return True, ("Okay, that's your usual now." if answer == "yes" else "Okay, just this time."), {"pending": True}

    def skills_now(self, now: float | None = None) -> str | None:
        """The talker's skills-now line (at most 300 characters, in the volatile block after every cache breakpoint):
        the last skill run in this conversation and its values (for "a bit more" and a correction), its options to pick
        from, and a "Keep this as your usual?" still waiting. ``None`` when there is nothing to say."""
        now = time.time() if now is None else float(now)
        parts: list[str] = []
        last = self.last_skill
        if last and now - float(last.get("at") or 0.0) <= _CORRECTION_S:
            values = ", ".join(f"{k} {(v.get('name') if isinstance(v, dict) else v)}"
                               for k, v in (last.get("values") or {}).items() if v not in (None, ""))
            mins = max(0, round((now - float(last.get("at") or now)) / 60))
            parts.append(f"last run_skill {last.get('skill')} ({values or 'no values'}): {last.get('outcome')}, "
                         f"{'just now' if mins == 0 else f'{mins} min ago'}")
            options = [str(o) for o in (last.get("options") or []) if str(o).strip()]
            if options:
                parts.append("its options: " + " | ".join(o[:40] for o in options[:4]))
        pending = self.pending_keep
        if pending and now - float(pending.get("at") or 0.0) <= _KEEP_PENDING_S:
            parts.append("\"Keep this as your usual?\" waits for their answer (keep_usual)")
        if not parts:
            return None
        text = "Skills now: " + "; ".join(parts) + "."
        return text if len(text) <= SKILLS_NOW_CHARS else text[: SKILLS_NOW_CHARS - 1].rstrip() + "…"

    def _window(self, args: dict[str, Any]) -> int:
        """The hwnd a hand names, refused unless the mouth was shown that window."""
        try:
            hwnd = int(args.get("window"))
        except (TypeError, ValueError):
            raise _Refused("give the window's hwnd from the desktop block") from None
        with self._lock:
            known = set(self.last.hwnds)
        if hwnd not in known:
            raise _Refused(f"window {hwnd} is not in the desktop block you were shown; nothing was done", stale=True)
        return hwnd

    def _front(self, hwnd: int) -> str:
        """Bring ``hwnd`` to the front unless it is there (an empty string: it is in front now)."""
        foreground = self._foreground()
        if foreground == hwnd:
            return ""
        result = self.backend.focus_window(hwnd)
        if not getattr(result, "ok", False):
            raise _Refused(f"could not bring window {hwnd} to the front: {getattr(result, 'summary', '')}")
        return "brought it to the front; "

    def _foreground(self) -> int | None:
        try:
            _text, payload, _ms = self._dispatcher.overview_text()
            return int(payload["foreground_hwnd"]) if payload.get("foreground_hwnd") else None
        except Exception:  # noqa: BLE001
            return None

    def _guard_desk(self, name: str) -> None:
        """Refuse a window hand while the thinker works on the desktop; refuse any hand after a wrong effect."""
        if self.halted and name in AUDIO_HANDS:
            # v3.9: sound hands halt too (live 14:18 a failed stop key was followed by a playpause the talker
            # chose itself, then a hand-off: three spoken lines for one pause).
            raise _Refused(f"your last hand did not show the effect it was for ({self.halted}); your hands stay "
                           f"still until the user speaks again, and the thinker already has it. Nothing was done")
        if name in AUDIO_HANDS:
            return
        busy = self.bridge.desk_job() if self.bridge is not None and hasattr(self.bridge, "desk_job") else None
        if busy:
            raise _Refused(f"the thinker is working on the desktop right now ({busy}); two hands at once would "
                           f"collide, so nothing was done. Tell the user it will be done after that, or pass it to "
                           f"the running task with tell_yuki intent follow_up")
        if self.halted:
            raise _Refused(f"your last hand did not show the effect it was for ({self.halted}); your hands stay "
                           f"still until the user speaks again. Nothing was done. Tell them in a few words what the "
                           f"report showed, or hand it to the thinker with the state; never repeat it blindly")

    def _acting_run(self, name: str, args: dict[str, Any],
                    hand_id: str | None = None) -> tuple[bool, str, dict[str, Any]]:
        """:meth:`_run` inside memory's "Yuki is acting" marker (appOpen F3): what a hand puts on screen is recorded as
        Yuki's doing, not the user's (5 Oct the Riot window the open_app hand brought up was logged as the user's own
        for 107 s). Blocking, off the loop; the marker is a small file write. Sound-only hands publish nothing.
        ``hand_id``: the hand's id (phase F): the marker carries it as its ``request_id``, as the hand's trace does."""
        memory = self.memory
        begin = getattr(memory, "acting_begin", None) if memory is not None else None
        token = f"voice-hand-{id(self)}-{time.monotonic_ns()}"
        published = False
        if callable(begin) and name not in AUDIO_HANDS:
            words = [str(w) for w in (self._call_safely(self.words, []) or []) if str(w).strip()]
            extra = {"request_id": hand_id} if hand_id and _takes_kw(begin, "request_id") else {}
            try:
                published = bool(begin(token, " / ".join(words[-2:]) or name, lane="voice", **extra))
            except Exception:  # noqa: BLE001 - the marker never breaks a hand
                published = False
        try:
            return self._run(name, args)
        finally:
            if published:
                try:
                    memory.acting_end(token)
                except Exception:  # noqa: BLE001
                    pass

    def _run(self, name: str, args: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:  # noqa: C901
        """Blocking: validate, act through the backend, verify, return ``(ok, report, facts)``."""
        facts: dict[str, Any] = {"sent_at": None, "verified": None}
        if name == "scroll" and args.get("keep") == "stop":
            return self._stop_scrolling(facts)   # V7: stopping always works (while the thinker works, after trouble)
        try:
            self._guard_desk(name)
            if VOLUME_SKILL in self.skill_keys and (name == "app_volume" or (
                    name == "media_key" and str(args.get("key") or "") in _VOLUME_KEYS)):
                # Phase S (map-skills S5): with the volume skill on, the talker's mixer hand and the volume keys are
                # off (their schemas are gone; a call written anyway is refused here, nothing moved)
                raise _Refused("the volume is run_skill volume in this conversation (code picks their usual control); "
                               "nothing was changed")
            if name != SKILL_HAND:
                self._check_hand(name, args)   # v3.18: a close, a tab close, a held kind: Yuki's own check first
            ok, report, facts = self._act(name, args, facts)
            if not (ok and facts.get("verified") is True) and not facts.get("settled"):
                self.halted = f"{name}: {report[:160]}"
            return ok, report, facts
        except _Refused as exc:
            facts["refused"] = True  # nothing was done: the talker says why ...
            if exc.held is not None:
                facts["held"] = True   # ... or asks the check's question (v3.18)
                facts["question"] = exc.held
            if exc.act_off is not None:
                facts["act_off"] = exc.act_off   # ... or says Act is off for that app in Settings (v3.18)
            if exc.stale:
                # ... unless what it was shown has changed under it (a tab or window no longer where it was): the
                # request still stands and the thinker, which reads the window afresh, gets it by code (v3.10; live
                # 2026-09-28 09:07:52 a stale tab close was narrated in two talker runs before a hand-off).
                facts["stale"] = True
            return False, str(exc), facts

    def _app_at(self, hwnd: Any) -> str | None:
        """The process of a window the last summary listed (a popup: the app memory's Now picture names)."""
        try:
            with self._lock:
                return self.last.apps.get(int(hwnd)) or None
        except (TypeError, ValueError):
            return None

    def _title_shown(self, hwnd: Any) -> str:
        """A window's title as the last summary read it (``""``: not listed)."""
        try:
            want = int(hwnd)
        except (TypeError, ValueError):
            return ""
        with self._lock:
            overview = self.last.overview or {}
        return next((str(w.get("title") or "") for w in overview.get("windows") or []
                     if isinstance(w, dict) and w.get("hwnd") == want), "")

    def _act(self, name: str, args: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:  # noqa: C901
        """One hand, through the backend (raises :class:`_Refused` before doing anything it will not do).

        v3.18: each hand also says what it acted on, for its run trace: ``facts["target"]`` (the control by role and
        name, the window or tab by its title, or what a sound hand moves) and ``facts["app"]`` (the process)."""
        if name == SKILL_HAND:
            return self._skill(args, facts)
        if name in ("scroll", "window", "tab", "click", "type") and args.get("window") is not None:
            # what the talker was shown (no read here: a hand's speed is the point)
            facts["app"] = self._app_at(args.get("window"))
            title = self._title_shown(args.get("window"))
            facts["target"] = f'Window "{title[:80]}"' if title else None
        if name == "media_key":
            key = str(args.get("key") or "")
            facts["target"] = ("the system volume (a media key: every app at once)" if key in _VOLUME_KEYS
                               else "the media session playing (else the current one)")
        elif name == "app_volume":
            facts["target"] = f"the mixer volume of {str(args.get('app') or '?')[:60]} (that app only)"
            facts["app"] = str(args.get("app") or "") or None
        elif name == "open_url":
            facts["target"] = str(args.get("url") or "")[:160] or None
            facts["app"] = str(args.get("app") or "") or None
        elif name == "open_app":
            facts["target"] = f'the app "{str(args.get("name") or "")[:60]}"'
            facts["app"] = str(args.get("name") or "") or None
        elif name == "open_yuki":
            facts["target"] = f'Yuki\'s own window, page {str(args.get("page") or "")[:20]}'
            facts["app"] = "yuki"
        if name == "media_key":
            key = str(args.get("key") or "")
            if key not in MEDIA_KEYS:
                raise _Refused(f"unknown media key {key!r} ({', '.join(MEDIA_KEYS)})")
            times = max(1, min(int(args.get("times") or 1), 50))
            want = str(args.get("want") or "").strip().lower() if key == "playpause" else ""
            # fGate (D17(4)): transport through the media session's own command, aimed at one session and checked on
            # that same session; the system volume through the Core Audio endpoint. Neither is input nor takes focus,
            # so both work with a game in front. A key stands in only when that route is missing (a stand-in desk) or
            # failed, and never with a game in front (the input backstop would refuse it anyway).
            routed = (self._media_command(key, want, times, facts) if key in _TRANSPORT_KEYS
                      else self._endpoint_volume(key, times, facts))
            if routed is not None:
                ok, report, facts = routed
                game = "" if (ok and facts.get("verified") is True) or facts.get("settled") else self._game_front()
                if game:
                    # D17(2), review r2 M1: with a game in front nothing is handed to the thinker (no model pass, no
                    # read of the window in front, no free-form step): no session to aim at, a session that refused,
                    # or a command whose effect was not seen stays as the report says, and nothing more is tried
                    from yuki.memory.gamegate import game_name

                    facts["game_front"] = True
                    if ok:
                        facts["settled"] = True
                    else:
                        facts["refused"] = "game"
                    report += f"; {game_name(game)} is in front, so nothing more is tried until you leave it"
                return ok, report, facts
            game = self._game_front()
            if game:
                from yuki.memory.gamegate import refusal_text

                facts["verified"] = False
                facts["refused"], facts["game_front"] = "game", True
                return False, (f"{refusal_text(game, 'front')}; Windows' media controls could not take it either, so "
                               f"nothing was sent"), facts
            before = self._media_status() if key in _TRANSPORT_KEYS else None
            volume = self._volume() if key in _VOLUME_KEYS else None
            if key in _TRANSPORT_KEYS and before is None:
                # No media session shows what is playing (a video in a browser that just started often has none
                # yet; live 2026-09-27 14:18 "media: none" while YouTube played): a key could be neither aimed nor
                # checked, and a blind toggle can undo what they want. Nothing is pressed; the thinker uses the
                # page's own control (v3.9).
                facts["verified"] = False
                return False, ("no media session shows what is playing, so a media key could not be aimed or "
                               "checked; nothing was pressed"), facts
            if want and before is not None and before[1].lower() == want:
                # playpause toggles: pressing it on a session already in the state they want undoes it (live, a
                # "pause" on paused music resumed it and said "Paused."). The session's own status, read first.
                facts["settled"] = True
                return True, f"media: {before[0]} is already {before[1]}; nothing was pressed", facts
            facts["sent_at"] = time.monotonic()
            result = self.backend.press(key, times=times)
            ok, report, facts = self._report(result, "", facts)
            if _detail(result, "refused") == "game":
                facts["refused"] = "game"   # D17(2): a game came to the front before the key: never handed over
            if ok and before is not None:
                changed, words = self._media_after(before)
                report += "; " + words
                now = self._media_status() if want else None
                # With the state they want known, verified means it is in that state now, not only that it changed.
                facts["verified"] = changed and (not want or (now is not None and now[1].lower() == want))
            elif ok and volume is not None:
                changed, words = self._volume_after(volume)
                report += "; " + words
                facts["verified"] = changed
            return ok, report, facts
        if name == "scroll" and args.get("keep") == "start":
            return self._keep_scrolling(args, facts)   # V7: on and on, until they say stop
        if name == "scroll":
            hwnd = self._window(args)
            direction = str(args.get("direction") or "down")
            notches = max(1, min(int(args.get("amount") or 5), 50))
            lead = self._front(hwnd)
            x, y = self._scroll_point(hwnd)
            facts["sent_at"] = time.monotonic()
            result = self.backend.scroll(x, y, dy=notches if direction == "up" else -notches, expect_hwnd=hwnd)
            ok, report, facts = self._report(result, lead, facts)
            facts["verified"] = _detail(result, "verified")
            if _detail(result, "at_end") or any(isinstance(x, dict) and x.get("at_end")
                                                for x in (_detail(result, "scrolls") or [])):
                facts["settled"] = True  # already at the end: nothing more can happen, nothing to hand over
            if ok and facts["verified"] is None and _detail(result, "unverifiable") == "page":
                # V6: a web page that does not report its scroll: expected-unverifiable, not trouble to hand over
                from yuki.agent.finish import PAGE_SCROLL_UNVERIFIABLE

                facts["expected"] = facts["settled"] = True
                facts["why"] = PAGE_SCROLL_UNVERIFIABLE
            return ok, report, facts
        if name == "window":
            hwnd = self._window(args)
            action = str(args.get("action") or "")
            if action == "close" and not str(args.get("named") or "").strip():
                # A whole window holds everything in it: closed only when the user asked for exactly that
                # (live 14:20 the talker offered to close all of Chrome because one tab would not close).
                raise _Refused("closing a whole window is only for when the user asked to close that window: put "
                               "their words asking for it in named. Nothing was closed; if a tab would not close, "
                               "the thinker handles it")
            act = getattr(self.backend, "window_action", None)
            if not callable(act):
                raise _Refused("this desktop has no window hand")
            facts["sent_at"] = time.monotonic()
            ok, report, facts = self._report(act(hwnd, action), "", facts)
            # window_action's ok is the new state observed (focus: the window in front).
            facts["verified"] = ok
            return ok, report, facts
        if name == "tab":
            return self._tab(args, facts)
        if name == "click":
            return self._click(args, facts)
        if name == "type":
            return self._type(args, facts)
        if name == "app_volume":
            return self._app_volume(args, facts)
        if name == "open_url":
            return self._open_url(args, facts)
        if name == "open_app":
            query = str(args.get("name") or "").strip()
            if not query:
                raise _Refused("no app name given")
            facts["sent_at"] = time.monotonic()
            result = self.backend.launch_app(query)
            ok, report, facts = self._report(result, "", facts)
            # appOpen F2 (5 Oct): launch_app says itself whether the app's own window is in front
            # (details["verified"]); its ok is only "nothing failed". A launcher's window (the Riot Client's tray
            # window for "open Valorant"), no window, or a window it cannot recognise is never "verified".
            details = getattr(result, "details", None)
            if isinstance(details, dict) and "verified" in details:
                own = details.get("verified")
                facts["verified"] = own if own is None else bool(own) and ok
            else:
                facts["verified"] = ok   # a backend that does not report it (stubs written before appOpen)
            if _detail(result, "process_name"):
                facts["app"] = str(_detail(result, "process_name"))
            if ok and _detail(result, "starting") and isinstance(_detail(result, "launcher"), dict):
                # a game handed to its launcher, no window of it yet: said as starting, nothing handed over; a light
                # watch reports its window when it shows (or what came up instead) to the action log and the trace
                facts.update(starting=True, settled=True, launcher=dict(_detail(result, "launcher")))
            return ok, report, facts
        if name == "open_yuki":
            # Yuki's own window (v3.15), through the tray's opener: the page is the model's choice from the enum.
            from yuki.ui.own_window import open_page

            facts["sent_at"] = time.monotonic()
            ok, report = open_page(str(args.get("page") or ""))
            facts["verified"] = ok   # brought forward or started, on that page
            return ok, report, facts
        raise _Refused(f"no hand called {name!r}")

    def _now_board(self) -> dict[str, Any] | None:
        """Memory's Now picture (:attr:`now_source`), or ``None`` (no source, no service, no answer in time)."""
        source = self.now_source
        if source is None:
            return None
        try:
            board = source()
        except Exception:  # noqa: BLE001 - then the windows are read here
            return None
        return board if isinstance(board, dict) else None

    def _now_items(self, hwnd: int, title: str, board: dict[str, Any] | None = None
                   ) -> tuple[ScreenItems, dict[str, Any], bool] | None:
        """The window's names from memory's Now picture, when its entry is fresh (read after its last change).

        ``(items, tree, whole)``: the items have no element ids (``read`` 0: a click finds its item in a read of its
        own), the tree is the names as list items, for the screen names; ``whole`` says the entry kept every control
        name the window had (fewer than :data:`NOW_CONTROLS_CAP`). None: no service, no answer in 50 ms, no entry or a
        stale one (then the caller reads the window itself).
        """
        board = self._now_board() if board is None else board
        entry = next((w for w in (board or {}).get("windows") or []
                      if isinstance(w, dict) and int(w.get("hwnd") or 0) == int(hwnd)), None)
        if not entry or entry.get("stale") or entry.get("read_at") is None or entry.get("blocked"):
            return None
        controls = list(entry.get("controls") or [])
        names: list[str] = []
        for name in [*controls,
                     *(str(m.get("sender") or "") for m in entry.get("messages") or [] if isinstance(m, dict))]:
            name = " ".join(str(name or "").split())
            if name and name != "the user" and name.casefold() not in {n.casefold() for n in names}:
                names.append(name)
        items = ScreenItems(hwnd=int(hwnd), title=title, read=0,
                            items=[{"id": None, "role": "", "name": n} for n in names[:ITEMS_CAP]],
                            at=time.monotonic(), version=self.actions.version)
        tree = {"elements": [{"id": i, "role": "ListItem", "name": n, "depth": 1, "parent": -1,
                              "is_interactive": True} for i, n in enumerate(names)]}
        return items, tree, len(controls) < NOW_CONTROLS_CAP

    def _read_tree(self, hwnd: int) -> tuple[dict[str, Any], int, float] | None:
        """One bounded UI Automation read of ``hwnd`` here, through the hands' dispatcher: ``(tree, read, ms)``, or
        ``None``. Blocking (a few hundred ms at most); off every loop. Its element ids are good for that read only."""
        try:
            outcome = self._dispatcher.dispatch("look_at_window", {"hwnd": int(hwnd)})
        except Exception:  # noqa: BLE001 - the items are a hint
            return None
        self._read_ms[int(hwnd)] = float(outcome.elapsed_ms or 0.0)
        if not outcome.ok or not isinstance(outcome.payload, dict):
            return None
        latest = getattr(self._dispatcher, "_latest_trees", {}).get(int(hwnd))
        return outcome.payload, (int(latest[0]) if latest else 0), float(outcome.elapsed_ms or 0.0)

    def _read_items(self, hwnd: int, title: str, *, own: bool = False) -> Any:
        """The window in front's items: kept as :attr:`items`; returns the tree (or None).

        D7 (v3.17, 07 §6 I): from memory's Now picture first (a pipe answer in a few ms; the watcher reads the window in
        front on every settle), whenever its entry is fresh and whole (or the window is heavy, v3.15). Otherwise one
        bounded tree read here, as before (live 2 Oct: 22 reads of ~220 ms in 165 s of talk, inside the voice's process).
        ``own`` reads it here regardless (a click finding its item). Blocking; call it off every loop. One at a time.
        """
        with self._items_lock:
            why = "own"
            if not own:
                heavy = self._read_ms.get(int(hwnd), 0.0) >= HEAVY_READ_MS
                started = time.perf_counter()
                got = self._now_items(hwnd, title)
                if got is not None and (got[2] or heavy):
                    items, tree, whole = got
                    with self._lock:
                        self.items = items
                    if self.log is not None:
                        self.log.event("hands", op="items_read", hwnd=int(hwnd), items=len(items.items),
                                       ms=round((time.perf_counter() - started) * 1000), source="now",
                                       whole=whole, heavy=heavy)
                    return tree
                why = "no_now" if got is None else "now_capped"
            read = self._read_tree(hwnd)
            if read is None:
                return None
            payload, number, ms = read
            items = ScreenItems(hwnd=int(hwnd), title=title, read=number, items=items_from_tree(payload),
                                at=time.monotonic(), version=self.actions.version)
            with self._lock:
                self.items = items
            if self.log is not None:
                self.log.event("hands", op="items_read", hwnd=int(hwnd), items=len(items.items), ms=round(ms),
                               source="uia", why=why)
            return payload

    def _extra_lines(self, snap: Snapshot, board: dict[str, Any] | None) -> list[str]:
        """F1 (v3.17, 07 §6 F): popups and other visible windows from memory's Now picture, numbered for the click hand.

        Live 2 Oct "Restore the pages." went to the thinker (5.7 s to its click) because Chrome's "Restore pages?"
        bubble is a window of its own, never on the items line. Popups first (what just appeared, newest first), then
        the windows not in front that can mostly be seen; each with its control names as the board read them, fresh
        reads only. Their hwnds join the summary's windows so the click hand may act on them. Plumbing: what is on the
        screen and how fresh, never what the names say.
        """
        extra: dict[int, ScreenItems] = {}
        lines: list[str] = []
        if not board:
            with self._lock:
                self.extra_items = extra
            return lines
        now = time.monotonic()

        def names_of(entry: dict[str, Any]) -> list[str]:
            out: list[str] = []
            for name in entry.get("controls") or []:
                name = " ".join(str(name or "").split())
                if name and name.casefold() not in {n.casefold() for n in out}:
                    out.append(name)
            return out[:EXTRA_ITEMS]

        def add(hwnd: int, title: str, names: list[str], head: str) -> None:
            items = ScreenItems(hwnd=hwnd, title=title, read=0, items=[{"id": None, "role": "", "name": n}
                                                                       for n in names], at=now,
                                version=self.actions.version)
            extra[hwnd] = items
            snap.hwnds.add(hwnd)
            parts = [f'{n} "{name if len(name) <= ITEM_NAME_CHARS else name[:ITEM_NAME_CHARS - 1] + "…"}"'
                     for n, name in enumerate(names, 1)]
            lines.append(f"{head}, numbered for your click hand: " + "; ".join(parts))

        popups = [p for p in (board.get("popups") or []) if isinstance(p, dict) and p.get("hwnd")]
        popups.sort(key=lambda p: float(p.get("age_s") or 0.0))
        for p in popups:
            if len(extra) >= EXTRA_POPUPS:
                break
            hwnd = int(p["hwnd"])
            names = names_of(p)
            if hwnd == snap.foreground or hwnd in extra or not names:
                continue
            title = " ".join(str(p.get("title") or "").split())[:60]
            body = " ".join(str(p.get("text") or "").split())[:120]
            age = p.get("age_s")
            add(hwnd, title, names, f'popup [{hwnd}] {p.get("app") or ""} "{title}"'
                + (f" saying \"{body}\"" if body else "") + (f" (appeared {round(float(age))} s ago)" if age is not None
                                                            else ""))
        others = 0
        for w in board.get("windows") or []:
            if others >= EXTRA_WINDOWS:
                break
            if not isinstance(w, dict) or w.get("foreground") or w.get("blocked") or w.get("stale"):
                continue
            hwnd = int(w.get("hwnd") or 0)
            if not hwnd or hwnd == snap.foreground or hwnd in extra or float(w.get("share") or 0.0) < EXTRA_MIN_SHARE:
                continue
            names = names_of(w)
            if not names:
                continue
            title = " ".join(str(w.get("live_title") or w.get("title") or "").split())[:60]
            add(hwnd, title, names, f'items in [{hwnd}] {w.get("app") or ""} "{title}" (visible, not in front)')
            others += 1
        with self._lock:
            self.extra_items = extra
        return lines

    def _focus(self, timeout_s: float = 0.25) -> Any:
        """What has the keyboard (the backend's ``focus_snapshot``), or ``None`` when it cannot say."""
        read = getattr(self.backend, "focus_snapshot", None)
        if not callable(read):
            return None
        try:
            snap = read(timeout_s=timeout_s)
        except TypeError:
            snap = read()
        except Exception:  # noqa: BLE001
            return None
        return snap if getattr(snap, "ok", False) else None

    @staticmethod
    def _caret_text(snap: Any) -> str:
        """The text around the caret of a focus read (the Text pattern's, else the field's value), for comparing two
        reads of the same field. Never a password field's."""
        if snap is None or getattr(snap, "is_password", False):
            return ""
        return f"{getattr(snap, 'before', '') or ''}‸{getattr(snap, 'after', '') or ''}" \
            if getattr(snap, "text_source", "") == "text" else str(getattr(snap, "value", "") or "")

    def _mark_focus(self, front: int, payload: dict[str, Any]) -> None:
        """N7 (report 47): as the user starts talking, what has the keyboard in the window in front and the text at
        its caret (another app's text field only, never Yuki's own window). Read on the prefetch thread."""
        self._focus_mark = None
        process = next((str(w.get("process_name") or "") for w in payload.get("windows") or []
                        if isinstance(w, dict) and w.get("hwnd") == front), "")
        if not process or process.casefold() == _own_process_name().casefold():
            return
        snap = self._focus(timeout_s=0.25)
        if snap is None or not getattr(snap, "accepts_text", False) or getattr(snap, "is_password", False):
            return
        title = next((str(w.get("title") or "") for w in payload.get("windows") or []
                      if isinstance(w, dict) and w.get("hwnd") == front), "")
        self._focus_mark = {"at": time.monotonic(), "front": front, "window": int(getattr(snap, "window", 0) or 0),
                            "role": str(getattr(snap, "role", "") or ""), "name": str(getattr(snap, "name", "") or ""),
                            "text": self._caret_text(snap), "process": process, "title": title}

    def _check_dictation(self, foreground: int | None, snap: Any) -> None:
        """N7: the same text field of another app as when they started talking, and its text at the caret changed
        since: they were typing or dictating into that app while they spoke (``dictating``, with when it was seen).
        Measured facts of two focus reads; whether the words were for Yuki stays the talker's judgement."""
        mark = getattr(self, "_focus_mark", None)
        self.dictating = None
        if not mark or snap is None or not foreground or mark["front"] != foreground:
            return
        same = (str(getattr(snap, "role", "") or "") == mark["role"] and str(getattr(snap, "name", "") or "")
                == mark["name"] and int(getattr(snap, "window", 0) or 0) == mark["window"])
        if same and self._caret_text(snap) != mark["text"]:
            self.dictating = {"at": time.monotonic(), "since": mark["at"], "app": mark["process"],
                              "title": mark["title"][:80], "field": mark["name"][:60] or mark["role"]}

    def _focus_line(self, foreground: int | None) -> str:
        """The desktop block's focus line, only when a text field of the window in front has the keyboard (v3.14)."""
        snap = self._focus() if foreground else None
        self._check_dictation(foreground, snap)
        if snap is None or not getattr(snap, "accepts_text", False) or getattr(snap, "is_password", False):
            return ""
        window = int(getattr(snap, "window", 0) or 0)
        if window and window != foreground:
            return ""
        role = str(getattr(snap, "role", "") or "field")
        name = str(getattr(snap, "name", "") or "")[:60]
        what = f'{role} "{name}"' if name else role
        return f"focus: the text field {what} in [{foreground}] has the keyboard (your type hand types there)"

    def _click(self, args: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """Click an item of the items line, by the read it came from; found again, or refused, when that read is old."""
        hwnd = self._window(args)
        with self._lock:
            items = self.items if self.items is not None and self.items.hwnd == hwnd else self.extra_items.get(hwnd)
        try:
            number = int(args.get("item"))
        except (TypeError, ValueError):
            raise _Refused("give the item's number from the items line") from None
        if items is None or items.hwnd != hwnd or not 1 <= number <= len(items.items):
            raise _Refused(f"item {number} of window {hwnd} is not on the items line you were shown; nothing was "
                           f"clicked", stale=True)
        item = items.items[number - 1]
        title = self._title(hwnd)
        if not title and not items.read:
            title = items.title   # a popup is in no window list: its title is the one the board read
        title = title or ""
        if (title != items.title or items.version != self.actions.version or not items.read
                or time.monotonic() - items.at > ITEMS_FRESH_S):
            # The read is old, or the window changed since, or the items came from memory's Now picture (no element
            # ids, v3.15; popups and other windows, v3.17): find the same item (its role and name; the name alone from
            # Now) in a fresh read of that window here.
            found = self._find_item(hwnd, title, item)
            if found is None:
                raise _Refused(f"the window changed since it was read and \"{item['name'][:60]}\" is not on it once "
                               f"now; nothing was clicked", stale=True)
            items, item = found
        role_name = f'{item.get("role") or "control"} "{str(item.get("name") or "")[:80]}"'
        facts["target"] = role_name + (f' in "{title[:60]}"' if title else "")
        facts["control"] = f'{item.get("role") or ""}|{item.get("name") or ""}'   # the trace's control (fx2)
        facts["app"] = facts.get("app") or self._app_at(hwnd)
        self._refuse_if_act_off(facts["app"])          # 34-review H5
        # 34-review H1/H2: a click on a button, a menu item or a link can send, delete or buy: Yuki's own check reads
        # the user's heard words and this call; other clicks only as the user's policy says
        verdict = self.authorize("click", {"hwnd": hwnd, "element": role_name},
                                 {"hwnd": hwnd, "app": facts["app"], "title": title, "element": role_name},
                                 force=str(item.get("role") or "") in RISKY_CLICK_ROLES)
        if verdict is not None and not getattr(verdict, "allowed", False):
            raise _Refused(f"held by Yuki's own check: {getattr(verdict, 'why', '') or 'not plainly asked for'}",
                           held=str(getattr(verdict, "question", "") or ""))
        lead = self._front(hwnd)
        facts["sent_at"] = time.monotonic()
        outcome = self._dispatcher.dispatch("click", {"element": item["id"], "read": items.read, "expect_hwnd": hwnd})
        from yuki.agent.loop import action_verdict

        verified, why = action_verdict("click", outcome)
        facts["verified"] = verified if outcome.ok else False
        facts["action_ms"] = round(outcome.elapsed_ms)
        if not outcome.ok:
            facts["stale"] = True   # nothing was clicked (a newer read came in between): the thinker reads afresh
        return outcome.ok, f"{lead}clicked \"{item['name'][:80]}\" ({item['role']}): {outcome.summary} ({why})", facts

    def _find_item(self, hwnd: int, title: str, item: dict[str, Any]
                   ) -> tuple[ScreenItems, dict[str, Any]] | None:
        """``item`` found again, once, in a fresh read of ``hwnd`` here: ``(that read's items, the item)``, else ``None``.

        By its role (when known) and its name: the whole name, or a name memory's Now picture cut at its length
        (``yuki.memory.extract``'s 80 characters). Every named element of the read is looked at, not only the items line's
        first twenty; an interactive one wins over a label with the same name. Plumbing: identity of strings."""
        read = self._read_tree(hwnd)
        if read is None:
            return None
        payload, number, _ms = read
        name, role = str(item.get("name") or ""), str(item.get("role") or "")
        cut = len(name) >= 80

        def same(e: dict[str, Any]) -> bool:
            have = " ".join(str(e.get("name") or "").split())
            if role and str(e.get("role") or "") != role:
                return False
            return have == name or (cut and have.startswith(name))

        elements = [e for e in (payload.get("elements") or []) if isinstance(e, dict) and isinstance(e.get("id"), int)]
        hits = [e for e in elements if same(e)]
        if len(hits) > 1:
            hits = [e for e in hits if e.get("is_interactive", True)] or hits
        if len(hits) != 1:
            return None
        hit = hits[0]
        found = {"id": int(hit["id"]), "role": str(hit.get("role") or ""), "name": " ".join(str(hit.get("name") or
                                                                                                 "").split())}
        items = ScreenItems(hwnd=int(hwnd), title=title, read=number, items=items_from_tree(payload),
                            at=time.monotonic(), version=self.actions.version)
        with self._lock:
            if self.items is not None and self.items.hwnd == hwnd:
                self.items = items   # the window in front's line keeps up with what was just read
        return items, found

    def _type(self, args: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """Type the words into the text field that has the keyboard in the window named; verified by reading it back."""
        hwnd = self._window(args)
        text = str(args.get("text") or "")
        if not text.strip():
            raise _Refused("no text to type")
        snap = self._focus(timeout_s=0.4)
        if snap is None:
            raise _Refused("which field has the keyboard could not be read; nothing was typed", stale=True)
        window = int(getattr(snap, "window", 0) or 0)
        if window and window != hwnd:
            raise _Refused(f"the keyboard is in another window, not [{hwnd}]; nothing was typed", stale=True)
        if not getattr(snap, "accepts_text", False) or getattr(snap, "is_password", False):
            raise _Refused("what has the keyboard is not a text field you may type in; nothing was typed", stale=True)
        facts["target"] = f'{getattr(snap, "role", "") or "field"} "{str(getattr(snap, "name", "") or "")[:60]}"'
        self._refuse_if_act_off(facts.get("app"))      # 34-review H5
        # 34-review H1/M1: the type hand never presses Enter, so plain words cannot send; a line break in them can
        # (Enter or Ctrl+Enter sends in most chats and mail): then Yuki's own check, with their heard words
        sends = "\r" in text or "\n" in text
        verdict = self.authorize("type_text", {"hwnd": hwnd, "text": text},
                                 {"hwnd": hwnd, "app": facts.get("app"), "title": self._title_shown(hwnd),
                                  "element": facts["target"]}, force=sends)
        if verdict is not None and not getattr(verdict, "allowed", False):
            raise _Refused(f"held by Yuki's own check: {getattr(verdict, 'why', '') or 'not plainly asked for'}",
                           held=str(getattr(verdict, "question", "") or ""))
        facts["sent_at"] = time.monotonic()
        outcome = self._dispatcher.dispatch("type_text", {"text": text, "expect_hwnd": hwnd})
        from yuki.agent.loop import action_verdict

        verified, why = action_verdict("type_text", outcome)
        facts["verified"] = verified if outcome.ok else False
        facts["action_ms"] = round(outcome.elapsed_ms)
        return outcome.ok, f"typed into {getattr(snap, 'role', '') or 'the field'}: {outcome.summary} ({why})", facts

    def _title(self, hwnd: int) -> str:
        """The window's title now, from the window list (``""`` when it is not listed)."""
        try:
            _text, payload, _ms = self._dispatcher.overview_text()
        except Exception:  # noqa: BLE001
            return ""
        return next((str(w.get("title") or "") for w in payload.get("windows") or []
                     if isinstance(w, dict) and w.get("hwnd") == hwnd), "")

    def _app_volume(self, args: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        act = getattr(self.backend, "app_volume", None)
        if not callable(act):
            raise _Refused("this desktop has no app volume hand")
        app = str(args.get("app") or "").strip()
        if not app:
            raise _Refused("name the app as the app volumes line shows it")

        def number(key: str) -> float | None:
            try:
                return float(args[key]) if args.get(key) is not None else None
            except (TypeError, ValueError):
                return None

        mute = args.get("mute") if isinstance(args.get("mute"), bool) else None
        level, change = number("level"), number("change")
        if level is None and change is None and mute is None:
            raise _Refused("give change (points), level (0-100) or mute")
        facts["sent_at"] = time.monotonic()
        result = act(app, level=level, change=change, mute=mute)
        ok, report, facts = self._report(result, "", facts)
        facts["verified"] = bool(_detail(result, "verified")) if ok else False
        if ok and _detail(result, "noop"):
            # N1: nothing moved (already at the level or its end): no done line, no hand-off; the talker says it
            facts.update(noop=True, settled=True, verified=None)
        return ok, report, facts

    def _tab(self, args: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        action = str(args.get("action") or "show")
        if action == "close":
            return self._tab_close(args, facts)
        if action == "reopen":
            return self._tab_reopen(args, facts)
        hwnd = self._window(args)
        select = getattr(self.backend, "select_tab", None)
        if not callable(select):
            raise _Refused("this desktop has no tab hand")
        try:
            index = int(args.get("tab"))
        except (TypeError, ValueError):
            raise _Refused("give the tab's number from the tabs line") from None
        with self._lock:
            shown = list(self.last.tabs.get(hwnd) or [])
        if not 1 <= index <= len(shown):
            raise _Refused(f"tab {index} of window {hwnd} is not on the tabs line you were shown; nothing was done",
                           stale=True)
        facts["target"] = f'TabItem "{shown[index - 1][:80]}"'
        facts["sent_at"] = time.monotonic()
        result = select(hwnd, index, expect_name=shown[index - 1])
        ok, report, facts = self._report(result, "", facts)
        facts["verified"] = bool(_detail(result, "verified")) if ok else False
        if ok and self._foreground() != hwnd:
            report = self._front(hwnd) + report
        return ok, report, facts

    def _tab_close(self, args: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """Close exactly the tabs named by their numbers on the tabs line, each by its identity."""
        hwnd = self._window(args)
        act = getattr(self.backend, "tab", None)
        if not callable(act):
            raise _Refused("this desktop cannot close a tab by its identity; hand it to the thinker")
        numbers = [args.get("tab")] if args.get("tab") is not None else []
        numbers += list(args.get("tabs") or [])
        try:
            wanted = sorted({int(n) for n in numbers})
        except (TypeError, ValueError):
            raise _Refused("give the tabs' numbers from the tabs line") from None
        with self._lock:
            names = list(self.last.tabs.get(hwnd) or [])
            ids = list(self.last.tab_ids.get(hwnd) or [])
        if not wanted:
            raise _Refused("name the tab (or tabs) to close by the numbers on the tabs line; nothing was closed")
        if not str(args.get("named") or "").strip():
            raise _Refused("say in named which of the user's words name each tab to close; a tab their words do "
                           "not name is never closed. Nothing was closed")
        if any(not 1 <= n <= len(names) for n in wanted):
            raise _Refused(f"not every tab {wanted} of window {hwnd} is on the tabs line you were shown; nothing "
                           f"was closed", stale=True)
        if any(not ids[n - 1] for n in wanted):
            raise _Refused("those tabs have no identity to close them by; hand it to the thinker. Nothing was closed")
        facts["target"] = "TabItem " + ", ".join(f'"{names[n - 1][:60]}"' for n in wanted)
        facts["sent_at"] = time.monotonic()
        closed, failed, lines = [], [], []
        for n in wanted:
            result = act(hwnd, ids[n - 1], "close")
            good = bool(getattr(result, "ok", False)) and bool(_detail(result, "verified"))
            (closed if good else failed).append(names[n - 1])
            lines.append(str(getattr(result, "summary", "")))
            facts["action_ms"] = round(float(getattr(result, "elapsed_ms", 0.0) or 0.0))
        facts["verified"] = bool(closed) and not failed
        report = "; ".join(lines)
        if closed:
            report = ("closed, each by its identity and verified gone: " + ", ".join(f'"{x}"' for x in closed)
                      + (f"; NOT closed: {', '.join(repr(x) for x in failed)}" if failed else "") + f". ({report})")
        return bool(closed) and not failed, report, facts

    def _tab_reopen(self, args: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """The browser's own reopen of its most recently closed tab, verified against the title wanted."""
        hwnd = self._window(args)
        read = getattr(self.backend, "read_tabs", None)
        expect = str(args.get("expect") or "").strip()
        if not callable(read):
            raise _Refused("this desktop cannot read its tabs to check a reopened one; hand it to the thinker")
        if not expect:
            raise _Refused("give expect: the title of the tab that was closed (the done lines have it)")
        before = read(hwnd, budget_s=0.4)
        seen = {(getattr(t, "tab_id", "") or t.name) for t in getattr(before, "tabs", [])}
        facts["target"] = f'TabItem "{expect[:80]}" (reopened)'
        lead = self._front(hwnd)
        facts["sent_at"] = time.monotonic()
        result = self.backend.hotkey("ctrl", "shift", "t", expect_hwnd=hwnd)
        ok, report, facts = self._report(result, lead, facts)
        if not ok:
            facts["verified"] = False
            return False, report, facts
        deadline = time.monotonic() + _REOPEN_WAIT_S
        tab = None
        while True:
            strip = read(hwnd, budget_s=0.3)
            new = [t for t in getattr(strip, "tabs", []) if (getattr(t, "tab_id", "") or t.name) not in seen]
            tab = next((t for t in new if t.selected), new[0] if new else None)
            if (tab is not None and same_tab(expect, tab.name)) or time.monotonic() >= deadline:
                break
            time.sleep(_POLL_S * 2)
        if tab is None:
            facts["verified"] = False
            return False, f"pressed the browser's reopen, but no tab came back in {_REOPEN_WAIT_S:g} s", facts
        right = same_tab(expect, tab.name)
        facts["verified"] = right
        where = f"tab {tab.index} of {len(getattr(strip, 'tabs', []))}, \"{tab.name}\""
        if right:
            return True, f"reopened the most recently closed tab (the browser's own reopen): {where}, the one wanted", facts
        return False, (f"the browser's reopen brought back {where}, which is NOT the one wanted (\"{expect}\"); it is "
                       f"left as it is"), facts

    def _open_url(self, args: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        url = str(args.get("url") or "").strip()
        if not url:
            raise _Refused("no address given")
        app = str(args.get("app") or "").strip() or None
        facts["sent_at"] = time.monotonic()
        result = self.backend.open_url(url, app=app) if app else self.backend.open_url(url)
        ok, report, facts = self._report(result, "", facts)
        details = getattr(result, "details", None) or {}
        page = details.get("page") if isinstance(details.get("page"), dict) else {}
        hwnd = details.get("hwnd")
        if ok and not details.get("page_targets") and "page_verified" not in details:
            # Not a web page (a Settings page, a folder, a file): what can be verified is that its window came
            # up in front (v3.6; live 2026-09-27 "open Bluetooth settings" went to the thinker instead). The
            # report says which window; with none seen (the handler's window was already in front and kept its
            # title), it stays unverified and says so.
            facts["verified"] = True if hwnd else None
            return ok, report, facts
        if not ok or details.get("page_verified") is not True:
            facts["verified"] = False if details.get("page_verified") is False else None
            return ok, report, facts
        # At the address is not loaded: a page gets its title once it has arrived.
        title = str(page.get("title") or "")
        titled = bool(title) and title != str(page.get("url") or "")
        waited = time.monotonic()
        deadline = waited + _TITLE_WAIT_S
        can_read = callable(getattr(self.backend, "read_tabs", None))
        while True:
            tab = self._selected_tab(hwnd)
            if not titled and tab is not None and tab[1] and not _is_address(tab[1]):
                titled, title = True, tab[1]
            if titled or not can_read or time.monotonic() >= deadline:
                break
            time.sleep(_POLL_S * 2)
        facts["title_wait_ms"] = round((time.monotonic() - waited) * 1000)
        facts["verified"] = titled
        # The verified facts first (they are what the action log and the thinker need), then the
        # opening's own report.
        where = f"it is tab {tab[0]} of {tab[2]} in window {hwnd}, \"{tab[1]}\", now showing; " if tab else ""
        state = (f"the page has its title (\"{title}\"): loaded" if titled
                 else f"after {_TITLE_WAIT_S:g} s the page still has no title: not loaded yet")
        return ok, f"opened {url}: {where}{state}. ({report})", facts

    def _selected_tab(self, hwnd: Any) -> tuple[int, str, int] | None:
        """``(place, name, count)`` of the tab ``hwnd`` shows, or ``None``."""
        read = getattr(self.backend, "read_tabs", None)
        if not callable(read) or not hwnd:
            return None
        try:
            strip = read(int(hwnd), budget_s=0.3)
        except Exception:  # noqa: BLE001
            return None
        tab = getattr(strip, "selected", None)
        return (tab.index, tab.name, len(strip.tabs)) if tab is not None else None

    def _game_front(self) -> str:
        """D9 for the hands: the game in front ("" none): the backend's own answer (a test desk's ``game_front``),
        the real desktop's local read (:func:`yuki.memory.gamegate.game_in_front`), else none (a stand-in desk never
        reads the real screen, rule 7)."""
        own = getattr(self.backend, "game_front", None)
        try:
            if callable(own):
                return str(own() or "")
            if _is_real_backend(self.backend):
                from yuki.memory.gamegate import game_in_front

                return game_in_front()
        except Exception:  # noqa: BLE001 - unknown: the input layer's backstop still checks
            return ""
        return ""

    def _game_strict(self) -> str:
        """D9 for the skill engine (review r1 L2): :meth:`_game_front`'s read, but a read that fails raises, so the
        engine's ``RunContext.game_now`` fails closed ("unknown" holds the rung) instead of reading as no game."""
        own = getattr(self.backend, "game_front", None)
        if callable(own):
            return str(own() or "")
        if _is_real_backend(self.backend):
            from yuki.memory.gamegate import game_in_front

            return str(game_in_front() or "")
        return ""

    def _game_closed(self) -> str:
        """:meth:`_game_strict`, failing closed: ``"unknown"`` when the read raised (a hand-off then waits)."""
        try:
            return self._game_strict()
        except Exception:  # noqa: BLE001
            return "unknown"

    def _media_route(self) -> Any:
        """The media session route (``(verb, app, log=) -> MediaCommand``): the backend's own (a test desk's
        ``media_command``), the real desktop's (:func:`yuki.actions.media.command`), else None (keys only)."""
        own = getattr(self.backend, "media_command", None)
        if callable(own):
            return own
        if _is_real_backend(self.backend):
            from yuki.actions.media import command

            return command
        return None

    def _volume_route(self) -> Any:
        """The system volume without a key (``(key, times) -> {"level", "muted"} | None``): the backend's own (a test
        desk's ``endpoint_volume``), the real desktop's (:func:`yuki.actions.media.endpoint_volume`), else None."""
        own = getattr(self.backend, "endpoint_volume", None)
        if callable(own):
            return own
        if _is_real_backend(self.backend):
            from yuki.actions.media import endpoint_volume

            return endpoint_volume
        return None

    def _media_command(self, key: str, want: str, times: int,
                       facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]] | None:
        """A transport media key as the media session's own command (fGate, D17(4)): aimed at one session (the
        playing one, else the current one; never a game's), checked on **that** session (critic M1c). ``None``: no
        such route here, or it failed before reaching a session (the caller may use the key, never over a game)."""
        route = self._media_route()
        if route is None:
            return None
        from yuki.actions.media import verb_for_key

        verb = verb_for_key(key, want) or "toggle"
        log = self.log.event if self.log is not None else None
        facts["sent_at"] = time.monotonic()
        result = route(verb, None, log=log)
        facts["route"] = "media_session"
        facts["media"] = result.facts()
        if not result.reached:
            return None
        name = (result.app_id or "?").split("!")[-1]
        if result.outcome == "no_session":
            # No media session shows what is playing (a video in a browser that just started often has none yet;
            # live 2026-09-27 14:18): nothing could be aimed or checked, and a blind toggle can undo what they want.
            # Nothing is sent; the thinker uses the page's own control (v3.9).
            facts["verified"] = False
            return False, ("no media session shows what is playing, so nothing could be aimed or checked; nothing "
                           "was sent"), facts
        if result.outcome == "already":
            # "pause" on paused music: nothing to do (a toggle would undo it, live "Paused." that resumed it)
            facts["settled"] = True
            return True, f"media: {name} is already {result.status}; nothing was sent", facts
        if result.outcome == "refused":
            facts["verified"] = False
            return False, (f"media: {name}'s media session refused {verb} (it may not offer it); nothing else was "
                           f"sent"), facts
        before = (name, result.status, result.title)
        for _ in range(times - 1 if verb in ("next", "previous") else 0):
            if not route(verb, result.app_id, log=log).sent:   # the rest of the presses, on the same session
                break
        changed, words = self._media_after(before, app_id=result.app_id, verb=verb)
        now = self._media_status(result.app_id) if want else None
        # With the state they want known, verified means that session is in it now, not only that it changed.
        facts["verified"] = changed and (not want or (now is not None and now[1].lower() == want))
        return True, f"sent {verb} to {name}'s media session (Windows' media controls, no key); {words}", facts

    def _endpoint_volume(self, key: str, times: int,
                         facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]] | None:
        """A volume key as the default output's Core Audio level (fGate, D17(4)), read back like the key was.
        ``None``: no such route here, or Core Audio failed (the caller may use the key, never over a game)."""
        route = self._volume_route()
        if route is None:
            return None
        volume = self._volume()
        facts["sent_at"] = time.monotonic()
        after = route(key, times)
        if after is None:
            return None
        facts["route"] = "core_audio"
        if volume is None:
            facts["verified"] = None
            return True, (f"the system volume is now {after.get('level')}%{' (muted)' if after.get('muted') else ''} "
                          f"(Windows' audio endpoint, no key)"), facts
        changed, words = self._volume_after(volume)
        facts["verified"] = changed
        return True, f"set the system volume through Windows' audio endpoint (no key); {words}", facts

    def _media_status(self, app_id: str | None = None) -> tuple[str, str, str] | None:
        """``(app, status, title)`` of one media session: ``app_id``'s (status "closed" when it is gone), else the
        current one (the one the media keys act on); ``None`` when the sessions can't be read (or none is current)."""
        try:
            facts = self._dispatcher.activity_facts()
        except Exception:  # noqa: BLE001
            return None
        if (facts.get("errors") or {}).get("media"):
            return None
        sessions = facts.get("media") or []
        if app_id:
            mine = next((m for m in sessions if isinstance(m, dict) and str(m.get("app_id") or "") == app_id), None)
            if mine is None:
                return (app_id.split("!")[-1], "closed", "")
            return (app_id.split("!")[-1], str(mine.get("status") or "unknown"), str(mine.get("title") or ""))
        current = next((m for m in sessions if isinstance(m, dict) and m.get("current")), None)
        if current is None:
            return None
        return (str(current.get("app_id") or "?").split("!")[-1], str(current.get("status") or "unknown"),
                str(current.get("title") or ""))

    @staticmethod
    def _media_moved(before: tuple[str, ...], now: tuple[str, ...], verb: str) -> bool:
        """Whether a session shows the command's effect: next/previous by a new track (the title, when it was read
        before; else the status, a closed session never counts), the rest by the status; a key (no verb) by any
        change, as before."""
        if not verb:
            return now != before
        if verb in ("next", "previous"):
            if before[2]:
                return bool(now[2]) and now[2] != before[2]
            return now[1] != before[1] and now[1] != "closed"
        return now[1] != before[1]

    def _media_after(self, before: tuple[str, ...], *, app_id: str | None = None, verb: str = "") -> tuple[bool, str]:
        """What the media session says after a transport command or key: ``app_id``'s session (the one the command
        was aimed at, critic M1c: another app's title change never counts), else the current one; watched until it
        shows the effect, at most :data:`_WATCH_S` (a condition poll). A measured fact for the report."""
        deadline = time.monotonic() + _WATCH_S
        after = before
        while time.monotonic() < deadline:
            now = self._media_status(app_id)
            if now is not None and self._media_moved(before, now, verb):
                after = now
                break
            time.sleep(_POLL_S)
        if after == before:
            return False, f"media: {before[0]} still {before[1]} after {_WATCH_S:g} s"
        track = f" \"{after[2]}\"" if len(after) > 2 and after[2] and after[2] != before[2] else ""
        return True, f"media: {after[0]} now {after[1]}{track} (was {before[0]} {before[1]})"

    def _volume(self) -> dict[str, Any] | None:
        read = getattr(self.backend, "master_volume", None)
        try:
            return read() if callable(read) else None
        except Exception:  # noqa: BLE001
            return None

    def _volume_after(self, before: dict[str, Any]) -> tuple[bool, str]:
        deadline = time.monotonic() + _WATCH_S
        after = before
        while time.monotonic() < deadline:
            now = self._volume()
            if now is not None and now != before:
                after = now
                break
            time.sleep(_POLL_S)
        if after == before:
            return False, f"the volume is still {before.get('level')}%{' (muted)' if before.get('muted') else ''}"
        return True, (f"the volume is now {after.get('level')}%{' (muted)' if after.get('muted') else ''} "
                      f"(was {before.get('level')}%{' muted' if before.get('muted') else ''})")

    # -- V7: keep scrolling until they say stop ---------------------------------------------------------------------

    def _keep_scrolling(self, args: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """Start the continuous scroll (:class:`yuki.actions.scrolling.ContinuousScroll`) on a window the talker was
        shown: the first step runs here and is the hand's report; the rest run on their own thread. The scroll is
        claimed before that first step, so a keep stop said during it stops it (review F8)."""
        from yuki.actions.scrolling import (
            PACES, ContinuousScroll, claim_continuous, drop_continuous, input_watch, start_continuous,
            step_result, stopped_words, voice_hold,
        )
        from yuki.agent.finish import app_name

        hwnd = self._window(args)
        direction = "up" if str(args.get("direction") or "down") == "up" else "down"
        pace = str(args.get("pace") or "normal")
        pace = pace if pace in PACES else "normal"
        dy = PACES[pace][1] * (1 if direction == "up" else -1)
        lead = self._front(hwnd)
        x, y = self._scroll_point(hwnd)
        backend = self.backend

        def step() -> Any:
            return step_result(backend.scroll(x, y, dy=dy, expect_hwnd=hwnd))

        title = self._title_shown(hwnd)
        where = f"{app_name(self._app_at(hwnd))} \"{title[:60]}\"" if title else app_name(self._app_at(hwnd))
        watch = input_watch()
        scroller = ContinuousScroll(step, where=where, direction=direction, pace=pace, hold=voice_hold,
                                    took_over=watch, moving=getattr(watch, "moving", None),
                                    on_stop=self._scroll_stopped)
        claim_continuous(scroller)
        facts["sent_at"] = time.monotonic()
        try:
            first = step()
        except Exception:
            drop_continuous(scroller)
            raise
        if not scroller.running:
            # a stop came while the first step ran: it never goes on
            drop_continuous(scroller)
            scroller.steps = 1 if first.ok else 0
            facts["verified"], facts["settled"] = first.moved, True
            return True, f"{lead}{first.report}; {stopped_words(scroller.report())}", facts
        if not first.ok or (first.moved is False and not first.at_end):
            drop_continuous(scroller)
            facts["verified"] = False
            return False, lead + first.report, facts
        if first.at_end:
            drop_continuous(scroller)
            facts["verified"], facts["settled"] = False, True
            return True, (f"{lead}{first.report}: already at the {'bottom' if direction == 'down' else 'top'}, "
                          f"nothing more to scroll"), facts
        scroller.steps = 1
        scroller.stills = 1 if first.still else 0   # a web page that did not move counts toward its end (review F5)
        start_continuous(scroller)
        facts["verified"] = first.moved            # True when the page reports its position; None on a web page
        facts["settled"] = facts["continuous"] = True   # it goes on by itself: nothing to hand over
        if first.moved is not True:
            facts["expected"] = True
        return True, (f"{lead}scrolling {direction} by itself now, a step every {PACES[pace][0]:g} s "
                      f"({first.report}); it pauses while they talk; scroll with keep stop ends it"), facts

    def _stop_scrolling(self, facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """Stop the continuous scroll (V7): its report says where it stopped."""
        from yuki.actions.scrolling import stop_continuous, stopped_words

        report = stop_continuous("voice")
        facts["verified"], facts["settled"] = True, True   # it is not scrolling now, either way
        facts["steps"] = int(report["steps"]) if report else 0
        return True, (stopped_words(report) if report else "nothing was scrolling, so there was nothing to stop"), facts

    def _scroll_stopped(self, report: dict[str, Any]) -> None:
        """A continuous scroll stopped by itself (its own thread): into the action log (the talker's "done on the
        desktop" lines, so it can say so on its next turn) and the session log. Nothing is said by code: a line spoken
        up to 5 minutes later, unprompted, could land in a call or over the user's own talk (review F11; Yuki never
        speaks first)."""
        from yuki.actions.scrolling import stopped_words

        why = str(report.get("why") or "")
        self.actions.add("your hand", "scroll keep (stopped by itself)", stopped_words(report)[:400],
                         ok=why not in ("failed", "error"), verified=None)
        if self.log is not None:
            self.log.event("hands", op="scroll_keep", stopped=why, steps=report.get("steps"),
                           seconds=report.get("seconds"))

    def _scroll_point(self, hwnd: int) -> tuple[int, int]:
        """The window's centre: its content, whatever the cursor is over (plumbing: where to look for it)."""
        with self._lock:
            bounds = self.last.bounds.get(hwnd)
        if not bounds:
            raise _Refused(f"window {hwnd} has no position on screen (minimized?); bring it up first")
        left, top, right, bottom = bounds
        return (left + right) // 2, (top + bottom) // 2

    @staticmethod
    def _report(result: Any, lead: str, facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        ok = bool(getattr(result, "ok", False))
        facts["action_ms"] = round(float(getattr(result, "elapsed_ms", 0.0) or 0.0))
        if _detail(result, "refused") == "game":
            facts["refused"] = "game"   # D17(2): the backstop refused it for a game; never handed to the thinker
        return ok, lead + str(getattr(result, "summary", "")), facts


def same_tab(expected: str, name: str) -> bool:
    """Whether a tab's title is the one expected: most of the expected title's words are in it.

    Titles drift while a page lives (a notification count, a memory note, the video now playing),
    so equality is too strict and "contains" too loose. A measure of overlap, no word list.
    """
    def words(text: str) -> list[str]:
        clean = "".join(ch.casefold() if ch.isalnum() else " " for ch in text or "")
        return [w for w in clean.split() if len(w) >= 3 and not w.isdigit()]

    want, have = words(expected), set(words(name))
    if not want or not have:
        return False
    return sum(1 for w in want if w in have) / len(want) >= 0.6


def _detail(result: Any, key: str) -> Any:
    details = getattr(result, "details", None)
    return details.get(key) if isinstance(details, dict) else None


def _is_address(text: str) -> bool:
    try:
        from yuki.perception.tree import is_address

        return bool(is_address(text))
    except Exception:  # noqa: BLE001
        return False


def hand_words(name: str, args: dict[str, Any], app: Any = None) -> str:
    """A hand's step in the words every run surface uses (``yuki.ui.step_words.describe_hand``: "Closed Slack",
    "Turned Spotify down"), for the thread and the screen; never its tool name or arguments (the user's 3 Oct
    feedback 12 and 14). ``app``: the window's app the hand acted in, when its facts name it."""
    from yuki.ui.step_words import describe_hand

    try:
        return describe_hand(name, args, app=app if isinstance(app, str) else None)
    except Exception:  # noqa: BLE001 - presentation only
        return "Did a step"


def _what(name: str, args: dict[str, Any]) -> str:
    """One line naming a hand's action from its arguments (for the shared action log the mouth reads; never shown)."""
    parts = [name]
    for key in ("skill", "window", "tab", "action", "direction", "amount", "keep", "key", "keys", "times", "url", "app",
                "name", "level", "change", "mute", "page"):
        if args.get(key) not in (None, "", []):
            parts.append(f"{key}={args[key]}")
    return " ".join(str(p) for p in parts)


def _takes_kw(fn: Any, name: str) -> bool:
    """Whether ``fn`` names a keyword parameter ``name`` (probed, never assumed)."""
    if not callable(fn):
        return False
    try:
        import inspect

        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def judged_call(name: str, args: dict[str, Any], *, tab_names: list[str] | None = None
                ) -> tuple[str, dict[str, Any]] | None:
    """A talker hand as the thinker's tool for the authorization check (revamp D S1): ``(tool, input)``, whose fixed
    metadata (``yuki.agent.tools.tool_traits``) and the user's stored policy decide whether it is judged at all; the
    hand's words for the user (``say_*``, ``named``) are left out. ``None``: never judged - the media keys (a fixed set
    of sound keys: none can close or send anything) and Yuki's own window."""
    w = args.get("window")
    if name == "window":
        return "window_action", {"hwnd": w, "action": str(args.get("action") or "")}
    if name == "tab":
        action = str(args.get("action") or "show")
        out: dict[str, Any] = {"hwnd": w, "action": action}
        if action == "close":
            out["tabs"] = list(tab_names or [])
        elif action == "reopen":
            out["expect"] = str(args.get("expect") or "")
        elif tab_names:
            out["tab"] = tab_names[0]
        return "tab", out
    if name == "click":
        return "click", {"hwnd": w, "item": args.get("item")}
    if name == "type":
        return "type_text", {"hwnd": w, "text": str(args.get("text") or "")}
    if name == "scroll":   # V7: keep start is the same kind of step, repeated (keep stop never reaches the check)
        return "scroll", {"hwnd": w, "direction": args.get("direction"), "amount": args.get("amount"),
                          **({"until_stopped": True} if args.get("keep") == "start" else {})}
    if name == "open_url":
        return "open_url", {"url": args.get("url"), **({"app": args["app"]} if args.get("app") else {})}
    if name == "open_app":
        return "launch_app", {"query": args.get("name")}
    if name == "app_volume":
        return "app_volume", {k: args[k] for k in ("app", "change", "level", "mute") if args.get(k) is not None}
    return None


def hand_run(trace: dict[str, Any], *, session_id: Any, injected: list[int]) -> dict[str, Any]:
    """One hand's trace (the arguments of ``record_hand_trace``) as the run ``record_run`` keeps, with the playbooks shown
    to the talker for it (``injected``): the same run ``yuki.memory.practice.record_hand_trace`` builds, one step."""
    from yuki.memory.practice import input_summary

    summary, numbers = input_summary(trace.get("args"))
    at = float(trace.get("at") or time.time())
    ms = float(trace.get("ms") or 0.0)
    step = {"tool": trace["name"], "kind": "action", "app": trace.get("app"), "target": trace.get("target"),
            "input": summary, "ok": bool(trace.get("ok")), "verdict": trace.get("verified"), "why": trace.get("why"),
            "result": trace.get("report"), "ms": ms, "at": at}
    return {"source": "hand", "session_id": session_id, "request_id": trace.get("request_id"), "lane": "voice",
            "at": at, "ended_at": at + ms / 1000.0, "words": trace.get("words") or "", "reply": "",
            "outcome": "final" if trace.get("ok") else "error", "steps": [step], "wall_s": ms / 1000.0,
            "numbers": numbers, "app": trace.get("app"), "injected": list(injected)}


class _Refused(Exception):
    """A hand refused before doing anything (the message is the report).

    ``stale``: refused because what the talker was shown changed (a tab or window not where the desktop block
    said): the thinker gets the request by code instead of a talker run narrating the refusal.
    """

    def __init__(self, message: str, *, stale: bool = False, held: str | None = None,
                 act_off: str | None = None) -> None:
        super().__init__(message)
        self.stale = stale
        #: Held by the authorization check (v3.18): its question for the user (``""`` without one); ``None``: not held.
        self.held = held
        #: The app whose "Act" switch is off in Settings (v3.18, 34-review H5), or ``None``.
        self.act_off = act_off


#: Review r2 M3: how long a voice skill run waits for memory's resolve (the user's usuals, the trust ledger, the
#: "again" numbers): longer than the engine's own 1 s (the run is off the loop), and no answer refuses the run.
SKILL_RESOLVE_WAIT_S = 2.0
#: Said by code when memory didn't answer the resolve in time: nothing ran.
MEMORY_SLOW_LINE = "I couldn't reach my memory just now, try again?"


class _ResolveWait:
    """The engine's memory for one voice skill run (review r2 M3): ``skill_resolve`` waits
    :data:`SKILL_RESOLVE_WAIT_S`, and when memory doesn't answer (a timeout or a failure) the run is refused
    (``Refused(skill, "memory_slow")``: nothing runs, no ledger row) instead of resolving with no store, which would
    ignore the confirmed usual, run rungs the ledger disabled and lose the "again" numbers. Every other call and
    attribute is the memory's own (it holds no ``store``, so the engine keeps calling through it)."""

    def __init__(self, inner: Any, skill_key: str, wait_s: float = SKILL_RESOLVE_WAIT_S) -> None:
        self._inner = inner
        self._skill_key = skill_key
        self._wait_s = float(wait_s)
        #: Whether the resolve got no answer (the run was refused for it).
        self.slow = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def call(self, what: str, fn: Any, timeout_s: float = 1.0) -> Any:
        if what != "skill_resolve":
            return self._inner.call(what, fn, timeout_s=timeout_s)
        try:
            return self._inner.call(what, fn, timeout_s=max(float(timeout_s), self._wait_s))
        except Exception:  # noqa: BLE001 - no answer in time, or memory failed: refused, never resolved blind
            from yuki.skills.resolve import Refused

            self.slow = True
            return Refused(self._skill_key, "memory_slow")


def _step_key(name: str, args: dict[str, Any]) -> str:
    """What a hand acts on, for telling one step from the same step again (its words left out)."""
    import json

    keep = {k: v for k, v in (args or {}).items() if k not in ("say_first", "say_when_done", "named", "then")}
    if isinstance(keep.get("url"), str):
        keep["url"] = keep["url"].strip().rstrip("/").casefold()
    if isinstance(keep.get("name"), str):
        keep["name"] = " ".join(keep["name"].split()).casefold()
    return name + json.dumps(keep, sort_keys=True, default=str)


def hands_backend(runtime: Any) -> Any:
    """The desktop backend the brain's worker acts through (the real one, or a test's stub).

    ``None`` when the runtime has no worker with a backend (a stand-in brain in a test):
    then the mouth has no hands, rather than hands on the real desktop.
    """
    worker = getattr(runtime, "worker", None)
    agent = getattr(worker, "agent", None)
    dispatcher = getattr(agent, "dispatcher", None)
    backend = getattr(dispatcher, "backend", None)
    return with_app_volume(backend)


class _AppVolumeBackend:
    """The real desktop backend plus the per-app volume (:mod:`yuki.actions.app_volume`), for a backend
    that does not have it yet. Everything else is the backend's own."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @staticmethod
    def app_volume(app: str, **kwargs: Any) -> Any:
        from yuki.actions.app_volume import set_app_volume

        return set_app_volume(app, **kwargs)

    @staticmethod
    def audio_apps() -> list[dict[str, Any]]:
        from yuki.actions.app_volume import audio_apps

        return audio_apps()


_OWN_NAME: str | None = None


def _own_process_name() -> str:
    """This process's executable name as the volume mixer lists its audio session (``Yuki.exe`` installed)."""
    global _OWN_NAME
    if _OWN_NAME is None:
        try:
            import os

            import psutil

            _OWN_NAME = psutil.Process(os.getpid()).name()
        except Exception:  # noqa: BLE001
            _OWN_NAME = ""
    return _OWN_NAME


def with_app_volume(backend: Any) -> Any:
    """``backend``, able to set one app's own volume: the real desktop backend gets it from
    :mod:`yuki.actions.app_volume` when it has no ``app_volume`` of its own; any other backend (a
    test's stub) is returned as it is, so a stub never reaches the real mixer (rule 7)."""
    if backend is None:
        return None
    try:
        from yuki.agent.tools import _LazyRealBackend
    except Exception:  # noqa: BLE001
        return backend
    if isinstance(backend, _LazyRealBackend) and "app_volume" not in type(backend).__dict__:
        return _AppVolumeBackend(backend)
    return backend
