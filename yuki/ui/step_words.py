"""The words a person reads for a step Yuki takes, and the return card's summary of a run: one vocabulary.

Every place the user sees a step (the status strip, the island's step line and run card, the chat's step folds,
the return card and the Activity ledger) gets its words from :func:`describe_step` (revamp 38 §8, the user's 3 Oct
feedback 12 and 14: "no technical things for users; plain words only"). Built from three things only:

* the call's ``what``: the brain's own plain words for a step whose input says nothing a person reads (a shell
  command, keys, typed text): ``close Slack`` reads "Closing Slack" while it runs and "Closed Slack" after;
* what the agent knows about the target (``_about``, added by the agent loop to the ``tool_call`` event): the
  control's role and visible name from the latest read, the app's name, the tab's name; in the ledger the stored
  step's control role and name and its app's name;
* the call's fixed machine fields (an ``action`` enum, ``press_enter``, ``mute``, the scroll's sign, an app name
  the tool takes as a name).

Never the command, the typed text, an address, a key combination, a window or element id or any number: no code
path here reads ``command``, ``text`` or ``url`` (``tests/test_appC1_run_text.py`` greps for it). A name that looks
like data (digits, an address, a path, a long sentence) is left out and the step says what kind of thing it was
("Clicking a button"). Pure functions, no Qt and no agent import at module load (the HTML window's process uses
them for the ledger).
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from typing import Any

#: The field the agent loop adds to a ``tool_call`` event's input (``yuki.agent.tools.STEP_ABOUT_FIELD``).
ABOUT = "_about"
#: The brain's plain words for a step (``yuki.agent.tools.WHAT_FIELD``).
WHAT = "what"

#: A name longer than this, or with more words, is a sentence or data, not a name a person reads as one.
NAME_CHARS = 32
NAME_WORDS = 5
#: The model's ``what`` is cut to this many characters.
WHAT_CHARS = 60
#: The input fields the words are built from (a run surface keeps only these of a call: never the command, typed text
#: or address).
LABEL_KEYS = frozenset({WHAT, ABOUT, "query", "app", "action", "press_enter", "mute", "change", "level", "dy", "dx",
                        "button", "clicks", "kind", "since", "until", "before", "after", "day", "start", "end"})


def label_input(tool_input: Any) -> dict[str, Any]:
    """The part of a call's input the words are built from (:data:`LABEL_KEYS`), for a surface that keeps steps."""
    return {k: v for k, v in tool_input.items() if k in LABEL_KEYS} if isinstance(tool_input, dict) else {}

# -- names ------------------------------------------------------------------------------------------------------------


def _squash(value: Any) -> str:
    return " ".join(str(value or "").split()) if isinstance(value, str) else ""


def _looks_like_data(text: str) -> bool:
    """Characters a name never has but data does: digits, an address, a path, an identifier's underscore, a dotted
    token with no space (a host or file name). Character tests on a fixed format, not words."""
    if any(ch.isdigit() for ch in text) or "://" in text or "\\" in text or "/" in text or "_" in text:
        return True
    return any("." in tok.strip(".") for tok in text.split())


def plain_name(value: Any, *, chars: int = NAME_CHARS) -> str:
    """A control's, tab's or app's name when a person reads it as a name, else ``""``."""
    text = _squash(value).strip(" .:;,")
    if not text or len(text) > chars or len(text.split()) > NAME_WORDS or _looks_like_data(text):
        return ""
    return text


def app_name(value: Any) -> str:
    """An app as people say it: the name the agent resolved, or the name the tool took (``spotify`` -> ``Spotify``,
    ``Spotify.exe`` -> ``Spotify``); a trailing version number is dropped (``FL Studio 21`` -> ``FL Studio``)."""
    text = _squash(value)
    if text.casefold().endswith(".exe"):
        text = text[:-4].rstrip()
    words = text.split()
    while words and any(ch.isdigit() for ch in words[-1]):
        words.pop()
    text = plain_name(" ".join(words), chars=40)
    if text and text.islower():
        text = text[:1].upper() + text[1:]
    return text


