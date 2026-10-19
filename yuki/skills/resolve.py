"""The resolver: code fills a skill's slots and picks its rung (PLAN.md Phase S; map-skills S3.2; decisions D14, D17).

:func:`resolve` takes the skill the talker (or the thinker) picked from the menu, the slots it gave (from the user's
words, exactly as said, or a name the menu listed) and the user's own words, and returns one of:

* :class:`Plan` - the filled values, where each came from, which guessed values the done line names, and the ladder
  of rungs that may run now;
* :class:`Ask` - one short question (a required slot with no value, two candidates that tie, a call live and no app
  named);
* :class:`Held` - a game is in front and no rung that neither takes focus nor sends input may run: "after the round?"
  (D17: never a hand-off to the thinker with a game in front);
* :class:`Handoff` - no rung may run (none fits, none is trusted): rung C, the thinker with the skill pinned.

It runs in a few milliseconds on the store's own reads (``store``: the tray's ``MemoryClient.store``, or a copy in an
eval). Step by step:

1. **Validate what was given** against every turn of the current exchange (``words``: the user's turns since the
   last finished job, newest last) and any older turns of the conversation the caller passes (``earlier``). An enum
   value must be one of its options, or a boolean that spells exactly one of them (lSkills, live 17 Oct: ``mute:
   true`` for on / off, :func:`bool_option`); numbers are numbers in range; ``points`` is a signed number or the
   schema's direction words ``up`` / ``down``; an entity or a text must stand in the user's words as whole words
   (:func:`yuki.skills.uia_route.said`) or be a name the menu listed (taste; an entity slot's known values and
   ``app.way``'s control labels, :func:`_listed`; a live way's key, :func:`_way_given`) or a told value; an entity
   value no taste row is called that sounds like exactly one listed name is that name (lSkills, live 17 Oct: "Lumin" for
   lumen with ``[playlist: lumen | ネオン]`` listed; :func:`yuki.skills.taste.heard_as`). A person (a ``person``
   slot, or a text slot the capability binds to a ``contact``: ``message``'s ``person``) may also be a name the
   people index resolves to exactly one person whom the words name as heard: a learned misheard form ("hey meant"),
   a transliteration ("हेमंत") or a close mishearing by sound (:func:`yuki.memory.contacts.names_person`); a group
   chat or channel still needs its name as said. A name the user said in another script than their people's names
   runs as the one person it reads as ("हेमंत" as Hemanth: a conversion). A Latin name the user said that none of
   their people goes by as written but that sounds like exactly one of them is asked about once
   (:data:`CONFIRM_PERSON`: "Did you mean Hemanth?"), never swapped: sound only confirms a name the talker or thinker
   inferred, and the same name given again within the exchange runs as said. lResolve (live test 17 Oct, problem 4):
   on a draft or an open (a capability that may send, on a run that doesn't), a name whose closest sound is a heard
   form of that person the user confirmed (:func:`yuki.memory.contacts.sounds_alike` ``confirmed``: "hey mean" near
   their confirmed "hey meant") runs as that person unasked, the done line naming them; a send still asks. A person
   the caller gave that the words don't hold (the talker's "Hemanth" for the heard "Hemish") is asked about
   (:data:`CONFIRM_PERSON`) when a run of the user's own words sounds like that person and nobody else
   (``sounds_alike``), never handed to the thinker; the question is noted with the user's turns
   (:func:`yuki.memory.contacts.note_alike_asked`), so the caller's same name after their answer is that person
   (``names_person``), and a send to them is typed and left by the chat driver, never sent by sound. An app may also
   be one whose known names (its key, a web address's name, a chat app's title, the names the user's timeline shows
   it under) stand in the words, or are heard there by sound ("this card" for Discord, "Ark" for Arc); a site (a
   text slot bound to a site) one whose identity stands there ("youtube.com" for "YouTube"). **Phase 2.1:** on an
   undoable run (no send, no commit) a value that fails these checks is the caller's reading and is accepted as given,
   tagged where it came from (:attr:`Plan.sources`, :data:`GUESS_SOURCES`) and named in the done line; on a lasting
   run (a send, a join), and for a person on any run, a value that fails is dropped and its slot name returned
   (``dropped``), never "corrected". One exception, on the new brain only: a text
   (the message, the person) that is exactly what the brain's consent rule allowed and counted down for this very run
   (``granted``, failure fix-plan Item 11's L2: Yuki's wording of what the user asked to say) is kept as given, filled
   :data:`FROM_GRANT`. And the 10 Oct decision, "a message Yuki words itself stays a draft" (lResolve): the free text
   of a capability that may send (what to write: a text slot bound to nothing), given in the caller's own wording, on a
   run that doesn't send (its ``sends_when`` data on the values, ``send = "draft"``), is kept as given, filled
   :data:`FROM_WORDED`, and noted for the step runner (:func:`yuki.skills.uia_route.note_draft`), which types exactly
   it in a run that only types (never one that sends or presses anything after it). A send stays the user's words or
   the brain's grant.
2. **Fill what is missing**, in :data:`FILL_ORDER`: what the user said always first (an app their words name, the
   newest turn that names one, for an app slot; :func:`named_app`; never an app Yuki itself just acted in
   (:func:`just_acted`: a turn may name it to complain), and with a call live, for a slot that asks in a call, only
   the newest turn and never the call's own app (:func:`call_apps`); when the caller gave an app the words don't hold
   and the words name another, the two readings disagree and it is asked with the slot dropped, never swapped), so a
   value they said beats a learned skill's bound value (only its default; a value they said for a bound slot that is
   another one runs the capability), its fill policy, a preference and a live guess -> "again" (the last run's
   numbers within 10 minutes) -> told -> a live fact (``playing``: the app playing now; two playing is a question)
   -> the preference
   (:func:`yuki.skills.prefs.choose`) -> taste (:func:`yuki.skills.taste.top`) -> the connection's default. A text
   the caller gave for what to write that the words don't hold is asked about on a send (a draft keeps it, step 1),
   never replaced by its default.
3. **Name what was not said.** A value from a preference that is not the usual, from taste or from a default is
   named in the done line (the slot's ``say``), so one word corrects it.
4. **Pick the rungs:** each route's ``when`` conditions on the values, its trust (:func:`yuki.skills.registry.usable`)
   and, with a game in front, only routes that take no focus and whose data allows a game (``game = "always"``, or
   ``"states"`` in a window state the live checks proved).

``eval_resolver(db_path=<copy>)`` builds the requests suite's resolver (``yuki.eval.habits_replay``): one cached
talker-model call per phrasing with the real menu, then this code's slot fill as of the phrasing's time.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Collection, Mapping, Sequence

from yuki.skills.catalog import load
from yuki.skills.connection import Loaded, Route, Skill, Slot, fill

#: "again" repeats a run of the same skill at most this old.
AGAIN_S = 600.0
#: The schema's direction words for a ``points`` slot with no amount said (``run_skill``'s documented values).
DIRECTIONS: dict[str, float] = {"up": 1.0, "down": -1.0}
#: What the user hears when a game is in front and nothing may run.
HELD_LINE = "You're in a game: after the round?"
#: Where a slot's value comes from; the first that has one wins, and the slot loop in :func:`resolve` keeps exactly
#: this order. What the user said is always first (a value given and found in their words, or an app their words
#: name): a learned skill's bound value is only the slot's default, its fill policy only its route's fallback, and a
#: live "playing" fact only a guess, so none of them can replace it.
FILL_ORDER: tuple[str, ...] = ("words", "again", "told", "live", "pref", "taste", "default")
#: Where a given value came from when the words don't hold it but the brain's consent grant for this very run allowed
#: exactly it (``resolve``'s ``granted``; failure fix-plan Item 11's L2): kept in the given slot's own place in the slot
#: loop, right after "words" and before every fill, so it is never a guess and never fills an empty slot.
FROM_GRANT = "granted"
#: Where a given value came from when it is the caller's own wording of what to write (a text the words don't hold) and
#: the run doesn't send (lResolve; the 10 Oct decision, "a message Yuki words itself stays a draft"): kept in the given
#: slot's own place, right after "words" and before every fill, so it is never a guess and never fills an empty slot;
#: the step runner types it as a draft only (:func:`yuki.skills.uia_route.note_draft`). A code word in the logs.
FROM_WORDED = "worded"
#: Where a value came from when Yuki chose it because the user asked it to choose ("a video I like", "put something
#: on": ``choose = true``; kTaste, contract C2, :func:`yuki.skills.taste.choose`): kept in the slot's own place right
#: after "words" (what the user named still wins: the engine chooses only when they named nothing), named in the done
#: line by the engine, and a guess for taste (``taste.GUESS_FILLS``: Yuki's own pick never feeds itself).
FROM_CHOSEN = "chosen"
#: Where a given value came from when the words don't hold it and it is kept as given anyway, for its route to refuse:
#: today only ``app.way``'s ``way`` naming none of the user's live ways (kWays review L1: the route asks "Which of your
#: ways?", never runs another way in its place). The engine's word for a given value too (``engine.FROM_GIVEN``).
FROM_GIVEN = "given"
#: Phase 2.1 (18 Oct "free thinker", reasoning.md 1.2 D: five correct readings vetoed in test A, open_excalidraw 16.9 s
#: to a fail): where a value the caller gave came from, each a code word in RAM (:attr:`Plan.sources`) and in the logs.
#: ``words``: it stands in the user's words (as written, or its identity: "youtube.com" for "YouTube"); ``heard``: it
#: is heard there by sound or a learned heard form ("Ark" for Arc, "Lumin" for lumen, "hey meant" for Hemanth); ``menu``:
#: the caller's own pick of a name the menu or the card lists for that slot, the words not holding it (it passed the
#: pick's why, or said ``menu``; a listed name the user pointed at, "the second one", stays ``words``); ``card``: one of
#: the user's own known sites or things the words only hint at ("my anime" -> justanime.to); ``screen``: the caller
#: read it on screen (its own word, :data:`CALLER_SOURCES`); ``reading``: the model's own reading of their words that
#: nothing above holds ("Pourbex caladra" -> excalidraw.com). On an undoable run the last four are accepted as given
#: (the model's reading wins: a slip costs one "not that") and named in the done line (:data:`GUESS_SOURCES`); on a
#: lasting run (a send, a join) and for a person they are never accepted.
FROM_WORDS = "words"
FROM_HEARD = "heard"
FROM_MENU = "menu"
FROM_CARD = "card"
FROM_SCREEN = "screen"
FROM_READING = "reading"
#: The sources that are Yuki's guess at what they meant: the done line names the value (:func:`done_line`), "not
#: that" takes the next one (the engine), and none of them is ever counted as the user's own word by the ledger
#: (``skill_runs.filled`` keeps the word itself: never ``words``).
GUESS_SOURCES: tuple[str, ...] = (FROM_MENU, FROM_CARD, FROM_SCREEN, FROM_READING)
#: The words a caller may give for where a value it gave came from (``resolve``'s ``given_from``): only ever a tag on
#: a value the words don't hold, never a reason to accept one on a lasting run.
CALLER_SOURCES: tuple[str, ...] = (FROM_CARD, FROM_SCREEN, FROM_MENU, FROM_READING)
#: ``app.way``'s slots (connections/ways.toml, contract C4): which way (a live way's key or name), and where in the app
#: (a control's label the menu lists, one of their things, or an item's own words).
WAY_CAPABILITY = "app.way"
WAY_SLOT = "way"
WAY_ITEM_SLOT = "item"
#: The slot types code checks with no words at all (an option, a number, a switch): how a ``sends_when`` slot's given
#: value is read before the slot loop (:func:`_sends`).
CODE_TYPES: tuple[str, ...] = ("enum", "number", "points", "bool")
#: The user's apps as their timeline shows them (:func:`known_apps`) are read again at most this often per database.
APPS_TTL_S = 600.0
#: The bind source that marks a text slot as a person (``message``'s ``bind = { person = "contact" }``).
PERSON_BIND = "contact"
#: The bind source that marks a text slot as a site (``web.open_page``'s ``bind = { site = "site" }``).
SITE_BIND = "site"
#: The bind source that marks a text slot as one of a site's page kinds (``web.open_page``'s ``page``): data, never a
#: reading (Phase 2.1).
PAGE_BIND = "page"
#: skill19 (R7): the key :func:`_site_page_given` marks a page item with when it is the user's own name matched in their
#: words in place of the caller's value (a guess): internal, never a slot name (no slot may hold a colon).
SITE_GUESS_KEY = ":guessed_item"
#: The question when the user said a name none of their people (nor a group chat or channel) goes by, and it sounds
#: like exactly one of them: asked, never swapped (a message to someone who merely sounds alike is a wrong send).
CONFIRM_PERSON = "Did you mean {name}?"
#: How far back "what Yuki just did" reaches (:func:`just_acted`): the exchange's turns span about 120 s (fix-plan
#: IF-words), and the run they may talk about ended before them (the longest run tonight took 47 s).
JUST_ACTED_S = 180.0
#: A confirm question (:data:`CONFIRM_PERSON`) is asked once per name within this long (the exchange's span): the same
#: literal name given again means the answer was no, so it runs as said and the chat driver says it doesn't know who
#: that is, rather than asking the same question again. RAM only, never logged.
CONFIRM_ONCE_S = 120.0


@dataclass
class Live:
    """The live facts the resolver may use (built by the caller: the voice's own picture, the Now board, local reads;
    an eval passes none): the game in front, the app keys of media sessions playing now, the app in front, whether a
    call is live, and a window-state reader for the "states" game rule."""

    game: str = ""
    playing: tuple[str, ...] = ()
    front_app: str = ""
    in_call: bool = False
    window_state: Callable[[str], str | None] | None = None


@dataclass
class Plan:
    skill: Skill
    values: dict[str, Any]
    filled: dict[str, str]
    named: list[str]
    routes: list[Route]
    skipped: dict[str, str] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    game: str = ""
    ask_keep: dict[str, Any] | None = None
    handoff: bool = True
    #: Phase 2.1: each slot the caller gave a value for -> where that value came from (:data:`FROM_WORDS` ...
    #: :data:`FROM_READING`, or a fill word such as ``granted`` / ``worded`` / ``given`` / ``chosen``). RAM and code
    #: words only.
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return "plan"

    @property
    def guessed(self) -> list[str]:
        """The slots whose value is Yuki's guess at what they meant (:data:`GUESS_SOURCES`), in slot order."""
        return [k for k, src in self.sources.items() if src in GUESS_SOURCES]


