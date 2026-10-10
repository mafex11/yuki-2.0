"""Chats: the drivers of ``message`` and ``discord.voice`` (PLAN.md Phase S-2; decisions D0, D2, D17, D19, Q3/rule 8;
B.2; map-skills S2.5).

The connection files name these calls (``connections/message.toml``, ``connections/discord.toml``); each chat app's
own facts (its programs, the services memory stores its chats under, its link and URI shapes, its shortcuts, its labels)
are data in that app's connection file, table ``[chat]`` (``discord.toml``, ``whatsapp.toml``, ``slack.toml``), read by
:func:`app_data`. Nothing here names an app.

**Whom, and where.** The ``person`` slot is the user's own words. Memory's people index finds them
(:func:`yuki.memory.contacts.resolve`: names, aliases, misheard forms the user corrected, a first name for a full
one); two people who fit are a question, never a guess. A group chat or channel is found by its name (the chat name's
words, identity only). The
person's routes are ranked by memory (``rank_routes``: the user's own lasting choice first, then where they message
most lately); each route's app comes from its stored service or its threads' programs, matched against the apps' data.
The app is the one the user named, else the usual route's; the done line names an app the user didn't.

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
  shortcut (the switcher or the search box opened; :func:`_run_switch`); otherwise nothing more is sent.

**Checked open by encrypted ids, never a title** (:func:`chat_state`): memory's own read of the window in front (the
watcher's Now board, read after the run began) is looked up as a capture thread (``Store.find_thread``: a keyed digest
of the app and the chat's scope) and must be one of the route's thread ids; another chat of that app in front is a
"no". Failing that, one of the route's threads read since the run began. **Nothing is typed into a chat that isn't
confirmed open.**

**Rule 8** (Q3): ``send = "send"`` (the user plainly said "send X to Y") types X and presses the app's send key, both
judged by Yuki's own check with the user's words, and both whom and what must stand in their words
(:func:`yuki.skills.uia_route.check_sends`); ``draft`` (naming only the person, or saying what to write without "send")
types it and leaves it; no text just opens the chat. The opened chat's composer is read first: other text already
there (the app's kept draft) means nothing is typed or pressed, said plainly; a composer that can't be read gets a
send's words typed and its send key never pressed (told to press Enter). A send is "done" only when memory
captured the user's own message equal to that text in that very chat (the messages table, matched by the route's
capture thread ids, never by the app's name); "pressed send, couldn't confirm it landed" otherwise, once the send key
went out (its own step dispatched).
If Yuki's own check holds the send key after the text was typed, the user is told it's typed and not sent (never
asked: a "yes" would type it twice).
A rung that acted on a send is never followed by another (the engine's rule: never twice).

**discord.voice** (:func:`route_voice_join`): Discord's quick switcher with its voice filter ("!") and the channel's
name from the user's words, Enter; if Discord doesn't take the microphone within 2 s, Discord's "Join Voice" button
(its label from the data, judged like the switcher's keys). A join that took is noted in tastes (kind
``discord_voice``, encrypted). With a call live it asks first, in the rung (after the engine's game check), and the
question says what will happen (review r2 M2): "Switch to {channel}?" when the call is Discord's own, else "Join
{channel} on Discord as well?" (nothing leaves another app's call); the answer comes back as the ``join_in_call`` slot.

Every route takes focus: never with a game in front (the engine holds the run: "after the round?"), each focus and
opened link asks ``gamegate.refuse_input`` here, and the input layer refuses again. The steps run through the hands
the run was given: the voice hands, or on the new brain its desk hands (``uia_route.DeskHands``, zFinal: told each
run's steps by :func:`run_steps`, they let the capability's own steps through and the send key only when the brain's
consent rule allowed this very send and counted it down). Without hands (the old loop's own run, a card) a route that
must type or press keys doesn't run; the thinker then does it itself with the skill pinned. Logs and facts are
content-free (code words, counts); names, chat names, links and text stay in RAM.
"""

from __future__ import annotations

import threading
import time
import tomllib
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from yuki.skills.engine import RouteCall, RouteResult

