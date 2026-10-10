"""YouTube's fresh uploads from the user's own channels, named on request; nothing plays (PLAN.md Phase T, D0, D16;
map-routines T1.4).

**Zero setup (D0, D16):** no API key, no account, no sign-in. Every address is a template of the site file
``yuki/connect/sites/youtube.toml`` (``[urls]``: the handle page, the public feed, oEmbed, the watch page), and every
answer is a fixed machine format: the page's ``<link rel="canonical">`` tag, the Atom feed, oEmbed's JSON. Nothing
here guesses meaning.

**Which channels:** the user's own, from taste (kind :data:`CHANNEL_KIND`, the site's handle page kind:
:func:`yuki.skills.taste.site_kinds`): the handle pages they visit and the videos they watch, by hour of day. A watched
video counts for its channel once its channel is known: a feed listed it, or one oEmbed read of it (the nightly part).
Never the portrait's interests (rule 3: no join of interests to handles by name).

**Reads** (all counted in ``yt_reads``, at most :data:`MAX_READS_PER_HOUR` in any hour across processes, of which the
nightly part takes at most :data:`NIGHT_READS`; never with a game in front; never when the user's privacy file blocks
the site):

* a channel's id: one read of its handle page, once (``yt_channels``; a failure is retried at most
  :data:`LOOKUP_TRIES` times, a week apart): the nightly part, for the channels the user picks most;
* a watched video's channel: one oEmbed read per video id, ever (the nightly part, the newest watches first, the
  ones right after an offer before the rest);
* a channel's fresh uploads: its public feed, **only on request** (:func:`offer`): when a run opens YouTube's home
  page (:func:`wants`), the top :data:`FEED_CHANNELS` channels at this hour whose feed is older than
  :data:`FEED_FRESH_S` are read in parallel within :data:`REQUEST_BUDGET_S`; older feeds serve otherwise.

**The offer:** videos published in the last :data:`FRESH_DAYS` that the user never had on screen (no ``page`` moment
of that video: the moments' ``entity_key``, D23), ranked by ``taste score of the channel at this hour x
exp(-age / 7 days)``, one per channel, the top :data:`OFFER_N`. They are named aloud with the done line
(:func:`offer_line`: "New from NDL, Deji and Sidemen: want one?"; the names are the channels' own, from the user's
data). **Nothing plays:** a video opens only when the user picks one ("the Deji one": :func:`pick`, through
``web.open_video``, whose slot takes no taste guess). Shorts are left out (their link is not a watch page).

**Learning and the live check:** each offer is a ``taste_events`` row; the nightly part writes its outcome: the user's
next YouTube pick within :data:`OUTCOME_S` was one of the three (``hit``), from one of the three channels
(``channel``), something else (``miss``), or nothing (``none``). Once :data:`LIVE_MIN_PICKS` picks are on record, a
rate under :data:`LIVE_MIN_RATE` switches the offers off (PLAN.md Phase T: plain YouTube home stays), noted once.

Logs and returned counts are content-free (``yt_fetch``: channels, ok, failed, capped, bytes, ms); names, handles,
ids and titles are encrypted at rest and decrypted in RAM only. None of the tables is in the MCP.
"""

from __future__ import annotations

import codecs
import json
import math
import statistics
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Mapping, Sequence
from xml.etree import ElementTree

#: The site file this module reads (``yuki/connect/sites/youtube.toml``) and the names of its page kinds and URL
#: templates it uses: data names, the addresses themselves live in the file.
SITE = "youtube"
HANDLE_PAGE = "handle"
CHANNEL_PAGE = "channel"
WATCH_PAGE = "watch"
HOME_PAGE = "home"
FEED_URL = "feed"
OEMBED_URL = "oembed"
#: The taste kind of a channel: the site's handle page kind (``taste.site_kinds`` names it ``<site>_<page>``).
CHANNEL_KIND = f"{SITE}_{HANDLE_PAGE}"