@dataclass
class Ask:
    skill: str
    slot: str
    question: str
    options: list[str] = field(default_factory=list)
    #: The slots whose given value failed validation (names only), so a caller can tell a question about a value it
    #: gave and the resolver dropped (``slot in dropped``) from one about a value nobody gave.
    dropped: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return "ask"


@dataclass
class Held:
    skill: str
    game: str
    say: str = HELD_LINE
    why: str = "game"

    @property
    def kind(self) -> str:
        return "held"


@dataclass
class Handoff:
    skill: Skill
    values: dict[str, Any]
    filled: dict[str, str]
    why: str
    skipped: dict[str, str] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return "handoff"


@dataclass
class Refused:
    skill: str
    why: str

    @property
    def kind(self) -> str:
        return "refused"


Result = Plan | Ask | Held | Handoff | Refused


# ---------------------------------------------------------------------------------------------------------------------
# Step 1: validate what was given
# ---------------------------------------------------------------------------------------------------------------------


def _words(value: Any) -> str:
    return " ".join(str(value or "").split())


def _said(text: str, words: Sequence[str]) -> bool:
    from yuki.skills.uia_route import said

    return said(text, [str(w) for w in words if w])


def _heard(name: str, words: Sequence[str]) -> bool:
    """Whether the words hold a run that sounds like the app name ``name`` (:func:`yuki.memory.contacts.sounds_in`:
    the same sound key and onset and a close spelling; a short name such as "Arc" within one letter of three, "Ark")."""
    try:
        from yuki.memory.contacts import sounds_in

        return sounds_in(name, [str(w) for w in words if w], short=True) is not None
    except Exception:  # noqa: BLE001
        return False


def _site_said(value: str, words: Sequence[str]) -> bool:
    """Whether a site value's identity stands in the words (:func:`yuki.skills.connection.scope_value`: a site file's
    name for an address of one of its hosts, a domain without ``www.``, and that domain's name): "youtube.com" given
    for "open YouTube in Arc"."""
    try:
        from yuki.skills.connection import domain_like, scope_value
    except Exception:  # noqa: BLE001
        return False
    ident = scope_value(value)
    forms = [ident] if ident else []
    if ident and domain_like(ident):
        try:
            from yuki.memory.moments import registrable_domain

            forms.append(registrable_domain(ident).split(".")[0])
        except Exception:  # noqa: BLE001
            pass
    # Phase 2.1: a site the words only approximate ("Pourbex caladra" for Excalidraw) is no longer guessed by string
    # distance here; the caller's reading is accepted on an undoable run and named as a guess (``resolve``)
    return any(f and f != _words(value).casefold() and _said(f, words) for f in dict.fromkeys(forms))


def _site_page_given(skill: Skill, given: Mapping[str, Any], words: Sequence[str], store: Any,
                     now: float, heard: Sequence[str] | None = None) -> dict[str, Any]:
    """fSkills: for a capability that binds a site, a page kind and an entity (web.open_page), the page and item values
    :func:`yuki.skills.web_route.site_page` reads from what the caller gave (a page kind of that site as data; a name
    given as the page, or an item, that is one of the user's own names for that site's pages, as written or by sound
    in their words). ``{slot: value}`` for the slots it settled; ``{}`` for any other capability. Never raises."""
    bind = dict(getattr(skill, "bind", None) or {})
    site_slot = next((k for k, v in bind.items() if v == SITE_BIND), None)
    page_slot = next((k for k, v in bind.items() if v == "page"), None)
    item_slot = next((k for k, v in bind.items() if v == "entity"), None)
    if not (site_slot and page_slot and item_slot) or site_slot not in given or \
            (page_slot not in given and item_slot not in given):
        return {}
    try:
        from yuki.skills.web_route import site_page

        kind, item, how = site_page(store, given[site_slot], given.get(page_slot), given.get(item_slot), words,
                                    now=now, heard=heard)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, Any] = {}
    if kind:
        out[page_slot] = kind
    if item:
        out[item_slot] = {"name": item}
        if how == "heard_words":
            # skill19 (R7): their own name matched in their words in place of the caller's rewrite: Yuki's guess,
            # named in the done line ("I took that as NDL"), so one word corrects it
            out[SITE_GUESS_KEY] = item_slot
    return out


def _truth(value: Any) -> bool | None:
    """A value as a boolean: a JSON boolean as itself, a string in the standard library's boolean spellings
    (``configparser.ConfigParser.BOOLEAN_STATES``: 1/yes/true/on, 0/no/false/off, case aside), else ``None``. A data
    format conversion (how a tool argument spells a switch), never the meaning of the user's words."""
    if isinstance(value, bool):
        return value
    if not isinstance(value, (str, int)):
        return None
    from configparser import ConfigParser

    return ConfigParser.BOOLEAN_STATES.get(_words(value).casefold())


def bool_option(options: Sequence[str], value: Any) -> str | None:
    """lSkills: the one option of an enum slot that spells the same switch as a boolean ``value`` (``True`` -> "on" of
    ``["on", "off"]``, through :func:`_truth` on both sides), else ``None`` (no boolean, or options that aren't a
    switch: ``["in_app", "mixer"]``)."""
    truth = _truth(value)
    if truth is None:
        return None
    hits = [o for o in options if _truth(o) is truth]
    return hits[0] if len(hits) == 1 else None


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _point_token(token: str) -> str:
    """A token as a points value may spell it: its letters, digits, sign and decimal point (a trailing "%" or a comma
    off). Format only."""
    return "".join(ch for ch in token if ch.isalnum() or ch in "+-.").strip(".")


def points_value(value: Any) -> str | float | None:
    """skill19 (live 19 Oct, spotify RC5a: ``change: "down a little"`` was dropped and the skill asked "Louder,
    quieter, or to what level?"): a ``points`` slot's value in the schema's own form, ``None`` when it holds none.
    A number (or a numeric string, "+5", "-10%") that isn't 0 is itself; a value whose words hold exactly one of the
    schema's direction words (:data:`DIRECTIONS`: "down a little", "a bit up") is that direction, signed by it when
    it also holds one number ("down 10" -> -10). Two directions, two numbers, or neither: ``None``. The schema's own
    words found inside the value, never a guess at what other words mean ("quieter" holds none: dropped, and the
    caller is told the form, ``menu.slot_form``)."""
    if isinstance(value, bool):
        return None
    x = _num(value)
    if x is not None:
        return x if x != 0.0 else None
    if not isinstance(value, str):
        return None
    tokens = [_point_token(t) for t in _words(value).casefold().split()]
    tokens = [t for t in tokens if t]
    dirs = {t for t in tokens if t in DIRECTIONS}
    nums = [n for n in (_num(t) for t in tokens) if n is not None]
    if not dirs:
        # a number alone ("-10%") is itself; a number among other words ("10 lower") says no direction of the schema's
        return nums[0] if len(tokens) == 1 and len(nums) == 1 and nums[0] != 0.0 else None
    if len(dirs) > 1 or len(nums) > 1:
        return None
    way = next(iter(dirs))
    if nums:
        return DIRECTIONS[way] * abs(nums[0]) if nums[0] != 0.0 else None
    return way


def app_name(value: Any) -> str:
    """An app as a slot keeps it: its key (``"Spotify.exe"`` / ``"Spotify"`` -> ``"spotify"``), the form the
    timeline and the mixer use (``yuki.memory.store.app_key``)."""
    try:
        from yuki.memory.store import app_key

        return app_key(_words(value)) or _words(value).casefold()
    except Exception:  # noqa: BLE001
        return _words(value).casefold()


def check_value(slot: Slot, value: Any, words: Sequence[str], *, menu_names: Sequence[str] = (),
                told: Any = None, people: Callable[[str], dict[str, Any] | None] | None = None,
                heard_person: Callable[[str, Sequence[str]], str | None] | None = None,
                app_names: Callable[[str], tuple[str, list[str]]] | None = None,
                site: bool = False, loose: bool = False) -> tuple[bool, Any]:
    """``(valid, canonical value)`` of one given slot value (step 1): :func:`_check` without where it was found."""
    ok, got, _how = _check(slot, value, words, menu_names=menu_names, told=told, people=people,
                           heard_person=heard_person, app_names=app_names, site=site, loose=loose)
    return ok, got


def _check(slot: Slot, value: Any, words: Sequence[str], *, menu_names: Sequence[str] = (),
           told: Any = None, people: Callable[[str], dict[str, Any] | None] | None = None,
           heard_person: Callable[[str, Sequence[str]], str | None] | None = None,
           app_names: Callable[[str], tuple[str, list[str]]] | None = None,
           site: bool = False, loose: bool = False) -> tuple[bool, Any, str]:
    """``(valid, canonical value, where it was found)`` of one given slot value (step 1). Identity and whole-word
    spans, plus checks on the user's own data: ``heard_person(name, words) -> the person's name | None`` (a person
    slot, or a text slot bound to a contact: the people index resolves the name to exactly one person whom the words
    name as heard, a transliteration or a close mishearing; the value becomes that person's own name, so "Hemant" for
    "हेमंत" runs as Hemanth), ``app_names(value) -> (key, names)`` (an app slot: the app's known names, any of which
    may stand in the words, or be heard there by sound; the app's key is kept) and ``site`` (a text slot the capability
    binds to a site: the site's identity may stand in the words instead, :func:`_site_said`). ``loose``: a listed name
    may also be heard by :func:`yuki.skills.taste.heard_as`'s closed-set tier (the caller's taste slots only; review
    M2). Where: :data:`FROM_WORDS` (as written, an identity, a told value, an option or a number), :data:`FROM_HEARD`
    (by sound or a heard form) or :data:`FROM_MENU` (a name listed for the slot that the words don't hold); ``""``
    when not valid."""
    if value is None or value == "":
        return False, None, ""
    t = slot.type
    if t == "enum":
        want = _words(value).casefold()
        hit = next((o for o in slot.options if o.casefold() == want), None)
        if hit is None:
            # lSkills (live 17 Oct): the talker sent ``mute: true`` (a JSON boolean) for options on / off, the value
            # was dropped and the volume skill asked "Louder, quieter, or to what level?"
            hit = bool_option(slot.options, value)
        return (hit is not None), hit, (FROM_WORDS if hit is not None else "")
    if t == "number":
        x = _num(value)
        if x is None or (slot.min is not None and x < slot.min) or (slot.max is not None and x > slot.max):
            return False, None, ""
        return True, x, FROM_WORDS
    if t == "points":
        got = points_value(value)
        return (got is not None), got, (FROM_WORDS if got is not None else "")
    if t == "bool":
        # a JSON boolean only (review r1 L5): the one shipped bool slot is discord.voice's join_in_call, a join gate
        # (true leaves the current call), so no string spelling ("yes", "on", "1") ever answers it
        if isinstance(value, bool):
            return True, value, FROM_WORDS
        return False, None, ""
    text = _words(value)
    if t == "app":
        if any(text.casefold() == o.casefold() for o in slot.options):
            return True, next(o for o in slot.options if o.casefold() == text.casefold()), FROM_WORDS
        # the app's identity and every name it goes by ("Discord.exe" / "discord.com" for "Discord", "Google Chrome"
        # for "Chrome"); kept by its key, the form the routes and the mixer compare
        key, names = app_names(text) if app_names is not None else (app_name(text), [text])
        key = key or app_name(text)
        if _said(text, words) or any(_said(n, words) for n in names):
            return True, key, FROM_WORDS
        if any(_heard(n, words) for n in names):       # by sound: "this card" for Discord
            return True, key, FROM_HEARD
        return False, None, ""
    if t == "text":
        if _said(text, words):
            return True, text, FROM_WORDS
        who = heard_person(text, words) if heard_person is not None else None
        if who:
            return True, who, FROM_HEARD
        if site and _site_said(text, words):
            return True, text, FROM_WORDS
        # kWays merge note 2 (review M1): a label the menu lists for this slot (``app.way``'s item: a way's control,
        # "New thread", whatever words the user used for it, "a fresh one"); only ``app.way``'s item slot is given
        # such names (:func:`_listed`), never a person or what to write
        listed = next((n for n in menu_names if _words(n).casefold() == text.casefold()), None)
        if listed is not None:
            return True, listed, FROM_MENU
        return False, None, ""
    if t == "person":
        who = None
        how = FROM_WORDS
        if not (_said(text, words) or text.casefold() in {_words(n).casefold() for n in menu_names}):
            who = heard_person(text, words) if heard_person is not None else None
            if not who:
                return False, None, ""
            how = FROM_HEARD
        got = people(who or text) if people is not None else None
        return (got is not None), got, (how if got is not None else "")
    # entity: a span of the words, a name the menu listed, or the told value
    listed = next((n for n in menu_names if _words(n).casefold() == text.casefold()), None)
    if listed is not None:
        return True, {"name": listed}, (FROM_WORDS if _said(text, words) else FROM_MENU)
    if told is not None and _words(told if not isinstance(told, Mapping) else told.get("name")).casefold() == \
            text.casefold():
        return True, (told if isinstance(told, Mapping) else {"name": _words(told)}), FROM_WORDS
    if _said(text, words):
        return True, {"name": text}, FROM_WORDS
    if menu_names:
        # lSkills: a value the words don't hold as written ("Lumin playlist") that sounds like exactly one listed name,
        # which the words do hold by sound ("Put on Lumin on Spotify"): that listed name
        from yuki.skills.taste import heard_as

        alike = heard_as(menu_names, text, loose=loose)
        if alike and any(heard_as([alike], str(w), loose=loose) for w in words if w):
            return True, {"name": alike}, FROM_HEARD
    return False, None, ""