#: The lines said when nothing is done (asked or told plainly; never a "done").
ASK_WHO = "Who to?"
LINE_UNKNOWN = "I don't know who {who} is."
ASK_WHICH = "Which {who}: {names}?"
ASK_OTHER_APP = "I don't have a {app} chat with {name}. {usual} instead?"
LINE_NO_APP_CHAT = "I don't have a {app} chat with {name}."
LINE_NO_ROUTE = "I don't know where you message {name}."
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
#: A send whose composer couldn't be read before typing (sWire r1 M1: it may hold the app's kept draft, which Enter
#: would send with the user's words): the words are typed and the send key is never pressed; told, not asked (a "yes"
#: would re-run the skill and type them a second time).
LINE_TYPED_UNREAD = ("It's typed in your chat with {name}, but I couldn't read the message box, so I didn't send it. "
                     "Press Enter when you want it to go.")
LINE_VOICE_UNCONFIRMED = "I opened {channel} in Discord, but couldn't confirm you joined it."
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


@dataclass
class Target:
    """Whom the message is for: the name memory keeps (a person's, or a group chat's), the user's own words for them,
    and the routes, the usual first."""

    name: str
    said: str
    routes: list[ChatRoute]
    place: bool = False


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


def find_target(store: Any, person: str) -> Target | Asked:
    """Whom ``person`` (the user's words) names: one of their people (``contacts.resolve``: a name or alias, whole or a
    whole-word part of it, a misheard form the user corrected; two who fit: a question), else a group chat or channel
    whose name is those words (identity), else "I don't know who that is"."""
    from yuki.memory import contacts as C

    who = _words(person)
    if not who:
        return Asked(ASK_WHO, outcome="no_person")
    try:
        found = C.resolve(store, who)
    except Exception:  # noqa: BLE001
        found = []
    people: dict[int, Any] = {}
    for c in found:
        people.setdefault(int(c.id), c)
    if len(people) > 1:
        names = [c.name for c in people.values()][:3]
        return Asked(ASK_WHICH.format(who=who, names=" or ".join(names)), names, outcome="two_people")
    if people:
        contact = next(iter(people.values()))
        try:
            ranking = C.rank_routes(store, contact)
            routes = [_chat_route(store, r) for r in ranking.routes]
        except Exception:  # noqa: BLE001
            routes = []
        return Target(name=contact.name, said=who, routes=routes)
    want = C.tokens(who)
    try:
        places = [r for r in C.places(store) if want and C.tokens(r.chat) == want]
    except Exception:  # noqa: BLE001
        places = []
    if places:
        now = time.time()
        try:
            places = sorted((C.measure_route(store, r, None, now) for r in places), key=lambda r: -r.score)
        except Exception:  # noqa: BLE001
            pass
        return Target(name=places[0].chat, said=who, routes=[_chat_route(store, r) for r in places], place=True)
    return Asked(LINE_UNKNOWN.format(who=who), outcome="unknown_person")


def pick_route(target: Target, app_named: str | None) -> ChatRoute | Asked:
    """The route to use: the named app's (none: a question offering the usual one), else the usual route."""
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


