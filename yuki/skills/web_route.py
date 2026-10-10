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
opens its own link and is checked by the page in front showing that video and the browser's media session playing. A
browser the user didn't name is named in the done line, so one word corrects it.

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

import time
from typing import Any, Callable, Mapping, Sequence

from yuki.skills.connection import domain_like
from yuki.skills.engine import RouteCall, RouteResult

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
#: How much a done-check's front-window read may take (one UIA job on its own thread).
FRONT_READ_S = 0.5


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


def _refused(action: str, hwnd: int | None = None) -> bool:
    """D17's backstop asked right here, before a focus or an opened link (the input layer asks again)."""
    from yuki.memory import gamegate

    return gamegate.refuse_input(action, hwnd=hwnd) is not None


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


def _open_url(url: str, app: str | None) -> Any:
    from yuki.actions import launch

    return launch.open_url(url, app=app or None)


def _handler_key(url: str) -> str:
    """The app key of the program Windows opens ``url`` with (its registered handler, read from the registry: no
    window read, nothing launched); ``""`` when unknown. Review r2 L3 (D18): a page opened with Windows' default
    handler (no browser measured yet: a fresh install) still names its browser on the run's row before it acts."""
    try:
        from yuki.actions.launch import _default_handler

        return _app_key(_default_handler(url))
    except Exception:  # noqa: BLE001 - an unreadable association: nothing to claim
        return ""


