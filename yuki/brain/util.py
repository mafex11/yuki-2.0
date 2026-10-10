"""Helpers the brain keeps when bPrune deletes the old brain (brain-design.md §10.1 bExec row, §10.3; critique M5).

Everything below the first ``Copied verbatim`` banner is a **verbatim copy** of the named lines of a module bPrune
deletes (``agent/{loop,finish,judge,prompt,situation}.py``): the same text, so bPrune can switch each importer
(``skills/uia_route.py``, ``skills/donecheck.py``, ``voice/{hands,bridge,mouth}.py``, ``agent/tools.py``) to this module
with a one-line import change and nothing behaves differently. The copies are diff-checked against their sources (the
bExec report says how). Nothing here may be edited apart from its source until bPrune; the old modules stay the live
ones until then.

Two lines are not copies:

* ``finish_gate``: ``action_verdict`` (from ``loop.py``) names ``finish_gate.PAGE_SCROLL_UNVERIFIABLE``; in ``loop.py``
  that is ``yuki.agent.finish`` imported under that name. Here it is this module itself, which holds the same constant,
  so the copied function needs no edit.
* :func:`load_skills` / :func:`skill`: the catalog shim (M6): ``yuki.skills.catalog.load()`` once phase L ships it,
  else ``yuki.skills.connection.load()``. Every brain module reads skill data through it, never ``connection.load``.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from yuki.agent.tools import ToolOutcome, Traits

# =====================================================================================================================
# Copied verbatim from yuki.agent.finish (yuki/agent/finish.py, main c219a03)
# =====================================================================================================================

# -- yuki/agent/finish.py lines 37-39
#: The verdict reason of an action whose effect its report cannot show by the nature of its target (revamp A8).
EXPECTED_UNVERIFIABLE = ("the field shows no text to read back (expected for this kind of field, not a failure; "
                         "the keys were sent)")


# -- yuki/agent/finish.py lines 43-49
#: V6 (3 Oct): a scroll on a web page that takes no UI Automation scrolling: the wheel went in, and the page does not
#: report where it is (measured on each scroll: no scroll pattern, the point inside a page). Live a page scrolled and
#: was reported as "did not scroll", and Yuki told the user so while they watched it move.
PAGE_SCROLL_UNVERIFIABLE = ("the web page does not report its scroll position here (expected for a page in this app, "
                            "not a failure; the wheel went in)")

VERIFIED, EXPECTED, CONTRADICTED, UNVERIFIED, FAILED = "verified", "expected", "contradicted", "unverified", "failed"


# -- yuki/agent/finish.py lines 78-83
#: The request's outcome levels, strongest first. ``verified``: a step's own check showed it on screen (what was seen is
#: ``seen``); ``likely``: the action ran and its check is weak by the nature of the target (a field with no read-back,
#: a page that does not report its scroll, a control learned to behave so) or a later step backed it; ``unverified``:
#: nothing could show whether it worked (a send not seen in the chat, a key with nothing watched); ``failed``: a step
#: failed or its check showed the effect did not happen, and no later step put it right.
LIKELY = "likely"


# -- yuki/agent/finish.py lines 85-88
#: No action ran: an answer, nothing done on the desktop to grade.
ANSWER = "answer"
#: The levels whose words may be said as they are written (by code, or by the talker without an outcome note).
SAYABLE = frozenset({VERIFIED, LIKELY, ANSWER})


# -- yuki/agent/finish.py lines 95-106
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


# -- yuki/agent/finish.py lines 515-518
#: Said in place of a closing message that claims something got done (or was checked) when no action ran in the
#: request (live 5 Oct run 19: no action, yet "I opened and closed the queue, confirmed").
LINE_NOTHING_DONE = "I haven't done that: nothing was done on screen for it."
LINE_NOT_CHECKED = "I haven't checked that on screen."


# -- yuki/agent/finish.py lines 607-610
def sayable(verdict: dict[str, Any] | None) -> bool:
    """Whether a request's closing words may be said as written: no verdict (an older agent, a stand-in), or one at
    ``likely`` or stronger, or an answer with nothing done."""
    return not isinstance(verdict, dict) or verdict.get("level") in SAYABLE or not verdict.get("level")


# -- yuki/agent/finish.py lines 618-622
def app_name(process: str | None) -> str:
    """A window's app as people say it ("chrome.exe" -> "Chrome"), structurally (no list of apps)."""
    from yuki.core.appnames import prettify

    return prettify(process or "") or "It"


