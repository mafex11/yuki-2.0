"""Links seen: the pages the user had in front and the links people shared with them, queryable.

"Send him that link I was looking at" had nothing to stand on: memory kept the page as time in the
timeline and as a thread's address, but nothing could ask "which pages, when, how long, from
whom". Now ``links_seen`` (migration 23) holds one row per page viewed (the timeline's page
stretches and the capture threads' addresses: time in front, visits, first and last seen) and per
link shared in a conversation or email (a web address in a message, with who sent it and where).
Each row carries the thread of work its window was part of, and a local embedding of its title,
site and path, so the agent's ``links`` tool and the whole-memory ranking (:mod:`yuki.memory.related`)
find "the Stripe docs page" or "the link Varun sent" by meaning and by time.

Plumbing only (no model, nothing judged): the privacy gates already decided what the timeline and
the captures keep (blocked sites, private windows and sign-in pages never reach them); a web address
in a message is found by its fixed machine format (RFC 3986, parsed with :func:`urllib.parse.urlsplit`),
never by words. The sync is incremental (checkpoints in ``source_checkpoints``), runs in the service
about once a minute (:mod:`yuki.memory.screenrecall`'s worker) and costs a few indexed queries.

Public API::

    sync(store, *, embedder=None, full=False, now=None) -> dict         # incremental; full: from the start
    urls_in(text) -> list[str]                                           # web addresses in free text
    message_links(text) -> list[(url, sender)]                           # our own capture format for messages
    embed_call_digests(store, embedder, limit=64) -> int                 # vectors for call digests (whole-memory ranking)
    link_text(link) -> str                                               # what a link is embedded as
    search(store, embed, query=None, *, since=None, until=None, how=None, limit=10) -> list[LinkSeen]
    describe(link, seconds=None, now=None, own_last=None, *, own_known=False) -> str
    own_visits(store, page_keys, *, since=None, now=None) -> {page_key: end}   # the user's own newest stretch (S19)
    own_days(store, hosts, *, since, now=None) -> {host: days}                 # days they had its pages in front
    own_captures(store, thread_ids, *, since=None, now=None) -> {thread_id: at}  # a keyless page's own last capture
    own_lasts(store, links, *, since=None, now=None, measured=None) -> {link_id: end}  # each viewed link's own last
    own_last(store, link, *, since=None, now=None) -> float | None              # one link's

A page's ``last_seen`` moves with every sighting, a page Yuki opened (or a live test) included; how far the user
themselves got is read from their own timeline rows (:func:`own_visits`, mem22 S19: "where I left off"; integ22
MEM-2: under the page's address in every browser seen on its site, not only the browser of its newest sighting), and
for a page with no timeline key from their own captures of it (:func:`own_captures`). A keyed page measured with no
visit of theirs says so (integ22 MEM-1: ``own_known``).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable, Sequence
from urllib.parse import unquote, urlsplit

import numpy as np

#: Checkpoints of the incremental sync.
TIMELINE_CHECKPOINT = "links.timeline"
TIMELINE_ID_CHECKPOINT = "links.timeline_id"
THREADS_CHECKPOINT = "links.threads"
MESSAGES_CHECKPOINT = "links.messages"
#: Most rows one sync pass reads from each source (the rest waits for the next pass).
PASS_ROWS = 2_000
#: Most links embedded in one pass.
EMBED_BATCH = 64
#: Characters around a shared link kept as where it was said.
WHERE_CHARS = 160
#: S19 (mem22): seconds a capture may trail the end of the timeline stretch it was taken in (the capture is written
#: after the stretch's last tick; plumbing).
OWN_CAPTURE_SLACK_S = 5.0
#: Schemes a link can have (the fixed format; nothing else is a link).
SCHEMES = ("http", "https")
#: Characters that end a web address in running text (RFC 3986 does not allow them unescaped; plumbing).
_URL_END = " \t\r\n<>\"'`|{}^\\"
_TRAILING = ".,;:!?)]}'\"»”’…"


def urls_in(text: str | None) -> list[str]:
    """The http(s) web addresses in ``text``, in order, once each (a fixed machine format: scheme, ``://``,
    host; trailing sentence punctuation is not part of it)."""
    out: list[str] = []
    s = text or ""
    i = 0
    while True:
        j = s.find("://", i)
        if j < 0:
            break
        start = j
        while start > 0 and s[start - 1].isalpha():
            start -= 1
        end = j + 3
        while end < len(s) and s[end] not in _URL_END:
            end += 1
        i = end
        candidate = s[start:end].rstrip(_TRAILING)
        scheme = candidate.split("://", 1)[0].lower()
        if scheme not in SCHEMES:
            continue
        try:
            parts = urlsplit(candidate)
        except ValueError:
            continue
        if not parts.hostname or "." not in parts.hostname:
            continue
        if candidate not in out:
            out.append(candidate)
    return out


def message_links(delta: str | None) -> list[tuple[str, str | None]]:
    """``(url, sender)`` of the links in a conversation capture. Its lines are our own format
    (``Store.add_conversation``): ``[new|history] <time label or -> <sender or "the user">: <text>``."""
    out: list[tuple[str, str | None]] = []
    for line in (delta or "").splitlines():
        urls = urls_in(line)
        if not urls:
            continue
        sender = None
        if line.startswith("[") and "] " in line:
            rest = line.split("] ", 1)[1]
            head, sep, _text = rest.partition(": ")
            if sep:
                words = head.split(" ", 1)
                sender = words[1].strip() if len(words) > 1 else None
        for url in urls:
            out.append((url, sender))
    return out


def _host(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def link_text(link: Any) -> str:
    """What a link is embedded as: its title, its site and the words of its path (URL-decoded)."""
    try:
        parts = urlsplit(link.url)
        path = unquote(parts.path or "").replace("/", " ").replace("-", " ").replace("_", " ").strip()
        site = (parts.hostname or "").removeprefix("www.")
    except ValueError:
        path, site = "", link.host or ""
    bits = [b for b in (link.title or "", site, path) if b and b.strip()]
    if link.how == "shared" and link.sender:
        bits.append(f"shared by {link.sender}")
    return " - ".join(" ".join(b.split()) for b in bits)[:500]


def _work_of(store: Any, capture_thread_id: int | None, cache: dict[int, int | None]) -> int | None:
    if capture_thread_id is None:
        return None
    if capture_thread_id not in cache:
        try:
            found = store.work_threads_of_capture_threads([capture_thread_id], limit=1)
        except Exception:
            found = []
        cache[capture_thread_id] = found[0] if found else None
    return cache[capture_thread_id]


def sync(store: Any, *, embedder: Any = None, full: bool = False, now: float | None = None,
         embed_rounds: int = 64) -> dict[str, Any]:
    """Bring ``links_seen`` up to date from the timeline, the capture threads and the messages; embed the new
    rows (at most ``embed_rounds`` batches of :data:`EMBED_BATCH`: the service embeds one batch a pass, so a
    first catch-up spreads over minutes). Incremental (checkpoints); ``full``: from the start. Returns counts
    (content-free); never raises."""
    now = time.time() if now is None else float(now)
    t0 = time.perf_counter()
    out: dict[str, Any] = {"viewed": 0, "threads": 0, "shared": 0, "embedded": 0}
    work_cache: dict[int, int | None] = {}
    thread_of_page: dict[str, int] = {}
    try:
        after = 0.0 if full else float(store.source_checkpoint(THREADS_CHECKPOINT) or 0.0)
        rows = store.url_threads_since(after, limit=PASS_ROWS)
        items = []
        for r in rows:
            if not r["url"] or (r["url"].split("://", 1)[0].lower() not in SCHEMES):
                continue
            items.append({"url": r["url"], "how": "viewed", "title": r["title"], "host": r["host"] or _host(r["url"]),
                          "app": r["app"], "at": r["first_seen"], "until": r["last_seen"], "visit": False,
                          # mem22 (A2a, S19): a page only ever captured while Yuki acted is Yuki's (its row was
                          # written as the user's here, so no row ever kept by_yuki: 0 of 1,742 on 19 Oct)
                          "by_yuki": bool(r.get("by_yuki")),
                          "capture_thread_id": r["id"], "work_thread_id": _work_of(store, r["id"], work_cache),
                          # a page only ever read beside the window in front (migration 24): weaker evidence
                          "attention": None if r.get("front", True) else "visible"})
            thread_of_page[r["url"]] = r["id"]
        if items:
            store.upsert_links(items)
            out["threads"] = len(items)
        if rows:
            store.set_source_checkpoint(THREADS_CHECKPOINT, str(max(r["last_seen"] for r in rows)))
    except Exception as exc:
        out["error_threads"] = f"{type(exc).__name__}: {exc}"
    try:
        after = 0.0 if full else float(store.source_checkpoint(TIMELINE_CHECKPOINT) or 0.0)
        seen_id = 0 if full else int(store.source_checkpoint(TIMELINE_ID_CHECKPOINT) or 0)
        rows = store.timeline_pages_since(after, limit=PASS_ROWS)
        items = []
        for r in rows:
            path = r["path"] or "/"
            url = f"https://{r['host']}{path if path.startswith('/') else '/' + path}"
            # a stretch is rewritten while it is open (every 30 s): it is one visit, counted when first read
            items.append({"url": url, "how": "viewed", "title": r["title"], "host": r["host"], "app": r["app"],
                          "at": r["started_at"], "until": r["ended_at"], "page_key": r["page_key"],
                          "by_yuki": r["by_yuki"], "visit": int(r["id"]) > seen_id})
        if items:
            store.upsert_links(items)
            out["viewed"] = len(items)
        if rows:
            store.set_source_checkpoint(TIMELINE_CHECKPOINT, str(max(r["updated_at"] for r in rows)))
            store.set_source_checkpoint(TIMELINE_ID_CHECKPOINT, str(max(seen_id, *(int(r["id"]) for r in rows))))
    except Exception as exc:
        out["error_timeline"] = f"{type(exc).__name__}: {exc}"
    try:
        after = 0 if full else int(store.source_checkpoint(MESSAGES_CHECKPOINT) or 0)
        caps = store.message_captures_after(after, limit=PASS_ROWS)
        items = []
        for c in caps:
            where = " ".join(p for p in (c["app"] or "", c["scope"] or c["title"] or "") if p)[:WHERE_CHARS]
            for url, sender in message_links(c["text"]):
                items.append({"url": url, "how": "shared", "host": _host(url), "app": c["app"], "sender": sender,
                              "where": where, "at": c["at"], "until": c["at"], "by_yuki": c["by_yuki"],
                              "capture_thread_id": c["thread_id"],
                              "work_thread_id": _work_of(store, c["thread_id"], work_cache)})
        if items:
            store.upsert_links(items)
            out["shared"] = len(items)
        if caps:
            store.set_source_checkpoint(MESSAGES_CHECKPOINT, str(max(c["id"] for c in caps)))
    except Exception as exc:
        out["error_messages"] = f"{type(exc).__name__}: {exc}"
    if embedder is not None:
        out["embedded"] = embed_links(store, embedder, rounds=embed_rounds)
    out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


def embed_links(store: Any, embedder: Any, limit: int = EMBED_BATCH, rounds: int = 64) -> int:
    """Vectors for links stored without one, at most ``rounds`` batches (never raises)."""
    total = 0
    for _ in range(max(1, int(rounds))):
        try:
            missing = store.links_without_vectors(limit)
            if not missing:
                break
            vecs = embedder.embed([link_text(link) for link in missing])
            total += store.add_link_vectors(list(zip([link.id for link in missing], vecs)),
                                            getattr(embedder, "model_name", None) or "")
        except Exception:
            break
        if len(missing) < limit:
            break
    return total


def digest_text(d: dict[str, Any]) -> str:
    """What a call digest is embedded as: its app, where and with whom (as the screen around it showed), its gist
    and its blocks' topics."""
    topics = [str(b.get("topic") or "") for b in d.get("blocks") or [] if b.get("topic")]
    ctx = d.get("context") or {}
    people = [str(p.get("name") or "") for p in ctx.get("people") or [] if p.get("name")]
    around = (f" {ctx['where']}." if ctx.get("where") else "") + (f" With {', '.join(people)}." if people else "")
    return " ".join(f"{d.get('app') or ''} call.{around} {d.get('gist') or ''} Topics: {'; '.join(topics)}"
                    .split())[:2000]


