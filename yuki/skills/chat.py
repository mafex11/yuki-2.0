"""Chats: the drivers of ``message`` and ``discord.voice`` (PLAN.md Phase S-2; decisions D0, D2, D17, D19, Q3/rule 8;
B.2; map-skills S2.5).

The connection files name these calls (``connections/message.toml``, ``connections/discord.toml``); each chat app's
own facts (its programs, the services memory stores its chats under, its link and URI shapes, its shortcuts, its labels)
are data in that app's connection file, table ``[chat]`` (``discord.toml``, ``whatsapp.toml``, ``slack.toml``), read by
:func:`app_data`. Nothing here names an app.

**Whom, and where.** The ``person`` slot is the user's own words. Memory's people index finds them
(:func:`yuki.memory.contacts.resolve`: names, aliases, misheard forms the user corrected, a first name for a full
one); two people who fit are a question, never a guess. A group chat or channel is found by its name (the chat name's
words, identity only), and only then is a message typed there; words that are a group's whole name and also name a
person are a question, unless the person's name only stands inside them ("Rohan" in "Rohan's bday": the group). The
person's routes are ranked by memory (``rank_routes``: the user's own lasting choice first, then where they message
most lately), and only their own direct messages count (:data:`PERSON_KIND`, the chat's kind as memory keeps it): a
message to a person, a draft or a send in any wording, never goes into a group chat or channel they write in, even
one ranked first. A person whose chats are groups or channels only is told plainly ("I don't have a direct chat with
X"); one with no DM in the named app hears "I don't have a <app> chat with X" (or is offered their DM in another
app); with DMs only in apps the routes don't drive, the run says "no route" (the thinker does it its own way). Each
route's app comes from its stored service or its threads' programs, matched against the apps' data. The app is the one
the user named, else the usual route's; the done line names an app the user didn't. A DM memory linked to two people
goes to the one its chat name names (``contacts.dm_claim``), never to the other (lMsg). A name none of their people
goes by that sounds like exactly one of them is that person for a draft or an open, named in the done line, only when
it is tight to a heard form of theirs the user confirmed ("Kumar" for "quix") or it answers Yuki's own "Who to?" ("to
hardish", fMsg); anything looser, or a send, is asked once ("Did you mean Hemanth?"; :func:`_sounds_alike`), and a send
to that person after the answer is typed and left, never sent by sound (:func:`_send_gate`, review r1 H1). A send the
send check would refuse is refused before the chat opens.

**Opening the chat** (rung A, two ways per app, in order):

* :func:`route_open_link`: a deep link built from the chat's own address in memory (a capture thread's URL, by the app's
  documented URL shape: ``discord://-/channels/...``, ``slack://channel?team=...&id=...``), or WhatsApp's
  ``whatsapp://send?phone=`` when the chat's stored handle is a phone number (digits, checked in code). Opened with its
  default handler (``launch.open_url``'s guarded ``os.startfile``).
* :func:`route_switcher`: the app's own quick switcher (Discord and Slack: Ctrl+K, the chat's name as memory keeps it
  with the app's filter character, Enter) or chat search (WhatsApp: its search shortcut, the name, then the result row
  picked by the keyed digest of that name), run through the voice hands' step runner (each step checked, Yuki's own
  check on every key and every typed text, the user's hands stop it), into the app's own window only, brought forward
  first by the guarded ``focus_window``. The name is typed only once another text field took the keyboard after the
  shortcut (the switcher or the search box opened; :func:`_run_switch`); otherwise nothing more is sent. chat19 (final
  test 19 Oct, RC1): a switcher run whose shortcut went out but that didn't finish closes what it opened with the app's
  own close key (``[chat] close``, on its own judged run: :func:`_tidy_switch`) and ends ``switcher_failed``; the chat
  is never "opened" by the shortcut alone. gb (final-G2 R13): that close key goes as the run's own undo of the field
  its own shortcut opened (contract K-U: Yuki's check is told so only while its focus read shows that field holding
  the keyboard), and a switcher still holding the keyboard as the run ends (measured) ends ``switcher_held``, with
  ``facts["switcher_held"]`` and why it stopped (``stopped``, a code word: contract K-S).

**The keyboard in the composer** (chat19, :func:`_composer_focus`): before anything is typed, the opened chat's
composer gets the keyboard: found by the app's own label for it (``[chat.labels.<lang>] composer``, Discord's
``Edit "Message @<name>"``) and focused through UIA (the game gate and the Act switch asked as for any focus), or the
field the app itself gives the keyboard (structure), confirmed by the focus read within ``uia_route.FOCUS_WAIT_S``.
A dialog holding the keyboard is closed only when this very run's switcher opened it; otherwise nothing is typed and
the result says what holds it (contract C3, :func:`_not_typed`: a failed result with ``why = "not_typed"``, never a
question, so the engine's next rung or the caller's own hands may take it on).

**Checked open by encrypted ids, never a title** (:func:`chat_state`): memory's own read of the window in front (the
watcher's Now board, read after the run began) is looked up as a capture thread (``Store.find_thread``: a keyed digest
of the app and the chat's scope) and must be one of the route's thread ids; another chat of that app in front is a
"no". Failing that, one of the route's threads read since the run began. **Nothing is typed into a chat that isn't
confirmed open.**

**Rule 8** (Q3; the send rule, lead decision 18 Oct): ``send = "send"`` (their words message, tell, ask or reply to
someone and give what to say: "tell Y X", "send X to Y"; the talker or the planner judges it, after the countdown)
types X and presses the app's send key, both judged by Yuki's own check with the user's words, and both whom and what
must stand in their words (:func:`yuki.skills.uia_route.check_sends`); ``draft`` (they asked for a draft) types it and
leaves it; no text (naming only the person) just opens the chat. On the new brain the consent rule may allow a send in Yuki's own
wording of what the user asked to say (failure fix-plan Item 11, row 4, with the 3 s countdown showing the text, or
after their yes): that very step's grant (:attr:`yuki.skills.engine.RunContext.grant`) lets exactly that text go to
exactly that person (by their id in the people index), in their own direct message only, and nothing else (Item 11's L2;
a group chat or channel, even the person's usual one, still needs the user's own words). The opened chat's composer is
read first, the field's own text (its Value and its document, both: either may lag) and never the caret context
(zero-width characters and the BOM are not text: Discord's empty box reads "U+FEFF" and a newline, live 17 Oct):
exactly a text Yuki typed into that chat itself (kept as a keyed digest in RAM, :func:`note_typed`) is selected with
the app's select-all on its own run and typed over; other text already there (the user's kept draft) means nothing is
typed or pressed, said plainly; a composer that can't be read gets a send's words typed and its send key never
pressed (told to press Enter). The send key is its own run, pressed only once the box, read again after the typing,
holds exactly the words (fMsg review M2). A send is "done" only when memory captured the user's own message equal to that text
in that very chat (the messages table, matched by the route's capture thread ids, never by the app's name); "pressed
send, couldn't confirm it landed" otherwise, once the send key went out (its own step dispatched).
If Yuki's own check holds the send key after the text was typed, the user is told it's typed and not sent (never
asked: a "yes" would type it twice).
A rung that acted on a send is never followed by another (the engine's rule: never twice).

**discord.voice** (:func:`route_voice_join`): Discord's quick switcher with its voice filter ("!") and the channel's
name from the user's words, Enter; if Discord doesn't take the microphone within 2 s, Discord's "Join Voice" button
(its label from the data, judged like the switcher's keys). A join that took is noted in tastes (kind
``discord_voice``, encrypted). With a call live it asks first, in the rung (after the engine's game check), and the
question says what will happen (review r2 M2): "Switch to {channel}?" when the call is Discord's own, else "Join
{channel} on Discord as well?" (nothing leaves another app's call); the answer comes back as the ``join_in_call`` slot.
lMsg (live problem 11): a channel memory never saw in the app (:func:`channel_known`) is never joined ("I don't know a
Discord voice channel called NDLS."); memory's open calls are checked against the live microphone (:func:`_current_calls`),
so a call that ended is not "live"; and a ``join_in_call`` "no" with no call live answers nothing.

Every route takes focus: never with a game in front (the engine holds the run: "after the round?"), each focus and
opened link asks ``gamegate.refuse_input`` here, and the input layer refuses again. The steps run through the hands
the run was given: the voice hands, or on the new brain its desk hands (``uia_route.DeskHands``, zFinal: told each
run's steps by :func:`run_steps`, they let the capability's own steps through and the send key only when the brain's
consent rule allowed this very send and counted it down). Without hands (the old loop's own run, a card) a route that
must type or press keys doesn't run; the thinker then does it itself with the skill pinned. Logs and facts are
content-free (code words, counts); names, chat names, links and text stay in RAM.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
import tomllib
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from yuki.skills.engine import NOTHING_OUT as _NOTHING_OUT
from yuki.skills.engine import RouteCall, RouteResult

#: The lines said when nothing is done (asked or told plainly; never a "done").
ASK_WHO = "Who to?"
LINE_UNKNOWN = "I don't know who {who} is."
ASK_WHICH = "Which {who}: {names}?"
ASK_OTHER_APP = "I don't have a {app} chat with {name}. {usual} instead?"
LINE_NO_APP_CHAT = "I don't have a {app} chat with {name}."
LINE_NO_ROUTE = "I don't know where you message {name}."
#: A person memory knows, whose chats with the user are all group chats or channels (review r2 L1): told plainly, never
#: a hand-off that could type into one of those.
LINE_NO_DM = "I don't have a direct chat with {name}."
#: The words are a group chat's or channel's whole name, and also name one of the user's people (review r2 M2): asked,
#: never guessed. ``kind`` is the chat's kind as memory keeps it (``group``, ``channel``).
ASK_PERSON_OR_PLACE = "{person}, or the {place} {kind}?"
LINE_ACT_OFF = "Act is switched off for {app} in Yuki's settings, so I left it."
LINE_TOOK_OVER = "You took over, so I left it there."
LINE_HELD = "Should I go ahead?"
LINE_STAY = "Okay, I'll leave your call as it is."
#: What a "couldn't confirm" says, by what was done.
LINE_UNSURE_CHAT = "I opened {app}, but couldn't confirm it's your chat with {name}, so I didn't type anything."
LINE_OPEN_UNCONFIRMED = "I opened {app}, but couldn't confirm it's your chat with {name}."
LINE_NOT_TYPED = "I opened your chat with {name}, but couldn't type it."
LINE_DRAFT_UNCONFIRMED = "I typed it in your chat with {name}, but couldn't confirm it's there."
LINE_TYPED_NOT_SENT = "I typed it in your chat with {name}, but couldn't confirm it, so I didn't send it."
LINE_SENT_UNCONFIRMED = "I pressed send to {name}, but couldn't confirm it landed."
#: Yuki's own check held the send key after the text was typed (review r1 L6): told, not asked, so a "yes" never
#: re-runs the skill and types the text a second time into the kept draft.
LINE_TYPED_HELD = "It's typed in your chat with {name}, but I held back sending it. Press Enter when you want it to go."
#: The opened chat's composer already holds other text (the app's kept draft; review r2 H1): nothing typed, nothing
#: pressed (Enter would send that text with the user's words).
LINE_DRAFT_THERE = "There's already unsent text in your chat with {name}, so I left it."
#: fMsg review M2: typed, and the message box read back holds more than those words (an earlier draft wasn't replaced):
#: no send key, said plainly
LINE_BOX_MORE = "I typed it in your chat with {name}, but the message box holds more than that, so nothing was sent."
#: A send whose composer couldn't be read before typing (sWire r1 M1: it may hold the app's kept draft, which Enter
#: would send with the user's words): the words are typed and the send key is never pressed; told, not asked (a "yes"
#: would re-run the skill and type them a second time).
LINE_TYPED_UNREAD = ("It's typed in your chat with {name}, but I couldn't read the message box, so I didn't send it. "
                     "Press Enter when you want it to go.")
#: lMsg: a send Yuki's own send check refused before any step (whom or what is not in the user's words): nothing typed,
#: nothing sent; told, not asked (a "yes" would only meet the same check). Review r1 L7: the check runs before the chat
#: is opened (:func:`_send_gate`), so nothing takes focus; :data:`LINE_SEND_REFUSED` is the runner's own later refusal.
LINE_SEND_HELD = "I didn't send anything to {name}: I wasn't sure it's who and what you said."
LINE_SEND_REFUSED = ("Your chat with {name} is open, but I didn't type or send anything: I wasn't sure it's what you "
                     "said.")
#: lMsg review r1 H1: a send to the person the sound-alike question asked about, after the user answered it, is typed
#: and left (whom it goes to is never settled by sound; the user presses Enter, or a later send by the name memory
#: learned from their answer goes as usual). Told, not asked.
LINE_TYPED_ALIKE = ("It's typed in your chat with {name}, not sent: I don't send to a name I only matched by sound. "
                    "Press Enter if it's them.")
LINE_VOICE_UNCONFIRMED = "I opened {channel} in Discord, but couldn't confirm you joined it."
#: lMsg (live problem 11): a voice channel memory never saw in the app is never joined; said plainly (never a guess).
LINE_NO_CHANNEL = "I don't know a {app} voice channel called {channel}."
#: Asked before a join while a call is live (review r2 M2: the question says what the route does; never a promise to
#: leave a call it can't leave). The app's own words come from its data (``[chat.asks]``); these are the fallbacks.
ASK_ALSO_JOIN = "You're already in a call. Join {channel} on {app} as well?"
ASK_SWITCH = "Switch to {channel}?"
#: The done lines (said only once the done-check saw it).
SAY_OPENED = "Your chat with {name} is open{where}."
SAY_DRAFT = "It's typed in your chat with {name}{where}, not sent."
SAY_SENT = "Sent to {name}{where}."
SAY_JOINED = "Joined {channel} on {app}."

#: The chat seen open before anything is typed (s); Discord taking the microphone after the switcher, after Join (s).
CHAT_WAIT_S = 4.0
VOICE_WAIT_S = 2.0
JOIN_WAIT_S = 3.0
POLL_S = 0.25
#: A route kind with no filter character in a quick switcher (a group DM goes by its own name).
KINDS = ("dm", "group", "channel")
#: The one route kind a message to a person goes to: their own direct message (memory's ``contacts.ROUTE_KINDS``). A
#: group chat or channel is read by other people; it is a target only when the user named it (a place).
PERSON_KIND = "dm"


# ---------------------------------------------------------------------------------------------------------------------
# The apps' own data ([chat] of each connection file)
# ---------------------------------------------------------------------------------------------------------------------

_lock = threading.Lock()
_cache: dict[str, tuple[tuple[float, int], dict[str, Any]]] = {}


def _conn_dir() -> Path:
    from yuki.skills import connection

    return Path(connection.CONNECTIONS_DIR)


def app_data(app: str) -> dict[str, Any]:
    """The ``[chat]`` table of ``connections/<app>.toml`` (cached until the file changes); ``{}`` when there is none."""
    key = str(app or "").strip().casefold()
    if not key or not key.replace("_", "").isalnum():
        return {}
    path = _conn_dir() / f"{key}.toml"
    try:
        st = path.stat()
    except OSError:
        return {}
    stamp = (st.st_mtime, st.st_size)
    with _lock:
        hit = _cache.get(str(path))
        if hit is not None and hit[0] == stamp:
            return hit[1]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        data = {}
    chat = data.get("chat") if isinstance(data.get("chat"), dict) else {}
    with _lock:
        _cache[str(path)] = (stamp, chat)
    return chat


def chat_apps() -> list[str]:
    """The apps whose connection file has a ``[chat]`` table (file names, sorted)."""
    try:
        names = sorted(p.stem for p in _conn_dir().glob("*.toml"))
    except OSError:
        return []
    return [n for n in names if app_data(n)]


def _app_key(name: Any) -> str:
    from yuki.memory.store import app_key

    return app_key(str(name or "")) or ""


def app_of(service: str | None, processes: Sequence[str] = ()) -> str:
    """The chat app a route belongs to: its stored service (a URL host or a program's key) among an app's
    ``services``, else one of its threads' programs among an app's ``processes``. Identities from data; ``""``."""
    s = str(service or "").strip().casefold()
    procs = {_app_key(p) for p in processes if p}
    apps = chat_apps()
    for app in apps:
        d = app_data(app)
        if s and (s in {str(x).casefold() for x in d.get("services") or []}
                  or s in {_app_key(x) for x in d.get("processes") or []}):
            return app
    for app in apps:
        if procs & {_app_key(x) for x in app_data(app).get("processes") or []}:
            return app
    return ""


def _title(app: str) -> str:
    return str(app_data(app).get("title") or app.title())


# ---------------------------------------------------------------------------------------------------------------------
# Whom and where (memory's people index; RAM only)
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class ChatRoute:
    """One place the user talks with the target: its app (from data), kind, chat name and handle as memory keeps them,
    its capture threads (ids, merged ones resolved) and their addresses (RAM only)."""

    app: str
    kind: str
    chat: str
    handle: str | None
    thread_ids: list[int]
    urls: list[str] = field(default_factory=list)
    preferred: bool = False
    #: review r2 L2: another of the user's people holds a route on this chat's threads too (memory linked one DM to two
    #: people, two Rahuls): the chat is not one person's own DM for sure, so no send grant covers it
    shared: bool = False


@dataclass
class Target:
    """Whom the message is for: the name memory keeps (a person's, or a group chat's), the user's own words for them,
    and the routes, the usual first. ``id``: the person's id in memory's people index (``None`` for a group chat or
    channel), which a send into that person's own direct message declares, so the brain's consent grant is matched by
    identity (Item 11's L2; a send into a group or channel route of theirs declares none)."""

    name: str
    said: str
    routes: list[ChatRoute]
    place: bool = False
    id: int | None = None
    #: review r2 L1: the person has chats with the user, and none is a direct message (groups or channels only)
    no_dm: bool = False
    #: lMsg: found by how the words sound (a heard form the user confirmed, for a draft or an open only), never as
    #: written: no send goes to such a target, and no send grant covers it
    by_sound: bool = False


@dataclass
class Asked:
    """A question (or a plain line) instead of a guess."""

    line: str
    options: list[str] = field(default_factory=list)
    outcome: str = "asked"


def _words(value: Any) -> str:
    return " ".join(str(value or "").split())


def _chat_route(store: Any, route: Any) -> ChatRoute:
    ids = [int(t) for t in getattr(route, "thread_ids", []) or [] if isinstance(t, int)]
    procs: list[str] = []
    urls: list[str] = []
    if ids:
        marks = ",".join("?" * len(ids))
        try:
            for proc, url_ct in store._query(f"SELECT process, url_ciphertext FROM threads WHERE id IN ({marks})",
                                             ids):
                if proc:
                    procs.append(str(proc))
                if url_ct:
                    try:
                        url = store._dec(url_ct)
                    except Exception:  # noqa: BLE001 - a row the key can't read
                        url = None
                    if url:
                        urls.append(str(url))
        except Exception:  # noqa: BLE001
            pass
    return ChatRoute(app=app_of(getattr(route, "service", ""), procs), kind=str(getattr(route, "kind", "") or ""),
                     chat=str(getattr(route, "chat", "") or ""), handle=getattr(route, "handle", None),
                     thread_ids=ids, urls=list(dict.fromkeys(urls)),
                     preferred=bool(getattr(route, "preferred", False)))


def _person_target(store: Any, contact: Any, said: str, *, by_sound: bool = False) -> Target:
    """``contact`` as a message target: their own direct messages as routes, most used first (:data:`PERSON_KIND`,
    memory's chat kind: never a group chat or channel they write in, even one ranked first). A DM memory also linked
    to other people is marked ``shared`` (no send grant covers it) and comes after their own; one whose chat name
    names one of those others and not them (``contacts.dm_claim``: Malini's link to "Darvesh (DM)", live data 17 Oct)
    is not theirs at all and is left out, so "message Malini" never opens Darvesh's chat."""
    from yuki.memory import contacts as C

    try:
        ranking = C.rank_routes(store, contact)
        dms = [r for r in ranking.routes if getattr(r, "kind", "") == PERSON_KIND]
        others = len(ranking.routes) - len(dms)
        owners = C.contacts_of_threads(store, [t for r in dms for t in r.thread_ids]) if dms else {}
        own: list[ChatRoute] = []
        shared: list[ChatRoute] = []
        for r in dms:
            holders = {int(c.id): c for t in r.thread_ids for c, _r in owners.get(t, []) if int(c.id) != int(contact.id)}
            claim = C.dm_claim(contact, r, list(holders.values())) if holders else C.CLAIM_OWN
            if claim == C.CLAIM_OTHER:
                continue
            route = _chat_route(store, r)
            # review r2 L2: a DM whose threads another live contact also holds is not this person's alone
            route.shared = bool(holders)
            (own if claim == C.CLAIM_OWN else shared).append(route)
        routes = own + shared
    except Exception:  # noqa: BLE001
        routes, others = [], 0
    return Target(name=contact.name, said=said, routes=routes, id=int(contact.id), no_dm=not routes and others > 0,
                  by_sound=by_sound)


#: lMsg (live test 17 Oct: "Kumar" for the user's "quix"): asked once when a name none of their people goes by sounds
#: like exactly one of them by the looser rule (``contacts.sounds_alike``), or by the tight rule on a send. The strict
#: rule's matches are the skill resolver's own question (``resolve.CONFIRM_PERSON``, the same words). The person is
#: named by memory's name when the form matched is a heard form ("Did you mean Hemanth?", never "niche?").
ASK_ALIKE = "Did you mean {name}?"


#: How long Yuki's own "Who to?" for a message stays open for the user's answer (s), as the sound-alike question's
#: (``contacts.ALIKE_ANSWER_S``).
WHO_ANSWER_S = 120.0
#: The most "Who to?" questions kept.
WHO_KEPT = 4
_who_lock = threading.Lock()
#: [(when, how many turns of the user's stood when it was asked, keyed digest of the newest)]. RAM only, never logged.
_who_asked: list[tuple[float, int, bytes]] = []


def _turns(words: Sequence[str] | None) -> list[str]:
    return [str(w) for w in words or () if str(w or "").strip()]


def note_who_asked(words: Sequence[str] | None, *, now: float | None = None) -> None:
    """Note that Yuki asked whom a message is for ("Who to?"), with the user's turns as they stood (``words``, newest
    last): this driver's own question (:func:`find_target`), and the skill resolver's for the message's person slot
    that nobody gave (fMsg merge note E1). Only the user's very next turn answers it (:func:`_answers_who`). RAM only
    (a keyed digest of the newest turn); never raises."""
    try:
        turns = _turns(words)
        at = time.time() if now is None else float(now)
        last = _typed_digest(turns[-1]) if turns else b""
        with _who_lock:
            kept = [w for w in _who_asked if 0.0 <= at - w[0] <= WHO_ANSWER_S]
            _who_asked[:] = (kept + [(at, len(turns), last)])[-WHO_KEPT:]
    except Exception:  # noqa: BLE001
        pass


def _answers_who(who: str, words: Sequence[str] | None, *, now: float | None = None) -> bool:
    """Whether the name ``who`` answers Yuki's own "Who to?" (:func:`note_who_asked`): asked within
    :data:`WHO_ANSWER_S`, the user has said exactly one turn since (the turns as they stood are still there, unchanged)
    and the name stands, whole words in order, in that turn. A later turn, or a name said anywhere else, is no answer
    to it. Identity and order only; never raises."""
    try:
        from yuki.memory.contacts import span_in

        turns = _turns(words)
        at = time.time() if now is None else float(now)
        with _who_lock:
            asked = [w for w in _who_asked if 0.0 <= at - w[0] <= WHO_ANSWER_S]
        for _t, n, last in asked:
            if len(turns) == n + 1 and (n == 0 or _typed_digest(turns[n - 1]) == last) and span_in(who, turns[-1]):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _sounds_alike(store: Any, who: str, mode: str, words: Sequence[str] = ()) -> Target | Asked | None:
    """For a name none of the user's people (nor a group chat or channel) goes by as written: the one person it sounds
    like (``contacts.sounds_alike``). For a draft or an open (ask or act by cost, synthesis §6.2: a draft is typed and
    left, an open only shows a chat, and the done line names the person, so a wrong guess shows), that person
    (``by_sound``) when either holds: the name is tight to a heard form of theirs the user confirmed ("Kumar" for
    "quix": that form plus one final consonant), or it is the user's answer to Yuki's own "Who to?" (:func:`_answers_who`,
    fMsg, live 18 Oct: "to hardish" answering "Who to?" was asked "Did you mean Hemanth?" a second time). Anything else
    is asked once, "Did you mean Hemanth?" (review r1 M2, fMsg review H1: a bare loose match - "Nick", "Nisha", "Papa"
    on the user's index - never opens a chat or types unasked), and so is a send, always (rule 8, x6/xL2Grant: whom a
    message is sent to is never matched by sound, confirmed first). The question is noted with the user's turns as
    they stand (``contacts.note_alike_asked``) so only an answer given after it counts (:func:`_answered_alike`).
    ``None``: nobody alike or two people alike, the strict rule's match (the resolver asks that), or the question was
    asked already (the same name again: the answer was no)."""
    from yuki.memory import contacts as C

    alike = C.sounds_alike(store, who)
    if alike is None or alike.strict:
        return None
    if mode != "send" and (alike.tight or _answers_who(who, words)):
        return _person_target(store, alike.contact, who, by_sound=True)
    if C.note_alike_asked(store, who, int(alike.contact.id), words):
        name = alike.contact.name if alike.confirmed else alike.form
        return Asked(ASK_ALIKE.format(name=name), list(dict.fromkeys([name, alike.form])), outcome="sounds_like")
    return None


def _answered_alike(call: RouteCall, target: Target) -> bool:
    """Whether this run's person is the one the sound-alike question asked about, answered since
    (``contacts.alike_answered``: a turn of the user's newer than the question). Such a run may open the chat or type
    a draft, never send (review r1 H1): :func:`_send_gate` turns its send into a draft."""
    if target.place or target.id is None:
        return False
    store = _store(call.ctx)
    if store is None:
        return False
    try:
        from yuki.memory.contacts import alike_answered

        return alike_answered(store, target.id, [str(w) for w in call.ctx.words or () if w]) is not None
    except Exception:  # noqa: BLE001
        return False


def find_target(store: Any, person: str, *, mode: str = "", words: Sequence[str] = ()) -> Target | Asked:
    """Whom ``person`` (the user's words) names: one of their people (``contacts.resolve``: a name or alias, whole or a
    whole-word part of it, a misheard form the user corrected; two who fit: a question), with only their own direct
    messages as routes (:func:`_person_target`); else a group chat or channel whose name is those words (identity);
    else, for a run (``mode``: ``send``, ``draft`` or ``open``; ``words``: the user's turns, newest last), the one
    person the name sounds like (:func:`_sounds_alike`: for a draft or an open, a tight match or the answer to Yuki's
    "Who to?"; else asked once); else "I don't know who that is" ("Who to?" when no name was given, noted for the
    answer: :func:`note_who_asked`). Without ``mode`` (:func:`whom`, the send grant's person, the done-check) never by
    sound. When the words
    are a group's or channel's whole name, a person whose name only stands inside them is not the one named (the place
    wins), and a person they name as a whole name or a part of one makes it a question, never a guess (review r2
    M2)."""
    from yuki.memory import contacts as C

    who = _words(person)
    if not who:
        if mode:
            note_who_asked(words)
        return Asked(ASK_WHO, outcome="no_person")
    try:
        found = C.resolve(store, who)
    except Exception:  # noqa: BLE001
        found = []
    people: dict[int, Any] = {}
    for c in found:
        people.setdefault(int(c.id), c)
    want = C.tokens(who)
    try:
        places = [r for r in C.places(store) if want and C.tokens(r.chat) == want]
    except Exception:  # noqa: BLE001
        places = []
    if places and people:
        # review r2 M2: the words are a group chat's or channel's whole name. A person whose name only stands inside
        # them ("Rohan" in "Rohan's bday") is not who was named: the place the user named wins. A person the words
        # name as a whole name or alias, or as a part of their name, fits too: asked, never guessed
        fits = [c for c in people.values() if any(C.tokens(n) == want or C.span_in(want, n) for n in c.names)]
        if not fits:
            people = {}
        else:
            kind = str(getattr(places[0], "kind", "") or "group")
            return Asked(ASK_PERSON_OR_PLACE.format(person=fits[0].name, place=places[0].chat, kind=kind),
                         [fits[0].name, places[0].chat], outcome="person_or_place")
    if len(people) > 1:
        names = [c.name for c in people.values()][:3]
        return Asked(ASK_WHICH.format(who=who, names=" or ".join(names)), names, outcome="two_people")
    if people:
        return _person_target(store, next(iter(people.values())), who)
    if places:
        now = time.time()
        try:
            places = sorted((C.measure_route(store, r, None, now) for r in places), key=lambda r: -r.score)
        except Exception:  # noqa: BLE001
            pass
        return Target(name=places[0].chat, said=who, routes=[_chat_route(store, r) for r in places], place=True)
    if mode:
        try:
            alike = _sounds_alike(store, who, mode, words)
        except Exception:  # noqa: BLE001 - memory can't say: nobody, as before
            alike = None
        if alike is not None:
            return alike
    return Asked(LINE_UNKNOWN.format(who=who), outcome="unknown_person")


def whom(store: Any, name: str) -> tuple[str, int | None]:
    """How the chat driver reads ``name`` as a recipient (:func:`find_target`, the very lookup a run makes):
    ``("person", id)``, ``("place", None)`` (a group chat or channel by its whole name), ``("ask", None)`` (two people,
    or a person and a place: a question) or ``("unknown", None)``. A name written in another script than their people's names
    ("हेमंत") is the one person it reads as (``contacts.transliterated``). Identity data only; never raises."""
    try:
        from yuki.memory import contacts as C

        target = find_target(store, name)
        if isinstance(target, Target):
            return ("place", None) if target.place or target.id is None else ("person", int(target.id))
        if target.outcome == "unknown_person":
            one = C.transliterated(store, name)
            return ("person", int(one.id)) if one is not None else ("unknown", None)
        return ("ask", None) if target.outcome in ("two_people", "person_or_place") else ("unknown", None)
    except Exception:  # noqa: BLE001
        return "unknown", None


def pick_route(target: Target, app_named: str | None) -> ChatRoute | Asked:
    """The route to use: the named app's (none there: a question offering their DM in another app, else the plain
    "I don't have a <app> chat with X"), else the usual route. A person's routes are their direct messages only
    (:func:`find_target`), never a group chat or channel: with groups or channels only, the plain "I don't have a
    direct chat with X" (review r2 L1); with DMs only in apps the routes don't drive, or no chats known at all, "no
    route" (memory knows them, but no chat of theirs the routes drive: the thinker does it its own way)."""
    known = [r for r in target.routes if r.app]
    if app_named:
        hit = next((r for r in known if r.app == app_named), None)
        if hit is not None:
            return hit
        if known:
            return Asked(ASK_OTHER_APP.format(app=_title(app_named), name=target.name, usual=_title(known[0].app)),
                         [known[0].app], outcome="no_app_chat")
        return Asked(LINE_NO_APP_CHAT.format(app=_title(app_named), name=target.name), outcome="no_app_chat")
    if known:
        return known[0]
    if target.no_dm:
        # review r2 L1: their chats are groups or channels only: said plainly, never a hand-off that could type there
        return Asked(LINE_NO_DM.format(name=target.name), outcome="no_dm")
    return Asked(LINE_NO_ROUTE.format(name=target.name), outcome="no_route")


def _store(ctx: Any) -> Any:
    from yuki.skills.engine import _mem, _store_of

    memory = getattr(ctx, "memory", None)
    if memory is None:
        return None
    return _mem(memory, "skill_store", lambda c: _store_of(c), timeout_s=0.5)


def _probe_store(ctx: Any) -> Any:
    call = getattr(ctx, "memory_call", None)
    if call is None:
        return None
    try:
        return call("skill_store", lambda c: getattr(c, "store", c))
    except Exception:  # noqa: BLE001
        return None


def _said_as(call: RouteCall, target: Target) -> str:
    """Whom a send goes to, in the user's own words (rule 8: the send check reads ``to`` as a span of what they said).
    The name as given or as memory keeps it when it stands in their words; else the span of their words that names
    that same person as written, as a heard form memory learned when they confirmed it ("hey meant"), or as a
    transliteration ("हेमंत"): :func:`yuki.memory.contacts.names_person` with ``by_sound=False``, so a Latin word that
    only sounds like the person never authorises a send. Nothing found (or a group chat): the name as given, which the
    send check refuses before any step, so nothing is typed or sent and the run falls back (Item 5), unless the brain's
    consent grant for this very step covers the send (the user said yes to "Send ... to Hemanth?": the same person
    by id, in their own direct message, :func:`yuki.skills.uia_route.granted_send`)."""
    from yuki.skills.uia_route import said

    if target.by_sound:
        # lMsg: a person found only by how the words sound never receives a send (their name as memory keeps it is
        # not in the user's words, so the send check refuses it before any step)
        return target.name
    words = [str(w) for w in call.ctx.words or () if w]
    for form in (target.said, target.name):
        if form and said(form, words):
            return form
    if target.place:
        return target.said
    store = _store(call.ctx)
    if store is None:
        return target.said
    try:
        from yuki.memory import contacts as C

        person = C.names_person(store, target.said or target.name, words, by_sound=False)
        if person is not None:
            for text in reversed(words):
                for c, span in C.named_in(store, text):
                    if c.id == person.id and said(span, words):
                        return span
    except Exception:  # noqa: BLE001 - a memory read never sends; the name as given is refused
        pass
    return target.said


def _app_named(values: Mapping[str, Any]) -> str | None:
    """The chat app the run's ``app`` value names: an app with chat data by its key, title, program or image name
    (identity), else the one chat app whose title the value sounds like (``contacts.sounds_in``, the short-name rule:
    "this card" for Discord, live 17 Oct, when the value reached the driver as heard); else the value's own key (no
    chat data: "I don't have a <app> chat with X"). ``None`` when no app was given."""
    app = values.get("app")
    key = _app_key(app) if app else ""
    if not key:
        return None
    if app_data(key):
        return key
    apps = chat_apps()
    for name in apps:
        d = app_data(name)
        known = {name, _app_key(d.get("title"))} | {_app_key(x) for x in [*(d.get("processes") or []),
                                                                         *(d.get("images") or [])]}
        if key in known:
            return name
    try:
        from yuki.memory.contacts import sounds_in

        heard = [name for name in apps if sounds_in(_title(name), [str(app)], short=True)]
    except Exception:  # noqa: BLE001
        heard = []
    return heard[0] if len(heard) == 1 else key


def _target_route(call: RouteCall) -> tuple[Target, ChatRoute] | RouteResult:
    """The target and its route for this run (resolved once per run, kept on the run's context), or a result: a
    question, or "couldn't run" without memory."""
    cache = getattr(call.ctx, "_chat_cache", None)
    if cache is None:
        cache = {}
        try:
            setattr(call.ctx, "_chat_cache", cache)
        except Exception:  # noqa: BLE001
            pass
    person = call.values.get("person")
    person = person.get("name") if isinstance(person, Mapping) else person
    text, send = _text_mode(call.values)
    mode = "send" if send else ("draft" if text else "open")
    key = (_words(person), _app_named(call.values) or "", mode)
    if key not in cache:
        store = _store(call.ctx)
        if store is None:
            cache[key] = None
        else:
            target = find_target(store, str(person or ""), mode=mode,
                                 words=[str(w) for w in call.ctx.words or () if w])
            cache[key] = target if isinstance(target, Asked) else (target, pick_route(target, key[1] or None))
    got = cache[key]
    if got is None:
        return RouteResult(ran=False, outcome="no_memory")
    if isinstance(got, Asked):
        return RouteResult(ran=False, ask=got.line, options=list(got.options), outcome=got.outcome)
    target, route = got
    front = _front_of(call)
    if front is not None:
        # lb B1 (K-F): the person came from the chat in front when they spoke: the run stays in that very chat (the
        # route holding its thread), never their usual one elsewhere; none of theirs holds it: asked, nothing opened
        here = next((r for r in target.routes if int(front.get("thread") or 0) in set(r.thread_ids)), None)
        if here is None or (key[1] and here.app != key[1]):
            return RouteResult(ran=False, ask=ASK_WHO, outcome="no_person")
        return target, here
    if isinstance(route, Asked):
        if route.outcome == "no_route":
            # memory knows the person, but no chat of theirs is in an app these routes drive: the thinker may (pinned)
            return RouteResult(ran=False, outcome="no_route")
        return RouteResult(ran=False, ask=route.line, options=list(route.options), outcome=route.outcome)
    return target, route


def _front_of(call: RouteCall) -> dict[str, Any] | None:
    """lb B1 (K-F): the chat in front the resolver took this run's person from (the engine's ``ctx.front_chat``, set
    only when the person's fill source is ``resolve.FROM_FRONT``), else ``None``. Never raises."""
    try:
        from yuki.skills.resolve import FROM_FRONT

        sources = getattr(call, "fill_sources", None)
        if not isinstance(sources, Mapping) or FROM_FRONT not in {str(v) for v in sources.values()}:
            return None
        front = getattr(call.ctx, "front_chat", None)
        return dict(front) if isinstance(front, Mapping) and front.get("thread") else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------------------------------------------------
# Deep links and the quick switcher (data: the app's documented shapes and shortcuts)
# ---------------------------------------------------------------------------------------------------------------------


def _phone(handle: str | None) -> str | None:
    """A handle that is a phone number: digits (7-15), an optional leading +, spaces and dashes aside. Checked in
    code, a fixed format."""
    text = "".join(str(handle or "").split()).replace("-", "")
    digits = text[1:] if text.startswith("+") else text
    return digits if digits.isdigit() and 7 <= len(digits) <= 15 else None


def deep_link(data: Mapping[str, Any], route: ChatRoute) -> str | None:
    """The app's own link to this chat: the first of the app's ``links`` whose URL shape one of the chat's captured
    addresses matches (its values put into the URI shape), else the phone link when the handle is a phone number."""
    from yuki.connect.templates import match_template

    for link in data.get("links") or []:
        if not isinstance(link, Mapping):
            continue
        url_t, uri_t = str(link.get("url") or ""), str(link.get("uri") or "")
        if not url_t or not uri_t:
            continue
        for url in route.urls:
            got = match_template(url_t, url)
            if got:
                out = uri_t
                for k, v in got.items():
                    if not str(v).replace("@", "").isalnum():
                        out = ""
                        break
                    out = out.replace("{" + k + "}", str(v))
                if out and "{" not in out:
                    return out
    phone = data.get("phone") if isinstance(data.get("phone"), Mapping) else None
    number = _phone(route.handle)
    if phone and number and phone.get("uri"):
        return str(phone["uri"]).replace("{phone}", number)
    return None


def switcher_query(data: Mapping[str, Any], route: ChatRoute) -> str:
    """What goes into the quick switcher: the app's filter character for the chat's kind and the chat's name as memory
    keeps it (a leading filter character already there is not doubled)."""
    prefix = str((data.get("prefix") or {}).get(route.kind) or "")
    name = _words(route.chat)
    if prefix and name.startswith(prefix):
        name = name[len(prefix):].strip()
    return prefix + name


def run_steps(hands: Any, steps: list[dict[str, Any]], *, words: Sequence[str], apps: Sequence[str], key: str,
              way_text: str = "", sends: Mapping[str, Any] | None = None,
              grant: Mapping[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
    """Steps through the voice hands' step runner (:class:`yuki.skills.uia_route.SkillRunner`: each checked, keys and
    typing judged by Yuki's own check, the user's hands stop it). ``way_text``: the route's own words that may be typed
    (a chat's name memory keeps); everything else typed must be the user's words. ``sends``: ``{"to", "what"}`` when the
    steps send (rule 8: both must stand in the user's words), with ``kind`` the chat's kind as memory keeps it
    (``dm``, ``group``, ``channel``) and, only for the person's own direct message, ``person_id`` their id in memory's
    people index; ``grant``: the brain's consent grant for this very ``run_skill`` step (:attr:`RunContext.grant
    <yuki.skills.engine.RunContext.grant>`, validated by the engine; ``None`` on the voice and the old loop), which
    lets exactly its text go to exactly its person though Yuki worded it (:func:`yuki.skills.uia_route.granted_send`).
    Refused before any step: ``(False, {"refused_steps": True})``."""
    from yuki.skills.uia_route import SkillRefused, SkillRunner

    item: dict[str, Any] = {"run": [dict(s) for s in steps], "apps": list(apps), "ref": f"skill:{key}",
                            "intent": key, "text": way_text}
    args: dict[str, Any] = {"steps": [dict(s) for s in steps], "sends": bool(sends)}
    if sends:
        item["sends"] = True
        args.update(to=str(sends.get("to") or ""), what=str(sends.get("what") or ""))
        for name in ("person_id", "kind"):
            if sends.get(name) is not None:
                args[name] = sends.get(name)
    # zFinal: hands that check a capability's own steps by what its driver declared (the new brain's desk hands,
    # ``uia_route.DeskHands.driver_steps``) are told this run's steps, the route's own words and what it sends
    declare = getattr(hands, "driver_steps", None)
    scope = (declare(key=key, steps=[dict(s) for s in steps], way_text=way_text, sends=sends) if callable(declare)
             else nullcontext())
    try:
        with scope:
            ok, _report, facts = SkillRunner(hands).run(args, item=item, words=[str(w) for w in words if w],
                                                        grant=grant if sends else None)
    except SkillRefused:
        return False, {"refused_steps": True, "steps_done": 0}
    return bool(ok), dict(facts or {})


def _stopped(facts: Mapping[str, Any], app: str, *, acted: bool) -> RouteResult | None:
    """A run of steps that stopped for a reason the user hears (Act off, Yuki's own check, they took over, a game came
    up), as the route's result; ``None`` otherwise."""
    if facts.get("act_off"):
        return RouteResult(ran=acted, ask=LINE_ACT_OFF.format(app=_title(app)), outcome="act_off")
    if facts.get("to_planner"):
        # bSkill (bTalk merge note 4): the consent rule handed over a step that would send or join (brain = "all");
        # it did not run. The engine takes the run to rung C (the plan seed), never "Should I go ahead?"
        from yuki.skills.engine import TO_PLANNER

        return RouteResult(ran=acted, outcome=TO_PLANNER)
    if facts.get("held"):
        short = int(facts.get("steps_done") or 0) < int(facts.get("steps") or 0)
        if not str(facts.get("question") or "") and short and (kind := _refused("keys")):
            # a rule's refusal with no question while a game is in front: "after the round?", never "go ahead?" (a
            # gate that refused for another reason, fHands: the plain blocked stop, never the game line)
            return RouteResult(ran=acted, held=kind, outcome="refused")
        return RouteResult(ran=acted, ask=str(facts.get("question") or "") or LINE_HELD, outcome="held_by_check")
    if facts.get("took_over_at"):
        return RouteResult(ran=acted, ask=LINE_TOOK_OVER, outcome="taken_over")
    if not facts.get("refused_steps") and int(facts.get("steps_done") or 0) < int(facts.get("steps") or 0) \
            and (kind := _refused("keys")):
        return RouteResult(ran=acted, held=kind, outcome="refused")
    return None


def _step_verdict(facts: Mapping[str, Any], do: str) -> bool | None:
    """The verdict of the first action of kind ``do`` in this run (``None``: none ran, or its effect couldn't be
    seen)."""
    for r in facts.get("results") or []:
        if getattr(r, "kind", "") == "action" and getattr(getattr(r, "step", None), "do", "") == do:
            return getattr(r, "verdict", None)
    return None


#: How long the composer's own text may take to read (s), and how much of it is read (characters; more is "other text").
FIELD_READ_S = 0.6
FIELD_CHARS = 4000


def _field_text(window: int, timeout_s: float = FIELD_READ_S) -> list[str] | None:
    """Every read of the whole text of the field with the keyboard, scoped to that field (fMsg, live 18 Oct): its
    Value pattern's value and its Text pattern's own document range, each that answers (Discord's empty box gives
    "U+FEFF" and a newline). Both are read because either may lag what the box shows: a rich-text editor's Value can
    read empty while the text is in its document (``input._missing_unless_parts``), so the caller calls the box empty
    only when every read is (fMsg review M3: a lagging Value right after a chat switch would hide the user's kept
    draft). Never the characters around the caret: in a Chromium app (Discord, Slack, WhatsApp's web view) a caret
    range moved back by characters walks out of the field into the chat's messages above it, so an empty composer read
    as "unsent text" and nothing was typed (msg_hemanth_vc_draft, msg_hemanth_hinglish_quote). The field must sit in
    ``window``, the top-level window the hands' own focus read named (review L1: never another window's field, and a
    stub or offline desktop whose window isn't the real one in front reads nothing). RAM only, never logged; ``None``
    when no field has the keyboard, it is in another window or a password field, it exposes neither pattern, or UIA
    doesn't answer within ``timeout_s``."""
    if not window:
        return None
    try:
        import comtypes
        import comtypes.client

        from yuki.perception import tree as T
    except Exception:  # noqa: BLE001 - no UIA here: unread
        return None
    out: dict[str, Any] = {}

    def worker() -> None:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:  # noqa: BLE001
            pass
        try:
            module = T._uia_core()
            automation = comtypes.client.CreateObject(T._CUIAUTOMATION_CLSID, interface=module.IUIAutomation)
            element = automation.GetFocusedElement()
            if not element:
                return
            request = T._build_cache_request(automation, T._TREE_SCOPE_ELEMENT)
            request.AddProperty(T._P_IS_TEXT_AVAILABLE)
            request.AddProperty(T._P_IS_PASSWORD)
            get = element.BuildUpdatedCache(request).GetCachedPropertyValue
            if T._as_bool(get(T._P_IS_PASSWORD)):
                return
            try:
                native = int(get(T._P_NATIVE_WINDOW_HANDLE) or 0)
            except Exception:  # noqa: BLE001
                native = 0
            if T._top_window_of(native)[0] != int(window):
                return
            reads: list[str] = []
            if T._as_bool(get(T._P_IS_VALUE_AVAILABLE)):
                value = get(T._P_VALUE_VALUE)
                reads.append(str(value if isinstance(value, str) else "")[:FIELD_CHARS + 1])
            if T._as_bool(get(T._P_IS_TEXT_AVAILABLE)):
                try:
                    pattern = element.GetCurrentPattern(T._PATTERN_TEXT).QueryInterface(
                        module.IUIAutomationTextPattern)
                    reads.append(str(pattern.DocumentRange.GetText(FIELD_CHARS + 1) or ""))
                except Exception:  # noqa: BLE001 - the Value read (if any) stands alone
                    pass
            if reads:
                out["reads"] = reads
        except BaseException:  # noqa: BLE001 - unread
            out.pop("reads", None)
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass

    thread = threading.Thread(target=worker, name="yuki-chat-composer", daemon=True)
    thread.start()
    thread.join(max(0.05, float(timeout_s)))
    got = out.get("reads")
    return list(got) if isinstance(got, list) and got else None


def _composer_text(hands: Any) -> tuple[list[str] | None, int]:
    """What the text field with the keyboard (the opened chat's composer) holds now, every read of it
    (:func:`_field_text`), and the top-level window it is in, read in RAM and dropped (never logged). The hands' focus
    read says whether a text field has the keyboard (and never a password field's) and in which window; its contents
    are the field's own, never the caret context. ``None`` when no text field has the keyboard or its contents can't
    be read (sWire r1 M1: an unreadable composer may hold the app's kept draft, so it is never "empty")."""
    read = getattr(hands, "_focus", None)
    if not callable(read):
        return None, 0
    try:
        snap = read(timeout_s=0.4)
    except Exception:  # noqa: BLE001
        return None, 0
    if snap is None or not getattr(snap, "accepts_text", False) or getattr(snap, "is_password", False):
        return None, 0
    window = int(getattr(snap, "window", 0) or 0)
    return _field_text(window), window


# ---------------------------------------------------------------------------------------------------------------------
# Yuki's own typing (RAM only): a composer holding exactly a text Yuki typed there is Yuki's draft, never the user's
# ---------------------------------------------------------------------------------------------------------------------

#: How long a text Yuki typed is known as its own (s): an earlier turn or request of the same sitting.
TYPED_KEEP_S = 1800.0
#: The most typed texts kept per window.
TYPED_PER_WINDOW = 8
_TYPED_KEY = os.urandom(16)
_typed_lock = threading.Lock()
#: window -> [(when, the chat's key or ``b""`` for the window as a whole, keyed digest of the text as compared)]. RAM
#: only, never logged; a restart forgets it.
_typed: dict[int, list[tuple[float, bytes, bytes]]] = {}


def _typed_digest(text: Any) -> bytes:
    from yuki.skills.donecheck import _norm_text

    norm = _norm_text(text)
    return hmac.new(_TYPED_KEY, norm.encode("utf-8"), hashlib.sha256).digest() if norm else b""


def _chat_key(chat: Sequence[int] | None) -> bytes:
    """One chat of an app's window, as the route knows it (memory's capture thread ids), keyed in RAM; ``b""`` when
    the caller doesn't know the chat (the window as a whole)."""
    ids = sorted({int(t) for t in chat or () if isinstance(t, int)})
    if not ids:
        return b""
    return hmac.new(_TYPED_KEY, ("chat:" + ",".join(map(str, ids))).encode("ascii"), hashlib.sha256).digest()


def note_typed(window: int, text: Any, *, chat: Sequence[int] | None = None, now: float | None = None) -> None:
    """Keep that Yuki typed ``text`` into a field of the top-level window ``window`` (a keyed digest of the text as
    compared, in RAM only), so a later run that finds exactly that text in a chat's composer knows it is Yuki's own
    draft and may replace it (:func:`typed_by_yuki`). ``chat``: the chat it was typed into (the route's capture thread
    ids) when the caller knows it: Discord, Slack and WhatsApp show every chat in one window (review L2). Called by this
    driver's own typing; every other way Yuki types (the voice hands' ``type``, the thinker's ``type_text``) may call it
    with the window alone. Never raises."""
    try:
        digest = _typed_digest(text)
        if not window or not digest:
            return
        where = _chat_key(chat)
        at = time.time() if now is None else float(now)
        with _typed_lock:
            kept = [(t, c, d) for t, c, d in _typed.get(int(window), [])
                    if 0.0 <= at - t <= TYPED_KEEP_S and (c, d) != (where, digest)]
            _typed[int(window)] = (kept + [(at, where, digest)])[-TYPED_PER_WINDOW:]
    except Exception:  # noqa: BLE001
        pass


def typed_by_yuki(window: int, held: Any, *, chat: Sequence[int] | None = None, now: float | None = None) -> bool:
    """Whether ``held`` (a composer's whole text) is exactly a text Yuki typed into the window ``window`` within
    :data:`TYPED_KEEP_S` (whitespace, zero-width characters and case folded): typed into this very ``chat`` when the
    driver noted the chat, or into the window by a caller that couldn't say which chat (review L2: the same words
    typed by Yuki in chat A are never Yuki's in chat B). Text the user typed, or Yuki's text with anything of theirs
    added, is not. Never raises."""
    try:
        digest = _typed_digest(held)
        if not window or not digest:
            return False
        where = _chat_key(chat)
        at = time.time() if now is None else float(now)
        with _typed_lock:
            return any(d == digest and c in (b"", where) and 0.0 <= at - t <= TYPED_KEEP_S
                       for t, c, d in _typed.get(int(window), []))
    except Exception:  # noqa: BLE001
        return False


def _composer_state(hands: Any, text: str, chat: Sequence[int] | None = None) -> tuple[str, int]:
    """The opened chat's composer against the words to type (review r1 L6, r2 H1), and its window: ``"empty"``
    (every read of it shows nothing but whitespace, zero-width characters and the BOM: Discord's empty message box
    holds "U+FEFF" and a newline, live test 17 Oct), ``"same_text"`` (every read that shows text is exactly ``text``,
    whitespace, zero-width characters and case folded), ``"yuki_text"`` (every such read is exactly a text Yuki typed
    there itself: :func:`typed_by_yuki`, fMsg), ``"other_text"`` (anything else: the user's kept draft) or
    ``"unread"``. A read that shows text always outweighs one that shows none (fMsg review M3: either may lag)."""
    from yuki.skills.donecheck import _norm_text

    reads, window = _composer_text(hands)
    if reads is None:
        return "unread", window
    shown = [r for r in reads if _norm_text(r)]
    if not shown:
        return "empty", window
    if text and all(_norm_text(r) == _norm_text(text) for r in shown):
        return "same_text", window
    if all(typed_by_yuki(window, r, chat=chat) for r in shown):
        return "yuki_text", window
    return "other_text", window


def _dispatched(facts: Mapping[str, Any], do: str) -> bool:
    """Whether a step of kind ``do`` ran in this run: its action went out (``result.ok``: dispatched), whatever its
    screen watch then saw (review r1 M1: a send key whose watch saw no change was still pressed)."""
    return any(getattr(r, "kind", "") == "action" and bool(getattr(r, "ok", False))
               and getattr(getattr(r, "step", None), "do", "") == do for r in facts.get("results") or [])


def _merged_runs(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The runs of one message (the select-all, the typing, the send key) as one: every step's result and count, and
    how the last run ended (held, Act off, taken over, refused)."""
    if not runs:
        return {"steps": 0, "steps_done": 0, "results": []}
    out = dict(runs[-1])
    out["steps"] = sum(int(r.get("steps") or 0) for r in runs)
    out["steps_done"] = sum(int(r.get("steps_done") or 0) for r in runs)
    out["results"] = [x for r in runs for x in r.get("results") or []]
    return out


#: After typing, how long the composer may take to read as exactly the typed words before a send key (s; fMsg review
#: M2: only a box that holds exactly the words, once, is sent).
VERIFY_S = 0.8


def _box_now(call: RouteCall, text: str, chat: Sequence[int]) -> str:
    """The composer right after typing, read every :data:`POLL_S` for at most :data:`VERIFY_S` until it holds exactly
    ``text`` (:func:`_composer_state`'s word for it: ``same_text``, else the last read's)."""
    deadline = call.ctx.clock() + VERIFY_S
    while True:
        state, _window = _composer_state(call.ctx.hands, text, chat)
        if state == "same_text" or call.ctx.clock() + POLL_S > deadline:
            return state
        call.ctx.sleep(POLL_S)


# ---------------------------------------------------------------------------------------------------------------------
# The quick switcher seen open before anything is typed into it (review r1 L1)
# ---------------------------------------------------------------------------------------------------------------------

#: How long the field with the keyboard may take to change after the switcher's shortcut (s; only while waiting).
SWITCHER_WAIT_S = 1.5
#: When no text field has the keyboard right after the app came forward, how long its own field may take to take it
#: before the shortcut is sent (s; review r2 L3: a composer focused late must not pass for the switcher opening).
SETTLE_S = 0.5


def _field(hands: Any) -> tuple[int, int, str, str] | None:
    """The text field with the keyboard as an identity (its window, native window, role and name: UI facts, never
    read for meaning), or ``None`` when no text field has it or it can't be read."""
    read = getattr(hands, "_focus", None)
    if not callable(read):
        return None
    try:
        snap = read(timeout_s=0.4)
    except Exception:  # noqa: BLE001
        return None
    if snap is None or not getattr(snap, "accepts_text", False) or getattr(snap, "is_password", False):
        return None
    return (int(getattr(snap, "window", 0) or 0), int(getattr(snap, "hwnd", 0) or 0),
            str(getattr(snap, "role", "") or ""), str(getattr(snap, "name", "") or ""))


def _field_opened(ctx: Any, hands: Any, before: tuple[int, int, str, str] | None
                  ) -> tuple[int, int, str, str] | None:
    """The other text field that took the keyboard after the shortcut (the switcher's or the search box's), read
    every :data:`POLL_S` for at most :data:`SWITCHER_WAIT_S`; ``None`` when none did."""
    deadline = ctx.clock() + SWITCHER_WAIT_S
    while True:
        now = _field(hands)
        if now is not None and now != before:
            return now
        if ctx.clock() + POLL_S > deadline:
            return None
        ctx.sleep(POLL_S)


def _field_changed(ctx: Any, hands: Any, before: tuple[int, int, str, str] | None) -> bool:
    """Whether another text field took the keyboard after the shortcut (:func:`_field_opened`)."""
    return _field_opened(ctx, hands, before) is not None


def _settled_field(ctx: Any, hands: Any) -> tuple[int, int, str, str] | None:
    """The text field with the keyboard once focus has settled: read every :data:`POLL_S` for at most
    :data:`SETTLE_S` until one has it (only while this skill waits), else ``None``."""
    deadline = ctx.clock() + SETTLE_S
    while True:
        now = _field(hands)
        if now is not None or ctx.clock() + POLL_S > deadline:
            return now
        ctx.sleep(POLL_S)


def _holder(hands: Any) -> Any:
    """chat19: what has the keyboard now, text field or not (the hands' focus read: a ``FocusSnapshot``), or ``None``
    when it can't be read. Read in RAM; only its control type is ever logged (:func:`_holder_facts`)."""
    read = getattr(hands, "_focus", None)
    if not callable(read):
        return None
    try:
        snap = read(timeout_s=0.4)
    except Exception:  # noqa: BLE001
        return None
    return snap if snap is not None and getattr(snap, "ok", True) else None


@dataclass(frozen=True)
class _Ident:
    """A field identity (:func:`_field`) read back as a snapshot's attributes, for :func:`_fits`."""

    window: int
    hwnd: int
    role: str
    name: str


def _ident(snap: Any) -> tuple[int, int, str, str] | None:
    """A focus read as an identity (its window, native window, role and name: UI facts, never read for meaning)."""
    if snap is None:
        return None
    return (int(getattr(snap, "window", 0) or 0), int(getattr(snap, "hwnd", 0) or 0),
            str(getattr(snap, "role", "") or ""), str(getattr(snap, "name", "") or ""))


def _close_key(data: Mapping[str, Any]) -> list[str]:
    """The app's own key that closes its quick switcher or search (``[chat] close``, data); ``[]`` when it has none."""
    return [str(k) for k in data.get("close") or [] if str(k).strip()]


def _close_own(call: RouteCall, hands: Any, data: Mapping[str, Any], *, apps: Sequence[str], key: str,
               field: Sequence[Any] | None = None) -> str:
    """chat19: the app's own close key (:func:`_close_key`) on its own run (each key judged by Yuki's own check and the
    game gate like the switcher's, into the app's own window only), for a switcher or search this very run opened and
    did not finish. A code word: ``sent`` (the key went out), ``not_sent`` (refused, held or stopped), ``no_key`` (the
    app's data has none: nothing is pressed).

    gb (B2, final-G2 R13; contract K-U, writer): ``field`` is what holds the keyboard by this run's own shortcut, as
    this run measured it (window, native window, role, name): the text field the shortcut gave the keyboard to
    (:func:`_field`), or the switcher's own element that takes no text and took it in that field's place
    (:func:`_tidy_switch`, fix-r33). With hands that take it (``hands.own_undo``, the voice hands), the close key is
    declared as the run's own undo of that holder: Yuki's check is told so only while its own focus read at that key
    shows that very holder still holding the keyboard (the name Yuki typed into the switcher's search is then this
    run's own act, never a message to send). ``None`` (no field was seen opening): nothing is declared and the check
    reads the key as before."""
    keys = _close_key(data)
    if not keys:
        return "no_key"
    declare = getattr(hands, "own_undo", None) if field is not None else None
    try:
        scope = declare(keys=keys, field=tuple(field)) if callable(declare) else nullcontext()
    except Exception:  # noqa: BLE001 - declared or not, the key is judged; never a run broken by it
        scope = nullcontext()
    with scope:
        _ok, facts = run_steps(hands, [{"do": "keys", "keys": keys}], words=call.ctx.words, apps=apps, key=key)
    return "sent" if _dispatched(facts, "keys") else "not_sent"


def _still_held(hands: Any, held_by: Sequence[Any] | None) -> bool | None:
    """gb (B3): whether what this run's shortcut opened (``held_by``: its identity, :func:`_ident`) still holds the
    keyboard now, by one focus read; ``None`` when that can't be told (nothing known to compare, or no read)."""
    if held_by is None:
        return None
    now = _ident(_holder(hands))
    return None if now is None else now == tuple(held_by)


def _mark_held(run: dict[str, Any], hands: Any, held_by: Sequence[Any] | None) -> None:
    """gb (B3, contract K-S, writer): ``run["switcher_held"]`` from :func:`_still_held` (left out when unknown)."""
    held = _still_held(hands, held_by)
    if held is not None:
        run["switcher_held"] = held


def _tidy_switch(call: RouteCall, hands: Any, data: Mapping[str, Any], run: dict[str, Any], *, apps: Sequence[str],
                 key: str, before: tuple[int, int, str, str] | None, opened: tuple[int, int, str, str] | None) -> None:
    """chat19 (all-messaging RC1: a Quick Switcher Ctrl+K opened and never closed held Discord's keyboard for every
    later draft, both brains): the shortcut went out but the switcher's run didn't finish, so what it opened is closed
    with the app's own close key, judged on its own run. Never when the user took over (their hands are on it) or a
    game stopped it (the gate would refuse the key anyway). With no text field seen taking the keyboard (``opened``
    None), the key goes only when the focus read shows something else holding the keyboard than before the shortcut
    (measured; a switcher whose own window, not a field, has the keyboard), or can't be read: never into an app where
    nothing changed. With the switcher's field seen (``opened``), the key goes only while the focus read shows that
    field, or something new that takes no text (the switcher's own window), still holding the keyboard, or can't be
    read (review L2): a switcher that already closed itself gave the keyboard back to where it was, or to another
    text field (the opened chat's composer, a message being edited), and a close key there would land in the app's
    own field. ``run["switcher_closed"]``: ``yes`` / ``no`` (still held by what the shortcut opened) / ``unknown``
    (the focus can't be read) / ``unchanged`` (nothing opened) / ``already`` (it closed itself: nothing pressed) /
    ``theirs`` / ``no_key`` / ``not_sent``.

    gb (B2): the close key goes as the run's own undo of what it opened (:func:`_close_own`): the field it opened
    (``opened``), or (fix-r33, final-K R13) the switcher's own element that takes no text, holding the keyboard in that
    field's place, new since the shortcut, while that field is still in the app's window. gb (B3, contract K-S):
    ``run["switcher_held"]`` (bool, measured by the focus read; left out when it can't be told, or when the user took
    over): what the shortcut opened still holds the keyboard as the run ends. After a close key it is told only of
    that same holder (with ``opened`` measured taking the keyboard after the shortcut), never of a holder merely read
    later with nothing seen opening (gb review F3: with the holder before the shortcut unread, that may be the app's
    own composer)."""
    if run.get("took_over_at"):
        run["switcher_closed"] = "theirs"
        return
    if _refused("keys"):
        run["switcher_closed"] = "not_sent"
        _mark_held(run, hands, opened)
        return
    snap = _holder(hands)
    now = _ident(snap)
    if opened is None and before is not None and now is not None and now == before:
        run["switcher_closed"] = "unchanged"
        run["switcher_held"] = False
        return
    if opened is not None and now is not None and now != tuple(opened) and (now == before or _is_field(snap)):
        run["switcher_closed"] = "already"
        run["switcher_held"] = False
        return
    # fix-r33 (final-K R13): with a name typed into it, the switcher's own element that takes no text can hold the
    # keyboard instead of the field its shortcut opened. Past the two checks above, such a holder (``now``) is neither
    # that field nor any text field (never the app's composer), and differs from what held the keyboard before the
    # shortcut (read, so measured new since it). While that field is still found in the app's window (the switcher
    # still open: one read-only look-up), that holder is what this run's close key undoes, and what "still held" is
    # told against. Gone, unknown or unread: the opened field, as before
    own = opened
    if opened is not None and before is not None and now is not None and now != tuple(opened) \
            and _shortcut_closed(data, tuple(opened), None) == "there":
        own = now
    held_by = own if own is not None else now
    sent = _close_own(call, hands, data, apps=apps, key=key, field=own)
    if sent != "sent":
        run["switcher_closed"] = sent
        # gb review F3: ``switcher_held`` only from what the shortcut was measured opening (``own``); with none seen,
        # the holder now may be the app's own composer (the holder before the shortcut unread): left out, unknown
        _mark_held(run, hands, own)
        return
    deadline = call.ctx.clock() + SWITCHER_WAIT_S
    while True:
        after = _ident(_holder(hands))
        if after is not None and after != held_by:
            run["switcher_closed"] = "yes"
            if opened is not None:
                run["switcher_held"] = False
            return
        if call.ctx.clock() + POLL_S > deadline:
            run["switcher_closed"] = "unknown" if after is None else "no"
            if after is not None and opened is not None:
                run["switcher_held"] = True   # measured: what it opened still holds the keyboard after its close key
            return
        call.ctx.sleep(POLL_S)


def _shortcut_closed(data: Mapping[str, Any] | None, before: tuple[int, int, str, str] | None,
                     held_before: tuple[int, int, str, str] | None) -> str:
    """Integration review F2: whether the switcher's shortcut closed what held the keyboard before it rather than
    opening something, from the app's own UI only (UIA role and name, never the user's words): what held it (``before``
    when it was a text field, else the focus read's holder) is looked up once in its own window (read only). A code
    word: ``gone`` (no longer there: the shortcut closed a switcher or dialog that was already open, and the field that
    took the keyboard is the app's own), ``there`` (still there: the shortcut opened something new), ``unknown`` (the
    look-up couldn't run, more than one control fits, or the holder has no role and name to look by), ``root`` (the
    holder is the window itself, which no shortcut closes) or ``unread`` (nothing was read before the shortcut)."""
    held = before if before is not None else held_before
    if held is None:
        return "unread"
    window, hwnd, role, name = held
    if not window and data is not None:
        window = _app_hwnd(data)
    if hwnd and window and int(hwnd) == int(window):
        return "root"
    if not window or not role or not name:
        return "unknown"
    try:
        from yuki.skills.uia_route import find_in

        status = str(find_in(int(window), role=role, name=name).status)
    except Exception:  # noqa: BLE001 - a look-up that can't run is no answer
        return "unknown"
    return "gone" if status == "missing" else "there" if status == "found" else "unknown"


def _run_switch(call: RouteCall, hands: Any, steps: list[dict[str, Any]], *, apps: Sequence[str], key: str,
                way_text: str, data: Mapping[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
    """The switcher's (or search's) steps, its shortcut first on its own: the name is typed and Enter pressed only once
    a different text field took the keyboard (the switcher opened). If it didn't, nothing more is sent (a name typed
    into the open chat's composer and Enter would send it): ``facts["switcher"] = "unseen"``. When no text field has
    the keyboard as the app comes forward, the read waits for focus to settle first (review r2 L3), so a composer that
    takes the keyboard late is the "before", never the switcher.

    chat19: a run whose shortcut went out but that did not finish (the switcher unseen, a step held, refused or stopped
    before the open key or the pick) closes what it opened (:func:`_tidy_switch`, the app's ``[chat] close`` from
    ``data``) and says so: ``facts["switcher"]`` ``unseen`` / ``unfinished``, ``facts["finished"]`` False. The text
    field the switcher gave the keyboard to is kept as ``facts["switcher_field"]`` (RAM: the caller compares later
    focus reads with it).

    Integration review F2: a shortcut that closed what held the keyboard (:func:`_shortcut_closed`) types nothing and
    presses nothing more: ``facts["switcher"]`` ``closed_own``, ``facts["switcher_closed"]`` ``by_shortcut`` (seen
    gone) or ``untold`` (a quick switcher in an app with no composer shapes, where it couldn't be told)."""
    before = _field(hands)
    if before is None:
        before = _settled_field(call.ctx, hands)
    held_before = _ident(_holder(hands))
    ok, first = run_steps(hands, steps[:1], words=call.ctx.words, apps=apps, key=key)
    first["steps"] = len(steps)
    first["finished"] = False
    if not _dispatched(first, "keys"):
        return False, first          # the shortcut never went out: nothing opened, nothing to close
    # the switcher's field, waited for even when the shortcut's own run stopped there (a dialog it brought up, no
    # change seen yet): what it opened is then known before it is closed
    opened = _field_opened(call.ctx, hands, before)
    if ok and opened is not None and data is not None and _fits(_Ident(*opened), composer_shapes(data)):
        # integration (skill19 review M2): the field that took the keyboard is a chat's composer, not the switcher
        # (a switcher already open closed on its own shortcut and gave the keyboard back): the chat's name typed and
        # Enter there would send it to whoever's chat is open. Nothing more is sent, nothing of this run's is open
        first["switcher"] = "composer"
        return False, first
    if not ok or opened is None:
        first["switcher"] = "unfinished" if not ok else "unseen"
        if data is not None:
            _tidy_switch(call, hands, data, first, apps=apps, key=key, before=held_before, opened=opened)
        return False, first
    # integration review F2: the composer shapes above exist only where the app's data has a composer label (none for
    # Slack or WhatsApp, none for Discord outside its listed languages), so the structure decides too: what held the
    # keyboard before the shortcut no longer there means the shortcut closed a switcher already open and the field
    # that took the keyboard is the app's own. A quick switcher (the toggling shortcut M2 is about) in an app with no
    # composer shapes also stops when that can't be told while a text field held the keyboard. Nothing more is sent
    # and nothing is tidied (it closed itself)
    shut = _shortcut_closed(data, before, held_before)
    if shut == "gone" or (shut == "unknown" and before is not None and data is not None and data.get("switcher")
                          and not composer_shapes(data)):
        first["switcher"] = "closed_own"
        first["switcher_closed"] = "by_shortcut" if shut == "gone" else "untold"
        return False, first
    ok, rest = run_steps(hands, steps[1:], words=call.ctx.words, apps=apps, key=key, way_text=way_text)
    merged = dict(rest)
    merged["steps"] = len(steps)
    merged["steps_done"] = int(first.get("steps_done") or 0) + int(rest.get("steps_done") or 0)
    merged["results"] = list(first.get("results") or []) + list(rest.get("results") or [])
    merged["switcher_field"] = opened
    merged["finished"] = bool(ok) and merged["steps_done"] >= len(steps)
    if not merged["finished"]:
        merged["switcher"] = "unfinished"
        if data is not None:
            _tidy_switch(call, hands, data, merged, apps=apps, key=key, before=held_before, opened=opened)
    return ok, merged


# ---------------------------------------------------------------------------------------------------------------------
# Is the chat open? (memory's encrypted thread ids, never a title)
# ---------------------------------------------------------------------------------------------------------------------


def _board_now(wait_s: float = 0.15) -> dict[str, Any] | None:
    """Memory's Now board (the watcher's latest reads), or ``None``."""
    try:
        from yuki.memory.now import shared_client

        client = shared_client()
        return client.now(wait_s=wait_s) if client is not None else None
    except Exception:  # noqa: BLE001
        return None


def _board_refresh(hwnd: int) -> bool:
    """Ask the watcher to read ``hwnd`` again now (returns at once)."""
    try:
        from yuki.memory.now import shared_client

        client = shared_client()
        return bool(client.refresh(int(hwnd))) if client is not None else False
    except Exception:  # noqa: BLE001
        return False


def _other_chat(store: Any, tid: int | None, ids: set[int], person_id: int | None) -> dict[str, Any]:
    """gb (B6, final-G2 R13's trigger lead: the deep link opened the right person's DM, yet the board's read named a
    thread outside the route's ids): content-free facts of that read. ``other_ids``: how many thread ids the route
    had; ``other_known``: whether memory has a thread for the window in front (a bool: gb review F4, a string here
    would be logged as its length); with the person's id in memory's people index, ``other_same_person``: whether that
    thread is on one of that same person's own routes (``contacts.contacts_of_threads``: identity), and
    ``other_holders``: how many of their people hold a route on it. Counts and booleans only. Never raises."""
    out: dict[str, Any] = {"other_ids": len(ids), "other_known": tid is not None}
    if tid is None or person_id is None:
        return out
    try:
        from yuki.memory import contacts as C

        holders = {int(c.id) for c, _r in C.contacts_of_threads(store, [int(tid)]).get(int(tid), [])}
        out["other_same_person"] = int(person_id) in holders
        out["other_holders"] = len(holders)
    except Exception:  # noqa: BLE001 - the counts alone
        pass
    return out


#: gb (B6): the content-free facts :func:`_other_chat` adds to an ``other_chat`` read, carried to the route's facts.
OTHER_CHAT_FACTS = ("other_ids", "other_known", "other_same_person", "other_holders")


def front_chat(store: Any, board: Mapping[str, Any] | None, *, spoke: int | None = None,
               brought: Sequence[int] = ()) -> dict[str, Any] | None:
    """lb B1 (final-H2 kw_name_word_in_text: "type bro I'm on my way in this chat" had no path; contract K-F, writer):
    the chat in front, measured as :func:`chat_state` measures an opened chat, never by a title: memory's Now board's
    window in front, a chat app's own program (its ``[chat]`` data), read and not stale since, not blocked, with a chat
    scope or an address; its capture thread (``Store.find_thread``: a keyed digest of the app and the chat's scope);
    and on that thread exactly one person's own direct message (``contacts.contacts_of_threads``: one person, every
    route there a ``dm``: never a group chat or channel, never a DM memory linked to two people), in an app the routes
    drive. ``spoke`` / ``brought`` (the engine's :func:`yuki.skills.engine._front_spoken`, from the run's hands): the
    window in front when they spoke, when an act of Yuki's moved the focus since (the window must be that one), and
    windows Yuki's own steps brought forward (never one of those): "the chat that was in front when they spoke".
    ``{"id", "name", "thread", "app", "chat", "handle", "hwnd"}`` (RAM only: names stay here, never logged), else
    ``None``. Never raises."""
    try:
        if store is None or not isinstance(board, Mapping):
            return None
        windows = list(board.get("windows") or [])
        front = next((w for w in windows if isinstance(w, Mapping) and w.get("foreground")), None)
        if front is None or front.get("blocked") or front.get("stale") or not front.get("read_at"):
            return None
        if not (front.get("scope") or front.get("url")):
            return None                                     # a title alone is never an identity
        hwnd = int(front.get("hwnd") or 0)
        if not hwnd or (spoke and hwnd != int(spoke)) or hwnd in {int(h) for h in brought or () if h}:
            return None
        proc = _app_key(front.get("process"))
        app = next((a for a in chat_apps() if proc and proc in {_app_key(p) for p in app_data(a).get("processes")
                                                                or []}), "")
        if not app:
            return None
        tid = store.find_thread(front.get("app"), front.get("title"), front.get("url"), process=front.get("process"),
                                scope=front.get("scope"))
        if tid is None:
            return None
        from yuki.memory import contacts as C

        hits = C.contacts_of_threads(store, [int(tid)]).get(int(tid), [])
        if not hits or any(getattr(r, "kind", "") != PERSON_KIND for _c, r in hits):
            return None
        people = {int(c.id): (c, r) for c, r in hits}
        if len(people) != 1:
            return None
        contact, route = next(iter(people.values()))
        own = _chat_route(store, route)
        if own.app != app:
            return None
        return {"id": int(contact.id), "name": _words(contact.name), "thread": int(tid), "app": app,
                "chat": _words(route.chat), "handle": _words(route.handle), "hwnd": hwnd}
    except Exception:  # noqa: BLE001 - not measured: nobody in front
        return None


def _front_shown(target: Target, front: Mapping[str, Any] | None) -> str:
    """lb B1 (K-F): how the done lines name a person taken from the chat in front: their name and that chat's own name
    as memory keeps it (what the window shows: "Hemanth (Quasar40404)"), so the person and the handle on screen are
    tied; the name alone when the chat's name is theirs. Never raises."""
    try:
        name = _words(target.name)
        shown = _words((front or {}).get("chat")) or _words((front or {}).get("handle"))
        if not shown or not name or shown.casefold().lstrip("@") in name.casefold() \
                or name.casefold() in shown.casefold():
            return name
        return f"{name} ({shown})"
    except Exception:  # noqa: BLE001
        return _words(getattr(target, "name", ""))


def chat_state(store: Any, thread_ids: Sequence[int], data: Mapping[str, Any], since: float,
               board: Mapping[str, Any] | None, *, person_id: int | None = None) -> tuple[bool | None, dict[str, Any]]:
    """Whether the chat with these capture threads is open now: memory's read of the window in front (read after
    ``since``, with a chat scope or an address) is one of them -> True; another chat or view of the same app in front
    -> False; else one of the threads read since -> True; else None. Identity by keyed digests of the chat's scope or
    address (``Store.find_thread``), never a window title. gb (B6): an ``other_chat`` read carries
    :func:`_other_chat`'s content-free facts (``person_id``: the person the route is for, when one is)."""
    ids = {int(t) for t in thread_ids or []}
    if not ids:
        return None, {"status": "no_threads"}
    keys = {_app_key(p) for p in data.get("processes") or []}
    ev: dict[str, Any] = {}
    windows = list((board or {}).get("windows") or [])
    front = next((w for w in windows if w.get("foreground")), None)
    if front is not None:
        proc = _app_key(front.get("process"))
        fresh = bool(front.get("read_at")) and float(front["read_at"]) >= float(since)
        if proc not in keys:
            ev["board"] = "other_app"
        elif front.get("blocked"):
            ev["board"] = "blocked"
        elif not fresh:
            ev["board"] = "stale"
        elif not (front.get("scope") or front.get("url")):
            ev["board"] = "no_identity"           # a title alone is never an identity
        else:
            try:
                tid = store.find_thread(front.get("app"), front.get("title"), front.get("url"),
                                        process=front.get("process"), scope=front.get("scope"))
            except Exception:  # noqa: BLE001
                tid = None
            if tid is not None and int(tid) in ids:
                return True, {"by": "board"}
            return False, {"by": "board", "status": "other_chat", **_other_chat(store, tid, ids, person_id)}
    else:
        ev["board"] = "none"
    marks = ",".join("?" * len(ids))
    try:
        rows = store._query(f"SELECT max(last_seen), max(messages_seen_at) FROM threads WHERE id IN ({marks})",
                            sorted(ids))
        seen = max((float(x) for x in (rows[0] if rows else ()) if x is not None), default=0.0)
    except Exception:  # noqa: BLE001
        seen = 0.0
    if seen >= float(since):
        return True, {"by": "threads"}
    return None, ev


def _wait_chat(call: RouteCall, data: Mapping[str, Any], route: ChatRoute, since: float, hwnd: int,
               person_id: int | None = None) -> tuple[bool | None, dict[str, Any]]:
    """:func:`chat_state` read every :data:`POLL_S` for at most :data:`CHAT_WAIT_S` (only while this skill waits),
    asking the watcher once to read the window again. Ends at the first "open"; else the last read's word (a "no" only
    if the window still shows another chat at the end). ``person_id``: gb (B6), for the ``other_chat`` read's facts."""
    from yuki.skills.engine import _mem, _store_of

    ctx = call.ctx
    if ctx.memory is None:
        return None, {"status": "no_memory"}
    deadline = ctx.clock() + CHAT_WAIT_S
    refreshed = False
    while True:
        board = _board_now()
        got = _mem(ctx.memory, "chat_state",
                   lambda c: chat_state(_store_of(c), route.thread_ids, data, since, board, person_id=person_id),
                   timeout_s=0.8)
        verdict, ev = got if isinstance(got, tuple) else (None, {"status": "unread"})
        if verdict is True:
            return verdict, ev
        # "another chat" counts only as the last word: a read taken just before the app switched shows the old one
        if not refreshed and hwnd:
            _board_refresh(hwnd)
            refreshed = True
        if ctx.clock() + POLL_S > deadline:
            return verdict, ev
        ctx.sleep(POLL_S)


# ---------------------------------------------------------------------------------------------------------------------
# Desktop plumbing (each guarded; replaced in tests)
# ---------------------------------------------------------------------------------------------------------------------


def _refused(action: str, hwnd: int | None = None) -> str | None:
    """D17's backstop asked here, before a focus, an opened link or keys (the input layer asks again): ``None`` (go
    ahead), else the refusal's kind (fHands, ``yuki.actions.gate``): ``"game"`` (held for a game) or ``"blocked"``
    (the gate failed closed, or another guard refused it: a settled stop, never said as a game)."""
    from yuki.actions import gate

    why = gate.refuse(action, hwnd=hwnd)
    return None if why is None else gate.kind_of(why)


def _app_hwnd(data: Mapping[str, Any]) -> int:
    from yuki.skills.uia_route import app_window

    for image in data.get("images") or []:
        hwnd = app_window(str(image))
        if hwnd:
            return int(hwnd)
    return 0


def _focus(hwnd: int) -> Any:
    from yuki.actions import launch

    return launch.focus_window(int(hwnd))


#: How long opening a chat's own link may wait for the app's window to change and come forward (s) when the app is
#: already running (fMsg, live 18 Oct: the generic open waited for a "document" in Discord's window too, up to 6 s
#: more; the route checks the chat open by memory's thread ids itself, :func:`_wait_chat`).
LINK_OPEN_S = 2.0


def _open_uri(uri: str, *, running: bool = False) -> Any:
    """The app's own link, opened with its default handler (D17-guarded). With the app already ``running`` only its
    window is waited for, within :data:`LINK_OPEN_S` (no page or document wait: the chat is checked open by memory's
    thread ids right after); a cold start keeps the full wait."""
    if running:
        from yuki.actions import launch

        return launch.open_url(str(uri or ""), content_timeout_s=0.0, deadline=time.monotonic() + LINK_OPEN_S)
    from yuki.skills.engine import open_uri

    return open_uri(uri)


def _place(ctx: Any, hwnd: int, *, activate: bool) -> bool:
    """P1.4: the app's window where the run's place hint says (``engine.place_for``; nothing without a hint). Never
    types, never sends: every send safety after it is unchanged."""
    from yuki.skills.engine import place_for

    return place_for(ctx, hwnd, activate=activate)


def _held_by_game(result: Any) -> str | None:
    """The refusal kind an action's result carries (fHands, ``yuki.actions.gate.carried``): ``"game"`` or
    ``"blocked"``, else ``None``."""
    details = getattr(result, "details", None) or {}
    value = details.get("refused") if isinstance(details, Mapping) else None
    return value if value in ("game", "blocked") else None


def _mic_held(image: str) -> bool | None:
    from yuki.skills.donecheck import baseline_mic_held

    return baseline_mic_held({"process": image})


def _press(hwnd: int, role: str, name: str) -> str:
    from yuki.skills.uia_route import press

    return press(int(hwnd), role=role, name=name, pattern="invoke").outcome


def _focus_app(data: Mapping[str, Any], ctx: Any = None) -> tuple[int, RouteResult | None]:
    """The app's own window, brought forward (guarded): ``(hwnd, None)`` or ``(0, a result: not open, refused...)``.
    P1.4: with the run's place hint (``ctx.place``) it is put on that screen first, so it comes forward there."""
    hwnd = _app_hwnd(data)
    if not hwnd:
        return 0, RouteResult(ran=False, outcome="not_open")
    if kind := _refused("focus", hwnd=hwnd):
        return 0, RouteResult(ran=False, held=kind, outcome="refused")
    _place(ctx, hwnd, activate=False)
    result = _focus(hwnd)
    if kind := _held_by_game(result):
        return 0, RouteResult(ran=False, held=kind, outcome="refused")
    if not getattr(result, "ok", False):
        return 0, RouteResult(ran=False, outcome="no_focus")
    return hwnd, None


def _minimised(hwnd: int) -> bool:
    """fix-r31 (A1): whether the window is minimised now (``IsIconic``; read only). ``False`` when it can't be read."""
    try:
        import win32gui

        return bool(hwnd) and bool(win32gui.IsWindow(int(hwnd))) and bool(win32gui.IsIconic(int(hwnd)))
    except Exception:  # noqa: BLE001
        return False


def _app_shown(data: Mapping[str, Any], hwnd: int) -> str:
    """fix-r31 (A1): where the app's own window is now, measured: ``minimised``, ``front`` (the window in front is one
    of the app's own programs: its data's ``images``), ``behind``, or ``unread``. Read only; never raises."""
    if hwnd and _minimised(hwnd):
        return "minimised"
    try:
        import win32gui
        import win32process

        from yuki.connect.spotify_desktop import _image_of

        front = int(win32gui.GetForegroundWindow() or 0)
        if not front:
            return "behind"
        if hwnd and front == int(hwnd):
            return "front"
        image = _image_of(int(win32process.GetWindowThreadProcessId(front)[1])).casefold()
        images = {str(i).casefold() for i in data.get("images") or [] if str(i).strip()}
        return "front" if image and image in images else "behind"
    except Exception:  # noqa: BLE001
        return "unread"


def _bring_once(call: RouteCall, data: Mapping[str, Any], hwnd: int) -> tuple[str, str | None]:
    """fix-r31 (A1): after the link, the app's own window brought forward once when it is measured minimised or behind
    (:func:`_app_shown`), through :func:`_focus_app` (guarded: D17; the place hint honoured). ``(code word, held)``:
    ``forward`` (it came forward), ``front`` (already there: nothing done), ``unread``, or the refusal's outcome
    (``refused`` with ``held`` the gate's kind, ``not_open``, ``no_focus``). Content-free."""
    where = _app_shown(data, hwnd)
    if where in ("front", "unread"):
        return where, None
    got, stop = _focus_app(data, call.ctx)
    if stop is not None:
        return str(stop.outcome or "no_focus"), stop.held
    return ("forward" if got else "no_focus"), None


# ---------------------------------------------------------------------------------------------------------------------
# Y3: a text field a request's own key step brought up, left holding the keyboard (contract K-S, widened)
# ---------------------------------------------------------------------------------------------------------------------


def _is_composer(snap: Any) -> bool:
    """fix-r31 (Y3): whether a focus read fits any chat app's composer by that app's own label data
    (:func:`composer_shapes`): an Escape there may change the chat (close it, mark it read), so it is never pressed
    there. Data only; never raises."""
    try:
        return any(_fits(snap, shapes) for shapes in (composer_shapes(app_data(a)) for a in chat_apps()) if shapes)
    except Exception:  # noqa: BLE001 - unknown: as a composer (no key)
        return True


def holder_ident(hands: Any) -> tuple[int, int, str, str] | None:
    """fix-r31 (Y3): what holds the keyboard now as an identity (its top-level window, native window, role and name:
    UI facts, never read for meaning), by one focus read through ``hands._focus``; ``None`` when unread. RAM only."""
    return _ident(_holder(hands))


def raised_field(hands: Any, window: int, before: Sequence[Any] | None = None) -> tuple[int, int, str, str] | None:
    """fix-r31 (Y3, final-I R13: the planner's own Ctrl+K left a quick switcher holding a chat app's keyboard when its
    request ended): the text field a key step of the request just gave the keyboard to, as an identity, when it is one
    that step brought up: a text field (never a password field), in that very ``window`` (the step's own), other than
    ``before`` (what held the keyboard before the step, when read), and never a chat app's composer
    (:func:`_is_composer`). ``None`` otherwise, or when the focus can't be read. Read only; RAM only."""
    snap = _holder(hands)
    if not _is_field(snap) or not window:
        return None
    ident = _ident(snap)
    if ident is None or int(ident[0]) != int(window):
        return None
    if before is not None and tuple(before) == ident:
        return None
    if _is_composer(snap):
        return None
    return ident


def still_held(hands: Any, held: Sequence[Any] | None) -> bool | None:
    """fix-r31 (Y3): :func:`_still_held` (gb B3's K-S read) for a field a request's own key step brought up."""
    return _still_held(hands, held)


# ---------------------------------------------------------------------------------------------------------------------
# message
# ---------------------------------------------------------------------------------------------------------------------


def _text_mode(values: Mapping[str, Any]) -> tuple[str, bool]:
    text = _words(values.get("text"))
    return text, bool(text) and str(values.get("send") or "") == "send"


def _where(values: Mapping[str, Any], app: str) -> str:
    return "" if _app_named(values) else f" on {_title(app)}"


def _send_decl(call: RouteCall, target: Target, route: ChatRoute, text: str) -> tuple[dict[str, Any], Any]:
    """What a send declares to the send check, and the grant that may cover it: ``({"to", "what", "person_id",
    "kind"}, grant)``. Item 11's L2: the send also says whom by the person's id, so the brain's consent grant for this
    step (the engine's validated ``ctx.grant``; none on the voice or the old loop) covers exactly its text to exactly
    its person, though Yuki worded the text or speech wrote the name another way. Only into that person's own direct
    message (the route's kind as memory keeps it): a group chat or channel of theirs is read by other people, so there
    the send carries no person id and needs the user's own words (review r1 H1); never a person found by sound."""
    own_dm = not target.place and target.id is not None and route.kind == "dm" and not route.shared \
        and not target.by_sound
    sends = {"to": _said_as(call, target), "what": text, "person_id": target.id if own_dm else None,
             "kind": route.kind}
    grant = getattr(call.ctx, "grant", None) if getattr(call.ctx, "origin", "") == "thinker" else None
    return sends, grant


def _send_gate(call: RouteCall, target: Target, route: ChatRoute) -> tuple[bool, RouteResult | None]:
    """Before the chat is opened, a send meets the very check its steps would meet (``uia_route.check_sends`` on the
    same declaration, words and grant: whom and what in the user's words, or the brain's grant). ``(as_draft,
    refusal)``: ``(False, None)`` it may go (or it is no send); ``(True, None)`` it would be refused, but its person is
    the one the sound-alike question asked about and the user answered since (:func:`_answered_alike`): typed and left
    as a draft (review r1 H1: never sent by sound, confirmed or not); ``(False, result)`` refused, said before anything
    takes focus (review r1 L7)."""
    from yuki.skills.uia_route import check_sends

    text, send = _text_mode(call.values)
    if not send:
        return False, None
    sends, grant = _send_decl(call, target, route, text)
    args: dict[str, Any] = {"sends": True, "to": str(sends.get("to") or ""), "what": str(sends.get("what") or "")}
    for name in ("person_id", "kind"):
        if sends.get(name) is not None:
            args[name] = sends[name]
    if check_sends(args, [str(w) for w in call.ctx.words or () if w], grant=grant) is None:
        return False, None
    if _answered_alike(call, target):
        return True, None
    return False, RouteResult(ran=False, ask=LINE_SEND_HELD.format(name=target.name), outcome="send_refused",
                              facts={"mode": "send", "held_at": "send_check"})


# ---------------------------------------------------------------------------------------------------------------------
# chat19: the keyboard is put in the opened chat's composer before anything is typed (all-messaging RC1, one-desk D2:
# every Discord draft of the final test failed with the keyboard held by a Quick Switcher; the type step only finds a
# text field that already has the keyboard, and its first-step read waits 0.4 s)
# ---------------------------------------------------------------------------------------------------------------------


def composer_labels(data: Mapping[str, Any], route: ChatRoute) -> list[tuple[str, str]]:
    """The opened chat's composer as the app labels it, per display language: ``[chat.labels.<lang>] composer = [role,
    template]`` (Discord: ``["Edit", "Message {chat}"]``), the template's ``{chat}`` being the chat as the app's quick
    switcher names it (its kind's filter character and its name as memory keeps it: :func:`switcher_query`, "@name" for
    a DM). ``(role, name)`` pairs, data only; ``[]`` when the app's data has none or memory keeps no name."""
    if not _words(route.chat):
        return []
    labels = data.get("labels") if isinstance(data.get("labels"), Mapping) else {}
    chat = switcher_query(data, route)
    out: list[tuple[str, str]] = []
    for lang in labels.values():
        spec = lang.get("composer") if isinstance(lang, Mapping) else None
        if not isinstance(spec, (list, tuple)) or len(spec) != 2:
            continue
        role, template = str(spec[0] or "").strip(), str(spec[1] or "")
        if not role or "{chat}" not in template:
            continue
        pair = (role, _words(template.replace("{chat}", chat)))
        if pair not in out:
            out.append(pair)
    return out


def composer_shapes(data: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """The composer label templates' shapes: ``(role, the words before {chat}, the words after)`` (Discord's
    ``("Edit", "Message ", "")``), data only. A field the app itself focused whose name fits one is the composer even
    when memory keeps the chat under another name than the app shows; a quick switcher's or a search box's field never
    fits."""
    labels = data.get("labels") if isinstance(data.get("labels"), Mapping) else {}
    out: list[tuple[str, str, str]] = []
    for lang in labels.values():
        spec = lang.get("composer") if isinstance(lang, Mapping) else None
        if not isinstance(spec, (list, tuple)) or len(spec) != 2:
            continue
        role, template = str(spec[0] or "").strip(), str(spec[1] or "")
        head, sep, tail = template.partition("{chat}")
        if role and sep and (head.strip() or tail.strip()) and (role, head, tail) not in out:
            out.append((role, head, tail))
    return out


def _is_field(snap: Any) -> bool:
    return snap is not None and bool(getattr(snap, "accepts_text", False)) and not getattr(snap, "is_password", False)


def _labelled(snap: Any, labels: Sequence[tuple[str, str]]) -> bool:
    role, name = str(getattr(snap, "role", "") or "").casefold(), _words(getattr(snap, "name", "")).casefold()
    return any(role == r.casefold() and name == n.casefold() for r, n in labels)


def _fits(snap: Any, shapes: Sequence[tuple[str, str, str]]) -> bool:
    role = str(getattr(snap, "role", "") or "").casefold()
    name = " ".join(str(getattr(snap, "name", "") or "").split()).casefold()
    return any(role == r.casefold() and len(name) > len(h) + len(t) and name.startswith(h.casefold())
               and name.endswith(t.casefold()) for r, h, t in shapes)


def _in_composer(snap: Any, hwnd: int, labels: Sequence[tuple[str, str]], own_field: Any = None,
                 shapes: Sequence[tuple[str, str, str]] = ()) -> bool:
    """Whether the keyboard is in the opened chat's composer: a text field (never a password field) in the app's own
    window ``hwnd``, not the field this very run's switcher gave it to (``own_field``: still open), and, with
    ``labels``, the control the app's data labels as the composer; else, with ``shapes``, one whose name fits the
    label's template (:func:`composer_shapes`); with neither (an app with no label data), the field itself."""
    if not _is_field(snap):
        return False
    if hwnd and int(getattr(snap, "window", 0) or 0) != int(hwnd):
        return False
    if own_field is not None and _ident(snap) == tuple(own_field):
        return False
    if labels:
        return _labelled(snap, labels)
    return not shapes or _fits(snap, shapes)


def _await_composer(ctx: Any, hands: Any, hwnd: int, labels: Sequence[tuple[str, str]], own_field: Any,
                    wait_s: float, shapes: Sequence[tuple[str, str, str]] = ()) -> Any:
    """The focus read every :data:`POLL_S` for at most ``wait_s`` until the keyboard is in the composer
    (:func:`_in_composer`): that read, else ``None``."""
    deadline = ctx.clock() + max(0.0, float(wait_s))
    while True:
        snap = _holder(hands)
        if _in_composer(snap, hwnd, labels, own_field, shapes):
            return snap
        if ctx.clock() + POLL_S > deadline:
            return None
        ctx.sleep(POLL_S)


def _focus_control(hwnd: int, role: str, name: str) -> str:
    """The control's own UIA ``SetFocus`` (``uia_route.press`` pattern ``focus``: found by role and exact name in the
    app's window, never two; the game gate asked as for any focus). Its outcome code word (``pressed``, ``missing``,
    ``ambiguous``, ``refused``, ...)."""
    from yuki.skills.uia_route import press

    try:
        return str(press(int(hwnd), role=role, name=name, pattern="focus").outcome)
    except Exception:  # noqa: BLE001
        return "error"


def _act_off(hands: Any, image: str) -> str | None:
    act_off = getattr(hands, "act_off", None)
    if not callable(act_off):
        return None
    try:
        return str(act_off(image) or "") or None
    except Exception:  # noqa: BLE001
        return None


@dataclass
class _Keyboard:
    """Where the keyboard went before the typing (:func:`_composer_focus`): ``ok`` in the composer; ``how`` a code word;
    ``holder`` the last focus read (RAM); ``stop`` a result that ends the route now (Act off, a game)."""

    ok: bool
    how: str
    holder: Any = None
    stop: RouteResult | None = None


def _put_in_composer(call: RouteCall, hands: Any, hwnd: int, labels: Sequence[tuple[str, str]],
                     own_field: Any, shapes: Sequence[tuple[str, str, str]] = ()) -> tuple[str, Any]:
    """One try: the composer found by the app's own label (:func:`composer_labels`), focused through UIA
    (:func:`_focus_control`) and confirmed by the focus read within ``uia_route.FOCUS_WAIT_S`` (never the step
    runner's 0.4 s first-step read); with no label in the app's data, or that label nowhere in its window, the text
    field the app itself gives the keyboard as the chat opens (structure), waited for as long, and only one whose name
    fits the label's template when the app has one (``shapes``: never a switcher's or a search box's field). ``(how,
    focus read or None)``."""
    from yuki.skills.uia_route import FOCUS_WAIT_S

    for role, name in labels:
        outcome = _focus_control(hwnd, role, name)
        if outcome == "pressed":
            got = _await_composer(call.ctx, hands, hwnd, labels, own_field, FOCUS_WAIT_S)
            return ("focused", got) if got is not None else ("not_taken", None)
        if outcome == "refused":
            return "refused", None
    got = _await_composer(call.ctx, hands, hwnd, [], own_field, FOCUS_WAIT_S, shapes)
    if got is not None:
        return "settled", got
    return ("no_composer" if labels else "no_field"), None


def _composer_focus(call: RouteCall, app: str, data: Mapping[str, Any], route: ChatRoute, hwnd: int, *,
                    own_field: Any = None) -> _Keyboard:
    """chat19: before anything is typed, the keyboard goes into the opened chat's composer instead of being hoped to be
    there: already there (``in_composer``), or focused by its label through UIA (``focused``), or the app's own field
    (``settled``), confirmed by the focus read. The rows a focus meets are asked first, as for the app's window
    (:func:`_focus_app`): the app's Act switch (told, nothing done) and the game gate (held). Something else holding
    the keyboard (a dialog) is closed only when this very run opened it: the switcher whose field still has the
    keyboard (``own_field``), with the app's own close key on its own judged run (``closed_own``); anything else is
    left as it is and reported as a code fact by the caller (:func:`_not_typed`), since it may be the user's."""
    hands = call.ctx.hands
    labels = composer_labels(data, route)
    shapes = composer_shapes(data)
    snap = _holder(hands)
    if _in_composer(snap, hwnd, labels, own_field, shapes):
        return _Keyboard(True, "in_composer", snap)
    image = str((data.get("images") or [""])[0])
    off = _act_off(hands, image)
    if off:
        return _Keyboard(False, "act_off", snap, stop=RouteResult(ran=True, ask=LINE_ACT_OFF.format(app=off),
                                                                  outcome="act_off", app=app))
    if not hwnd:
        from yuki.skills.uia_route import FOCUS_WAIT_S

        got = _await_composer(call.ctx, hands, 0, [], own_field, FOCUS_WAIT_S, shapes)
        return _Keyboard(got is not None, "settled" if got is not None else "no_window", got or _holder(hands))
    if kind := _refused("focus", hwnd=hwnd):
        return _Keyboard(False, "refused", snap, stop=RouteResult(ran=True, held=kind, outcome="refused", app=app))
    closed = False
    while True:
        how, got = _put_in_composer(call, hands, hwnd, labels, own_field, shapes)
        if got is not None:
            return _Keyboard(True, "closed_own" if closed else how, got)
        holder = _holder(hands)
        if how == "refused" and (kind := _refused("focus", hwnd=hwnd)):
            return _Keyboard(False, "refused", holder, stop=RouteResult(ran=True, held=kind, outcome="refused",
                                                                        app=app))
        if closed or own_field is None or _ident(holder) != tuple(own_field):
            return _Keyboard(False, how, holder)
        # the switcher this very run opened still holds the keyboard (its open key went and the chat showed, but it
        # stayed): closed with the app's own close key (gb B2: as the run's own undo of that field), then the composer
        # once more
        if _close_own(call, hands, data, apps=list(data.get("images") or []), key="message",
                      field=own_field) != "sent":
            return _Keyboard(False, "own_left", holder)
        closed = True


def _holder_facts(snap: Any, hwnd: int) -> tuple[dict[str, Any], str]:
    """What has the keyboard, as content-free facts (its control type, a code word; whether it is in the app's window;
    whether it takes text) and, for RAM only, its role and name as the caller may read them (``Window "Quick
    Switcher"``: a name can be the user's content, so it never reaches a log)."""
    if snap is None:
        return {"holder": "unread"}, ""
    role = str(getattr(snap, "role", "") or "")
    window = int(getattr(snap, "window", 0) or 0)
    facts = {"holder": role if role.isalnum() else ("none" if not role else "other"),
             "holder_in_app": bool(hwnd) and window == int(hwnd), "holder_takes_text": _is_field(snap)}
    if not role:
        return facts, "nothing"
    return facts, f'{role} "{_words(getattr(snap, "name", ""))[:60]}"'


def _not_typed(call: RouteCall, app: str, name: str, hwnd: int, facts: Mapping[str, Any], ram: Mapping[str, Any],
               holder: Any = None, *, read: bool = True) -> RouteResult:
    """chat19 (contract C3): the chat is open and nothing could be typed. Never a question (``LINE_NOT_TYPED`` as a
    final ask ended the turn and stopped the caller's own hands from finishing it): an acted, failed result with code
    facts ``{"status": "failed", "why": "not_typed", "holder": <control type of what has the keyboard>}`` and, in RAM,
    ``facts_ram["holder"]`` (its role and name), so the engine passes it on as unverified for the next rung or the
    caller's own hands to take on. ``LINE_NOT_TYPED`` stays its fallback line. ``holder``: the focus read already
    taken, else read now (``read``)."""
    snap = holder if holder is not None or not read else _holder(call.ctx.hands)
    held, shown = _holder_facts(snap, hwnd)
    return RouteResult(ran=True, outcome="failed", app=app, unverified_line=LINE_NOT_TYPED.format(name=name),
                       facts={**facts, "typed": "no", "pressed": False, "status": "failed", "why": "not_typed",
                              **held},
                       facts_ram={**ram, "typed": False, "pressed": False, "holder": shown})


def _after_open(call: RouteCall, app: str, data: Mapping[str, Any], target: Target, route: ChatRoute, *, via: str,
                since: float, hwnd: int, as_draft: bool = False, own_field: Any = None, bring_once: bool = False,
                restored: bool = False) -> RouteResult:
    """After the chat was asked to open: wait until it is seen open, then (only then) put the keyboard in its composer
    (chat19, :func:`_composer_focus`), type the user's words, and press send when they plainly said so. The route's
    result: acted, with its RAM facts for the done-check. ``as_draft`` (:func:`_send_gate`): a send to a person matched
    by sound, answered since: typed and left, said so. ``own_field``: the text field this very run's switcher gave the
    keyboard to (its own to close if it still holds it). Nothing typed: :func:`_not_typed` (contract C3), never a
    question.

    fix-r31 (A1): ``bring_once`` (the link route): when the wait ends with another app's window in front while the
    app's own window is minimised or behind (measured), that window is brought forward once (:func:`_focus_app`,
    guarded) and the chat waited for once more. ``restored``: the route restored the app's minimised window before
    its link (a content-free fact). A2: on the link route (``via == "link"``), a chat never confirmed open ends with
    nothing typed and nothing pressed, said as the code fact :data:`yuki.skills.engine.NOTHING_OUT`; the switcher
    route already typed into the switcher, so its hand-off stays an ordinary one."""
    text, send = _text_mode(call.values)
    send = send and not as_draft
    name = target.name
    front = _front_of(call)
    if front is not None:
        # lb B1 (K-F): a person taken from the chat in front is named with that chat's own name ("your chat with
        # Hemanth (Quasar40404)"), so the person and the handle on screen are tied and a wrong one shows
        name = _front_shown(target, front)
    where = _where(call.values, app)
    person_id = getattr(target, "id", None) if not getattr(target, "place", False) else None
    chat, ev = _wait_chat(call, data, route, since, hwnd, person_id=person_id)
    brought = ""
    if bring_once and chat is not True and ev.get("board") == "other_app":
        # fix-r31 (A1): the link took, but the app's window never came in front (minimised, or behind another app's):
        # brought forward once, and the chat waited for once more. A game in front refuses it (D17)
        brought, held = _bring_once(call, data, hwnd)
        if held:
            return RouteResult(ran=True, held=held, outcome="refused", app=app,
                               facts={"via": via, "brought": brought, **({"restored": True} if restored else {})},
                               facts_ram={"app": app, "thread_ids": list(route.thread_ids), "chat": None,
                                          "typed": False, "pressed": False})
        if brought == "forward":
            hwnd = _app_hwnd(data) or hwnd
            chat, ev = _wait_chat(call, data, route, since, hwnd, person_id=person_id)
    facts: dict[str, Any] = {"via": via, "chat": {True: "open", False: "other", None: "unknown"}[chat],
                             "by": str(ev.get("by") or ev.get("board") or ev.get("status") or ""),
                             "mode": "send" if send else ("draft" if text else "open")}
    if restored:
        facts["restored"] = True
    if brought:
        facts["brought"] = brought
    if chat is False:
        # gb (B6, final-G2 R13's trigger lead): what the "another chat" read was, content-free (counts, booleans and a
        # code word), for the route's logged facts
        facts.update({k: ev[k] for k in OTHER_CHAT_FACTS if k in ev})
    if as_draft:
        facts["as_draft"] = "sound_alike"
    ram: dict[str, Any] = {"app": app, "thread_ids": list(route.thread_ids), "chat": chat}
    # fix-r31 (A2): the link route's chat never confirmed open: no typing step ran and no key was pressed anywhere (the
    # link opens by the shell; A1's restore and bring-forward only focus), the code fact the engine reads
    # (``NOTHING_OUT``), so a hand-off says "run it once more", never "type it by steps". Never the switcher route's:
    # it already pressed the switcher's shortcut, typed the chat's name and pressed Enter (an ordinary hand-off)
    nothing_out = chat is not True and via == "link"
    if nothing_out:
        facts.update({"typed": "no", "pressed": False, _NOTHING_OUT: True})
    if not text:
        return RouteResult(ran=True, outcome="opened", app=app, say=SAY_OPENED.format(name=name, where=where),
                           unverified_line=LINE_OPEN_UNCONFIRMED.format(app=_title(app), name=name), facts=facts,
                           facts_ram=ram)
    if chat is not True:
        # never type into a chat that isn't confirmed open: it may be someone else's
        if chat is False and (send or as_draft):
            # review r2 L4: another chat opened and nothing was typed: settled with the driver's own line (never "the
            # PC shows it didn't go through", and not counted as a failed send)
            return RouteResult(ran=True, ask=LINE_UNSURE_CHAT.format(app=_title(app), name=name),
                               outcome="other_chat", app=app, facts=facts, facts_ram={**ram, "typed": False,
                                                                                     "pressed": False})
        return RouteResult(ran=True, outcome="chat_unconfirmed" if chat is None else "other_chat", app=app,
                           unverified_line=LINE_UNSURE_CHAT.format(app=_title(app), name=name), facts=facts,
                           facts_ram={**ram, "typed": False, "pressed": False} if nothing_out else ram)
    # chat19: the keyboard into the opened chat's composer first (found by the app's label data or by structure,
    # focused through UIA, confirmed by the focus read); nothing is read, typed or pressed anywhere else
    keyboard = _composer_focus(call, app, data, route, hwnd, own_field=own_field)
    facts["keyboard"] = keyboard.how
    if keyboard.stop is not None:
        keyboard.stop.facts.update(facts)
        keyboard.stop.facts_ram = {**ram, "typed": False, "pressed": False}
        return keyboard.stop
    if not keyboard.ok:
        if keyboard.how == "own_left":
            # gb (B3, contract K-S): this run's own switcher was left holding the keyboard (its close key didn't go):
            # measured once more, for the hand-off
            _mark_held(facts, call.ctx.hands, own_field)
        return _not_typed(call, app, name, hwnd, facts, ram, keyboard.holder, read=False)
    # The composer is read before anything is typed (review r1 L6, r2 H1), the field's own text (never the caret
    # context, fMsg). Exactly these words already there (a draft Yuki typed on an earlier run whose send key was held,
    # kept by the app): never typed a second time; only the send key, if asked. Exactly another text Yuki typed into
    # this chat itself (an earlier turn's draft, known by its keyed digest in RAM): selected with the app's own
    # select-all and typed over (fMsg; an app with no select-all in its data leaves it, as below). Any other text there
    # (the user's kept draft): nothing typed and nothing pressed, in either mode (appended, Enter would send that text
    # with the user's words). Unreadable (no pattern answered, the read timed out, no text field has the keyboard): a
    # draft is typed as before; a send is typed and its send key is never pressed (sWire r1 M1: a hidden kept draft
    # would go out with the words), and the user is told to press Enter.
    composer, window = _composer_state(call.ctx.hands, text, route.thread_ids)
    select_all = [str(k) for k in data.get("select_all") or []]
    if composer == "yuki_text" and not select_all:
        composer = "other_text"
    facts["composer"] = composer
    if composer == "other_text":
        ram.update(typed=False, pressed=False)
        return RouteResult(ran=True, ask=LINE_DRAFT_THERE.format(name=name), outcome="draft_in_the_way", app=app,
                           facts={**facts, "typed": "no", "pressed": False}, facts_ram=ram)
    kept = composer == "same_text"
    blind = send and composer == "unread"
    send_keys = [str(k) for k in data.get("send") or ["enter"]]
    # Item 11's L2 (:func:`_send_decl`): whom by the person's id too, only into their own direct message
    decl, grant = _send_decl(call, target, route, text)
    apps = list(data.get("images") or [])
    runs: list[dict[str, Any]] = []

    def phase(steps: list[dict[str, Any]], declared: Mapping[str, Any] | None) -> dict[str, Any]:
        # fMsg review M1, M2: each part on its own run (the select-all, the typing, the send key), so the select-all is
        # judged on its own, the typing is the run's one step (a draft Yuki worded may be typed, ``draft_of``), and the
        # box is read between the typing and the send key
        _ok, got = run_steps(call.ctx.hands, steps, words=call.ctx.words, apps=apps, key="message", sends=declared,
                             grant=grant)
        got = dict(got)
        got["steps"] = len(steps)
        runs.append(got)
        return got

    facts["kept_draft"] = kept
    typed: bool | None = True if kept else None
    went = False
    box = ""
    if composer == "yuki_text":
        # fMsg review M1: the select-all goes on its own run, counted once it went out. Selecting text changes nothing
        # the screen watch sees ("no visible change" is no failure here); whether it took is read in the box after the
        # typing. The run declares no send, so the select-all is never taken for the send key (``DeskHands._own``)
        facts["replaced"] = "own_draft"
        if not _dispatched(phase([{"do": "keys", "keys": select_all}], None), "keys"):
            typed = False
    if not kept and typed is not False:
        typing = phase([{"do": "type", "text": text}], decl if send and not blind else None)
        went = _dispatched(typing, "type")
        # fMsg review M1: no typing went out (refused, held, stopped before it): never "typed"
        typed = _step_verdict(typing, "type") if went else False
        if went:
            note_typed(window or hwnd, text, chat=route.thread_ids)
        if went and ((send and not blind) or composer == "yuki_text"):
            # fMsg review M2: before a send key the whole box, read again, must hold exactly the words, once (Yuki's
            # earlier draft gone, nothing of the user's added); a draft typed over Yuki's own is checked the same way
            box = _box_now(call, text, route.thread_ids)
            facts["box"] = box
            if box == "same_text":
                typed = True
    pressed = False
    if send and not blind and (kept or box == "same_text"):
        pressed = _dispatched(phase([{"do": "keys", "keys": send_keys}], decl), "keys")
    run = _merged_runs(runs)
    ram.update(typed=typed, pressed=pressed)
    facts.update(typed={True: "yes", False: "no", None: "unknown"}[typed], pressed=pressed,
                 steps=int(run.get("steps") or 0), steps_done=int(run.get("steps_done") or 0))
    if run.get("refused_steps"):
        # integ27 (fd merge note 1, D5, final-F k_clarify_who_draft): the step check held the run's steps (their text
        # is not in the request's words): carried to the not-typed result, so the engine says the typing was held and
        # whose chat opened (``typing_held``), never "nothing was typed" with no why. Content-free: a boolean
        facts["refused_steps"] = True
    if send and run.get("refused_steps") and not went and not pressed:
        # lMsg: the send check refused the steps before any ran (whom or what isn't in the user's words, and no grant):
        # nothing typed, nothing sent. Told so, never "I tried to send it, but the PC shows it didn't go through"
        ram.update(typed=False, pressed=False)
        return RouteResult(ran=True, ask=LINE_SEND_REFUSED.format(name=name), outcome="send_refused", app=app,
                           facts={**facts, "held_at": "send_check"}, facts_ram=ram)
    if send and not pressed and run.get("held") and went:
        # review r1 L6: the text is in the composer and Yuki's own check held the send key. Told, not asked: a "yes"
        # would re-run the skill and type the text again into the kept draft
        return RouteResult(ran=True, ask=LINE_TYPED_HELD.format(name=name), outcome="typed_held", app=app,
                           facts={**facts, "held_at": "send"}, facts_ram=ram)
    stop = _stopped(run, app, acted=True)
    if stop is not None:
        stop.facts.update(facts)
        stop.facts_ram = ram
        stop.app = app
        return stop
    if box and box != "same_text" and (send and not blind or box == "other_text"):
        # fMsg review M2: typed, but the box doesn't hold exactly the words: no send key. More than the words there
        # (the select-all didn't take, or text of the user's came with them) is said plainly; for a send, a box that
        # can't be read or doesn't show the words yet is "couldn't confirm, didn't send"
        line = LINE_BOX_MORE if box == "other_text" else LINE_TYPED_UNREAD if box == "unread" else LINE_TYPED_NOT_SENT
        return RouteResult(ran=True, ask=line.format(name=name), outcome="typed_unconfirmed", app=app,
                           facts={**facts, "held_at": "box"}, facts_ram=ram)
    if not kept and not went and not blind and not as_draft:
        # fMsg review M1: no typing went out (the select-all or the typing was refused or stopped without a reason
        # the user hears): never "typed". chat19 (C3): a failed result with the code facts, never a question
        return _not_typed(call, app, name, hwnd, facts, ram)
    if blind:
        # sWire r1 M1: a composer that couldn't be read; the send key was never pressed. Told, not asked, either way
        if typed is not False and went:
            return RouteResult(ran=True, ask=LINE_TYPED_UNREAD.format(name=name), outcome="typed_unread", app=app,
                               facts={**facts, "held_at": "composer"}, facts_ram=ram)
        return _not_typed(call, app, name, hwnd, facts, ram)
    if as_draft:
        # review r1 H1: the send they asked for, to a person matched by sound and answered since, is typed and left;
        # told plainly, never "Sent" (the done-check would wait for a send that never goes)
        if typed is False:
            # chat19 (C3): not typed is a failed result; ``held_at = whom`` tells the caller it is never sent by sound
            return _not_typed(call, app, name, hwnd, {**facts, "held_at": "whom"}, ram)
        return RouteResult(ran=True, ask=LINE_TYPED_ALIKE.format(name=name), outcome="typed_alike", app=app,
                           facts={**facts, "held_at": "whom"}, facts_ram=ram)
    say = (SAY_SENT if send else SAY_DRAFT).format(name=name, where=where)
    unverified = (LINE_SENT_UNCONFIRMED if pressed else LINE_NOT_TYPED if typed is False else
                  LINE_TYPED_NOT_SENT if send else LINE_DRAFT_UNCONFIRMED).format(name=name)
    return RouteResult(ran=True, outcome="sent" if pressed else "typed", app=app, say=say,
                       unverified_line=unverified, facts=facts, facts_ram=ram)


def route_open_link(call: RouteCall) -> RouteResult:
    """``message``, rung A first way: the app's own deep link to the chat (see the module docstring)."""
    app = str(call.args.get("app") or "")
    data = app_data(app)
    if not data:
        return RouteResult(ran=False, outcome="no_app_data")
    got = _target_route(call)
    if isinstance(got, RouteResult):
        return got
    target, route = got
    if route.app != app:
        return RouteResult(ran=False, outcome="other_app")
    uri = deep_link(data, route)
    if not uri:
        return RouteResult(ran=False, outcome="no_link")
    text, _send = _text_mode(call.values)
    if text and call.ctx.hands is None:
        return RouteResult(ran=False, outcome="no_hands")      # typing needs hands (the voice's or the brain's desk)
    as_draft, refused = _send_gate(call, target, route)
    if refused is not None:
        return refused
    if kind := _refused("open_url"):
        return RouteResult(ran=False, held=kind, outcome="refused")
    running = _app_hwnd(data)
    restored = False
    if running and _minimised(running):
        # fix-r31 (A1, final-I R13's trigger, a message row; both brains, 5 of 5 minimised attempts
        # unconfirmed): the app the user asked to message is the ask, not a side effect. Its link switches the chat
        # inside a minimised window and leaves it there, so the chat is never seen open and nothing is typed. The
        # window is restored in place first (the guarded focus: D17; ``SW_RESTORE`` keeps its own size), the place hint
        # honoured right after (the place verb takes no minimised window). The window's measured state only
        if kind := _refused("focus", hwnd=running):
            return RouteResult(ran=False, held=kind, outcome="refused")
        shown = _focus(running)
        if kind := _held_by_game(shown):
            return RouteResult(ran=False, held=kind, outcome="refused")
        restored = not _minimised(running)
    # P1.4: an open window is put on the hint's screen first (the link brings it forward there); a cold start, or a
    # window that couldn't be placed yet (minimised), as soon as the link has brought it up
    placed = bool(running) and _place(call.ctx, running, activate=False)
    since = time.time()
    opened = _open_uri(uri, running=bool(running))
    if kind := _held_by_game(opened):
        return RouteResult(ran=False, held=kind, outcome="refused")
    if not getattr(opened, "ok", False):
        return RouteResult(ran=False, outcome="not_opened")
    if not placed:
        _place(call.ctx, _app_hwnd(data) or running, activate=True)
    return _after_open(call, app, data, target, route, via="link", since=since, hwnd=_app_hwnd(data) or running,
                       as_draft=as_draft, bring_once=True, restored=restored)


def route_switcher(call: RouteCall) -> RouteResult:
    """``message``, rung A second way: the app's own quick switcher (or chat search), into its own window only."""
    app = str(call.args.get("app") or "")
    data = app_data(app)
    if not data:
        return RouteResult(ran=False, outcome="no_app_data")
    got = _target_route(call)
    if isinstance(got, RouteResult):
        return got
    target, route = got
    if route.app != app:
        return RouteResult(ran=False, outcome="other_app")
    hands = call.ctx.hands
    if hands is None:
        return RouteResult(ran=False, outcome="no_hands")
    steps, way_text = _switch_steps(call, data, route)
    if not steps:
        return RouteResult(ran=False, outcome="no_switcher")
    as_draft, refused = _send_gate(call, target, route)
    if refused is not None:
        return refused
    hwnd, failed = _focus_app(data, call.ctx)
    if failed is not None:
        return failed
    since = time.time()
    ok, run = _run_switch(call, hands, steps, apps=list(data.get("images") or []), key="message", way_text=way_text,
                          data=data)
    done = int(run.get("steps_done") or 0)
    tidy = _switch_facts(run, steps)
    stop = _stopped(run, app, acted=done > 0)
    if stop is not None:
        # gb (K-S, review F2): the stop keeps its own outcome (the engine acts on it); ``switcher_held`` rides its facts
        stop.facts.update(tidy)
        return stop
    if run.get("switcher") == "unseen":
        # review r1 L1: the shortcut went but no other field took the keyboard: nothing typed, nothing sent (gb B3:
        # ``switcher_held`` when what it opened still holds the keyboard, measured)
        return RouteResult(ran=False, outcome=_switch_outcome(run, "switcher_unseen"), facts=tidy)
    if not run.get("finished"):
        # chat19 (RC1): never on to the chat with only part of the switcher done (the shortcut alone counted before,
        # and the chat already on screen passed for the one opened while the switcher kept the keyboard); what it
        # opened was closed (:func:`_tidy_switch`), nothing typed, nothing sent. gb (B3, contract K-S): one its close
        # key did not close ends ``switcher_held`` (measured), so the hand-off knows the switcher holds the keyboard
        return RouteResult(ran=False, outcome=_switch_outcome(run, "switcher_failed"), facts=tidy)
    return _after_open(call, app, data, target, route, via="switcher", since=since, hwnd=hwnd, as_draft=as_draft,
                       own_field=run.get("switcher_field"))


def _switch_facts(run: Mapping[str, Any], steps: Sequence[Any]) -> dict[str, Any]:
    """A switcher run's content-free facts: its steps, how many went, and (chat19) whether what it opened was closed.

    gb (B3, final-G2 R13; contract K-S, writer): ``switcher_held`` (bool, measured: what this run's shortcut opened
    still holds the app's keyboard as the run ends; left out when it can't be told) and, for a run that stopped before
    its last step, ``stopped``: why, as a code word (:func:`_stop_word`)."""
    out: dict[str, Any] = {"steps": len(steps), "steps_done": int(run.get("steps_done") or 0)}
    if run.get("switcher_closed"):
        out["switcher_closed"] = str(run["switcher_closed"])
    if isinstance(run.get("switcher_held"), bool):
        out["switcher_held"] = run["switcher_held"]
    word = _stop_word(run)
    if word:
        out["stopped"] = word
    return out


def _stop_word(run: Mapping[str, Any]) -> str:
    """gb (B3; final-G2 R13: "the Enter never reached consent ... the cause is not logged"): why a switcher run stopped
    before its last step, as a code word from the step runner's own facts (``uia_route.SkillRunner``):
    ``refused_steps``, ``taken_over``, ``act_off``, ``to_planner``, ``held``, the runner's deviation (``blind``: no key
    after a step whose effect couldn't be seen; ``no_effect``, ``failed``, ``window``, ``no_focus``, ``dialog`` ...),
    the switcher's own word (``unseen``, ``composer``, ``closed_own``) or ``unknown``; ``""`` for a run that finished.
    Content-free."""
    if run.get("finished"):
        return ""
    if run.get("refused_steps"):
        return "refused_steps"
    if run.get("took_over_at"):
        return "taken_over"
    if run.get("act_off"):
        return "act_off"
    if run.get("to_planner"):
        return "to_planner"
    if run.get("held"):
        return "held"
    dev = run.get("deviation")
    if isinstance(dev, str) and dev and dev.replace("_", "").isalpha():
        return dev[:20]
    switcher = run.get("switcher")
    if switcher in ("unseen", "composer", "closed_own"):
        return str(switcher)
    return "unknown"


def _switch_outcome(run: Mapping[str, Any], word: str) -> str:
    """gb (B3, contract K-S): a switcher run that ends with what its own shortcut opened still holding the app's
    keyboard (measured, ``run["switcher_held"]``) ends ``switcher_held``, so the rungs tried (the hand-off's seed, the
    talker's evidence) say so in code; otherwise its own word (``switcher_unseen`` / ``switcher_failed``). Only on
    these two ends: a run :func:`_stopped` ended (``to_planner``, ``held_by_check``, ``taken_over``, ``refused``) and
    the ``own_left`` path (``not_typed``) keep their own outcome, which the engine acts on, and carry the measured
    ``facts["switcher_held"]`` alone (gb review F2): the engine copies it onto its ``tried`` entry (lane D)."""
    return "switcher_held" if run.get("switcher_held") is True else word


def _switch_steps(call: RouteCall, data: Mapping[str, Any], route: ChatRoute) -> tuple[list[dict[str, Any]], str]:
    """The quick switcher's steps (shortcut, the chat's name, open), or the chat search's (shortcut, the name, the
    result row by the keyed digest of the name), and the route's own words that may be typed."""
    name = _words(route.chat)
    if not name:
        return [], ""
    if data.get("switcher"):
        query = switcher_query(data, route)
        return ([{"do": "keys", "keys": [str(k) for k in data["switcher"]]}, {"do": "type", "text": query},
                 {"do": "keys", "keys": [str(k) for k in data.get("open") or ["enter"]]}], query)
    pick = data.get("pick") if isinstance(data.get("pick"), Mapping) else None
    if data.get("search") and pick and pick.get("role"):
        from yuki.skills.engine import _mem

        image = str((data.get("images") or [""])[0])
        role = str(pick["role"])
        digests = _mem(call.ctx.memory, "pick_digests", lambda c: c.name_digests(image, role, [name]),
                       timeout_s=1.0) if call.ctx.memory is not None else None
        if not isinstance(digests, list) or not digests or not digests[0]:
            return [], ""
        return ([{"do": "keys", "keys": [str(k) for k in data["search"]]}, {"do": "type", "text": name},
                 {"do": "pick", "role": role, "in": {"role": str(pick.get("box_role") or "List")},
                  "item": str(digests[0]), "app": image}], name)
    return [], ""


#: How many of the user's newest captured messages (every app) the send check reads.
SENT_ROWS = 50


def sent_in_chat(ctx: Any, text: str, thread_ids: Sequence[int]) -> tuple[bool | None, dict[str, Any]]:
    """The user's own message with ``text`` captured since the run began in one of these capture threads (the chat the
    route opened, by memory's thread ids; review r1 M5, L7): True. Matched by thread, never by the app's name (a chat
    kept under an older program key or display name still counts; the same text in another chat of that app doesn't).
    The captured text must equal ``text`` once whitespace and case are folded (review r2 H1: a message that only
    contains the user's words carried something else with them, such as a kept draft, and is never "Sent").
    Nothing matching yet: ``None``, never False (the watcher lags)."""
    from yuki.skills.donecheck import _norm_text

    def _same_text(want: str, got: str) -> bool:
        return bool(want) and _norm_text(want) == _norm_text(got)

    ids = {int(t) for t in thread_ids or [] if isinstance(t, int)}
    call = getattr(ctx, "memory_call", None)
    if call is None:
        return None, {"status": "no_memory"}
    if not ids:
        return None, {"status": "no_threads"}
    try:
        got = call("sent_messages", lambda c: c.sent_messages(None, since=float(ctx.since), limit=SENT_ROWS))
    except Exception:  # noqa: BLE001
        return None, {"status": "unread"}
    if not isinstance(got, list):
        return None, {"status": "unread"}
    rows = [r for r in got if isinstance(r, Mapping) and isinstance(r.get("thread"), int) and int(r["thread"]) in ids]
    hits = sum(1 for r in rows if _same_text(text, str(r.get("text") or "")))
    if hits:
        return True, {"n": len(rows), "matched": hits}
    return None, {"n": len(rows), "other_chats": len(got) - len(rows), "status": "not_seen_yet"}


def probe_message_done(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """``message``'s done-check. A send: memory captured the user's own message with that text in that chat since the
    run began (:func:`sent_in_chat`, by the route's thread ids; none yet is "couldn't confirm", never "no"; send never
    pressed is "no" when nothing could have gone). A draft: the chat was seen open and the text read back typed. Just
    opening: the chat seen open. With ``live`` (the re-check) or after a hand-off (no route result) it reads the chat
    again now."""
    app = str(args.get("app") or "")
    send = str(args.get("send") or "") == "send"
    text = _words(args.get("text"))
    live = bool(args.get("live"))
    result = getattr(ctx, "result", None)
    ram = dict(getattr(result, "facts_ram", None) or {})
    if ram.get("app") and ram.get("app") != app:
        ram = {}
    if send and text:
        if ram and not live and not ram.get("pressed"):
            if ram.get("chat") is False:
                return False, {"status": "not_sent"}
            if ram.get("typed") is False:
                # chat19 review M1: nothing typed and no key pressed (C3) is no failed send ("the PC shows it didn't go
                # through"), and no failed rung: unverified in the check's own word, so the route's own line is said
                return None, {"status": "not_typed"}
            return None, {"status": "not_pressed"}
        thread_ids = list(ram.get("thread_ids") or [])
        if not thread_ids:
            # after a hand-off: every chat memory has with them (whatever app the thinker used)
            store = _probe_store(ctx)
            target = find_target(store, str(args.get("person") or "")) if store is not None else None
            if isinstance(target, Target):
                thread_ids = [t for r in target.routes for t in r.thread_ids]
        return sent_in_chat(ctx, text, thread_ids)
    if ram and not live:
        chat = ram.get("chat")
        if chat is False:
            return False, {"status": "other_chat"}
        if text:
            if chat is True and ram.get("typed") is True:
                return True, {"status": "typed"}
            # chat19 (C3): a draft the route couldn't type says so in the check's own word (the evidence's status)
            return None, {"status": "not_typed" if ram.get("typed") is False else "unconfirmed"}
        if chat is True:
            return True, {"status": "open"}
    store = _probe_store(ctx)
    if store is None:
        return None, {"status": "no_memory"}
    thread_ids = list(ram.get("thread_ids") or [])
    data = app_data(ram.get("app") or app)
    if not thread_ids:
        # after a hand-off: the person's route as memory ranks it (whatever app the thinker used)
        target = find_target(store, str(args.get("person") or ""))
        if isinstance(target, Asked):
            return None, {"status": "no_target"}
        route = pick_route(target, None)
        if isinstance(route, Asked):
            return None, {"status": "no_route"}
        thread_ids, data = list(route.thread_ids), app_data(route.app)
    verdict, ev = chat_state(store, thread_ids, data, float(getattr(ctx, "since", 0.0) or 0.0), _board_now())
    if verdict is True and text and not ram:
        return None, {"status": "draft_unread"}        # the thinker's draft: open, its text unreadable here
    return verdict, ev


# ---------------------------------------------------------------------------------------------------------------------
# discord.voice
# ---------------------------------------------------------------------------------------------------------------------


def _wait_mic(ctx: Any, image: str, before: bool | None, budget_s: float) -> bool:
    """Whether the app took the microphone (an onset against ``before``) within ``budget_s`` (only while waiting)."""
    if before is not False:
        return False
    deadline = ctx.clock() + budget_s
    while True:
        if _mic_held(image) is True:
            return True
        if ctx.clock() + POLL_S > deadline:
            return False
        ctx.sleep(POLL_S)


#: :func:`_judged`'s answer when the consent rule handed the press over (``brain = "all"``: a join no plan declared;
#: bSkill, bTalk merge note 4): the engine's rung C, never "Should I go ahead?".
HANDED_OVER = "to_planner"


def _one_call() -> Any:
    """Phase 4 (``brain = "one"``): the ONE runner's call running on this thread (``voice.hands.one_now``), else
    ``None``. Imports nothing: with the hands module not loaded no ONE call can be running. Never raises."""
    import sys

    module = sys.modules.get("yuki.voice.hands")
    now = getattr(module, "one_now", None) if module is not None else None
    try:
        return now() if callable(now) else None
    except Exception:  # noqa: BLE001 - unreadable: no ONE call (the press as before)
        return None


def _judged(hands: Any, hwnd: int, image: str, role: str, name: str) -> str | None:
    """Yuki's own check on pressing the app's button (a join is judged like a send, rule 8): ``None`` allowed,
    :data:`HANDED_OVER` when the consent rule handed it to the planner, else what to ask or say.

    Phase 4 (``brain = "one"``): while the ONE runner's call runs on this thread, the ONE gate decides the press
    (``yuki.one.runner.skill_step``: the Act switch of the app and of the run's skill's own app, then the join on
    their plain words through the gate's grant); never handed over. Under ``all`` nothing changes."""
    one = _one_call()
    if one is not None:
        from yuki.one.runner import skill_step

        element = f'{role} "{name}"'
        try:
            verdict = skill_step(hands, "click", {"hwnd": hwnd, "element": element},
                                 {"hwnd": hwnd, "app": image, "element": element}, [], force=True, ctx=one)
        except Exception:  # noqa: BLE001 - fail closed
            return LINE_HELD
        if verdict is None:
            return None
        off = str(getattr(verdict, "act_off", "") or "")
        if off:
            return LINE_ACT_OFF.format(app=off)
        return str(getattr(verdict, "question", "") or "") or LINE_HELD
    act_off = getattr(hands, "act_off", None)
    if callable(act_off):
        try:
            off = act_off(image)
        except Exception:  # noqa: BLE001
            off = None
        if off:
            return LINE_ACT_OFF.format(app=str(off))
    authorize = getattr(hands, "authorize", None)
    if not callable(authorize):
        return None
    element = f'{role} "{name}"'
    try:
        verdict = authorize("click", {"hwnd": hwnd, "element": element}, {"hwnd": hwnd, "app": image,
                                                                         "element": element}, [], force=True)
    except Exception:  # noqa: BLE001 - fail closed
        return LINE_HELD
    if verdict is not None and not getattr(verdict, "allowed", False):
        if getattr(verdict, "handover", False) is True:
            return HANDED_OVER
        return str(getattr(verdict, "question", "") or "") or LINE_HELD
    return None


def _known_image(key: str) -> str:
    """The program image of the chat app whose data names ``key`` among its app keys (``processes``; Discord's
    "discord" -> "discord.exe"), casefolded; ``""`` when no chat app's data names it."""
    for name in chat_apps():
        d = app_data(name)
        if key in {_app_key(p) for p in d.get("processes") or []} and d.get("images"):
            return str(d["images"][0]).casefold()
    return ""


def _mic_live(process: str) -> bool | None:
    """Whether the program ``process`` (memory's app key or an image name) holds the microphone now: True with an
    active capture session; False when it has capture sessions and none is active (it let go of the microphone: the
    call ended), or when it is a chat app whose own data gives its image name and that image has no capture session
    at all (review r1 L5: an app that drops its session after a call); ``None`` when it can't be read, or an app
    whose image name is only guessed from its key has no session under that guess (a packaged app's key is not its
    image name, so absence proves nothing there)."""
    from yuki.skills.donecheck import _mic_held_by

    name = str(process or "").strip().casefold()
    if not name:
        return None
    known = name if name.endswith(".exe") else _known_image(_app_key(name))
    held, n = _mic_held_by(known or f"{name}.exe")
    if held is None or (not held and n == 0 and not known):
        return None
    return bool(held)


def _current_calls(memory: Any) -> list[str] | None:
    """The app keys of the calls going on now: memory's open calls (``MemoryClient.current_calls``: an app using the
    microphone as a call), each checked against the live microphone right now (lMsg, live problem 11: a call memory
    still showed open 2 hours after it ended made a join ask "switch?"). A call whose app holds no active capture
    session now is over and left out; one whose microphone can't be read keeps memory's word. ``None`` when memory
    can't say."""
    from yuki.skills.engine import _mem

    if memory is None:
        return None
    calls = _mem(memory, "current_calls", lambda c: c.current_calls(), timeout_s=0.3)
    if not isinstance(calls, list):
        return None
    out: list[str] = []
    for c in calls:
        if not isinstance(c, Mapping):
            continue
        key = _app_key(c.get("process") or c.get("app"))
        if key and _mic_live(str(c.get("process") or key)) is not False:
            out.append(key)
    return out


def voice_labels(data: Mapping[str, Any]) -> list[list[str]]:
    """The app's own words for a voice channel's header, per display language (``[chat.labels.<lang>]``
    ``voice_channel``: Discord's "Voice Channel", which memory's capture keeps before the channel's name in the
    thread's scope, "Voice Channel lofi lounge"), as word lists. ``[]`` when the app's data has none."""
    from yuki.memory.contacts import tokens

    out: list[list[str]] = []
    labels = data.get("labels") if isinstance(data.get("labels"), Mapping) else {}
    for lang in labels.values():
        if not isinstance(lang, Mapping):
            continue
        value = lang.get("voice_channel")
        for label in [value] if isinstance(value, str) else list(value or []):
            words = tokens(str(label or ""))
            if words and words not in out:
                out.append(words)
    return out


def channel_known(store: Any, data: Mapping[str, Any], name: str, *, kind: str = "discord_voice") -> bool | None:
    """Whether ``name`` (the user's words for a voice channel) names a voice channel memory has seen in this app (the
    app's own keys in its data, ``processes``): a capture thread whose scope (the chat header) is the app's own
    voice-channel label (:func:`voice_labels`) followed by the channel's name with those words in it, whole and in
    order ("Voice Channel lofi lounge"), or a channel a join was noted for (tastes of ``kind``). Review r1 L4: a DM, a
    text channel ("general") or a server's name is not a voice channel. With no label in the app's data, any of the
    app's thread titles or scopes counts (the first build's rule). Identity of the user's words in memory's own data,
    never a word list; titles and names stay in RAM. ``None`` when memory's threads can't be read."""
    from yuki.memory import contacts as C

    want = C.tokens(name)
    keys = sorted({_app_key(p) for p in data.get("processes") or []} - {""})
    if not want or not keys:
        return False
    labels = voice_labels(data)
    marks = ",".join("?" * len(keys))
    try:
        rows = store._query(f"SELECT title_ciphertext, scope_ciphertext FROM threads WHERE process IN ({marks})"
                            " AND merged_into IS NULL", keys)
    except Exception:  # noqa: BLE001
        return None
    for row in rows:
        for blob in ((row[1],) if labels else (row[0], row[1])):
            if not blob:
                continue
            try:
                text = store._dec(blob)
            except Exception:  # noqa: BLE001 - a row the key can't read
                continue
            if not text:
                continue
            if not labels:
                if C.span_in(want, str(text)):
                    return True
                continue
            words = C.tokens(str(text))
            if any(words[:len(lab)] == lab and C.span_in(want, words[len(lab):]) for lab in labels):
                return True
    try:
        from yuki.skills import taste

        return any(C.span_in(want, n) for n in taste.candidates(store, kind, limit=50))
    except Exception:  # noqa: BLE001
        return False


def _known_channel(ctx: Any, app: str, data: Mapping[str, Any], name: str) -> bool | None:
    """:func:`channel_known` through the run's memory (``None``: memory can't say; the join is then judged as
    before)."""
    from yuki.skills.engine import _mem, _store_of

    if getattr(ctx, "memory", None) is None:
        return None
    got = _mem(ctx.memory, "voice_channel", lambda c: channel_known(_store_of(c), data, name), timeout_s=0.8)
    return got if isinstance(got, bool) else None


def _call_question(call: RouteCall, data: Mapping[str, Any], app: str, name: str, mic_before: bool | None
                   ) -> tuple[str, str] | None:
    """The question asked before a join while a call is live (review r2 M2, L1), or ``None`` (no call live). Asked
    here, in the rung, so it comes after the engine's game check (a game in front is "after the round?", never this
    question). The live call is this app's own (it holds the microphone now, or memory's open calls name it): "Switch
    to {channel}?" (the app leaves its current channel by itself). Another app's, or one memory can't name: "Join
    {channel} on {app} as well?" (no route leaves another app's call, so none is promised). Words from the app's data
    (``[chat.asks]``). ``(question, code word)``. lMsg (live problem 11): memory's open calls are checked against the
    live microphone first (:func:`_current_calls`), and the voice's own picture counts only when its hands carry one
    (``in_call``), so a call that ended hours ago never makes a join ask "switch?"."""
    keys = {_app_key(p) for p in data.get("processes") or []}
    calls = _current_calls(call.ctx.memory) or []
    own = mic_before is True or any(k in keys for k in calls)
    hands = call.ctx.hands
    if hands is not None and hasattr(hands, "in_call"):
        live = bool(getattr(hands, "in_call", False))
    else:
        live = bool(calls)
    if not (own or live):
        return None
    asks = data.get("asks") if isinstance(data.get("asks"), Mapping) else {}
    line = str(asks.get("switch") or ASK_SWITCH) if own else str(asks.get("in_call") or ASK_ALSO_JOIN)
    return line.replace("{channel}", name).replace("{app}", _title(app)), "own" if own else "other"


def route_voice_join(call: RouteCall) -> RouteResult:
    """``discord.voice``'s rung A: asked first when a call is live (:func:`_call_question`), then the quick switcher
    with the voice filter and the channel's name, Enter, then "Join Voice" if the microphone wasn't taken (see the
    module docstring)."""
    app = str(call.args.get("app") or "")
    data = app_data(app)
    if not data:
        return RouteResult(ran=False, outcome="no_app_data")
    channel = call.values.get("channel")
    name = _words(channel.get("name") if isinstance(channel, Mapping) else channel)
    if not name:
        return RouteResult(ran=False, ask="Which voice channel?", outcome="no_channel")
    known = _known_channel(call.ctx, app, data, name)
    if known is False:
        # lMsg (live problem 11): "open NDL's channel" was planned as a Discord voice join. A channel memory never saw
        # in this app is never joined: said plainly, nothing done (the caller may mean a creator's channel elsewhere)
        return RouteResult(ran=False, ask=LINE_NO_CHANNEL.format(app=_title(app), channel=name),
                           outcome="unknown_channel")
    image = str((data.get("images") or [""])[0])
    # the microphone before anything is done: the question's "is it Discord's own call", and the onset's baseline
    before = _mic_held(image)
    # the in-call question (their "no" keeps their call as it is; with no call live a "no" is no answer, lMsg) and,
    # after the switcher, the join itself: the same code as discord.voice's link rung (kHands merge note 8)
    from yuki.skills.discord_route import in_call_gate, join_tail

    gate = in_call_gate(call, data, app, name, before)
    if gate is not None:
        return gate
    hands = call.ctx.hands
    if hands is None or not data.get("switcher"):
        return RouteResult(ran=False, outcome="no_hands" if hands is None else "no_switcher")
    hwnd, failed = _focus_app(data)
    if failed is not None:
        return failed
    prefix = str((data.get("prefix") or {}).get("voice") or "")
    query = prefix + (name[len(prefix):].strip() if prefix and name.startswith(prefix) else name)
    steps = [{"do": "keys", "keys": [str(k) for k in data["switcher"]]}, {"do": "type", "text": query},
             {"do": "keys", "keys": [str(k) for k in data.get("open") or ["enter"]]}]
    ok, run = _run_switch(call, hands, steps, apps=list(data.get("images") or []), key="discord.voice",
                          way_text=query, data=data)
    done = int(run.get("steps_done") or 0)
    tidy = _switch_facts(run, steps)
    stop = _stopped(run, app, acted=done > 0)
    if stop is not None:
        stop.facts.update(tidy)
        return stop
    if run.get("switcher") == "unseen":
        # review r1 L1: the switcher never took the keyboard: the channel's name is never typed into a chat
        return RouteResult(ran=False, outcome=_switch_outcome(run, "switcher_unseen"), facts=tidy)
    if not run.get("finished"):
        # chat19 (RC1): a switcher left partway is closed by its own run (:func:`_tidy_switch`); no Join pressed (gb
        # B3: ``switcher_held`` when its close key did not close it, measured)
        return RouteResult(ran=False, outcome=_switch_outcome(run, "switcher_failed"), facts=tidy)
    return join_tail(call, data, app, hwnd, image, before, name, {"steps_done": done})


def _note_channel(ctx: Any, name: str) -> None:
    """A join that took: the channel (the user's words) noted in tastes, kind ``discord_voice`` (encrypted)."""
    from yuki.skills import taste
    from yuki.skills.engine import _mem, _store_of

    if ctx.memory is None:
        return
    _mem(ctx.memory, "taste_note", lambda c: taste.note(_store_of(c), "discord_voice", name), timeout_s=0.5)


ROUTES: dict[str, Callable[[RouteCall], RouteResult]] = {
    "open_link": route_open_link, "switcher": route_switcher, "voice_join": route_voice_join,
}
PROBES: dict[str, Callable[..., tuple[bool | None, dict[str, Any]]]] = {"message_done": probe_message_done}

__all__ = ["ROUTES", "PROBES", "ChatRoute", "Target", "Asked", "app_data", "chat_apps", "app_of", "find_target",
           "pick_route", "whom", "deep_link", "switcher_query", "run_steps", "chat_state", "route_open_link",
           "route_switcher", "route_voice_join", "probe_message_done", "sent_in_chat", "front_chat",
           "holder_ident", "raised_field", "still_held"]
