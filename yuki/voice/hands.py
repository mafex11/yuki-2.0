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

#: The hands' schemas (Pipecat standard function schema fields).
HAND_TOOLS: list[dict[str, Any]] = [
    {
        "name": "media_key",
        "description": (
            "Your hand: a media key, or the SYSTEM volume (every app at once, 2% per press). playpause, nexttrack, "
            "prevtrack and stop act on the media session the desktop block marks. One app's volume is app_volume. "
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
            "Your hand: ONE APP's own mixer volume (only Chrome, only Spotify), the app as the app volumes line "
            "names it; nothing on the screen is touched. Verified by reading its level back." + _NOW
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
            "say_when_done": _DONE_LINE,
        },
        "required": ["say_first", "url", "say_when_done"],
    },
    {
        "name": "open_app",
        "description": "Your hand: open or bring up an app by its Start menu name. Verified once its window is in front.",
        "properties": {"say_first": _SAY_FIRST,
                       "name": {"type": "string", "description": "The app's name as they said it."},
                       "say_when_done": _DONE_LINE},
        "required": ["say_first", "name", "say_when_done"],
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
HAND_NAMES = tuple(t["name"] for t in HAND_TOOLS)

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


def items_line(items: ScreenItems) -> str:
    """The desktop block's items line (``""`` without items)."""
    if not items.items:
        return ""
    parts = []
    for n, item in enumerate(items.items, 1):
        name = item["name"] if len(item["name"]) <= ITEM_NAME_CHARS else item["name"][:ITEM_NAME_CHARS - 1] + "…"
        parts.append(f'{n} "{name}" {_ROLE_WORDS.get(item["role"], item["role"].casefold())}'.rstrip())
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
        self._dispatcher = Dispatcher(backend, max_tree_elements=300, tree_timeout_s=0.8)
        from yuki.voice.workers import shared

        #: Where the background reads run (yuki.voice.workers): started here, off every loop.
        self._workers = shared()
        #: The window in front's clickable items (``ScreenItems``, v3.14), read with the names; ``None`` before.
        self.items: ScreenItems | None = None
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

    def new_user_turn(self) -> None:
        """The user spoke: the talker may use its hands again (after telling them what happened)."""
        self.halted = None

    # -- the desktop summary ------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """Capture the summary now (blocking, a few ms; call it off the event loop). Never raises."""
        started = time.perf_counter()
        snap = Snapshot(at=time.monotonic(), version=self.actions.version)
        try:
            text, payload, _ms = self._dispatcher.overview_text()
            windows = [w for w in (payload.get("windows") or []) if isinstance(w, dict)]
            snap.hwnds = {int(w["hwnd"]) for w in windows if w.get("hwnd") is not None}
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
            if (items is not None and items.hwnd == snap.foreground
                    and items.title == titles.get(snap.foreground or 0, "")):
                line = items_line(items)
                if line:
                    parts.append(line)
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
        """The apps with sound on the output and their own (mixer) volume, one line ("" without it)."""
        read = getattr(self.backend, "audio_apps", None)
        if not callable(read):
            return ""
        try:
            from yuki.actions.app_volume import audio_line

            return audio_line(read())
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
        try:
            ok, report, facts = await asyncio.to_thread(self._run, name, args)
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
        if confirmed and name in ("open_url", "open_app"):
            self._opened = {k: v for k, v in self._opened.items() if started - v[0] < _OPEN_REPEAT_S}
            self._opened[key] = (time.monotonic(), report)
        speak = bool(line) and confirmed
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
                self.on_step(name, _what(name, args), bool(ok and verified is not False), report[:400], ms)
            except Exception:  # noqa: BLE001 - the screen never breaks a hand
                pass
        if self.log is not None:
            self.log.event("hands", op=name, ok=ok, ms=ms, report_chars=len(report), call=params.tool_call_id,
                           done_line=speak, **{k: v for k, v in facts.items()
                                               if isinstance(v, (int, float, bool)) or v is None})
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
        if name == "press_keys" and ok:
            note, key = _KEYS_NOTE, "report"
        elif ok:
            note, key = _UNCONFIRMED_NOTE, "not_verified"
        else:
            note, key = _FAILED_NOTE, "failed"
        if (((name in INSTANT_HANDS and not facts.get("refused")) or facts.get("stale")) and not facts.get("settled")
                and self.bridge is not None and callable(getattr(self.bridge, "escalate", None))):
            # Tried and did not take effect (not refused, not "already so"): the thinker gets the same request
            # now, by code, with this report and the window read; no talker run narrates the failure (v3.9).
            ack = False
            try:
                ack = bool(self.bridge.may_ack()) if callable(getattr(self.bridge, "may_ack", None)) else False
            except Exception:  # noqa: BLE001
                ack = False
            try:
                try:
                    passed = await self.bridge.escalate(params, name, args, report, acked=ack)
                except TypeError:   # a stand-in bridge without the v3.14 argument
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

    def _run(self, name: str, args: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:  # noqa: C901
        """Blocking: validate, act through the backend, verify, return ``(ok, report, facts)``."""
        facts: dict[str, Any] = {"sent_at": None, "verified": None}
        try:
            self._guard_desk(name)
            ok, report, facts = self._act(name, args, facts)
            if not (ok and facts.get("verified") is True) and not facts.get("settled"):
                self.halted = f"{name}: {report[:160]}"
            return ok, report, facts
        except _Refused as exc:
            facts["refused"] = True  # nothing was done: the talker says why ...
            if exc.stale:
                # ... unless what it was shown has changed under it (a tab or window no longer where it was): the
                # request still stands and the thinker, which reads the window afresh, gets it by code (v3.10; live
                # 2026-09-28 09:07:52 a stale tab close was narrated in two talker runs before a hand-off).
                facts["stale"] = True
            return False, str(exc), facts

    def _act(self, name: str, args: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:  # noqa: C901
        """One hand, through the backend (raises :class:`_Refused` before doing anything it will not do)."""
        if name == "media_key":
            key = str(args.get("key") or "")
            if key not in MEDIA_KEYS:
                raise _Refused(f"unknown media key {key!r} ({', '.join(MEDIA_KEYS)})")
            times = max(1, min(int(args.get("times") or 1), 50))
            before = self._media_status() if key in _TRANSPORT_KEYS else None
            volume = self._volume() if key in _VOLUME_KEYS else None
            want = str(args.get("want") or "").strip().lower() if key == "playpause" else ""
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
            ok, report, facts = self._report(self.backend.launch_app(query), "", facts)
            # launch_app's ok is its window found and in front, ready for input.
            facts["verified"] = ok
            return ok, report, facts
        if name == "open_yuki":
            # Yuki's own window (v3.15), through the tray's opener: the page is the model's choice from the enum.
            from yuki.ui.own_window import open_page

            facts["sent_at"] = time.monotonic()
            ok, report = open_page(str(args.get("page") or ""))
            facts["verified"] = ok   # brought forward or started, on that page
            return ok, report, facts
        raise _Refused(f"no hand called {name!r}")

    def _now_items(self, hwnd: int, title: str) -> tuple[ScreenItems, dict[str, Any]] | None:
        """The window's names from memory's Now picture, when its entry is fresh (read after its last change).

        ``(items, tree)``: the items have no element ids (``read`` 0: a click finds its item in a read of its own),
        the tree is the names as list items, for the screen names. None: no service, no answer in 50 ms, no entry
        or a stale one (then the caller reads the window itself).
        """
        try:
            from yuki.memory.now import shared_client

            client = shared_client()
            board = client.now(wait_s=0.05) if client is not None else None
        except Exception:  # noqa: BLE001 - then the window is read here
            return None
        entry = next((w for w in (board or {}).get("windows") or []
                      if isinstance(w, dict) and int(w.get("hwnd") or 0) == int(hwnd)), None)
        if not entry or entry.get("stale") or entry.get("read_at") is None:
            return None
        names: list[str] = []
        for name in [*(entry.get("controls") or []),
                     *(str(m.get("sender") or "") for m in entry.get("messages") or [] if isinstance(m, dict))]:
            name = " ".join(str(name or "").split())
            if name and name != "the user" and name.casefold() not in {n.casefold() for n in names}:
                names.append(name)
        items = ScreenItems(hwnd=int(hwnd), title=title, read=0,
                            items=[{"id": None, "role": "", "name": n} for n in names[:ITEMS_CAP]],
                            at=time.monotonic(), version=self.actions.version)
        tree = {"elements": [{"id": i, "role": "ListItem", "name": n, "depth": 1, "parent": -1,
                              "is_interactive": True} for i, n in enumerate(names)]}
        return items, tree

    def _read_items(self, hwnd: int, title: str, *, own: bool = False) -> Any:
        """One bounded tree read of ``hwnd`` through the hands' dispatcher: keeps its items, returns the tree (or None).

        Blocking (a few hundred ms at most); call it off every loop. One read at a time. A window measured heavy
        (:data:`HEAVY_READ_MS`) is taken from memory's Now picture instead, when that is fresh (v3.15); ``own``
        reads it here regardless (a click finding its item).
        """
        with self._items_lock:
            if not own and self._read_ms.get(int(hwnd), 0.0) >= HEAVY_READ_MS:
                started = time.perf_counter()
                got = self._now_items(hwnd, title)
                if got is not None:
                    items, tree = got
                    with self._lock:
                        self.items = items
                    if self.log is not None:
                        self.log.event("hands", op="items_read", hwnd=int(hwnd), items=len(items.items),
                                       ms=round((time.perf_counter() - started) * 1000), source="now")
                    return tree
            try:
                outcome = self._dispatcher.dispatch("look_at_window", {"hwnd": int(hwnd)})
            except Exception:  # noqa: BLE001 - the items are a hint
                return None
            self._read_ms[int(hwnd)] = float(outcome.elapsed_ms or 0.0)
            if not outcome.ok or not isinstance(outcome.payload, dict):
                return None
            latest = getattr(self._dispatcher, "_latest_trees", {}).get(int(hwnd))
            read = int(latest[0]) if latest else 0
            items = ScreenItems(hwnd=int(hwnd), title=title, read=read, items=items_from_tree(outcome.payload),
                                at=time.monotonic(), version=self.actions.version)
            with self._lock:
                self.items = items
            if self.log is not None:
                self.log.event("hands", op="items_read", hwnd=int(hwnd), items=len(items.items),
                               ms=round(outcome.elapsed_ms))
            return outcome.payload

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

    def _focus_line(self, foreground: int | None) -> str:
        """The desktop block's focus line, only when a text field of the window in front has the keyboard (v3.14)."""
        snap = self._focus() if foreground else None
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
            items = self.items
        try:
            number = int(args.get("item"))
        except (TypeError, ValueError):
            raise _Refused("give the item's number from the items line") from None
        if items is None or items.hwnd != hwnd or not 1 <= number <= len(items.items):
            raise _Refused(f"item {number} of window {hwnd} is not on the items line you were shown; nothing was "
                           f"clicked", stale=True)
        item = items.items[number - 1]
        title = self._title(hwnd)
        if (title != items.title or items.version != self.actions.version or not items.read
                or time.monotonic() - items.at > ITEMS_FRESH_S):
            # The read is old, or the window changed since, or the items came from memory's Now picture (no element
            # ids, v3.15): find the same item (its role and name; the name alone from Now) in a fresh read here.
            self._read_items(hwnd, title, own=True)
            with self._lock:
                fresh = self.items
            same = [i for i in (fresh.items if fresh is not None and fresh.hwnd == hwnd else [])
                    if (not item["role"] or i["role"] == item["role"]) and i["name"] == item["name"]]
            if len(same) != 1:
                raise _Refused(f"the window changed since it was read and \"{item['name'][:60]}\" is not on it once "
                               f"now; nothing was clicked", stale=True)
            items, item = fresh, same[0]
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

    def _media_status(self) -> tuple[str, str, str] | None:
        """``(app, status, title)`` of the media session the media keys act on, or ``None``."""
        try:
            sessions = self._dispatcher.activity_facts().get("media") or []
        except Exception:  # noqa: BLE001
            return None
        current = next((m for m in sessions if isinstance(m, dict) and m.get("current")), None)
        if current is None:
            return None
        return (str(current.get("app_id") or "?").split("!")[-1], str(current.get("status") or "unknown"),
                str(current.get("title") or ""))

    def _media_after(self, before: tuple[str, ...]) -> tuple[bool, str]:
        """What the media session says after a transport key: watched until it changes, at most
        :data:`_WATCH_S` (a condition poll). A measured fact for the report."""
        deadline = time.monotonic() + _WATCH_S
        after = before
        while time.monotonic() < deadline:
            now = self._media_status()
            if now is not None and now != before:
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


def _what(name: str, args: dict[str, Any]) -> str:
    """One line naming a hand's action from its arguments (for the shared action log)."""
    parts = [name]
    for key in ("window", "tab", "action", "direction", "amount", "key", "keys", "times", "url", "app", "name",
                "level", "change", "mute", "page"):
        if args.get(key) not in (None, "", []):
            parts.append(f"{key}={args[key]}")
    return " ".join(str(p) for p in parts)


class _Refused(Exception):
    """A hand refused before doing anything (the message is the report).

    ``stale``: refused because what the talker was shown changed (a tab or window not where the desktop block
    said): the thinker gets the request by code instead of a talker run narrating the refusal.
    """

    def __init__(self, message: str, *, stale: bool = False) -> None:
        super().__init__(message)
        self.stale = stale


def _step_key(name: str, args: dict[str, Any]) -> str:
    """What a hand acts on, for telling one step from the same step again (its words left out)."""
    import json

    keep = {k: v for k, v in (args or {}).items() if k not in ("say_first", "say_when_done", "named")}
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