#: Network reads in any hour, every kind together (PLAN.md Phase T: at most 8 an hour).
MAX_READS_PER_HOUR = 8
#: Channels whose feed may be read on one request, and how old a feed may be before it is read again (T1.4).
FEED_CHANNELS = 8
FEED_FRESH_S = 3600.0
#: How long a request waits for the feeds (T1.4: 1.5 s); a slower feed serves the next request.
REQUEST_BUDGET_S = 1.5
#: Fresh: published within this many days; the age weight's scale (T1.4).
FRESH_DAYS = 14.0
AGE_DAYS = 7.0
#: Videos named per offer, and how long the named ones stay pickable ("the Deji one").
OFFER_N = 3
OFFER_KEEP_S = 900.0
#: The user's next pick within this long after an offer is its outcome.
OUTCOME_S = 600.0
#: A watch whose channel is still unknown leaves its offer's outcome open this long (the nightly oEmbed may name it).
OUTCOME_OPEN_S = 3 * 86400.0
#: The live check (PLAN.md Phase T, about 10 picks): under this rate the offers go off.
LIVE_MIN_PICKS = 10
LIVE_MIN_RATE = 0.5
LIVE_DAYS = 30.0
#: The nudge-state row that says the offers are off (``off:<epoch>``; delete it to turn them back on).
OFF_STATE = "taste.youtube_offers"
#: A channel id lookup that failed is retried after this long, at most this many times in all.
LOOKUP_RETRY_S = 7 * 86400.0
LOOKUP_TRIES = 3
#: The nightly part: its reads at most (half the hour's cap, so a request in the same hour still reads feeds), of
#: which channel lookups at most (the rest go to oEmbed), the channels it looks at (most picked first), how far back it
#: attributes watched videos, and each read's socket timeout.
NIGHT_READS = 4
NIGHT_LOOKUPS = 3
NIGHT_CHANNELS = 16
ATTRIBUTE_DAYS = 14.0
NIGHT_TIMEOUT_S = 8.0
#: Bytes read at most per answer (a handle page is large; its canonical link sits in the head and ends the read).
PAGE_MAX_BYTES = 2_000_000
FEED_MAX_BYTES = 512_000
OEMBED_MAX_BYTES = 65_536
CHUNK = 65_536
#: Videos and the read log kept this long.
KEEP_DAYS = 60.0
READS_KEEP_S = 2 * 86400.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Yuki"
#: The Atom feed's XML namespaces (YouTube's documented feed format).
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"

LogFn = Callable[..., None]


@dataclass(frozen=True)
class Offer:
    """One fresh upload named aloud: the name said (the channel's own display name, its handle until a feed gave the
    name), the video's title and watch link (RAM only), its key (the moments' ``entity_key`` of its watch page) and
    its channel's key, its age and score."""

    name: str
    title: str
    ref: str
    key: str
    channel_key: str
    age_days: float
    score: float

    def value(self) -> dict[str, Any]:
        """The ``video`` slot's value when the user picks it: the name said, the key and the link."""
        return {"name": self.name, "key": self.key, "ref": self.ref}


def _log(log: LogFn | None, event: str, **fields: Any) -> None:
    if log is None:
        return
    try:
        from yuki.log.events import content_free

        rec = content_free({"type": event, **{k: v for k, v in fields.items() if v is not None}})
        rec.pop("type", None)
        log(event, **rec)
    except Exception:  # noqa: BLE001 - a log never breaks a read
        pass