def _app_named(values: Mapping[str, Any]) -> str | None:
    app = values.get("app")
    return _app_key(app) or None if app else None


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
    key = (_words(person), _app_named(call.values) or "")
    if key not in cache:
        store = _store(call.ctx)
        if store is None:
            cache[key] = None
        else:
            target = find_target(store, str(person or ""))
            cache[key] = target if isinstance(target, Asked) else (target, pick_route(target, key[1] or None))
    got = cache[key]
    if got is None:
        return RouteResult(ran=False, outcome="no_memory")
    if isinstance(got, Asked):
        return RouteResult(ran=False, ask=got.line, options=list(got.options), outcome=got.outcome)
    target, route = got
    if isinstance(route, Asked):
        if route.outcome == "no_route":
            # memory knows the person, but no chat of theirs is in an app these routes drive: the thinker may (pinned)
            return RouteResult(ran=False, outcome="no_route")
        return RouteResult(ran=False, ask=route.line, options=list(route.options), outcome=route.outcome)
    return target, route


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
              way_text: str = "", sends: Mapping[str, str] | None = None) -> tuple[bool, dict[str, Any]]:
    """Steps through the voice hands' step runner (:class:`yuki.skills.uia_route.SkillRunner`: each checked, keys and
    typing judged by Yuki's own check, the user's hands stop it). ``way_text``: the route's own words that may be typed
    (a chat's name memory keeps); everything else typed must be the user's words. ``sends``: ``{"to", "what"}`` when the
    steps send (rule 8: both must stand in the user's words). Refused before any step:
    ``(False, {"refused_steps": True})``."""
    from yuki.skills.uia_route import SkillRefused, SkillRunner

    item: dict[str, Any] = {"run": [dict(s) for s in steps], "apps": list(apps), "ref": f"skill:{key}",
                            "intent": key, "text": way_text}
    args: dict[str, Any] = {"steps": [dict(s) for s in steps], "sends": bool(sends)}
    if sends:
        item["sends"] = True
        args.update(to=str(sends.get("to") or ""), what=str(sends.get("what") or ""))
    # zFinal: hands that check a capability's own steps by what its driver declared (the new brain's desk hands,
    # ``uia_route.DeskHands.driver_steps``) are told this run's steps, the route's own words and what it sends
    declare = getattr(hands, "driver_steps", None)
    scope = (declare(key=key, steps=[dict(s) for s in steps], way_text=way_text, sends=sends) if callable(declare)
             else nullcontext())
    try:
        with scope:
            ok, _report, facts = SkillRunner(hands).run(args, item=item, words=[str(w) for w in words if w])
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
        if not str(facts.get("question") or "") and short and _refused("keys"):
            # a rule's refusal with no question while a game is in front: "after the round?", never "go ahead?"
            return RouteResult(ran=acted, held="game", outcome="refused")
        return RouteResult(ran=acted, ask=str(facts.get("question") or "") or LINE_HELD, outcome="held_by_check")
    if facts.get("took_over_at"):
        return RouteResult(ran=acted, ask=LINE_TOOK_OVER, outcome="taken_over")
    if not facts.get("refused_steps") and int(facts.get("steps_done") or 0) < int(facts.get("steps") or 0) \
            and _refused("keys"):
        return RouteResult(ran=acted, held="game", outcome="refused")
    return None


def _step_verdict(facts: Mapping[str, Any], do: str) -> bool | None:
    """The verdict of the first action of kind ``do`` in this run (``None``: none ran, or its effect couldn't be
    seen)."""
    for r in facts.get("results") or []:
        if getattr(r, "kind", "") == "action" and getattr(getattr(r, "step", None), "do", "") == do:
            return getattr(r, "verdict", None)
    return None


def _composer_text(hands: Any) -> str | None:
    """What the text field with the keyboard (the opened chat's composer) holds now, read in RAM and dropped (never
    logged); ``None`` when no text field has the keyboard or it can't be read. A field that answered neither its Text
    nor its Value pattern (``text_source`` empty and no value: the focus read can't tell "no pattern" from a Value
    pattern that read empty) is unread, never "empty" (sWire r1 M1: an unreadable composer may hold the app's kept
    draft)."""
    read = getattr(hands, "_focus", None)
    if not callable(read):
        return None
    try:
        snap = read(timeout_s=0.4)
    except Exception:  # noqa: BLE001
        return None
    if snap is None or not getattr(snap, "accepts_text", False) or getattr(snap, "is_password", False):
        return None
    source = str(getattr(snap, "text_source", "") or "")
    if source == "text":
        return f"{getattr(snap, 'before', '') or ''}{getattr(snap, 'after', '') or ''}"
    value = getattr(snap, "value", None)
    if value is None and not source:
        return None
    return str(value or "")


def _composer_state(hands: Any, text: str) -> str:
    """The opened chat's composer against the user's words (review r1 L6, r2 H1): ``"empty"``, ``"same_text"`` (it
    already holds exactly ``text``, whitespace and case folded: an identity check of the user's own words),
    ``"other_text"`` (anything else: the app's kept draft) or ``"unread"``."""
    from yuki.skills.donecheck import _norm_text

    held = _composer_text(hands)
    if held is None:
        return "unread"
    if not held.strip():
        return "empty"
    return "same_text" if text and _norm_text(held) == _norm_text(text) else "other_text"


def _dispatched(facts: Mapping[str, Any], do: str) -> bool:
    """Whether a step of kind ``do`` ran in this run: its action went out (``result.ok``: dispatched), whatever its
    screen watch then saw (review r1 M1: a send key whose watch saw no change was still pressed)."""
    return any(getattr(r, "kind", "") == "action" and bool(getattr(r, "ok", False))
               and getattr(getattr(r, "step", None), "do", "") == do for r in facts.get("results") or [])


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


