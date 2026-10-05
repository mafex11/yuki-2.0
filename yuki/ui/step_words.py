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

A finished run's one closing line (the return card's title, the strip's final word, the thread's final line) is
:func:`achievement`: the brain's plain past-tense ``achieved`` on ``done``, else the done words of the effect that
happened; never the reply and never a bare "Done". A voice hand reads the same way (:func:`describe_hand`).
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
#: The brain's plain past-tense line of what a finished run achieved (``yuki.agent.tools.ACHIEVED_FIELD`` on ``done``):
#: the return card's title, the strip's final word and the thread's final line.
ACHIEVED = "achieved"
#: It is cut to this many characters; fewer words than this is not a line that says what was achieved ("Done.").
ACHIEVED_CHARS = 80
ACHIEVED_MIN_WORDS = 2
#: The input fields the words are built from (a run surface keeps only these of a call: never the command, typed text
#: or address). ``media`` and ``want`` are a voice hand's media key enum (:func:`hand_input`).
LABEL_KEYS = frozenset({WHAT, ABOUT, ACHIEVED, "query", "app", "action", "press_enter", "mute", "change", "level", "dy",
                        "dx", "button", "clicks", "kind", "since", "until", "before", "after", "day", "start", "end",
                        "media", "want"})


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


#: An irregular past form -> its verb (``sent`` -> ``send``); a verb whose past is the same word maps to itself.
_PAST_BASE: dict[str, str] = {past: base for base, past in _PAST.items()}
#: Letters English doubles naturally at a verb's end (``call``, ``pass``, ``buzz``, ``stuff``): never undoubled.
_NATURAL_DOUBLES = "lsfz"


def _unsuffix(word: str, n: int) -> str:
    """The verb under ``word`` minus an ``-ed``/``-ing`` ending of ``n`` letters, by English spelling: a doubled last
    consonant is undoubled (``stopp`` -> ``stop``) and a dropped ``e`` comes back (``clos`` -> ``close``)."""
    stem = word[:-n]
    s = stem.casefold()
    if len(s) >= 3 and s[-1] == s[-2] and s[-1] not in _VOWELS + _NATURAL_DOUBLES and _doubles(s[:-1]):
        return stem[:-1]
    if _doubles(s) and s not in _DOUBLES:
        return stem + "e"   # "clos" would have doubled ("clossed"); it did not, so the verb ends in e
    return stem


def _verb(word: str) -> tuple[str, str]:
    """``(verb, form)`` of the first word of a ``what``, by English spelling: ``form`` is ``past`` (``closed``,
    ``sent``), ``ing`` (``closing``), ``s`` (``sends``, the third person) or ``base`` (``close``)."""
    w = word.casefold()
    if w in _PAST or w in _ING:
        return word, "base"
    if w in _PAST_BASE:
        return _PAST_BASE[w], "past"
    if len(w) > 4 and w.endswith("ing"):
        return _unsuffix(word, 3), "ing"
    if len(w) > 3 and w.endswith("ied"):
        return word[:-3] + "y", "past"
    if len(w) > 3 and w.endswith("ed") and not w.endswith("eed"):
        return _unsuffix(word, 2), "past"
    if len(w) > 3 and w.endswith("ies"):
        return word[:-3] + "y", "s"
    if len(w) > 3 and w.endswith(("ches", "shes", "sses", "xes", "zzes", "oes")):
        return word[:-2], "s"
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return word[:-1], "s"
    return word, "base"


#: Characters a plain sentence never has but a command, path, switch or expression does (a fixed character set).
CODE_CHARS = frozenset("/\\|_$=;{}[]<>`@*~^")


def _looks_like_code(text: str) -> bool:
    """Character tests on fixed formats, never words: an address, a path or pipe, a dotted token (a host, a file, a
    member), a switch (``-Force``) or a PowerShell ``Verb-Noun`` name."""
    if "://" in text or any(ch in CODE_CHARS for ch in text):
        return True
    for tok in text.split():
        core = tok.strip(".,:!?\"'()")
        if "." in core or (core.startswith("-") and len(core) > 1):
            return True
        _head, sep, tail = core.partition("-")
        if sep and tail[:1].isupper():
            return True
    return False


#: appPT (review F8): a run of this many digits is a number nobody says (a window handle, an id), not a time or count.
ID_DIGITS = 5