def embed_call_digests(store: Any, embedder: Any, limit: int = 64) -> int:
    """Vectors for call digests stored without one (never raises)."""
    try:
        missing = store.call_digests_without_vectors(limit)
        if not missing:
            return 0
        vecs = embedder.embed([digest_text(d) for d in missing])
        return store.add_call_digest_vectors(list(zip([d["call_id"] for d in missing], vecs)),
                                             getattr(embedder, "model_name", None) or "")
    except Exception:
        return 0


def search(store: Any, embed: Callable[[list[str]], Any] | None, query: str | None = None, *, since: float | None = None,
           until: float | None = None, how: str | None = None, limit: int = 10, pool: int = 400) -> list[Any]:
    """Links seen in ``[since, until)``, best first: by meaning when ``query`` is given (the local embedder
    over their titles, sites and paths; recency breaks near-ties), else most recently seen first."""
    links = store.links(since=since, until=until, how=how, limit=pool)
    if not query or not (query or "").strip() or embed is None or not links:
        return links[: max(0, int(limit))]
    try:
        q = np.asarray(embed([query])[0], dtype=np.float32)
        q = q / (float(np.linalg.norm(q)) or 1.0)
        vecs = store.link_vectors([link.id for link in links])
    except Exception:
        return links[: max(0, int(limit))]
    now = time.time()
    scored = []
    for link in links:
        v = vecs.get(link.id)
        if v is None or v.shape != q.shape:
            continue
        cos = float(v @ q / (float(np.linalg.norm(v)) or 1.0))
        recency = 0.5 ** (max(now - link.last_seen, 0.0) / 86400.0 / 3.0)
        link.score = round(cos + 0.05 * recency, 4)
        scored.append(link)
    scored.sort(key=lambda link: -(link.score or 0.0))
    return scored[: max(0, int(limit))]