#: How a control with no readable name is named, by its UI Automation role (a closed set of type names).
ROLE_NOUNS: dict[str, str] = {
    "Button": "a button", "SplitButton": "a button", "MenuItem": "a menu item", "Hyperlink": "a link",
    "ListItem": "an item in a list", "TreeItem": "an item in a list", "DataItem": "a row", "TabItem": "a tab",
    "CheckBox": "a checkbox", "RadioButton": "an option", "ComboBox": "a list box", "Edit": "a text box",
    "Document": "the page", "Text": "some text", "Image": "a picture", "MenuBar": "the menu", "Menu": "a menu",
    "List": "a list", "Tree": "a list", "ToolBar": "a toolbar", "Slider": "a slider", "Pane": "a panel",
}
#: Roles whose name reads as a box ("the Search box") rather than as a label one clicks ("Done").
BOX_ROLES = frozenset({"Edit", "ComboBox", "Document"})


def _about(args: dict[str, Any]) -> dict[str, Any]:
    about = args.get(ABOUT)
    return about if isinstance(about, dict) else {}


def _control(about: dict[str, Any], fallback: str) -> str:
    """``Done``, ``the Search box``, ``a button``: the control a step acted on."""
    role = _squash(about.get("role"))
    name = plain_name(about.get("element"))
    if name:
        return f"the {name} box" if role in BOX_ROLES else name
    return ROLE_NOUNS.get(role, fallback)


def _in_app(about: dict[str, Any]) -> str:
    app = app_name(about.get("app"))
    return f" in {app}" if app else ""


# -- the brain's own words ------------------------------------------------------------------------------------------

_ING: dict[str, str] = {"be": "being", "see": "seeing", "flee": "fleeing", "agree": "agreeing", "free": "freeing",
                        "lie": "lying", "die": "dying", "tie": "tying", "dye": "dyeing", "eye": "eyeing"}
_PAST: dict[str, str] = {
    "be": "was", "begin": "began", "bring": "brought", "build": "built", "buy": "bought", "catch": "caught",
    "choose": "chose", "come": "came", "cut": "cut", "do": "did", "draw": "drew", "drive": "drove", "feed": "fed",
    "find": "found", "forget": "forgot", "freeze": "froze", "get": "got", "give": "gave", "go": "went",
    "grow": "grew", "hang": "hung", "have": "had", "hear": "heard", "hide": "hid", "hit": "hit", "hold": "held",
    "keep": "kept", "know": "knew", "lead": "led", "leave": "left", "let": "let", "light": "lit", "lose": "lost",
    "make": "made", "mean": "meant", "meet": "met", "overwrite": "overwrote", "pay": "paid", "put": "put",
    "quit": "quit", "read": "read", "rebuild": "rebuilt", "redo": "redid", "rerun": "reran", "reset": "reset",
    "rewrite": "rewrote", "ring": "rang", "run": "ran", "say": "said", "see": "saw", "sell": "sold", "send": "sent",
    "set": "set", "shake": "shook", "shut": "shut", "sit": "sat", "sleep": "slept", "speak": "spoke",
    "spend": "spent", "split": "split", "spread": "spread", "stand": "stood", "stick": "stuck", "take": "took",
    "teach": "taught", "tell": "told", "think": "thought", "throw": "threw", "undo": "undid", "unfreeze": "unfroze",
    "wake": "woke", "wear": "wore", "win": "won", "withdraw": "withdrew", "write": "wrote",
}
#: Verbs of more than one syllable stressed on the last one: their final consonant doubles (``submitting``).
_DOUBLES = frozenset({"admit", "begin", "commit", "compel", "control", "emit", "equip", "expel", "forget",
                      "occur", "omit", "patrol", "permit", "prefer", "propel", "quit", "rebut", "refer", "regret",
                      "repel", "submit", "transmit", "unpin", "unzip", "upset"})
_VOWELS = "aeiou"


def _doubles(verb: str) -> bool:
    """English spelling: a one-syllable verb ending consonant-vowel-consonant doubles its last letter (``stop`` ->
    ``stopping``), except after w, x or y."""
    if verb in _DOUBLES:
        return True
    groups = sum(1 for i, ch in enumerate(verb) if ch in _VOWELS and (i == 0 or verb[i - 1] not in _VOWELS))
    return (groups == 1 and len(verb) >= 3 and verb[-1] not in _VOWELS + "wxy" and verb[-2] in _VOWELS
            and verb[-3] not in _VOWELS)


