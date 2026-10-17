"""YouTube's fresh uploads from the user's own channels, and which channel each watched video is (PLAN.md Phase T, D0,
D16; map-routines T1.4; audit 17 Oct decision 2).

**Zero setup (D0, D16):** no API key, no account, no sign-in. Every address is a template of the site file
``yuki/connect/sites/youtube.toml`` (``[urls]``: the handle page, the public feed, oEmbed, the watch page), and every
answer is a fixed machine format: the page's ``<link rel="canonical">`` tag, the Atom feed, oEmbed's JSON. Nothing
here guesses meaning.

**Which channels:** the user's own, from taste (kind :data:`CHANNEL_KIND`, the site's handle page kind:
:func:`yuki.skills.taste.site_kinds`): the handle pages they visit and the videos they watch, by hour of day, weighted
by how long they watched (:func:`watched`: the timeline's media seconds on each watch page; YouTube itself says a click
misleads). A watched video counts for its channel once its channel is known (:func:`attribute_local`, then the nightly
part). Never the portrait's interests (rule 3: no join of interests to handles by name).

**Which channel a watched video is** (kTaste, 17 Oct audit: 15 of 240 watched videos were tied to a channel):

* first from what Yuki already captured, no read: the watch page's own capture (``captures`` kind :data:`CAPTURE_KIND`,
  Yuki's extractor writes ``Video: <title>`` and ``By: <author>``, a fixed format of its own), taken only when its
  title stands in that visit's window title (identity: the same video), and only once the author is a channel Yuki
  knows by that exact name (a feed's or oEmbed's display name, or its handle);
* else one oEmbed read **per author** (not per video): the author's most-watched video is read, and when oEmbed names
  the same author, every video captured under that author joins that channel; the videos with no capture, one read
  each, most-watched first.

**Reads** (all counted in ``yt_reads``; never with a game in front; never when the user's privacy file blocks the
site): a request's feeds at most :data:`MAX_READS_PER_HOUR` in any hour on top of the nightly part's own reads; the
nightly part at most :data:`NIGHT_LOOKUPS` handle pages and :data:`NIGHT_OEMBED` oEmbed answers (small JSON) in any
hour, counting its own reads of the last hour (a pass run again after a restart takes no more):

* a channel's id: one read of its handle page, once (``yt_channels``; a failure that got an answer is retried at most
  :data:`LOOKUP_TRIES` times, a week apart; no answer at all, offline, counts no try): the nightly part, for the
  channels the user picks most;
* a watched video's channel: oEmbed (above), one read per video id ever (an answer, even a failed one; offline is
  tried again);
* a channel's fresh uploads: its public feed, **only on request** (:func:`offer`): when a run opens YouTube's home
  page (:func:`wants`), the top :data:`FEED_CHANNELS` channels at this hour whose feed is older than
  :data:`FEED_FRESH_S` are read in parallel within :data:`REQUEST_BUDGET_S` (``Settings.youtube_feed_budget_s``);
  older feeds serve otherwise. A feed counts as read only once its answer parsed (an error answer waits the same
  hour: a backoff); no answer or a cut-off body is read again on the next request.

**The offer:** videos published in the last :data:`FRESH_DAYS` (``Settings.youtube_fresh_days``) that the user never
had on screen (no ``page`` moment of that video: the moments' ``entity_key``, D23), ranked by ``taste score of the
channel at this hour x exp(-age / 7 days)``, one per channel, the top :data:`OFFER_N`. Ranking notes nothing: the
caller names them with a checked done line (:func:`offer_line`: "New from NDL, Deji and Sidemen: want one?"; the names
are the channels' own, from the user's data) and only then notes them (:func:`said`, :meth:`Pending.confirm`), so an
offer never named is never scored and never pickable. **The offer plays nothing:** after "open YouTube" a video opens
only when the user picks one ("the Deji one": :func:`pick`, through ``web.open_video``). When the user asks Yuki to
choose ("a video I like", "put something on": ``choose = true``, decision 2), Yuki's own pick plays instead
(:func:`yuki.skills.taste.choose`, which reads the same feeds through :func:`refresh`). Shorts are left out (their link
is not a watch page).

**Learning and the live check:** each offer named is a ``taste_events`` row; the nightly part writes its outcome: the
user's next YouTube pick within :data:`OUTCOME_S` was one of the three (``hit``), from one of the three channels
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
#: The nightly part: its handle-page reads and oEmbed reads in any hour at most (its own reads of the last hour count
#: against them; a request's feeds keep their own :data:`MAX_READS_PER_HOUR` on top), the channels it looks at (most
#: picked first), how far back it attributes watched videos, and each read's socket timeout. An oEmbed answer is a
#: small JSON (under 1 kB), one per author, so a few dozen a night catch up within days.
NIGHT_LOOKUPS = 6
NIGHT_OEMBED = 60
NIGHT_KINDS = ("page", "oembed")
NIGHT_CHANNELS = 24
ATTRIBUTE_DAYS = 60.0
NIGHT_TIMEOUT_S = 8.0
#: Yuki's own capture of a watch page (``yuki.memory.extract.page.extract_video``): its kind and the fixed starts of
#: its first two lines (Yuki's own format, read back by identity, never a guess at meaning).
CAPTURE_KIND = "video"
CAPTURE_TITLE = "Video: "
CAPTURE_AUTHOR = "By: "
#: A capture counts for a visit when it was taken this long before the visit's first row or after its last.
CAPTURE_BEFORE_S = 10.0
CAPTURE_AFTER_S = 120.0
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


def request_cap(store: Any, now: float | None = None) -> int:
    """The hourly cap a request's feed reads go under: :data:`MAX_READS_PER_HOUR` on top of the nightly part's own
    reads of the last hour (they never starve a request). Never raises."""
    now = time.time() if now is None else float(now)
    try:
        used = (store.yt_reads_since(now - 3600.0).get("kinds") or {})
        night = sum(int(used.get(k, 0) or 0) for k in NIGHT_KINDS)
    except Exception:  # noqa: BLE001
        night = 0
    return MAX_READS_PER_HOUR + night


def _read(store: Any, kind: str, url: str | None, *, max_bytes: int, timeout_s: float, deadline: float | None = None,
          stop: Callable[[bytes], bool] | None = None, cap: int = MAX_READS_PER_HOUR) -> tuple[int, bytes] | None:
    """One GET under the hourly cap ``cap`` (every kind counted, across processes): ``(HTTP status, body)`` (status 0:
    no answer), or None when the cap is reached or the address is not an https template (nothing read).
    ``stop(chunk)`` True ends the read early. Never raises."""
    if not url or not url.startswith("https://"):
        return None
    try:
        rid = store.yt_read_reserve(kind, cap=int(cap))
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
    (YouTube's documented format). A document with a DTD is refused (no entities are ever expanded). Never raises;
    ``(None, [])`` for a body that does not parse."""
    return _parse_feed(data) or (None, [])


