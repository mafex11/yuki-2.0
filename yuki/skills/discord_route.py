"""``discord.voice`` by server (kHands): a voice channel memory saw in the app, found by its server and/or its name,
opened by the app's own deep link, then joined (yuki/skills/connections/discord.toml, rung ``link``).

The data is the app's own (``[chat]`` in its connection file: its processes, its voice-channel label, the window
title's place separator, its link shapes); which servers and channels exist is what memory's capture saw. The join is
the same as after the quick switcher (:func:`join_tail`: the microphone's onset, else the app's own "Join Voice",
judged like a send), asked first when a call is live (:func:`in_call_gate`), never with a game in front.

The join rule (redteam D10, review kHands r1 H1/M2): only a channel memory saw, only on the user's plain words. A
channel joins straight away when their words name it whole, sound like its name, or name part of it and it is the
only channel there or the one they join most (their picked tastes only); a server alone joins its one known channel,
or the one they join most there. A server matched only by its letters ("SKT" for "Skate Club") never decides a join by
itself: the channel is asked by name. Everything else is the one question which.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from yuki.skills import chat as C_
from yuki.skills.engine import RouteCall, RouteResult

#: Said when the user named a server and memory knows no voice channel there (never a guess).
LINE_NO_SERVER_VOICE = "I don't know a voice channel in {server} on {app}."
#: Asked when their words fit several known voice channels, or fit one only in part (the one question for a join).
ASK_WHICH_VOICE = "Which one: {names}?"
#: Asked when the server was matched only by its letters and one channel sits there: the join waits for their yes.
ASK_JOIN_THERE = "Join {channel} in {server}?"


@dataclass
class VoiceChannel:
    """A voice channel memory saw in the app: its name, the place it sits in (Discord's server), its address (a
    capture thread's URL), when last seen and how many of memory's rows name it. RAM only."""

    name: str
    place: str
    url: str
    seen: float = 0.0
    rows: int = 0


def voice_channels(store: Any, data: Mapping[str, Any]) -> list[VoiceChannel] | None:
    """The voice channels memory saw in the app (its own keys, ``processes``): capture threads whose scope is the app's
    voice-channel label and the channel's name ("Voice Channel lounge") and that carry the channel's address; the
    place is read from the window title by the app's own separator (``place_sep``: Discord's "lounge | <server>").
    One per address, the newest name. ``None`` when memory's threads can't be read."""
    from yuki.memory import contacts as C

    keys = sorted({C_._app_key(p) for p in data.get("processes") or []} - {""})
    labels = C_.voice_labels(data)
    sep = str(data.get("place_sep") or "")
    if not keys or not labels:
        return []
    marks = ",".join("?" * len(keys))
    try:
        rows = store._query(f"SELECT title_ciphertext, scope_ciphertext, url_ciphertext, last_seen FROM threads "
                            f"WHERE process IN ({marks}) AND merged_into IS NULL ORDER BY last_seen DESC", keys)
    except Exception:  # noqa: BLE001
        return None
    found: dict[str, VoiceChannel] = {}
    places: dict[str, str] = {}
    for title_b, scope_b, url_b, seen in rows:
        try:
            title = str(store._dec(title_b) or "") if title_b else ""
            scope = str(store._dec(scope_b) or "") if scope_b else ""
            url = str(store._dec(url_b) or "") if url_b else ""
        except Exception:  # noqa: BLE001 - a row the key can't read
            continue
        if not url:
            continue
        place = title.rsplit(sep, 1)[1].strip() if sep and sep in title else ""
        if place and url not in places:
            places[url] = place
        words = C.tokens(scope)
        label = next((lab for lab in labels if words[:len(lab)] == lab and len(words) > len(lab)), None)
        if label is None:
            continue
        name = " ".join(scope.split()[len(label):]).strip()
        got = found.get(url)
        if got is None:
            found[url] = VoiceChannel(name=name, place=place, url=url, seen=float(seen or 0.0), rows=1)
        else:
            got.rows += 1
    for url, vc in found.items():
        vc.place = vc.place or places.get(url, "")
    return sorted(found.values(), key=lambda v: -v.seen)


def _skeleton(word: str) -> str:
    """A word's consonants in order (letters only, casefolded): how a name said as its letters ("SKT") stands for a
    spoken one ("Skate"). A fixed spelling rule over names, never a meaning."""
    return "".join(ch for ch in word.casefold() if ch.isalpha() and ch not in "aeiouy")


def _place_named(said: str, places: Sequence[str]) -> tuple[list[str], str]:
    """``(places, how)``: the places (servers) the user's words name and how: ``held`` (the name holds their words,
    whole and in order), ``sound`` (it sounds like them: :func:`yuki.skills.taste.heard_as`) or ``letters`` (a said
    token that is only consonants, "skt", is the consonants of one of the name's words, "Skate": a name said as its
    letters; a plain word such as "the" never is). ``([], "")``: none. Identity of names only."""
    from yuki.memory import contacts as C
    from yuki.skills import taste

    want = C.tokens(said)
    pool = [p for p in dict.fromkeys(places) if p]
    if not want or not pool:
        return [], ""
    held = [p for p in pool if C.span_in(want, p)]
    if held:
        return held, "held"
    alike = taste.heard_as(pool, said)
    if alike:
        return [alike], "sound"
    keys = {w for w in want if len(w) >= 2 and w.isalpha() and w == _skeleton(w)}
    if not keys:
        return [], ""
    lettered = [p for p in pool if keys & {_skeleton(w) for w in C.tokens(p) if len(_skeleton(w)) >= 2}]
    return lettered, ("letters" if lettered else "")


def _which(pool: Sequence[VoiceChannel], first: Sequence[VoiceChannel] = ()) -> str:
    """The one question which, naming at most three channels (``first`` before the rest; the server too when they sit
    in several)."""
    shown: list[VoiceChannel] = []
    for v in [*first, *pool]:
        if all(v.url != s.url for s in shown):
            shown.append(v)
    places = {v.place for v in shown}
    names = [f"{v.name} in {v.place}" if len(places) > 1 and v.place else v.name for v in shown[:3]]
    return ASK_WHICH_VOICE.format(names=", ".join(names[:-1]) + (" or " if len(names) > 1 else "") + names[-1])


def pick_voice_channel(found: Sequence[VoiceChannel], channel: str, place: str, usual: Sequence[str] = ()
                       ) -> VoiceChannel | str | None:
    """The one known voice channel the user's words name (``channel`` and/or ``place``, their words), a question
    (``str``) when it isn't settled, or ``None`` when none fits. ``usual``: the channels they joined (picked tastes,
    best first; never names only seen).

    Joined straight away: a channel their words name whole or by sound; one they name in part that is the only channel
    in the pool or the one of those they join most; with no channel said, a server's only channel or the one they join
    most there. A server matched only by its letters never settles the channel by itself (asked by name: H1)."""
    from yuki.memory import contacts as C
    from yuki.skills import taste

    def liked(among: Sequence[VoiceChannel]) -> list[VoiceChannel]:
        return [v for v in among if any(C.span_in(C.tokens(u), v.name) for u in usual)]

    pool = list(found)
    how = ""
    if place:
        named, how = _place_named(place, [v.place for v in pool])
        pool = [v for v in pool if v.place in set(named)]
        if not pool:
            return None
    if channel:
        want = C.tokens(channel)
        whole = [v for v in pool if C.tokens(v.name) == want]
        if len(whole) == 1:
            return whole[0]
        if whole:
            return _which(whole)
        alike = taste.heard_as([v.name for v in pool], channel)
        if alike:
            return next(v for v in pool if v.name == alike)
        part = [v for v in pool if C.span_in(want, v.name)]
        if not part:
            return None
        # M2: words that cover only part of a name ("VC" of "vc for venting") join only when nothing else is there to
        # mean, or it is the one of them they join most; else asked, that channel first
        if len(part) == 1 and (len(pool) == 1 or liked(part)) and how != "letters":
            return part[0]
        picked = liked(part)
        if len(picked) == 1 and how != "letters":
            return picked[0]
        return _which(pool, first=part)
    if how == "letters":
        # H1: a server matched by its letters alone is never joined on that match: the channel is asked by name
        if len(pool) == 1:
            return ASK_JOIN_THERE.format(channel=pool[0].name, server=pool[0].place)
        return _which(pool)
    if len(pool) == 1:
        return pool[0]
    picked = liked(pool)
    if len(picked) == 1:
        return picked[0]
    return _which(pool, first=picked)


def _voice_link(data: Mapping[str, Any], url: str) -> str | None:
    """The app's own deep link for a channel's address (the first of its ``links`` whose URL shape matches)."""
    return C_.deep_link(data, C_.ChatRoute(app="", kind="channel", chat="", handle=None, thread_ids=[], urls=[url]))


def in_call_gate(call: RouteCall, data: Mapping[str, Any], app: str, name: str, before: bool | None
                 ) -> RouteResult | None:
    """The in-call question before a join (``chat._call_question``), or ``None`` when the join may go ahead. A "no"
    with no call live is no answer (lMsg, live 17 Oct): the join goes ahead."""
    answer = call.values.get("join_in_call")
    if answer is True:
        return None
    asked = C_._call_question(call, data, app, name, before)
    if asked is None:
        return None
    if answer is False:
        return RouteResult(ran=False, ask=C_.LINE_STAY, outcome="declined")   # their "no": their call stays as it is
    return RouteResult(ran=False, ask=asked[0], outcome="ask_in_call", facts={"call": asked[1]})


def join_tail(call: RouteCall, data: Mapping[str, Any], app: str, hwnd: int, image: str, before: bool | None,
              name: str, facts: dict[str, Any]) -> RouteResult:
    """After a voice channel was opened: the app taking the microphone, else its own "Join Voice" button (its label
    from the data, judged like a send), then the onset again. A join that took is noted in tastes. The same tail as
    ``chat.route_voice_join``'s after its switcher."""
    hands = call.ctx.hands
    joined = C_._wait_mic(call.ctx, image, before, C_.VOICE_WAIT_S)
    pressed = ""
    if not joined and before is not False:
        # review r2 L5: no onset can show (the microphone unreadable, or the app already held it): the channel still
        # gets the same time to open before "Join Voice" is pressed (never the channel in view before)
        call.ctx.sleep(C_.VOICE_WAIT_S)
    if not joined:
        label = ((data.get("labels") or {}).get("en") or {}).get("join_voice")
        if isinstance(label, list) and len(label) >= 2 and hwnd:
            role, button = str(label[0]), str(label[1])
            question = C_._judged(hands, hwnd, image, role, button)
            if question == C_.HANDED_OVER:
                from yuki.skills.engine import TO_PLANNER

                return RouteResult(ran=True, outcome=TO_PLANNER, app=app)
            if question is not None:
                return RouteResult(ran=True, ask=question, outcome="held_by_check", app=app)
            if C_._refused("click", hwnd=hwnd):
                return RouteResult(ran=True, held="game", outcome="refused", app=app)
            pressed = C_._press(hwnd, role, button)
            if pressed == "pressed":
                joined = C_._wait_mic(call.ctx, image, before, C_.JOIN_WAIT_S)
    if joined:
        C_._note_channel(call.ctx, name)
    return RouteResult(ran=True, outcome="joined" if joined else "opened", app=app,
                       say=C_.SAY_JOINED.format(channel=name, app=C_._title(app)),
                       unverified_line=C_.LINE_VOICE_UNCONFIRMED.format(channel=name),
                       facts={**facts, "onset": joined, "join_button": pressed or "not_needed",
                              "mic_before": {True: "held", False: "free", None: "unknown"}[before]})