def _when(at: float, now: float) -> str:
    stamp = datetime.fromtimestamp(float(at))
    return stamp.strftime("today %H:%M") if stamp.date() == datetime.fromtimestamp(now).date() else stamp.strftime(
        "%a %d %b %H:%M")


def describe(link: Any, seconds: float | None = None, now: float | None = None, own_last: float | None = None, *,
             own_known: bool = False) -> str:
    """One line: ``"Title" - https://... (github.com; viewed in Google Chrome, last Tue 29 Sep 03:40, 4 min in front,
    3 visits)``, or for a shared link who sent it and where. ``own_last`` (S19: :func:`own_last`): when the user's own
    last visit ended. When the page was seen later than that, the line leads with their own visit and says the later
    sightings were never theirs in front (own visits count only their own stretches with them present: a later
    sighting was Yuki acting, a test, the page up while they were away, or the page behind another one they had in
    front), so a cut line still dates it by their visit. ``own_known`` (integ22 MEM-1): the caller measured this
    link's own visits (:func:`own_lasts`' ``measured``), so ``own_last`` None means none of theirs, not unmeasured: a
    keyed page in front with none is marked as never theirs (the related block's wording), never read as their page."""
    now = time.time() if now is None else float(now)
    when = _when(link.last_seen, now)
    title = f"\"{' '.join((link.title or '').split())[:120]}\" - " if link.title else ""
    bits = [link.host or ""]
    if link.how == "shared":
        who = f" by {link.sender}" if link.sender else ""
        bits.append(f"shared{who} in {link.where or link.app or 'a conversation'}, {when}")
    elif getattr(link, "attention", None) == "visible":
        bits.append(f"on screen{' in ' + link.app if link.app else ''} beside the window in front (maybe not read), "
                    f"last {when}")
    else:
        later = own_last is not None and float(link.last_seen) - float(own_last) > 60.0
        bits.append(f"viewed{' in ' + link.app if link.app else ''}, "
                    + (f"their own last visit {_when(own_last, now)}" if later else f"last {when}"))
        if later:   # measured 19 Oct on the copy: mostly behind another page of theirs, else Yuki's, a test's, away
            bits.append(f"seen later ({when}), never in front as theirs since (Yuki acting, a test, away, or behind "
                        "another page)")
        elif own_known and own_last is None and link.page_key and not link.by_yuki:
            # integ22 MEM-1: its key's stretches are the timeline's present ones and none is theirs (a page Yuki opened
            # left up after the act, or a test's); a keyless page stays unmarked (own captures miss some own visits)
            bits.append("never in front as theirs: up only while Yuki acted or a test ran (not their own progress)")
        if seconds:
            minutes = int(round(seconds / 60.0))
            bits.append(f"{minutes} min in front" if minutes else f"{int(seconds)} s in front")
        if link.visits > 1:
            bits.append(f"{link.visits} visits")
    if link.by_yuki:
        bits.append("opened by Yuki at the user's request")
    return f"{title}{link.url} ({'; '.join(b for b in bits if b)})"