def _parse_feed(data: bytes) -> tuple[str | None, list[dict[str, Any]]] | None:
    """:func:`parse_feed`, None when the body is no whole Atom feed (empty, a DTD, cut off, malformed)."""
    if not data or b"<!DOCTYPE" in data[:4096].upper() or b"<!ENTITY" in data.upper():
        return None
    try:
        root = ElementTree.fromstring(data)
    except (ElementTree.ParseError, ValueError):
        return None
    if root.tag != f"{ATOM}feed":
        return None
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
               deadline: float | None = None, cap: int = MAX_READS_PER_HOUR) -> dict[str, int]:
    """Read one channel's feed and keep its uploads (watch pages only: shorts left out), its display name and the
    cache row. The cache row marks the feed read only once its body parsed (tTaste r1 L2), or for an error answer (a
    404, a 429: it waits :data:`FEED_FRESH_S` like a read one, a backoff under the hourly cap); no answer (offline, a
    timeout) or a body cut off by the deadline leaves it stale, so the next request reads it again. Content-free
    counts: ``ok`` / ``failed`` / ``capped``, ``bytes``, ``videos``."""
    got = _read(store, "feed", _url(FEED_URL, id=channel_id), max_bytes=FEED_MAX_BYTES, timeout_s=timeout_s,
                deadline=deadline, cap=cap)
    if got is None:
        return {"capped": 1}
    status, body = got

    def cached() -> None:
        try:
            store.save_yt_feed_cache(key, at=now, status=status, nbytes=len(body))
        except Exception:  # noqa: BLE001
            pass

    if status != 200:
        if status:
            cached()
        return {"failed": 1, "bytes": len(body)}
    parsed = _parse_feed(body)
    if parsed is None:
        return {"failed": 1, "bytes": len(body)}
    name, entries = parsed
    rows = [{"video_key": video_key(store, e["video_id"]), "channel_key": key, "video_id": e["video_id"],
             "title": e["title"] or None, "published_at": e["published_at"], "source": "feed"}
            for e in entries if not e["link"] or _is_watch(e["link"])]
    try:
        store.save_yt_videos(rows, at=now)
        if name:
            store.save_yt_channel(key, name=name, at=now)
    except Exception:  # noqa: BLE001
        return {"failed": 1, "bytes": len(body)}
    cached()
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
    cap = request_cap(store, now)

    def one(key: str, cid: str) -> None:
        got = fetch_feed(store, key, cid, now=now, timeout_s=budget_s, deadline=deadline + 0.5, cap=cap)
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


