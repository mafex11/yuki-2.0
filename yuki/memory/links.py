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
    describe(link, seconds=None, now=None) -> str
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


def describe(link: Any, seconds: float | None = None, now: float | None = None) -> str:
    """One line: ``"Title" - https://... (github.com; viewed in Google Chrome, last Tue 29 Sep 03:40, 4 min in front,
    3 visits)``, or for a shared link who sent it and where."""
    now = time.time() if now is None else float(now)
    last = datetime.fromtimestamp(link.last_seen)
    when = last.strftime("today %H:%M") if last.date() == datetime.fromtimestamp(now).date() else last.strftime(
        "%a %d %b %H:%M")
    title = f"\"{' '.join((link.title or '').split())[:120]}\" - " if link.title else ""
    bits = [link.host or ""]
    if link.how == "shared":
        who = f" by {link.sender}" if link.sender else ""
        bits.append(f"shared{who} in {link.where or link.app or 'a conversation'}, {when}")
    elif getattr(link, "attention", None) == "visible":
        bits.append(f"on screen{' in ' + link.app if link.app else ''} beside the window in front (maybe not read), "
                    f"last {when}")
    else:
        bits.append(f"viewed{' in ' + link.app if link.app else ''}, last {when}")
        if seconds:
            minutes = int(round(seconds / 60.0))
            bits.append(f"{minutes} min in front" if minutes else f"{int(seconds)} s in front")
        if link.visits > 1:
            bits.append(f"{link.visits} visits")
    if link.by_yuki:
        bits.append("opened by Yuki at the user's request")
    return f"{title}{link.url} ({'; '.join(b for b in bits if b)})"


def seconds_of(store: Any, links: Sequence[Any]) -> dict[int, float]:
    """Time in front per viewed link (the timeline's page stretches)."""
    keys = {link.page_key for link in links if link.page_key}
    try:
        per_key = store.page_seconds(keys)
    except Exception:
        return {}
    return {link.id: per_key[link.page_key][0] for link in links if link.page_key in per_key}