def _ing(verb: str) -> str:
    v = verb.casefold()
    if v in _ING:
        return _ING[v]
    if v.endswith("ing") and len(v) > 4:
        return verb
    if v.endswith("ie"):
        return verb[:-2] + "ying"
    if v.endswith(("ee", "ye", "oe")):
        return verb + "ing"
    if v.endswith("e") and len(v) > 2:
        return verb[:-1] + "ing"
    if _doubles(v):
        return verb + verb[-1] + "ing"
    return verb + "ing"


def _ed(verb: str) -> str:
    v = verb.casefold()
    if v in _PAST:
        return _PAST[v]
    if v.endswith("e"):
        return verb + "d"
    if v.endswith("y") and len(v) > 1 and v[-2] not in _VOWELS:
        return verb[:-1] + "ied"
    if _doubles(v):
        return verb + verb[-1] + "ed"
    return verb + "ed"


def plain_what(value: Any) -> str:
    """The brain's ``what`` when it is plain words (verb first, no digits, address or path), else ``""``."""
    text = _squash(value).strip(" .")
    if not text or any(ch.isdigit() for ch in text) or "://" in text or "\\" in text:
        return ""
    return text if len(text) <= WHAT_CHARS else text[: WHAT_CHARS - 1].rstrip() + "…"


def say_what(what: str, *, done: bool) -> str:
    """``close Slack`` -> ``Closing Slack`` (running) or ``Closed Slack`` (done): its first word inflected by
    English spelling rules, the rest as the brain wrote it."""
    first, _sep, rest = what.partition(" ")
    if not first.isalpha():
        word = first
    elif first.casefold().endswith("ing") and len(first) > 4:
        word = first   # already "closing ..." (a habit of the model): kept as written
    else:
        word = _ed(first) if done else _ing(first)
    line = f"{word} {rest}".strip()
    return line[:1].upper() + line[1:]


# -- one step ---------------------------------------------------------------------------------------------------------


def _two(done: bool, now: str, then: str) -> str:
    return then if done else now


def _launch(args: dict[str, Any], about: dict[str, Any], done: bool) -> str:
    app = app_name(about.get("app")) or app_name(args.get("query")) or "an app"
    return f"{_two(done, 'Opening', 'Opened')} {app}"


def _open_url(args: dict[str, Any], about: dict[str, Any], done: bool) -> str:
    thing = "a file" if about.get("local") else "a page"
    app = app_name(args.get("app")) or app_name(about.get("app"))
    return f"{_two(done, 'Opening', 'Opened')} {thing}" + (f" in {app}" if app else "")


def _focus(args: dict[str, Any], about: dict[str, Any], done: bool) -> str:
    app = app_name(about.get("app"))
    return f"{_two(done, 'Switching to', 'Switched to')} {app}" if app else _two(done, "Switching windows",
                                                                                  "Switched windows")


def _click(args: dict[str, Any], about: dict[str, Any], done: bool) -> str:
    verb = _two(done, "Clicking", "Clicked")
    if args.get("button") == "right":
        verb = _two(done, "Right-clicking", "Right-clicked")
    elif args.get("clicks") == 2:
        verb = _two(done, "Double-clicking", "Double-clicked")
    target = _control(about, "")
    return f"{verb} {target}" if target else f"{verb}{_in_app(about) or ' on the screen'}"


def _type(args: dict[str, Any], about: dict[str, Any], done: bool) -> str:
    if args.get("press_enter") is True:
        return _two(done, "Typing and sending a message", "Typed and sent a message")
    name = plain_name(about.get("element"))
    if name:
        return f"{_two(done, 'Typing in', 'Typed in')} the {name} box"
    return _two(done, "Typing", "Typed") + _in_app(about)


def _hotkey(args: dict[str, Any], about: dict[str, Any], done: bool) -> str:
    return _two(done, "Pressing a shortcut", "Pressed a shortcut") + _in_app(about)


def _press(args: dict[str, Any], about: dict[str, Any], done: bool) -> str:
    return _two(done, "Pressing a key", "Pressed a key") + _in_app(about)