def _field_changed(ctx: Any, hands: Any, before: tuple[int, int, str, str] | None) -> bool:
    """Whether another text field took the keyboard after the shortcut (the switcher's or the search box's), read
    every :data:`POLL_S` for at most :data:`SWITCHER_WAIT_S`."""
    deadline = ctx.clock() + SWITCHER_WAIT_S
    while True:
        now = _field(hands)
        if now is not None and now != before:
            return True
        if ctx.clock() + POLL_S > deadline:
            return False
        ctx.sleep(POLL_S)


def _settled_field(ctx: Any, hands: Any) -> tuple[int, int, str, str] | None:
    """The text field with the keyboard once focus has settled: read every :data:`POLL_S` for at most
    :data:`SETTLE_S` until one has it (only while this skill waits), else ``None``."""
    deadline = ctx.clock() + SETTLE_S
    while True:
        now = _field(hands)
        if now is not None or ctx.clock() + POLL_S > deadline:
            return now
        ctx.sleep(POLL_S)


def _run_switch(call: RouteCall, hands: Any, steps: list[dict[str, Any]], *, apps: Sequence[str], key: str,
                way_text: str) -> tuple[bool, dict[str, Any]]:
    """The switcher's (or search's) steps, its shortcut first on its own: the name is typed and Enter pressed only once
    a different text field took the keyboard (the switcher opened). If it didn't, nothing more is sent (a name typed
    into the open chat's composer and Enter would send it): ``facts["switcher"] = "unseen"``. When no text field has
    the keyboard as the app comes forward, the read waits for focus to settle first (review r2 L3), so a composer that
    takes the keyboard late is the "before", never the switcher."""
    before = _field(hands)
    if before is None:
        before = _settled_field(call.ctx, hands)
    ok, first = run_steps(hands, steps[:1], words=call.ctx.words, apps=apps, key=key)
    first["steps"] = len(steps)
    if not ok or int(first.get("steps_done") or 0) < 1:
        return False, first
    if not _field_changed(call.ctx, hands, before):
        first["switcher"] = "unseen"
        return False, first
    ok, rest = run_steps(hands, steps[1:], words=call.ctx.words, apps=apps, key=key, way_text=way_text)
    merged = dict(rest)
    merged["steps"] = len(steps)
    merged["steps_done"] = int(first.get("steps_done") or 0) + int(rest.get("steps_done") or 0)
    merged["results"] = list(first.get("results") or []) + list(rest.get("results") or [])
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


def chat_state(store: Any, thread_ids: Sequence[int], data: Mapping[str, Any], since: float,
               board: Mapping[str, Any] | None) -> tuple[bool | None, dict[str, Any]]:
    """Whether the chat with these capture threads is open now: memory's read of the window in front (read after
    ``since``, with a chat scope or an address) is one of them -> True; another chat or view of the same app in front
    -> False; else one of the threads read since -> True; else None. Identity by keyed digests of the chat's scope or
    address (``Store.find_thread``), never a window title."""
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
            return False, {"by": "board", "status": "other_chat"}
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


def _wait_chat(call: RouteCall, data: Mapping[str, Any], route: ChatRoute, since: float, hwnd: int
               ) -> tuple[bool | None, dict[str, Any]]:
    """:func:`chat_state` read every :data:`POLL_S` for at most :data:`CHAT_WAIT_S` (only while this skill waits),
    asking the watcher once to read the window again. Ends at the first "open"; else the last read's word (a "no" only
    if the window still shows another chat at the end)."""
    from yuki.skills.engine import _mem, _store_of

    ctx = call.ctx
    if ctx.memory is None:
        return None, {"status": "no_memory"}
    deadline = ctx.clock() + CHAT_WAIT_S
    refreshed = False
    while True:
        board = _board_now()
        got = _mem(ctx.memory, "chat_state",
                   lambda c: chat_state(_store_of(c), route.thread_ids, data, since, board), timeout_s=0.8)
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


def _refused(action: str, hwnd: int | None = None) -> bool:
    """D17's backstop asked here, before a focus, an opened link or keys (the input layer asks again)."""
    from yuki.memory import gamegate

    return gamegate.refuse_input(action, hwnd=hwnd) is not None


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