# -- yuki/agent/finish.py lines 730-732
#: How similar the captured message must be to what was typed (difflib's ratio over the two texts, casefolded and
#: whitespace-squashed: a measured similarity; a captured text containing the typed one also counts).
SIMILAR = 0.6


# -- yuki/agent/finish.py lines 739-748
def similar(a: str, b: str) -> float:
    """How alike two message texts are (0-1), casefolded and whitespace-squashed."""
    import difflib

    x, y = " ".join((a or "").split()).casefold(), " ".join((b or "").split()).casefold()
    if not x or not y:
        return 0.0
    if x in y or y in x:
        return 1.0
    return difflib.SequenceMatcher(None, x, y).ratio()


# =====================================================================================================================
# Copied verbatim from yuki.agent.loop (yuki/agent/loop.py, main c219a03)
# =====================================================================================================================

# -- yuki/agent/loop.py lines 287-290
#: Input-watch facts of a key or click report (yuki.actions.input): each says the input changed something the
#: user can see. Plumbing: which fields of the action's own report are its measured effects.
_EFFECT_FACTS = ("foreground_changed", "focus_changed", "foreground_title_changed", "page_changed",
                 "window_state_changed")   # v3.14b: normal / maximized / full screen, before -> after


# -- yuki/agent/loop.py lines 334-415
def action_verdict(name: str, outcome: ToolOutcome) -> tuple[bool | None, str]:
    """Whether an action's own report verifies that it took effect: ``(True | False | None, why)``.

    Read from the report the action layer measured (docs/VOICE.md v3.14), never from its words: a tab's selected
    state, a scroll position, a volume read back, a page at its address, a field read back after typing, a window
    in front, and for keys and clicks the focus, window and page watch (something visibly changed: True; watched
    and nothing changed: False). ``None``: the report cannot show it (a shell command, a key with nothing watched).
    A failed action is False.
    """
    if not outcome.ok:
        return False, "it failed"
    payload = outcome.payload if isinstance(outcome.payload, dict) else {}
    if name == "type_text" and isinstance(payload.get("type_text"), dict):
        # An element-targeted type_text (clicked to focus it first) nests the typing's own report (v3.15: live
        # 30 Sept its read-back "arrived" was missed here, read as "could not be read back" and escalated).
        payload = payload["type_text"]
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    own = details.get("verified")
    change = details.get("screen_change") if isinstance(details.get("screen_change"), dict) else None
    if isinstance(own, bool) and change is not None and change.get("verified") is own and change.get("why"):
        # report 58: the window read before and after the action, compared (yuki.perception.outcome)
        return own, str(change["why"])
    if own is None and change is not None and change.get("undecided"):
        return None, str(change.get("why") or "")   # percFix: the window's account could not decide (a click on nothing)
    if name == "launch_app" and isinstance(own, bool):
        # appOpen F2: launch_app's own verdict is the app's own window in front (never a launcher's window)
        return own, ("the app's own window is in front" if own else
                     "its report says the app's own window is not in front")
    if isinstance(own, bool):
        return own, "its report says it was " + ("seen" if own else "not seen")
    if name == "launch_app" and "verified" in details and own is None:
        # appOpen F2: no window of the app itself yet (a game its launcher is still starting), or one nothing can
        # recognise as its: not done, and not a failure either
        if details.get("starting"):
            return None, "it is still starting through its launcher: no window of the app itself yet"
        if details.get("hwnd") and not details.get("foreground") and \
                "not confirmed" not in str(details.get("window_reason") or ""):
            # second review V3: its own window came up; only bringing it to the front failed
            return None, "its window came up but could not be brought to the front"
        return None, "no window it could recognise as the app's own came up in front"
    if name == "scroll" and details.get("unverifiable") == "page":
        # V6: a web page that takes no UI Automation scrolling never shows its scroll from here: expected, not trouble
        return None, finish_gate.PAGE_SCROLL_UNVERIFIABLE
    if name in ("scroll", "tab", "app_volume", "hover"):
        return None, "its report could not tell whether it took effect"
    if name == "open_url" or (name == "launch_app" and details.get("page_verified") is not None):
        page = details.get("page_verified")
        if page is True:
            return True, "the page is at the address"
        if page is False:
            return False, "the page is not at the address"
        if not details.get("page_targets") and details.get("hwnd"):
            return True, "its window came up in front"
        return None, "the page could not be checked"
    if name in ("launch_app", "focus_window"):
        return True, "the window is in front"
    if name in ("window_action", "write_clipboard"):
        # Their ok is the state read back after acting (the window's state from Windows; the clipboard's text).
        return True, "its report reads back the new state"
    if name == "type_text":
        seen = details.get("readback")
        if seen == "arrived":
            return True, "the field reads back the text"
        if seen == "unreadable":   # the field's own nature (no Value/Text pattern): expected, never a failure
            return None, EXPECTED_UNVERIFIABLE
        return (False, "the field does not hold the text") if seen == "missing" else (
            None, "the field could not be read back")
    if name in ("click", "hotkey", "press"):
        facts = [details.get(k) for k in _EFFECT_FACTS if k in details]
        if any(f is True for f in facts) or details.get("new_windows"):
            return True, "something visibly changed after it"
        if change is not None and change.get("verified") is None and change.get("why"):
            # small8 (report 73, release 7): the window was read before and after and that read could not tell (its
            # content is drawn where it cannot be read, or only things that change on their own changed, such as a
            # status bar's line number): focus, title and page staying the same cannot say the effect did not happen
            # (Page Down in a page or a document moves only what is inside). Live, 7 of 8 Page Ups that moved the
            # view were "contradicted" this way, and the done card said "Didn't work"
            return None, str(change["why"])
        if facts and all(f is False for f in facts):
            return False, "focus, window, its state and page were watched and nothing changed"
        return None, "nothing its report watched could show the effect"
    return None, "its report cannot show the effect"