def rank(store: Any, picks: Sequence[Any], known: Mapping[str, Mapping[str, Any]], *, now: float,
         fresh_days: float = FRESH_DAYS) -> list[Offer]:
    """The top :data:`OFFER_N` uploads of ``picks`` (taste rows of :data:`CHANNEL_KIND` with their score at this hour)
    published in the last ``fresh_days`` and never shown, one per channel, by ``score x exp(-age / AGE_DAYS)``; names
    told apart (a second channel of the same name is left out)."""
    from yuki.connect.templates import fill

    score = {t.entity_key: float(t.score) for t in picks}
    if not score:
        return []
    vids = store.yt_videos(channel_keys=list(score), published_since=now - float(fresh_days) * 86400.0,
                           unwatched=True)
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


def refresh(store: Any, keys: Sequence[str], *, now: float, budget_s: float = REQUEST_BUDGET_S,
            log: LogFn | None = None) -> dict[str, int]:
    """The feeds of the channels ``keys`` (taste rows of :data:`CHANNEL_KIND`) with a known id whose feed is older than
    :data:`FEED_FRESH_S`, read in parallel within ``budget_s`` under the request's cap (:func:`request_cap`); a late
    one finishes on its own thread and serves the next request. Content-free counts; never raises."""
    try:
        known = store.yt_channels(keys)
        cache = store.yt_feed_cache(keys)
    except Exception:  # noqa: BLE001
        return {"channels": 0, "ok": 0, "failed": 0, "capped": 0, "late": 0, "bytes": 0}
    stale = [(k, str(known[k]["channel_id"])) for k in keys
             if (known.get(k) or {}).get("channel_id") and now - cache.get(k, (0.0, 0))[0] >= FEED_FRESH_S]
    return _fetch_all(store, stale, now=now, budget_s=float(budget_s), log=log)


def offer(store: Any, *, now: float | None = None, game: str | None = None, budget_s: float = REQUEST_BUDGET_S,
          fresh_days: float = FRESH_DAYS, log: LogFn | None = None) -> list[Offer]:
    """On request (a run opening YouTube's home): read the stale feeds of the top :data:`FEED_CHANNELS` channels at
    this hour within ``budget_s`` (the hourly cap holds), then rank the top :data:`OFFER_N` uploads of the last
    ``fresh_days``. Ranks only: nothing is noted until the caller names them (:func:`said`; tTaste r1 M2), and nothing
    is opened or played. ``[]`` when the offers are off, a game is in front, the site is blocked, or nothing is fresh.
    Never raises."""
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
        fetched = refresh(store, keys, now=now, budget_s=float(budget_s) - (time.perf_counter() - started), log=log)
        known = store.yt_channels(keys)   # display names a feed just gave
        offers = rank(store, picks, known, now=now, fresh_days=fresh_days)
        _log(log, "taste_offer", n=len(offers), channels=len(picks), fetched=fetched["ok"], with_id=sum(
            1 for k in keys if (known.get(k) or {}).get("channel_id")), ms=round((time.perf_counter() - started)
                                                                                    * 1000.0, 1))
        return offers
    except Exception as exc:  # noqa: BLE001 - an offer never breaks the request
        _log(log, "taste_offer", error=type(exc).__name__)
        return []


def said(store: Any, offers: Sequence[Offer], *, at: float | None = None, log: LogFn | None = None) -> bool:
    """The offers were named aloud (with a checked done line): their videos are marked offered and the ``offered``
    taste event is noted, from ``at`` (the time they were said). From then on they are what "the X one" matches for
    :data:`OFFER_KEEP_S` (:func:`offered`) and what the nightly outcome scores. Call it only once the line will be
    said; an offer never said is never noted. False (nothing to note, or the write failed: then don't name them, they
    could not be picked). Never raises."""
    got = [o for o in offers or [] if o.key]
    if not got:
        return False
    at = time.time() if at is None else float(at)
    try:
        store.mark_yt_videos([o.key for o in got], "offered_at", at)
        store.add_taste_event("offered", {"videos": [o.key for o in got], "channels": [o.channel_key for o in got],
                                          "ages_d": [o.age_days for o in got]}, at=at)
    except Exception as exc:  # noqa: BLE001
        _log(log, "taste_offered", error=type(exc).__name__)
        return False
    _log(log, "taste_offered", n=len(got))
    return True