def _looks_like_id(text: str) -> bool:
    """Character tests on fixed formats, never words: a run of :data:`ID_DIGITS` digits or more (``655432``), or a token
    where a letter runs straight into a digit (``e12``, ``hwnd3``): window and element numbers. Times, counts and
    "4pm" / "2nd" (a digit first) are fine."""
    run = 0
    for ch in text:
        run = run + 1 if ch.isdigit() else 0
        if run >= ID_DIGITS:
            return True
    return any(a.isalpha() and b.isdigit() for tok in text.split() for a, b in zip(tok, tok[1:]))


def _cut(text: str, chars: int) -> str:
    return text if len(text) <= chars else text[: chars - 1].rstrip() + "…"


def plain_what(value: Any) -> str:
    """The brain's ``what`` when it is plain words (verb first; no digits, address, path, switch or command name),
    else ``""``."""
    text = _squash(value).strip(" .")
    if not text or any(ch.isdigit() for ch in text) or _looks_like_code(text):
        return ""
    return _cut(text, WHAT_CHARS)


def plain_achieved(value: Any) -> str:
    """The brain's ``achieved`` line when it says what was done in plain words (at least
    :data:`ACHIEVED_MIN_WORDS` words, so never just "Done."; no address, path, switch or command name; a number a
    person says, "60%", is fine), first letter capital, else ``""``."""
    text = _squash(value).strip(" .!")
    if len(text.split()) < ACHIEVED_MIN_WORDS or _looks_like_code(text):
        return ""
    text = _cut(text, ACHIEVED_CHARS)
    return text[:1].upper() + text[1:]


#: appPT: the brain's live one-line note (its ``now``, or the situation check's plain line) is cut to this.
THOUGHT_CHARS = 110


def plain_thought(value: Any) -> str:
    """The brain's live note of what it checks or does and why (appPT: the island's thinking and working line, the run
    card's current step) when it is a plain line: one line, no address, path, switch or command name (character tests
    on fixed formats, never words), first letter capital, no closing full stop; else ``""``. Times and counts a
    person says ("6:08 PM", "3 messages") are fine; a window or element number is not (review F8)."""
    text = _squash(value).strip(" .")
    if not text or _looks_like_code(text) or _looks_like_id(text):
        return ""
    text = _cut(text, THOUGHT_CHARS)
    return text[:1].upper() + text[1:]


def say_what(what: str, *, done: bool) -> str:
    """``close Slack`` -> ``Closing Slack`` (running) or ``Closed Slack`` (done): its first word in the matching
    tense by English spelling rules (a first word already in that tense is kept as written: ``closed Slack`` stays
    ``Closed Slack``; ``sends Ana a message`` reads ``Sending``/``Sent``), the rest as the brain wrote it."""
    first, _sep, rest = what.partition(" ")
    if not first.isalpha():
        word = first
    else:
        verb, form = _verb(first)
        if done:
            word = first if form == "past" else _ed(verb)
        else:
            word = first if form == "ing" else _ing(verb)
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
    if args.get("action") == "reopen":
        return _two(done, "Reopening a tab", "Reopened a tab")
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


#: A voice hand's media key (``yuki.voice.hands.MEDIA_KEYS``, a fixed enum) as (running, done) words.
MEDIA_WORDS: dict[str, tuple[str, str]] = {
    "playpause": ("Playing or pausing the music", "Played or paused the music"),
    "nexttrack": ("Skipping to the next track", "Skipped to the next track"),
    "prevtrack": ("Going back a track", "Went back a track"), "stop": ("Stopping the music", "Stopped the music"),
    "volumeup": ("Turning the sound up", "Turned the sound up"),
    "volumedown": ("Turning the sound down", "Turned the sound down"),
    "volumemute": ("Muting the sound", "Muted the sound"),
}


def _media(args: dict[str, Any], about: dict[str, Any], done: bool) -> str:
    want = args.get("want")
    if args.get("media") == "playpause" and want == "playing":
        return _two(done, "Playing the music", "Played the music")
    if args.get("media") == "playpause" and want == "paused":
        return _two(done, "Pausing the music", "Paused the music")
    pair = MEDIA_WORDS.get(str(args.get("media") or ""))
    return pair[1 if done else 0] if pair else _two(done, "Pressing a media key", "Pressed a media key")