def _own_rows(store: Any, sql: str, args: Sequence[Any], lo: float, hi: float) -> list[Any]:
    """A timeline read (alias ``t``; ``{own}`` where the conditions go) of the user's own rows only: ``by_yuki = 0``,
    never a row inside a page visit memory attributes to Yuki (``yuki.skills.web_route.own_rows_query``: a ``page``
    moment ``by = 'yuki'``, the visit whose first stretch began while Yuki acted), never one inside a live test's span
    (``yuki.memory.testtag.store_test_spans``). Raises what the read raises."""
    cond, cond_args = "1", []
    try:
        from yuki.memory.testtag import sql_outside_spans, store_test_spans

        cond, cond_args = sql_outside_spans("t.started_at", [(a, b) for a, b in store_test_spans(store)
                                                             if b >= lo and a <= hi])
    except Exception:  # noqa: BLE001 - no spans known: every row counts
        pass
    sql = sql.replace("{own}", f" AND t.by_yuki = 0 AND {cond}{{own}}")
    args = (*args, *cond_args)
    try:
        from yuki.skills.web_route import own_rows_query
    except Exception:  # noqa: BLE001 - a build without the browser drivers: Yuki's own rows by their mark only
        return store._query(sql.replace("{own}", ""), args)
    return own_rows_query(store, sql, args, alias="t")