# ---------------------------------------------------------------------------------------------------------------------
# Reads (the store's own; each never raises)
# ---------------------------------------------------------------------------------------------------------------------


def pref_option(slot: Slot, value: Any) -> str | None:
    """How a value is kept as a preference option: an enum, app or number as itself; an entity only by its taste key
    (a keyed digest, never its name in clear); a person or a text never (people routes live in ``contact_prefs``)."""
    if slot.type in ("enum", "app"):
        return str(value)
    if slot.type in ("number", "points") and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if slot.type == "entity" and isinstance(value, Mapping) and value.get("key"):
        return str(value["key"])
    return None


def from_pref(store: Any, slot: Slot, option: str) -> tuple[bool, Any]:
    """A preference option back as a slot value: an entity through its taste row (by key), the rest checked by type."""
    if slot.type == "entity":
        from yuki.skills import taste

        if not slot.taste:
            return False, None
        hit = next((t for t in taste.top(store, slot.taste, n=200) if t.entity_key == option), None)
        return (hit is not None), ({"name": hit.name, "key": hit.entity_key, "ref": hit.ref} if hit else None)
    if slot.type in ("person", "text"):
        return False, None
    return check_value(slot, option, [option], menu_names=[option])


def _listed(skill: Skill, slot: Slot, store: Any, *, now: float) -> list[str]:
    """The names the menu lists for a slot with no taste kind of its own, which step 1 accepts as listed (the slot
    loop's ``menu_names``; a speed-up: each route still matches what it is given against the user's own things):

    * an entity slot: the user's known values of the capability for that slot (contract C5,
      :func:`yuki.skills.learn.values.known`, Not mine left out: the menu's "theirs"; its label, the form a request
      passes), and, for a capability that joins a call, on its first entity slot (the channel the done line names),
      the channels a join was noted for (tastes ``<app>_voice``: the menu's "voice channels"; kHands merge note 4);
    * ``app.way``'s item slot: the labels of the user's ways' controls (kWays merge note 2:
      :func:`yuki.skills.learn.ways.control_labels`, ``item = "New thread"``).

    Any other slot: none (a person or what to write is never taken from a list). Never raises (``[]``)."""
    if store is None or slot.taste:
        return []
    cap = skill.capability or skill.key
    out: list[Any] = []
    try:
        if slot.type == "text":
            if cap != WAY_CAPABILITY or slot.name != WAY_ITEM_SLOT:
                return []
            from yuki.skills.learn.ways import control_labels

            return [n for n in dict.fromkeys(_words(x) for x in control_labels(store)) if n]
        if slot.type != "entity":
            return []
        from yuki.skills.learn.values import known

        out += [v.get("label") or v.get("value") for v in known(store, cap, now=now).get(cap) or []
                if v.get("slot") == slot.name]
        first = next((s for s in skill.slots if s.type == "entity"), None)
        if str(getattr(skill, "end", "") or "") == "call_join" and skill.app and first is slot:
            from yuki.skills import taste

            out += taste.candidates(store, f"{skill.app}_voice", now=now)
    except Exception:  # noqa: BLE001 - what was read so far
        pass
    return [n for n in dict.fromkeys(_words(x) for x in out) if n]


def _way_given(store: Any, value: Any) -> dict[str, Any] | None:
    """kWays merge note 1: the live way of the user's (``app.way``'s ``way`` slot) a given value names, by its key or
    its name as a menu line or the you-card shows it (case and spaces aside): ``{"name", "key"}``, the form the route
    compares (:func:`yuki.skills.uia_route.choose_way`). ``None``: none (or memory can't say). Never raises."""
    if store is None:
        return None
    if isinstance(value, Mapping):
        value = value.get("key") or value.get("name")
    want = _words(value).casefold()
    if not want:
        return None
    try:
        from yuki.skills.learn.ways import live_ways

        for w in live_ways(store):
            key, name = _words(w.get("key")), _words(w.get("name"))
            if want in (key.casefold(), name.casefold()) and key:
                return {"name": name or key, "key": key}
    except Exception:  # noqa: BLE001
        return None
    return None


def told_values(store: Any, skill_key: str) -> dict[str, Any]:
    """The values the user told for this skill (``skills.told_ciphertext``, encrypted JSON), decrypted in RAM."""
    if store is None:
        return {}
    try:
        rows = store._query("SELECT told_ciphertext FROM skills WHERE key = ? AND retired_at IS NULL", (skill_key,))
        if rows and rows[0][0]:
            got = json.loads(store._dec(rows[0][0]) or "{}")
            return got if isinstance(got, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _again(store: Any, skill_key: str, now: float) -> dict[str, float]:
    """The numbers of the last finished run of this skill within :data:`AGAIN_S` (the ledger keeps only numbers)."""
    if store is None:
        return {}
    try:
        rows = store._query(
            "SELECT evidence FROM skill_runs WHERE skill = ? AND t0 >= ? AND t0 <= ? AND outcome IN ('done', "
            "'unverified', 'already') ORDER BY t0 DESC LIMIT 1",
            (skill_key, now - AGAIN_S, now))
        ev = json.loads(rows[0][0] or "{}") if rows else {}
        nums = ev.get("numbers") if isinstance(ev, dict) else None
        return {str(k): float(v) for k, v in (nums or {}).items() if _num(v) is not None}
    except Exception:  # noqa: BLE001
        return {}


def _person(store: Any) -> Callable[[str], dict[str, Any] | None]:
    def find(text: str) -> dict[str, Any] | None:
        if store is None:
            return None
        try:
            from yuki.memory.contacts import named_in

            got = named_in(store, text)
        except Exception:  # noqa: BLE001
            return None
        if len({c.id for c, _w in got}) != 1:
            return None
        c = got[0][0]
        return {"name": getattr(c, "name", text), "id": getattr(c, "id", None)}

    return find


def _heard_person(store: Any) -> Callable[[str, Sequence[str]], str | None]:
    """``(name, words) -> the person's own name | None``: the people index resolves ``name`` to exactly one person, and
    one of the words' turns names that same person: a name or alias as written, a heard form memory learned ("hey
    meant"), a transliteration ("हेमंत") or a close mishearing (:func:`yuki.memory.contacts.names_person`). A group
    chat or channel is not a person here: its name must be said."""
    def check(name: str, words: Sequence[str]) -> str | None:
        if store is None:
            return None
        try:
            from yuki.memory.contacts import names_person

            got = names_person(store, name, [str(w) for w in words if w])
        except Exception:  # noqa: BLE001
            return None
        return (" ".join(str(getattr(got, "name", "") or "").split()) or None) if got is not None else None

    return check


def _sounds_like(store: Any) -> Callable[[str], tuple[str, int] | None]:
    """``name -> (the one person it sounds like, their id) | None`` for a name the user said literally: ``None`` when
    the people index finds anyone by that name as written (:func:`yuki.memory.contacts.resolve`, the chat driver's own
    lookup) or a group chat or channel goes by it; else the one person it sounds like (``resolve(..., heard=True)``),
    whom the resolver asks about (:data:`CONFIRM_PERSON`) rather than run a name the chat driver doesn't know or send
    to a person the user may not have meant."""
    def check(name: str) -> tuple[str, int] | None:
        if store is None:
            return None
        try:
            from yuki.memory import contacts as C

            if C.resolve(store, name):
                return None
            got = C.resolve(store, name, heard=True)
        except Exception:  # noqa: BLE001
            return None
        if len({c.id for c in got}) != 1:
            return None
        who = " ".join(str(getattr(got[0], "name", "") or "").split())
        pid = getattr(got[0], "id", None)
        return (who, int(pid)) if who and isinstance(pid, int) and not isinstance(pid, bool) else None

    return check


def _heard_confirmed(store: Any) -> Callable[[str, int], bool]:
    """``(name, person id) -> bool``: whether the name's closest sound among the user's people is a heard form of that
    very person the user confirmed (:func:`yuki.memory.contacts.sounds_alike`, lMsg: ``confirmed`` is a
    ``name_aliases`` correction, ``how = "heard"``, never their own name or a handle), and nobody else sounds like it.
    lResolve (live test 17 Oct, problem 4): "hey mean" near the confirmed "hey meant" is Hemanth, so a run that doesn't
    send never asks "Did you mean Hemanth?" about it again. Data only (memory's aliases and the fixed sound rules).
    Never raises (``False``)."""
    def check(name: str, person_id: int) -> bool:
        if store is None:
            return False
        try:
            from yuki.memory.contacts import sounds_alike

            got = sounds_alike(store, name)
        except Exception:  # noqa: BLE001
            return False
        return got is not None and bool(got.confirmed) and int(got.contact.id) == int(person_id)

    return check


def _answers_who(name: str, turns: Sequence[str]) -> bool:
    """Whether ``name`` is the user's direct answer to Yuki's own "Who to?" (the chat driver's or this resolver's
    question for a person slot nobody gave, noted by the engine: fMsg merge note E1): their very next turn, the name
    standing in it (:func:`yuki.skills.chat._answers_who`). Never raises (``False``)."""
    try:
        from yuki.skills.chat import _answers_who as answers

        return bool(answers(name, [str(t) for t in turns or () if t]))
    except Exception:  # noqa: BLE001
        return False


def _site_forms(value: Any) -> set[str]:
    """A site value's identities (fixed formats, never a reading of meaning): its words case-folded, the same with the
    spaces out ("Just Anime"), its scope identity (:func:`yuki.skills.connection.scope_value`: a site file's name, a
    domain without ``www.``) and, for a domain, its registrable domain's name ("justanime" for justanime.to)."""
    from yuki.skills.connection import domain_like, scope_value

    text = _words(value.get("name") if isinstance(value, Mapping) else value).casefold()
    ident = scope_value(text)
    forms = {f for f in (text, text.replace(" ", ""), ident) if f}
    if ident and domain_like(ident):
        try:
            from yuki.memory.moments import registrable_domain

            forms.add(registrable_domain(ident).split(".")[0])
        except Exception:  # noqa: BLE001
            pass
    return forms


def _registrable(ident: str) -> str:
    """A domain's registrable domain (``yuki.memory.moments.registrable_domain``: "app.excalidraw.com" ->
    "excalidraw.com"), else the identity as it is. Never raises."""
    if not ident:
        return ""
    try:
        from yuki.memory.moments import registrable_domain

        return str(registrable_domain(ident) or ident)
    except Exception:  # noqa: BLE001
        return ident


def _known_site(store: Any, cap: str, slot_name: str, value: Any, *, now: float) -> str | None:
    """fBrain merge note (contract C5): the user's own known site (the learner's values for ``cap``'s ``slot_name``,
    best first, Not mine left out: :func:`yuki.skills.learn.values.known`) that the given ``value`` is by identity
    (:func:`_site_forms`: its value, label or name), as that known value (the form its runs used); else ``None``.
    Data only. Never raises."""
    if store is None or not _words(value.get("name") if isinstance(value, Mapping) else value):
        return None
    try:
        from yuki.skills.connection import domain_like, scope_value
        from yuki.skills.learn.values import known

        want = _site_forms(value)
        # Phase 2.1 (every reading of a site passes here now): a value that is itself an address matches only the same
        # registrable domain ("github.com" is never their "x.github.io"); a name ("JustAnime") by its forms, as before
        ident = scope_value(_words(value.get("name") if isinstance(value, Mapping) else value).casefold())
        domain = _registrable(ident) if ident and domain_like(ident) else ""
        for v in known(store, cap, now=now).get(cap) or []:
            if v.get("slot") != slot_name or v.get("mark"):
                continue
            own = _words(v.get("value") or v.get("label"))
            if not own:
                continue
            if domain:
                theirs = {_registrable(scope_value(_words(v.get(k)).casefold())) for k in ("value", "label", "name")
                          if _words(v.get(k))}
                if domain in theirs:
                    return own
            elif any(want & _site_forms(v.get(k)) for k in ("value", "label", "name") if _words(v.get(k))):
                return own
    except Exception:  # noqa: BLE001
        return None
    return None


def _said_for(store: Any) -> Callable[[str, Sequence[str]], tuple[str, str, int] | None]:
    """R4 (lMsg's report, "What's left after round 1" item 3): ``(name, turns) -> (the person's own name, the user's
    words for them, their id) | None`` for a person-bound value the caller gave that the words don't hold (the
    talker's "Hemanth" for the heard "Hemish"): the one person ``name`` names as written
    (:func:`yuki.memory.contacts.resolve`), when a run of one to three of the user's own Latin words in ``turns``
    (newest turn first, then in reading order) sounds like that person and nobody else
    (:func:`yuki.memory.contacts.sounds_alike`: the strict, loose or tight sound rule over memory's names and heard
    forms; no one's name as written, no group chat's or channel's). Each run is first compared with that person's own
    names only (the same rules), so the people-wide lookup runs once per likely run, not once per run of words. A run
    in a Brahmic script is left out: a transliteration already counts as naming them (``names_person``). Never raises
    (``None``)."""
    def check(name: str, turns: Sequence[str]) -> tuple[str, str, int] | None:
        if store is None:
            return None
        try:
            from yuki.memory import contacts as C

            who = {int(c.id): c for c in C.resolve(store, name)}
            if len(who) != 1:
                return None
            person = next(iter(who.values()))
            forms = [f for f in (C.heard_form(n) for n in person.names) if f is not None and f.key]
            if not forms:
                return None
            for text in reversed([str(t) for t in turns or () if str(t or "").strip()]):
                for _start, said, run in C.heard_spans(text):
                    if run.script:
                        continue
                    one = len(C.spoken_words(said)) == 1
                    # contacts' own sound rules (the ones sounds_alike applies), on this person's names only
                    if not any(C._sound_ok(run, f) or C._alike(run, f) or (one and C._alike_tight(run, f))
                               for f in forms):
                        continue
                    got = C.sounds_alike(store, said)
                    if got is not None and int(got.contact.id) == int(person.id):
                        own = " ".join(str(person.name or "").split())
                        return (own, " ".join(said.split()), int(person.id)) if own else None
        except Exception:  # noqa: BLE001
            return None
        return None

    return check


def _note_asked(store: Any, said: str, person_id: int, turns: Sequence[str], now: float) -> bool:
    """Note the R4 question with the user's turns as they stand, in time order and the very list the answer is
    validated against (``resolve``'s ``sources``; :func:`yuki.memory.contacts.note_alike_asked`, the
    chat driver's own record of its sound-alike question), so an answer given after it counts
    (``contacts.alike_answered``: the caller's same name then validates through ``names_person``, and the chat driver
    types a send to that person and leaves it, never sends by sound). ``False`` when the same words were asked about
    within ``contacts.ALIKE_ANSWER_S`` (the same name again: the answer was no) or memory can't say. RAM only."""
    if store is None:
        return False
    try:
        from yuki.memory.contacts import note_alike_asked

        return bool(note_alike_asked(store, said, int(person_id), [str(t) for t in turns or () if t], now=now))
    except Exception:  # noqa: BLE001
        return False


def _sends(skill: Skill, given: Mapping[str, Any], told: Mapping[str, Any]) -> bool:
    """Whether this run sends (the capability's own ``sends`` / ``sends_when`` data,
    :func:`yuki.skills.connection.sends_with`), read before the slot loop fills its slots: each ``sends_when`` slot as
    the loop will fill it, a given value code checks alone (:data:`CODE_TYPES`: an option, a number, a switch), else a
    told value, else its default; a given value of any other type is read as given (erring toward "sends"). The slot
    loop's own values are checked again after it (``resolve``: a mismatch resolves again with nothing relaxed). Data
    only, never the words."""
    from yuki.skills.connection import sends_with

    if not getattr(skill, "sends", False):
        return False
    values: dict[str, Any] = {}
    for cond in getattr(skill, "sends_when", ()) or ():
        name = str(cond.get("slot") or "") if isinstance(cond, Mapping) else ""
        slot = skill.slot(name) if name else None
        if slot is None:
            continue
        raw = given.get(name)
        value: Any = None
        if raw is not None and raw != "":
            if slot.type in CODE_TYPES:
                ok, got = check_value(slot, raw, [])
                value = got if ok else None
            else:
                value = raw
        if value is None and slot.told and name in told:
            value = told[name]
        if value is None:
            value = slot.default
        values[name] = value
    return sends_with(skill, values)


def _worded_slot(skill: Skill, slot: Slot) -> bool:
    """A slot whose given value may be the caller's own wording on a run that doesn't send: the free text of a
    capability that may send (its data's ``sends``): a text slot bound to nothing (``message``'s ``text``: what to
    write), filled from the request (role ``fill``). Data only."""
    return bool(getattr(skill, "sends", False)) and slot.type == "text" and slot.role == "fill" \
        and not skill.bind.get(slot.name)


def _note_draft(skill_key: str, text: Any) -> None:
    """Tell the step runner the draft Yuki worded may be typed (:func:`yuki.skills.uia_route.note_draft`). Never
    raises."""
    try:
        from yuki.skills.uia_route import note_draft

        note_draft(skill_key, _words(text))
    except Exception:  # noqa: BLE001 - not noted: the steps refuse to type it, and nothing is typed or sent
        pass


def _written(store: Any) -> Callable[[str], str | None]:
    """``name -> the person's own name | None`` for a name the user said in another script than their people's names
    ("हेमंत"): the one person it reads as, letter for letter (:func:`yuki.memory.contacts.transliterated`: a
    conversion, not a mishearing), so it runs as that person without a question; ``None`` for a Latin name, or one
    that reads as nobody or as two people."""
    def check(name: str) -> str | None:
        if store is None:
            return None
        try:
            from yuki.memory.contacts import transliterated

            got = transliterated(store, name)
        except Exception:  # noqa: BLE001
            return None
        return (" ".join(str(getattr(got, "name", "") or "").split()) or None) if got is not None else None

    return check


_confirm_lock = threading.Lock()
#: (skill's capability, slot, the name as said, folded) -> when :data:`CONFIRM_PERSON` was last asked about it (RAM).
_confirms: dict[tuple[str, str, str], float] = {}


def _first_confirm(key: tuple[str, str, str], now: float) -> bool:
    """Whether the confirm question about ``key`` is new within :data:`CONFIRM_ONCE_S` (and note it asked now). The
    same literal name given again after the question means the answer was no (a yes gives the person's own name)."""
    with _confirm_lock:
        for k in [k for k, at in _confirms.items() if not (0.0 <= now - at <= CONFIRM_ONCE_S)]:
            _confirms.pop(k, None)
        if key in _confirms:
            return False
        _confirms[key] = now
        return True


def _confirm_asked(key: tuple[str, str, str], now: float) -> bool:
    """Whether the confirm question about ``key`` was asked within :data:`CONFIRM_ONCE_S` (a read-only peek: nothing
    noted). lResolve r1 L1: the same words given again after it mean the answer was no, so a draft never runs them as
    that person unasked even when their closest sound is a heard form the user confirmed."""
    with _confirm_lock:
        at = _confirms.get(key)
    return at is not None and 0.0 <= now - at <= CONFIRM_ONCE_S


def _identity(slot: Slot | None, value: Any) -> str:
    """A slot value as one identity, to compare a value the user said with a learned skill's bound one: an app by its
    key, anything else as a preference scope compares it (:func:`yuki.skills.connection.scope_value`)."""
    from yuki.skills.connection import scope_value

    if slot is not None and slot.type == "app" and not isinstance(value, Mapping):
        return app_name(value)
    return scope_value(value)


def _granted(slot: Slot, value: Any, granted: Mapping[str, str] | None) -> bool:
    """Whether a given value the words don't hold is the exact value the brain's consent grant allowed for this slot
    (``resolve``'s ``granted``): a text slot only, string equality with case and spaces aside, never a part of it."""
    if not granted or slot.type != "text" or not isinstance(value, str):
        return False
    want = _words(granted.get(slot.name))
    return bool(want) and _words(value).casefold() == want.casefold()


def _person_bound(skill: Skill, slot: Slot) -> bool:
    """A slot that names a person: of type ``person``, or a text slot the capability binds to a contact (data:
    ``bind = { person = "contact" }``; a learned skill keeps its capability's ``bind``)."""
    return slot.type == "person" or (slot.type == "text" and skill.bind.get(slot.name) == PERSON_BIND)


_apps_lock = threading.Lock()
_apps_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}