def _open_uri(uri: str) -> Any:
    from yuki.skills.engine import open_uri

    return open_uri(uri)


def _held_by_game(result: Any) -> bool:
    details = getattr(result, "details", None) or {}
    return isinstance(details, Mapping) and details.get("refused") == "game"


def _mic_held(image: str) -> bool | None:
    from yuki.skills.donecheck import baseline_mic_held

    return baseline_mic_held({"process": image})


def _press(hwnd: int, role: str, name: str) -> str:
    from yuki.skills.uia_route import press

    return press(int(hwnd), role=role, name=name, pattern="invoke").outcome


def _focus_app(data: Mapping[str, Any]) -> tuple[int, RouteResult | None]:
    """The app's own window, brought forward (guarded): ``(hwnd, None)`` or ``(0, a result: not open, refused...)``."""
    hwnd = _app_hwnd(data)
    if not hwnd:
        return 0, RouteResult(ran=False, outcome="not_open")
    if _refused("focus", hwnd=hwnd):
        return 0, RouteResult(ran=False, held="game", outcome="refused")
    result = _focus(hwnd)
    if _held_by_game(result):
        return 0, RouteResult(ran=False, held="game", outcome="refused")
    if not getattr(result, "ok", False):
        return 0, RouteResult(ran=False, outcome="no_focus")
    return hwnd, None


# ---------------------------------------------------------------------------------------------------------------------
# message
# ---------------------------------------------------------------------------------------------------------------------


def _text_mode(values: Mapping[str, Any]) -> tuple[str, bool]:
    text = _words(values.get("text"))
    return text, bool(text) and str(values.get("send") or "") == "send"


def _where(values: Mapping[str, Any], app: str) -> str:
    return "" if _app_named(values) else f" on {_title(app)}"