def own_visits(store: Any, page_keys: Sequence[str], *, since: float | None = None,
               now: float | None = None) -> dict[str, float]:
    """S19 (mem22): ``{page_key: when the user's own newest stretch on that page ended}``, from the timeline's rows with
    them present (:func:`_own_rows`: never a page Yuki opened, nor a test's), ``since`` on. A page with none is left
    out. Content-free (the keys are digests). Never raises (``{}``)."""
    keys = sorted({str(k) for k in page_keys if k})
    if not keys:
        return {}
    try:
        return _own_visits(store, keys, since=since, now=now)
    except Exception:  # noqa: BLE001
        return {}


def _own_visits(store: Any, keys: Sequence[str], *, since: float | None, now: float | None) -> dict[str, float]:
    """:func:`own_visits`' read (``keys``: distinct, non-empty). Raises what the read raises."""
    now = time.time() if now is None else float(now)
    lo = float(since) if since is not None else 0.0
    out: dict[str, float] = {}
    for i in range(0, len(keys), 400):
        part = list(keys[i:i + 400])
        for key, last in _own_rows(
                store, f"SELECT t.page_key, max(coalesce(t.ended_at, t.started_at)) FROM timeline t WHERE "
                f"t.page_key IN ({','.join('?' * len(part))}) AND t.state = 'present' AND t.started_at >= ? "
                "AND t.started_at <= ?{own} GROUP BY t.page_key", (*part, lo, now), lo, now):
            if key and last is not None:
                out[str(key)] = float(last)
    return out


def _address_paths(link: Any) -> set[str]:
    """integ22 MEM-2: the path (with its query) a keyed link's timeline rows were keyed by, both spellings the sync
    may have read it from (it writes ``https://host`` + the path with one ``/`` put in front when it had none)."""
    url, host = str(getattr(link, "url", "") or ""), str(getattr(link, "host", "") or "")
    head = f"https://{host}"
    rest = url[len(head):] if host and url.startswith(head) else ""
    if not rest.startswith("/"):
        parts = urlsplit(url)
        rest = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    return {rest, rest[1:]}


def _page_keys(store: Any, links: Sequence[Any]) -> dict[int, set[str]]:
    """integ22 MEM-2: each keyed link's timeline keys: its stored one (its newest sighting's), and its address under
    every program the timeline saw on its site (a key holds the browser's process, so the same page had in Chrome and
    then opened by Yuki or a test in Arc is two keys, and the link keeps only the newest). Data only: the programs are
    read, never named. On a failed read, the stored key alone."""
    out = {int(link.id): {str(link.page_key)} for link in links if link.page_key}
    hosts = sorted({str(link.host) for link in links if link.page_key and link.host})
    if not hosts:
        return out
    procs: dict[str, set[str]] = {}
    try:
        for i in range(0, len(hosts), 400):
            part = hosts[i:i + 400]
            for host, proc in store._query(
                    f"SELECT DISTINCT host, process FROM timeline WHERE host IN ({','.join('?' * len(part))})"
                    " AND page_key IS NOT NULL", tuple(part)):
                if host and proc:
                    procs.setdefault(str(host), set()).add(str(proc))
        for link in links:
            if not link.page_key or not procs.get(str(link.host)):
                continue
            paths = _address_paths(link)
            out[int(link.id)] |= {store.timeline_key(proc, link.host, path, None)
                                  for proc in procs[str(link.host)] for path in paths}
    except Exception:  # noqa: BLE001 - the stored keys only, as before
        return {int(link.id): {str(link.page_key)} for link in links if link.page_key}
    return out