def _scroll(args: dict[str, Any], about: dict[str, Any], done: bool) -> str:
    dy, dx = args.get("dy"), args.get("dx")
    way = ""
    if isinstance(dy, (int, float)) and not isinstance(dy, bool) and dy:
        way = " up" if dy > 0 else " down"
    elif isinstance(dx, (int, float)) and not isinstance(dx, bool) and dx:
        way = " sideways"
    what = "the list" if _squash(about.get("role")) in ("List", "Tree", "DataGrid", "Table") else "the page"
    return f"{_two(done, 'Scrolling', 'Scrolled')}{way} {what}"


def _shell(args: dict[str, Any], about: dict[str, Any], done: bool) -> str:
    return _two(done, "Working on your PC", "Worked on your PC")


def _tab(args: dict[str, Any], about: dict[str, Any], done: bool) -> str:
    tab = plain_name(about.get("tab"))
    if args.get("action") == "close":
        return f"{_two(done, 'Closing', 'Closed')} {tab or 'a tab'}"
    return f"{_two(done, 'Switching to', 'Switched to')} {tab}" if tab else _two(done, "Switching tabs",
                                                                                  "Switched tabs")


def _hover(args: dict[str, Any], about: dict[str, Any], done: bool) -> str:
    return f"{_two(done, 'Pointing at', 'Pointed at')} {_control(about, 'something')}"


def _volume(args: dict[str, Any], about: dict[str, Any], done: bool) -> str:
    app = app_name(args.get("app")) or app_name(about.get("app")) or "an app"
    mute, change = args.get("mute"), args.get("change")
    if mute is True:
        return f"{_two(done, 'Muting', 'Muted')} {app}"
    if mute is False:
        return f"{_two(done, 'Unmuting', 'Unmuted')} {app}"
    if isinstance(change, (int, float)) and not isinstance(change, bool) and change:
        return f"{_two(done, 'Turning', 'Turned')} {app} {'up' if change > 0 else 'down'}"
    if isinstance(args.get("level"), (int, float)):
        return f"{_two(done, 'Setting', 'Set')} {app}’s volume"
    return _two(done, "Checking which apps play sound", "Checked which apps play sound")


#: ``window_action``'s fixed ``action`` enum, as (running, done) verbs; ``{}`` is the window's app.
WINDOW_ACTIONS: dict[str, tuple[str, str]] = {
    "minimize": ("Minimizing {}", "Minimized {}"), "maximize": ("Maximizing {}", "Maximized {}"),
    "fullscreen": ("Making {} full screen", "Made {} full screen"), "restore": ("Restoring {}", "Restored {}"),
    "focus": ("Switching to {}", "Switched to {}"), "close": ("Closing {}", "Closed {}"),
}


def _window(args: dict[str, Any], about: dict[str, Any], done: bool) -> str:
    pair = WINDOW_ACTIONS.get(str(args.get("action") or ""))
    if pair is None:
        return _two(done, "Arranging a window", "Arranged a window")
    return pair[1 if done else 0].format(app_name(about.get("app")) or "a window")


#: The action tools (``yuki.agent.tools.ACTION_TOOL_NAMES``), each with its builder.
ACTION_WORDS = {
    "launch_app": _launch, "open_url": _open_url, "focus_window": _focus, "click": _click, "type_text": _type,
    "hotkey": _hotkey, "press": _press, "scroll": _scroll, "run_powershell": _shell, "tab": _tab, "hover": _hover,
    "app_volume": _volume, "window_action": _window,
    "write_clipboard": lambda a, b, done: _two(done, "Copying that", "Copied that"),
    "open_yuki": lambda a, b, done: _two(done, "Opening my window", "Opened my window"),
}

#: Yuki's own tools, done (the ledger's memory steps), by name; the to-do tool by its ``action`` enum.
DONE_WORDS: dict[str, str] = {
    "remember_rule": "Kept a rule", "forget_rule": "Dropped a rule", "correct_memory": "Corrected what I know",
    "watch_for": "Started keeping an eye out", "remember_how": "Kept how it’s done",
    "answer_question": "Kept your answer", "note_to_self": "Made a note", "memory": "Checked my memory",
    "about": "Checked who that is", "todo": "Changed your list", "say": "Told you", "ask_user": "Asked you",
}
TODO_DONE: dict[str, str] = {"add": "Added to your list", "done": "Marked done on your list",
                             "not_needed": "Took off your list", "wrong": "Took off your list"}