# =====================================================================================================================
# Copied verbatim from yuki.agent.prompt (yuki/agent/prompt.py, main c219a03)
# =====================================================================================================================

# -- yuki/agent/prompt.py lines 264-268
#: The cache marker of the stable layers (tools, core, profile, rules): kept an hour, not five minutes (v3.11).
#: Bedrock's Claude models take ``ttl: "1h"`` (measured 2026-09-28 on Haiku 4.5, Sonnet 5, Opus 5): Yuki's voice
#: keeps its halves warm between conversations with one cheap read an hour instead of one every few minutes. A
#: write costs 2x the input price instead of 1.25x; reads refresh it. The moving conversation marker stays 5 min.
STABLE_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral", "ttl": "1h"}


# =====================================================================================================================
# Copied verbatim from yuki.agent.judge (yuki/agent/judge.py, main c219a03)
# =====================================================================================================================

# -- yuki/agent/judge.py lines 52-222
#: The rungs, least to most on its own (``yuki.ui.policy.LEVELS``).
LEVELS: tuple[str, ...] = ("suggest", "show_plan", "ask", "do_and_tell")
#: Each kind's default and ceiling (``yuki.ui.policy.KINDS``; a test keeps the two in step).
POLICY_DEFAULTS: dict[str, str] = {"read_screen": "do_and_tell", "open_apps": "do_and_tell", "click_type": "do_and_tell",
                                   "send_messages": "ask", "files": "show_plan", "money": "suggest"}
POLICY_CEILINGS: dict[str, str] = {"read_screen": "do_and_tell", "open_apps": "do_and_tell",
                                   "click_type": "do_and_tell", "send_messages": "ask", "files": "do_and_tell",
                                   "money": "ask"}
#: How each kind and level reads in the judge's request.
KIND_WORDS: dict[str, str] = {"read_screen": "Read the screen", "open_apps": "Open apps and windows",
                              "click_type": "Click and type", "send_messages": "Send messages", "files": "Files",
                              "money": "Money"}
LEVEL_WORDS: dict[str, str] = {"suggest": "Just suggest", "show_plan": "Show me the plan",
                               "ask": "Ask before doing", "do_and_tell": "Do it and tell me"}