def _name_key(name: Any) -> str:
    from yuki.memory.contacts import tokens

    return " ".join(tokens(str(name or "")))


def _tidy(key: str) -> str:
    """An app key as people write it ("discord" -> "Discord"): :func:`yuki.core.appnames.prettify`, structure only."""
    try:
        from yuki.core.appnames import prettify

        return prettify(key) or key
    except Exception:  # noqa: BLE001
        return key


_chats_cache: list[tuple[float, dict[str, tuple[str, set[str]]]]] = []


def _chat_titles() -> dict[str, tuple[str, set[str]]]:
    """Each chat app's file name -> (its title, every identity its data gives it: the file name, the title, its
    programs, its images' keys and its services), from the connection files' ``[chat]`` tables (read again at most
    once a minute: a resolve asks for it once per app it compares)."""
    now = time.monotonic()
    with _apps_lock:
        if _chats_cache and now - _chats_cache[0][0] < 60.0:
            return _chats_cache[0][1]
    out: dict[str, tuple[str, set[str]]] = {}
    try:
        from yuki.skills.chat import app_data, chat_apps

        for stem in chat_apps():
            d = app_data(stem)
            title = str(d.get("title") or "")
            ids = {stem, title.casefold(), *(str(x).casefold() for x in d.get("services") or []),
                   *(app_name(x) for x in d.get("processes") or []), *(app_name(x) for x in d.get("images") or [])}
            out[stem] = (title, {i for i in ids if i})
    except Exception:  # noqa: BLE001 - no chat data: none named by it (not kept: read again next time)
        return out
    with _apps_lock:
        _chats_cache[:] = [(now, out)]
    return out


def known_apps(store: Any, *, until: float | None = None) -> dict[str, dict[str, Any]]:
    """The user's own programs as their timeline shows them (never Yuki's rows): ``{app key: {"names": [...],
    "sound": bool, "mic": bool}}``. ``names``: the display name it is shown under most, its key tidied ("chrome":
    "Google Chrome", "Chrome") and a chat app's title from its connection data ("whatsapp.root": "WhatsApp"); a name
    two programs share names neither (Yuki's own windows carry its name in several). ``sound``: in those rows it played
    media, in front or behind, or held a call's microphone (measured, never a list of audio apps); ``mic``: it held a
    call's microphone (a program a call may be in). Cached per database for
    :data:`APPS_TTL_S` (an eval's ``until`` reads as of then, uncached). Never raises (``{}``)."""
    if store is None:
        return {}
    cache_key = str(getattr(store, "path", id(store)))
    now = time.monotonic()
    if until is None:
        with _apps_lock:
            hit = _apps_cache.get(cache_key)
            if hit is not None and now - hit[0] < APPS_TTL_S:
                return hit[1]
    cut, args = ("", ()) if until is None else (" AND started_at <= ?", (float(until),))
    try:
        rows = store._query(
            "SELECT process, app, count(*), coalesce(sum(media_s), 0), coalesce(sum(mic_s), 0) FROM timeline "
            f"WHERE by_yuki = 0 AND process IS NOT NULL AND process != ''{cut} GROUP BY process, app", args)
        behind = store._query(
            "SELECT media_other FROM timeline WHERE by_yuki = 0 AND media_other IS NOT NULL AND media_other != ''"
            f"{cut} GROUP BY media_other", args)
    except Exception:  # noqa: BLE001
        return {}
    shown: dict[str, dict[str, int]] = {}
    sound: dict[str, bool] = {}
    held_mic: dict[str, bool] = {}
    for process, app, n, media, mic in rows:
        key = app_name(process)
        if not key:
            continue
        if app:
            shown.setdefault(key, {})[str(app)] = shown.get(key, {}).get(str(app), 0) + int(n or 0)
        else:
            shown.setdefault(key, {})
        sound[key] = sound.get(key, False) or float(media or 0) > 0 or float(mic or 0) > 0
        held_mic[key] = held_mic.get(key, False) or float(mic or 0) > 0
    # a background player is named by its display name: the one program shown under that name
    by_display: dict[str, set[str]] = {}
    for key, names in shown.items():
        for n in names:
            by_display.setdefault(_name_key(n), set()).add(key)
    for (raw,) in behind:
        try:
            got = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError:
            continue
        for n, seconds in (got.items() if isinstance(got, dict) else []):
            keys = by_display.get(_name_key(n), set())
            if len(keys) == 1 and (_num(seconds) or 0.0) > 0:
                sound[next(iter(keys))] = True
    chats = _chat_titles()
    names_of: dict[str, list[str]] = {}
    for key, counts in shown.items():
        most = max(counts.items(), key=lambda kv: kv[1])[0] if counts else ""
        names = [most, _tidy(key)]
        names += [title for title, ids in chats.values() if key in ids and title]
        names_of[key] = [n for n in dict.fromkeys(names) if _name_key(n)]
    owners: dict[str, set[str]] = {}
    for key, names in names_of.items():
        for n in names:
            owners.setdefault(_name_key(n), set()).add(key)
    out = {key: {"names": [n for n in names if len(owners.get(_name_key(n), ())) == 1], "sound": sound.get(key, False),
                 "mic": held_mic.get(key, False)}
           for key, names in names_of.items()}
    if until is None:
        with _apps_lock:
            _apps_cache[cache_key] = (now, out)
    return out