def offer_line(offers: Sequence[Offer]) -> str:
    """The question said with the done line: "New from NDL, Deji and Sidemen: want one?" (the channels' own names,
    from the user's data). ``""`` with no offer."""
    names = [" ".join(o.name.split()) for o in offers if o.name.strip()][:OFFER_N]
    if not names:
        return ""
    said = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return f"New from {said}: want {'it' if len(names) == 1 else 'one'}?"


class Pending:
    """An offer being ranked on its own short thread while the page opens. :meth:`wait` gives the offers (nothing
    noted); :meth:`confirm` notes them once the caller names them."""

    def __init__(self, memory: Any = None, *, log: LogFn | None = None, budget_s: float = REQUEST_BUDGET_S) -> None:
        self._done = threading.Event()
        self._memory = memory
        self._log = log
        self._confirmed = False
        #: How long the ranking may read feeds (``Settings.youtube_feed_budget_s``): what the caller waits at most.
        self.budget_s = float(budget_s)
        self.offers: list[Offer] = []

    def wait(self, timeout_s: float | None = None) -> list[Offer]:
        """The offers when they are ready within ``timeout_s`` (default :attr:`budget_s`), else ``[]`` (a late ranking
        is never named, so it is never noted nor pickable)."""
        if self._done.wait(max(0.0, float(self.budget_s if timeout_s is None else timeout_s))):
            return list(self.offers)
        return []

    def confirm(self, offers: Sequence[Offer] | None = None, *, at: float | None = None) -> bool:
        """Note the offers as named (:func:`said`), once: call it after the run's checked done and a non-empty
        :meth:`wait`, right before the line is said. False when there is nothing to note, it was noted already, or
        the write failed (then don't name them). Never raises."""
        got = list(self.offers if offers is None else offers)
        if self._confirmed or not got:
            return False
        self._confirmed = True
        try:
            return bool(_with_store(self._memory, lambda store: said(store, got, at=at, log=self._log),
                                    timeout_s=1.0))
        except Exception:  # noqa: BLE001
            return False


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


def _setting(settings: Any, name: str, default: float) -> float:
    """A phase T knob from ``Settings`` (``youtube_feed_budget_s``, ``youtube_fresh_days``), else the module's
    default; a value that is not a positive number keeps the default."""
    try:
        from yuki.skills import setting

        value = float(setting(settings, name) or 0.0)
    except (TypeError, ValueError, ImportError):
        return float(default)
    return value if value > 0 else float(default)


def start(memory: Any, values: Mapping[str, Any] | None, *, game: str | None = None, log: LogFn | None = None,
          settings: Any = None, budget_s: float | None = None) -> Pending | None:
    """On request: when a run's values open YouTube's home (:func:`wants`) and no game is in front, rank the offer on a
    short thread of its own (it reads the feeds while the page opens); None otherwise. The feed budget and the fresh
    window come from ``settings`` (``youtube_feed_budget_s``, ``youtube_fresh_days``; else :data:`REQUEST_BUDGET_S`,
    :data:`FRESH_DAYS`). The caller waits on the returned :class:`Pending` after the run's done-check, names the offers
    only with a checked done, and notes them then (:meth:`Pending.confirm`)."""
    if not wants(values) or game:
        return None
    budget = float(budget_s) if budget_s is not None else _setting(settings, "youtube_feed_budget_s",
                                                                   REQUEST_BUDGET_S)
    fresh_days = _setting(settings, "youtube_fresh_days", FRESH_DAYS)
    pending = Pending(memory, log=log, budget_s=budget)

    def work() -> None:
        try:
            got = _with_store(memory, lambda store: offer(store, game=game, budget_s=budget, fresh_days=fresh_days,
                                                          log=log), timeout_s=budget + 1.0)
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
# What the user watched: per video, how long, and whose (for taste's recompute and its chooser)
# ---------------------------------------------------------------------------------------------------------------------

#: Watch time counts in units of this many seconds of the page's media playing (five minutes watched = one unit), at
#: most :data:`UNITS_CAP` per visit; a visit that played nothing is still a click (:data:`CLICK_UNITS`).
UNIT_S = 300.0
UNITS_CAP = 6.0
CLICK_UNITS = 0.1


def _template(row: Mapping[str, Any]) -> str:
    return str((row.get("facts") or {}).get("template") or "")


def units(secs: float) -> float:
    """Watch seconds as taste units (:data:`UNIT_S`), between :data:`CLICK_UNITS` and :data:`UNITS_CAP`."""
    return min(UNITS_CAP, max(CLICK_UNITS, float(secs or 0.0) / UNIT_S))