#: The action tools (``yuki.agent.tools.ACTION_TOOL_NAMES``), each with its builder; then the voice hands that are no
#: thinker tool (:func:`hand_input` maps the others onto the thinker's).
ACTION_WORDS = {
    "launch_app": _launch, "open_url": _open_url, "focus_window": _focus, "click": _click, "type_text": _type,
    "hotkey": _hotkey, "press": _press, "scroll": _scroll, "run_powershell": _shell, "tab": _tab, "hover": _hover,
    "app_volume": _volume, "window_action": _window,
    "write_clipboard": lambda a, b, done: _two(done, "Copying that", "Copied that"),
    "open_yuki": lambda a, b, done: _two(done, "Opening my window", "Opened my window"),
    "media_key": _media,
    "run_skill": lambda a, b, done: _two(done, "Doing it the way I learned", "Did it the way I learned"),
}

#: Yuki's own tools, done (the ledger's memory steps), by name; the to-do tool by its ``action`` enum.
DONE_WORDS: dict[str, str] = {
    "remember_rule": "Kept a rule", "forget_rule": "Dropped a rule", "correct_memory": "Corrected what I know",
    "watch_for": "Started keeping an eye out", "remember_how": "Kept how it’s done",
    "answer_question": "Kept your answer", "note_to_self": "Made a note", "memory": "Checked my memory",
    "about": "Checked who that is", "todo": "Changed your list", "say": "Told you", "ask_user": "Asked you",
    "check_situation": "Checked first", "tasks": "Kept your list",
}
#: Running words for Yuki's own steps when the agent's registry is not loaded in this process (the HTML window's):
#: the situation check before a step that lasts and the kept list (report 47 N0, N10; appPT).
RUNNING_WORDS: dict[str, str] = {"check_situation": "Checking first", "tasks": "Keeping your list"}
TODO_DONE: dict[str, str] = {"add": "Added to your list", "done": "Marked done on your list",
                             "not_needed": "Took off your list", "wrong": "Took off your list",
                             "list": "Checked your list"}
#: The to-do tool's actions that change the list (its ``action`` enum minus ``list``, a read).
TODO_WRITES = frozenset({"add", "done", "not_needed", "wrong"})


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
        phrase = _registry_label(name, args) or RUNNING_WORDS.get(name) or "Working on it"
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


#: ``window_action``'s actions that change what the user sees (``focus`` only switches: getting there).
WINDOW_CHANGES = frozenset({"minimize", "maximize", "fullscreen", "restore", "close"})
#: The card's title for a finished run when no step says what it achieved (never just "Done").
FINISHED_TITLE = "Finished what you asked."
#: The plain reason for a run that did not finish, whatever the loop's own error text said (that text stays in the
#: log; the card, the strip and the thread's final line never show it).
FAILED_TITLE = "I couldn’t finish that."
NOTHING_SENT = "Nothing was sent."


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def changes_state(name: str, tool_input: dict[str, Any] | None) -> bool:
    """A step that is itself what the user asked for: an app's volume set, changed or muted, a window minimised,
    maximised, made full screen, restored or closed. Machine fields only (the tool's name and its fixed fields)."""
    args = tool_input if isinstance(tool_input, dict) else {}
    if name == "app_volume":
        return _number(args.get("level")) or (_number(args.get("change")) and bool(args.get("change"))) or \
            isinstance(args.get("mute"), bool)
    if name == "window_action":
        return str(args.get("action") or "") in WINDOW_CHANGES
    return False


def effect_key(name: str, tool_input: dict[str, Any] | None) -> str:
    """A step's machine key for matching a run trace's verified steps to the card's rows (second review V2-4): the tool,
    its fixed ``action`` and the app its input names (casefolded, without ``.exe``). Plumbing, never words."""
    args = tool_input if isinstance(tool_input, dict) else {}
    app = " ".join(str(args.get("app") or "").split()).casefold()
    app = app[:-4] if app.endswith(".exe") else app
    return f"{name}|{' '.join(str(args.get('action') or '').split()).casefold()}|{app}"