def _fold(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _plain(text: str) -> bool:
    """An id as YouTube writes one (letters, digits, ``-`` and ``_``): a fixed machine format."""
    return bool(text) and len(text) <= 64 and all(ch.isascii() and (ch.isalnum() or ch in "-_") for ch in text)


def _url(name: str, **values: str) -> str | None:
    from yuki.connect.templates import fill

    url = fill(SITE, name, **values)
    return url if url and url.startswith("https://") else None


def _page(url: Any) -> Any:
    from yuki.connect.templates import page_of

    m = page_of(str(url or ""))
    return m if m is not None and m.site == SITE else None


def video_key(store: Any, video_id: str) -> str:
    """A video's key: the moments' ``entity_key`` of its watch page (D23), so "never on screen" is an index lookup."""
    from yuki.memory.moments import entity_key

    return str(entity_key(store.cipher, SITE, video_id) or "")


def channel_key(store: Any, handle: str) -> str:
    """A channel's key: its taste row's ``entity_key`` (kind :data:`CHANNEL_KIND`, the handle)."""
    from yuki.skills.taste import entity_key

    return entity_key(store, CHANNEL_KIND, handle)


def _handle_group(store: Any, handle: str) -> str:
    """The moments' ``entity_key`` of the channel's handle page: the group a watched video's pick joins in taste."""
    from yuki.memory.moments import entity_key

    return str(entity_key(store.cipher, SITE, handle) or _fold(handle))


# ---------------------------------------------------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------------------------------------------------


def wants(values: Mapping[str, Any] | None) -> bool:
    """Whether a run's values open this site's home page (``web.open_page`` with the site and no page, or its home
    page kind): the request that brings the offer. Identity of the site's name (:func:`connection.scope_value`)."""
    if not isinstance(values, Mapping):
        return False
    try:
        from yuki.skills.connection import scope_value

        if scope_value(values.get("site")) != SITE:
            return False
    except Exception:  # noqa: BLE001
        return False
    return _fold(values.get("page")) in ("", HOME_PAGE)


_privacy_lock = threading.Lock()
_privacy: Any = None


def _privacy_blocked() -> bool:
    """Whether the user's privacy file blocks the site (its home address checked like any page): then nothing is read
    or offered. Fails closed."""
    global _privacy
    try:
        from yuki.connect.templates import site
        from yuki.memory.privacy import PrivacyConfig

        with _privacy_lock:
            if _privacy is None:
                _privacy = PrivacyConfig()
            rules = _privacy.rules()
        home = str((site(SITE).get("site") or {}).get("home") or "")
        return not home or rules.check_url(home) is not None
    except Exception:  # noqa: BLE001
        return True


def blocked(game: str | None = None) -> str | None:
    """A code word when nothing may be read now: ``game`` (a game in front: the caller's read, else D9's local read;
    ``game_unknown`` when that fails), ``privacy`` (the site is blocked); else None."""
    if game:
        return "game"
    try:
        from yuki.memory.gamegate import game_in_front

        if game_in_front():
            return "game"
    except Exception:  # noqa: BLE001
        return "game_unknown"
    return "privacy" if _privacy_blocked() else None


def enabled(store: Any) -> bool:
    """Whether the offers are on (the live check switches them off: :data:`OFF_STATE`)."""
    try:
        return not str(store.nudge_state(OFF_STATE) or "").startswith("off")
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------------------------------------------------------------
# Reads (fixed formats)
# ---------------------------------------------------------------------------------------------------------------------


def _read(store: Any, kind: str, url: str | None, *, max_bytes: int, timeout_s: float, deadline: float | None = None,
          stop: Callable[[bytes], bool] | None = None) -> tuple[int, bytes] | None:
    """One GET under the hourly cap: ``(HTTP status, body)`` (status 0: no answer), or None when the cap is reached or
    the address is not an https template (nothing read). ``stop(chunk)`` True ends the read early. Never raises."""
    if not url or not url.startswith("https://"):
        return None
    try:
        rid = store.yt_read_reserve(kind, cap=MAX_READS_PER_HOUR)
    except Exception:  # noqa: BLE001 - no table yet: no read
        return None
    if rid is None:
        return None
    started = time.perf_counter()
    status, parts, n = 0, [], 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
        with urllib.request.urlopen(req, timeout=max(0.2, float(timeout_s))) as resp:  # noqa: S310 - https templates
            status = int(getattr(resp, "status", 200) or 200)
            while n < max_bytes:
                if deadline is not None and time.perf_counter() > deadline:
                    break
                chunk = resp.read(min(CHUNK, max_bytes - n))
                if not chunk:
                    break
                parts.append(chunk)
                n += len(chunk)
                if stop is not None and stop(chunk):
                    break
    except urllib.error.HTTPError as exc:
        status = int(getattr(exc, "code", 0) or 0)
    except Exception:  # noqa: BLE001 - offline, a timeout, a reset: status 0
        status = 0
    body = b"".join(parts)
    try:
        store.yt_read_done(rid, status=status, nbytes=len(body), ms=(time.perf_counter() - started) * 1000.0)
    except Exception:  # noqa: BLE001
        pass
    return status, body


class _Canonical(HTMLParser):
    """The first ``<link rel="canonical" href=...>`` of a page (a fixed tag), fed chunk by chunk."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.href: str | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.href is not None or tag != "link":
            return
        a = {str(k).casefold(): str(v or "") for k, v in attrs}
        if "canonical" in a.get("rel", "").casefold().split() and a.get("href"):
            self.href = a["href"]

    def take(self, chunk: bytes) -> bool:
        """Feed one chunk; True once the canonical link was read (the read may stop)."""
        if self.href is None:
            try:
                self.feed(self._decoder.decode(chunk))
            except Exception:  # noqa: BLE001 - malformed markup: read on
                pass
        return self.href is not None


def channel_id_of(href: Any) -> str | None:
    """The channel id of a canonical link that is the site's channel page (its ``/channel/{id}`` template), or None."""
    m = _page(href)
    if m is None or m.kind != CHANNEL_PAGE:
        return None
    cid = str(m.entity or "")
    return cid if _plain(cid) else None


def _iso(text: Any) -> float | None:
    try:
        stamp = datetime.fromisoformat(str(text or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def parse_feed(data: bytes) -> tuple[str | None, list[dict[str, Any]]]:
    """``(the channel's display name, [{video_id, title, published_at, link}])`` of a channel's public Atom feed
    (YouTube's documented format). A document with a DTD is refused (no entities are ever expanded). Never raises."""
    if not data or b"<!DOCTYPE" in data[:4096].upper() or b"<!ENTITY" in data.upper():
        return None, []
    try:
        root = ElementTree.fromstring(data)
    except (ElementTree.ParseError, ValueError):
        return None, []
    name = root.findtext(f"{ATOM}author/{ATOM}name") or root.findtext(f"{ATOM}title") or ""
    out: list[dict[str, Any]] = []
    for entry in root.findall(f"{ATOM}entry"):
        vid = (entry.findtext(f"{YT}videoId") or "").strip()
        if not _plain(vid):
            continue
        link = next((str(el.get("href") or "") for el in entry.findall(f"{ATOM}link")
                     if (el.get("rel") or "alternate") == "alternate"), "")
        out.append({"video_id": vid, "title": " ".join((entry.findtext(f"{ATOM}title") or "").split()),
                    "published_at": _iso(entry.findtext(f"{ATOM}published")), "link": link})
    return (" ".join(name.split()) or None), out


def _is_watch(link: str) -> bool:
    m = _page(link)
    return m is not None and m.kind == WATCH_PAGE


def fetch_feed(store: Any, key: str, channel_id: str, *, now: float, timeout_s: float,
               deadline: float | None = None) -> dict[str, int]:
    """Read one channel's feed and keep its uploads (watch pages only: shorts left out), its display name and the
    cache row. Content-free counts: ``ok`` / ``failed`` / ``capped``, ``bytes``, ``videos``."""
    got = _read(store, "feed", _url(FEED_URL, id=channel_id), max_bytes=FEED_MAX_BYTES, timeout_s=timeout_s,
                deadline=deadline)
    if got is None:
        return {"capped": 1}
    status, body = got
    try:
        store.save_yt_feed_cache(key, at=now, status=status, nbytes=len(body))
    except Exception:  # noqa: BLE001
        pass
    if status != 200:
        return {"failed": 1, "bytes": len(body)}
    name, entries = parse_feed(body)
    rows = [{"video_key": video_key(store, e["video_id"]), "channel_key": key, "video_id": e["video_id"],
             "title": e["title"] or None, "published_at": e["published_at"], "source": "feed"}
            for e in entries if not e["link"] or _is_watch(e["link"])]
    try:
        store.save_yt_videos(rows, at=now)
        if name:
            store.save_yt_channel(key, name=name, at=now)
    except Exception:  # noqa: BLE001
        return {"failed": 1, "bytes": len(body)}
    return {"ok": 1, "bytes": len(body), "videos": len(rows)}


def _fetch_all(store: Any, todo: Sequence[tuple[str, str]], *, now: float, budget_s: float,
               log: LogFn | None) -> dict[str, int]:
    """The feeds of ``todo`` (``(channel key, channel id)``) read in parallel, waited for at most ``budget_s``; a feed
    still on its way finishes on its own short thread and serves the next request."""
    counts = {"channels": len(todo), "ok": 0, "failed": 0, "capped": 0, "late": 0, "bytes": 0}
    if not todo or budget_s <= 0:
        return counts
    started = time.perf_counter()
    deadline = started + budget_s
    results: list[dict[str, int]] = []
    lock = threading.Lock()

    def one(key: str, cid: str) -> None:
        got = fetch_feed(store, key, cid, now=now, timeout_s=budget_s, deadline=deadline + 0.5)
        with lock:
            results.append(got)

    threads = [threading.Thread(target=one, args=(k, c), name="yuki-yt-feed", daemon=True) for k, c in todo]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, deadline - time.perf_counter()))
    with lock:
        for r in results:
            for k in ("ok", "failed", "capped", "bytes"):
                counts[k] += int(r.get(k, 0))
        counts["late"] = len(todo) - len(results)
    _log(log, "yt_fetch", ms=round((time.perf_counter() - started) * 1000.0, 1), **counts)
    return counts


# ---------------------------------------------------------------------------------------------------------------------
# The offer (on request)
# ---------------------------------------------------------------------------------------------------------------------


def rank(store: Any, picks: Sequence[Any], known: Mapping[str, Mapping[str, Any]], *, now: float) -> list[Offer]:
    """The top :data:`OFFER_N` fresh, never-shown uploads of ``picks`` (taste rows of :data:`CHANNEL_KIND` with their
    score at this hour), one per channel, by ``score x exp(-age / AGE_DAYS)``; names told apart (a second channel of
    the same name is left out)."""
    from yuki.connect.templates import fill

    score = {t.entity_key: float(t.score) for t in picks}
    if not score:
        return []
    vids = store.yt_videos(channel_keys=list(score), published_since=now - FRESH_DAYS * 86400.0, unwatched=True)
    seen = store.page_entity_keys_seen([v["video_key"] for v in vids])
    best: dict[str, Offer] = {}
    for v in vids:
        key, ch, pub = str(v["video_key"]), str(v.get("channel_key") or ""), v.get("published_at")
        if key in seen or not v.get("video_id") or pub is None or float(pub) > now or ch not in score:
            continue
        age = max(0.0, (now - float(pub)) / 86400.0)
        s = score[ch] * math.exp(-age / AGE_DAYS)
        info = known.get(ch) or {}
        name = " ".join(str(info.get("name") or info.get("handle") or "").split())
        ref = fill(SITE, WATCH_PAGE, id=str(v["video_id"])) or ""
        if not name or not ref:
            continue
        if ch not in best or s > best[ch].score:
            best[ch] = Offer(name, str(v.get("title") or ""), ref, key, ch, round(age, 2), s)
    out: list[Offer] = []
    names: set[str] = set()
    for o in sorted(best.values(), key=lambda o: (-o.score, o.age_days, o.key)):
        if _fold(o.name) in names:
            continue
        names.add(_fold(o.name))
        out.append(o)
        if len(out) >= OFFER_N:
            break
    return out


def offer(store: Any, *, now: float | None = None, game: str | None = None, budget_s: float = REQUEST_BUDGET_S,
          log: LogFn | None = None) -> list[Offer]:
    """On request (a run opening YouTube's home): read the stale feeds of the top :data:`FEED_CHANNELS` channels at
    this hour within ``budget_s`` (the hourly cap holds), then the top :data:`OFFER_N` fresh uploads, marked offered
    and noted (``taste_events`` ``offered``). Nothing is opened or played. ``[]`` when the offers are off, a game is
    in front, the site is blocked, or nothing is fresh. Never raises."""
    now = time.time() if now is None else float(now)
    started = time.perf_counter()
    try:
        if not enabled(store):
            _log(log, "taste_offer", off=True)
            return []
        why = blocked(game)
        if why is not None:
            _log(log, "taste_offer", held=why)
            return []
        from yuki.skills import taste

        picks = taste.top(store, CHANNEL_KIND, now=now, n=FEED_CHANNELS)
        if not picks:
            _log(log, "taste_offer", n=0, channels=0)
            return []
        keys = [t.entity_key for t in picks]
        known = store.yt_channels(keys)
        cache = store.yt_feed_cache(keys)
        stale = [(k, str(known[k]["channel_id"])) for k in keys
                 if (known.get(k) or {}).get("channel_id") and now - cache.get(k, (0.0, 0))[0] >= FEED_FRESH_S]
        left = float(budget_s) - (time.perf_counter() - started)
        fetched = _fetch_all(store, stale, now=now, budget_s=left, log=log)
        known = store.yt_channels(keys)   # display names a feed just gave
        offers = rank(store, picks, known, now=now)
        if offers:
            store.mark_yt_videos([o.key for o in offers], "offered_at", now)
            store.add_taste_event("offered", {"videos": [o.key for o in offers],
                                              "channels": [o.channel_key for o in offers],
                                              "ages_d": [o.age_days for o in offers], "fetched": fetched["ok"]}, at=now)
        _log(log, "taste_offer", n=len(offers), channels=len(picks), with_id=sum(
            1 for k in keys if (known.get(k) or {}).get("channel_id")), ms=round((time.perf_counter() - started)
                                                                                    * 1000.0, 1))
        return offers
    except Exception as exc:  # noqa: BLE001 - an offer never breaks the request
        _log(log, "taste_offer", error=type(exc).__name__)
        return []


def offer_line(offers: Sequence[Offer]) -> str:
    """The question said with the done line: "New from NDL, Deji and Sidemen: want one?" (the channels' own names,
    from the user's data). ``""`` with no offer."""
    names = [" ".join(o.name.split()) for o in offers if o.name.strip()][:OFFER_N]
    if not names:
        return ""
    said = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return f"New from {said}: want {'it' if len(names) == 1 else 'one'}?"


class Pending:
    """An offer being made on its own short thread while the page opens."""

    def __init__(self) -> None:
        self._done = threading.Event()
        self.offers: list[Offer] = []

    def wait(self, timeout_s: float) -> list[Offer]:
        """The offers when they are ready within ``timeout_s``, else ``[]`` (they still land for "the X one")."""
        if self._done.wait(max(0.0, float(timeout_s))):
            return list(self.offers)
        return []


def _with_store(memory: Any, fn: Callable[[Any], Any], timeout_s: float) -> Any:
    """``fn(store)`` on a store, an object holding one (``.store``), or through a bounded memory call."""
    if memory is None:
        return None
    if hasattr(memory, "_query") and hasattr(memory, "cipher"):
        return fn(memory)
    store = getattr(memory, "store", None)
    if store is not None:
        return fn(store)
    call = getattr(memory, "call", None)
    if callable(call):
        return call("taste offers", lambda client: fn(getattr(client, "store", client)), timeout_s=timeout_s)
    return None


def start(memory: Any, values: Mapping[str, Any] | None, *, game: str | None = None, log: LogFn | None = None,
          budget_s: float = REQUEST_BUDGET_S) -> Pending | None:
    """On request: when a run's values open YouTube's home (:func:`wants`) and no game is in front, make the offer on a
    short thread of its own (it reads the feeds while the page opens); None otherwise. The caller waits on the
    returned :class:`Pending` after the run's done-check, and names the offers only with a checked done."""
    if not wants(values) or game:
        return None
    pending = Pending()

    def work() -> None:
        try:
            got = _with_store(memory, lambda store: offer(store, game=game, budget_s=budget_s, log=log),
                              timeout_s=budget_s + 1.0)
            pending.offers = list(got or [])
        except Exception:  # noqa: BLE001
            pending.offers = []
        finally:
            pending._done.set()

    threading.Thread(target=work, name="yuki-taste-offer", daemon=True).start()
    return pending


def offered(store: Any, *, now: float | None = None, within_s: float = OFFER_KEEP_S) -> list[Offer]:
    """The last offer's videos, when it was made within ``within_s`` (what "the Deji one" is matched against), in the
    order they were named. Never raises (``[]``)."""
    now = time.time() if now is None else float(now)
    try:
        events = store.taste_events(since=now - float(within_s), until=now + 1.0, kinds=["offered"])
        if not events:
            return []
        keys = [str(k) for k in (events[-1].get("detail") or {}).get("videos") or []]
        rows = {str(v["video_key"]): v for v in store.yt_videos(video_keys=keys)}
        known = store.yt_channels({str(v.get("channel_key") or "") for v in rows.values()})
    except Exception:  # noqa: BLE001
        return []
    from yuki.connect.templates import fill

    out: list[Offer] = []
    for key in keys:
        v = rows.get(key)
        if v is None or not v.get("video_id"):
            continue
        info = known.get(str(v.get("channel_key") or "")) or {}
        name = " ".join(str(info.get("name") or info.get("handle") or "").split())
        ref = fill(SITE, WATCH_PAGE, id=str(v["video_id"])) or ""
        pub = v.get("published_at")
        age = max(0.0, (now - float(pub)) / 86400.0) if pub is not None else 0.0
        if name and ref:
            out.append(Offer(name, str(v.get("title") or ""), ref, key, str(v.get("channel_key") or ""),
                             round(age, 2), 0.0))
    return out


def pick(store: Any, value: Any, *, now: float | None = None) -> dict[str, Any] | None:
    """The offered video the user picked: ``value`` (a name, or a slot value with a ``name``) equals, case and spacing
    folded, the name said, the channel's handle or the video's whole title. Identity only, never meaning; None when
    it matches none or more than one."""
    name = value.get("name") if isinstance(value, Mapping) else value
    want = _fold(name).lstrip("@")
    if not want:
        return None
    offers = offered(store, now=now)
    if not offers:
        return None
    try:
        known = store.yt_channels({o.channel_key for o in offers})
    except Exception:  # noqa: BLE001
        known = {}
    hits = [o for o in offers
            if want in {_fold(o.name), _fold(o.title), _fold((known.get(o.channel_key) or {}).get("handle"))} - {""}]
    return hits[0].value() if len(hits) == 1 else None


# ---------------------------------------------------------------------------------------------------------------------
# For taste's recompute
# ---------------------------------------------------------------------------------------------------------------------


def _template(row: Mapping[str, Any]) -> str:
    return str((row.get("facts") or {}).get("template") or "")


def attributed(store: Any, page_rows: Iterable[Mapping[str, Any]],
               users: Callable[[Mapping[str, Any]], bool]) -> list[tuple[float, str, str, str]]:
    """The user's watched videos whose channel is known (a feed listed it, or oEmbed named it), as channel picks for
    taste: ``(at, handle, the handle page's link, group)`` (group: the moments' ``entity_key`` of the handle page, so a
    channel page and one of its videos minutes apart are one pick). ``users(row)``: the pick is the user's."""
    from yuki.connect.templates import fill

    watch = f"{SITE}.{WATCH_PAGE}"
    rows = [r for r in page_rows if _template(r) == watch and r.get("entity_key") and users(r)]
    if not rows:
        return []
    vids = {str(v["video_key"]): str(v["channel_key"]) for v in store.yt_videos(
        video_keys={str(r["entity_key"]) for r in rows}) if v.get("channel_key")}
    chans = store.yt_channels(set(vids.values()))
    out: list[tuple[float, str, str, str]] = []
    for r in rows:
        handle = (chans.get(vids.get(str(r["entity_key"]), "")) or {}).get("handle")
        if handle:
            out.append((float(r["at"]), str(handle), fill(SITE, HANDLE_PAGE, handle=str(handle)) or "",
                        _handle_group(store, str(handle))))
    return out


def fresh_ages(store: Any) -> dict[str, float]:
    """``{channel_key: hours}``: the median age of the videos the user watched from each channel when they watched
    them (T1.1's ``fresh_age_h``: measured, not assumed)."""
    try:
        return {k: round(statistics.median(v), 1) for k, v in store.yt_watch_ages().items() if v}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------------------------------------------------
# The nightly part (taste.seed)
# ---------------------------------------------------------------------------------------------------------------------


def lookup_channels(store: Any, *, now: float, log: LogFn | None = None, limit: int = NIGHT_LOOKUPS) -> dict[str, int]:
    """One read of the handle page of each channel the user picks most (:data:`NIGHT_CHANNELS`, most picked first)
    that has no id yet: its canonical link's channel id, kept for good. At most ``limit`` reads; a failure is retried a
    week later, at most :data:`LOOKUP_TRIES` times."""
    from yuki.skills import taste

    rows = taste.top(store, CHANNEL_KIND, now=now, n=10_000)
    rows.sort(key=lambda t: (-t.n_obs, t.entity_key))
    rows = [t for t in rows if t.name][:NIGHT_CHANNELS]
    known = store.yt_channels([t.entity_key for t in rows])
    counts = {"lookups": 0, "resolved": 0, "lookup_failed": 0}
    for t in rows:
        info = known.get(t.entity_key) or {}
        if info.get("channel_id"):
            continue
        if not info:
            store.save_yt_channel(t.entity_key, handle=t.name, at=now)
        tries = int(info.get("tries") or 0)
        if tries >= LOOKUP_TRIES or (tries and now - float(info.get("updated_at") or 0.0) < LOOKUP_RETRY_S):
            continue
        if counts["lookups"] >= limit or blocked() is not None:
            break
        parser = _Canonical()
        got = _read(store, "page", _url(HANDLE_PAGE, handle=t.name), max_bytes=PAGE_MAX_BYTES,
                    timeout_s=NIGHT_TIMEOUT_S, stop=parser.take)
        if got is None:
            break   # the hour's reads are used up
        counts["lookups"] += 1
        status, _body = got
        cid = channel_id_of(parser.href) if status == 200 else None
        if cid:
            store.save_yt_channel(t.entity_key, handle=t.name, channel_id=cid, tried=True, at=now)
            counts["resolved"] += 1
        else:
            store.save_yt_channel(t.entity_key, tried=True, error="no_canonical" if status == 200 else "http",
                                  at=now)
            counts["lookup_failed"] += 1
    _log(log, "yt_lookup", **counts)
    return counts


def attribute_watched(store: Any, *, now: float, log: LogFn | None = None, limit: int = NIGHT_READS) -> dict[str, int]:
    """oEmbed reads (one per video id, ever) for the user's own watched videos of the last :data:`ATTRIBUTE_DAYS` whose
    channel is unknown: the watches right after an offer first (their outcome waits on it), then the newest; as many
    as ``limit`` and the hour's cap allow. Each answer's ``author_url`` (the channel's handle page) and
    ``author_name``."""
    counts = {"oembed": 0, "attributed": 0, "oembed_failed": 0}
    try:
        rows = store.moments_between(now - ATTRIBUTE_DAYS * 86400.0, now + 1.0, kinds=["page"])
    except Exception:  # noqa: BLE001
        return counts
    watch = f"{SITE}.{WATCH_PAGE}"
    rows = [r for r in rows if _template(r) == watch and r.get("by") == "user" and r.get("entity_key")]
    if not rows:
        return counts
    known = {str(v["video_key"]) for v in store.yt_videos(video_keys={str(r["entity_key"]) for r in rows})}
    windows = [(float(e["at"]), float(e["at"]) + OUTCOME_S) for e in store.taste_events(
        since=now - ATTRIBUTE_DAYS * 86400.0, kinds=["offered"])]
    todo: dict[str, Mapping[str, Any]] = {}
    for r in sorted(rows, key=lambda r: (not any(lo <= float(r["at"]) <= hi for lo, hi in windows),
                                         -float(r["at"]))):
        key = str(r["entity_key"])
        if key not in known and key not in todo:
            todo[key] = r
    for key, r in todo.items():
        if counts["oembed"] >= limit or blocked() is not None:
            break
        try:
            vid = str(store.moment_entity(r) or "")
        except Exception:  # noqa: BLE001
            continue
        watch_url = _url(WATCH_PAGE, id=vid) if _plain(vid) else None
        if not watch_url:
            continue
        got = _read(store, "oembed", _url(OEMBED_URL, url=watch_url), max_bytes=OEMBED_MAX_BYTES,
                    timeout_s=NIGHT_TIMEOUT_S)
        if got is None:
            break   # the hour's reads are used up
        counts["oembed"] += 1
        status, body = got
        handle, name = None, None
        if status == 200:
            try:
                data = json.loads(body.decode("utf-8", errors="replace"))
                m = _page(data.get("author_url"))
                if m is not None and m.kind == HANDLE_PAGE and m.entity:
                    handle = str(m.entity)
                    name = " ".join(str(data.get("author_name") or "").split()) or None
            except (ValueError, AttributeError):
                handle = None
        ch = channel_key(store, handle) if handle else None
        if ch:
            store.save_yt_channel(ch, handle=handle, name=name, at=now)
            counts["attributed"] += 1
        else:
            counts["oembed_failed"] += 1
        # one row either way: a video id is read once, ever (a failed one keeps no channel)
        store.save_yt_videos([{"video_key": key, "channel_key": ch, "video_id": vid, "source": "oembed"}], at=now)
        store.mark_yt_videos([key], "watched_at", float(r["at"]))
    _log(log, "yt_oembed", **counts)
    return counts


def _outcome(store: Any, event: Mapping[str, Any], now: float) -> str | None:
    """An offer's outcome from the user's next YouTube page within :data:`OUTCOME_S` (any side: a pick through Yuki
    counts): ``hit`` / ``channel`` / ``miss`` / ``none``, or None while the picked video's channel is still unknown
    (for :data:`OUTCOME_OPEN_S`; ``unknown`` after)."""
    detail = event.get("detail") or {}
    videos = {str(k) for k in detail.get("videos") or []}
    channels = {str(k) for k in detail.get("channels") or []}
    at = float(event["at"])
    watch, handle_page = f"{SITE}.{WATCH_PAGE}", f"{SITE}.{HANDLE_PAGE}"
    for r in store.moments_between(at, at + OUTCOME_S, kinds=["page"]):
        tmpl = _template(r)
        if tmpl == watch and r.get("entity_key"):
            key = str(r["entity_key"])
            if key in videos:
                return "hit"
            ch = next((str(v.get("channel_key") or "") for v in store.yt_videos(video_keys=[key])), "")
            if ch:
                return "channel" if ch in channels else "miss"
            return None if now - at < OUTCOME_OPEN_S else "unknown"
        if tmpl == handle_page:
            try:
                handle = str(store.moment_entity(r) or "")
            except Exception:  # noqa: BLE001
                continue
            if handle:
                return "channel" if channel_key(store, handle) in channels else "miss"
    return "none"


def live_bar(store: Any, *, now: float | None = None, days: float = LIVE_DAYS) -> dict[str, Any]:
    """The live check's numbers (content-free): offers, outcomes by kind, picks (hit + channel + miss), how many were
    among the three, the rate, and whether the offers are off."""
    now = time.time() if now is None else float(now)
    try:
        events = store.taste_events(since=now - float(days) * 86400.0, kinds=["offered", "outcome"])
    except Exception:  # noqa: BLE001
        return {"error": 1}
    out: dict[str, Any] = {"offers": sum(1 for e in events if e["kind"] == "offered"), "hit": 0, "channel": 0,
                           "miss": 0, "none": 0, "unknown": 0}
    for e in events:
        if e["kind"] == "outcome":
            result = str((e.get("detail") or {}).get("result") or "")
            if result in out:
                out[result] += 1
    picks = out["hit"] + out["channel"] + out["miss"]
    out.update(picks=picks, in_three=out["hit"] + out["channel"],
               rate=round((out["hit"] + out["channel"]) / picks, 3) if picks else None, off=not enabled(store))
    return out


def settle(store: Any, *, now: float, log: LogFn | None = None) -> dict[str, int]:
    """No reads: marks the videos the user had on screen as watched, writes the outcomes of offers older than
    :data:`OUTCOME_S`, applies the live check (offers off under :data:`LIVE_MIN_RATE` after :data:`LIVE_MIN_PICKS`
    picks, noted once), and prunes old videos and the read log."""
    counts = {"watched": 0, "outcomes": 0}
    for key, at in store.page_entity_keys_seen(store.yt_unwatched_keys()).items():
        counts["watched"] += store.mark_yt_videos([key], "watched_at", at)
    events = store.taste_events(since=now - LIVE_DAYS * 86400.0, kinds=["offered", "outcome"])
    done = {int((e.get("detail") or {}).get("offer") or 0) for e in events if e["kind"] == "outcome"}
    for e in events:
        if e["kind"] != "offered" or int(e["id"]) in done or float(e["at"]) + OUTCOME_S > now:
            continue
        result = _outcome(store, e, now)
        if result is not None:
            store.add_taste_event("outcome", {"offer": int(e["id"]), "result": result}, at=float(e["at"]) + OUTCOME_S)
            counts["outcomes"] += 1
    bar = live_bar(store, now=now)
    if not bar.get("off") and int(bar.get("picks") or 0) >= LIVE_MIN_PICKS and (bar.get("rate") or 0.0) < LIVE_MIN_RATE:
        store.set_nudge_state(OFF_STATE, f"off:{int(now)}")
        store.add_taste_event("offers_off", {"picks": bar["picks"], "in_three": bar["in_three"]}, at=now)
        counts["switched_off"] = 1
        _log(log, "taste_offers_off", picks=bar["picks"], in_three=bar["in_three"])
    pruned = store.prune_yt(videos_before=now - KEEP_DAYS * 86400.0, reads_before=now - READS_KEEP_S)
    counts.update(pruned_videos=pruned.get("videos", 0))
    return counts


def nightly(store: Any, *, now: float | None = None, log: LogFn | None = None, network: bool = True) -> dict[str, int]:
    """YouTube's nightly part (``taste.seed``, after the recompute): with the offers on, reads allowed and nothing in
    front, the channel lookups then oEmbed for watched videos (the hour's cap shared); then :func:`settle`. Content-free
    counts; never raises (``{"error": 1}``)."""
    now = time.time() if now is None else float(now)
    out: dict[str, int] = {}
    try:
        if network and enabled(store) and blocked() is None:
            out.update(lookup_channels(store, now=now, log=log))
            out.update(attribute_watched(store, now=now, log=log, limit=NIGHT_READS - out.get("lookups", 0)))
        out.update(settle(store, now=now, log=log))
        out["reads_hour"] = int(store.yt_reads_since(now - 3600.0).get("reads", 0))
    except Exception as exc:  # noqa: BLE001
        _log(log, "yt_nightly", error=type(exc).__name__)
        out["error"] = 1
    return out


__all__ = ["SITE", "CHANNEL_KIND", "MAX_READS_PER_HOUR", "FEED_CHANNELS", "REQUEST_BUDGET_S", "FRESH_DAYS",
           "OFFER_N", "OFFER_KEEP_S", "OFF_STATE", "Offer", "Pending", "wants", "blocked", "enabled", "video_key",
           "channel_key", "parse_feed", "channel_id_of", "fetch_feed", "rank", "offer", "offer_line", "start",
           "offered", "pick", "attributed", "fresh_ages", "lookup_channels", "attribute_watched", "live_bar", "settle",
           "nightly"]