def _registry_label(name: str, args: dict[str, Any]) -> str:
    """The tool's own running label from the agent's registry, when that registry is loaded in this process (the
    tray); the HTML window's process never loads the agent."""
    tools = sys.modules.get("yuki.agent.tools")
    if tools is None:
        return ""
    try:
        return str(tools.tool_label(name, args) or "")
    except Exception:  # noqa: BLE001 - presentation only
        return ""


def describe_step(name: str, tool_input: dict[str, Any] | None = None, *, done: bool = False,
                  limit: int = 60) -> str:
    """The one line a person reads for a step, running (``Closing Slack``) or done (``Closed Slack``).

    Args:
        name: The tool's name as the model called it.
        tool_input: Its input as the ``tool_call`` event carries it (with the agent's ``_about``), or the ledger's
            stored input (the control's ``role`` and ``name``, the app under ``_about``).
        done: Past tense (the return card, the ledger).
        limit: The longest line; longer is cut with an ellipsis.

    Returns:
        A short line in plain words, no trailing full stop; never empty.
    """
    args = tool_input if isinstance(tool_input, dict) else {}
    about = _about(args)
    what = plain_what(args.get(WHAT))
    if what:
        line = say_what(what, done=done)
    elif name in ACTION_WORDS:
        line = ACTION_WORDS[name](args, about, done)
    elif done:
        line = TODO_DONE.get(str(args.get("action") or ""), "") if name == "todo" else ""
        line = line or DONE_WORDS.get(name, "") or "Did a step"
    else:
        phrase = _registry_label(name, args) or "Working on it"
        when = _when_words([(k, d) for k, v in args.items() if (d := _iso(v)) is not None])
        line = f"{phrase} {when}" if when else phrase
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


# -- dates in a memory tool's arguments (the tools' fixed ISO format) --------------------------------------------------


def _iso(value: Any) -> Any:
    """A date or date-time from an ISO string (the tools' fixed format), else None."""
    if not isinstance(value, str) or not value[:4].isdigit() or len(value) < 10:
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        try:
            d = date.fromisoformat(value.strip()[:10])
            return datetime(d.year, d.month, d.day)
        except ValueError:
            return None


def clock(d: datetime) -> str:
    """``9:12 PM``: a time in a sentence (12-hour, as people say it)."""
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def _day_words(d: Any, *, time_too: bool = True) -> str:
    days = (date.today() - d.date()).days
    day = "today" if days == 0 else "yesterday" if days == 1 else f"{d.day} {d:%b}"
    return f"{day} {clock(d)}" if time_too and (d.hour, d.minute) != (0, 0) else day


def _when_words(dates: list[tuple[str, Any]]) -> str:
    """``on 26 Sep``, ``since yesterday``, ``from 25 Sep to today``, from the ISO dates of a call's arguments."""
    if not dates:
        return ""
    if len(dates) == 1:
        key, d = dates[0]
        words = _day_words(d)
        if key in ("since", "until", "before", "after"):
            return f"{key} {words}"
        return words if words.startswith(("today", "yesterday")) else f"on {words}"
    (_ka, a), (_kb, b) = sorted(dates[:2], key=lambda kd: kd[1])
    if a.date() == b.date():
        words = _day_words(a, time_too=False)
        return words if words in ("today", "yesterday") else f"on {words}"
    return f"from {_day_words(a)} to {_day_words(b)}"


# -- the return card ----------------------------------------------------------------------------------------------------

#: The card's header by how the run really ended: (icon, word).
OUTCOME_HEADS: dict[str, tuple[str, str]] = {
    "done": ("check", "Done"), "stopped": ("stop", "Stopped"), "failed": ("x", "Didn’t finish"),
    "handed": ("hand", "Handed back"),
}
#: Rows shown before "+N more".
RETURN_ROWS = 4
#: Steps that only take the user somewhere (opened, switched to): folded into one "Opened …" line.
OPENING_TOOLS = frozenset({"launch_app", "open_url"})
#: Yuki's own tools whose step changes something the user keeps (their list, their rules, what Yuki watches for).
KEEPING_TOOLS = frozenset({"todo", "remember_rule", "forget_rule", "watch_for", "correct_memory", "remember_how"})