def policy_path() -> Path:
    """``%LOCALAPPDATA%\\Yuki\\autonomy.json`` (``yuki.ui.policy.policy_path``)."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki" / "autonomy.json"


def clean_levels(raw: Any) -> dict[str, str]:
    """Every kind's level from the file's ``levels``: anything unknown, malformed or above its ceiling reads as the
    default (the same rule ``yuki.ui.policy.clean`` applies, so a hand edit can never lift a kind past its rule)."""
    levels = raw.get("levels") if isinstance(raw, dict) else None
    levels = levels if isinstance(levels, dict) else {}
    out: dict[str, str] = {}
    for kind, default in POLICY_DEFAULTS.items():
        value = levels.get(kind)
        ok = isinstance(value, str) and value in LEVELS and LEVELS.index(value) <= LEVELS.index(POLICY_CEILINGS[kind])
        out[kind] = value if ok else default
    return out


def clean_apps(raw: Any) -> dict[str, dict[str, Any]]:
    """The per-app "Act" switches from the file's ``apps`` (``yuki.ui.policy.app_acts``' shape): ``{casefolded name:
    {"name": as Settings shows it, "act": bool}}``. Anything malformed is left out (an app not listed may act)."""
    apps = raw.get("apps") if isinstance(raw, dict) else None
    out: dict[str, dict[str, Any]] = {}
    for key, value in (apps.items() if isinstance(apps, dict) else ()):
        name = " ".join(str((value.get("name") if isinstance(value, dict) else None) or key or "").split())
        if not name or not isinstance(value, dict):
            continue
        out[str(key).casefold()] = {"name": name, "act": bool(value.get("act", True))}
    return out


def app_names(app: str | None) -> set[str]:
    """Every name a target app goes by, casefolded: as given (``Discord.exe``), its process key (``discord``) and the
    name Windows gives it (``yuki.core.appnames``: "Google Chrome" for chrome.exe), so a switch stored under the
    name Settings shows matches a window's process. Machine identities only; never raises."""
    from yuki.core.appnames import app_key

    text = " ".join(str(app or "").split())
    if not text:
        return set()
    names = {text.casefold(), app_key(text)}
    try:
        from yuki.core.appnames import resolver

        names.add(resolver().name_for_key(app_key(text)).casefold())
    except Exception:  # noqa: BLE001 - the plain names still match
        pass
    return {n for n in names if n}


def _stored_names(name: str) -> set[str]:
    """A switched-off app's name as stored, its process form and the raw names memory has for it (casefolded)."""
    from yuki.core.appnames import app_key

    names = {name.casefold(), app_key(name)}
    try:
        from yuki.core.appnames import resolver

        for alias in resolver().aliases(name):
            names |= {alias.casefold(), app_key(alias)}
    except Exception:  # noqa: BLE001
        pass
    return {n for n in names if n}


def app_switched_off(app: str | None, apps: dict[str, Any],
                     names: Callable[[str | None], set[str]] = app_names) -> str | None:
    """The name Settings shows for ``app`` when the user switched off clicking and typing in it, else ``None``.
    ``apps``: ``{casefolded name: allowed}`` (``yuki.ui.policy.app_acts``' shape) or ``{casefolded name: {"name",
    "act"}}`` (:func:`clean_apps`)."""
    off = [(str(v.get("name") or k), bool(v.get("act", True))) if isinstance(v, dict) else (str(k), bool(v))
           for k, v in apps.items()]
    off = [name for name, allowed in off if not allowed]
    if not off or not app:
        return None
    mine = names(app)
    for name in off:
        if mine & _stored_names(name):
            return name
    return None


def app_off_line(app_name: str) -> str:
    """The plain line for a refused click or type in a switched-off app."""
    return (f"You turned off clicking and typing in {app_name} (Settings, App access), so I won't click or type "
            f"there.")