def _held_by_game(result: Any) -> bool:
    details = getattr(result, "details", None) or {}
    return isinstance(details, Mapping) and details.get("refused") == "game"


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
        if _refused("tab_select", hwnd=hwnd):
            return RouteResult(ran=False, held="game", outcome="refused", facts=facts)
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
    if _refused("focus", hwnd=hwnd):
        # the tab is selected (a pattern call, no focus), the browser stays where it is
        return RouteResult(ran=True, held="game", outcome="refused", facts=facts, app=process or None)
    focused = _focus(hwnd)
    if _held_by_game(focused):
        return RouteResult(ran=True, held="game", outcome="refused", facts=facts, app=process or None)
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
    if key:
        name, source = _browser_title(key), "slot" if call.values.get("browser") else "identity"
    else:
        name, key, source = choose_browser(call)
    if _refused("open_url"):
        return RouteResult(ran=False, held="game", outcome="refused")
    if source == "default" and not key:
        key = _handler_key(link)
    _claim_app(call, key)
    result = _open_url(link, name if source != "default" else None)
    if _held_by_game(result):
        return RouteResult(ran=False, held="game", outcome="refused")
    if not getattr(result, "ok", False):
        details = getattr(result, "details", None) or {}
        if not (isinstance(details, Mapping) and (details.get("launched") or details.get("hwnd"))):
            return RouteResult(ran=False, outcome="not_opened", facts={"browser_from": source})
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
    are the definition's two knobs, for the replay that chose them (``scripts/browser_rule_replay.py``). Never raises
    (``[]``)."""
    now = time.time() if now is None else float(now)
    hosts = [str(h).casefold() for h in site_hosts if h]
    site_sql = (f"max(CASE WHEN host IN ({','.join('?' * len(hosts))}) THEN ended_at END)" if hosts else "NULL")
    try:
        rows = store._query(
            f"SELECT process, sum(active_s + passive_s) AS s, max(ended_at) AS last, count(DISTINCT host) AS n, "
            f"{site_sql} AS site_last FROM timeline WHERE host IS NOT NULL AND started_at >= ? AND started_at <= ? "
            "AND by_yuki = 0 AND state = 'present' AND process IS NOT NULL GROUP BY process ORDER BY s DESC",
            (*hosts, now - days * 86400.0, now))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for process, seconds, last, n_hosts, site_last in rows:
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
                    "hosts": int(n_hosts or 0), "site_last": float(site_last) if site_last is not None else None})
    return out


def last_in_front(store: Any, keys: Sequence[str], *, now: float | None = None, within_s: float = RECENT_S,
                  min_s: float = 0.0, active_only: bool = False, page_only: bool = False) -> str | None:
    """Of ``keys`` (browsers), the one in front most recently, if its row ended at most ``within_s`` ago. The knobs are
    for the replay's refinements (review r2 M3; ``scripts/browser_rule_replay.py``): ``min_s`` skips glance rows (the
    user's time on the row, active plus passive, under it), ``active_only`` counts only rows with input,
    ``page_only`` only rows that showed a web page."""
    now = time.time() if now is None else float(now)
    if not keys:
        return None
    marks = ",".join("?" * len(keys))
    extra = "".join(part for part, on in ((" AND (active_s + passive_s) >= ?", min_s > 0),
                                          (" AND active_s > 0", active_only),
                                          (" AND host IS NOT NULL", page_only)) if on)
    try:
        rows = store._query(
            f"SELECT process FROM timeline WHERE process IN ({marks}) AND state = 'present' AND by_yuki = 0 "
            f"AND ended_at >= ? AND started_at <= ?{extra} ORDER BY ended_at DESC LIMIT 1",
            (*keys, now - within_s, now, *((float(min_s),) if min_s > 0 else ())))
    except Exception:  # noqa: BLE001
        return None
    return _app_key(rows[0][0]) if rows else None


def band_browser(store: Any, keys: Sequence[str], *, now: float | None = None, hours: int = BAND_H,
                 days: float = BAND_DAYS) -> str | None:
    """Of ``keys`` (browsers), the one the user had in front most in this hour band (now's local hour ``hours`` either
    side, on weekdays or on weekends like now) over the last ``days``; ``None`` when none was. Never raises."""
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
        rows = store._query(
            f"SELECT process, started_at, active_s + passive_s FROM timeline WHERE process IN ({marks}) "
            "AND state = 'present' AND by_yuki = 0 AND started_at >= ? AND started_at <= ?",
            (*keys, now - float(days) * 86400.0, now))
    except Exception:  # noqa: BLE001
        return None
    totals: dict[str, float] = {}
    for process, started, seconds in rows:
        at = datetime.fromtimestamp(float(started or 0.0))
        if at.hour in band and (at.weekday() >= 5) == weekend:
            key = _app_key(process)
            totals[key] = totals.get(key, 0.0) + float(seconds or 0.0)
    return max(totals, key=totals.__getitem__) if totals else None


def browser_rule(*, named: str | None, said: bool, front: str | None, recent: str | None, pref: str | None,
                 measured: Sequence[Mapping[str, Any]], site_first: bool = False, policy: str = "most",
                 band: str | None = None) -> tuple[str, str]:
    """``(browser app key, where it came from)`` (design §5): ``words`` (named and in their words) > ``pref`` (the
    slot's value the resolver filled: their usual) > the fill ``policy`` (:data:`BROWSER_POLICIES`; a learned skill's,
    else ``most``): ``front`` / ``recent`` for ``front_or_recent``, ``band``, ``site`` (the browser the site was last
    shown in) > ``most`` (the browser they use most) > ``default`` (``""``: Windows' handler). Pure: a replay on a
    database copy runs it too (``scripts/browser_rule_replay.py``; ``site_first`` is that script's old knob, kept as
    an alias of the ``site`` policy)."""
    keys = [str(m.get("key") or "") for m in measured if m.get("key")]
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
        if band and band in keys:
            return band, "band"
    elif policy == "site":
        shown = [m for m in measured if m.get("key") and m.get("site_last") is not None]
        if shown:
            return str(max(shown, key=lambda m: float(m["site_last"]))["key"]), "site"
    if keys:
        return keys[0], "most"
    return "", "default"


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


def choose_browser(call: RouteCall, *, hosts: Sequence[str] = (), policy: str | None = None) -> tuple[str, str, str]:
    """``(name to launch it by, app key, where it came from)`` for a browser route (see :func:`browser_rule`).
    ``hosts``: the site's own hosts (a program that showed it counts as a browser; the ``site`` policy)."""
    named = call.values.get("browser")
    named = str(named) if named else None
    said = False
    if named:
        from yuki.skills.uia_route import said as is_said

        said = is_said(named, [str(w) for w in call.ctx.words if w])
    store = _store(call.ctx)
    measured = measured_browsers(store, site_hosts=list(hosts)) if store is not None else []
    keys = [m["key"] for m in measured]
    policy = policy or _policy(call)
    front = recent = band = None
    if store is not None and not named and policy == "front_or_recent":
        _hwnd, image = _front()
        front = _app_key(image) or None
        recent = last_in_front(store, keys)
    if store is not None and not named and policy == "band":
        band = band_browser(store, keys)
    key, source = browser_rule(named=named, said=said, front=front, recent=recent,
                               pref=named if not said else None, measured=measured, policy=policy, band=band)
    name = next((m["name"] for m in measured if m["key"] == key), "") or _browser_title(key) or key
    return name, key, source


def _browser_line(base: str, name: str, source: str) -> str:
    """The done line: the browser named when the user didn't name it (one word corrects it)."""
    if source in ("words", "default") or not name:
        return base
    return f"{base.rstrip('.')} in {_browser_title(name) if name.islower() else name}."


def _open_in_browser(call: RouteCall, url: str, *, hosts: Sequence[str] = (),
                     policy: str | None = None) -> tuple[RouteResult | None, str, str, str]:
    """Open ``url`` in the chosen browser: ``(a result when it didn't open, name, key, source)``."""
    name, key, source = choose_browser(call, hosts=hosts, policy=policy)
    if _refused("open_url"):
        return RouteResult(ran=False, held="game", outcome="refused"), name, key, source
    if source == "default" and not key:
        key = _handler_key(url)
    _claim_app(call, key)
    result = _open_url(url, name if source != "default" else None)
    if _held_by_game(result):
        return RouteResult(ran=False, held="game", outcome="refused"), name, key, source
    if not getattr(result, "ok", False):
        details = getattr(result, "details", None) or {}
        launched = isinstance(details, Mapping) and bool(details.get("launched") or details.get("hwnd"))
        # a browser that started but didn't show the page may still: acted, and the done-check decides
        return (None if launched else RouteResult(ran=False, outcome="not_opened",
                                                  facts={"browser_from": source})), name, key, source
    return None, name, key, source


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
    data = site_data(name) if name.replace("_", "").isalnum() else {}
    if not data and domain_like(name):
        m = page_of(f"https://{name}/")
        if m is not None:
            name, data = m.site, site_data(m.site)
    if not data:
        if domain_like(name) and not page:
            return f"https://{name}/", {"host": name}
        return None, {"why": "no_address"}
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
    said = _words(call.values.get("site"))
    url, expect = page_address(said, call.values.get("page"), call.values.get("item"))
    if not url:
        return RouteResult(ran=False, outcome=str(expect.get("why") or "no_address"))
    failed, name, key, source = _open_in_browser(call, url, hosts=_hosts_of(expect))
    if failed is not None:
        return failed
    line = _browser_line(LINE_PAGE_UP.format(site=_site_title(expect, said)), name, source)
    return RouteResult(ran=True, outcome="opened", app=key or None, say=line, facts={"browser_from": source},
                       facts_ram={"expect": expect, "browser": key})


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
    video = call.values.get("video")
    ref = video.get("ref") if isinstance(video, Mapping) else None
    got = video_page(ref)
    if got is None:
        return RouteResult(ran=False, outcome="no_link")
    url, expect = got
    failed, name, key, source = _open_in_browser(call, url, hosts=_hosts_of(expect))
    if failed is not None:
        return failed
    return RouteResult(ran=True, outcome="opened", app=key or None, say=_browser_line(LINE_VIDEO_UP, name, source),
                       facts={"browser_from": source}, facts_ram={"expect": expect, "browser": key})


def _front_urls(hwnd: int) -> list[str] | None:
    """The addresses of the Documents of the window in front (a browser's page), read in one UIA job."""
    from yuki.connect.spotify_desktop import UiaTree, run_uia

    def job(automation: Any, shared: dict) -> None:
        docs = UiaTree(automation, int(hwnd)).find("Document")
        shared["urls"] = [d.value for d in docs if d.value and "://" in d.value][:4]

    shared, late = run_uia(job, FRONT_READ_S, name="yuki-skill-page")
    if late:
        return None
    return list(shared.get("urls") or [])


def _front_page(expect: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    hwnd, _image = _front()
    if not hwnd:
        return None, {"status": "unread"}
    urls = _front_urls(hwnd)
    if urls is None:
        return None, {"status": "timeout"}
    if not urls:
        return None, {"status": "no_document"}
    if any(page_matches(url, expect) for url in urls):
        return True, {"status": "page"}
    return False, {"status": "other_page"}


def probe_page_shown(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """web.open_page's done-check: the page in front is the one the route opened (by template value, or by host for a
    host-scope page). After a hand-off (no route result) it can't say: None."""
    expect = _ram(ctx).get("expect")
    if not isinstance(expect, Mapping) or not expect:
        return None, {"status": "no_route"}
    return _front_page(expect)


def probe_video_shown(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """web.open_video's done-check: the page in front is the very video page the route opened. After a hand-off (no
    route result) it can't say which page counts: None (never a false done)."""
    expect = _ram(ctx).get("expect")
    if not isinstance(expect, Mapping) or not expect:
        return None, {"status": "no_route"}
    return _front_page(expect)


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
    """web.open_page's re-check from a second source: memory's own read of the window in front (the watcher's Now
    board, read after the run began) shows the page the route opened (``site`` / ``kind`` in the check: any page of
    them). A stale or blocked read: None."""
    expect = _ram(ctx).get("expect")
    if not isinstance(expect, Mapping) or not expect:
        expect = {k: args[k] for k in ("site", "kind", "host") if args.get(k)}
    if not expect:
        return None, {"status": "no_route"}
    board = _board_now()
    if not board:
        return None, {"status": "no_board"}
    front = next((w for w in board.get("windows") or [] if w.get("foreground")), None)
    if front is None:
        return None, {"status": "no_front"}
    if front.get("blocked") or not front.get("read_at") or float(front["read_at"]) < float(ctx.since):
        return None, {"status": "stale"}
    ok = page_matches(front.get("url"), expect)
    return ok, {"status": "page" if ok else "other_page"}


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
    failed, name, key, source = _open_in_browser(call, url, hosts=[host] if host else [],
                                                 policy=_policy(call, default="site"))
    if failed is not None:
        return failed
    words = {"name": target.name, "browser": _browser_title(key) or name or "the browser"}
    return RouteResult(ran=True, outcome="opened", app=key or None, say=LINE_CHAT_WEB.format(**words),
                       facts={"browser_from": source}, facts_ram={"url": url, "browser": key, "app": route.app})


def probe_chat_page_shown(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """The web rung's done-check and re-check: the page in front is the chat's own address the route opened."""
    want = str(_ram(ctx).get("url") or "")
    if not want:
        return None, {"status": "no_route"}
    hwnd, _image = _front()
    if not hwnd:
        return None, {"status": "unread"}
    urls = _front_urls(hwnd)
    if urls is None:
        return None, {"status": "timeout"}
    if not urls:
        return None, {"status": "no_document"}
    ok = any(_same_page(u, want) for u in urls)
    return ok, {"status": "page" if ok else "other_page"}


ROUTES: dict[str, Callable[[RouteCall], RouteResult]] = {
    "site_tab": route_site_tab, "told_link": route_told_link, "open_page": route_open_page,
    "open_video": route_open_video, "chat_web_link": route_chat_web_link,
}
PROBES: dict[str, Callable[..., tuple[bool | None, dict[str, Any]]]] = {
    "site_tab_shown": probe_site_tab_shown, "site_front": probe_site_front, "browser_front": probe_browser_front,
    "page_shown": probe_page_shown, "page_seen": probe_page_seen, "video_shown": probe_video_shown,
    "video_playing": probe_video_playing, "chat_page_shown": probe_chat_page_shown,
}

__all__ = ["ROUTES", "PROBES", "route_site_tab", "route_told_link", "route_open_page", "route_open_video",
           "route_chat_web_link", "probe_site_tab_shown", "probe_site_front", "probe_browser_front",
           "probe_page_shown", "probe_page_seen", "probe_video_shown", "probe_video_playing", "probe_chat_page_shown",
           "browser_rule", "choose_browser", "measured_browsers", "last_in_front", "band_browser", "site_hosts",
           "known_services", "service_of", "page_address", "page_matches", "video_page", "domain_like",
           "chat_web_address", "read_whole", "BROWSER_MIN_HOSTS", "BROWSER_POLICIES", "LINE_TAB_UNREADABLE",
           "LINE_TAB_PARTIAL", "LINE_TAB_NONE", "LINE_TAB_UNKNOWN", "LINE_BROWSER_CLOSED", "LINE_TAB_NO_ID",
           "ASK_TAB_WHICH", "LINE_TAB_UP"]