def app_identity(value: Any, *, store: Any = None, until: float | None = None) -> tuple[str, list[str]]:
    """An app value's identity key and every name it goes by, from data only: the value, its key (:func:`app_name`:
    "Discord.exe" -> "discord") and that key tidied; a web address's registrable name ("discord.com" -> "discord");
    a chat app's title and file name when the value is one of its identities (its data); the names the user's
    timeline shows that program under (:func:`known_apps`, also found by a display name: "Google Chrome" -> chrome).
    The key is the chat app's file name (what the chat routes compare), else the timeline's key, else
    :func:`app_name`. ``("", [])`` for no value."""
    text = _words(value)
    if not text:
        return "", []
    key = app_name(text)
    names = [text, key, _tidy(key)]
    label = ""
    try:
        from yuki.memory.moments import registrable_domain
        from yuki.skills.connection import domain_like

        if domain_like(text):
            label = registrable_domain(text).split(".")[0]
            names.append(label)
    except Exception:  # noqa: BLE001
        pass
    ident = key
    chat = False
    forms = {f for f in (text.casefold(), key, label) if f}
    for stem, (title, ids) in _chat_titles().items():
        if forms & ids:
            names += [title, stem]
            ident, chat = stem, True
            break
    apps = known_apps(store, until=until) if store is not None else {}
    tkey = next((k for k in (key, label) if k and k in apps), "")
    if not tkey:
        want = _name_key(text)
        tkey = next((k for k, a in apps.items() if any(_name_key(n) == want for n in a["names"])), "")
    if tkey:
        names += apps[tkey]["names"]
        if not chat:
            ident = tkey
    return ident, [n for n in dict.fromkeys(names) if _name_key(n)]


def _app_forms(value: Any, *, store: Any = None, until: float | None = None) -> set[str]:
    """The identities one app goes by, to tell whether two values are one app: its key (:func:`app_name`) and its
    identity key (:func:`app_identity`: a chat app's file name, so the timeline's "whatsapp.root" and the message
    route's "whatsapp" are one app)."""
    text = _words(value)
    if not text:
        return set()
    forms = {app_name(text)}
    try:
        forms.add(app_identity(text, store=store, until=until)[0])
    except Exception:  # noqa: BLE001 - its key alone
        pass
    return {f for f in forms if f}


def just_acted(store: Any, *, now: float, until: float | None = None, skills: Collection[str] = ()) -> set[str]:
    """The apps Yuki itself just acted in, within :data:`JUST_ACTED_S` before ``now`` (``until`` in an eval), each as
    its identities (:func:`_app_forms`): the timeline's stretches begun while Yuki acted (``by_yuki``: the windows its
    own steps brought up, whatever lane ran them: tonight's three YouTube-in-Chrome opens came from the thinker's own
    steps) and the apps the skill ledger says runs of ``skills`` drove (``skill_runs.app``; the caller passes the
    skill's capability and the learned skills of it, so a volume change on Arc's sound doesn't count against "open
    YouTube in Arc"). A turn of the exchange that names one of them may be the user talking about what Yuki just did
    ("You opened YouTube in Chrome again. Try another way."), so :func:`named_app` never fills a slot with one. Data
    only (the ledger and the timeline, never what the turn's words mean). Never raises (an empty set)."""
    if store is None:
        return set()
    t = float(until) if until is not None else float(now)
    raw: list[str] = []
    try:
        raw += [str(r[0]) for r in store._query(
            "SELECT DISTINCT process FROM timeline WHERE by_yuki = 1 AND process IS NOT NULL AND process != '' "
            "AND started_at >= ? AND started_at <= ?", (t - JUST_ACTED_S, t))]
    except Exception:  # noqa: BLE001
        pass
    keys = sorted({str(k) for k in skills or () if k})
    if keys:
        try:
            marks = ",".join("?" * len(keys))
            raw += [str(r[0]) for r in store._query(
                f"SELECT DISTINCT app FROM skill_runs WHERE skill IN ({marks}) AND app IS NOT NULL AND app != '' "
                "AND t0 >= ? AND t0 <= ?", (*keys, t - JUST_ACTED_S, t))]
        except Exception:  # noqa: BLE001
            pass
    out: set[str] = set()
    for a in dict.fromkeys(raw):
        out |= _app_forms(a, store=store, until=until)
    return out


def call_apps(store: Any, *, now: float, until: float | None = None) -> set[str]:
    """The apps a live call may be in, each as its identities (:func:`_app_forms`): the calls memory sees going on now
    (:func:`yuki.memory.callspans.open_calls`: the app holding the microphone as a call), else, when it sees none (the
    voice's own picture said a call is live), every program the timeline measured holding a call's microphone
    (:func:`known_apps` ``mic``). audio.toml: a call live and no app named is asked, never the call's own app by
    guess. Never raises (an empty set)."""
    if store is None:
        return set()
    raw: list[str] = []
    try:
        from yuki.memory.callspans import open_calls

        for c in open_calls(store, now=float(until) if until is not None else float(now)):
            raw += [str(c.get("process") or ""), str(c.get("app") or "")]
    except Exception:  # noqa: BLE001
        pass
    if not any(raw):
        raw = [k for k, a in known_apps(store, until=until).items() if a.get("mic")]
    out: set[str] = set()
    for a in dict.fromkeys(r for r in raw if r):
        out |= _app_forms(a, store=store, until=until)
    return out


def _app_candidates(skill: Skill, slot: Slot, *, store: Any, live: "Live", until: float | None,
                    now: float | None) -> dict[str, list[str]]:
    """The apps the user's words may name for an app slot (key -> names), from data: the slot's options; for a slot a
    live fact fills, the programs their timeline shows playing media or holding a call (:func:`known_apps`) and the
    ones playing now; for a means slot, the apps the skill's own routes drive (their literal ``app`` argument, named by
    their chat data's title), else, when its routes serve a browser, the browsers the timeline measures
    (:func:`yuki.skills.web_route.measured_browsers`)."""
    out: dict[str, list[str]] = {}
    for o in slot.options:
        out[o] = [o, o.replace("_", " ")]
    apps = known_apps(store, until=until) if store is not None else {}
    if slot.live:
        for key, a in apps.items():
            if a["sound"] and a["names"]:
                out.setdefault(key, list(a["names"]))
        for key in live.playing:
            k = app_name(key)
            if k:
                out.setdefault(k, list((apps.get(k) or {}).get("names") or [_tidy(k)]))
    if slot.role == "means":
        drives = [str(r.args.get("app")) for r in skill.routes
                  if isinstance(r.args.get("app"), str) and "{" not in str(r.args.get("app"))]
        if drives:
            chats = _chat_titles()
            for a in dict.fromkeys(drives):
                title = chats.get(a, ("", set()))[0]
                out.setdefault(app_name(a), [n for n in (title, a, _tidy(a)) if n])
        elif store is not None and any(r.means == "browser" for r in skill.routes):
            try:
                from yuki.skills.web_route import measured_browsers

                for b in measured_browsers(store, now=until if until is not None else now):
                    k = app_name(b.get("key"))
                    if k:
                        out.setdefault(k, [n for n in dict.fromkeys(
                            [*(apps.get(k) or {}).get("names", []), str(b.get("name") or ""), _tidy(k)]) if n])
            except Exception:  # noqa: BLE001 - no measured browser: none named
                pass
    return out


def _outside(turn: str, skip: Sequence[str]) -> list[str]:
    """The parts of ``turn`` outside every whole-word occurrence of the ``skip`` values (each part its casefolded
    words), so a name is matched only where it stands outside them and never across one."""
    from yuki.memory.contacts import tokens

    words = tokens(turn)
    cut = [False] * len(words)
    for value in skip:
        n = tokens(value)
        if not n:
            continue
        for i in range(len(words) - len(n) + 1):
            if words[i:i + len(n)] == n:
                cut[i:i + len(n)] = [True] * len(n)
    parts: list[str] = []
    cur: list[str] = []
    for w, gone in zip(words, cut):
        if gone:
            if cur:
                parts.append(" ".join(cur))
            cur = []
        else:
            cur.append(w)
    if cur:
        parts.append(" ".join(cur))
    return parts


def named_app(skill: Skill, slot: Slot, turns: Sequence[str], *, store: Any = None, live: "Live | None" = None,
              until: float | None = None, now: float | None = None, skip: Sequence[str] = (),
              avoid: Collection[str] = ()) -> str | None:
    """The app the user's own words name for an app slot no valid value was given for: the newest of ``turns``
    (newest last) that names any candidate (:func:`_app_candidates`), when it names exactly one; a turn that names two
    decides nothing (the usual fill runs, or asks). Whole words only: an app is never filled by sound or guess. What
    they said wins over a live "playing" guess, a preference, a learned skill's bound value or fill policy (failure
    fix-plan principle 2: "Can you play my Japanese playlist from Spotify? Make the volume a little bit down." turns
    Spotify down, not the app playing). ``skip``: the given values of the skill's free text slots (what to write, a
    link): an app named only inside them is part of the message, never its channel ("tell Hemanth I'm on Discord
    now" names no app to message him on), so a turn that names one only there decides nothing. ``avoid``: app
    identities never filled from the words (:func:`just_acted`: what Yuki itself just acted in, which a turn may name
    to complain about it; :func:`call_apps`: a live call's own app): such an app is never filled, and it still counts
    as a hit for "names exactly one" (x8WebArc review r2: the just-acted record is partial, it sees only windows that
    came to the front, so at 21:05 it held Arc but not the Chrome the default handler opened behind; "Dude, I ask you
    to open YouTube in arc not in Chrome." must never fill Chrome). A turn naming an avoided app and another decides
    nothing; the caller's own value ("You opened it in Chrome. I asked you to open it in Arc." with the talker's
    ``browser: Arc``) is kept by :func:`check_value` before this runs, and with none the browser driver asks."""
    turns = [str(t) for t in turns or [] if str(t).strip()]
    if not turns:
        return None
    cands = _app_candidates(skill, slot, store=store, live=live or Live(), until=until, now=now)
    if not cands:
        return None
    avoided = {k for k in cands if ({k} | _app_forms(k, store=store, until=until)) & set(avoid)} if avoid else set()
    skip = [str(v) for v in skip or () if _words(v)]
    for turn in reversed(turns):
        parts = _outside(turn, skip) if skip else [turn]
        hit = {key for key, names in cands.items() if any(_said(n, parts) for n in names)}
        if hit and hit <= avoided:
            continue      # a complaint about what Yuki just did ("You opened YouTube in Chrome again"): an older turn
        if hit:
            return next(iter(hit)) if len(hit) == 1 and not (hit & avoided) else None
    return None


# ---------------------------------------------------------------------------------------------------------------------
# Steps 2-4
# ---------------------------------------------------------------------------------------------------------------------


def _when_ok(route: Route, values: Mapping[str, Any]) -> bool:
    for cond in route.when:
        v = values.get(str(cond.get("slot")))
        if "in" in cond and (v if not isinstance(v, Mapping) else v.get("name")) not in list(cond.get("in") or []):
            return False
        if "present" in cond and bool(cond.get("present")) != (v is not None and v != ""):
            return False
        if "field" in cond:
            have = v.get(str(cond.get("field"))) if isinstance(v, Mapping) else None
            if bool(have) != bool(cond.get("has", True)):
                return False
    return True


def _window_state(skill: Skill, live: Live) -> str | None:
    """The app's window state for the ``states`` game rule: the caller's reader, else the skill's driver hook
    (:func:`yuki.skills.connection.window_state_reader`); ``None`` when neither reads it (the rung stays off)."""
    from yuki.skills.connection import WINDOW_STATES, window_state_reader

    reader = live.window_state if live.window_state is not None else window_state_reader(skill)
    if reader is None:
        return None
    try:
        got = reader(skill.app)
    except Exception:  # noqa: BLE001
        return None
    return got if isinstance(got, str) and got in WINDOW_STATES else None


def ladder(skill: Skill, values: Mapping[str, Any], *, store: Any = None, live: Live | None = None,
           now: float | None = None, until: float | None = None,
           disable_after: int = 2) -> tuple[list[Route], dict[str, str]]:
    """The rungs that may run now, in order, and why each other one may not (``when``, ``disabled``,
    ``untrusted``, ``game``, ``couldnt``). With a game in front only no-focus routes the data allows; never rung C.
    skill19 (live 19 Oct, RC2): a rung's own measured record orders it (:func:`yuki.skills.registry.by_record`): one
    whose last tries all ended "couldn't", each costing time, runs after every other rung, and after more of them is
    left out for a while (``couldnt``); never a fixed list."""
    from yuki.skills import registry

    live = live or Live()
    table = registry.trust(store, skill.key, now=now, until=until, disable_after=disable_after) \
        if store is not None else {}
    ok: list[Route] = []
    last: list[Route] = []
    skipped: dict[str, str] = {}
    for r in skill.routes:
        if not _when_ok(r, values):
            skipped[r.key] = "when"
            continue
        usable, why = registry.usable(r, table.get(r.key), now=now)
        if not usable:
            skipped[r.key] = why
            continue
        place = registry.by_record(r, table.get(r.key), now=now) if r.via != "agent" else ""
        if place == "rest":
            skipped[r.key] = "couldnt"
            continue
        if live.game:
            if r.needs_focus or r.game == "never":
                skipped[r.key] = "game"
                continue
            if r.game == "states":
                state = _window_state(skill, live)
                if state not in r.game_states:
                    skipped[r.key] = "game"
                    continue
        (last if place == "last" else ok).append(r)
    if last:
        # after every other rung that may run, before rung C (the agent rung ends the engine's ladder)
        at = next((i for i, r in enumerate(ok) if r.via == "agent"), len(ok))
        ok[at:at] = last
    return ok, skipped