class PolicyReader:
    """The stored policy, read from its file and re-read when the file changes (by its modification time): the
    levels, and the per-app "Act" switches (``apps``, review 34 H5)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._stamp: tuple[float, int] | None = None
        self._levels: dict[str, str] = dict(POLICY_DEFAULTS)
        self._apps: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _load(self) -> bool:
        """Re-read the file when it changed; ``False`` when there is none (the defaults then)."""
        path = self.path or policy_path()
        try:
            st = path.stat()
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            return False
        with self._lock:
            if stamp != self._stamp:
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raw = {}
                self._levels, self._apps, self._stamp = clean_levels(raw), clean_apps(raw), stamp
        return True

    def levels(self) -> dict[str, str]:
        if not self._load():
            return dict(POLICY_DEFAULTS)
        with self._lock:
            return dict(self._levels)

    def apps(self) -> dict[str, bool]:
        """``{casefolded name: allowed}``, the shape of ``yuki.ui.policy.app_acts`` (the voice hands read this) - ``{}``
        with no file."""
        return {k: bool(v["act"]) for k, v in self.app_entries().items()}

    def app_entries(self) -> dict[str, dict[str, Any]]:
        """``{casefolded name: {"name": as Settings shows it, "act"}}`` (``{}`` with no file)."""
        if not self._load():
            return {}
        with self._lock:
            return {k: dict(v) for k, v in self._apps.items()}

    def app_off(self, app: str | None) -> str | None:
        """The name Settings shows for ``app`` when its "Act" switch is off, else ``None``."""
        return app_switched_off(app, self.app_entries())


#: A key press that acts system-wide and types nothing into the window in front (``Dispatcher.SYSTEM_KEYS``).
_SYSTEM_KEYS = frozenset({"playpause", "nexttrack", "prevtrack", "stop", "volumeup", "volumedown", "volumemute"})


def acts_in_app(name: str, tool_input: dict[str, Any] | None, traits: Traits) -> bool:
    """Whether a call clicks or types in its target app (a ``click_type`` kind), so an app whose "Act" switch is off
    refuses it. Media and volume keys act system-wide, not in the app in front (a fixed key vocabulary)."""
    if "click_type" not in traits.kinds or name == "write_clipboard":
        return False
    tool_input = tool_input or {}
    keys = tool_input.get("keys") if name == "hotkey" else [tool_input.get("key")] if name == "press" else None
    if keys and all(isinstance(k, str) and k.strip().casefold() in _SYSTEM_KEYS for k in keys):
        return False
    return True



# -- yuki/agent/judge.py lines 239-248
_READER: PolicyReader | None = None
_READER_LOCK = threading.Lock()


def _shared_reader() -> PolicyReader:
    global _READER
    with _READER_LOCK:
        if _READER is None:
            _READER = PolicyReader()
        return _READER


# -- yuki/agent/judge.py lines 266-271
def suggest_only(traits: Traits, levels: dict[str, str]) -> str | None:
    """The kind of this call the user set to "just suggest" (never act on it), or ``None``."""
    for kind in traits.kinds:
        if levels.get(kind) == "suggest":
            return kind
    return None


# =====================================================================================================================
# Copied verbatim from yuki.agent.situation (yuki/agent/situation.py, main c219a03)
# =====================================================================================================================

# -- yuki/agent/situation.py lines 56-57
#: An element whose box covers more than this share of the window is a container, not a line of its content.
CONTAINER_SHARE = 0.4


# =====================================================================================================================
# Not copies
# =====================================================================================================================

#: ``action_verdict`` reads ``finish_gate.PAGE_SCROLL_UNVERIFIABLE`` (``loop.py`` imports ``yuki.agent.finish`` under that
#: name); this module holds the same constant, so the name is this module.
finish_gate = sys.modules[__name__]


def load_skills(*, store: Any = None) -> Any:
    """The skill catalog (critique M6): ``yuki.skills.catalog.load()`` when phase L's catalog is installed (``store``
    passed on when given), else ``yuki.skills.connection.load()``. Returns a ``connection.Loaded`` either way
    (``.get(key)``, ``.skills``). Raises only what the loader itself raises."""
    import importlib.util

    if importlib.util.find_spec("yuki.skills.catalog") is not None:
        from yuki.skills import catalog

        if store is not None:
            try:
                return catalog.load(store=store)
            except TypeError:   # a catalog without the store argument
                pass
        return catalog.load()
    from yuki.skills.connection import load

    return load()


def skill(key: Any) -> Any:
    """One skill of the catalog by its key (``None``: no such skill, or the files could not be read). Never raises."""
    try:
        return load_skills().get(str(key or ""))
    except Exception:  # noqa: BLE001 - unreadable data is no skill
        return None


__all__ = ["ANSWER", "CONTAINER_SHARE", "EXPECTED_UNVERIFIABLE", "KIND_WORDS", "LEVELS", "LEVEL_LABELS", "LEVEL_WORDS",
           "LIKELY", "LINE_FAILED", "LINE_NOTHING_DONE", "LINE_NOT_CHECKED", "LINE_PARTIAL", "LINE_SEND_UNCONFIRMED",
           "LINE_UNREAD", "LINE_UNVERIFIED", "PAGE_SCROLL_UNVERIFIABLE", "POLICY_CEILINGS", "POLICY_DEFAULTS",
           "PolicyReader", "SAYABLE", "SIMILAR", "STABLE_CACHE_CONTROL", "CONTRADICTED", "EXPECTED", "FAILED",
           "UNVERIFIED", "VERIFIED", "action_verdict", "acts_in_app", "app_name", "app_names", "app_off_line",
           "app_switched_off", "clean_apps", "clean_levels", "load_skills", "policy_path", "sayable", "similar",
           "skill", "suggest_only"]