def is_effect(name: str, tool_input: dict[str, Any] | None) -> bool:
    """A step whose result the user cares about after the run: the agent marked it as sending, closing or deleting
    for certain (``_about``: ``sends`` / ``changes``, from the tool's fixed traits), the brain declared its effect
    (``risk.effect``), it changed a volume or a window as asked (:func:`changes_state`), or it changed Yuki's own
    lists and rules (the to-do tool only when it writes: ``list`` is a read). Opening, switching, clicking,
    scrolling and keys are getting there, not results. Machine fields only."""
    args = tool_input if isinstance(tool_input, dict) else {}
    about = _about(args)
    if about.get("sends") or about.get("changes") or _squash(about.get("effect")) or changes_state(name, args):
        return True
    if name == "todo":
        return str(args.get("action") or "") in TODO_WRITES
    if name == "watch_for":
        return str(args.get("action") or "add") != "list"   # second review V2: listing the watches is a read
    return name in KEEPING_TOOLS


def _joined(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _opened(steps: list[dict[str, Any]]) -> list[str]:
    """What the run's opening steps opened, in order, once each ("Discord", "a page in Arc")."""
    out: list[str] = []
    for st in steps:
        name, args = str(st.get("name") or ""), st.get("input") or {}
        if st.get("ok") is True and name in OPENING_TOOLS and not is_effect(name, args):
            thing = describe_step(name, args, done=True).removeprefix("Opened ")
            if thing not in out:
                out.append(thing)
    return out


def achievement(steps: list[dict[str, Any]]) -> str:
    """What a finished run achieved, in plain past tense ("Messaged Ana on Discord", "Closed Slack"): the return
    card's title, the strip's final word and the thread's final line, one line for all three.

    In order: the brain's ``achieved`` on its ``done`` call (:func:`plain_achieved`); else the done words of the
    send that happened, else of the last effect that worked (:func:`is_effect`); else what the run opened. ``""``
    when no step says (the caller keeps the reply). Never just "Done": every source is at least two words."""
    for st in reversed(steps):
        if str(st.get("name") or "") == "done":
            line = plain_achieved((st.get("input") or {}).get(ACHIEVED))
            if line:
                return line
            break
    worked = [st for st in steps if st.get("ok") is True and is_effect(str(st.get("name") or ""), st.get("input"))]
    sent = [st for st in worked if _about(st.get("input") or {}).get("sends")]
    pick = (sent or worked)[-1] if (sent or worked) else None
    if pick is not None:
        return describe_step(str(pick.get("name") or ""), pick.get("input") or {}, done=True, limit=ACHIEVED_CHARS)
    opened = _opened(steps)
    return "Opened " + _joined(opened) if opened else ""


def failed_row(name: str, tool_input: dict[str, Any] | None) -> str:
    """An effect that did not happen: ``Didn’t finish closing Slack`` (the running words, whatever tense the brain's
    ``what`` was in)."""
    running = describe_step(name, tool_input)
    return f"Didn’t finish {running[:1].lower() + running[1:]}"


#: honest (87): the card's header for a run that ended done, by its outcome verdict's level (``yuki.agent.finish``).
LEVEL_HEADS: dict[str, tuple[str, str]] = {"unverified": ("dash", "Not confirmed"), "failed": ("x", "Didn’t work")}


#: honest review F7: the title of a run below likely when no step names what it tried.
ATTEMPTED_TITLE = "Tried what you asked"


def attempted(steps: list[dict[str, Any]], verdict: dict[str, Any] | None) -> str:
    """honest review F7: the one line for a run that ended done below likely (the card's title, the strip's final word
    and the window's line), in the running words of what it tried ("Sending Ana the message", "Quitting Slack"),
    never the brain's past-tense claim: an unconfirmed send's own step, else the step the verdict turned on (its
    ``what``), else the last effect, else :data:`ATTEMPTED_TITLE`. Machine fields only."""
    verdict = verdict if isinstance(verdict, dict) else {}
    if verdict.get("send") == "unconfirmed":
        sends = [st for st in steps if _about(st.get("input") or {}).get("sends")]
        if sends:
            return describe_step(str(sends[-1].get("name") or ""), sends[-1].get("input") or {}, limit=ACHIEVED_CHARS)
    step = " ".join(str(verdict.get("step") or "").split())
    if step:
        return describe_step("", {WHAT: step}, limit=ACHIEVED_CHARS)
    effects = [st for st in steps if str(st.get("name") or "") != "done"
               and is_effect(str(st.get("name") or ""), st.get("input"))]
    if effects:
        return describe_step(str(effects[-1].get("name") or ""), effects[-1].get("input") or {}, limit=ACHIEVED_CHARS)
    return ATTEMPTED_TITLE


def final_line(steps: list[dict[str, Any]], verdict: dict[str, Any] | None) -> str:
    """The one line a run that ended done is shown with: what it achieved (:func:`achievement`), or below likely what
    it tried (:func:`attempted`)."""
    level = str((verdict or {}).get("level") or "") if isinstance(verdict, dict) else ""
    return attempted(steps, verdict) if level in LEVEL_HEADS and verdict.get("acted") else achievement(steps)


def _sentence_case(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text[:1].upper() + text[1:] if text else ""


def level_row(verdict: dict[str, Any] | None) -> dict[str, str] | None:
    """honest (87): the done card's first row, the run's outcome level and what its check saw ("Checked on screen: the
    message shows in the chat"; "Not confirmed: the message did not show up in the chat"; "Didn’t work: closing
    Slack"), or ``None`` (no verdict, or an answer with nothing done)."""
    if not isinstance(verdict, dict) or not verdict.get("acted") or not verdict.get("label"):
        return None
    level = str(verdict.get("level") or "")
    label = str(verdict["label"])
    if level == "failed":
        step = " ".join(str(verdict.get("step") or "").split())
        what = step[:1].lower() + step[1:] if step else str(verdict.get("seen") or "")
        return {"text": f"{label}: {what}" if what else label, "icon": "dash"}
    seen = " ".join(str(verdict.get("seen") or "").split())
    return {"text": f"{label}: {seen}" if seen else label, "icon": "check" if level in ("verified", "likely")
            else "dash"}


def graded_reply(text: str, verdict: dict[str, Any] | None) -> str:
    """honest (87): a reply shown as written beside its run's outcome level when that is below likely (the reply is
    the thinker's claim; the level is what was checked): ``"<reply>\\n\\nNot confirmed: <what the check saw>."``."""
    if not isinstance(verdict, dict) or verdict.get("level") not in ("unverified", "failed"):
        return text
    row = level_row(verdict)
    if row is None:
        seen = " ".join(str(verdict.get("seen") or "").split())
        row = {"text": f"Not checked: {seen}" if seen else "Not checked"}
    line = _sentence_case(row["text"]).rstrip(".") + "."
    return f"{text}\n\n{line}" if (text or "").strip() else line


def return_view(outcome: str, text: str, steps: list[dict[str, Any]],
                verdict: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the return card says about a run that acted, from how it ended and its steps.

    Args:
        outcome: ``done`` | ``stopped`` | ``failed`` | ``handed`` (the real end, :meth:`YukiUi._settle`).
        text: The closing text, used only for a hand-back. A finished run's title is what it achieved
            (:func:`achievement`), never the reply; a failed run's is :data:`FAILED_TITLE`, never the loop's error.
        steps: The run's calls in order: ``{"name", "input" (with ``_about``; ``achieved`` on ``done``), "ok" (True /
            False / None: not finished)}``.

    Returns:
        ``{"icon", "head", "title", "rows": [{"text", "icon"}] (at most :data:`RETURN_ROWS`; ``check`` for what
        happened, ``dash`` for an effect that did not; a row that only repeats the title is left out), "more" (how
        many rows did not fit), "worth"}``: ``worth`` is False when there is nothing to tell beyond the strip's final
        word. No row carries an undo of any kind (the user, 3 Oct 19:10: "dont show the undo options in this card").
    """
    icon, head = OUTCOME_HEADS.get(outcome, OUTCOME_HEADS["done"])
    level = str((verdict or {}).get("level") or "") if isinstance(verdict, dict) else ""
    if outcome == "done" and level in LEVEL_HEADS:
        # honest (87): a run that ended with words its checks do not back is headed by the checked level
        icon, head = LEVEL_HEADS[level]
        if level == "failed" and (verdict or {}).get("partial"):
            head = "Partly done"
    unconfirmed_send = isinstance(verdict, dict) and verdict.get("send") == "unconfirmed"
    # second review V2-4: below likely, a step that ran is drawn with a check only when its own check showed it (the
    # verdict's ``checked`` keys); the rest the level row speaks for, never a check mark
    below = outcome == "done" and level in LEVEL_HEADS and bool((verdict or {}).get("acted"))
    checked = set((verdict or {}).get("checked") or []) if below else set()
    rows: list[dict[str, str]] = []
    sent_ok = sent_missed = False
    for st in steps:
        name, args, ok = str(st.get("name") or ""), st.get("input") or {}, st.get("ok")
        about = _about(args)
        if about.get("sends"):
            sent_ok = sent_ok or ok is True
            sent_missed = sent_missed or ok is not True
        if not is_effect(name, args):
            continue
        if about.get("sends") and unconfirmed_send:
            continue   # pressed, never seen in the chat: the level row says so, never a check mark
        if ok is True and below and effect_key(name, args) not in checked:
            continue
        if ok is True:
            row = {"text": describe_step(name, args, done=True), "icon": "check"}
        elif about.get("sends"):
            continue   # said once, in the title ("Nothing was sent.")
        else:
            row = {"text": failed_row(name, args), "icon": "dash"}
        if row not in rows:
            rows.append(row)
    opened = _opened(steps)
    if opened:
        rows.insert(0, {"text": "Opened " + _joined(opened), "icon": "check"})
    nothing_sent = sent_missed and not sent_ok
    worth = bool(rows) or nothing_sent
    if outcome == "stopped":
        title = "Stopped before it finished." + (f" {NOTHING_SENT}" if nothing_sent else "")
    elif outcome == "failed":
        title = FAILED_TITLE + (f" {NOTHING_SENT}" if nothing_sent else "")
    elif outcome == "handed":
        title = " ".join(str(text or "").split()) or "Handed back. You have the controls."
    else:
        title = final_line(steps, verdict) or FINISHED_TITLE   # review F7: below likely, what it tried
        rows = [r for r in rows if r["text"] != title]
        graded = level_row(verdict)
        if graded is not None:
            rows.insert(0, graded)   # honest (87): what was achieved, with its level, first
            # a level below likely is news the strip's final word does not carry: the card comes for it
            worth = worth or level in LEVEL_HEADS
    return {"icon": icon, "head": head, "title": title, "rows": rows[:RETURN_ROWS],
            "more": max(0, len(rows) - RETURN_ROWS), "worth": worth}


# -- a step's result, where a surface shows one beside its words ------------------------------------------------------


def result_words(ok: bool | None) -> str:
    """A finished step's result in plain words, from whether it worked only (the thread's step folds): nothing for a
    step that worked (its words and the check mark say it), "Didn’t work" for one that failed. Never the tool's own
    summary (shell output, timings, ids), which stays in the log."""
    return "Didn’t work" if ok is False else ""


# -- the voice's hands ----------------------------------------------------------------------------------------------

#: The voice's hands (``yuki.voice.hands.HAND_NAMES``) whose thinker tool has another name (the map
#: ``yuki.voice.hands.judged_call`` uses); the rest share the thinker tool's name or have their own words.
HAND_AS_TOOL: dict[str, str] = {"window": "window_action", "type": "type_text", "open_app": "launch_app"}


def hand_input(name: str, args: Any) -> tuple[str, dict[str, Any]]:
    """A voice hand's call as ``(tool, input)`` for :func:`describe_step`: the thinker's tool name and the fields
    the words are built from (an opened app's name as ``query``, a scroll's direction as the sign of ``dy``, a media
    key's fixed enum as ``media``). Never the typed text, the address or the window and item numbers."""
    a = args if isinstance(args, dict) else {}
    out = label_input(a)
    if name == "open_app" and isinstance(a.get("name"), str):
        out["query"] = a["name"]
    if name == "scroll" and a.get("direction") in ("up", "down"):
        out["dy"] = 1 if a["direction"] == "up" else -1
    if name == "media_key" and isinstance(a.get("key"), str):
        out["media"] = a["key"]
    return HAND_AS_TOOL.get(name, name), out


def describe_hand(name: str, args: Any, *, app: Any = None, done: bool = True) -> str:
    """A voice hand's step in the same words as the thinker's (``Closed Slack``, ``Turned Spotify down``)."""
    tool, inp = hand_input(name, args)
    if app:
        inp[ABOUT] = {"app": app}
    return describe_step(tool, inp, done=done)


def took_words(seconds: float) -> str:
    """How long a run took, for the card's footer: ``41 s``, ``1 min 9 s``, ``1 h 2 min``."""
    secs = max(1, int(round(seconds)))
    if secs < 60:
        return f"{secs} s"
    if secs < 3600:
        return f"{secs // 60} min {secs % 60} s" if secs % 60 else f"{secs // 60} min"
    return f"{secs // 3600} h {secs % 3600 // 60} min"