def _given_source(given_from: Mapping[str, str] | str | None, slot_name: str) -> str:
    """The caller's own word for where the value it gave for ``slot_name`` came from (``resolve``'s ``given_from``: one
    word for every slot, or a slot -> word map), when it is one of :data:`CALLER_SOURCES`; else ``""``."""
    raw = given_from.get(slot_name) if isinstance(given_from, Mapping) else given_from
    word = _words(raw).casefold()
    return word if word in CALLER_SOURCES else ""


def _known_app(slot: Slot, text: str, *, store: Any, until: float | None) -> str:
    """The key of the app ``text`` names when the PC knows that app (one of the slot's options, a chat app's
    connection data, or a program the user's timeline shows: :func:`app_identity`), else ``""`` ("music" names no app:
    the slot's fills find it). Never raises."""
    try:
        opt = next((o for o in slot.options if o.casefold() == text.casefold()), None)
        if opt is not None:
            return opt
        key = app_identity(text, store=store, until=until)[0]
        apps = known_apps(store, until=until) if store is not None else {}
        return key if key and (key in apps or key in _chat_titles()) else ""
    except Exception:  # noqa: BLE001 - not known: the fills
        return ""


def _reading(slot: Slot, value: Any, *, store: Any, until: float | None) -> Any:
    """Phase 2.1: a value the caller gave that the words don't hold, as the slot keeps it when it is taken as the
    caller's reading: an app by its key (only an app the PC knows, :func:`_known_app`), a text as its words, an entity
    as ``{"name"}`` (an entity's link is never taken from the caller: it only ever comes from the user's own things,
    ``taste.find`` / ``find_offered``). ``None`` for a slot type code checks alone (an option, a number, a switch: a
    wrong one is no mishearing), a told slot (a link they told Yuki is their data: only theirs or one in their words),
    an app no program goes by, or an empty value. A person is never passed here."""
    text = _words(value.get("name") if isinstance(value, Mapping) else value)
    if not text or slot.told or slot.type not in ("app", "text", "entity"):
        return None
    if slot.type == "app":
        return _known_app(slot, text, store=store, until=until) or None
    if slot.type == "text":
        return text
    return {"name": text}