def own_days(store: Any, hosts: Sequence[str], *, since: float, now: float | None = None) -> dict[str, int]:
    """S19 (mem22): ``{host: on how many days since ``since`` the user had its pages in front themselves}``
    (:func:`_own_rows`; local days). Content-free. Never raises (``{}``)."""
    wanted = sorted({str(h) for h in hosts if h})
    if not wanted:
        return {}
    now = time.time() if now is None else float(now)
    try:
        rows = _own_rows(store, f"SELECT t.host, count(DISTINCT date(t.started_at, 'unixepoch', 'localtime')) FROM "
                         f"timeline t WHERE t.host IN ({','.join('?' * len(wanted))}) AND t.state = 'present' AND "
                         "t.started_at >= ? AND t.started_at <= ?{own} GROUP BY t.host", (*wanted, float(since), now),
                         float(since), now)
    except Exception:  # noqa: BLE001
        return {}
    return {str(h): int(n or 0) for h, n in rows if h}


def own_captures(store: Any, thread_ids: Sequence[int], *, since: float | None = None,
                 now: float | None = None) -> dict[int, float]:
    """S19 (mem22): ``{capture thread id: the user's own last capture of it}`` for a viewed page with no timeline key
    (its stretch had none, e.g. a full-screen one the links sync skips): a capture taken with the window in front (not
    a popup, nor one only beside it), never while Yuki acted (``by_yuki = 0``), at a moment one of the user's own
    timeline stretches on that site was in front with them present (:func:`_own_rows`: never Yuki's visit, nor a live
    test's; a capture alone also comes from a page behind another one, measured 19 Oct), ``since`` on. A thread with
    none is left out. Content-free. Never raises (``{}``)."""
    import bisect

    ids = sorted({int(i) for i in thread_ids if i is not None})
    if not ids:
        return {}
    now = time.time() if now is None else float(now)
    lo = float(since) if since is not None else 0.0
    caps: list[tuple[int, str, float]] = []
    try:
        for i in range(0, len(ids), 400):
            part = ids[i:i + 400]
            caps += [(int(r[0]), str(r[1]), float(r[2])) for r in store._query(
                "SELECT c.thread_id, th.host, c.at FROM captures c JOIN threads th ON th.id = c.thread_id WHERE"
                f" c.thread_id IN ({','.join('?' * len(part))}) AND th.host IS NOT NULL AND c.by_yuki = 0 AND"
                " c.trigger != 'popup' AND c.trigger NOT LIKE 'visible:%' AND c.at >= ? AND c.at <= ?",
                (*part, lo, now))]
        hosts = sorted({h for _t, h, _a in caps})
        spans: dict[str, list[tuple[float, float]]] = {}
        for i in range(0, len(hosts), 400):
            part = hosts[i:i + 400]
            for host, a, b in _own_rows(
                    store, f"SELECT t.host, t.started_at, coalesce(t.ended_at, t.started_at) FROM timeline t WHERE"
                    f" t.host IN ({','.join('?' * len(part))}) AND t.state = 'present' AND"
                    " coalesce(t.ended_at, t.started_at) >= ? AND t.started_at <= ?{own}", (*part, lo, now), lo, now):
                spans.setdefault(str(host), []).append((float(a), float(b)))
    except Exception:  # noqa: BLE001
        return {}
    # per site: its own stretches by start, with the latest end so far (a capture at x lies in one of them when the
    # latest end of those begun by x reaches back to it)
    index: dict[str, tuple[list[float], list[float]]] = {}
    for host, rows in spans.items():
        rows.sort()
        ends, top = [], float("-inf")
        for _a, b in rows:
            top = max(top, b)
            ends.append(top)
        index[host] = ([a for a, _b in rows], ends)
    out: dict[int, float] = {}
    for tid, host, at in caps:
        starts, ends = index.get(host, ([], []))
        k = bisect.bisect_right(starts, at) - 1
        if k >= 0 and ends[k] >= at - OWN_CAPTURE_SLACK_S and at > out.get(tid, float("-inf")):
            out[tid] = at
    return out