def _usual(store: Any) -> list[str]:
    """The voice channels the user joined (picked tastes ``discord_voice``, best at this hour first; never a name only
    seen)."""
    from yuki.skills import taste

    return [t.name for t in taste.top(store, "discord_voice", n=20) if t.name]


def route_voice_link(call: RouteCall) -> RouteResult:
    """``discord.voice``'s first rung: a voice channel memory saw in the app, found by the server and/or the channel the
    user named (:func:`pick_voice_channel`), asked first when a call is live, opened by the app's own deep link (the
    guarded open), then joined (:func:`join_tail`). Not settled: asked by name. None known: the next rung (the
    switcher by the channel's name), or, with a server named and no channel, said plainly."""
    from yuki.skills.engine import _mem, _store_of

    app = str(call.args.get("app") or "")
    data = C_.app_data(app)
    if not data:
        return RouteResult(ran=False, outcome="no_app_data")
    channel = call.values.get("channel")
    server = call.values.get("server")
    name = C_._words(channel.get("name") if isinstance(channel, Mapping) else channel)
    place = C_._words(server.get("name") if isinstance(server, Mapping) else server)
    if not name and not place:
        return RouteResult(ran=False, ask="Which voice channel?", outcome="no_channel")
    if call.ctx.memory is None:
        return RouteResult(ran=False, outcome="no_memory")
    found = _mem(call.ctx.memory, "voice_channels", lambda c: voice_channels(_store_of(c), data), timeout_s=0.8)
    if not isinstance(found, list):
        return RouteResult(ran=False, outcome="no_memory")
    usual = _mem(call.ctx.memory, "voice_usual", lambda c: _usual(_store_of(c)), timeout_s=0.5) or []
    pick = pick_voice_channel(found, name, place, usual)
    if isinstance(pick, str):
        return RouteResult(ran=False, ask=pick, outcome="ask_which", facts={"known": len(found)})
    if pick is None:
        if place and not name:
            return RouteResult(ran=False, ask=LINE_NO_SERVER_VOICE.format(server=place, app=C_._title(app)),
                               outcome="unknown_channel", facts={"known": len(found)})
        return RouteResult(ran=False, outcome="not_known", facts={"known": len(found)})
    uri = _voice_link(data, pick.url)
    if not uri:
        return RouteResult(ran=False, outcome="no_link")
    image = str((data.get("images") or [""])[0])
    before = C_._mic_held(image)
    gate = in_call_gate(call, data, app, pick.name, before)
    if gate is not None:
        return gate
    if C_._refused("open_url"):
        return RouteResult(ran=False, held="game", outcome="refused")
    # fMsg: with Discord already running only its window is waited for (the join is checked by the microphone)
    opened = C_._open_uri(uri, running=bool(C_._app_hwnd(data)))
    if C_._held_by_game(opened):
        return RouteResult(ran=False, held="game", outcome="refused")
    if not getattr(opened, "ok", False):
        return RouteResult(ran=False, outcome="not_opened")
    return join_tail(call, data, app, C_._app_hwnd(data), image, before, pick.name, {"known": len(found), "by": "link"})


ROUTES: dict[str, Callable[[RouteCall], RouteResult]] = {"voice_link": route_voice_link}

__all__ = ["ROUTES", "VoiceChannel", "voice_channels", "pick_voice_channel", "route_voice_link", "join_tail",
           "in_call_gate"]