def resolve(skill_key: str, slots: Mapping[str, Any] | None, words: Sequence[str] | str, *, store: Any = None,
            live: Live | None = None, loaded: Loaded | None = None, now: float | None = None,
            until: float | None = None, again: bool = False, disable_after: int = 2,
            people: Callable[[str], dict[str, Any] | None] | None = None,
            default_step: float = 10.0, earlier: Sequence[str] | None = None,
            heard_person: Callable[[str, Sequence[str]], str | None] | None = None,
            granted: Mapping[str, str] | None = None, chosen: Mapping[str, Any] | None = None,
            chosen_from: str = FROM_CHOSEN, given_from: Mapping[str, str] | str | None = None,
            own_pick: bool = False, _strict: bool = False) -> Result:
    """Fill ``skill_key``'s slots and pick its rungs (see the module docstring). ``words``: the user's own words, each
    turn exactly as heard or typed (the current exchange: their turns since the last finished job, newest last);
    ``earlier``: older turns of this conversation (their own words too), a source for validating a value the caller
    gave, never for filling one; ``until``: as of that time (an eval or a replay: no ledger, preference or taste after
    it); ``store``: the store to read (``None``: files only). ``people`` / ``heard_person``: the people-index checks
    (default: this store's, :func:`yuki.memory.contacts`). ``granted``: slot name -> the exact value the brain's
    consent rule allowed for this very run (the engine's :func:`~yuki.skills.engine.granted_values`: a send it allowed
    and counted down, or the user's yes to it; failure fix-plan Item 11's L2): a given text value of that slot that the
    words don't hold but equals it (case and spaces aside) is kept as given (filled :data:`FROM_GRANT`), never
    dropped; nothing is ever filled from it, and it changes nothing for a value the words already hold. ``chosen``
    (kTaste, contract C2): slot name -> the value the engine chose for this very run (Yuki's pick when the user asked
    it to choose, or the upload of the channel they named), filled ``chosen_from`` (:data:`FROM_CHOSEN`, or ``words``
    when the user named the channel) before any other source of that slot; a value the caller gave for it is not
    checked then (the engine took it out).

    Phase 2.1 (the model's reading wins on undoable acts): on a run that neither sends nor commits (a page, a video, a
    playlist, an app, a browser, a tab, a way's item, a volume), a value the caller gave that the words don't hold is
    accepted as given, tagged where it came from (:attr:`Plan.sources`: ``given_from``, the caller's own word, else
    :data:`FROM_CARD` for one of the user's own known sites or things, else :data:`FROM_READING`) and named in the done
    line as a guess; an app only when the PC knows it. Data, not readings: a site's page kind, a told slot (their told
    link) and, with a call live, the call's own app. A person, and every slot of a lasting run (a send, a call join:
    rule 8), keep the strict checks exactly as before: the words, a confirmed heard form, a listed name, else dropped
    (and asked or handed over). ``own_pick``: the caller says a listed name it gave that the words don't hold is its
    own pick from the candidates (it passed their why): tagged :data:`FROM_MENU`, a guess; without it (or a
    ``given_from`` word) such a name is the user's pointing at it ("the second one"): ``words``, as before.

    lResolve: on a draft or an open (a capability that may send, on a run that doesn't: :func:`_sends`, read before
    the slot loop and checked again on its values), the caller's own wording of what to write is kept as a draft
    (:data:`FROM_WORDED`), a person whose closest sound is a heard form the user confirmed runs unasked
    (:func:`_heard_confirmed`), and the caller's reading of an undoable slot (the chat app, a way's item) is accepted.
    ``_strict`` (internal): none of those, every run as a send (the loop's values sent after all, so it resolves
    again)."""
    from yuki.skills import prefs, taste

    now = time.time() if now is None else float(now)
    loaded = loaded if loaded is not None else load()
    skill = loaded.get(str(skill_key or ""))
    if skill is None:
        return Refused(str(skill_key or ""), "unknown_skill")
    live = live or Live()
    if live.game and skill.routes and all(r.needs_focus or r.game == "never" for r in skill.routes):
        # sWeb r1 M4 / r2 L1 (sWire): with a game in front, a skill whose every route takes focus is held before any
        # question ("Who to?" mid-round could only end in "after the round?"); routes that may run with a game
        # (``game = "always"``, or ``"states"`` in a proven window state) keep the normal path
        return Held(skill.key, live.game)
    heard = [words] if isinstance(words, str) else [str(w) for w in words or [] if w]
    # validation reads every turn of the exchange and the older turns the caller passed (IF-words); filling from the
    # words (an app they named) reads the exchange only, newest turn first. lResolve r1 M1: in time order (the older
    # turns first, as the brain passes them, then the exchange, newest last), the order contacts' sound-alike answer
    # record reads ("spoken since" = a newer last turn), so a call in the same request as a question is no yes
    sources = [str(w) for w in earlier or [] if str(w).strip()] + heard
    # an empty value is no value given (``"text": ""`` = nothing to write), never a value dropped
    given = {k: v for k, v in dict(slots or {}).items() if v is not None and v != ""}
    # fSkills (live 18 Oct): a site page's kind and item as the site's data and the user's own names read them
    # (``page = "Deji"`` is the handle page of their channel Deji; "MBL" heard for their channel NDL)
    derived = _site_page_given(skill, given, sources, store, now, heard=heard)
    guessed_item = derived.pop(SITE_GUESS_KEY, None)
    told = told_values(store, skill.key)
    people = people or _person(store)
    heard_person = heard_person or _heard_person(store)
    sounds_like = _sounds_like(store)
    written = _written(store)
    heard_confirmed = _heard_confirmed(store)
    said_for = _said_for(store)
    # lResolve: whether this run sends (rule 8), from the capability's own data before any slot fills. ``drafting``: a
    # capability that may send, on a run that doesn't (a draft or an open): it may keep Yuki's wording of the message
    # and run a confirmed heard form unasked (a skill that never sends but reaches people otherwise, such as a call
    # join, keeps every question). ``relaxed``: one of those was used, so the loop's own values must not send (checked
    # after it)
    sending = True if _strict else _sends(skill, given, told)
    drafting = bool(getattr(skill, "sends", False)) and not sending
    # Phase 2.1: a lasting run (it sends, or reaches an end that commits otherwise: a call join, D24's commit table)
    # keeps the strict checks on every slot; any other run takes the caller's reading of an undoable slot
    from yuki.skills.engine import run_commit

    lasting = sending or (run_commit(skill) != "none" and not drafting)
    relaxed = False
    worded: str | None = None
    # what Yuki just did (:func:`just_acted`), read once and only for an app slot: the windows its steps brought up,
    # and the apps runs of this skill's capability (the capability and its learned skills) drove
    acted: set[str] | None = None
    cap = skill.capability or skill.key
    family = {cap, skill.key, *(s.key for s in getattr(loaded, "skills", ()) or () if s.capability == cap)}
    # the given values of free text slots (bound to nothing: what to write, a link): an app named inside them is the
    # message's content, never its channel (named_app's ``skip``)
    free_text = [str(v) for k, v in given.items()
                 if isinstance(v, str) and (free := skill.slot(k)) is not None and free.type == "text"
                 and not skill.bind.get(k)]

    def app_names(value: str) -> tuple[str, list[str]]:
        return app_identity(value, store=store, until=until)

    values: dict[str, Any] = {}
    filled: dict[str, str] = {}
    origin: dict[str, str] = {}       # Plan.sources: where each value the caller gave came from
    named: list[str] = []
    dropped: list[str] = []
    ask_keep: dict[str, Any] | None = None
    last = _again(store, skill.key, now) if again else {}

    def keep_check(slot: Slot, value: Any) -> dict[str, Any] | None:
        """A means value the user said that is not their usual: "Keep this as your usual?" (``ask_keep``)."""
        option = pref_option(slot, value)
        if slot.pref and store is not None and option is not None:
            current = prefs.choose(store, slot.pref_key(skill.key, values), now=now, until=until)
            if current is None or current.option != option or current.source != "usual":
                return {"slot": slot.name, "pref": slot.pref_key(skill.key, values), "option": option}
        return None

    def own(slot: Slot, value: Any, menu_names: Sequence[str], offered: Sequence[str]) -> tuple[Any, str]:
        """An entity value as one of the user's own things when it is one: a taste row by its name, or the one listed
        name it sounds like (lSkills, live 17 Oct: "Lumin" heard for lumen, with [playlist: lumen | ネオン] on the
        menu: contacts' sound key, onset and spelling distance, data, never a word list), else an upload just offered
        aloud (tTaste merge note 2: ``{"name", "key", "ref"}``, rung A's ``when ref has`` passes). ``(value, how it is
        theirs)``: :data:`FROM_WORDS` by its name, :data:`FROM_HEARD` by sound, ``""`` not one of theirs."""
        if not isinstance(value, dict) or store is None:
            return value, ""
        if slot.taste:
            how = FROM_WORDS
            hit = taste.find(store, slot.taste, value.get("name", ""))
            if hit is None and menu_names:
                alike = taste.heard_as(menu_names, value.get("name", ""), loose=True)
                if alike:
                    hit, how = taste.find(store, slot.taste, alike), FROM_HEARD
            if hit is not None:
                return {"name": hit.name, "key": hit.entity_key, "ref": hit.ref}, how
        elif offered:
            pick = taste.find_offered(store, value.get("name", ""), now=now)
            if pick is not None:
                return pick, FROM_WORDS
        return value, ""

    # The slot loop keeps FILL_ORDER: each step below ends in ``continue`` once it filled the slot, so a later source
    # (a learned skill's bound default, its fill policy's preference, a live guess) can never replace an earlier one,
    # and what the user said ("words") always comes first.
    for slot in skill.slots:
        if chosen and slot.name in chosen and chosen[slot.name] not in (None, ""):
            # kTaste (C2): the engine's pick for this run (the user asked Yuki to choose, or named only the channel)
            values[slot.name], filled[slot.name] = chosen[slot.name], str(chosen_from or FROM_CHOSEN)
            origin[slot.name] = filled[slot.name]
            continue
        if slot.name in derived:
            values[slot.name], filled[slot.name] = derived[slot.name], "words"
            origin[slot.name] = FROM_WORDS
            if slot.name == guessed_item:
                filled[slot.name] = origin[slot.name] = FROM_CARD    # skill19 (R7): one of theirs, Yuki's guess
            continue
        way_slot = cap == WAY_CAPABILITY and slot.name == WAY_SLOT and slot.type == "entity" and store is not None
        if way_slot and slot.name in given:
            # kWays merge note 1: a live way of theirs named by its key or name (the menu line's ``way = <key>``)
            way = _way_given(store, given[slot.name])
            if way is not None:
                values[slot.name], filled[slot.name] = way, "words"
                origin[slot.name] = FROM_WORDS
                continue
        menu_names = taste.candidates(store, slot.taste, now=now) if (slot.taste and store is not None) \
            else _listed(skill, slot, store, now=now)
        # tTaste merge note 2 (phase T): a slot every route of which runs only with a value an offered option carries
        # (``taste.takes_offers``: today ``web.open_video``'s ``video``) lists the names just offered aloud, and a value
        # that is one of them takes the offer's link ("the Deji one" plays it). Any other span is still accepted as for
        # any entity slot; with no link, rung A's ``when`` skips it and the run hands off with the capability pinned
        offered = [t.name for t in taste.offered(store, now=now)] \
            if (store is not None and taste.takes_offers(skill, slot)) else []
        # FILL_ORDER "words" (1): a value the caller gave that stands in the user's words (or is heard there, or is a
        # name listed for the slot)
        if slot.name in given:
            ok, value, how = _check(slot, given[slot.name], sources, menu_names=menu_names or offered,
                                    told=told.get(slot.name), people=people,
                                    heard_person=heard_person if _person_bound(skill, slot) else None,
                                    app_names=app_names, site=skill.bind.get(slot.name) == SITE_BIND,
                                    loose=bool(slot.taste))
            if ok:
                value, mine = own(slot, value, menu_names, offered)
                if mine == FROM_HEARD and how == FROM_WORDS:
                    how = FROM_HEARD      # their word, heard as one of their own things ("Lumin" -> lumen)
                if how == FROM_MENU:
                    # a listed name the words don't hold is Yuki's guess only when the caller says so (Phase 2.2: its
                    # own pick from the candidates, passed with their why, or a ``given_from`` word); otherwise the
                    # user pointed at it ("the second one", "my chill one"): their own ask, as before
                    how = _given_source(given_from, slot.name) or (FROM_MENU if own_pick else FROM_WORDS)
                literal = isinstance(value, str) and _words(value).casefold() == _words(given[slot.name]).casefold()
                if literal and slot.type == "text" and _person_bound(skill, slot):
                    # a name said in another script than their people's names ("हेमंत") is the one person it reads as,
                    # letter for letter (a conversion): run as that person, unasked
                    script = written(value)
                    if script:
                        value = script
                    else:
                        # a name the user said that none of their people goes by as written but sounds like exactly
                        # one of them ("Hemant" for Hemanth; "Karan" with only a Kiran): asked once, never run as
                        # that person. Given again as said within the exchange, the answer was no: it runs as said,
                        # and the chat driver says it doesn't know who that is (never the same question twice; an
                        # eval's replay, ``until``, asks every time: its phrasings are separate requests)
                        alike = sounds_like(value)
                        confirm_key = (skill.capability or skill.key, slot.name, _words(value).casefold())
                        if alike is not None and drafting and \
                                (heard_confirmed(value, alike[1]) or _answers_who(value, heard)) and \
                                (until is not None or not _confirm_asked(confirm_key, now)):
                            # lResolve (lMsg's what's left 3) and the one draft/open rule (fMsg C2 + fTalk, live 18
                            # Oct k_clarify_who_draft): the name sounds like exactly one person and a second signal
                            # agrees - its closest sound is a heard form of that person the user confirmed ("hey
                            # mean" near their "hey meant"), or it is their direct answer to Yuki's own "Who to?" ("to
                            # hardish"). On a draft or an open: that person, unasked (the done line names them, so a
                            # wrong one shows); a send still asks (rule 8: never sent by sound), and once asked within
                            # CONFIRM_ONCE_S the same words again are a no (r1 L1)
                            value, relaxed, how = alike[0], True, FROM_HEARD
                        elif alike is not None and (until is not None or _first_confirm(confirm_key, now)):
                            return Ask(skill.key, slot.name, CONFIRM_PERSON.format(name=alike[0]), [alike[0]],
                                       dropped=list(dropped))
                # a value their words hold (as written or by sound) or a listed name they pointed at is "words" in the
                # ledger, as before; the caller's own pick (above) keeps its tag (a guess, never their own word)
                values[slot.name], origin[slot.name] = value, how
                filled[slot.name] = "words" if how in (FROM_WORDS, FROM_HEARD) else how
                if how in (FROM_WORDS, FROM_HEARD):
                    ask_keep = keep_check(slot, value) or ask_keep
                continue
            if _granted(slot, given[slot.name], granted):
                # Item 11's L2: the very text (or person) the brain's consent rule allowed this step and counted down,
                # as given: Yuki's wording of what the user asked to say ("When will you come?" for "ask him when he
                # will come"), never dropped to an Ask and handed back as a draft
                values[slot.name], filled[slot.name] = _words(given[slot.name]), FROM_GRANT
                origin[slot.name] = FROM_GRANT
                continue
            if drafting and _worded_slot(skill, slot) and isinstance(given[slot.name], str) and \
                    _words(given[slot.name]):
                # lResolve, the 10 Oct decision: "a message Yuki words itself stays a draft". What to write, in the
                # caller's own wording ("when you joining vc" for "asking when he'll join the VC"), on a run that
                # doesn't send: kept as given, never dropped and handed to the thinker (30-98 s live). The step runner
                # types exactly it and nothing after it (uia_route.note_draft, below); a send stays the user's words
                # or the brain's grant
                values[slot.name], filled[slot.name] = _words(given[slot.name]), FROM_WORDED
                origin[slot.name] = FROM_WORDED
                worded, relaxed = slot.name, True
                continue
            if _person_bound(skill, slot) and isinstance(given[slot.name], str):
                # R4 (lMsg, what's left after round 1, item 3): the caller gave a person the words don't hold (the
                # talker's "Hemanth" for the heard "Hemish"), and a run of the user's own words sounds like that very
                # person and nobody else: asked, never handed to the thinker and never run as them unasked. The given
                # name is the question's answer, so the slot is not in ``dropped`` and the engine keeps the question
                # (x5's ``_dropped``). Noted with the very turns the answer is checked against (``sources``, r1 M1),
                # so it counts only once they've spoken since; a repeat of the same words within the exchange is a no
                # and falls through to the hand-off
                heard_as = said_for(given[slot.name], heard)
                if heard_as is not None and drafting:
                    # the one draft/open rule (fMsg merge note C2 + fTalk): the person the caller inferred
                    # ("bobsled", "Hemanth") and the name in the user's words that sounds like that same one person
                    # and nobody else ("Kwix", "hardish") are two signals that agree: on a draft or an open, that
                    # person, unasked, by their own name (the done line names them, so a wrong one shows). A send
                    # still asks below (rule 8: whom a message is sent to is never matched by sound); the loop's
                    # values are checked again after it (``relaxed``)
                    values[slot.name], filled[slot.name] = heard_as[0], "words"
                    origin[slot.name] = FROM_HEARD
                    relaxed = True
                    continue
                if heard_as is not None and (until is not None or _note_asked(store, heard_as[1], heard_as[2],
                                                                              sources, now)):
                    return Ask(skill.key, slot.name, CONFIRM_PERSON.format(name=heard_as[0]), [heard_as[0]],
                               dropped=list(dropped))
            if way_slot:
                # kWays review L1: a way that is none of theirs (a misroute: "in Notion type X") is kept as given, so
                # the route asks "Which of your ways?"; dropped, the slot's default would run their only way instead
                raw = given[slot.name]
                text = (raw.get("key") or raw.get("name")) if isinstance(raw, Mapping) else raw
                if _words(text):
                    values[slot.name], filled[slot.name] = {"name": _words(text)}, FROM_GIVEN
                    origin[slot.name] = FROM_GIVEN
                    continue
            # Phase 2.1: the caller's reading of an undoable slot, accepted as given (never "not in the words as
            # heard"), even when the words name another app: the caller read the whole conversation, and the done line
            # names its guess, so one word corrects it. A person, or any slot of a lasting run, is dropped below
            # exactly as before. Data is no reading: a slot bound to a site's page kind (a page kind the site file
            # lacks is dropped and the site's front page opens, as before; their own names for its pages were read
            # above, ``derived``), a told slot (their told link fills it) and an app no program goes by (the fills)
            reading = None if (lasting or _person_bound(skill, slot) or skill.bind.get(slot.name) == PAGE_BIND) \
                else _reading(slot, given[slot.name], store=store, until=until)
            if reading is not None and slot.type == "app" and slot.ask_in_call and live.in_call and \
                    (_app_forms(reading, store=store, until=until) & call_apps(store, now=now, until=until)):
                # with a call live, a slot that asks in a call never takes the call's own app unasked (audio.toml:
                # "turn it down" on a call asks whose volume)
                reading = None
            if reading is not None:
                src = _given_source(given_from, slot.name)
                theirs = False
                if skill.bind.get(slot.name) == SITE_BIND:
                    # fBrain merge note (contract C5): one of the user's own known sites ("my anime" -> JustAnime,
                    # from the card), kept as that known value (the form its runs used)
                    site = _known_site(store, cap, slot.name, reading, now=now)
                    if site is not None:
                        reading, theirs = site, True
                elif slot.type == "entity":
                    reading, mine = own(slot, reading, menu_names, offered)
                    theirs = bool(mine)
                    if not theirs and slot.taste and menu_names and store is not None:
                        # skill19 (live 19 Oct, all-browser R7): the caller's reading is none of theirs and isn't in
                        # the user's words; before it is taken, their own words are matched against their own names
                        # by sound or spelling (taste.heard_in): a hit is one of theirs, Yuki's guess (named)
                        hit = taste.heard_in(menu_names, heard)
                        if hit:
                            mapped, mine = own(slot, {"name": hit}, menu_names, offered)
                            if mine:
                                reading, theirs = mapped, True
                src = src or (FROM_CARD if theirs else FROM_READING)
                values[slot.name], filled[slot.name] = reading, src
                origin[slot.name] = src
                # on a draft or an open, a run whose filled values send after all resolves again without it
                relaxed = relaxed or drafting
                continue
            dropped.append(slot.name)
        # FILL_ORDER "words" (2): an app their own words name, before any live guess, preference, fill policy or
        # bound default (the volume of "Spotify", not of the app playing). Never an app Yuki itself just acted in (a
        # turn may name it to complain: "You opened YouTube in Chrome again"), and with a call live, for a slot that
        # asks in a call, only the newest turn and never the call's own app (audio.toml: "I'm on Discord with
        # Hemanth, turn the music down" asks whose volume, as before)
        if slot.type == "app":
            in_call_ask = bool(slot.ask_in_call and live.in_call)
            if acted is None:
                acted = just_acted(store, now=now, until=until, skills=family)
            avoid = set(acted) | (call_apps(store, now=now, until=until) if in_call_ask else set())
            app = named_app(skill, slot, heard[-1:] if in_call_ask else heard, store=store, live=live, until=until,
                            now=now, skip=free_text, avoid=avoid)
            if app is not None and slot.name in dropped and \
                    not (_app_forms(app, store=store, until=until) &
                         _app_forms(given[slot.name], store=store, until=until)):
                # the caller gave one app (its reading of the whole conversation) that the words don't hold, and the
                # words name another: two readings that disagree. Never silently swapped for the words' app: asked,
                # with the slot dropped, so the engine hands the caller's value to the thinker (fix-plan
                # IF-handoff), which has the conversation; a value nobody gave fills from the words as above
                theirs = app_identity(given[slot.name], store=store, until=until)[0] or app_name(given[slot.name])
                return Ask(skill.key, slot.name, f"{_title(theirs)} or {_title(app)}?", [theirs, app],
                           dropped=list(dropped))
            if app is not None:
                values[slot.name], filled[slot.name] = app, "words"
                ask_keep = keep_check(slot, app) or ask_keep
                continue
        # FILL_ORDER "again"
        if slot.name in last:
            values[slot.name], filled[slot.name] = last[slot.name], "again"
            continue
        # FILL_ORDER "told"
        if slot.told and slot.name in told:
            values[slot.name], filled[slot.name] = told[slot.name], "told"
            continue
        if slot.ask_in_call and live.in_call and slot.name not in given:
            # a call live and no app the newest turn names (save the call's own): asked, never a guess
            return Ask(skill.key, slot.name, slot.ask or f"Which {slot.name}?", [], dropped=list(dropped))
        # FILL_ORDER "live"
        if slot.live == "playing":
            playing = list(dict.fromkeys(live.playing))
            if len(playing) == 1:
                values[slot.name], filled[slot.name] = playing[0], "live"
                continue
            if len(playing) > 1:
                return Ask(skill.key, slot.name, " or ".join(_title(p) for p in playing[:3]) + "?", playing[:3],
                           dropped=list(dropped))
        if slot.live == "front" and live.front_app and (not slot.options or live.front_app in slot.options):
            values[slot.name], filled[slot.name] = live.front_app, "live"
            continue
        # FILL_ORDER "pref" (the user's usual; a learned skill's fill policy acts later, in its route, and only on a
        # means slot nothing here filled)
        if slot.pref and store is not None:
            choice = prefs.choose(store, slot.pref_key(skill.key, values), now=now, until=until)
            if choice is not None:
                ok, value = from_pref(store, slot, choice.option)
                if ok:
                    values[slot.name], filled[slot.name] = value, "pref"
                    if choice.named:
                        named.append(slot.name)
                    continue
        # FILL_ORDER "taste"
        if slot.taste and store is not None:
            best = taste.top(store, slot.taste, now=now, n=1, until=until)
            if best:
                values[slot.name] = {"name": best[0].name, "key": best[0].entity_key, "ref": best[0].ref}
                filled[slot.name] = "taste"
                named.append(slot.name)
                continue
        # A text the caller gave for what to write ("when will you come?", worded by the thinker for "ask him when he
        # will come") that the user's words don't hold is never swapped for the slot's default ("" = just open the
        # chat): that would run another request and call it done. On a send (a draft kept it above, FROM_WORDED) it is
        # asked instead, with the slot dropped, so the engine can hand a value its caller gave to the thinker
        # (fix-plan IF-handoff), which does it by steps where the send rule judges the wording
        if slot.name in dropped and slot.type == "text" and slot.role == "fill" and slot.default is not None:
            return Ask(skill.key, slot.name, slot.ask or f"Which {slot.name}?", [], dropped=list(dropped))
        # FILL_ORDER "default" (a learned skill's bound value is its slot's default)
        if slot.default is not None:
            values[slot.name], filled[slot.name] = slot.default, "default"
            if slot.type in ("enum", "app", "entity"):
                named.append(slot.name)
            continue
        if slot.required:
            return Ask(skill.key, slot.name, slot.ask or f"Which {slot.name}?", list(slot.options),
                       dropped=list(dropped))
    if relaxed:
        from yuki.skills.connection import sends_with

        if sends_with(skill, values):
            # lResolve: a draft's relaxations were taken for a run whose filled values send after all (a fill
            # :func:`_sends` didn't foresee): resolved again with none of them, so Yuki's wording is dropped, a sound
            # match asked and a reading checked against the words, as before (rule 8)
            return resolve(skill_key, slots, words, store=store, live=live, loaded=loaded, now=now, until=until,
                           again=again, disable_after=disable_after, people=people, default_step=default_step,
                           earlier=earlier, heard_person=heard_person, granted=granted, chosen=chosen,
                           chosen_from=chosen_from, given_from=given_from, own_pick=own_pick, _strict=True)
    # What the user said wins over a learned skill's bound value too: its routes carry that value in their arguments,
    # so a value given for a bound slot that is another one runs its capability instead (a learned skill never hides
    # its capability; the engine's request_key does the same for a given key slot before this). Phase 2.1: any value
    # the run was given counts (their words, a reading taken as given, a grant, Yuki's wording, the engine's pick: every
    # entry of ``origin``), never one a fill chose (again, told, live, a preference, the taste guess, a bound default)
    if skill.learned and skill.capability and skill.capability != skill.key and \
            any(name in origin and _identity(skill.slot(name), values.get(name)) !=
                _identity(skill.slot(name), bound) for name, bound in (skill.bound or {}).items()):
        return resolve(skill.capability, slots, words, store=store, live=live, loaded=loaded, now=now, until=until,
                       again=again, disable_after=disable_after, people=people, default_step=default_step,
                       earlier=earlier, heard_person=heard_person, granted=granted, chosen=chosen,
                       chosen_from=chosen_from, given_from=given_from, own_pick=own_pick, _strict=_strict)
    # a direction with no amount: the user's own step for it (a preference), else the default step
    for slot in skill.slots:
        v = values.get(slot.name)
        if slot.type == "points" and isinstance(v, str) and v in DIRECTIONS:
            step = None
            if store is not None:
                choice = prefs.choose(store, f"{skill.key}:{slot.name}@step", now=now, until=until)
                step = _num(choice.option) if choice is not None else None
            values[slot.name] = DIRECTIONS[v] * abs(step or float(default_step))
    routes, skipped = ladder(skill, values, store=store, live=live, now=now, until=until, disable_after=disable_after)
    if live.game and not routes:
        return Held(skill.key, live.game)
    if not routes:
        if skill.handoff:
            return Handoff(skill, values, filled, why="no_route", skipped=skipped, dropped=list(dropped))
        return Refused(skill.key, "no_route")
    if worded is not None:
        # lResolve: the draft Yuki worded may be typed by this skill's own steps for a short while, and only as a draft
        # (uia_route.check_texts: a run that types it and does nothing else)
        _note_draft(cap, values.get(worded))
    return Plan(skill=skill, values=values, filled=filled, named=named, routes=routes, skipped=skipped,
                dropped=dropped, game=live.game, ask_keep=ask_keep, handoff=skill.handoff and not live.game,
                sources=origin)