def _after_open(call: RouteCall, app: str, data: Mapping[str, Any], target: Target, route: ChatRoute, *, via: str,
                since: float, hwnd: int) -> RouteResult:
    """After the chat was asked to open: wait until it is seen open, then (only then) type the user's words, and press
    send when they plainly said so. The route's result: acted, with its RAM facts for the done-check."""
    text, send = _text_mode(call.values)
    name = target.name
    where = _where(call.values, app)
    chat, ev = _wait_chat(call, data, route, since, hwnd)
    facts: dict[str, Any] = {"via": via, "chat": {True: "open", False: "other", None: "unknown"}[chat],
                             "by": str(ev.get("by") or ev.get("board") or ev.get("status") or ""),
                             "mode": "send" if send else ("draft" if text else "open")}
    ram: dict[str, Any] = {"app": app, "thread_ids": list(route.thread_ids), "chat": chat}
    if not text:
        return RouteResult(ran=True, outcome="opened", app=app, say=SAY_OPENED.format(name=name, where=where),
                           unverified_line=LINE_OPEN_UNCONFIRMED.format(app=_title(app), name=name), facts=facts,
                           facts_ram=ram)
    if chat is not True:
        # never type into a chat that isn't confirmed open: it may be someone else's
        if chat is False and send:
            # review r2 L4: another chat opened and nothing was typed: settled with the driver's own line (never "the
            # PC shows it didn't go through", and not counted as a failed send)
            return RouteResult(ran=True, ask=LINE_UNSURE_CHAT.format(app=_title(app), name=name),
                               outcome="other_chat", app=app, facts=facts, facts_ram={**ram, "typed": False,
                                                                                     "pressed": False})
        return RouteResult(ran=True, outcome="chat_unconfirmed" if chat is None else "other_chat", app=app,
                           unverified_line=LINE_UNSURE_CHAT.format(app=_title(app), name=name), facts=facts,
                           facts_ram=ram)
    # The composer is read before anything is typed (review r1 L6, r2 H1). Exactly these words already there (a draft
    # Yuki typed on an earlier run whose send key was held, kept by the app): never typed a second time; only the send
    # key, if asked. Any other text there (the chat's own kept draft): nothing typed and nothing pressed, in either mode
    # (appended, Enter would send that text with the user's words). Unreadable (no pattern answered, the read timed
    # out, no text field has the keyboard): a draft is typed as before; a send is typed and its send key is never
    # pressed (sWire r1 M1: a hidden kept draft would go out with the words), and the user is told to press Enter.
    # The live check settles whether each app's composer can be read.
    composer = _composer_state(call.ctx.hands, text)
    facts["composer"] = composer
    if composer == "other_text":
        ram.update(typed=False, pressed=False)
        return RouteResult(ran=True, ask=LINE_DRAFT_THERE.format(name=name), outcome="draft_in_the_way", app=app,
                           facts={**facts, "typed": "no", "pressed": False}, facts_ram=ram)
    kept = composer == "same_text"
    blind = send and composer == "unread"
    steps: list[dict[str, Any]] = [] if kept else [{"do": "type", "text": text}]
    if send and not blind:
        steps.append({"do": "keys", "keys": [str(k) for k in data.get("send") or ["enter"]]})
    if steps:
        _ok, run = run_steps(call.ctx.hands, steps, words=call.ctx.words, apps=list(data.get("images") or []),
                             key="message", sends={"to": target.said, "what": text} if send and not blind else None)
    else:
        run = {"steps": 0, "steps_done": 0, "results": []}
    facts["kept_draft"] = kept
    typed = True if kept else _step_verdict(run, "type")
    if typed is None and not kept and run.get("refused_steps"):
        typed = False
    # review r1 M1: the send key counts as pressed once it went out (its own step dispatched), never from the run's
    # overall ok (a screen watch that saw no change after Enter makes the run "not ok", yet the message may be gone)
    pressed = bool(send and _dispatched(run, "keys"))
    ram.update(typed=typed, pressed=pressed)
    facts.update(typed={True: "yes", False: "no", None: "unknown"}[typed], pressed=pressed,
                 steps=int(run.get("steps") or len(steps)), steps_done=int(run.get("steps_done") or 0))
    if send and not pressed and run.get("held") and _dispatched(run, "type"):
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
    if blind:
        # sWire r1 M1: a composer that couldn't be read; the send key was never pressed. Told, not asked, either way
        if typed is not False and _dispatched(run, "type"):
            return RouteResult(ran=True, ask=LINE_TYPED_UNREAD.format(name=name), outcome="typed_unread", app=app,
                               facts={**facts, "held_at": "composer"}, facts_ram=ram)
        ram.update(typed=False)
        return RouteResult(ran=True, ask=LINE_NOT_TYPED.format(name=name), outcome="not_typed", app=app,
                           facts={**facts, "typed": "no"}, facts_ram=ram)
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
    if _refused("open_url"):
        return RouteResult(ran=False, held="game", outcome="refused")
    since = time.time()
    opened = _open_uri(uri)
    if _held_by_game(opened):
        return RouteResult(ran=False, held="game", outcome="refused")
    if not getattr(opened, "ok", False):
        return RouteResult(ran=False, outcome="not_opened")
    return _after_open(call, app, data, target, route, via="link", since=since, hwnd=_app_hwnd(data))


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
    hwnd, failed = _focus_app(data)
    if failed is not None:
        return failed
    since = time.time()
    ok, run = _run_switch(call, hands, steps, apps=list(data.get("images") or []), key="message", way_text=way_text)
    done = int(run.get("steps_done") or 0)
    stop = _stopped(run, app, acted=done > 0)
    if stop is not None:
        return stop
    if run.get("switcher") == "unseen":
        # review r1 L1: the shortcut went but no other field took the keyboard: nothing typed, nothing sent
        return RouteResult(ran=False, outcome="switcher_unseen", facts={"steps": len(steps), "steps_done": done})
    if done == 0:
        return RouteResult(ran=False, outcome="switcher_failed", facts={"steps": len(steps)})
    return _after_open(call, app, data, target, route, via="switcher", since=since, hwnd=hwnd)


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
            if ram.get("chat") is False or ram.get("typed") is False:
                return False, {"status": "not_sent"}
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
            return None, {"status": "unconfirmed"}
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


def _judged(hands: Any, hwnd: int, image: str, role: str, name: str) -> str | None:
    """Yuki's own check on pressing the app's button (a join is judged like a send, rule 8): ``None`` allowed,
    :data:`HANDED_OVER` when the consent rule handed it to the planner, else what to ask or say."""
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


