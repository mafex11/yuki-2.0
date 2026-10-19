"""Browser drivers: a site's tab brought forward, a web page or a picked video opened, a chat's own web address
(the drivers of ``browser.site_tab``, ``web.open_page``, ``web.open_video`` and ``message``'s ``web_link`` rung;
phase L design §4.4-§4.5, §5, §6; PLAN.md decisions D0, D8, D16, D17, D19).

Their connection files (``connections/browser_tabs.toml``, ``connections/web.toml``, ``connections/message.toml``) name
these calls; the closed verbs can't express them, because each needs a fact read at run time: the tab is found by the
tab census, and the browser is chosen when the request runs. Nothing here names a user's site, service or browser:
those come from the slots (the user's words, or a learned skill's bound values) and from memory's own measurements.

**browser.site_tab** (:func:`route_site_tab`). A site's tab that is already open is found by identity only, never by
its title (D8): memory learned its RuntimeId, its name digests and its pinned place on every sighting of the site
(``nudge_state['site_tabs:<service>']``, :class:`yuki.memory.callevidence.SiteTabs`), with the browser it was seen in,
and the census reads that browser's windows' tabs (every group, at most 0.2 s a window), digests each name and drops
it, and ranks the tabs against those identities (:func:`yuki.perception.tabs.read_census`). The service is the slot's
value matched to the product names memory keeps identities under (exactly, else the one whose words hold every word
said: identity of names, never meaning). Then:

* the strip can't be read (no tab at all): Yuki says so and does nothing;
* part of it wasn't read (a window's walk cut short at its 0.2 s, a window that read nothing; the census's per-window
  diagnostics) and no single tab was found: Yuki says it couldn't read all the tabs and does nothing, not even a told
  link (review r2 M1);
* none matches on a strip read whole: "There's no <service> tab open in <browser>" (or, with a link the user told, the
  next rung opens it in the means browser);
* two match equally: Yuki asks which, counted along the strip, and the user's answer comes back as the ``pick`` slot;
* one: that tab is selected by its RuntimeId (``yuki.actions.tabs.tab_action``: a UI Automation pattern, no key, no
  pointer; a candidate that won't select gives way to the next one), then the browser comes forward (the guarded
  ``focus_window``). The user does the act (Join). Done when the browser is in front on that very tab
  (:func:`probe_site_tab_shown`).

**web.open_page / web.open_video** (:func:`route_open_page`, :func:`route_open_video`). The address comes from the
site files (``yuki/connect/sites/*.toml``: a site's front page, or a page kind's template with the item's value) or,
for a host-scope page, is the registrable domain's front page. The browser (:func:`choose_browser`; design §5, critique
M8: no ranked rule is shipped ahead of the user's own): the one the user named (it stands in their words); else the
slot's value the resolver filled (their usual, from preferences); else the fill policy a learned skill carries
(``means_policy`` in its route arguments: :data:`BROWSER_POLICIES`, chosen nightly by the learner on that skill's own
occasions); else the browser they use most; else Windows' default handler. "A browser" is measured, never a list: a
program whose own timeline rows (not Yuki's) of the last 14 days showed pages of at least 3 hosts, or a page of the
site itself (a chat app that draws its one site in its own window shows one or two). The address is a launch argument
(``launch.open_url``: nothing typed). A page opens and nothing else: nothing plays until the user picks. A picked video
(theirs, or Yuki's pick when they asked it to choose: ``yuki.skills.taste.choose``) opens its own link and is checked by
the page in front showing that video and the browser's media session playing. A
browser the user didn't name is named in the done line, so one word corrects it.

x8WebArc (10 Oct: "open YouTube in Arc" opened nothing new in Arc five times, and once "verified" an old tab):

* **A browser named anywhere in the exchange wins** (:func:`choose_browser`): their words are the request's whole
  exchange, and a measured browser whose name stands in them beats their usual and any learned fill policy. Two
  different browsers named and the slot naming neither: Yuki asks which, never guesses.
* **The ladder is protocol, then the address bar, then the hand-off.** Rung A hands the address over as a launch
  argument (``launch.open_url``: a packaged browser by protocol activation, how a running browser takes a link), within
  the request's deadline. A browser that kept the page it had (``page_unchanged``), or one whose protocol activation
  Windows refused that came up showing no page at all (``page_blank``: a cold Arc), did not open it: that rung did not
  run, and the next one does. The address-bar rung (:func:`route_address_bar`) types the address into the chosen
  browser's own address field in its own window (by hwnd): a new tab (the route's ``new_tab`` keys), the address typed
  and read back, Enter as navigation; only into a text field of the browser's own frame (never a page's own box, so
  never a send), checked by structure, not by name. When it doesn't get there either, the engine hands off.
* **The done-check reads the browser the rung opened the page in** (:func:`_page_window`: the window by hwnd, else that
  browser's top window), never the window in front while that window is known: on 10 Oct it read the window in front
  (Arc's command bar, or Yuki's own) 19 times and never saw a page. Another page there is False (the rung counts as
  contradicted and the next one runs); a page that window already showed before the rung, which the rung itself never
  saw arrive, is no proof either (False). Both rungs read every window's page before they act and count only a page
  newly at the address (review r1 H1); a window that already showed the very page asked for, and still does, is
  "already" (review r1 M1), said only once the done-check sees it, never a fresh "done".
* **A browser that ignored the last two hand-offs** (kept the very page it had) is not handed the next one for a while
  (RAM, this process; review r1 L1): its address-bar rung runs at once.

lSkills (live 17 Oct: every Arc open failed with protocol activation's ``0x80270254``, then the address-bar rung said
"no window" in 96 ms with Arc up, and Yuki opened Excalidraw in Chrome):

* rung A's hand-off goes on through the browser's alias when Windows refuses its protocol activation (inside
  ``launch``: never "not installed"), and asks ``launch`` for the hand-off only (``address_bar=False``): typing is this
  skill's next rung;
* the address-bar rung finds the browser's window by the Start menu app's identity when no window of its program is
  listed (:func:`_bar_window`: a window hidden in the background is shown, a browser with none is brought up), and logs
  where it came from (``how``) or, with none, how many of its processes run (``procs``);
* **the user's own recent browsing outweighs old counts** (habits: Arc -> Chrome on 11 Oct): "the one used most" and
  the hour band weigh each second by its age (:data:`BROWSER_HALF_LIFE_DAYS`), and a browser no longer in use
  (:func:`fresh_keys`) fills nothing by them. Yuki's own opens still count for nothing (bRound2).

**message, web_link** (:func:`route_chat_web_link`). A chat whose captured address the chat app's own ``[chat.links]``
data matches (critique L4: an address is a route only when the app's link data recognises it) opens at that address in
a browser (the one that address's site was last shown in, unless a learned policy says otherwise). It only opens the
chat: with words to type it never runs, so it can never send.

Every route here takes focus: never with a game in front (the engine holds it as "after the round?"), and each focus or
opened link goes through ``gamegate.refuse_input`` (the input layer refuses it too). Logs are content-free: the facts
each route returns are code words and counts (``browser_from``, ``decision``, ``match``, ``tabs``); names, titles,
services and links stay in RAM.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Mapping, Sequence

from yuki.skills.connection import domain_like
from yuki.skills.engine import DONE_AT, RouteCall, RouteResult

#: What Yuki says when it does nothing for a site's tab (asked or told plainly; never a "done"). Templates: the service
#: as memory keeps its product name, the browser as people say it.
LINE_TAB_UNREADABLE = "I can't read {browser}'s tabs right now, so I left it as it is."
#: Some of the browser's tabs were read, not all (a window's walk cut short, or a window that read nothing): the tab may
#: be in the part not read, so "none" is never said and no told link is opened (review r2 M1).
LINE_TAB_PARTIAL = "I couldn't read all of {browser}'s tabs, so I left it as it is."
LINE_TAB_NONE = "There's no {service} tab open in {browser}."
LINE_TAB_UNKNOWN = "I don't know your {service} tab yet. Bring it up once and I'll find it next time."
LINE_BROWSER_CLOSED = "{browser} isn't open, so there's no {service} tab to bring up."
LINE_TAB_NO_ID = "I found your {service} tab, but {browser} didn't let me select it, so I left it."
ASK_TAB_WHICH = "I see {n} {service} tabs in {browser}. Which one, counting along the tab list: {choices}?"
LINE_TAB_UP = "{service} is up in {browser}."
#: A web page (``{site}``: the site file's name or the domain as the user hears it).
LINE_PAGE_UP = "{site} is up."
LINE_VIDEO_UP = "Playing it."
LINE_CHAT_WEB = "Your chat with {name} is open in {browser}."
#: Review r1 M1: the browser window already showed the very page asked for (and still does): said as "already", and
#: only once the engine's done-check saw it (never a fresh "done"; a video only once it plays).
LINE_PAGE_ALREADY = "{site} is already up."
LINE_VIDEO_ALREADY = "It's already playing."
LINE_CHAT_WEB_ALREADY = "Your chat with {name} is already open in {browser}."
#: x8WebArc: two different browsers named in the exchange and the slot naming neither (``{choices}``: "Arc or Chrome").
ASK_BROWSER = "Which browser, {choices}?"
ORDINALS = ("the first", "the second", "the third", "the fourth")
#: The candidates tried in identity order when one won't select (D8: each checked, the next one after a miss).
TAB_TRIES = 2
#: Where memory keeps a site's tab identities (``SiteTabs.state_key``: ``site_tabs:<service>``).
TAB_STATE_PREFIX = "site_tabs:"

#: A browser is a program whose timeline rows carried web pages within this many days, of at least this many hosts (or
#: a page of the site itself): measured on the user's own memory, a chat app that draws one site shows one or two.
BROWSER_DAYS = 14
BROWSER_MIN_HOSTS = 3
#: "The browser you're using": in front now, or last in front at most this long ago (s); one fill policy among
#: :data:`BROWSER_POLICIES`, never shipped ahead of the user's own (design §5).
RECENT_S = 30 * 60.0
#: The fill policies a learned skill's means slot may carry (design §5: scored nightly on that skill's own occasions,
#: the best kept only when it beats "most" by 5 points): ``front_or_recent`` the measured browser in front, or the last
#: one in front at most :data:`RECENT_S` ago; ``band`` the one used most in this weekday/weekend hour band; ``site`` the
#: one the site was last shown in; ``most`` the one used most overall (the baseline, and the cold-start fill).
BROWSER_POLICIES: tuple[str, ...] = ("front_or_recent", "band", "site", "most")
#: The hour band (h either side of now) and look-back (days) of the ``band`` policy.
BAND_H = 1
BAND_DAYS = 28
#: lSkills (live 17 Oct; habits: Arc -> Chrome on 11 Oct): the user's own recent browsing outweighs old observation
#: counts. A second of browsing counts ``0.5 ** (age / BROWSER_HALF_LIFE_DAYS)`` in "the one used most" and in the hour
#: band (``measured_browsers``' ``weight``, ``band_browser``), and a browser whose own page rows stopped
#: ``BROWSER_STALE_DAYS`` before the newest row of the browser used last (relative, so a week away from the PC makes
#: none stale) fills nothing by ``most`` or ``band`` (:func:`fresh_keys`); named, the user's usual, ``site`` (the
#: browser the site itself was last shown in: Excalidraw's drawings live in Arc) and front/recent are untouched. On the 17 Oct
#: copy the weekend 03-05 h band still read Arc (Arc's last weekend nights) four days after the switch.
BROWSER_HALF_LIFE_DAYS = 3.5
BROWSER_STALE_DAYS = 4.0
#: How much a done-check's front-window read may take (one UIA job on its own thread).
FRONT_READ_S = 0.5
#: x8WebArc: the least time a rung's page open gets, however much of the request's budget the resolve and an earlier
#: rung spent (the request's 5 s is a p90 target, and the engine lets an acted rung's check run past it too): with less,
#: a browser that did open the page could not be seen doing it.
OPEN_FLOOR_S = 3.0
#: x8WebArc, the address-bar rung: how long it reads the browser's windows' pages before it acts (review r1 H1: every
#: window it later looks in, at most ``BAR_BEFORE_WINDOWS`` of them, its own window first, each for at most
#: ``BAR_BEFORE_WINDOW_S``), how long the frame-field check may take, how long it watches for the page after Enter (at
#: least ``BAR_PAGE_MIN_S``), and the least time the whole rung gets however much of the request's budget the rungs
#: before it spent.
BAR_BEFORE_S = 0.8
BAR_BEFORE_WINDOWS = 4
BAR_BEFORE_WINDOW_S = 0.35
FIELD_READ_S = 1.5
#: fSkills: the frame-field reads that may settle (focus not yet on an element, or the climb cut short), read once more
#: after this long; a timeout or a failed read is not retried (review L3).
FIELD_SETTLING = ("no_focus", "climb_cut")
FIELD_SETTLE_S = 0.2
BAR_PAGE_S = 4.0
BAR_PAGE_MIN_S = 1.5
BAR_FLOOR_S = 8.0
#: fSkills: the done-check's second reader of a window's page (the rungs' page-only pass) when the Documents' read
#: found none (Arc), and how many of a browser's windows have their selected tab read before the open rung acts.
PAGE_READ_S = 0.6
TABS_BEFORE_WINDOWS = 1
#: How long the window a rung opened a page in is kept for the next rung of the same run (RAM only).
NOTE_KEEP_S = 30.0
#: Review r2 H1: how far back "a browser Yuki itself just acted in" reaches (:func:`_just_acted`), before the request
#: began: Item 6's window (``yuki.skills.resolve.JUST_ACTED_S``; the exchange's turns span about 120 s). A request
#: began at most ``REQUEST_BACK_S`` before its run (its first ledger row of the turn).
JUST_ACTED_S = 180.0
REQUEST_BACK_S = 600.0
#: Review r1 L1: a browser that kept the very page it had after an address was handed to it (``page_unchanged``, state
#: ``same``) this many times in a row is not handed one by web.open_page / web.open_video again for ``IGNORED_KEEP_S``
#: (RAM, this process): their address-bar rung runs at once instead of after the whole wait. One page that opens newly
#: clears it; after ``IGNORED_KEEP_S`` the hand-off is tried once more. Two, not one: a single slow page must not turn
#: a working browser's link path into keystrokes.
IGNORED_AFTER = 2
IGNORED_KEEP_S = 4 * 3600.0
#: UI Automation control type ids (``UIA_*ControlTypeId``): a page's document, and the text fields an address or
#: command bar is (an edit box, or an editable combo box).
UIA_DOCUMENT = 50030
UIA_EDIT = 50004
UIA_COMBOBOX = 50003
#: How far up from the focused element the frame-field check climbs (the desktop root is reached long before).
CLIMB_MAX = 60
#: bRound2: the user's choice of browser is measured on their own timeline rows, never on a page Yuki itself opened.
#: ``by_yuki = 0`` alone left in the stretch right after Yuki's act ended, on the page it brought up: on 10 Oct at
#: 21:10:12 Yuki opened YouTube in Chrome (the default handler), and the next 48 s on that very page (the user looking
#: at the wrong browser while saying so) counted as their own, so from then on "the browser YouTube was last shown in"
#: (the learned skill's ``site`` policy) and "the one used most" read Chrome. A row inside a page visit memory
#: attributes to Yuki (``yuki.memory.moments``: a ``page`` moment ``by = 'yuki'``, the visit whose first stretch began
#: while Yuki acted, in the same program) is left out of every measure of the user's browser (:func:`measured_browsers`,
#: :func:`last_in_front`, :func:`band_browser`, and the learner's ``prefs.FillPolicyScorer``). ``{t}``: the outer
#: query's timeline alias. Data only: memory's own visit attribution, no rule about any browser.
#: Yuki's own page visits, read once per query (``MATERIALIZED``: a few hundred rows, so each timeline row is checked
#: against them in memory rather than by a scan of every page moment: the plain correlated form took 0.85 s against
#: 0.05 s and ran past the executor's 0.8 s browser read on 16 Oct)
YUKIS_VISITS_SQL = (
    "WITH yv AS MATERIALIZED (SELECT yt.process AS p, ym.at AS a, coalesce(ym.ended_at, ym.at) AS e FROM moments ym "
    "JOIN timeline yt ON yt.id = ym.source_id WHERE ym.kind = 'page' AND ym.by = 'yuki' AND ym.source = 'timeline') ")
NOT_YUKIS_VISIT_SQL = ("NOT EXISTS (SELECT 1 FROM yv WHERE yv.p = {t}.process AND yv.a <= {t}.started_at "
                       "AND {t}.started_at <= yv.e)")


def own_rows_query(store: Any, sql: str, args: Sequence[Any], *, alias: str = "t") -> list[Any]:
    """``store._query`` of a timeline read (a ``SELECT``) whose ``{own}`` marks where :data:`NOT_YUKIS_VISIT_SQL` goes
    (with ``AND``), for the timeline aliased ``alias``, with Yuki's visits read first (:data:`YUKIS_VISITS_SQL`). A
    database without memory's moments (an old one, a test's) reads as before, ``{own}`` dropped. Raises what the read
    itself raises."""
    clause = " AND " + NOT_YUKIS_VISIT_SQL.format(t=alias)
    try:
        return store._query(YUKIS_VISITS_SQL + sql.replace("{own}", clause), args)
    except Exception as exc:  # noqa: BLE001 - only a missing moments table (or column) falls back
        if "moments" not in str(exc) and "no such column" not in str(exc):
            raise
    return store._query(sql.replace("{own}", ""), args)


# ---------------------------------------------------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------------------------------------------------


def _store(ctx: Any) -> Any:
    """The memory store of the run (the tray's ``MemoryClient.store``, a copy in a replay), or ``None``."""
    from yuki.skills.engine import _mem, _store_of

    memory = getattr(ctx, "memory", None)
    if memory is None:
        return None
    return _mem(memory, "skill_store", lambda c: _store_of(c), timeout_s=0.5)


def _probe_store(ctx: Any) -> Any:
    """The store a probe may read (through its ``memory_call``), or ``None``."""
    call = getattr(ctx, "memory_call", None)
    if call is None:
        return None
    try:
        return call("skill_store", lambda c: getattr(c, "store", c))
    except Exception:  # noqa: BLE001
        return None


def _log(ctx: Any) -> Callable[..., None]:
    from yuki.skills.engine import _emit

    return lambda event, **fields: _emit(ctx, event, **fields)


def _refused(action: str, hwnd: int | None = None) -> str | None:
    """D17's backstop asked right here, before a focus or an opened link (the input layer asks again): ``None`` (go
    ahead), else the refusal's kind (fHands, ``yuki.actions.gate``): ``"game"`` (held for a game) or ``"blocked"``
    (the gate failed closed, or another guard refused it: the engine's settled stop, never said as a game)."""
    from yuki.actions import gate

    why = gate.refuse(action, hwnd=hwnd)
    return None if why is None else gate.kind_of(why)


def _words(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("name")
    return " ".join(str(value or "").split())


def _app_key(name: Any) -> str:
    from yuki.memory.store import app_key

    return app_key(str(name or "")) or ""


def _browser_title(key: str) -> str:
    """A browser's app key as people say it (``yuki.core.appnames.prettify``: ``"zen"`` -> ``"Zen"``)."""
    if not key:
        return ""
    try:
        from yuki.skills.uia_route import app_words

        return app_words(key) or key
    except Exception:  # noqa: BLE001
        return key if not key.islower() else key.title()


def _app_windows(process: str) -> list[tuple[int, int]]:
    """``[(hwnd, pid)]`` of the user windows of the program whose app key is ``process`` (read only), top first."""
    key = _app_key(process)
    if not key:
        return []
    try:
        from yuki.perception.windows import list_windows

        return [(int(w.hwnd), int(w.pid)) for w in list_windows() if _app_key(w.process_name) == key]
    except Exception:  # noqa: BLE001
        return []


def _front() -> tuple[int, str]:
    """``(hwnd, image)`` of the window in front (read only)."""
    from yuki.skills.donecheck import _front as front

    return front()


def _census(store: Any, service: str, windows: list[tuple[int, int]], log: Callable[..., None]) -> dict[str, Any]:
    """The tab census of ``service``'s tab over ``windows`` (:meth:`SiteTabs.census` on the tray's side: identities
    from memory, names digested with memory's key and dropped, the result content-free and logged ``tab_census``)."""
    from yuki.memory.callevidence import SiteTabs
    from yuki.perception.tabs import read_census

    tabs = SiteTabs(None, store=store, log=log,
                    census_fn=lambda w, ident, digest_fn: read_census(w, ident, digest_fn))
    return tabs.census(service, windows)


def _identities(store: Any, service: str) -> dict[str, Any]:
    from yuki.memory.callevidence import SiteTabs

    return SiteTabs(None, store=store).identities(service)


def _select_tab(hwnd: int, tab_id: str) -> Any:
    from yuki.actions.tabs import tab_action

    return tab_action(int(hwnd), str(tab_id), "select")


def _focus(hwnd: int) -> Any:
    from yuki.actions import launch

    return launch.focus_window(int(hwnd))


def _open_url(url: str, app: str | None, deadline: float | None = None) -> Any:
    """``launch.open_url``: the address handed over as a launch argument, answering by ``deadline`` (a
    ``time.monotonic()`` time; x8WebArc: on 10 Oct one Arc launch took 26-34 s, past the talker's 30 s). lSkills: the
    hand-off only (``address_bar=False``): this skill's own next rung types the address (:func:`route_address_bar`),
    so a browser that ignored it never gets two new tabs. A packaged browser whose protocol activation Windows refuses
    (Arc: ``0x80270254``) is started through its alias inside ``launch`` (``details["protocol_refused"]``)."""
    from yuki.actions import launch

    return launch.open_url(url, app=app or None, deadline=deadline, address_bar=False)


def _launch_deadline(call: RouteCall) -> float:
    """The ``time.monotonic()`` time a rung's page open answers by: what is left of the request's deadline (on the
    run's own clock), at least :data:`OPEN_FLOOR_S` from now."""
    try:
        left = float(call.deadline) - float(call.ctx.clock())
    except Exception:  # noqa: BLE001 - no clock: the floor
        left = 0.0
    return time.monotonic() + max(left, OPEN_FLOOR_S)


def _launch_seen(result: Any) -> dict[str, Any]:
    """What a launch saw, from its result's details (RAM): the window it acted in (``hwnd``), the code word of how it
    started the browser (``via``: ``protocol``, ``alias``, ``startfile``...), whether a window newly showed the page
    (``verified``) or kept the one it had (``reason`` ``page_unchanged``, ``state`` ``same``/``other``), or already
    showed the very page asked for and still does (``already``, state ``already``: review r1 M1, never ``verified``),
    whether the window came to the front, its process, the page that window showed before the hand-off (``before``)
    and what each of the browser's windows showed then (``befores``, ``{hwnd: address}``)."""
    details = getattr(result, "details", None) or {}
    if not isinstance(details, Mapping):
        details = {}
    try:
        hwnd = int(details.get("hwnd") or 0)
    except (TypeError, ValueError):
        hwnd = 0
    befores: dict[int, str] = {}
    for page in details.get("before_pages") or []:
        try:
            if isinstance(page, Mapping) and int(page.get("hwnd") or 0) and page.get("url"):
                befores[int(page["hwnd"])] = str(page["url"])
        except (TypeError, ValueError):
            continue
    state = str(details.get("page_state") or "")
    already = state == "already"
    return {"hwnd": hwnd, "via": str(details.get("via_word") or ""),
            "verified": details.get("page_verified") is True and not already, "already": already,
            "reason": str(details.get("reason") or ""), "state": state,
            "front": details.get("foreground") is True, "process": str(details.get("process_name") or ""),
            "before": befores.get(hwnd, ""), "befores": befores,
            # the browser wasn't running before this launch: none of its windows showed a page before it
            "cold": details.get("already_running") is False and bool(details.get("launched")),
            # integration (chat19 merge note 3): the launch's own stage ends (code words and numbers only), logged
            # by :func:`_log_open` so a long hand-off shows where its time went
            "timings": _stage_ms(details.get("timings_ms"))}


def _stage_ms(timings: Any) -> dict[str, int]:
    """A launch's ``timings_ms`` (stage -> ms) as ``{stage: ms}`` numbers (code-word stages, at most 16), or ``{}``.
    Integration review F5: kept a mapping of numbers, which a content-free log keeps (one ``stage=ms`` string was
    only its length there)."""
    if not isinstance(timings, Mapping):
        return {}
    out: dict[str, int] = {}
    for stage, ms in timings.items():
        if isinstance(ms, (int, float)) and not isinstance(ms, bool) and len(out) < 16:
            out[str(stage)[:24]] = int(round(float(ms)))
    return out


def _log_open(call: RouteCall, rung: str, seen: Mapping[str, Any], status: str) -> None:
    """One content-free line per page a rung opened or tried to (x8WebArc, "the launch's via isn't logged"): the rung
    (``protocol`` or ``address_bar``), how the browser was started or reached, the page's state, the process of the
    window it acted in and whether that window came to the front."""
    _log(call.ctx)("web_open", stage=rung, via=str(seen.get("via") or ""), status=status,
                   process=str(seen.get("process") or ""), front=bool(seen.get("front")),
                   state=str(seen.get("state") or ""), stage_ms=dict(seen.get("timings") or {}) or None)


# x8WebArc: the window a rung of this run opened (or tried to open) a page in, for the next rung of the same run (the
# address bar types into that very window): by run context, a few seconds, RAM only.
_noted: dict[int, tuple[float, str, int]] = {}
_noted_lock = threading.Lock()


def _note_window(call: RouteCall, key: str, hwnd: int) -> None:
    key = _app_key(key)
    if not key or not hwnd:
        return
    now = time.monotonic()
    with _noted_lock:
        for old in [k for k, (at, _key, _hwnd) in _noted.items() if now - at > NOTE_KEEP_S]:
            _noted.pop(old, None)
        _noted[id(call.ctx)] = (now, key, int(hwnd))


def _noted_window(call: RouteCall, key: str) -> int:
    """The window an earlier rung of this run acted in for browser ``key``, while it is still that browser's window."""
    with _noted_lock:
        got = _noted.get(id(call.ctx))
    if not got or time.monotonic() - got[0] > NOTE_KEEP_S or got[1] != _app_key(key):
        return 0
    hwnd = got[2]
    return hwnd if _is_window(hwnd) and _app_key(_window_image(hwnd)) == got[1] else 0


# Review r1 L1: per browser (app key), how many address hand-offs in a row it ignored (kept the very page it had) and
# when the last one was: RAM only, this process.
_ignored: dict[str, tuple[int, float]] = {}


def _note_ignored(key: str, ignored: bool) -> None:
    """One more hand-off ``key`` ignored (``ignored``), or one it opened newly (the count is cleared)."""
    key = _app_key(key)
    if not key:
        return
    with _noted_lock:
        if ignored:
            _ignored[key] = (_ignored.get(key, (0, 0.0))[0] + 1, time.monotonic())
        else:
            _ignored.pop(key, None)


def _ignores_handoff(key: str) -> bool:
    """Whether browser ``key`` ignored the last :data:`IGNORED_AFTER` hand-offs, the latest within
    :data:`IGNORED_KEEP_S`."""
    with _noted_lock:
        got = _ignored.get(_app_key(key))
    return bool(got) and got[0] >= IGNORED_AFTER and time.monotonic() - got[1] < IGNORED_KEEP_S


def _is_window(hwnd: int) -> bool:
    try:
        import win32gui

        return bool(hwnd) and bool(win32gui.IsWindow(int(hwnd)))
    except Exception:  # noqa: BLE001
        return False


def _window_pid(hwnd: int) -> int:
    try:
        import win32process

        return int(win32process.GetWindowThreadProcessId(int(hwnd))[1])
    except Exception:  # noqa: BLE001
        return 0


def _window_image(hwnd: int) -> str:
    """The image name of the process that owns ``hwnd`` (``""``: unknown). Never reads the foreground."""
    pid = _window_pid(hwnd)
    if not pid:
        return ""
    try:
        from yuki.skills.donecheck import _image_of

        return _image_of(pid)
    except Exception:  # noqa: BLE001
        return ""


def _handler_key(url: str) -> str:
    """The app key of the program Windows opens ``url`` with (its registered handler, read from the registry: no
    window read, nothing launched); ``""`` when unknown. Review r2 L3 (D18): a page opened with Windows' default
    handler (no browser measured yet: a fresh install) still names its browser on the run's row before it acts."""
    try:
        from yuki.actions.launch import _default_handler

        return _app_key(_default_handler(url))
    except Exception:  # noqa: BLE001 - an unreadable association: nothing to claim
        return ""


def _held_by_game(result: Any) -> str | None:
    """The refusal kind an action's result carries (fHands, ``yuki.actions.gate.carried``): ``"game"`` or
    ``"blocked"``, else ``None``."""
    details = getattr(result, "details", None) or {}
    value = details.get("refused") if isinstance(details, Mapping) else None
    return value if value in ("game", "blocked") else None


def _claim_app(call: RouteCall, app: str) -> None:
    """D18: the run's ledger row names the browser this rung acts in before it acts (the engine names the app from a
    route's arguments, and a browser chosen here at run time is in none), so a moment the rung causes (the page or the
    tab in front) is matched to Yuki's run live, never stored as the user's. The newest running row of this
    capability (of this request when it has an id). Never raises; nothing to claim without memory."""
    key = _app_key(app)
    memory = getattr(call.ctx, "memory", None)
    if not key or memory is None:
        return
    from yuki.skills import registry
    from yuki.skills.engine import _mem, _store_of

    rid = getattr(call.ctx, "request_id", None)

    def go(client: Any) -> bool:
        store = _store_of(client)
        rows = store._query(
            "SELECT run_key FROM skill_runs WHERE skill = ? AND outcome = 'running' AND t0 >= ?"
            + (" AND request_id = ?" if rid else "") + " ORDER BY t0 DESC LIMIT 1",
            (call.skill.key, time.time() - 120.0, *((rid,) if rid else ())))
        return bool(rows and rows[0][0] and registry.set_run_app(store, str(rows[0][0]), key))

    try:
        _mem(memory, "skill_run_app", go, timeout_s=0.5)
    except Exception:  # noqa: BLE001
        pass


def _ram(ctx: Any) -> dict[str, Any]:
    """The route's RAM-only facts a probe compares with (``None`` after a hand-off: ``{}``)."""
    got = getattr(getattr(ctx, "result", None), "facts_ram", None)
    return dict(got) if isinstance(got, Mapping) else {}


# ---------------------------------------------------------------------------------------------------------------------
# browser.site_tab
# ---------------------------------------------------------------------------------------------------------------------


def known_services(store: Any) -> list[str]:
    """The product names memory keeps tab identities under (``site_tabs:<service>`` rows), sorted. Never raises."""
    try:
        rows = store._query("SELECT name FROM nudge_state WHERE name LIKE ?", (TAB_STATE_PREFIX + "%",))
    except Exception:  # noqa: BLE001
        return []
    out = [str(r[0])[len(TAB_STATE_PREFIX):] for r in rows if str(r[0]).startswith(TAB_STATE_PREFIX)]
    return sorted(s for s in out if s)


def service_of(store: Any, said: Any) -> str:
    """The product name memory keeps identities under for the service the user named (or a learned skill bound): the
    one equal to it, else the only one whose words hold every word said (``"Meet"`` -> ``"Google Meet"``: identity of
    names, never meaning), else the words as said."""
    said = _words(said)
    if not said or store is None:
        return said
    known = known_services(store)
    exact = next((k for k in known if k.casefold() == said.casefold()), None)
    if exact is not None:
        return exact
    want = set(said.casefold().split())
    hits = [k for k in known if want <= set(k.casefold().split())]
    return hits[0] if len(hits) == 1 else said


def _strip_order(c: Any) -> tuple[int, int, int]:
    return (int(getattr(c, "group", 0) or 0), int(getattr(c, "index", 0) or 0), int(getattr(c, "hwnd", 0) or 0))


def read_whole(census: Mapping[str, Any], n_windows: int) -> bool:
    """Whether the census read every window's tab strip whole: one diagnostics entry per window, each walk complete
    with no ``reason`` (``cut_short``, ``error``, ``no_tab_items``, ``tabs_inside_documents``: content-free codes of
    :func:`yuki.perception.tabs.walk_tabs`). A missing entry counts as not read."""
    diags = census.get("diagnostics")
    if not isinstance(diags, list) or len(diags) < int(n_windows):
        return False
    return all(isinstance(d, Mapping) and d.get("complete") is True and not d.get("reason") for d in diags)


def _say(line: str, outcome: str, **facts: Any) -> RouteResult:
    """Nothing done, said plainly (the engine's settled "asked" path: never a done, never counted against the rung)."""
    return RouteResult(ran=False, ask=line, outcome=outcome, facts=facts)


def route_site_tab(call: RouteCall) -> RouteResult:
    """browser.site_tab's rung A: the tab census in the browser memory saw the site's tab in, the tab selected by
    identity, the browser brought forward (see the module docstring). With no tab of the site and a told link, it
    passes to the told-link rung; with no readable strip it does nothing and says so."""
    a, values = call.args, call.values
    link = bool(values.get("link"))
    store = _store(call.ctx)
    if store is None:
        return RouteResult(ran=False, outcome="no_memory")
    said = _words(a.get("service") or values.get("service"))
    service = service_of(store, said)
    ident = _identities(store, service) if service else {}
    # the tab lives where memory saw it; the means slot (said, or the user's usual) only when memory has no browser
    process = _app_key(ident.get("process")) or _app_key(values.get("browser"))
    words = {"service": service or said or "that site", "browser": _browser_title(process) or "the browser"}
    if not (ident.get("runtime_ids") or ident.get("names") or ident.get("positions")):
        return RouteResult(ran=False, outcome="no_identity") if link else _say(LINE_TAB_UNKNOWN.format(**words),
                                                                               "no_identity")
    windows = _app_windows(process)
    if not windows:
        return RouteResult(ran=False, outcome="not_open") if link else _say(LINE_BROWSER_CLOSED.format(**words),
                                                                            "not_open")
    census = _census(store, service, windows, _log(call.ctx))
    facts = {"windows": len(windows), "tabs": int(census.get("tabs") or 0),
             "candidates": len(census.get("candidates") or []), "decision": str(census.get("decision") or "none")}
    if not census.get("tabs"):
        # the strip can't be read: say so, do nothing (not even the told link: the tab may well be open)
        return _say(LINE_TAB_UNREADABLE.format(**words), "strip_unreadable", **facts)
    decision = str(census.get("decision") or "none")
    chosen = list(census.get("chosen") or [])
    facts["whole"] = read_whole(census, len(windows))
    if (decision in ("none", "ask") or not chosen) and not facts["whole"]:
        # review r2 M1: part of the strip wasn't read (a walk cut short, a window that read nothing): the tab may be
        # there. Never "no tab", never a question missing it, never the told link (a second tab)
        return _say(LINE_TAB_PARTIAL.format(**words), "strip_partial", **facts)
    if decision == "none" or not chosen:
        return RouteResult(ran=False, outcome="no_tab", facts=facts) if link else _say(
            LINE_TAB_NONE.format(**words), "no_tab", **facts)
    if decision == "ask":
        tied = sorted(chosen, key=_strip_order)[:len(ORDINALS)]
        pick = values.get("pick")
        try:
            n = int(pick) if pick is not None else 0
        except (TypeError, ValueError):
            n = 0
        if not 1 <= n <= len(tied):
            choices = " or ".join(ORDINALS[:len(tied)])
            return RouteResult(ran=False, ask=ASK_TAB_WHICH.format(n=len(tied), choices=choices, **words),
                               options=[str(i + 1) for i in range(len(tied))], outcome="ask_which", facts=facts)
        order = [tied[n - 1]]
    else:
        # the best by identity first; the next candidates only if it won't select (D8)
        rest = [c for c in (census.get("candidates") or []) if c is not chosen[0]]
        order = [chosen[0]] + rest
    _claim_app(call, process)
    selected = None
    tried = 0
    for cand in order[:TAB_TRIES]:
        if not getattr(cand, "tab_id", ""):
            continue
        hwnd = int(cand.hwnd)
        if kind := _refused("tab_select", hwnd=hwnd):
            return RouteResult(ran=False, held=kind, outcome="refused", facts=facts)
        tried += 1
        result = _select_tab(hwnd, cand.tab_id)
        if getattr(result, "ok", False):
            selected = cand
            break
    facts.update(tried=tried, match=str(getattr(selected or (order[0] if order else None), "match", "") or ""))
    if selected is None:
        # found but not selectable (no RuntimeId, or it won't select): said so; never the told link (a second tab)
        return _say(LINE_TAB_NO_ID.format(**words), "select_failed", **facts)
    hwnd = int(selected.hwnd)
    if kind := _refused("focus", hwnd=hwnd):
        # the tab is selected (a pattern call, no focus), the browser stays where it is
        return RouteResult(ran=True, held=kind, outcome="refused", facts=facts, app=process or None)
    focused = _focus(hwnd)
    if kind := _held_by_game(focused):
        return RouteResult(ran=True, held=kind, outcome="refused", facts=facts, app=process or None)
    facts["focused"] = bool(getattr(focused, "ok", False))
    return RouteResult(ran=True, outcome="selected", facts=facts, app=process or None,
                       say=LINE_TAB_UP.format(**words),
                       facts_ram={"hwnd": hwnd, "tab_id": str(selected.tab_id), "process": process,
                                  "service": service})


def route_told_link(call: RouteCall) -> RouteResult:
    """browser.site_tab's second rung A: no tab of the site open and a link the user told once -> that link opened in
    the means browser (the slot's value: said, or the user's usual; else the browser memory saw the site's tab in;
    else :func:`choose_browser`'s fill), as a launch argument (nothing typed)."""
    link = str(call.values.get("link") or "").strip()
    if "://" not in link or " " in link:
        return RouteResult(ran=False, outcome="no_link")
    store = _store(call.ctx)
    said = _words(call.args.get("service") or call.values.get("service"))
    service = service_of(store, said) if store is not None else said
    ident = _identities(store, service) if store is not None and service else {}
    key = _app_key(call.values.get("browser")) or _app_key(ident.get("process"))
    measured = measured_browsers(store) if store is not None else []
    said_key, choices, suspect = _said_browser(call, store, measured)
    if choices:
        return _ask_browser(choices)
    if said_key:
        # x8WebArc: a browser named in the exchange beats the slot's usual and memory's sighting
        key = said_key
        name, source = _browser_title(key), "words"
    elif key:
        name, source = _browser_title(key), "slot" if call.values.get("browser") else "identity"
        # review r2 H1: the one browser their words name is one Yuki just acted in: the usual or memory's sighting
        # doesn't decide it either (asked)
        ask = _suspect_ask(key, source, suspect, measured)
        if ask:
            return _ask_browser(ask)
    else:
        name, key, source = choose_browser(call)
        if source == "ask":
            return _ask_browser(name)
    if kind := _refused("open_url"):
        return RouteResult(ran=False, held=kind, outcome="refused")
    if source == "default" and not key:
        key = _handler_key(link)
    _claim_app(call, key)
    result = _open_url(link, name if source != "default" else None, _launch_deadline(call))
    if kind := _held_by_game(result):
        return RouteResult(ran=False, held=kind, outcome="refused")
    seen = _launch_seen(result)
    if not getattr(result, "ok", False):
        details = getattr(result, "details", None) or {}
        if seen["reason"] in ("page_unchanged", "page_blank"):
            # x8WebArc: the browser kept the page it had: not opened (page_blank, review r1 M1: a protocol-refused
            # browser came up showing no page at all: not opened either)
            if source != "default" and seen["state"] == "same":
                _note_ignored(key, True)
            _log_open(call, "protocol", seen, seen["reason"])
            return RouteResult(ran=False, outcome=seen["reason"],
                               facts={"browser_from": source, "via": seen["via"], "state": seen["state"]})
        if not (isinstance(details, Mapping) and (details.get("launched") or details.get("hwnd"))):
            return RouteResult(ran=False, outcome="not_opened", facts={"browser_from": source, "via": seen["via"]})
    if seen["verified"] and source != "default":
        _note_ignored(key, False)
    _log_open(call, "protocol", seen, "already" if seen["already"] else "page" if seen["verified"] else "unread")
    words = {"service": service or said or "It", "browser": _browser_title(key) or name or "the browser"}
    return RouteResult(ran=True, outcome="opened", app=key or None, say=LINE_TAB_UP.format(**words),
                       facts={"browser_from": source}, facts_ram={"process": key, "service": service})


def _selected_tabs(hwnd: int) -> list[str] | None:
    """The RuntimeIds of the selected tabs of ``hwnd``'s strip, every group (``None``: couldn't read)."""
    from yuki.connect.spotify_desktop import run_uia
    from yuki.perception.tabs import walk_tabs

    def job(automation: Any, shared: dict) -> None:
        found, _nodes, _complete = walk_tabs(automation, int(hwnd), time.monotonic() + FRONT_READ_S * 0.8,
                                             all_groups=True, diagnostics={})
        shared["selected"] = [t.tab_id for t, _e in found if t.selected and t.tab_id]
        shared["tabs"] = len(found)

    shared, late = run_uia(job, FRONT_READ_S, name="yuki-skill-tabs")
    if late or not shared.get("tabs"):
        return None
    return list(shared.get("selected") or [])


def probe_site_tab_shown(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """browser.site_tab's done-check: the browser the route selected in (its app key, ``process``) is in front, its
    window the one the route selected in, and that window's selected tab is the one the route selected (by RuntimeId).
    Not so: False; unreadable: None."""
    ram = _ram(ctx)
    want_hwnd, want_tab = int(ram.get("hwnd") or 0), str(ram.get("tab_id") or "")
    want_app = _app_key(ram.get("process") or args.get("process"))
    if not want_hwnd or not want_tab:
        return None, {"status": "no_route"}
    hwnd, image = _front()
    if not hwnd or not image:
        return None, {"status": "unread"}
    if (want_app and _app_key(image) != want_app) or hwnd != want_hwnd:
        return False, {"status": "other_window"}
    selected = _selected_tabs(hwnd)
    if selected is None:
        return None, {"status": "tabs_unread"}
    if not selected:
        return None, {"status": "no_selection"}
    return (want_tab in selected), {"selected": len(selected)}


def probe_site_front(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """browser.site_tab's re-check and the told link's done-check (review r1 L2): memory's own sighting of the site in
    front since the run began, a ``site_front`` moment of the service (the product name the route matched, else the
    check's ``service``) in the browser the route acted in (``app``, else the route's). Seen: True. Only another
    calling site of that browser in front since: False (the tab that came up is not that site). Nothing seen yet:
    None (the 1 s moment beat may lag)."""
    call = getattr(ctx, "memory_call", None)
    if call is None:
        return None, {"status": "no_memory"}
    ram = _ram(ctx)
    app = _app_key(args.get("app")) or _app_key(ram.get("process")) or _app_key(
        getattr(getattr(ctx, "result", None), "app", ""))
    service = _words(ram.get("service") or args.get("service"))
    if not app or not service:
        return None, {"status": "no_args"}
    try:
        rows = call("moments", lambda c: getattr(c, "store", c).moments_between(
            float(ctx.since), time.time() + 1.0, kinds=["site_front"], app=app))
    except Exception:  # noqa: BLE001
        return None, {"status": "unread"}
    if not isinstance(rows, list):
        return None, {"status": "unread"}
    opened = [r for r in rows if isinstance(r, Mapping) and r.get("service") is not None]
    if any(str(r.get("service")).casefold() == service.casefold() for r in opened):
        return True, {"n": len(opened)}
    if opened:
        return False, {"n": len(opened), "status": "other_site"}
    return None, {"n": 0, "status": "not_seen_yet"}


def probe_browser_front(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """The told link's re-check: the browser the route opened the link in is the program in front."""
    ram = _ram(ctx)
    want = _app_key(args.get("app")) or _app_key(ram.get("process")) or _app_key(
        getattr(getattr(ctx, "result", None), "app", ""))
    if not want:
        return None, {"status": "no_browser"}
    hwnd, image = _front()
    if not hwnd or not image:
        return None, {"status": "unread"}
    return (_app_key(image) == want), {"status": "front" if _app_key(image) == want else "other_app"}


# ---------------------------------------------------------------------------------------------------------------------
# The browser
# ---------------------------------------------------------------------------------------------------------------------


def measured_browsers(store: Any, *, now: float | None = None, days: float = BROWSER_DAYS,
                      site_hosts: Sequence[str] = (), min_hosts: int = BROWSER_MIN_HOSTS,
                      by_site: bool = True) -> list[dict[str, Any]]:
    """The browsers among the programs in the user's own timeline rows (never Yuki's) of the last ``days``, most used
    first: ``{"key", "name", "seconds", "last", "hosts", "site_last"}`` (app key, display name as Windows shows it, time
    on pages, its last row's end, distinct hosts, when it last showed a page of ``site_hosts``). A browser is measured,
    never listed: a program that showed pages of at least :data:`BROWSER_MIN_HOSTS` hosts, or a page of the site itself
    (an app that draws one site in its own window, such as a chat app, shows one or two). ``min_hosts`` / ``by_site``
    are the definition's two knobs, for the replay that chose them (``scripts/browser_rule_replay.py``). bRound2: a
    page Yuki itself opened counts for nothing here, however long the user then looked at it
    (:data:`NOT_YUKIS_VISIT_SQL`), and neither does a row inside a live test's span (skill19 review L5: tabs a test left
    open are no use of the user's; :func:`_outside_tests`). Never raises (``[]``)."""
    now = time.time() if now is None else float(now)
    hosts = [str(h).casefold() for h in site_hosts if h]
    outside, outside_args = _outside_tests(store, now - days * 86400.0, now)
    marks = ",".join("?" * len(hosts))
    site_sql = (f"max(CASE WHEN t.host IN ({marks}) THEN t.ended_at END)" if hosts else "NULL")
    # skill19 (all-browser R6): the user's own visits of the site per browser (its rows), for the site's share
    site_n_sql = (f"sum(CASE WHEN t.host IN ({marks}) THEN 1 ELSE 0 END)" if hosts else "0")
    try:
        rows = own_rows_query(
            store,
            f"SELECT t.process, sum(t.active_s + t.passive_s) AS s, max(t.ended_at) AS last, "
            f"count(DISTINCT t.host) AS n, {site_sql} AS site_last, {site_n_sql} AS site_n FROM timeline t "
            "WHERE t.host IS NOT NULL "
            "AND t.started_at >= ? AND t.started_at <= ? AND t.by_yuki = 0 AND t.state = 'present' "
            f"AND t.process IS NOT NULL AND {outside}{{own}} GROUP BY t.process ORDER BY s DESC",
            (*hosts, *hosts, now - days * 86400.0, now, *outside_args))
    except Exception:  # noqa: BLE001
        return []
    weights = _recent_weights(store, now, days, outside=(outside, outside_args))
    out = []
    for process, seconds, last, n_hosts, site_last, site_n in rows:
        key = _app_key(process)
        if not key or not (int(n_hosts or 0) >= int(min_hosts) or (by_site and site_last is not None)):
            continue
        name = ""
        try:
            got = store._query("SELECT app FROM timeline WHERE process = ? AND app != '' AND started_at <= ? "
                               "ORDER BY ended_at DESC LIMIT 1", (process, now))
            name = str(got[0][0]) if got else ""
        except Exception:  # noqa: BLE001
            pass
        out.append({"key": key, "name": name or key, "seconds": float(seconds or 0.0), "last": float(last or 0.0),
                    "hosts": int(n_hosts or 0), "site_last": float(site_last) if site_last is not None else None,
                    "weight": float(weights.get(str(process), float(seconds or 0.0))), "site_n": int(site_n or 0)})
    # lSkills: most used first by recent weight (a switch of browser shows within days, not after the 14-day window)
    out.sort(key=lambda m: (-m["weight"], -m["seconds"], m["key"]))
    return out


def _recent_weights(store: Any, now: float, days: float,
                    outside: tuple[str, Sequence[float]] | None = None) -> dict[str, float]:
    """``{timeline process: the user's own page seconds of the last days, each weighted by its age}``
    (:data:`BROWSER_HALF_LIFE_DAYS`; by the day, so one grouped read): what "the one used most" ranks by. Never a page
    Yuki itself opened (:data:`NOT_YUKIS_VISIT_SQL`), nor a row inside a live test's span (``outside``: the condition
    :func:`_outside_tests` made). Never raises (``{}``: the raw seconds rank)."""
    cond, cond_args = outside if outside is not None else _outside_tests(store, now - days * 86400.0, now)
    try:
        rows = own_rows_query(
            store,
            "SELECT t.process, CAST((? - t.started_at) / 86400.0 AS INTEGER) AS d, sum(t.active_s + t.passive_s) "
            "FROM timeline t WHERE t.host IS NOT NULL AND t.started_at >= ? AND t.started_at <= ? AND t.by_yuki = 0 "
            f"AND t.state = 'present' AND t.process IS NOT NULL AND {cond}{{own}} GROUP BY t.process, d",
            (now, now - days * 86400.0, now, *cond_args))
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, float] = {}
    for process, age_d, seconds in rows:
        w = 0.5 ** ((max(0.0, float(age_d or 0)) + 0.5) / BROWSER_HALF_LIFE_DAYS)
        out[str(process)] = out.get(str(process), 0.0) + float(seconds or 0.0) * w
    return out


def _outside_tests(store: Any, lo: float, hi: float) -> tuple[str, list[float]]:
    """Skill19 review L5 (all-browser R3: about 75 rows of a site in the browser a live test drove, on 19 Oct): a SQL
    condition on ``t.started_at`` that leaves out the live test's spans overlapping ``lo``..``hi``
    (:func:`yuki.memory.testtag.store_test_spans`, a content-free time filter), ``("1", [])`` with none. Known only
    while the test's own turns are kept (a purge deletes them, and the timeline rows stay). Never raises."""
    try:
        from yuki.memory.testtag import sql_outside_spans, store_test_spans

        spans = [(a, b) for a, b in store_test_spans(store) if b >= lo and a <= hi]
        return sql_outside_spans("t.started_at", spans)
    except Exception:  # noqa: BLE001 - no spans: every row counts, as before
        return "1", []


def fresh_keys(measured: Sequence[Mapping[str, Any]]) -> list[str]:
    """The measured browsers still in use (lSkills): every one whose own page rows went on until at most
    :data:`BROWSER_STALE_DAYS` before the newest row of the browser used last, in the order measured. Relative to that
    newest row, never to now: after a week away nothing is stale. Pure (the ``last`` field of
    :func:`measured_browsers`)."""
    keys = [m for m in measured if m.get("key")]
    if not keys:
        return []
    newest = max(float(m.get("last") or 0.0) for m in keys)
    return [str(m["key"]) for m in keys if newest - float(m.get("last") or 0.0) <= BROWSER_STALE_DAYS * 86400.0]


def last_in_front(store: Any, keys: Sequence[str], *, now: float | None = None, within_s: float = RECENT_S,
                  min_s: float = 0.0, active_only: bool = False, page_only: bool = False) -> str | None:
    """Of ``keys`` (browsers), the one in front most recently, if its row ended at most ``within_s`` ago. The knobs are
    for the replay's refinements (review r2 M3; ``scripts/browser_rule_replay.py``): ``min_s`` skips glance rows (the
    user's time on the row, active plus passive, under it), ``active_only`` counts only rows with input,
    ``page_only`` only rows that showed a web page. bRound2: never a row on a page Yuki itself opened
    (:data:`NOT_YUKIS_VISIT_SQL`)."""
    now = time.time() if now is None else float(now)
    if not keys:
        return None
    marks = ",".join("?" * len(keys))
    extra = "".join(part for part, on in ((" AND (t.active_s + t.passive_s) >= ?", min_s > 0),
                                          (" AND t.active_s > 0", active_only),
                                          (" AND t.host IS NOT NULL", page_only)) if on)
    try:
        rows = own_rows_query(
            store,
            f"SELECT t.process FROM timeline t WHERE t.process IN ({marks}) AND t.state = 'present' "
            f"AND t.by_yuki = 0 AND t.ended_at >= ? AND t.started_at <= ?{extra}{{own}} ORDER BY t.ended_at DESC "
            "LIMIT 1",
            (*keys, now - within_s, now, *((float(min_s),) if min_s > 0 else ())))
    except Exception:  # noqa: BLE001
        return None
    return _app_key(rows[0][0]) if rows else None


def band_browser(store: Any, keys: Sequence[str], *, now: float | None = None, hours: int = BAND_H,
                 days: float = BAND_DAYS) -> str | None:
    """Of ``keys`` (browsers), the one the user had in front most in this hour band (now's local hour ``hours`` either
    side, on weekdays or on weekends like now) over the last ``days``; ``None`` when none was. bRound2: never a row on a
    page Yuki itself opened (:data:`NOT_YUKIS_VISIT_SQL`). Never raises."""
    now = time.time() if now is None else float(now)
    if not keys:
        return None
    from datetime import datetime

    stamp = datetime.fromtimestamp(now)
    hour = stamp.hour
    weekend = stamp.weekday() >= 5
    band = sorted({(hour + d) % 24 for d in range(-int(hours), int(hours) + 1)})
    marks = ",".join("?" * len(keys))
    try:
        rows = own_rows_query(
            store,
            f"SELECT t.process, t.started_at, t.active_s + t.passive_s FROM timeline t WHERE t.process IN ({marks}) "
            "AND t.state = 'present' AND t.by_yuki = 0 AND t.started_at >= ? AND t.started_at <= ?{own}",
            (*keys, now - float(days) * 86400.0, now))
    except Exception:  # noqa: BLE001
        return None
    totals: dict[str, float] = {}
    for process, started, seconds in rows:
        at = datetime.fromtimestamp(float(started or 0.0))
        if at.hour in band and (at.weekday() >= 5) == weekend:
            key = _app_key(process)
            # lSkills: each second by its age (BROWSER_HALF_LIFE_DAYS): last week's nights outweigh the month's
            age_d = max(0.0, now - float(started or 0.0)) / 86400.0
            totals[key] = totals.get(key, 0.0) + float(seconds or 0.0) * 0.5 ** (age_d / BROWSER_HALF_LIFE_DAYS)
    return max(totals, key=totals.__getitem__) if totals else None


def browser_rule(*, named: str | None, said: bool, front: str | None, recent: str | None, pref: str | None,
                 measured: Sequence[Mapping[str, Any]], site_first: bool = False, policy: str = "most",
                 band: str | None = None) -> tuple[str, str]:
    """``(browser app key, where it came from)`` (design §5): ``words`` (named and in their words) > ``pref`` (the
    slot's value the resolver filled: their usual) > the fill ``policy`` (:data:`BROWSER_POLICIES`; a learned skill's,
    else ``most``): ``front`` / ``recent`` for ``front_or_recent``, ``band``, ``site`` (the browser the site was last
    shown in) > ``most`` (the browser they use most) > ``default`` (``""``: Windows' handler). Pure: a replay on a
    database copy runs it too (``scripts/browser_rule_replay.py``; ``site_first`` is that script's old knob, kept as
    an alias of the ``site`` policy).

    lSkills (live 17 Oct): ``measured`` comes most used by recent weight first (:func:`measured_browsers`), and
    ``band`` and ``most`` take only a browser still in use (:func:`fresh_keys`): Arc, unused since 12 Oct while Chrome
    ran every day, fills nothing by them; named, the usual, ``site`` and front/recent are as before.

    skill19 (live 19 Oct, R6): before ``most``, a site whose own visits are concentrated in one browser
    (:func:`site_browser`, from ``measured`` read with the site's hosts) opens in that browser, ``site``."""
    keys = [str(m.get("key") or "") for m in measured if m.get("key")]
    fresh = fresh_keys(measured) or keys
    if named and said:
        return _app_key(named), "words"
    if pref:
        return _app_key(pref), "pref"
    if site_first and policy == "most":
        policy = "site"
    if policy == "front_or_recent":
        if front and front in keys:
            return front, "front"
        if recent and recent in keys:
            return recent, "recent"
    elif policy == "band":
        if band and band in fresh:
            return band, "band"
    elif policy == "site":
        shown = [m for m in measured if m.get("key") and m.get("site_last") is not None]
        if shown:
            return str(max(shown, key=lambda m: float(m["site_last"]))["key"]), "site"
    lives = site_browser(measured)
    if lives:
        # skill19 (live 19 Oct, all-browser R6: Excalidraw, 15 of 15 visits in Arc, opened in Chrome by "most"): a
        # site whose own visits are concentrated in one browser opens there (its storage and login live there), fresh
        # or not, before the browser used most. A measured share bar, never a list of sites
        return lives, "site"
    if fresh:
        return fresh[0], "most"
    return "", "default"


#: skill19 (R6): a site "lives" in one browser when at least this share of the user's own visits of it (timeline rows on
#: its hosts, :func:`measured_browsers`' ``site_n``) were in that browser (prefs' rule 2 bar, ``prefs.SHARE``)...
SITE_SHARE = 0.8
#: ...over at least this many visits (``prefs.MIN_N``: one visit is no habit).
SITE_MIN_VISITS = 3


def site_browser(measured: Sequence[Mapping[str, Any]]) -> str | None:
    """The browser a site's own visits are concentrated in (:data:`SITE_SHARE` of at least :data:`SITE_MIN_VISITS`,
    from :func:`measured_browsers` read with the site's hosts), else ``None``. Pure."""
    counts = [(str(m.get("key") or ""), int(m.get("site_n") or 0)) for m in measured if m.get("key")]
    total = sum(n for _k, n in counts if n > 0)
    if total < SITE_MIN_VISITS:
        return None
    key, n = max(counts, key=lambda kn: kn[1])
    return key if key and n / total >= SITE_SHARE else None


def site_hosts(site_name: str) -> list[str]:
    """A site's hosts (``yuki/connect/sites/<site>.toml``, ``[site] hosts``)."""
    from yuki.connect.templates import site

    return [str(h) for h in (site(site_name).get("site") or {}).get("hosts") or []]


def _policy(call: RouteCall, slot: str = "browser", default: str = "most") -> str:
    """The fill policy a learned skill carries for its means slot (``means_policy`` in its route arguments, bound by
    :mod:`yuki.skills.catalog`), else ``default``."""
    table = call.args.get("means_policy") if isinstance(call.args, Mapping) else None
    got = str(table.get(slot) or "") if isinstance(table, Mapping) else ""
    return got if got in BROWSER_POLICIES else default


def _browser_names(m: Mapping[str, Any]) -> set[str]:
    """The names one measured browser goes by: its display name as Windows shows it, its app key as people say it
    ("Arc", "Chrome"), and each word of the display name that its app key holds (review r2 L4: "Microsoft Edge" with
    the key ``msedge`` is also "Edge"; "Google Chrome" with ``chrome``, "Chrome"). Data only (the name and the key)."""
    key = str(m.get("key") or "")
    display = str(m.get("name") or "")
    names = {display, _browser_title(key)}
    folded = key.casefold()
    for word in display.split():
        w = "".join(ch for ch in word if ch.isalnum())
        if len(w) >= 3 and w.casefold() in folded:
            names.add(w)
    return {n for n in names if n}


def browsers_said(measured: Sequence[Mapping[str, Any]], words: Sequence[str]) -> list[str]:
    """The app keys of the measured browsers whose name stands, whole words in order, in the user's ``words``
    (:func:`_browser_names`): identity of names, never meaning. In the order measured, each once."""
    from yuki.skills.uia_route import said as is_said

    sources = [str(w) for w in words if w]
    out: list[str] = []
    for m in measured:
        key = str(m.get("key") or "")
        if key and key not in out and any(is_said(n, sources) for n in _browser_names(m)):
            out.append(key)
    return out


def _skill_family(call: RouteCall) -> tuple[str, ...]:
    """The keys whose runs count as "this skill's" in the ledger: the skill's own, its capability's and the learned
    skills of that capability the catalog holds (Item 6's ``family``: a volume change on Arc's sound doesn't count
    against "open YouTube in Arc")."""
    key = str(getattr(call.skill, "key", "") or "")
    cap = str(getattr(call.skill, "capability", "") or "") or key
    out = [key, cap]
    try:
        from yuki.skills import catalog

        out += [str(s.key) for s in getattr(catalog.load(), "skills", ()) or ()
                if str(getattr(s, "capability", "") or "") == cap]
    except Exception:  # noqa: BLE001 - its own key and capability
        pass
    return tuple(dict.fromkeys(k for k in out if k))


def _request_began(call: RouteCall, store: Any, now: float, keys: Sequence[str]) -> float:
    """When the request this run serves began: its turn's first ledger row (``skill_runs.t0`` of ``request_id``), else
    this run's own running row (of ``keys``, the skill's family), else ``now``. What Yuki did after that is its work
    on this very request (the rung before, a run before it in the same turn), which the user's words, said before it,
    can't be about."""
    rid = getattr(call.ctx, "request_id", None)
    keys = list(keys) or [str(call.skill.key)]
    reads = [(f"SELECT t0 FROM skill_runs WHERE skill IN ({','.join('?' * len(keys))}) AND outcome = 'running' "
              "AND t0 >= ? AND t0 <= ? ORDER BY t0 DESC LIMIT 1", (*keys, now - 120.0, now + 1.0))]
    if rid:
        reads.insert(0, ("SELECT min(t0) FROM skill_runs WHERE request_id = ? AND t0 >= ? AND t0 <= ?",
                         (rid, now - REQUEST_BACK_S, now + 1.0)))
    for sql, args in reads:
        try:
            rows = store._query(sql, args)
        except Exception:  # noqa: BLE001
            continue
        if rows and rows[0][0] is not None:
            return min(float(rows[0][0]), now)
    return now


def _just_acted(call: RouteCall, store: Any) -> set[str]:
    """Review r2 H1: the app keys of the programs Yuki itself acted in within :data:`JUST_ACTED_S` before this request
    began (:func:`_request_began`): the same data and window as Item 6's ``resolve.just_acted`` (the timeline's
    stretches begun while Yuki acted, ``by_yuki``, and the apps the ledger says runs of this skill's family drove,
    ``skill_runs.app``), called through it when it is there. A turn that names one of them may be the user talking
    about what Yuki just did ("Dude, you open YouTube and Chrome again."), so that name decides nothing here: never
    the meaning of the words, only what Yuki did. Left out: a browser the user named for one of those runs (the
    ledger's ``filled`` says ``words`` for its browser), since Yuki acted there on their word. Never raises (an
    empty set)."""
    if store is None:
        return set()
    skills = _skill_family(call)
    began = _request_began(call, store, time.time(), skills) - 0.001   # strictly before: never this request's acts
    raw: set[str] = set()
    try:
        from yuki.skills import resolve as _resolve

        fn = getattr(_resolve, "just_acted", None)
    except Exception:  # noqa: BLE001
        fn = None
    if callable(fn):
        try:
            raw = {str(a) for a in fn(store, now=began, skills=skills) or () if a}
        except Exception:  # noqa: BLE001
            raw = set()
    else:
        try:
            raw |= {str(r[0]) for r in store._query(
                "SELECT DISTINCT process FROM timeline WHERE by_yuki = 1 AND process IS NOT NULL AND process != '' "
                "AND started_at >= ? AND started_at <= ?", (began - JUST_ACTED_S, began))}
        except Exception:  # noqa: BLE001
            pass
        try:
            raw |= {str(r[0]) for r in store._query(
                f"SELECT DISTINCT app FROM skill_runs WHERE skill IN ({','.join('?' * len(skills))}) "
                "AND app IS NOT NULL AND app != '' AND t0 >= ? AND t0 <= ?", (*skills, began - JUST_ACTED_S, began))}
        except Exception:  # noqa: BLE001
            pass
    acted = {_app_key(a) or a.casefold() for a in raw}
    if not acted:
        return acted
    # a browser the user named for one of those runs (its ledger row's browser filled from their words) is one they
    # chose: Yuki acting in it was their word, so naming it again ("put something on YouTube on Arc" after "open
    # YouTube in Arc") is no sign of a complaint about it
    asked: set[str] = set()
    try:
        import json

        for app, filled in store._query(
                f"SELECT app, filled FROM skill_runs WHERE skill IN ({','.join('?' * len(skills))}) "
                "AND app IS NOT NULL AND app != '' AND filled IS NOT NULL AND t0 >= ? AND t0 <= ?",
                (*skills, began - JUST_ACTED_S, began)):
            got = json.loads(filled) if isinstance(filled, str) and filled else {}
            if isinstance(got, dict) and got.get("browser") == "words":
                asked.add(_app_key(app) or str(app).casefold())
    except Exception:  # noqa: BLE001 - none known as asked for
        pass
    return acted - asked


def _said_browser(call: RouteCall, store: Any, measured: Sequence[Mapping[str, Any]] | None = None,
                  acted: Callable[[], set[str]] | None = None) -> tuple[str, str, tuple[str, ...]]:
    """``(app key, "", ())`` of the browser the user named (the slot's value when it stands in their words, else the
    one measured browser whose name does); ``("", "Arc or Chrome", ())`` when two different ones are named and the
    slot names neither (or is only their usual); ``("", "", ())`` when none is. ``measured``:
    :func:`measured_browsers` already read (else read here); ``acted``: :func:`_just_acted`'s reader (else read here;
    read only when the words name a browser).

    Review r2 H1: when the one browser the words name is one Yuki itself just acted in, and the slot doesn't hold it
    from their words, it decides nothing (Item 6's guard: the turn may name it to complain, "Dude, you open YouTube
    and Chrome again."), nor does the slot's usual or a fill: ``("", "", (that browser,))``, the third part the
    browsers named that way, and the caller asks (:func:`_suspect_ask`). A slot value is never swapped for it, and a
    slot value the resolver took from the words (``_slot_source`` ``words``: the talker read "open it in ARC" there)
    stands as it is. Two browsers named are never told apart by which one Yuki acted in: that record is partial (10
    Oct 21:05:05, "open YouTube in arc not in Chrome": the timeline held Yuki's act in Arc, not its YouTube in Chrome,
    which opened behind), so leaving the acted one out would have picked Chrome; they are asked, as before."""
    words = [str(w) for w in call.ctx.words if w]
    named = call.values.get("browser")
    if measured is None:
        measured = measured_browsers(store) if store is not None else []
    hits = browsers_said(measured, words)
    from yuki.skills.uia_route import said as is_said

    named_said = bool(named) and is_said(str(named), words)
    if not hits and not named_said:
        return "", "", ()

    def done_by_yuki() -> set[str]:
        return acted() if acted is not None else _just_acted(call, store)

    if named_said:
        key = _app_key(named)
        others = [k for k in hits if k != key]
        if not others:
            # their words name only this one: it stands, unless Yuki just acted in it and the value is only their
            # usual (then it decides nothing)
            if key in done_by_yuki() and _slot_source(call, "browser") != "words":
                return "", "", (key,)
            return key, "", ()
        # the words name others too: the talker gave it from their words (it resolved "Arc, not Chrome"), it stands;
        # the value is only their usual, asked, never assumed
        if _slot_source(call, "browser") in ("words", ""):
            return key, "", ()
        return "", " or ".join(_browser_title(k) for k in ([key] + others)[:3]), ()
    if len(hits) > 1:
        return "", " or ".join(_browser_title(k) for k in hits[:3]), ()
    if hits[0] in done_by_yuki():
        return "", "", (hits[0],)
    return hits[0], "", ()


def _suspect_ask(key: str, source: str, suspect: Sequence[str], measured: Sequence[Mapping[str, Any]]) -> str:
    """Review r2 H1: the choices to ask (``"Chrome or Arc"``) when the one browser the user's words name is one Yuki
    itself just acted in (:func:`_said_browser`'s third part) and the browser picked (``key``) wasn't taken from
    their words (their usual, a fill policy, memory's sighting). Such a turn may be a complaint about what Yuki did
    ("Dude, you open YouTube and Chrome again.") or a request for that browser ("put something on YouTube and ARC
    browser." with Arc just used): the words alone can't tell which, so any fill is a guess either way. On the 10 Oct
    copy both "most used" and the learned skill's ``site`` policy gave Chrome for the first, and ``site`` gave Chrome
    for the second at 21:10:30. Never that browser by guess, never another one by guess: asked, the named one and the
    pick (else the user's next browser) as the choices. ``""`` when there's nothing to ask (the words decided, or no
    other browser is measured)."""
    if not suspect or source in ("words", "words_again", "ask"):
        return ""
    first = str(suspect[0])
    other = key if key and key not in suspect else next(
        (str(m["key"]) for m in measured if m.get("key") and str(m["key"]) not in suspect), "")
    return f"{_browser_title(first)} or {_browser_title(other)}" if other else ""


def _slot_source(call: RouteCall, slot: str) -> str:
    """Where the resolver took ``slot``'s value from for this run (``words``: the caller gave it and it stands in
    their words; ``pref``, ``taste``, ``default``, ``again``...): the run's own ledger row, which the engine wrote before
    the first rung (``skill_runs.filled``, code words only). ``""`` when it can't be read (no memory)."""
    memory = getattr(call.ctx, "memory", None)
    if memory is None:
        return ""
    from yuki.skills.engine import _mem, _store_of

    rid = getattr(call.ctx, "request_id", None)
    keys = tuple(dict.fromkeys(k for k in (call.skill.key, getattr(call.skill, "capability", "")) if k))

    def go(client: Any) -> str:
        rows = _store_of(client)._query(
            f"SELECT filled FROM skill_runs WHERE skill IN ({','.join('?' * len(keys))}) AND outcome = 'running' "
            "AND t0 >= ?" + (" AND request_id = ?" if rid else "") + " ORDER BY t0 DESC LIMIT 1",
            (*keys, time.time() - 120.0, *((rid,) if rid else ())))
        if not rows or not rows[0][0]:
            return ""
        import json

        got = json.loads(rows[0][0])
        return str(got.get(slot) or "") if isinstance(got, dict) else ""

    try:
        return str(_mem(memory, "skill_slot_source", go, timeout_s=0.5) or "")
    except Exception:  # noqa: BLE001
        return ""


def _ask_browser(choices: str) -> RouteResult:
    """Two browsers named and none picked: asked, never guessed (the engine's settled "asked" path)."""
    return RouteResult(ran=False, ask=ASK_BROWSER.format(choices=choices), options=choices.split(" or "),
                       outcome="ask_browser")


def choose_browser(call: RouteCall, *, hosts: Sequence[str] = (), policy: str | None = None) -> tuple[str, str, str]:
    """``(name to launch it by, app key, where it came from)`` for a browser route (see :func:`browser_rule`).
    ``hosts``: the site's own hosts (a program that showed it counts as a browser; the ``site`` policy).

    x8WebArc: what the user said wins. The slot's value stands when it is in their words (the request's whole exchange,
    ``ctx.words``); otherwise a measured browser whose name is in those words beats their usual and any learned fill
    policy (10 Oct: the learned "Open YouTube home" was called with no browser while "in Arc" stood two turns back).
    Two different browsers named there and the slot naming neither (``"open it in Arc, not Chrome"``): ``source`` is
    ``"ask"`` and ``name`` the choices (``"Arc or Chrome"``), for :func:`_ask_browser`: never a guess.

    Review r2 H1: a browser Yuki itself just acted in (:func:`_just_acted`) decides nothing by being the one browser
    named (:func:`_said_browser`), nor by being in front for the ``front_or_recent`` policy (Yuki brought it there;
    the user's own last browser, ``recent``, still counts); then no fill decides either, it asks
    (:func:`_suspect_ask`). A browser said that Yuki also just acted in, kept because the resolver took it from the
    words, is ``source`` ``words_again``: the done line names it (:func:`_browser_line`)."""
    named = call.values.get("browser")
    named = str(named) if named else None
    store = _store(call.ctx)
    measured = measured_browsers(store, site_hosts=list(hosts)) if store is not None else []
    keys = [m["key"] for m in measured]
    seen: list[set[str]] = []

    def acted() -> set[str]:
        if not seen:
            seen.append(_just_acted(call, store))
        return seen[0]

    said_key, choices, suspect = _said_browser(call, store, measured, acted)
    if choices:
        return choices, "", "ask"
    said = bool(said_key)
    if said:
        named = said_key
    policy = policy or _policy(call)
    front = recent = band = None
    if store is not None and not named and policy == "front_or_recent":
        _hwnd, image = _front()
        front = _app_key(image) or None
        if front and front in acted():
            front = None
        recent = last_in_front(store, keys)
    if store is not None and not named and policy == "band":
        band = band_browser(store, fresh_keys(measured) or keys)     # lSkills: among the browsers still in use
    key, source = browser_rule(named=named, said=said, front=front, recent=recent,
                               pref=named if not said else None, measured=measured, policy=policy, band=band)
    ask = _suspect_ask(key, source, suspect, measured)
    if ask:
        return ask, "", "ask"
    if source == "words" and key in acted():
        source = "words_again"
    name = next((m["name"] for m in measured if m["key"] == key), "") or _browser_title(key) or key
    return name, key, source


def _browser_line(base: str, name: str, source: str) -> str:
    """The done line: the browser named when the user didn't name it (one word corrects it), and when they named one
    Yuki had just acted in (``words_again``, review r2 H1: after "you opened it in Chrome. I asked for Arc", the line
    says which browser it is now). A slot value the resolver filled (``pref``) is named by the resolver's own done
    line when it was a guess (its slot's ``say``; a confirmed usual is not), so it isn't named here a second time
    (review r2 sim: "Youtube is up in Google Chrome in chrome.")."""
    if source in ("words", "default", "pref") or not name:
        return base
    return f"{base.rstrip('.')} in {_browser_title(name) if name.islower() else name}."


def _open_in_browser(call: RouteCall, url: str, *, hosts: Sequence[str] = (), policy: str | None = None,
                     bar_next: bool = False) -> tuple[RouteResult | None, str, str, str, dict[str, Any]]:
    """Open ``url`` in the chosen browser: ``(a result when it didn't open, name, key, source, what the open saw)``
    (:func:`_launch_seen`; RAM). x8WebArc: the open answers by the request's deadline (:func:`_launch_deadline`); a
    browser that kept the page it had (``page_unchanged``: Arc ignored the address and still showed its old tab, or
    another page) did not open it, so the rung did not run and the next one (the address bar) does. ``bar_next`` (a
    route whose next rung is the address bar; review r1 L1): a browser that ignored the last hand-offs
    (:func:`_ignores_handoff`) is not handed this one, so that rung runs at once."""
    name, key, source = choose_browser(call, hosts=hosts, policy=policy)
    if source == "ask":
        return _ask_browser(name), name, key, source, {}
    if kind := _refused("open_url"):
        return RouteResult(ran=False, held=kind, outcome="refused"), name, key, source, {}
    if source == "default" and not key:
        key = _handler_key(url)
    named = source != "default"
    if bar_next and named and _ignores_handoff(key):
        _log_open(call, "protocol", {}, "skipped_ignored")
        return RouteResult(ran=False, outcome="handoff_ignored", facts={"browser_from": source}), \
            name, key, source, {}
    _claim_app(call, key)
    tabs_before = _tabs_before(key, url) if key else {}
    result = _open_url(url, name if named else None, _launch_deadline(call))
    if kind := _held_by_game(result):
        return RouteResult(ran=False, held=kind, outcome="refused"), name, key, source, {}
    seen = _launch_seen(result)
    seen["tabs_before"] = tabs_before
    if seen["hwnd"]:
        _note_window(call, key or _app_key(seen["process"]), seen["hwnd"])
    facts = {"browser_from": source, "via": seen["via"]}
    if not getattr(result, "ok", False):
        if seen["reason"] in ("page_unchanged", "page_blank"):
            # page_blank (review r1 M1): a browser whose protocol activation Windows refused came up showing no page
            # at all (a cold Arc drops the address): not opened either, so the address-bar rung types it now instead
            # of the done-check waiting 6 s on nothing and handing the request to the thinker
            if named and seen["state"] in ("same", "blank"):
                # fSkills (live 18 Oct: Arc refused the address and came up blank, 3.4 s, before the address bar):
                # a blank window is the address ignored too, so after IGNORED_AFTER of them the bar runs at once
                _note_ignored(key, True)
            _log_open(call, "protocol", seen, seen["reason"])
            return RouteResult(ran=False, outcome=seen["reason"], facts={**facts, "state": seen["state"]}), \
                name, key, source, seen
        details = getattr(result, "details", None) or {}
        launched = isinstance(details, Mapping) and bool(details.get("launched") or details.get("hwnd"))
        _log_open(call, "protocol", seen, "unread" if launched else "not_opened")
        # a browser that started but didn't show the page may still: acted, and the done-check decides
        return (None if launched else RouteResult(ran=False, outcome="not_opened", facts=facts)), \
            name, key, source, seen
    if seen["verified"] and named:
        _note_ignored(key, False)
    _log_open(call, "protocol", seen, "already" if seen["already"] else "page" if seen["verified"] else "unread")
    return None, name, key, source, seen


def _opened_result(line: str, key: str, source: str, seen: Mapping[str, Any], ram: dict[str, Any], *,
                   already_line: str | None = None) -> RouteResult:
    """A rung that handed the page over: acted. ``verified`` when the launch itself saw a window newly show the page
    (never a page it already showed; RAM ``arrived``); the done-check reads the window it acted in (``hwnd``) and
    compares with what that window showed before (``before``, ``befores``). Review r1 M1: a window that already showed
    the very page asked for and still does (``seen["already"]``) is no page opened: an "already" result (``ran`` False:
    the engine's done-check must see it before ``already_line`` is said, else the next rung runs)."""
    facts_ram = {**ram, "browser": key, "hwnd": int(seen.get("hwnd") or 0), "before": str(seen.get("before") or ""),
                 "befores": dict(seen.get("befores") or {}), "arrived": seen.get("verified") is True,
                 "cold": seen.get("cold") is True, "tabs_before": dict(seen.get("tabs_before") or {})}
    facts = {"browser_from": source, "via": str(seen.get("via") or "")}
    if seen.get("already"):
        return RouteResult(ran=False, already=True, outcome="already", app=key or None, say=already_line or line,
                           facts={**facts, "state": "already"}, facts_ram={**facts_ram, "already": True})
    return RouteResult(ran=True, verified=True if seen.get("verified") else None, outcome="opened", app=key or None,
                       say=line, facts=facts, facts_ram=facts_ram)


# ---------------------------------------------------------------------------------------------------------------------
# web.open_page / web.open_video
# ---------------------------------------------------------------------------------------------------------------------


def _plain_value(value: str) -> bool:
    return bool(value) and not any(ch in value for ch in "/?&=# ")


def page_address(site_word: Any, page_word: Any = None, item: Any = None) -> tuple[str | None, dict[str, Any]]:
    """``(address, what the page in front must be)`` for a site and page kind: a site file's front page or a page
    kind's template filled with the item's value (its link as it stands when that is a page of that kind), or a
    domain's front page (a host-scope page). ``(None, {"why": code})`` when no address can be built from data."""
    from yuki.connect.templates import fill as fill_url, page_of, site as site_data
    from yuki.skills.connection import template_names

    name = _words(site_word).casefold()
    page = _words(page_word).casefold()
    if not name:
        return None, {"why": "no_site"}
    filed = site_file(name)
    data = site_data(filed) if filed else {}
    if not data:
        if domain_like(name) and not page:
            return f"https://{name}/", {"host": name}
        return None, {"why": "no_address"}
    name = filed
    info = data.get("site") or {}
    if not page or page == "home":
        hosts = [str(h) for h in info.get("hosts") or []]
        home = str(info.get("home") or "") or (f"https://{hosts[0]}/" if hosts else "")
        if "home" in (data.get("pages") or {}):
            return (home or None), {"site": name, "kind": "home"}
        # review r1 L5: a site file with no "home" page kind: its front page is matched by host (any of its pages)
        if not (home and hosts):
            return None, {"why": "no_address"}
        return home, {"site": name, "host": min(hosts, key=len)}
    template = str((data.get("urls") or {}).get(page) or "")
    if not template or page not in (data.get("pages") or {}):
        return None, {"why": "no_template"}
    names = template_names(template)
    ref = item.get("ref") if isinstance(item, Mapping) else None
    if ref and "://" in str(ref):
        m = page_of(str(ref))
        if m is not None and m.site == name and m.kind == page and m.entity:
            return str(ref), {"site": name, "kind": page, "entity": str(m.entity)}
        return None, {"why": "other_link"}
    if names == ["id"]:
        # review r1 L5: a template value named "id" (a video's, a channel's) is an opaque identifier the site gave a
        # page; it comes only from a link or an offered option's ref, never from spoken words (a made-up id opens the
        # site's "unavailable" page, which a URL-only check would call done)
        value = _words(ref)
    else:
        value = _words(ref) or _words(item)
    if len(names) != 1 or not _plain_value(value):
        return None, {"why": "needs_link" if names == ["id"] and not ref else "no_value"}
    url = fill_url(name, page, **{names[0]: value})
    return (url, {"site": name, "kind": page, "entity": value}) if url else (None, {"why": "no_template"})


def site_file(site_word: Any) -> str:
    """The site file's name (``yuki/connect/sites/<name>.toml``) a site value names: the name itself, or the site whose
    hosts a domain is one of; ``""`` when none (a host-scope site, or no site). Data only."""
    from yuki.connect.templates import page_of, site as site_data

    name = _words(site_word).casefold()
    if not name:
        return ""
    if name.replace("_", "").isalnum() and site_data(name):
        return name
    if domain_like(name):
        m = page_of(f"https://{name}/")
        if m is not None and site_data(m.site):
            return str(m.site)
    return ""


def _said_in(text: str, words: Sequence[str]) -> bool:
    """Whether ``text`` stands in ``words`` as whole words (:func:`yuki.skills.uia_route.said`). Never raises."""
    if not text:
        return False
    try:
        from yuki.skills.uia_route import said

        return bool(said(text, [str(w) for w in words if w]))
    except Exception:  # noqa: BLE001
        return False


def site_page(store: Any, site_word: Any, page_word: Any, item_word: Any, words: Sequence[str], *,
              now: float | None = None, heard: Sequence[str] | None = None) -> tuple[str | None, str | None, str]:
    """fSkills (live 18 Oct: the talker gave ``page = "Deji"`` for "Deezy's YouTube channel" and ``page = "MBL
    channel"`` for the user's NDL; both pages were dropped and YouTube's front page opened, or nothing): a site page's
    kind and value as the site's data and the user's own names read them: ``(page kind, item, how)``.

    * the item given (or a value given as the page that is no page kind) that one of the user's own names of the
      site's speakable page kind (``taste.site_kinds``: YouTube's ``handle``; ``taste.candidates``) holds as written
      (``listed``), or by sound, the value itself or a run of its words (``heard``, ``taste.heard_as``'s closed-set
      tier: "MBL" for NDL, only among their own names), is that page of that name, whatever page kind came with it
      (review L4: "channel" + "MBL" is their channel NDL's handle page, never a channel id no link gives);
    * else a page that is one of the site file's page kinds stays (``kind``: data, like an option of an enum), unless
      its address takes a value and no item came (review L4: ``page = handle`` alone is no page to open);
    * skill19 (R7): the item given that the user's words don't hold (the caller's own rewrite, "MrBeastGaming" for the
      heard "MBL"): the user's own words (``heard``: the exchange's turns, else ``words``) matched against their own
      names of the page kind by sound or spelling (:func:`yuki.skills.taste.heard_in`) -> that page of that name
      (``heard_words``: Yuki's guess, the done line names it);
    * otherwise ``(None, None, "")``: nothing changed.

    Never raises."""
    try:
        from yuki.connect.templates import site as site_data
        from yuki.skills import taste

        name = site_file(site_word)
        if not name:
            return None, None, ""
        pages = {str(k).casefold(): str(v) for k, v in (site_data(name).get("pages") or {}).items()}
        page = _words(page_word).casefold()
        item = _words(item_word if not isinstance(item_word, Mapping) else item_word.get("name"))
        said = item or ("" if page in pages else _words(page_word))
        kinds = [(kind, str(pk)) for kind, site, pk, _var in taste.site_kinds() if site == name] if said else []
        named: list[tuple[str, list[str]]] = []
        for kind, pk in kinds:
            names = [n for n in taste.candidates(store, kind, now=now) if n]
            named.append((pk, names))
            exact = next((n for n in names if _words(n).casefold() == said.casefold()), None)
            if exact is not None:
                return pk, exact, "listed"
            alike = taste.heard_as(names, said, loose=True)
            if alike and any(taste.heard_as([alike], str(w), loose=True)
                             or _words(alike).casefold() in str(w).casefold() for w in words if w):
                return pk, alike, "heard"
        if named and not _said_in(said, heard if heard is not None else words):
            # skill19 (live 19 Oct, all-browser R7: "NDL" heard as "MBL", the talker passed its own "MrBeastGaming"
            # and @MrBeastGaming opened): the caller's value isn't in the user's words, so before it is taken as the
            # caller's reading, the user's own words are matched against their own names of this page kind by sound
            # or spelling (taste.heard_in: "MBL" -> NDL); a hit is Yuki's guess, named in the done line
            for pk, names in named:
                hit = taste.heard_in(names, heard if heard is not None else words)
                if hit:
                    return pk, hit, "heard_words"
        if page not in pages or (not item and "{" in pages[page]):
            # no page kind, or one whose address needs a value none came with: nothing changed (the resolver checks
            # the page as given, and drops it unless the words hold it: the site's front page)
            return None, None, ""
        # none of their names: a page kind given stays (its item is checked by the resolver as given)
        return page, None, "kind"
    except Exception:  # noqa: BLE001 - nothing changed
        return None, None, ""


def page_matches(url: Any, expect: Mapping[str, Any]) -> bool:
    """Whether ``url`` is the page ``expect`` describes: the same site, page kind and value by the site's templates,
    or (host scope) a page of that domain or one of its subdomains."""
    from yuki.connect.templates import page_of
    from yuki.memory.store import url_host

    if not url or not expect:
        return False
    if expect.get("host"):
        host = (url_host(str(url)) or "").casefold()
        domain = str(expect["host"]).casefold()
        return bool(host) and (host == domain or host.endswith("." + domain))
    m = page_of(str(url))
    if m is None or m.site != str(expect.get("site") or ""):
        return False
    if expect.get("kind") and m.kind != str(expect["kind"]):
        return False
    # a site may show its own canonical case of the value it was opened with (a handle): compared case-folded
    return not expect.get("entity") or str(m.entity or "").casefold() == str(expect["entity"]).casefold()


def _site_title(expect: Mapping[str, Any], said: str) -> str:
    """The site as the user hears it: their own words for it, else the domain or the site file's name."""
    if said:
        return said if not said.islower() else said[:1].upper() + said[1:]
    name = str(expect.get("host") or expect.get("site") or "")
    return name[:1].upper() + name[1:] if name else "It"


def _hosts_of(expect: Mapping[str, Any]) -> list[str]:
    if expect.get("site"):
        return site_hosts(str(expect["site"]))
    if expect.get("host"):
        domain = str(expect["host"])
        return [domain, f"www.{domain}"]
    return []


def route_open_page(call: RouteCall) -> RouteResult:
    """web.open_page's rung A: the page's address (:func:`page_address`) in the chosen browser. Nothing else is
    opened, so nothing plays until the user picks."""
    url, expect, base, already = _target(call, "page")
    if not url:
        return RouteResult(ran=False, outcome=str(expect.get("why") or "no_address"))
    failed, name, key, source, seen = _open_in_browser(call, url, hosts=_hosts_of(expect), bar_next=True)
    if failed is not None:
        return failed
    return _opened_result(_browser_line(base, name, source), key, source, seen, {"expect": expect, "address": url},
                          already_line=_browser_line(already, name, source))


def _target(call: RouteCall, opens: str) -> tuple[str | None, dict[str, Any], str, str]:
    """``(address, what the page must be, the done line before the browser is named, its "already" line)`` of a web
    route: a picked video's own link (``opens = "video"``), else the site's page (:func:`page_address`);
    ``(None, {"why": code}, "", "")`` when no address can be built from data."""
    if opens == "video":
        video = call.values.get("video")
        got = video_page(video.get("ref") if isinstance(video, Mapping) else None)
        if got is None:
            return None, {"why": "no_link"}, "", ""
        return got[0], got[1], LINE_VIDEO_UP, LINE_VIDEO_ALREADY
    said = _words(call.values.get("site"))
    url, expect = page_address(said, call.values.get("page"), call.values.get("item"))
    if not url and expect.get("why") == "no_address" and not _words(call.values.get("page")):
        # names21 (final-D S18; the user's decision 3): a site value no site file and no address names ("Porbex
        # Kaladra") that is, measured, one of the user's own sites (excalidraw.com) opens that site, named in the done
        # line ("Excalidraw.com is up."); none clearly: as before (no address, the next rung or the hand-off)
        took = measured_site(call, said)
        if took:
            said = took
            url, expect = page_address(said)
    if not url:
        return None, expect, "", ""
    site = _site_title(expect, said)
    return url, expect, LINE_PAGE_UP.format(site=site), LINE_PAGE_ALREADY.format(site=site)


def measured_site(call: RouteCall, said: str) -> str:
    """names21 (final-D S18): the user's own site (its host, :func:`yuki.skills.names.own_site_entries`) a site value
    with no address is a mishearing of: the value measured as a name given to the act
    (:func:`yuki.skills.names.best_match` ``as_name``: "Porbex Kaladra" -> excalidraw.com), else the user's newest turn
    measured whole (the stricter turn bar: a value that is the model's description, "drawing site", for "Open Porbex
    Kaladra"; mishearings only: a site the turn names as written is the user's own word for another site, never this
    one, so "close youtube and open my drawing site" is no youtube.com, names21 review F2); one clear winner, else
    ``""``. Logged content-free (``names_match``: where the words came from, the name's source, the score and the
    margin). Never raises."""
    try:
        from yuki.skills import names as N

        entries = N.own_site_entries(_store(call.ctx))
        sites = [e.name for e in entries]
        if not sites:
            return ""
        got, how = (N.best_match(said, sites, as_name=True), "value") if said else (None, "value")
        if got is None:
            turns = [str(w) for w in getattr(call.ctx, "words", None) or () if str(w or "").strip()]
            if turns:
                got, how = N.best_match(turns[-1], sites, literal_ok=False), "turn"
        if got is None:
            return ""
        name, score, margin = got
        source = next((e.source for e in entries if e.name == name), "")
        _log(call.ctx)("names_match", how=how, source=source, score=score, margin=margin, skill=call.skill.key)
        return name
    except Exception:  # noqa: BLE001 - no measure: as before
        return ""


def video_page(ref: Any) -> tuple[str, dict[str, Any]] | None:
    """``(the page address, what the page in front must be)`` of a picked video's link: a page of a site file whose
    template carries the video's value, as it stands. Fixed machine formats (``yuki/connect/sites/*.toml``)."""
    from yuki.connect.templates import page_of

    ref = str(ref or "").strip()
    if "://" not in ref or " " in ref:
        return None
    m = page_of(ref)
    if m is None or not m.entity:
        return None
    return ref, {"site": m.site, "kind": m.kind, "entity": str(m.entity)}


def route_open_video(call: RouteCall) -> RouteResult:
    """web.open_video's rung A: the picked video's own page in the chosen browser (its link from the offered options)."""
    url, expect, base, already = _target(call, "video")
    if not url:
        return RouteResult(ran=False, outcome=str(expect.get("why") or "no_link"))
    failed, name, key, source, seen = _open_in_browser(call, url, hosts=_hosts_of(expect), bar_next=True)
    if failed is not None:
        return failed
    return _opened_result(_browser_line(base, name, source), key, source, seen, {"expect": expect, "address": url},
                          already_line=_browser_line(already, name, source))


# ---------------------------------------------------------------------------------------------------------------------
# skill20 (final-D B6): a video title handed to web.open_video, as its own link, by code
# ---------------------------------------------------------------------------------------------------------------------

#: A title is taken for one video's link only when that link's words hold at least this share of the title's words
#: (a string measure, never a meaning), the next other video's at most :data:`TITLE_GAP` less, and the title has at
#: least :data:`TITLE_MIN_WORDS` distinct words (a one-word name fits too many links to say which).
TITLE_FIT = 0.8
TITLE_GAP = 0.15
TITLE_MIN_WORDS = 3
#: How long the links of a shown page may take to read (one UIA job), how many of them are kept, and how many windows
#: are read (the window in front, then the asked browser's top window).
TITLE_LINKS_S = 1.0
TITLE_LINKS_MAX = 600
TITLE_WINDOWS = 2


def _title_words(text: Any) -> list[str]:
    """The words of a text for :func:`title_fit` (letters and digits, case folded): a machine split, never meaning."""
    import re

    return re.findall(r"\w+", str(text or "").casefold())


def title_fit(title: Any, text: Any) -> float:
    """The share of ``title``'s words that ``text`` holds (each word counted as often as the title has it): ``1.0``
    when a link named "<title> by <channel> 1.2M views" carries every word of the title. Pure."""
    want = _title_words(title)
    if not want:
        return 0.0
    have: dict[str, int] = {}
    for w in _title_words(text):
        have[w] = have.get(w, 0) + 1
    hit = 0
    for w in want:
        if have.get(w, 0) > 0:
            have[w] -= 1
            hit += 1
    return hit / len(want)


def best_title_link(title: Any, candidates: Sequence[tuple[str, str]]) -> tuple[str, str, float] | None:
    """``(the link, its text, its fit)`` of the one video page among ``candidates`` (``(text, address)`` pairs) whose
    text fits ``title`` clearly (:data:`TITLE_FIT`, ahead of every other video by :data:`TITLE_GAP`); one video
    reached by several links counts once (its page identity, :func:`video_page`). ``None``: no title of enough words,
    no page of a site file with an entity, none fits, or two videos fit alike. Pure apart from the site files."""
    if len(set(_title_words(title))) < TITLE_MIN_WORDS:
        return None
    kinds = _video_kinds()
    best: dict[str, tuple[float, str, str]] = {}
    for text, url in candidates:
        got = video_page(url)
        if got is None or (got[1].get("site"), got[1].get("kind")) not in kinds:
            continue
        ident = f"{got[1].get('site')}:{got[1].get('kind')}:{got[1].get('entity')}"
        fit = title_fit(title, text)
        if fit > best.get(ident, (-1.0, "", ""))[0]:
            best[ident] = (fit, got[0], str(text or ""))
    ranked = sorted(best.values(), key=lambda b: -b[0])
    if not ranked or ranked[0][0] < TITLE_FIT:
        return None
    if len(ranked) > 1 and ranked[1][0] > ranked[0][0] - TITLE_GAP:
        return None
    fit, url, text = ranked[0]
    return url, text, fit


def _video_kinds() -> set[tuple[str, str]]:
    """The ``(site, page kind)`` a video's own page is, as the picks build their links (``youtube_feed``'s site and
    watch page): a channel's page or a home page that a title happens to fit is never taken for a video."""
    try:
        from yuki.skills import youtube_feed

        return {(str(youtube_feed.SITE), str(youtube_feed.WATCH_PAGE))}
    except Exception:  # noqa: BLE001
        return set()


def page_links(hwnd: int, budget_s: float = TITLE_LINKS_S) -> list[tuple[str, str]]:
    """``[(name, address)]`` of the links on the main page window ``hwnd`` shows (its Document whose address is the
    main page's, :func:`_main_page`; never a background tab's), at most :data:`TITLE_LINKS_MAX`. One read-only UIA job
    on its own thread (FindAll of Hyperlinks under that Document): nothing is focused, pressed or selected. ``[]`` when
    no main page is shown or the read doesn't answer in ``budget_s``."""
    main, _status = _main_page(hwnd)
    if not main:
        return []
    from yuki.connect.spotify_desktop import UiaTree, run_uia

    def job(automation: Any, shared: dict) -> None:
        tree = UiaTree(automation, int(hwnd))
        doc = next((d for d in tree.find("Document") if d.value == main), None)
        if doc is None:
            return
        links = tree.find("Hyperlink", scope=doc.ref)
        shared["links"] = [(str(a.name or ""), str(a.value or "")) for a in links
                           if a.value and "://" in str(a.value)][:TITLE_LINKS_MAX]

    shared, late = run_uia(job, max(0.2, float(budget_s)), name="yuki-skill-links")
    if late:
        return []
    return list(shared.get("links") or [])


def _feed_titles(store: Any) -> list[tuple[str, str, str]]:
    """``[(title and channel name, the video's page address, its key)]`` of the uploads memory cached from the user's
    channels' feeds (``yt_videos``, decrypted in this process only), each video's own page by the site file's
    template. ``[]`` when there are none. Never raises."""
    try:
        from yuki.connect.templates import fill
        from yuki.skills import youtube_feed

        rows = store.yt_videos()
        names = store.yt_channels(sorted({str(r.get("channel_key") or "") for r in rows if r.get("channel_key")}))
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[str, str, str]] = []
    for r in rows:
        vid = str(r.get("video_id") or "")
        url = fill(youtube_feed.SITE, youtube_feed.WATCH_PAGE, id=vid) if vid else None
        if not url:
            continue
        channel = str((names.get(str(r.get("channel_key") or "")) or {}).get("name") or "")
        out.append((" ".join(f"{r.get('title') or ''} {channel}".split()), url, str(r.get("video_key") or "")))
    return out


def link_for_title(title: Any, *, store: Any = None, windows: Sequence[int] = ()) -> dict[str, Any] | None:
    """skill20 (final-D B6: the agent read a video's title off YouTube's home page in Arc and handed web.open_video
    that title with no link; every rung needs a link, so the run ended no_route in 1 ms and the agent gave up): the
    title as one video's own link, by code. First the uploads memory cached from the user's channels' feeds
    (:func:`_feed_titles`), then the links of the main page each of ``windows`` shows (:func:`page_links`), each by
    :func:`best_title_link` (a string measure: one clear fit, never two alike). ``{"name", "ref", "via", "fit"}`` (and
    the upload's ``key`` from the feed): ``via`` ``feed`` or ``page``. ``None`` when none fits clearly (the run goes on
    as before). Read-only; content stays in RAM."""
    if len(set(_title_words(title))) < TITLE_MIN_WORDS:
        return None
    if store is not None:
        feed = _feed_titles(store)
        got = best_title_link(title, [(text, url) for text, url, _key in feed])
        if got is not None:
            key = next((k for _t, u, k in feed if u == got[0]), "")
            return {"name": _words(title), "ref": got[0], "via": "feed", "fit": round(got[2], 2),
                    **({"key": key} if key else {})}
    for hwnd in list(dict.fromkeys(int(h) for h in windows if h))[:TITLE_WINDOWS]:
        got = best_title_link(title, page_links(hwnd))
        if got is not None:
            url = got[0]
            canon = _canonical_video(url)
            return {"name": _words(title), "ref": canon or url, "via": "page", "fit": round(got[2], 2)}
    return None


def _canonical_video(url: str) -> str | None:
    """A video page's address as its site file builds it (``https://www.youtube.com/watch?v=<id>`` for a link that
    carried more query parameters), or ``None`` when the template can't be filled."""
    try:
        from yuki.connect.templates import fill, page_of

        m = page_of(url)
        if m is None or not m.values:
            return None
        return fill(m.site, m.kind, **m.values)
    except Exception:  # noqa: BLE001
        return None


def _web_window(hwnd: int, image: str = "", pid: int | None = None) -> bool:
    """Whether window ``hwnd``'s program is a web browser by machine data (:func:`yuki.voice.hands.web_app`: Windows
    lists it as a browser, or its package declares web addresses, as Arc's does). A chat app that draws a site in its
    own window never is. Read-only; ``False`` when it can't be told."""
    try:
        if pid is None or not image:
            import win32process

            pid = int(win32process.GetWindowThreadProcessId(int(hwnd))[1])
            if not image:
                from yuki.skills.donecheck import _image_of

                image = _image_of(pid)
        from yuki.voice.hands import web_app

        return bool(web_app(image, pid))
    except Exception:  # noqa: BLE001
        return False


def title_windows(browser: Any = None) -> list[int]:
    """The windows whose shown page :func:`link_for_title` reads: the window in front, then the top window of the
    browser the request named (its app key), read only, and each only when it is a browser's by machine data
    (:func:`_web_window`; skill20 review: a link a friend posted in a chat app is never the match)."""
    out: list[int] = []
    front, image = _front()
    if front and _web_window(int(front), image):
        out.append(int(front))
    key = _app_key(browser)
    if key:
        out.extend(h for h, pid in _app_windows(key)[:1] if _web_window(int(h), pid=int(pid)))
    return list(dict.fromkeys(out))[:TITLE_WINDOWS]


def _front_urls(hwnd: int) -> list[str] | None:
    """The addresses of every Document of window ``hwnd`` (a browser's pages), read in one UIA job (never the
    foreground: the caller names the window). skill20 (final-D B1): every one, never the first few; this read can't
    tell the page shown from a background tab's (Chrome exposed 6-8 of those on 19 Oct), so :func:`_shown_page` uses it
    only when the main-page read found nothing, and never takes a hit here as the page shown while it holds others."""
    from yuki.connect.spotify_desktop import UiaTree, run_uia

    def job(automation: Any, shared: dict) -> None:
        docs = UiaTree(automation, int(hwnd)).find("Document")
        shared["urls"] = [d.value for d in docs if d.value and "://" in d.value]

    shared, late = run_uia(job, FRONT_READ_S, name="yuki-skill-page")
    if late:
        return None
    return list(shared.get("urls") or [])


def _main_page(hwnd: int) -> tuple[str, str]:
    """skill20 (final-D B1): the address of the main page window ``hwnd`` shows, by the reader the rungs themselves
    watch with (:func:`yuki.perception.tree.wait_for_page`: its page-only pass leaves background tabs' and stacked
    pages out, the main one first). ``(address, "page")``; ``("", "no_page")`` read and no main page shown (a tab
    still loading); ``("", "timeout")`` the window didn't answer in :data:`PAGE_READ_S`."""
    wait = _page_wait(lambda: [hwnd], bool, PAGE_READ_S)
    if wait is None:
        return "", "timeout"
    main = next((p for p in getattr(wait, "seen", None) or [] if p.main and int(p.hwnd) == int(hwnd)), None)
    url = str(getattr(main, "url", "") or "")
    if url:
        return url, "page"
    return "", ("no_page" if getattr(wait, "answered", False) else "timeout")


def _page_window(ctx: Any) -> tuple[int, str]:
    """``(hwnd, how)`` of the window a page check reads (x8WebArc): the window the route opened the page in (its RAM
    ``hwnd``, while it is still a window of that browser): ``route``; else the top window of the browser it opened it in
    (``browser``; 0 when that browser has none): ``browser``; else, with no route facts at all, the window in front:
    ``front``. The foreground is never read while the route's window or browser is known: on 10 Oct the check read the
    window in front (Arc's command bar, or Yuki's own window) 19 times and never saw the page."""
    ram = _ram(ctx)
    key = _app_key(ram.get("browser"))
    try:
        hwnd = int(ram.get("hwnd") or 0)
    except (TypeError, ValueError):
        hwnd = 0
    if hwnd and _is_window(hwnd) and (not key or _app_key(_window_image(hwnd)) == key):
        return hwnd, "route"
    if key:
        return next((h for h, _pid in _app_windows(key)), 0), "browser"
    front, _image = _front()
    return front, "front"


def _page_key(url: str) -> Any:
    """An address as "the same page" (``launch._page_key``: scheme, host without ``www.``, port, path, query pairs)."""
    try:
        from yuki.actions.launch import _page_key as key

        return key(str(url or ""))
    except Exception:  # noqa: BLE001
        return str(url or "")


def _past_root(requested: str, actual: str) -> bool:
    """V11 (``launch._past_root``): ``actual`` matches ``requested`` only because a request for a site's root takes any
    page of that site."""
    try:
        from yuki.actions.launch import _past_root as past

        return bool(requested and actual and past(str(requested), str(actual)))
    except Exception:  # noqa: BLE001
        return False


def _before_of(ram: Mapping[str, Any], hwnd: int) -> str | None:
    """The page window ``hwnd`` showed before the rung acted: from the rung's read of the browser's windows
    (``befores``), else its own window's (``before``); ``""`` none (the browser wasn't running: RAM ``cold``);
    ``None`` not known."""
    befores = ram.get("befores")
    if isinstance(befores, Mapping) and hwnd in befores:
        return str(befores[hwnd] or "")
    if ram.get("cold") is True:
        return ""
    try:
        own = int(ram.get("hwnd") or 0)
    except (TypeError, ValueError):
        own = 0
    if own and own == hwnd and ram.get("before"):
        return str(ram["before"])
    return None


def _shown_page(ctx: Any, matches: Callable[[str], bool]) -> tuple[bool | None, dict[str, Any]]:
    """The page check of a browser route: the window :func:`_page_window` names shows a page ``matches`` takes. Another
    page there: False (contradicted, so the engine's next rung runs). Unless the rung itself saw the page newly arrive
    (RAM ``arrived``, set only then; review r1 H1):

    * the very page that window showed before the rung acted (:func:`_before_of`) is False (``unchanged``: an old tab is
      no proof the address was opened);
    * with that window's earlier page not known, a page deeper than a requested site root (V11, RAM ``address``) is
      None (``not_known``): it may well have been there before, so never a "done" on it.

    A rung that found the very page asked for already showing (RAM ``already``; review r1 M1) is True on it (the
    engine's "already" path, never a fresh "done"). No window, or no readable page: None. Evidence (content-free):
    ``status``, ``via`` (which window was read), ``process`` (that window's program) and ``reader``.

    skill20 (final-D B1: Chrome exposed its background tabs' pages, the first four Documents were read as the page
    shown, and ``other_page`` came 11-13 times over 6 s while the video played): the window's **main page** is read
    first, by the rungs' own page-only reader (:func:`_main_page`, ``reader = "main_page"``). Only when that finds no
    page are the window's Documents read (``reader = "documents"``): no Document of the page anywhere in the window is
    False (unless the rung saw the page arrive: then True, ``arrived``); a hit there while the window holds other
    Documents too can't be told from a background tab, so it is None (``background_only``), never a done and never a
    contradiction. ``other_page`` is said only on a main-page read, or with no Document of the page at all."""
    hwnd, how = _page_window(ctx)
    if not hwnd:
        return (None if how == "front" else False), {"status": "unread" if how == "front" else "no_window", "via": how}
    ev: dict[str, Any] = {"via": how, "process": _window_image(hwnd)}
    ram = _ram(ctx)
    seen_arrive = ram.get("arrived") is True and _ram_hwnd(ram) == hwnd
    main, read = _main_page(hwnd)
    if main:
        ev["reader"] = "main_page"
        hits = [main] if matches(main) else []
        if not hits:
            return False, {**ev, "status": "other_page"}
    elif seen_arrive:
        # the main-page reader reads no page in this window now (a tab still drawing, a provider stall), and the rung
        # itself saw the page newly arrive in this very window after it acted (a page-only read of it, RAM
        # ``arrived``): that sighting stands; never "other page" without a main-page read (fSkills, live 18 Oct Arc)
        return True, {**ev, "status": "arrived", "reader": read}
    else:
        urls = _front_urls(hwnd)
        ev["reader"] = "documents"
        if not urls:
            return None, {**ev, "status": "timeout" if urls is None else "no_document"}
        hits = [url for url in urls if matches(url)]
        if not hits:
            return False, {**ev, "status": "other_page"}
        if len(urls) > 1:
            return None, {**ev, "status": "background_only"}
    if ram.get("already") is True:
        return True, {**ev, "status": "already"}
    if ram.get("arrived") is not True:
        before = _before_of(ram, hwnd)
        if before and all(_page_key(url) == _page_key(before) for url in hits):
            # fSkills (live 18 Oct: YouTube asked for while the window's tab showed YouTube: "unchanged" for 6 s, the
            # rung charged a false contradiction and, with the user's next request, disabled): the page alone can't
            # tell a new tab on the same address from the old one, so the selected tab's identity does (its UIA
            # RuntimeId, read before the rung acted): another tab selected now, showing the page, is the page opened
            moved = _tab_moved(ram, hwnd)
            if moved is True:
                return True, {**ev, "status": "new_tab"}
            if moved is None and _tabs_known(ram, hwnd):
                return None, {**ev, "status": "tabs_unread"}     # known before, unreadable now: never a contradiction
            return False, {**ev, "status": "unchanged"}
        address = str(ram.get("address") or "")
        if before is None and all(_past_root(address, url) for url in hits):
            return None, {**ev, "status": "not_known"}
    return True, {**ev, "status": "page"}


def _ram_hwnd(ram: Mapping[str, Any]) -> int:
    try:
        return int(ram.get("hwnd") or 0)
    except (TypeError, ValueError):
        return 0


def _tabs_known(ram: Mapping[str, Any], hwnd: int) -> bool:
    tabs = ram.get("tabs_before")
    return isinstance(tabs, Mapping) and isinstance(tabs.get(hwnd), list)


def _tab_moved(ram: Mapping[str, Any], hwnd: int) -> bool | None:
    """Whether window ``hwnd``'s selected tab is another tab than the one selected before the rung acted (RAM
    ``tabs_before``: ``{hwnd: [RuntimeIds]}``, :func:`_tabs_now`): ``True`` another, ``False`` the same, ``None``
    unknown (not read before, or unreadable now)."""
    if not _tabs_known(ram, hwnd):
        return None
    before = [str(t) for t in ram["tabs_before"][hwnd]]
    now = _selected_tabs(hwnd)
    if not now or not before:
        return None
    return not set(now) & set(before)


def _tabs_before(key: str, url: str) -> dict[int, list[str]]:
    """fSkills review L2: :func:`_tabs_now` of the browser's top window(s), only when its title (the selected page's
    own) may show the asked address's site: it holds the site's name (the registrable domain's first label, letters
    and digits, case aside), or it can't be read. That is the one case the done-check needs the tab's identity for (a
    new tab on an address already showing); any other open skips the tab walk (~110 ms, up to
    :data:`FRONT_READ_S`). A title that hides the name only costs that case its identity (the old rule: unchanged)."""
    windows = [h for h, _pid in _app_windows(key)][:TABS_BEFORE_WINDOWS]
    if not windows:
        return {}
    try:
        from yuki.memory.moments import registrable_domain
        from yuki.memory.store import url_host

        label = "".join(ch for ch in registrable_domain(url_host(url) or "").split(".")[0] if ch.isalnum())
    except Exception:  # noqa: BLE001 - no name to look for: read
        label = ""
    worth = [h for h in windows if _title_may_show(h, label)]
    return _tabs_now(worth) if worth else {}


def _title_may_show(hwnd: int, label: str) -> bool:
    """Whether window ``hwnd``'s title holds ``label`` (letters and digits run together, case aside), or can't tell
    (no label, no title): a fixed comparison of two spellings, never a list of sites."""
    if not label:
        return True
    try:
        import win32gui

        title = str(win32gui.GetWindowText(int(hwnd)) or "")
    except Exception:  # noqa: BLE001
        return True
    letters = "".join(ch for ch in title.casefold() if ch.isalnum())
    return not letters or label in letters


def _tabs_now(hwnds: Sequence[int]) -> dict[int, list[str]]:
    """``{hwnd: [the selected tabs' RuntimeIds]}`` of a browser's windows right before a rung acts (fSkills: the
    identity :func:`_shown_page` tells a new tab on the same address by), at most :data:`TABS_BEFORE_WINDOWS` windows,
    each one bounded UIA read; a window whose tabs can't be read is left out."""
    out: dict[int, list[str]] = {}
    for hwnd in list(dict.fromkeys(int(h) for h in hwnds if h))[:TABS_BEFORE_WINDOWS]:
        got = _selected_tabs(hwnd)
        if got:
            out[hwnd] = list(got)
    return out


def probe_page_shown(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """web.open_page's done-check: the browser window the route opened the page in shows it (by template value, or by
    host for a host-scope page; :func:`_shown_page`). After a hand-off (no route result) it can't say: None."""
    expect = _ram(ctx).get("expect")
    if not isinstance(expect, Mapping) or not expect:
        return None, {"status": "no_route"}
    return _shown_page(ctx, lambda url: page_matches(url, expect))


def probe_video_shown(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """web.open_video's done-check: the browser window the route opened it in shows that very video page. After a
    hand-off (no route result) it can't say which page counts: None (never a false done)."""
    expect = _ram(ctx).get("expect")
    if not isinstance(expect, Mapping) or not expect:
        return None, {"status": "no_route"}
    return _shown_page(ctx, lambda url: page_matches(url, expect))


# ---------------------------------------------------------------------------------------------------------------------
# x8WebArc: the address-bar rung
# ---------------------------------------------------------------------------------------------------------------------


def _page_wait(windows: Callable[[], list[int]], accept: Callable[[str], bool], timeout_s: float,
               stale: Callable[[str], bool] | None = None) -> Any:
    """:func:`yuki.perception.tree.wait_for_page` (a page-only read on its own thread, V11's first-look ``stale``
    rule), or ``None`` when it can't run."""
    try:
        from yuki.perception.tree import wait_for_page

        return wait_for_page(windows, accept, timeout_s=max(0.0, float(timeout_s)), stale=stale)
    except Exception:  # noqa: BLE001
        return None


def _first_page(windows: Callable[[], list[int]], accept: Callable[[str], bool], timeout_s: float) -> Any:
    """The first page one of ``windows()`` shows that ``accept`` takes, within ``timeout_s``, else ``None``."""
    return getattr(_page_wait(windows, accept, timeout_s), "matched", None)


def _pages_now(hwnds: Sequence[int], budget_s: float = BAR_BEFORE_S) -> dict[int, str]:
    """``{hwnd: address}``: the main page each of ``hwnds`` shows right now (review r1 H1: every window the
    address-bar rung later looks in, read before it acts), at most :data:`BAR_BEFORE_WINDOWS` of them in the order
    given, each for at most :data:`BAR_BEFORE_WINDOW_S`, all within ``budget_s``; a window that shows no readable page
    is left out. The first page a window's read takes is its main one."""
    out: dict[int, str] = {}
    end = time.monotonic() + max(0.0, float(budget_s))
    for hwnd in list(dict.fromkeys(int(h) for h in hwnds if h))[:BAR_BEFORE_WINDOWS]:
        left = end - time.monotonic()
        if left <= 0.05:
            break
        page = _first_page(lambda h=hwnd: [h], bool, min(BAR_BEFORE_WINDOW_S, left))
        url = str(getattr(page, "url", "") or "")
        if url:
            out[hwnd] = url
    return out


def _arrived_page(wait: Any, expect: Mapping[str, Any], url: str, befores: Mapping[int, str],
                  known: set[int]) -> Any:
    """The page the address-bar rung opened, from its watch after Enter (review r1 H1), else ``None``: the page the
    watch took (a page of the target that no window showed before; a page deeper than a requested root that was
    already on the first look never counts: V11); else, from its last read, the main page of a window that showed
    another page before and now shows the target (not deeper than a requested root), or of a window that appeared
    since (not in ``known``). A window's own earlier page never counts."""
    if wait is None:
        return None
    if getattr(wait, "matched", None) is not None:
        return wait.matched
    for page in getattr(wait, "seen", None) or []:
        if not page.main or not page_matches(page.url, expect) or _past_root(url, page.url):
            continue
        if page.hwnd in befores:
            if _page_key(page.url) != _page_key(befores[page.hwnd]):
                return page
        elif page.hwnd not in known:
            return page
    return None


def frame_field(pid: int, budget_s: float = FIELD_READ_S) -> tuple[bool | None, str]:
    """Whether keyboard focus is on a text field of the browser's own frame (its address or command bar) and not inside
    a page: the focused element belongs to the browser's process ``pid``, is an edit box or an editable combo box, and
    nothing above it up to the desktop is a Document (a page's own box, a chat's composer or a search field, always
    is). ``(True, "frame_field")``; ``(False, code)`` read and not so; ``(None, code)`` couldn't read. Structure only,
    one UIA job on its own thread: nothing here knows which browser it is, or reads a name."""
    from yuki.connect.spotify_desktop import run_uia

    def job(automation: Any, shared: dict) -> None:
        element = automation.GetFocusedElement()
        if not element:
            shared["status"] = "no_focus"
            return
        shared["pid"] = int(element.CurrentProcessId or 0)
        shared["type"] = int(element.CurrentControlType or 0)
        walker = automation.ControlViewWalker
        node, depth = element, 0
        while node and depth < CLIMB_MAX:
            if int(node.CurrentControlType or 0) == UIA_DOCUMENT:
                shared["in_page"] = True
                return
            node = walker.GetParentElement(node)
            depth += 1
        # past the desktop root (a null parent): no Document above it; the climb cut short: unknown
        shared["in_page"] = None if node else False

    shared, late = run_uia(job, budget_s, name="yuki-skill-field")
    if late:
        return None, "timeout"
    if shared.get("error"):
        return None, "unread"
    if shared.get("status"):
        return None, str(shared["status"])
    if not pid or int(shared.get("pid") or 0) != int(pid):
        return False, "other_process"
    if shared.get("in_page") is True:
        return False, "in_page"
    if shared.get("in_page") is None:
        return None, "climb_cut"
    if int(shared.get("type") or 0) not in (UIA_EDIT, UIA_COMBOBOX):
        return False, "not_a_field"
    return True, "frame_field"


def _keys() -> Any:
    from yuki.actions import input as keys

    return keys


def _bar_window(call: RouteCall, key: str, name: str, source: str, end: float) -> tuple[int, str]:
    """``(hwnd, where it came from)`` of the browser window the address-bar rung types into (lSkills, live 17 Oct: this
    rung found "no window" in 96 ms with Arc up, after Arc refused protocol activation): the window the rung before
    acted in (``noted``); else that browser's top user window by its program (``listed``); else its own window by the
    Start menu app's identity (``launch.app_window``: its AppUserModelID or package, so a packaged browser is found
    whatever its process is called; a window hidden in the background shown, or the browser brought up when it shows
    none: ``open`` / ``minimized`` / ``hidden`` / ``launched``); ``(0, code)`` when none (``none``, ``no_app``,
    ``refused`` with a game in front, and ``blocked`` for a launch stopped by anything but a game: fHands, the gate
    failed closed or another guard refused it). Code words only in the facts."""
    hwnd = _noted_window(call, key)
    if hwnd:
        return hwnd, "noted"
    hwnd = next((h for h, _pid in _app_windows(key)), 0)
    if hwnd:
        return hwnd, "listed"
    app = name if source != "default" and name else _browser_title(key)
    try:
        from yuki.actions import launch

        got, how = launch.app_window(app, deadline=end)
    except Exception:  # noqa: BLE001
        return 0, "none"
    return (int(got), str(how)) if got else (0, str(how or "none"))


def _running_count(key: str) -> int:
    """How many processes of the program whose app key is ``key`` run now (a content-free count for the log)."""
    try:
        import psutil

        n = 0
        for p in psutil.process_iter(["name"]):
            if _app_key(p.info.get("name") or "") == key:
                n += 1
        return n
    except Exception:  # noqa: BLE001
        return -1


def route_address_bar(call: RouteCall) -> RouteResult:
    """web.open_page's and web.open_video's second rung A (x8WebArc; the path that worked in Arc on 5 Oct): the address
    typed into the chosen browser's own address field, in its own window.

    The browser is the one :func:`choose_browser` picks (the same as the rung before); its window is the one that rung
    acted in (kept for this run), else that browser's top window. It comes forward (the guarded ``focus_window``), the
    route's ``new_tab`` keys open a new tab with its address field focused (in Arc: the command bar), and only when
    focus is then on a text field of the browser's own frame in that same program (:func:`frame_field`: never a
    page's own box, so it can never send anything) is the address typed and read back (``type_text`` without Enter);
    an inline completion the field added after it is deleted, and Enter navigates. Then the browser's windows are read
    for the page (:data:`BAR_PAGE_S`). Anything short of typing the address (no window, the keys refused, no frame
    field, the text not kept) did not run: the engine hands off. Every key goes through Yuki's guarded input (nothing
    with a game in front: D17).

    Review r1 H1/M1: before it acts, the page each of the browser's windows shows is read (:func:`_pages_now`). Its own
    window already on the very page asked for: nothing is typed, it comes forward and the result is "already" (the
    engine's done-check must see it). After Enter, only a page newly at the address counts (:func:`_arrived_page`;
    ``verified`` and RAM ``arrived`` only then): a window's own earlier page (the GitHub 404 that stayed) is never "the
    page opened", so the done-check finds it ``unchanged`` (False) and the hand-off runs."""
    started = time.monotonic()
    a = call.args if isinstance(call.args, Mapping) else {}
    url, expect, base, already_base = _target(call, str(a.get("opens") or "page"))
    if not url:
        return RouteResult(ran=False, outcome=str(expect.get("why") or "no_address"))
    new_tab = [str(k) for k in (a.get("new_tab") or []) if str(k or "").strip()]
    if not new_tab:
        return RouteResult(ran=False, outcome="no_keys")
    name, key, source = choose_browser(call, hosts=_hosts_of(expect))
    if source == "ask":
        return _ask_browser(name)
    if source == "default" and not key:
        key = _handler_key(url)
    facts: dict[str, Any] = {"browser_from": source, "via": "address_bar"}
    if not key:
        return RouteResult(ran=False, outcome="no_browser", facts=facts)
    end = max(_launch_deadline(call), started + BAR_FLOOR_S)
    hwnd, window_from = _bar_window(call, key, name, source, end)
    facts["how"] = window_from      # where the window came from (a code word the content-free log keeps: "how")
    pid = _window_pid(hwnd) if hwnd else 0
    if not hwnd or not pid:
        if window_from == "refused":
            return RouteResult(ran=False, held="game", outcome="refused", facts=facts)
        if window_from == "blocked":
            # fHands merge note 5: the launch was stopped by something other than a game: the engine's settled stop
            # (said plainly), never "no_window" and the next rung
            return RouteResult(ran=False, held="blocked", outcome="refused", facts=facts)
        facts["procs"] = _running_count(key)     # live 17 Oct: "no_window" with Arc up; which it was, next time
        return RouteResult(ran=False, outcome="no_window", facts=facts)
    if kind := _refused("focus", hwnd=hwnd):
        return RouteResult(ran=False, held=kind, outcome="refused", facts=facts)
    seen: dict[str, Any] = {"via": "address_bar", "process": _window_image(hwnd), "front": False, "state": ""}
    # review r1 H1: what each window it will look in shows now (its own first): a page a window already showed is
    # never the page this rung opened, in that window (the done-check compares with these too)
    others = [h for h, _pid in _app_windows(key) if h != hwnd]
    known = {hwnd, *others}
    befores = _pages_now([hwnd] + others)
    before_url = befores.get(hwnd, "")
    # the done-check reads this window by the program it belongs to (found by identity, its image is the truth)
    ram: dict[str, Any] = {"expect": expect, "address": url, "browser": _app_key(seen["process"]) or key,
                           "befores": befores}
    _claim_app(call, key)
    focused = _focus(hwnd)
    if kind := _held_by_game(focused):
        return RouteResult(ran=False, held=kind, outcome="refused", facts=facts)
    if not getattr(focused, "ok", False):
        _log_open(call, "address_bar", seen, "not_focused")
        return RouteResult(ran=False, outcome="not_focused", facts=facts)
    seen["front"] = True
    if before_url and page_matches(before_url, expect) and not _past_root(url, before_url):
        # review r1 M1: its window already shows the very page asked for (now in front): nothing is typed; "already",
        # once the engine's done-check sees it (else the next rung runs: ran is False)
        seen["state"] = "already"
        _log_open(call, "address_bar", seen, "already")
        return RouteResult(ran=False, already=True, outcome="already", app=key,
                           say=_browser_line(already_base, name, source), facts={**facts, "status": "already"},
                           facts_ram={**ram, "hwnd": hwnd, "before": before_url, "already": True, "arrived": False})
    keys = _keys()
    opened = keys.hotkey(*new_tab, expect_hwnd=hwnd)
    if kind := _held_by_game(opened):
        return RouteResult(ran=False, held=kind, outcome="refused", facts=facts)
    if not getattr(opened, "ok", False):
        _log_open(call, "address_bar", seen, "keys_refused")
        return RouteResult(ran=False, outcome="keys_refused", facts=facts)
    details = getattr(opened, "details", None) or {}
    try:
        field_hwnd = int(details.get("foreground_hwnd") or 0) if isinstance(details, Mapping) else 0
    except (TypeError, ValueError):
        field_hwnd = 0
    if not field_hwnd or _window_pid(field_hwnd) != pid:
        # the keys took focus out of the browser (another program in front now): nothing is typed anywhere
        _log_open(call, "address_bar", seen, "other_window")
        return RouteResult(ran=False, outcome="other_window", facts=facts)
    ok, why = frame_field(pid)
    if ok is None and why in FIELD_SETTLING:
        # fSkills (live 18 Oct, Arc: its command bar just opened, then the thinker by steps, 17 s): a read that found
        # focus still moving is read once more after the bar has settled (structure only; still never typed unless it
        # is the frame's field). A read that timed out or failed is not (review L3: a blocked provider would only
        # leave another UIA thread behind and stretch the rung toward 3.2 s): the longer first read covers it
        time.sleep(FIELD_SETTLE_S)
        ok, why = frame_field(pid)
    facts["stage"] = why
    if ok is not True:
        # focus is not on the browser's own address field (or it couldn't be read): never typed into, so never sent
        _log_open(call, "address_bar", seen, "no_field")
        return RouteResult(ran=False, outcome="no_address_field", facts=facts)
    typed = keys.type_text(url, expect_hwnd=field_hwnd)
    if kind := _held_by_game(typed):
        return RouteResult(ran=False, held=kind, outcome="refused", facts=facts)
    typed_facts = getattr(typed, "details", None) or {}
    if not getattr(typed, "ok", False) or typed_facts.get("readback") != "arrived":
        # not kept, or unreadable: no Enter on an address that may be half there; the field is left as it was opened
        keys.press("escape", expect_hwnd=field_hwnd)
        _log_open(call, "address_bar", seen, "not_typed")
        return RouteResult(ran=False, outcome="not_typed", facts=facts)
    if typed_facts.get("readback_source") == "caret" or \
            len(" ".join(str(typed_facts.get("readback_text") or "").split())) > len(url):
        # the field completed the address inline (the rest selected after what was typed; a read up to the caret can't
        # see it): that rest goes, so Enter opens the address asked for (in a field opened empty, nothing else follows
        # the caret, and with nothing selected the key does nothing)
        keys.press("delete", expect_hwnd=field_hwnd)
    entered = keys.press("enter", expect_hwnd=field_hwnd)
    if kind := _held_by_game(entered):
        return RouteResult(ran=False, held=kind, outcome="refused", facts=facts)
    if not getattr(entered, "ok", False):
        _log_open(call, "address_bar", seen, "enter_refused")
        return RouteResult(ran=False, outcome="enter_refused", facts=facts)
    watch = max(BAR_PAGE_MIN_S, min(BAR_PAGE_S, end - time.monotonic()))
    # review r1 H1: only a page newly at the address counts as the one this rung opened: never one a window showed
    # before (its own or, by key, another's: a second window on the same page is settled window by window after), and
    # never a page deeper than a requested root that was there on the first look (V11)
    old = {_page_key(u) for u in befores.values()}
    wait = _page_wait(lambda: [hwnd] + [h for h, _pid in _app_windows(key) if h != hwnd],
                      lambda u: page_matches(u, expect) and _page_key(u) not in old, watch,
                      stale=lambda u: _past_root(url, u))
    page = _arrived_page(wait, expect, url, befores, known)
    at = int(getattr(page, "hwnd", 0) or 0) or hwnd
    now_main = next((p.url for p in (getattr(wait, "seen", None) or []) if p.hwnd == hwnd and p.main), "")
    seen["state"] = "new" if page is not None else (
        "same" if before_url and now_main and _page_key(now_main) == _page_key(before_url)
        else "other" if now_main else "")
    facts["status"] = "page" if page is not None else "not_seen"
    _note_window(call, key, at)
    _log_open(call, "address_bar", seen, facts["status"])
    # verified only on a page seen newly arriving (RAM ``arrived``); otherwise the done-check decides, and an unchanged
    # page there is False (the hand-off runs), never a "done"
    return RouteResult(ran=True, verified=True if page is not None else None, outcome="typed", app=key,
                       say=_browser_line(base, name, source), facts=facts,
                       facts_ram={**ram, "hwnd": at, "before": befores.get(at, ""), "arrived": page is not None})


def probe_video_playing(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """web.open_video's playing check (part of its done-check, review r1 M2, and its re-check): the browser's own
    media session says playing. The browser is the one the route opened the page in; after a hand-off (no route
    result) the program of the window in front (the page the done-check's other part reads is in it)."""
    from yuki.skills.donecheck import probe_media_session

    result = getattr(ctx, "result", None)
    browser = str(_ram(ctx).get("browser") or getattr(result, "app", "") or "")
    if not browser:
        _hwnd, image = _front()
        browser = _app_key(image)
    if not browser:
        return None, {"status": "no_browser"}
    return probe_media_session(ctx, {"app": browser, "status": "playing"})


def probe_page_seen(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """web.open_page's re-check from a second source: memory's own read of the window the route opened the page in
    (the watcher's Now board) shows that page (``site`` / ``kind`` in the check: any page of them). A stale, blocked
    or empty read: None.

    skill20 (final-D B2: three of these said "false done" on pages that had opened, read while Chrome came forward
    and before the new tab drew, and two such marks disabled the rung for 24 h): only a board read taken **after the
    done verdict** counts (the engine stamps it, :data:`yuki.skills.engine.DONE_AT`; with none, after the run began),
    of the very window the route opened the page in when it named one (the window in front only when it named none),
    with an address read. Anything else is a source that never read the page: None (``stale`` / ``not_read`` /
    ``no_url``), never a contradiction."""
    ram = _ram(ctx)
    expect = ram.get("expect")
    if not isinstance(expect, Mapping) or not expect:
        expect = {k: args[k] for k in ("site", "kind", "host") if args.get(k)}
    if not expect:
        return None, {"status": "no_route"}
    board = _board_now()
    if not board:
        return None, {"status": "no_board"}
    windows = [w for w in board.get("windows") or [] if isinstance(w, Mapping)]
    want = _ram_hwnd(ram)
    # x8WebArc: the window the route opened the page in; the one in front only when the route named no window
    if want:
        front = next((w for w in windows if _int(w.get("hwnd")) == want), None)
        if front is None:
            return None, {"status": "not_read"}
    else:
        front = next((w for w in windows if w.get("foreground")), None)
    if front is None:
        return None, {"status": "no_front"}
    after = max(float(getattr(ctx, "since", 0.0) or 0.0), _float(ram.get(DONE_AT)))
    if front.get("blocked") or not front.get("read_at") or _float(front.get("read_at")) < after:
        return None, {"status": "stale"}
    if not str(front.get("url") or "").strip():
        return None, {"status": "no_url"}
    ok = page_matches(front.get("url"), expect)
    return ok, {"status": "page" if ok else "other_page"}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _board_now(wait_s: float = 0.15) -> dict[str, Any] | None:
    """Memory's Now board (the watcher's latest reads), or ``None`` (no service, no answer in time)."""
    try:
        from yuki.memory.now import shared_client

        client = shared_client()
        return client.now(wait_s=wait_s) if client is not None else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------------------------------------------------
# message: the chat's own web address
# ---------------------------------------------------------------------------------------------------------------------


def chat_web_address(data: Mapping[str, Any], urls: Sequence[str]) -> str | None:
    """The first of a chat's captured addresses that the chat app's own ``[chat.links]`` URL shapes recognise (L4:
    an address is a route only when the app's link data matches it), else ``None``."""
    from yuki.connect.templates import match_template

    for link in data.get("links") or []:
        if not isinstance(link, Mapping) or not link.get("url"):
            continue
        for url in urls:
            if url and match_template(str(link["url"]), str(url)):
                return str(url)
    return None


def _same_page(a: Any, b: Any) -> bool:
    """Two addresses of one page: the same host and path (the query, the fragment and a trailing slash aside)."""
    from urllib.parse import urlsplit

    try:
        x, y = urlsplit(str(a or "")), urlsplit(str(b or ""))
    except ValueError:
        return False
    hx, hy = (x.hostname or "").casefold(), (y.hostname or "").casefold()
    return bool(hx) and hx == hy and x.path.rstrip("/") == y.path.rstrip("/")


def route_chat_web_link(call: RouteCall) -> RouteResult:
    """message's web rung A: the chat's own web address (:func:`chat_web_address`) in a browser (the one that address's
    site was last shown in, unless a learned skill carries another fill policy). Opening only: with words to type it
    doesn't run (the route's data says so too), so it never sends."""
    from yuki.memory.store import url_host
    from yuki.skills import chat

    if _words(call.values.get("text")):
        return RouteResult(ran=False, outcome="no_web_compose")
    store = _store(call.ctx)
    if store is None:
        return RouteResult(ran=False, outcome="no_memory")
    person = _words(call.values.get("person"))
    target = chat.find_target(store, person)
    if isinstance(target, chat.Asked):
        return RouteResult(ran=False, ask=target.line, options=list(target.options), outcome=target.outcome)
    route = chat.pick_route(target, _app_key(call.values.get("app")) or None)
    if isinstance(route, chat.Asked):
        return RouteResult(ran=False, outcome=route.outcome)
    url = chat_web_address(chat.app_data(route.app), route.urls)
    if not url:
        return RouteResult(ran=False, outcome="no_web_address")
    host = url_host(url) or ""
    failed, name, key, source, seen = _open_in_browser(call, url, hosts=[host] if host else [],
                                                       policy=_policy(call, default="site"))
    if failed is not None:
        return failed
    words = {"name": target.name, "browser": _browser_title(key) or name or "the browser"}
    return _opened_result(LINE_CHAT_WEB.format(**words), key, source, seen, {"url": url, "app": route.app},
                          already_line=LINE_CHAT_WEB_ALREADY.format(**words))


def probe_chat_page_shown(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """The web rung's done-check and re-check: the page in front is the chat's own address the route opened."""
    want = str(_ram(ctx).get("url") or "")
    if not want:
        return None, {"status": "no_route"}
    # x8WebArc: the browser window the route opened it in, never the window in front while that one is known
    return _shown_page(ctx, lambda url: _same_page(url, want))


ROUTES: dict[str, Callable[[RouteCall], RouteResult]] = {
    "site_tab": route_site_tab, "told_link": route_told_link, "open_page": route_open_page,
    "open_video": route_open_video, "chat_web_link": route_chat_web_link, "address_bar": route_address_bar,
}
PROBES: dict[str, Callable[..., tuple[bool | None, dict[str, Any]]]] = {
    "site_tab_shown": probe_site_tab_shown, "site_front": probe_site_front, "browser_front": probe_browser_front,
    "page_shown": probe_page_shown, "page_seen": probe_page_seen, "video_shown": probe_video_shown,
    "video_playing": probe_video_playing, "chat_page_shown": probe_chat_page_shown,
}

__all__ = ["ROUTES", "PROBES", "route_site_tab", "route_told_link", "route_open_page", "route_open_video",
           "route_chat_web_link", "route_address_bar", "frame_field", "browsers_said", "ASK_BROWSER",
           "probe_site_tab_shown", "probe_site_front", "probe_browser_front",
           "probe_page_shown", "probe_page_seen", "probe_video_shown", "probe_video_playing", "probe_chat_page_shown",
           "browser_rule", "choose_browser", "measured_browsers", "last_in_front", "band_browser", "site_hosts",
           "NOT_YUKIS_VISIT_SQL", "YUKIS_VISITS_SQL", "own_rows_query",
           "known_services", "service_of", "page_address", "page_matches", "video_page", "domain_like",
           "chat_web_address", "read_whole", "BROWSER_MIN_HOSTS", "BROWSER_POLICIES", "LINE_TAB_UNREADABLE",
           "LINE_TAB_PARTIAL", "LINE_TAB_NONE", "LINE_TAB_UNKNOWN", "LINE_BROWSER_CLOSED", "LINE_TAB_NO_ID",
           "ASK_TAB_WHICH", "LINE_TAB_UP", "site_browser", "SITE_SHARE", "SITE_MIN_VISITS"]