def watched(store: Any, *, since: float, now: float) -> dict[str, dict[str, Any]]:
    """``{video_key: {"id", "secs", "spans", "first", "last"}}``: every watch page of the site that was in front in
    ``[since, now)``, from the timeline's own rows (their path read by the site's templates, a fixed format), with the
    seconds its media played (``media_s``) and each row's ``(start, end, window title)`` (RAM only; titles never
    logged). Never raises (``{}``)."""
    try:
        from yuki.connect.templates import page_of, site

        hosts = [str(h) for h in (site(SITE).get("site") or {}).get("hosts") or []]
        if not hosts:
            return {}
        rows = store._query(
            f"SELECT started_at, ended_at, host, title_ciphertext, path_ciphertext, media_s FROM timeline WHERE host IN "
            f"({','.join('?' * len(hosts))}) AND started_at >= ? AND started_at < ? AND path_ciphertext IS NOT NULL "
            f"ORDER BY started_at", (*hosts, float(since), float(now)))
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict[str, Any]] = {}
    for started, ended, host, title_ct, path_ct, media_s in rows:
        try:
            path = str(store._dec(path_ct) or "")
            m = page_of(f"https://{host}{path}") if path.startswith("/") else None
            if m is None or m.site != SITE or m.kind != WATCH_PAGE or not _plain(str(m.entity or "")):
                continue
            title = " ".join(str(store._dec(title_ct) or "").split()) if title_ct else ""
        except Exception:  # noqa: BLE001 - a row the key can't read
            continue
        vid = str(m.entity)
        key = video_key(store, vid)
        e = out.setdefault(key, {"id": vid, "secs": 0.0, "spans": [], "first": float(started), "last": 0.0})
        e["secs"] += float(media_s or 0.0)
        e["spans"].append((float(started), float(ended or started), title))
        e["last"] = max(e["last"], float(started))
    return out


def _captures(store: Any, since: float, now: float) -> list[tuple[float, str, str]]:
    """``[(at, title, author)]`` of Yuki's own watch-page captures (:data:`CAPTURE_KIND`: its ``Video:`` and ``By:``
    lines), oldest first, decrypted in RAM. Never raises (``[]``)."""
    try:
        rows = store._query("SELECT at, delta_ciphertext FROM captures WHERE kind = ? AND at >= ? AND at < ? "
                            "ORDER BY at", (CAPTURE_KIND, float(since), float(now)))
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[float, str, str]] = []
    for at, text_ct in rows:
        try:
            lines = str(store._dec(text_ct) or "").split("\n")
        except Exception:  # noqa: BLE001
            continue
        if len(lines) < 2 or not lines[0].startswith(CAPTURE_TITLE) or not lines[1].startswith(CAPTURE_AUTHOR):
            continue
        title = " ".join(lines[0][len(CAPTURE_TITLE):].split())
        author = " ".join(lines[1][len(CAPTURE_AUTHOR):].split())
        if title and author:
            out.append((float(at), title, author))
    return out


def _author_of(spans: Sequence[tuple[float, float, str]], caps: Sequence[tuple[float, str, str]],
               starts: Sequence[float]) -> tuple[str, str] | None:
    """``(author, the video's title)`` of a watched video from the captures taken during its rows whose title stands
    in that row's window title (identity: the same video, not a sidebar's); the author most of them name. None when
    no capture holds it."""
    import bisect

    votes: dict[str, list[Any]] = {}
    for start, end, window in spans:
        win = _fold(window)
        if not win:
            continue
        i = bisect.bisect_left(starts, start - CAPTURE_BEFORE_S)
        while i < len(caps) and caps[i][0] <= end + CAPTURE_AFTER_S:
            _at, title, author = caps[i]
            if _fold(title) in win:
                got = votes.setdefault(_fold(author), [0, author, title])
                got[0] += 1
            i += 1
    if not votes:
        return None
    best = max(votes.values(), key=lambda v: v[0])
    return str(best[1]), str(best[2])


def _channel_names(store: Any) -> dict[str, str | None]:
    """``{folded display name or handle: channel_key}`` of the channels Yuki knows (a feed's or oEmbed's name, the
    handle); a name two channels share maps to None (never guessed between them)."""
    out: dict[str, str | None] = {}
    for key, info in (store.yt_channels() or {}).items():
        for n in {_fold(info.get("name")), _fold(info.get("handle"))} - {""}:
            out[n] = key if out.get(n, key) == key else None
    return out