def _current_calls(memory: Any) -> list[str] | None:
    """The app keys of the calls memory sees going on now (``MemoryClient.current_calls``: an app using the microphone
    as a call), or ``None`` when it can't say."""
    from yuki.skills.engine import _mem

    if memory is None:
        return None
    calls = _mem(memory, "current_calls", lambda c: c.current_calls(), timeout_s=0.3)
    if not isinstance(calls, list):
        return None
    return [k for k in (_app_key(c.get("process") or c.get("app")) for c in calls if isinstance(c, Mapping)) if k]


def _call_question(call: RouteCall, data: Mapping[str, Any], app: str, name: str, mic_before: bool | None
                   ) -> tuple[str, str] | None:
    """The question asked before a join while a call is live (review r2 M2, L1), or ``None`` (no call live). Asked
    here, in the rung, so it comes after the engine's game check (a game in front is "after the round?", never this
    question). The live call is this app's own (it holds the microphone now, or memory's open calls name it): "Switch
    to {channel}?" (the app leaves its current channel by itself). Another app's, or one memory can't name: "Join
    {channel} on {app} as well?" (no route leaves another app's call, so none is promised). Words from the app's data
    (``[chat.asks]``). ``(question, code word)``."""
    from yuki.skills.engine import _in_call

    keys = {_app_key(p) for p in data.get("processes") or []}
    calls = _current_calls(call.ctx.memory) or []
    own = mic_before is True or any(k in keys for k in calls)
    try:
        live = bool(_in_call(call.ctx.hands, call.ctx.memory))
    except Exception:  # noqa: BLE001
        live = False
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
    if call.values.get("join_in_call") is False:
        return RouteResult(ran=False, ask=LINE_STAY, outcome="declined")
    image = str((data.get("images") or [""])[0])
    # the microphone before anything is done: the question's "is it Discord's own call", and the onset's baseline
    before = _mic_held(image)
    if call.values.get("join_in_call") is not True:
        asked = _call_question(call, data, app, name, before)
        if asked is not None:
            return RouteResult(ran=False, ask=asked[0], outcome="ask_in_call", facts={"call": asked[1]})
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
                          way_text=query)
    done = int(run.get("steps_done") or 0)
    stop = _stopped(run, app, acted=done > 0)
    if stop is not None:
        return stop
    if run.get("switcher") == "unseen":
        # review r1 L1: the switcher never took the keyboard: the channel's name is never typed into a chat
        return RouteResult(ran=False, outcome="switcher_unseen", facts={"steps": len(steps), "steps_done": done})
    if done == 0:
        return RouteResult(ran=False, outcome="switcher_failed", facts={"steps": len(steps)})
    joined = _wait_mic(call.ctx, image, before, VOICE_WAIT_S)
    pressed = ""
    if not joined and before is not False:
        # review r2 L5: no onset can show (the microphone unreadable, or Discord already held it): the switcher's
        # channel still gets the same time to open before "Join Voice" is pressed (never the channel in view before)
        call.ctx.sleep(VOICE_WAIT_S)
    if not joined:
        label = ((data.get("labels") or {}).get("en") or {}).get("join_voice")
        if isinstance(label, list) and len(label) >= 2:
            role, button = str(label[0]), str(label[1])
            question = _judged(hands, hwnd, image, role, button)
            if question == HANDED_OVER:
                from yuki.skills.engine import TO_PLANNER

                return RouteResult(ran=True, outcome=TO_PLANNER, app=app)
            if question is not None:
                return RouteResult(ran=True, ask=question, outcome="held_by_check", app=app)
            if _refused("click", hwnd=hwnd):
                return RouteResult(ran=True, held="game", outcome="refused", app=app)
            pressed = _press(hwnd, role, button)
            if pressed == "pressed":
                joined = _wait_mic(call.ctx, image, before, JOIN_WAIT_S)
    if joined:
        _note_channel(call.ctx, name)
    return RouteResult(ran=True, outcome="joined" if joined else "opened", app=app,
                       say=SAY_JOINED.format(channel=name, app=_title(app)),
                       unverified_line=LINE_VOICE_UNCONFIRMED.format(channel=name),
                       facts={"onset": joined, "join_button": pressed or "not_needed", "steps_done": done,
                              "mic_before": {True: "held", False: "free", None: "unknown"}[before]})


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
           "pick_route", "deep_link", "switcher_query", "run_steps", "chat_state", "route_open_link",
           "route_switcher", "route_voice_join", "probe_message_done", "sent_in_chat"]