def is_effect(name: str, tool_input: dict[str, Any] | None) -> bool:
    """A step whose result the user cares about after the run: the agent marked it as sending, closing or deleting
    for certain (``_about``: ``sends`` / ``changes``, from the tool's fixed traits), the brain declared its effect
    (``risk.effect``), or it changed Yuki's own lists and rules. Opening, switching, clicking, scrolling and keys
    are getting there, not results. Machine fields only."""
    args = tool_input if isinstance(tool_input, dict) else {}
    about = _about(args)
    return bool(about.get("sends") or about.get("changes") or _squash(about.get("effect"))) or name in KEEPING_TOOLS


def _joined(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def return_view(outcome: str, text: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """What the return card says about a run that acted, from how it ended and its steps.

    Args:
        outcome: ``done`` | ``stopped`` | ``failed`` | ``handed`` (the real end, :meth:`YukiUi._settle`).
        text: The closing text (the answer for ``done``; the plain reason for ``failed``).
        steps: The run's calls in order: ``{"name", "input" (with ``_about``), "ok" (True / False / None: not
            finished)}``.

    Returns:
        ``{"icon", "head", "title", "rows": [{"text", "icon"}] (at most :data:`RETURN_ROWS`; ``check`` for what
        happened, ``dash`` for an effect that did not), "more" (how many rows did not fit), "worth"}``: ``worth`` is
        False when there is nothing to tell beyond the strip's final word. No row carries an undo of any kind (the
        user, 3 Oct 19:10: "dont show the undo options in this card").
    """
    icon, head = OUTCOME_HEADS.get(outcome, OUTCOME_HEADS["done"])
    opened: list[str] = []
    rows: list[dict[str, str]] = []
    sent_ok = sent_missed = False
    for st in steps:
        name, args, ok = str(st.get("name") or ""), st.get("input") or {}, st.get("ok")
        about = _about(args)
        if about.get("sends"):
            sent_ok = sent_ok or ok is True
            sent_missed = sent_missed or ok is not True
        if ok is True and name in OPENING_TOOLS and not is_effect(name, args):
            thing = describe_step(name, args, done=True).removeprefix("Opened ")
            if thing not in opened:
                opened.append(thing)
        elif is_effect(name, args):
            if ok is True:
                row = {"text": describe_step(name, args, done=True), "icon": "check"}
            elif about.get("sends"):
                continue   # said once, in the title ("Nothing was sent.")
            else:
                what = plain_what(args.get(WHAT))
                running = describe_step(name, args)
                row = {"text": f"Didn’t {what}" if what else f"Didn’t finish {running[:1].lower() + running[1:]}",
                       "icon": "dash"}
            if row not in rows:
                rows.append(row)
    if opened:
        rows.insert(0, {"text": "Opened " + _joined(opened), "icon": "check"})
    nothing_sent = sent_missed and not sent_ok
    title = " ".join(str(text or "").split())
    if outcome == "stopped":
        title = "Stopped before it finished." + (" Nothing was sent." if nothing_sent else "")
    elif outcome == "failed":
        title = (title or "I couldn’t finish that.") + (" Nothing was sent." if nothing_sent else "")
    elif outcome == "handed":
        title = title or "Handed back. You have the controls."
    return {"icon": icon, "head": head, "title": title, "rows": rows[:RETURN_ROWS],
            "more": max(0, len(rows) - RETURN_ROWS), "worth": bool(rows) or nothing_sent}


def took_words(seconds: float) -> str:
    """How long a run took, for the card's footer: ``41 s``, ``1 min 9 s``, ``1 h 2 min``."""
    secs = max(1, int(round(seconds)))
    if secs < 60:
        return f"{secs} s"
    if secs < 3600:
        return f"{secs // 60} min {secs % 60} s" if secs % 60 else f"{secs // 60} min"
    return f"{secs // 3600} h {secs % 3600 // 60} min"