def attribute_local(store: Any, *, now: float, days: float = ATTRIBUTE_DAYS,
                    seen: Mapping[str, Mapping[str, Any]] | None = None
                    ) -> tuple[dict[str, int], dict[str, dict[str, Any]], list[tuple[str, str, float]]]:
    """No reads: the watched videos of the last ``days`` with no channel yet, tied to a channel through Yuki's own
    capture of the page (:func:`_author_of`) when that author is a channel Yuki knows by that exact name
    (:func:`_channel_names`). Returns content-free counts, the authors still unknown (``{folded author: {"author",
    "title", "videos": [(video_key, id, secs)]}}``, RAM only) and the videos with no capture (``[(video_key, id,
    secs)]``), both for the nightly oEmbed reads (:func:`attribute_watched`)."""
    since = now - float(days) * 86400.0
    seen = watched(store, since=since, now=now) if seen is None else seen
    counts = {"watched": len(seen), "known": 0, "local": 0, "pending_authors": 0, "no_capture": 0}
    if not seen:
        return counts, {}, []
    have = {str(v["video_key"]): v for v in store.yt_videos(video_keys=list(seen))}
    counts["known"] = sum(1 for k in seen if (have.get(k) or {}).get("channel_key"))
    todo = {k: e for k, e in seen.items() if k not in have}
    caps = _captures(store, since - CAPTURE_AFTER_S, now + 1.0)
    starts = [c[0] for c in caps]
    names = _channel_names(store) if todo else {}
    rows: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    bare: list[tuple[str, str, float]] = []
    for key, e in todo.items():
        got = _author_of(e["spans"], caps, starts)
        if got is None:
            bare.append((key, e["id"], float(e["secs"])))
            continue
        author, title = got
        ch = names.get(_fold(author))
        if ch:
            rows.append({"video_key": key, "channel_key": ch, "video_id": e["id"], "title": title, "source": "page"})
            continue
        p = pending.setdefault(_fold(author), {"author": author, "videos": [], "secs": 0.0})
        p["videos"].append((key, e["id"], float(e["secs"])))
        p["secs"] += float(e["secs"])
    if rows:
        store.save_yt_videos(rows, at=now)
        for r in rows:
            store.mark_yt_videos([r["video_key"]], "watched_at", float(seen[r["video_key"]]["first"]))
    counts.update(local=len(rows), pending_authors=len(pending), no_capture=len(bare))
    return counts, pending, bare


def attributed(store: Any, page_rows: Iterable[Mapping[str, Any]],
               users: Callable[[Mapping[str, Any]], bool], *, now: float | None = None
               ) -> list[tuple[float, str, str, str, float]]:
    """The user's watched videos whose channel is known, as channel picks for taste: ``(at, handle, the handle page's
    link, group, units)`` per visit (group: the moments' ``entity_key`` of the handle page, so a channel page and one of
    its videos minutes apart are one pick; units: the video's watch seconds (:func:`watched`) shared over its visits by
    their dwell, as :func:`units`). ``users(row)``: the pick is the user's."""
    from yuki.connect.templates import fill

    watch = f"{SITE}.{WATCH_PAGE}"
    rows = [r for r in page_rows if _template(r) == watch and r.get("entity_key") and users(r)]
    if not rows:
        return []
    vids = {str(v["video_key"]): str(v["channel_key"]) for v in store.yt_videos(
        video_keys={str(r["entity_key"]) for r in rows}) if v.get("channel_key")}
    chans = store.yt_channels(set(vids.values()))
    end = time.time() if now is None else float(now)
    seen = watched(store, since=min(float(r["at"]) for r in rows) - 3600.0, now=end + 1.0)
    dwell: dict[str, float] = {}
    for r in rows:
        dwell[str(r["entity_key"])] = dwell.get(str(r["entity_key"]), 0.0) + max(0.0, float(r.get("value") or 0.0))
    out: list[tuple[float, str, str, str, float]] = []
    for r in rows:
        key = str(r["entity_key"])
        handle = (chans.get(vids.get(key, "")) or {}).get("handle")
        if not handle:
            continue
        secs = float((seen.get(key) or {}).get("secs") or 0.0)
        share = max(0.0, float(r.get("value") or 0.0)) / dwell[key] if dwell.get(key) else 1.0
        out.append((float(r["at"]), str(handle), fill(SITE, HANDLE_PAGE, handle=str(handle)) or "",
                    _handle_group(store, str(handle)), units(secs * share)))
    return out


#: :func:`by_channel` is kept in RAM this long per store and hour (the chooser asks it on every judged pick).
BY_CHANNEL_TTL_S = 600.0
_by_channel_lock = threading.Lock()
_by_channel_cache: dict[tuple[int, int, int], tuple[float, dict[str, dict[str, Any]]]] = {}