def _title(app: str) -> str:
    try:
        from yuki.skills.uia_route import app_words

        return app_words(app)
    except Exception:  # noqa: BLE001
        return str(app)


#: Phase 2.1: how the done line names Yuki's guess at what they meant (:data:`GUESS_SOURCES`), so one word corrects it:
#: the value when the line doesn't say it ("Opening excalidraw.com in Arc. I took that as Excalidraw."), else a short
#: hedge ("Opening excalidraw.com in Arc, if that's what you meant."). Phase 2.2: a pick the caller made from the
#: candidates with their code-made why says the why for that pick (the form of ``taste.choice_line``); any other guess
#: is still named.
GUESS_LINE = "{line} I took that as {what}."
GUESS_SAID_LINE = "{line}, if that's what you meant."
WHY_LINE = "{line} - {why}."
#: A pick's why as said: at most this long, one line.
WHY_CHARS = 90


def shown_value(skill: Skill, name: str, value: Any) -> str:
    """A slot value as the user hears it: an app by its title ("Arc", never "arc"), an entity or a person by its name,
    anything else as its words."""
    if isinstance(value, Mapping):
        value = value.get("name")
    text = _words(value)
    if text and getattr(skill.slot(name), "type", None) == "app":
        return _title(text)
    return text


def done_line(skill: Skill, values: Mapping[str, Any], named: Sequence[str] = (), *, route: Route | None = None,
              say: str | None = None, guessed: Sequence[str] = (), why: str = "", picked: Sequence[str] = ()) -> str:
    """The line said once the done-check saw it: the route's or the skill's template filled, plus each guessed slot's
    ``say`` (one word corrects it). ``say``: the route's own words (a driver's), used instead of the template. An app
    slot's value is said as the app's title ("in Arc"), never its key ("in arc"; e2e simulation F12). ``guessed``: the
    slots whose given value is Yuki's guess (:attr:`Plan.guessed`), named by :data:`GUESS_LINE` /
    :data:`GUESS_SAID_LINE`; ``why``: a pick's code-made why (the candidates' own, passed back by the caller), said as
    :data:`WHY_LINE` for the picked slots (``picked``: those guesses need no other naming); every other guess the line
    doesn't say is still named ("Opening excalidraw.com - you open it most mornings. I took that as Arc.")."""
    shown = {k: (_title(v) if isinstance(v, str) and v and getattr(skill.slot(k), "type", None) == "app" else v)
             for k, v in values.items()}
    base = say or fill(route.say if route is not None and route.say else skill.done_line, shown) or ""
    extra = []
    for name in named:
        slot = skill.slot(name)
        if slot is not None and slot.say:
            words = fill(slot.say, shown)
            if words and words.casefold() not in base.casefold():
                extra.append(words)
    if extra:
        base = base.rstrip(".") + " " + ", ".join(extra) + "."
    base = " ".join(base.split())
    why = _words(why)[:WHY_CHARS].rstrip(" .")
    if base and why:
        base = WHY_LINE.format(line=base.rstrip("."), why=why)
    covered = set(picked) if why else set()
    whats = [w for w in dict.fromkeys(shown_value(skill, k, values.get(k)) for k in guessed if k not in covered) if w]
    if base and whats:
        unsaid = [w for w in whats if w.casefold() not in base.casefold()]
        if unsaid:
            return GUESS_LINE.format(line=base if base.endswith((".", "!", "?")) else base + ".",
                                     what=" and ".join(unsaid[:2]))
        if not why:          # a why already says it was a pick
            return GUESS_SAID_LINE.format(line=base.rstrip("."))
    return base


def canonical(values: Mapping[str, Any]) -> dict[str, Any]:
    """Slot values as the requests suite compares them: an entity or a person by its name, numbers as numbers."""
    out: dict[str, Any] = {}
    for k, v in values.items():
        out[k] = v.get("name") if isinstance(v, Mapping) else v
    return out


# ---------------------------------------------------------------------------------------------------------------------
# The requests suite's resolver (yuki.eval.habits_replay; never the live database)
# ---------------------------------------------------------------------------------------------------------------------


class EvalResolver:
    """One talker-model call per phrasing with the real menu (cached prompt), then :func:`resolve` as of the phrasing's
    time on a **copy** of the memory database. ``resolve(words, at) -> {"skill", "slots", "cost_usd"}`` (the
    contract of ``yuki.eval.habits_replay.run_requests``); ``estimate_usd(words)`` its own cost estimate. No live read
    of the desktop: the live facts are empty (no game, nothing playing)."""

    def __init__(self, store: Any, *, client: Any = None, model: str | None = None,
                 prompt: Callable[[str], str] | None = None, loaded: Loaded | None = None,
                 settings: Any = None) -> None:
        self.store = store
        self._client = client
        self.model = model
        self._prompt = prompt
        self.loaded = loaded if loaded is not None else load()
        self.settings = settings

    def _settings(self) -> Any:
        if self.settings is None:
            from yuki.config import Settings

            self.settings = Settings()
        return self.settings

    def _model(self) -> str:
        if self.model:
            return self.model
        from yuki.config import DEFAULT_VOICE_MOUTH_MODEL

        return str(getattr(self._settings(), "voice_mouth_model", "") or DEFAULT_VOICE_MOUTH_MODEL)

    def client(self) -> Any:
        if self._client is None:
            from yuki.agent.failover import make_client

            self._client = make_client(self._settings(), role="voice", waited_on=True)
        return self._client

    def system(self, at: float) -> str:
        from yuki.skills.menu import TALKER, menu_text

        # review r2 L7: taste most seen first (not best at the fixture's hour), so the cached prefix stays the same
        # from one phrasing to the next (fewer cache writes against the suite's cost cap)
        menu = menu_text(self.store, audience=TALKER, loaded=self.loaded, now=at, rank="n_obs")
        if self._prompt is not None:
            return self._prompt(menu)
        try:
            # sVoice merge note (sWire): the talker's real hands block for this menu (its Skills paragraph, the volume
            # and playlist wording, then the menu), so the suite sees what the talker sees
            from yuki.voice.mouth import hands_block

            return str(hands_block(menu))
        except Exception:  # noqa: BLE001 - the menu alone
            head = "You are Yuki's voice. Pick the user's usual way from the Skills list with run_skill, or tell_yuki."
        return head + "\n\n" + menu

    def tools(self) -> list[dict[str, Any]]:
        from yuki.skills.menu import talker_tool

        t = talker_tool(self.loaded.keys)
        return [{"name": t["name"], "description": t["description"],
                 "input_schema": {"type": "object", "properties": t["properties"], "required": t["required"]}},
                {"name": "tell_yuki", "description": "Hand the request to Yuki's thinker (no skill fits).",
                 "input_schema": {"type": "object", "properties": {"context": {"type": "string"}}}}]

    def estimate_usd(self, words: str) -> tuple[float, float]:
        from yuki.eval.habits_replay import default_estimate

        return default_estimate(words)

    def resolve(self, words: str, at: float) -> dict[str, Any]:
        at = float(at)
        response = self.client().messages.create(
            model=self._model(), max_tokens=400,
            system=[{"type": "text", "text": self.system(at), "cache_control": {"type": "ephemeral"}}],
            tools=self.tools(), tool_choice={"type": "any"},
            messages=[{"role": "user", "content": str(words)}])
        cost = self._cost(response)
        call = next((b for b in getattr(response, "content", []) or []
                     if getattr(b, "type", None) == "tool_use"), None)
        if call is None or getattr(call, "name", "") != "run_skill":
            return {"skill": None, "slots": {}, "cost_usd": cost}
        args = dict(getattr(call, "input", {}) or {})
        key = str(args.get("skill") or "")
        got = resolve(key, args.get("slots") if isinstance(args.get("slots"), dict) else {}, [str(words)],
                      store=self.store, live=Live(), loaded=self.loaded, now=at, until=at,
                      again=bool(args.get("again")))
        values = getattr(got, "values", None) or {}
        return {"skill": key or None, "slots": canonical(values), "cost_usd": cost,
                "kind": getattr(got, "kind", "refused")}

    def _cost(self, response: Any) -> float | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        data = {k: int(getattr(usage, k, 0) or 0) for k in ("input_tokens", "output_tokens",
                                                            "cache_read_input_tokens", "cache_creation_input_tokens")}
        try:
            return self._settings().estimate_cost(self._model(), data)
        except Exception:  # noqa: BLE001 - unreported: the suite charges its worst case
            return None


def eval_resolver(db_path: Any, *, client: Any = None, model: str | None = None,
                  prompt: Callable[[str], str] | None = None, connections: Sequence[Any] | None = None) -> EvalResolver:
    """The requests suite's resolver on a **copy** of the memory database (never the live one: refused)."""
    from pathlib import Path

    from yuki.eval import dbcopy
    from yuki.memory.store import Store

    if db_path is None:
        raise ValueError("the resolver is built on a copy of the memory database")
    path = Path(db_path)
    if dbcopy.is_live(path):
        raise RuntimeError("the resolver never opens the live memory database")
    loaded = load(connections) if connections is not None else load()
    return EvalResolver(Store.open(path), client=client, model=model, prompt=prompt, loaded=loaded)


__all__ = ["AGAIN_S", "DIRECTIONS", "HELD_LINE", "FILL_ORDER", "FROM_GRANT", "FROM_WORDED", "FROM_GIVEN",
           "FROM_CHOSEN", "FROM_WORDS", "FROM_HEARD", "FROM_MENU", "FROM_CARD", "FROM_SCREEN", "FROM_READING",
           "GUESS_SOURCES", "CALLER_SOURCES", "GUESS_LINE", "GUESS_SAID_LINE", "WHY_LINE", "shown_value",
           "CODE_TYPES", "CONFIRM_PERSON", "JUST_ACTED_S",
           "CONFIRM_ONCE_S", "Live", "Plan", "Ask", "Held", "Handoff", "Refused", "app_name", "app_identity",
           "known_apps", "named_app", "just_acted", "call_apps", "check_value", "bool_option", "pref_option",
           "from_pref",
           "told_values", "ladder", "resolve", "done_line", "canonical", "EvalResolver", "eval_resolver"]