def own_lasts(store: Any, links: Sequence[Any], *, since: float | None = None,
              now: float | None = None, measured: set[int] | None = None) -> dict[int, float]:
    """S19 (mem22): ``{link id: when the user's own last visit of that viewed page ended}`` - its timeline keys' own
    stretches (:func:`own_visits`; integ22 MEM-2: the stored key and its address under every program seen on its site,
    :func:`_page_keys`), else, for a page with no key, their own captures of it (:func:`own_captures`). Left out: a
    shared link, a page only beside the window in front, one with no visit of theirs. ``measured`` (integ22 MEM-1):
    filled with the ids of the keyed viewed links whose own visits were read (a link left out of the result with its
    id in it has none of theirs; :func:`describe`'s ``own_known``). Never raises (``{}``)."""
    viewed = [link for link in links if getattr(link, "how", None) == "viewed"
              and getattr(link, "attention", None) != "visible"]
    keyed_links = [link for link in viewed if link.page_key]
    keys_of = _page_keys(store, keyed_links) if keyed_links else {}
    wanted = sorted({k for ks in keys_of.values() for k in ks if k})
    keyed: dict[str, float] = {}
    if wanted:
        try:
            keyed = _own_visits(store, wanted, since=since, now=now)
            if measured is not None:
                measured.update(int(link.id) for link in keyed_links)
        except Exception:  # noqa: BLE001 - not measured: no link reads as never theirs
            keyed = {}
    keyless = own_captures(store, [link.capture_thread_id for link in viewed if not link.page_key
                                   and link.capture_thread_id is not None], since=since, now=now)
    out: dict[int, float] = {}
    for link in viewed:
        if link.page_key:
            ats = [keyed[k] for k in keys_of.get(int(link.id), {str(link.page_key)}) if k in keyed]
            at = max(ats) if ats else None
        else:
            at = keyless.get(int(link.capture_thread_id)) if link.capture_thread_id is not None else None
        if at is not None:
            out[int(link.id)] = float(at)
    return out


def own_app(store: Any, link: Any, *, since: float | None = None,
            now: float | None = None) -> tuple[str, str] | None:
    """integ30 (F1): ``(app, process)`` of the user's own newest stretch on a keyed viewed page (its timeline keys,
    :func:`_page_keys`; the rows :func:`own_lasts` measures, :func:`_own_rows`: ``by_yuki = 0``, outside a live test's
    span, never Yuki's visit), ``since`` on. A link's own ``app`` is its newest sighting's, Yuki's or a test's too
    (``Store.upsert_links``: last writer wins), so where they had the page is read here. ``None``: no key, or no
    stretch of theirs. Content-free (cleartext metadata). Never raises."""
    if not getattr(link, "page_key", None) or getattr(link, "how", None) != "viewed" \
            or getattr(link, "attention", None) == "visible":
        return None
    try:
        keys = sorted(k for k in _page_keys(store, [link]).get(int(link.id), {str(link.page_key)}) if k)
        if not keys:
            return None
        now = time.time() if now is None else float(now)
        lo = float(since) if since is not None else 0.0
        rows = _own_rows(store, f"SELECT t.app, t.process, coalesce(t.ended_at, t.started_at) FROM timeline t WHERE "
                         f"t.page_key IN ({','.join('?' * len(keys))}) AND t.state = 'present' AND t.started_at >= ? "
                         "AND t.started_at <= ?{own} ORDER BY 3 DESC LIMIT 1", (*keys, lo, now), lo, now)
    except Exception:  # noqa: BLE001
        return None
    if not rows or not (rows[0][0] or rows[0][1]):
        return None
    return str(rows[0][0] or ""), str(rows[0][1] or "")


def own_last(store: Any, link: Any, *, since: float | None = None, now: float | None = None) -> float | None:
    """:func:`own_lasts` for one link (``None``: no visit of theirs). Never raises."""
    return own_lasts(store, [link], since=since, now=now).get(int(link.id))


def seconds_of(store: Any, links: Sequence[Any]) -> dict[int, float]:
    """Time in front per viewed link (the timeline's page stretches)."""
    keys = {link.page_key for link in links if link.page_key}
    try:
        per_key = store.page_seconds(keys)
    except Exception:
        return {}
    return {link.id: per_key[link.page_key][0] for link in links if link.page_key in per_key}