def by_channel(store: Any, *, since: float, now: float) -> dict[str, dict[str, Any]]:
    """``{channel_key: {"secs", "videos", "titles"}}``: what the user watched of each channel in ``[since, now)``
    (watch seconds, how many videos, the titles they watched longest first: RAM only, for the chooser's judgement),
    kept :data:`BY_CHANNEL_TTL_S`. Never raises (``{}``)."""
    key = (id(store), int(since // 3600), int(now // 3600))
    with _by_channel_lock:
        got = _by_channel_cache.get(key)
        if got is not None and time.monotonic() - got[0] < BY_CHANNEL_TTL_S:
            return got[1]
    out = _by_channel(store, since=since, now=now)
    with _by_channel_lock:
        _by_channel_cache.clear()
        _by_channel_cache[key] = (time.monotonic(), out)
    return out


def _by_channel(store: Any, *, since: float, now: float) -> dict[str, dict[str, Any]]:
    try:
        seen = watched(store, since=since, now=now)
        vids = {str(v["video_key"]): v for v in store.yt_videos(video_keys=list(seen)) if v.get("channel_key")}
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, e in sorted(seen.items(), key=lambda kv: -float(kv[1]["secs"])):
        v = vids.get(key)
        if v is None:
            continue
        c = out.setdefault(str(v["channel_key"]), {"secs": 0.0, "videos": 0, "titles": []})
        c["secs"] += float(e["secs"])
        c["videos"] += 1
        if v.get("title") and len(c["titles"]) < 3:
            c["titles"].append(" ".join(str(v["title"]).split()))
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


def lookup_channels(store: Any, *, now: float, log: LogFn | None = None, limit: int = NIGHT_LOOKUPS,
                    cap: int = MAX_READS_PER_HOUR) -> dict[str, int]:
    """One read of the handle page of each channel the user picks most (:data:`NIGHT_CHANNELS`, most picked first)
    that has no id yet: its canonical link's channel id, kept for good. At most ``limit`` reads under the hourly
    ``cap``; a failure that got an answer is retried a week later, at most :data:`LOOKUP_TRIES` times; no answer at all
    (offline, a timeout) counts no try and ends the pass (the next one tries again; tTaste r1 L4)."""
    from yuki.skills import taste

    rows = taste.top(store, CHANNEL_KIND, now=now, n=10_000)
    rows.sort(key=lambda t: (-t.n_obs, t.entity_key))
    rows = [t for t in rows if t.name][:NIGHT_CHANNELS]
    known = store.yt_channels([t.entity_key for t in rows])
    counts = {"lookups": 0, "resolved": 0, "lookup_failed": 0, "lookup_offline": 0}
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
                    timeout_s=NIGHT_TIMEOUT_S, stop=parser.take, cap=cap)
        if got is None:
            break   # the hour's reads are used up
        counts["lookups"] += 1
        status, _body = got
        if not status:
            counts["lookup_offline"] += 1
            break   # no answer: no try counted, the rest would fare the same
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


def _oembed(store: Any, vid: str, *, cap: int) -> tuple[int, str | None, str | None, str | None] | None:
    """One oEmbed read of a video: ``(status, handle, author name, title)`` (status 0: no answer), or None when the
    hour's reads are used up or the id is no plain id."""
    watch_url = _url(WATCH_PAGE, id=vid) if _plain(vid) else None
    if not watch_url:
        return 404, None, None, None
    got = _read(store, "oembed", _url(OEMBED_URL, url=watch_url), max_bytes=OEMBED_MAX_BYTES,
                timeout_s=NIGHT_TIMEOUT_S, cap=cap)
    if got is None:
        return None
    status, body = got
    handle = name = title = None
    if status == 200:
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
            m = _page(data.get("author_url"))
            if m is not None and m.kind == HANDLE_PAGE and m.entity:
                handle = str(m.entity)
                name = " ".join(str(data.get("author_name") or "").split()) or None
                title = " ".join(str(data.get("title") or "").split()) or None
        except (ValueError, AttributeError):
            handle = None
    return status, handle, name, title


def attribute_watched(store: Any, *, now: float, log: LogFn | None = None, limit: int = NIGHT_OEMBED,
                      cap: int = MAX_READS_PER_HOUR, network: bool = True) -> dict[str, int]:
    """The watched videos of the last :data:`ATTRIBUTE_DAYS` with no channel yet: first by Yuki's own captures, no
    read (:func:`attribute_local`); then, with ``network``, oEmbed reads (as many as ``limit`` and the hour's ``cap``
    allow): one per author still unknown (their most-watched video; when oEmbed names the same author, every video
    captured under that author joins that channel), then the videos with no capture, most-watched first. A video with
    an answer, even a failed one, is never read again; no answer (offline, a timeout) keeps nothing and ends the pass,
    so the next one reads it."""
    counts: dict[str, int] = {"oembed": 0, "attributed": 0, "oembed_failed": 0, "oembed_offline": 0}
    try:
        local, pending, bare = attribute_local(store, now=now)
    except Exception as exc:  # noqa: BLE001
        _log(log, "yt_oembed", error=type(exc).__name__)
        return counts
    counts.update({k: int(v) for k, v in local.items()})
    queue: list[tuple[str, str, float, dict[str, Any] | None]] = []
    for p in sorted(pending.values(), key=lambda p: -float(p["secs"])):
        key, vid, secs = max(p["videos"], key=lambda v: v[2])
        queue.append((key, vid, secs, p))
    queue += [(k, v, s, None) for k, v, s in sorted(bare, key=lambda b: -b[2])]
    for key, vid, _secs, group in queue if network else []:
        if counts["oembed"] >= limit or blocked() is not None:
            break
        got = _oembed(store, vid, cap=cap)
        if got is None:
            break   # the hour's reads are used up
        status, handle, name, title = got
        counts["oembed"] += 1
        if not status:
            counts["oembed_offline"] += 1
            break   # no answer: nothing kept, the next pass reads it again
        ch = channel_key(store, handle) if handle else None
        rows = [{"video_key": key, "channel_key": ch, "video_id": vid, "title": title, "source": "oembed"}]
        if ch:
            store.save_yt_channel(ch, handle=handle, name=name, at=now)
            counts["attributed"] += 1
            if group is not None and name and _fold(name) == _fold(group["author"]):
                # the same author by identity: every video captured under that name is this channel's
                rows += [{"video_key": k, "channel_key": ch, "video_id": v, "source": "page"}
                         for k, v, _s in group["videos"] if k != key]
                counts["attributed"] += len(rows) - 1
        else:
            counts["oembed_failed"] += 1
        # one row either way: a video id is read once, ever (a failed one keeps no channel)
        store.save_yt_videos(rows, at=now)     # watched_at: settle() marks it from the page moments
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
    """YouTube's nightly part (``taste.seed``, after the recompute): the watched videos' channels from Yuki's own
    captures (no read), then, with reads allowed and nothing in front, the channel lookups (at most
    :data:`NIGHT_LOOKUPS`) and oEmbed for watched videos (at most :data:`NIGHT_OEMBED`), each less the reads of its own
    kind the last hour already made (a pass run again after a restart takes no more; a request keeps its own cap on
    top, :func:`request_cap`); then :func:`settle`. The offers' live switch (:data:`OFF_STATE`) no longer stops the
    attribution: the chooser needs it. Content-free counts; never raises (``{"error": 1}``)."""
    now = time.time() if now is None else float(now)
    out: dict[str, int] = {}
    try:
        reads = network and blocked() is None
        used = (store.yt_reads_since(now - 3600.0).get("kinds") or {}) if reads else {}
        cap = MAX_READS_PER_HOUR + NIGHT_LOOKUPS + NIGHT_OEMBED
        pages = NIGHT_LOOKUPS - int(used.get("page", 0) or 0)
        if reads and pages > 0:
            out.update(lookup_channels(store, now=now, log=log, limit=pages, cap=cap))
        oembed = NIGHT_OEMBED - int(used.get("oembed", 0) or 0)
        out.update(attribute_watched(store, now=now, log=log, limit=max(0, oembed), cap=cap,
                                     network=bool(reads and oembed > 0 and not out.get("lookup_offline"))))
        out.update(settle(store, now=now, log=log))
        out["reads_hour"] = int(store.yt_reads_since(now - 3600.0).get("reads", 0))
    except Exception as exc:  # noqa: BLE001
        _log(log, "yt_nightly", error=type(exc).__name__)
        out["error"] = 1
    return out


__all__ = ["SITE", "CHANNEL_KIND", "MAX_READS_PER_HOUR", "FEED_CHANNELS", "REQUEST_BUDGET_S", "FRESH_DAYS",
           "OFFER_N", "OFFER_KEEP_S", "OFF_STATE", "NIGHT_LOOKUPS", "NIGHT_OEMBED", "NIGHT_KINDS", "UNIT_S", "Offer",
           "Pending", "wants", "blocked", "enabled", "video_key", "channel_key", "parse_feed", "channel_id_of",
           "fetch_feed", "request_cap", "refresh", "rank", "offer", "said", "offer_line", "start", "offered", "pick",
           "units", "watched", "attribute_local", "attributed", "by_channel", "fresh_ages", "lookup_channels",
           "attribute_watched", "live_bar", "settle", "nightly"]
