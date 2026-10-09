"""Site URL templates and app labels, in one place: ``yuki/connect/sites/*.toml`` (PLAN.md D0, critique M10).

Every URL shape, URI template and app UI label Yuki relies on lives in a data file under ``sites/``, one per site or
app (``youtube.toml``, ``spotify.toml``). The moments extractor reads a page's entity from them (a YouTube video id, a
channel handle), the skills build their links from them, and the Spotify reader takes its labels from them. Nothing
here guesses meaning: a template is a fixed machine format (a documented URL or URI shape), parsed as such.

Template syntax (paths and queries of a URL):

* a path segment is a literal (``watch``), a whole-segment variable (``{id}``) or a literal prefix plus a variable
  (``@{handle}``);
* a query part ``v={id}`` reads that query parameter; parameters the template does not name are ignored;
* ``"/"`` alone is a site's root page.

API::

    site("youtube")                          # the parsed TOML (dict), or {} when there is none
    page_of("https://www.youtube.com/watch?v=abc")   # PageMatch(site="youtube", kind="watch", values={"id": "abc"})
    fill("youtube", "watch", id="abc")       # "https://www.youtube.com/watch?v=abc"
    match_template("https://x/playlist/{id}", url)   # {"id": ...} or None (any absolute template)
"""

from __future__ import annotations

import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

#: Where the site files live (bundled with the package: ``packaging/yuki.spec`` collects ``**/*.toml``).
SITES_DIR = Path(__file__).with_name("sites")

_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] = {}
#: The site names and the host -> site index, built once (page_of runs per timeline row in a backfill).
_names: list[str] | None = None
_hosts: dict[str, str] | None = None


def site(name: str) -> dict[str, Any]:
    """The parsed ``sites/<name>.toml`` (cached), or ``{}`` when there is no such file or it does not parse."""
    key = str(name or "").strip().lower()
    if not key or "/" in key or "\\" in key or key.startswith("."):
        return {}
    with _lock:
        if key in _cache:
            return _cache[key]
    path = SITES_DIR / f"{key}.toml"
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    with _lock:
        _cache[key] = data
    return data


def sites() -> list[str]:
    """The names of every site file (listed once, cached)."""
    global _names
    with _lock:
        if _names is not None:
            return list(_names)
    try:
        names = sorted(p.stem for p in SITES_DIR.glob("*.toml"))
    except OSError:
        names = []
    with _lock:
        _names = names
    return list(names)


def _host_index() -> dict[str, str]:
    """``{host: site name}`` from every site file's ``[site] hosts`` (the first file listing a host wins)."""
    global _hosts
    with _lock:
        if _hosts is not None:
            return _hosts
    index: dict[str, str] = {}
    for name in sites():
        for h in (site(name).get("site") or {}).get("hosts") or []:
            index.setdefault(str(h).lower(), name)
    with _lock:
        _hosts = index
    return index


def clear_cache() -> None:
    """Forget the parsed files, the site list and the host index (tests, or after a data change)."""
    global _names, _hosts
    with _lock:
        _cache.clear()
        _names = None
        _hosts = None


@dataclass(frozen=True)
class PageMatch:
    """A URL recognised by a site's page templates: which site, which kind of page, and its variables."""

    site: str
    kind: str
    values: dict[str, str] = field(default_factory=dict)

    @property
    def entity(self) -> str | None:
        """The page's one variable (a video id, a handle), or None for a page without one (a home page)."""
        return next(iter(self.values.values()), None) if self.values else None


def _segments(path: str) -> list[str]:
    return [s for s in (path or "").split("/") if s]


def _match_segment(template: str, segment: str) -> tuple[bool, str | None, str | None]:
    """``(matched, variable name, value)`` for one path segment against one template segment."""
    if "{" in template and template.endswith("}"):
        prefix, _, rest = template.partition("{")
        var = rest[:-1]
        if not var or not segment.startswith(prefix) or len(segment) <= len(prefix):
            return False, None, None
        return True, var, segment[len(prefix):]
    return template == segment, None, None


def _match_parts(template_path: str, template_query: str, path: str, query: str) -> dict[str, str] | None:
    """The variables of a URL's path and query against a template's, or None when they do not match."""
    want, got = _segments(template_path), _segments(path)
    if len(want) != len(got):
        return None
    values: dict[str, str] = {}
    for t, s in zip(want, got):
        ok, var, value = _match_segment(t, s)
        if not ok:
            return None
        if var is not None and value is not None:
            values[var] = value
    if template_query:
        params = parse_qs(query or "", keep_blank_values=False)
        for part in template_query.split("&"):
            key, _, tmpl = part.partition("=")
            got_values = params.get(key)
            if not got_values or not got_values[0]:
                return None
            if tmpl.startswith("{") and tmpl.endswith("}"):
                values[tmpl[1:-1]] = got_values[0]
            elif got_values[0] != tmpl:
                return None
    return values


def match_template(template: str, url: str | None) -> dict[str, str] | None:
    """The variables of ``url`` against an absolute ``template`` (scheme, host, path, query), or None.

    ``spotify:playlist:{id}``-style URIs (no ``//``) are matched part by part on ``:``.
    """
    if not url or not template:
        return None
    if "://" not in template:
        want, got = template.split(":"), str(url).split(":")
        if len(want) != len(got):
            return None
        values: dict[str, str] = {}
        for t, s in zip(want, got):
            ok, var, value = _match_segment(t, s)
            if not ok:
                return None
            if var is not None and value is not None:
                values[var] = value
        return values
    try:
        t, u = urlsplit(template), urlsplit(str(url))
    except ValueError:
        return None
    if (t.scheme or "").lower() != (u.scheme or "").lower() or (t.hostname or "") != (u.hostname or "").lower():
        return None
    return _match_parts(t.path, t.query, u.path, u.query)


def page_of(url: str | None) -> PageMatch | None:
    """The site page ``url`` is, by the first page template that matches it (``[pages]`` of each site file whose
    ``[site] hosts`` holds the URL's host), or None. A fixed-format parse: nothing about the page is guessed."""
    if not url:
        return None
    try:
        parts = urlsplit(str(url) if "//" in str(url) else "https://" + str(url))
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    name = _host_index().get(host) if host else None
    if name is None:
        return None
    for kind, template in (site(name).get("pages") or {}).items():
        path, _, query = str(template).partition("?")
        values = _match_parts(path, query, parts.path, parts.query)
        if values is not None:
            return PageMatch(site=name, kind=str(kind), values=values)
    return None


def fill(site_name: str, url_name: str, **values: str) -> str | None:
    """A site's ``[urls]`` (or ``[links]``) template with ``values`` put in (each URL-quoted), or None when the site
    or template is unknown or a value is missing."""
    data = site(site_name)
    template = (data.get("urls") or {}).get(url_name) or (data.get("links") or {}).get(url_name)
    if not template:
        return None
    out = str(template)
    for key, value in values.items():
        out = out.replace("{" + key + "}", quote(str(value), safe=""))
    return None if "{" in out else out


__all__ = ["PageMatch", "SITES_DIR", "clear_cache", "fill", "match_template", "page_of", "site", "sites"]
