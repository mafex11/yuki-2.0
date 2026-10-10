"""The Meet tab and YouTube: the drivers of ``meet.open``, ``youtube.home`` and ``youtube.video`` (PLAN.md Phase S-2;
decisions D0, D8, D16, D17, D19; B.2; map-skills S2.1, S2.2).

Their connection files (``connections/zen_meet.toml``, ``connections/youtube.toml``) name these calls; the closed verbs
can't express them, because each needs a fact read at run time: the Meet tab is found by the tab census, and the
browser is the one in use now.

**meet.open** (:func:`route_meet_open`). The Meet tab is already open in Zen; it is found by identity only, never by
its title (D8): memory learned its RuntimeId, its name digests and its pinned place on every Google Meet sighting
(``nudge_state['site_tabs:Google Meet']``, :class:`yuki.memory.callevidence.SiteTabs`), and the census reads Zen's
windows' tabs (every group, at most 0.2 s a window), digests each name and drops it, and ranks the tabs against those
identities (:func:`yuki.perception.tabs.read_census`). Then:

* the strip can't be read (no tab at all): Yuki says so and does nothing (the user's first live check settles whether
  Zen's strip is readable);
* none matches: "There's no Meet tab open in Zen" (or, with a Meet link the user told, the next rung opens it);
* two match equally: Yuki asks which, counted along the strip, and the user's answer comes back as the ``pick`` slot;
* one: that tab is selected by its RuntimeId (``yuki.actions.tabs.tab_action``: a UI Automation pattern, no key, no
  pointer; a candidate that won't select gives way to the next one), then Zen comes forward (the guarded
  ``focus_window``). The user clicks Join. Done when Zen is in front on that very tab (:func:`probe_meet_shown`).

**youtube.home / youtube.video** (:func:`route_youtube_home`, :func:`route_youtube_video`). The browser
(:func:`choose_browser`, map-skills S2.2, redteam D4): the one the user named (it stands in their words); else a browser
in front, or the last one in front at most 30 minutes ago; else their usual (the ``youtube.home:browser`` preference the
resolver filled); else the browser YouTube was last shown in; else the browser they use most; else Windows' default
handler. "A browser" is measured, never a list: a program whose own timeline rows (not Yuki's) of the last 14 days
showed pages of at least 3 hosts, or a YouTube page (a chat app that draws its one site in its own window shows one or
two hosts: the threshold comes from the user's own memory, where the rule then picked the browser actually used
for 94.5% of 379 YouTube sessions; counting any program with a page, 57.5%).
The address (from ``yuki/connect/sites/youtube.toml``) is a launch argument (``launch.open_url``: nothing typed).
youtube.home opens the home page and nothing else: nothing plays until the user picks. youtube.video opens the picked
video's own link and is checked by the page in front showing that video and the browser's media session playing. A
browser the user didn't name is named in the done line, so one word corrects it.

Every route here takes focus: never with a game in front (the engine holds it as "after the round?"), and each focus or
opened link goes through ``gamegate.refuse_input`` (the input layer refuses it too). Logs are content-free: the facts
each route returns are code words and counts (``browser_from``, ``decision``, ``match``, ``tabs``); names, titles and
links stay in RAM.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Sequence

from yuki.skills.engine import RouteCall, RouteResult

#: What Yuki says when it does nothing for meet.open (asked or told plainly; never a "done").
LINE_MEET_UNREADABLE = "I can't read Zen's tabs right now, so I left Zen as it is."
LINE_MEET_NONE = "There's no Meet tab open in Zen."
LINE_MEET_UNKNOWN = "I don't know your Meet tab yet. Bring it up in Zen once and I'll find it next time."
LINE_ZEN_CLOSED = "Zen isn't open, so there's no Meet tab to bring up."
LINE_MEET_NO_ID = "I found your Meet tab, but Zen didn't let me select it, so I left it."
ASK_MEET_WHICH = "I see {n} Meet tabs in Zen. Which one, counting along the tab list: {choices}?"
ORDINALS = ("the first", "the second", "the third", "the fourth")
#: The candidates tried in identity order when one won't select (D8: each checked, the next one after a miss).
MEET_TRIES = 2

#: A browser is a program whose timeline rows carried web pages within this many days, of at least this many hosts (or
#: a page of the site itself): measured on the user's own memory, a chat app that draws one site shows one or two.
BROWSER_DAYS = 14
BROWSER_MIN_HOSTS = 3
#: "The browser you're using": in front now, or last in front at most this long ago (s).
RECENT_S = 30 * 60.0
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


def _app_key(name: Any) -> str:
    from yuki.memory.store import app_key

    return app_key(str(name or "")) or ""


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


def _held_by_game(result: Any) -> bool:
    details = getattr(result, "details", None) or {}
    return isinstance(details, Mapping) and details.get("refused") == "game"


# ---------------------------------------------------------------------------------------------------------------------
# meet.open
# ---------------------------------------------------------------------------------------------------------------------


def _strip_order(c: Any) -> tuple[int, int, int]:
    return (int(getattr(c, "group", 0) or 0), int(getattr(c, "index", 0) or 0), int(getattr(c, "hwnd", 0) or 0))


def _say(line: str, outcome: str, **facts: Any) -> RouteResult:
    """Nothing done, said plainly (the engine's settled "asked" path: never a done, never counted against the rung)."""
    return RouteResult(ran=False, ask=line, outcome=outcome, facts=facts)


def route_meet_open(call: RouteCall) -> RouteResult:
    """meet.open's rung A: the tab census, the Meet tab selected by identity, Zen brought forward (see the module
    docstring). With no Meet tab and a told link, it passes to the told-link rung; with no readable strip it does
    nothing and says so."""
    a, values = call.args, call.values
    service = str(a.get("service") or "")
    process = _app_key(a.get("process"))
    link = bool(values.get("link"))
    store = _store(call.ctx)
    if store is None:
        return RouteResult(ran=False, outcome="no_memory")
    ident = _identities(store, service)
    if not (ident.get("runtime_ids") or ident.get("names") or ident.get("positions")):
        return RouteResult(ran=False, outcome="no_identity") if link else _say(LINE_MEET_UNKNOWN, "no_identity")
    process = _app_key(ident.get("process")) or process
    windows = _app_windows(process)
    if not windows:
        return RouteResult(ran=False, outcome="not_open") if link else _say(LINE_ZEN_CLOSED, "not_open")
    census = _census(store, service, windows, _log(call.ctx))
    facts = {"windows": len(windows), "tabs": int(census.get("tabs") or 0),
             "candidates": len(census.get("candidates") or []), "decision": str(census.get("decision") or "none")}
    if not census.get("tabs"):
        # the strip can't be read: say so, do nothing (not even the told link: a Meet tab may well be open)
        return _say(LINE_MEET_UNREADABLE, "strip_unreadable", **facts)
    decision = str(census.get("decision") or "none")
    chosen = list(census.get("chosen") or [])
    if decision == "none" or not chosen:
        return RouteResult(ran=False, outcome="no_tab", facts=facts) if link else _say(LINE_MEET_NONE, "no_tab",
                                                                                       **facts)
    if decision == "ask":
        tied = sorted(chosen, key=_strip_order)[:len(ORDINALS)]
        pick = values.get("pick")
        try:
            n = int(pick) if pick is not None else 0
        except (TypeError, ValueError):
            n = 0
        if not 1 <= n <= len(tied):
            choices = " or ".join(ORDINALS[:len(tied)])
            return RouteResult(ran=False, ask=ASK_MEET_WHICH.format(n=len(tied), choices=choices),
                               options=[str(i + 1) for i in range(len(tied))], outcome="ask_which", facts=facts)
        order = [tied[n - 1]]
    else:
        # the best by identity first; the next candidates only if it won't select (D8)
        rest = [c for c in (census.get("candidates") or []) if c is not chosen[0]]
        order = [chosen[0]] + rest
    selected = None
    tried = 0
    for cand in order[:MEET_TRIES]:
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
        return _say(LINE_MEET_NO_ID, "select_failed", **facts)
    hwnd = int(selected.hwnd)
    if _refused("focus", hwnd=hwnd):
        # the tab is selected (a pattern call, no focus), Zen stays where it is
        return RouteResult(ran=True, held="game", outcome="refused", facts=facts, app=process or None)
    focused = _focus(hwnd)
    if _held_by_game(focused):
        return RouteResult(ran=True, held="game", outcome="refused", facts=facts, app=process or None)
    facts["focused"] = bool(getattr(focused, "ok", False))
    return RouteResult(ran=True, outcome="selected", facts=facts, app=process or None,
                       facts_ram={"hwnd": hwnd, "tab_id": str(selected.tab_id)})


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


def probe_meet_shown(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """meet.open's done-check: Zen (``process``) is in front, its window the one the route selected in, and that
    window's selected tab is the one the route selected (by RuntimeId). Not so: False; unreadable: None."""
    ram = getattr(getattr(ctx, "result", None), "facts_ram", None) or {}
    want_hwnd, want_tab = int(ram.get("hwnd") or 0), str(ram.get("tab_id") or "")
    if not want_hwnd or not want_tab:
        return None, {"status": "no_route"}
    hwnd, image = _front()
    if not hwnd or not image:
        return None, {"status": "unread"}
    if image.casefold() != str(args.get("process") or "").strip().casefold() or hwnd != want_hwnd:
        return False, {"status": "other_window"}
    selected = _selected_tabs(hwnd)
    if selected is None:
        return None, {"status": "tabs_unread"}
    if not selected:
        return None, {"status": "no_selection"}
    return (want_tab in selected), {"selected": len(selected)}


# ---------------------------------------------------------------------------------------------------------------------
# The browser
# ---------------------------------------------------------------------------------------------------------------------


def measured_browsers(store: Any, *, now: float | None = None, days: float = BROWSER_DAYS,
                      site_hosts: Sequence[str] = ()) -> list[dict[str, Any]]:
    """The browsers among the programs in the user's own timeline rows (never Yuki's) of the last ``days``, most used
    first: ``{"key", "name", "seconds", "last", "hosts", "site_last"}`` (app key, display name as Windows shows it, time
    on pages, its last row's end, distinct hosts, when it last showed a page of ``site_hosts``). A browser is measured,
    never listed: a program that showed pages of at least :data:`BROWSER_MIN_HOSTS` hosts, or a page of the site itself
    (an app that draws one site in its own window, such as a chat app, shows one or two). Never raises (``[]``)."""
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
        if not key or not (int(n_hosts or 0) >= BROWSER_MIN_HOSTS or site_last is not None):
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


def last_in_front(store: Any, keys: Sequence[str], *, now: float | None = None, within_s: float = RECENT_S
                  ) -> str | None:
    """Of ``keys`` (browsers), the one in front most recently, if its row ended at most ``within_s`` ago."""
    now = time.time() if now is None else float(now)
    if not keys:
        return None
    marks = ",".join("?" * len(keys))
    try:
        rows = store._query(
            f"SELECT process FROM timeline WHERE process IN ({marks}) AND state = 'present' AND by_yuki = 0 "
            "AND ended_at >= ? AND started_at <= ? ORDER BY ended_at DESC LIMIT 1",
            (*keys, now - within_s, now))
    except Exception:  # noqa: BLE001
        return None
    return _app_key(rows[0][0]) if rows else None


def browser_rule(*, named: str | None, said: bool, front: str | None, recent: str | None, pref: str | None,
                 measured: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    """``(browser app key, where it came from)`` by the rule (map-skills S2.2, redteam D4): ``words`` (named and in
    their words) > ``front`` (a measured browser in front) > ``recent`` (the last browser in front, <= 30 min ago) >
    ``pref`` (their usual, the resolver's fill) > ``site`` (the browser the site was last shown in) > ``most`` (the
    browser they use most) > ``default`` (``""``: Windows' handler). Pure: the replay on a database copy runs it too
    (``site`` before ``most``: in the replay on the copy it lifted the share from 93.1% to 95.0% of 379 sessions)."""
    keys = [str(m.get("key") or "") for m in measured if m.get("key")]
    if named and said:
        return _app_key(named), "words"
    if front and front in keys:
        return front, "front"
    if recent and recent in keys:
        return recent, "recent"
    if pref:
        return _app_key(pref), "pref"
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


def choose_browser(call: RouteCall, *, site_name: str = "youtube") -> tuple[str, str, str]:
    """``(name to launch it by, app key, where it came from)`` for a YouTube route (see :func:`browser_rule`)."""
    named = call.values.get("browser")
    named = str(named) if named else None
    said = False
    if named:
        from yuki.skills.uia_route import said as is_said

        said = is_said(named, [str(w) for w in call.ctx.words if w])
    store = _store(call.ctx)
    measured = measured_browsers(store, site_hosts=site_hosts(site_name)) if store is not None else []
    keys = [m["key"] for m in measured]
    _hwnd, image = _front()
    front = _app_key(image) or None
    recent = last_in_front(store, keys) if store is not None and not (named and said) else None
    key, source = browser_rule(named=named, said=said, front=front, recent=recent,
                               pref=named if not said else None, measured=measured)
    name = next((m["name"] for m in measured if m["key"] == key), "") or key
    return name, key, source


def _title(name: str) -> str:
    return name if not name.islower() else name.title()


def _browser_line(base: str, name: str, source: str) -> str:
    """The done line: the browser named when the user didn't name it (one word corrects it)."""
    if source in ("words", "default") or not name:
        return base
    return f"{base.rstrip('.')} in {_title(name)}."


def _open_in_browser(call: RouteCall, url: str) -> tuple[RouteResult | None, str, str, str]:
    """Open ``url`` in the chosen browser: ``(a result when it didn't open, name, key, source)``."""
    name, key, source = choose_browser(call)
    if _refused("open_url"):
        return RouteResult(ran=False, held="game", outcome="refused"), name, key, source
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


def _youtube_url(kind: str, **values: str) -> str | None:
    from yuki.connect.templates import fill, site

    if kind == "home":
        return str((site("youtube").get("site") or {}).get("home") or "") or None
    return fill("youtube", kind, **values)


def route_youtube_home(call: RouteCall) -> RouteResult:
    """youtube.home's rung A: YouTube's home page (the site file's address) in the chosen browser. Nothing else is
    opened, so nothing plays until the user picks."""
    url = _youtube_url("home")
    if not url:
        return RouteResult(ran=False, outcome="no_template")
    failed, name, key, source = _open_in_browser(call, url)
    if failed is not None:
        return failed
    return RouteResult(ran=True, outcome="opened", app=key or None, say=_browser_line("YouTube's up.", name, source),
                       facts={"browser_from": source})


def video_link(ref: str) -> tuple[str, str] | None:
    """``(the page address, the video id)`` of a picked video's link: a YouTube watch or shorts address as it stands,
    or a bare id put into the watch template. Fixed machine formats (``yuki/connect/sites/youtube.toml``)."""
    from yuki.connect.templates import page_of

    ref = str(ref or "").strip()
    if not ref:
        return None
    if "://" in ref:
        m = page_of(ref)
        if m is None or m.site != "youtube" or m.kind not in ("watch", "shorts") or not m.entity:
            return None
        return ref, str(m.entity)
    if any(ch in ref for ch in "/?&=# "):
        return None
    url = _youtube_url("watch", id=ref)
    return (url, ref) if url else None


def route_youtube_video(call: RouteCall) -> RouteResult:
    """youtube.video's rung A: the picked video's own page in the chosen browser (its link from the offered options)."""
    video = call.values.get("video")
    ref = video.get("ref") if isinstance(video, Mapping) else None
    got = video_link(str(ref or ""))
    if got is None:
        return RouteResult(ran=False, outcome="no_link")
    url, vid = got
    failed, name, key, source = _open_in_browser(call, url)
    if failed is not None:
        return failed
    return RouteResult(ran=True, outcome="opened", app=key or None,
                       say=_browser_line("Playing it on YouTube.", name, source), facts={"browser_from": source},
                       facts_ram={"video_id": vid, "browser": key})


# ---------------------------------------------------------------------------------------------------------------------
# Done-checks of the YouTube routes
# ---------------------------------------------------------------------------------------------------------------------


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


def probe_video_shown(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """youtube.video's done-check: the page in front is a YouTube video page, the very video the route opened when it
    knows it (the finish gate after a hand-off doesn't: any video page there)."""
    from yuki.connect.templates import page_of

    ram = getattr(getattr(ctx, "result", None), "facts_ram", None) or {}
    want = str(ram.get("video_id") or "")
    hwnd, _image = _front()
    if not hwnd:
        return None, {"status": "unread"}
    urls = _front_urls(hwnd)
    if urls is None:
        return None, {"status": "timeout"}
    if not urls:
        return None, {"status": "no_document"}
    for url in urls:
        m = page_of(url)
        if m is not None and m.site == str(args.get("site") or "youtube") and m.kind in ("watch", "shorts"):
            if not want or str(m.entity) == want:
                return True, {"status": "page"}
            return False, {"status": "other_video"}
    return False, {"status": "other_page"}


def probe_video_playing(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """youtube.video's re-check: the browser's own media session says playing (a second source)."""
    from yuki.skills.donecheck import probe_media_session

    result = getattr(ctx, "result", None)
    ram = getattr(result, "facts_ram", None) or {}
    browser = str(ram.get("browser") or getattr(result, "app", "") or "")
    if not browser:
        return None, {"status": "no_browser"}
    return probe_media_session(ctx, {"app": browser, "status": "playing"})


def probe_page_seen(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """youtube.home's re-check from a second source: memory's own read of the window in front (the watcher's Now
    board, read after the run began) shows a page of that site and kind. A stale or blocked read: None."""
    from yuki.connect.templates import page_of

    board = _board_now()
    if not board:
        return None, {"status": "no_board"}
    front = next((w for w in board.get("windows") or [] if w.get("foreground")), None)
    if front is None:
        return None, {"status": "no_front"}
    if front.get("blocked") or not front.get("read_at") or float(front["read_at"]) < float(ctx.since):
        return None, {"status": "stale"}
    m = page_of(front.get("url"))
    if m is None:
        return False, {"status": "other_page"}
    ok = m.site == str(args.get("site") or "") and (not args.get("kind") or m.kind == str(args.get("kind")))
    return ok, {"status": "page" if ok else "other_page"}


def _board_now(wait_s: float = 0.15) -> dict[str, Any] | None:
    """Memory's Now board (the watcher's latest reads), or ``None`` (no service, no answer in time)."""
    try:
        from yuki.memory.now import shared_client

        client = shared_client()
        return client.now(wait_s=wait_s) if client is not None else None
    except Exception:  # noqa: BLE001
        return None


ROUTES: dict[str, Callable[[RouteCall], RouteResult]] = {
    "meet_open": route_meet_open, "youtube_home": route_youtube_home, "youtube_video": route_youtube_video,
}
PROBES: dict[str, Callable[..., tuple[bool | None, dict[str, Any]]]] = {
    "meet_shown": probe_meet_shown, "video_shown": probe_video_shown, "video_playing": probe_video_playing,
    "page_seen": probe_page_seen,
}

__all__ = ["ROUTES", "PROBES", "route_meet_open", "route_youtube_home", "route_youtube_video", "probe_meet_shown",
           "probe_video_shown", "probe_video_playing", "probe_page_seen", "browser_rule", "choose_browser",
           "measured_browsers", "last_in_front", "site_hosts", "BROWSER_MIN_HOSTS", "video_link",
           "LINE_MEET_UNREADABLE", "LINE_MEET_NONE",
           "LINE_MEET_UNKNOWN", "LINE_ZEN_CLOSED", "LINE_MEET_NO_ID", "ASK_MEET_WHICH"]
