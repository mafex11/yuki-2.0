"""Spotify's desktop window, read through UI Automation: no account, no key, no token (PLAN.md D0, D4).

Spotify allows a Web API app at most 5 users, so no API route can serve people who install Yuki. Spotify's own window
shows Windows everything needed instead (evidence: ``docs/research/audit-2026-10-09/round4/spotify-desktop-route.md``
§2): the volume slider (Slider "Change volume", RangeValue 0-1), the player controls, and each playlist row of Your
Library with its own "Play {name}" button, which reads "Pause {name}" on the row that is playing. Everything here is
**read-only**: no Invoke, no SetValue, no focus, no key, no pointer, no window restore (phase S adds the actions, in
``yuki/skills/spotify_route.py``, on top of these reads).

**The reader** (used by the memory side here and by the skills later):

* :func:`window` - Spotify's top window, found by its process (any state, minimised included);
* :func:`window_state` - ``front`` / ``covered`` / ``visible`` (on screen, not in front: another monitor, partly
  covered) / ``minimised``, from window geometry (the visible share :mod:`yuki.perception.visibility` measures);
* :func:`warm` - a Chromium (CEF) window answers with its frame only (7 elements for ~3 s after a launch) until its
  child windows are asked (``perception/tree.py`` "Lazily built trees"): ask each visible child window, then wait until
  the element count stops growing, at most ``budget_s`` (3 s);
* :func:`read` (and :func:`library`, :func:`links`, :func:`slider`) - one bounded read (0.3 s on a warm window): the
  slider, the Library rows (name, owner kind, which one plays), and the playlist links whose name equals a Library
  row's name (``spotify:playlist:{id}``). A read that finds only the frame (no player controls) is ``not_built``,
  never "no Library"; "no Library" (``no_library``) needs the player controls present and no grid (the full-screen
  view). A drawn tree (at least :data:`DRAWN_MIN_ELEMENTS`) with none of the controls the reader knows is
  ``unrecognised`` (renamed labels after an update, or another language without a single 0-1 slider).

**Containers** (PLAN.md M5): every label is looked up inside its own container, a path of (role, name) steps in
``sites/spotify.toml``: the player controls under Group "Now playing bar" > Group "Player controls", the rows under
DataGrid "Your Library" > DataItem > DataItem > Group "{name}". Spotify's Home page has 8 more Buttons named "Play" and
"Pause {name}" buttons outside the grid: never matched. Every search is ``FindAll`` by control type and name from the
window root (never ``ElementFromPoint``: a CEF window answers with its whole renderer pane, ``slider.py:29-31``) and
never filtered on ``IsOffscreen`` (a covered or minimised window reports its elements off-screen). A container's whole
subtree is fetched in one cache request, then walked locally.

**Labels without English** (PLAN.md L6): if the English labels miss, the volume slider is the window's only Slider with
a 0-1 range (two or more: none), and the row button wording comes from a read where Spotify's media session says
playing and exactly one row's button reads differently from the rest (:func:`row_wording_candidate`). That is only a
candidate, held in RAM with no play state: the open button of the one row by another owner (or the one pinned row)
fits the same shape when the playing item is not a Library row in view. It is learned once a later read sees a row's
button flip between the two wordings (:func:`judge_wording`: a play button flips as music starts and stops, an owner
never does), and dropped when two rows carry the "pause" wording at once or the data's wording shows up. Learned
wording is kept by code, encrypted (``nudge_state['spotify_labels']``), never hand-written; until then rows carry no
play state.

**Media session** (PLAN.md L7): Spotify's session is the one whose app id belongs to the Spotify window's process (the
Store build's ``SpotifyAB.SpotifyMusic_…!Spotify``, the classic installer's ``Spotify.exe``; both forms are data).

**The memory side** (:class:`SpotifyWatch`): a warm-up when Spotify's window first appears (on the Now board's layout,
or in front) and when its media session first plays, never on a request path and never with a game in front; when
Spotify's music starts, one read of the Library for the playing row (a ``media_context`` moment, the name encrypted by
the moments writer, a Spotify-owned list digest-only) and of the playlist links on screen (kept encrypted); when Spotify
leaves the front, one slider read (a ``volume_change`` moment, means ``in_app``, against the last reading, which the
play-start read also takes; ``facts.prev_read_at`` says since when); and a daily counts-only state probe: once a day
per state (covered, visible, minimised), after 60 s in that state, one warm + slider + Library read, to answer "can
Yuki see Spotify in that state?" without the user doing anything (``[spotify] state_probe``). Its one-shot timer is
(re)started by events only: Spotify leaving the front, its place on the Now board's layout changing, or a play start
of a Spotify that is not in front. A warm-up stops as soon as a game comes to the front (checked before every poll).
Its triggers are the moments hub's subscriptions (``MomentHub.subscribe_media`` / ``subscribe_front``, called with the
hub's keyword facts) and the Now board's layout; :func:`hub_emit` writes its moments through the hub's
``media_context`` / ``volume_change``.

Logs are content-free (``spotify_read``, ``spotify_probe``): the trigger, the window state, cold or warm, counts, the
label source, ms. Never a name, a link, an id or a title.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from yuki.connect.templates import match_template, site

# UIA ids (UIAutomationClient.h); the same values as yuki.perception.tree's.
_P_CONTROL_TYPE = 30003
_P_NAME = 30005
_P_VALUE_VALUE = 30045
_P_IS_RANGE_VALUE_AVAILABLE = 30033
_P_RANGE_VALUE_VALUE = 30047
_P_RANGE_VALUE_MINIMUM = 30049
_P_RANGE_VALUE_MAXIMUM = 30050
_TREE_SCOPE_ELEMENT = 1
_TREE_SCOPE_CHILDREN = 2
_TREE_SCOPE_DESCENDANTS = 4
_TREE_SCOPE_SUBTREE = 7
_READ_PROPS = (_P_CONTROL_TYPE, _P_NAME, _P_VALUE_VALUE, _P_IS_RANGE_VALUE_AVAILABLE, _P_RANGE_VALUE_VALUE,
               _P_RANGE_VALUE_MINIMUM, _P_RANGE_VALUE_MAXIMUM)
_CUIAUTOMATION8 = "{e22ad333-b25f-460c-83d0-0581107395c9}"
_CUIAUTOMATION = "{ff48dba4-60ef-4201-aa87-54103eef594e}"

#: Budgets (PLAN.md C.5): a read of a warm window, a warm-up, the poll step while warming.
READ_BUDGET_S = 0.3
WARM_BUDGET_S = 3.0
WARM_STEP_S = 0.15
#: A warm-up ends once the tree is built and its element count stayed the same for this many polls.
WARM_STABLE_POLLS = 2
#: A day's state probe waits this long in one state before reading (PLAN.md M4).
PROBE_AFTER_S = 60.0
#: How many times one stretch away from the front re-checks the window state for the probe (one minute apart).
PROBE_CHECKS = 10
#: The states the daily probe covers.
PROBE_STATES = ("covered", "visible", "minimised")
#: Row names remembered for link learning (in RAM only).
KNOWN_NAMES_MAX = 500
#: A media_context of the same playlist again within this long is the same start (two triggers of one play).
CONTEXT_REPEAT_S = 60.0
#: A row-wording guess needs at least this many rows sharing the play wording (PLAN.md L6: "the rest").
LEARN_MIN_OTHERS = 2
#: Row-wording candidates held in RAM at most, waiting for a read that confirms one (a row flipping wording).
CANDIDATES_MAX = 4
#: After a warm-up that did not build the tree, the same window is not warmed again for this long (s): its reads say
#: "not built" meanwhile (a window that never builds must not cost 3 s on every trigger).
WARM_RETRY_S = 600.0
#: A window tree at least this big is drawn, whatever its labels: the frame alone is 7 elements, the smallest full view
#: read was 59 (round4/spotify-desktop-route.md §1, §4). A drawn tree without the controls the reader knows is
#: ``unrecognised`` (a Spotify update renamed them, or another display language with two 0-1 sliders), never
#: "not built".
DRAWN_MIN_ELEMENTS = 40
#: A warm-up ends on a drawn but unrecognised tree once its count stayed the same for this many polls (~0.9 s; a build
#: climbs 8 -> 24 -> 58 -> 311 within a few hundred ms, ``perception/tree.py`` "Lazily built trees").
WARM_DRAWN_STABLE_POLLS = 6

FRONT, COVERED, VISIBLE, MINIMISED = "front", "covered", "visible", "minimised"

#: nudge_state keys (content-free status; learned UI wording; encrypted learned links).
STATUS_KEY = "spotify_desktop"
LABELS_KEY = "spotify_labels"
LINKS_KEY = "spotify_links"


# ---------------------------------------------------------------------------------------------------------------------
# Data (sites/spotify.toml)
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One step of a container path: an element of this role and name."""

    role: str
    name: str


@dataclass(frozen=True)
class Labels:
    """Spotify's UI labels in one display language (``sites/spotify.toml`` ``[labels.<lang>]``)."""

    lang: str
    now_playing_bar: tuple[Step, ...]
    player_controls: tuple[Step, ...]
    library_grid: tuple[Step, ...]
    row_path: tuple[str, ...]
    controls: dict[str, tuple[str, str, str]]
    row_play: str
    row_pause: str
    row_open: tuple[str, ...]
    spotify_owner: str

    def path(self, container: str) -> tuple[Step, ...]:
        return {"now_playing_bar": self.now_playing_bar, "player_controls": self.player_controls,
                "library_grid": self.library_grid}.get(container, ())


def _steps(raw: Any) -> tuple[Step, ...]:
    out = []
    for item in raw or []:
        try:
            role, name = str(item[0]), str(item[1])
        except (IndexError, TypeError):
            continue
        out.append(Step(role, name))
    return tuple(out)


def data() -> dict[str, Any]:
    """The parsed ``sites/spotify.toml``."""
    return site("spotify")


def all_labels() -> list[Labels]:
    """Every display language's labels, English first."""
    tables = (data().get("labels") or {})
    out = []
    for lang in sorted(tables, key=lambda k: (k != "en", k)):
        t = tables.get(lang) or {}
        rows = t.get("rows") or {}
        controls = {}
        for key, value in (t.get("controls") or {}).items():
            try:
                controls[str(key)] = (str(value[0]), str(value[1]), str(value[2]))
            except (IndexError, TypeError):
                continue
        out.append(Labels(
            lang=str(lang), now_playing_bar=_steps(t.get("now_playing_bar")),
            player_controls=_steps(t.get("player_controls")), library_grid=_steps(t.get("library_grid")),
            row_path=tuple(str(r) for r in t.get("row_path") or ()), controls=controls,
            row_play=str(rows.get("play") or ""), row_pause=str(rows.get("pause") or ""),
            row_open=tuple(str(r) for r in rows.get("open") or ()), spotify_owner=str(rows.get("spotify_owner") or "")))
    return out


def process_image() -> str:
    """The image name of the program that draws Spotify's window (``Spotify.exe``)."""
    return str((data().get("app") or {}).get("process") or "Spotify.exe")


def app_key() -> str:
    """Spotify's app key, as the timeline writes it (``spotify``)."""
    image = process_image().strip().casefold()
    return image[:-4] if image.endswith(".exe") else image


def playlist_uri(link: str | None) -> str | None:
    """``spotify:playlist:{id}`` for a Spotify in-app playlist link (``https://xpui.app.spotify.com/playlist/{id}``,
    any query ignored), or None. A fixed-format parse of the link template in ``sites/spotify.toml``."""
    links = data().get("links") or {}
    template, uri = str(links.get("playlist") or ""), str(links.get("playlist_uri") or "")
    if not link or not template or not uri:
        return None
    values = match_template(template, str(link).split("?", 1)[0].split("#", 1)[0])
    pid = (values or {}).get("id") or ""
    if not pid or not pid.isalnum():
        return None
    return uri.replace("{id}", pid)


def playlist_uri_ok(uri: str | None) -> bool:
    """Whether ``uri`` has the shape of ``spotify:playlist:{id}`` (the URI template in the data, an alphanumeric id)."""
    template = str((data().get("links") or {}).get("playlist_uri") or "")
    values = match_template(template, str(uri)) if template and uri else None
    pid = (values or {}).get("id") or ""
    return bool(pid) and pid.isalnum()


def norm(name: Any) -> str:
    """A UI name with its whitespace collapsed (a machine normalisation, nothing more)."""
    return " ".join(str(name or "").split())


# ---------------------------------------------------------------------------------------------------------------------
# Media session (PLAN.md L7)
# ---------------------------------------------------------------------------------------------------------------------


def is_spotify_session(app_id: str | None, *, aumid: str | None = None, family: str | None = None,
                       image: str | None = None) -> bool:
    """Whether a media session's ``app_id`` is Spotify's.

    With the Spotify window's process known (its AppUserModelID ``aumid`` and package ``family``, or its ``image``
    for an unpackaged process) the session must belong to that process; otherwise the two install forms in
    ``sites/spotify.toml`` decide (the Store build's ``<SpotifyAB.SpotifyMusic family>!Spotify``, the classic
    ``Spotify.exe``)."""
    sid = str(app_id or "").strip()
    if not sid:
        return False
    low = sid.casefold()
    if aumid:
        return low == str(aumid).casefold()
    if family:
        return low.startswith(str(family).casefold() + "!")
    if image:
        return low == str(image).casefold() or low.endswith("\\" + str(image).casefold())
    s = data().get("session") or {}
    classic = str(s.get("classic_app_id") or "").casefold()
    if classic and (low == classic or low.endswith("\\" + classic)):
        return True
    fam, _, app = low.partition("!")
    name = str(s.get("store_package_name") or "").casefold()
    store_app = str(s.get("store_app") or "").casefold()
    return bool(name and app and fam.split("_", 1)[0] == name and app == store_app)


def process_identity(pid: int) -> dict[str, str]:
    """``{"aumid", "family", "image"}`` of a process (``""`` for what cannot be read)."""
    out = {"aumid": "", "family": "", "image": ""}
    try:
        from yuki.perception.windows import process_package_ids

        out["aumid"], out["family"] = process_package_ids(int(pid))
    except Exception:  # noqa: BLE001
        pass
    out["image"] = _image_of(int(pid))
    return out


def session_matches(app_id: str | None, ident: dict[str, str] | None, *, strict: bool = False) -> bool:
    """Whether a session is Spotify's, given the Spotify window process's identity (:func:`process_identity`): its
    AppUserModelID (or package family) decides when it has one; with only its image known (the package read failed,
    or a classic install) the image counts, and unless ``strict`` either install form in the data too (a Store build
    whose package could not be read still has the Store form)."""
    ident = ident or {}
    if ident.get("aumid") or ident.get("family"):
        return is_spotify_session(app_id, aumid=ident.get("aumid") or None,
                                  family=None if ident.get("aumid") else ident.get("family"))
    if ident.get("image"):
        return is_spotify_session(app_id, image=ident["image"]) or (not strict and is_spotify_session(app_id))
    return is_spotify_session(app_id)


def spotify_session(sessions: Iterable[dict[str, Any]], *, pid: int | None = None) -> dict[str, Any] | None:
    """Spotify's session among Windows' media sessions (``perception.system.media_sessions()`` rows), matched to the
    Spotify window's process ``pid`` when given (a session of that very process first); None when there is none."""
    ident = process_identity(pid) if pid else {}
    rows = [(s, s.get("app_id") if isinstance(s, dict) else getattr(s, "app_id", None)) for s in sessions or []]
    for strict in (True, False):
        for s, app_id in rows:
            if session_matches(app_id, ident, strict=strict):
                return s if isinstance(s, dict) else {"app_id": app_id}
    return None


# ---------------------------------------------------------------------------------------------------------------------
# The tree (live UI Automation; the tests drive the same code with a fake automation object)
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class El:
    """One element as read: role, name, its Value (a Hyperlink's link), its range, and its cached children."""

    role: str
    name: str
    value: str = ""
    rng: tuple[float, float, float] | None = None   # (minimum, maximum, value)
    kids: list["El"] | None = None
    ref: Any = None

    def walk(self) -> Iterable["El"]:
        for kid in self.kids or []:
            yield kid
            yield from kid.walk()


def _role_names() -> dict[int, str]:
    from yuki.perception.tree import _ROLE_NAMES

    return _ROLE_NAMES


def _role_ids() -> dict[str, int]:
    return {name: int(control) for control, name in _role_names().items()}


class UiaTree:
    """One window's live tree on the calling (COM) thread: ``FindAll`` by control type and name from the root,
    whole subtrees in one cache request, never ``ElementFromPoint``, never an ``IsOffscreen`` filter. Read-only."""

    def __init__(self, automation: Any, hwnd: int) -> None:
        self.automation = automation
        self.hwnd = int(hwnd)
        self.calls = 0
        self._roles = _role_names()
        self._ids = _role_ids()
        self.request = self._request(_TREE_SCOPE_ELEMENT)
        self.subtree_request = self._request(_TREE_SCOPE_SUBTREE)
        self.count_request = automation.CreateCacheRequest()
        self.count_request.AddProperty(_P_CONTROL_TYPE)
        self.count_request.TreeScope = _TREE_SCOPE_ELEMENT
        self.root = automation.ElementFromHandle(int(hwnd))

    def _request(self, scope: int) -> Any:
        request = self.automation.CreateCacheRequest()
        for prop in _READ_PROPS:
            try:
                request.AddProperty(prop)
            except Exception:  # noqa: BLE001 - an older UIA without it: absent
                pass
        request.TreeScope = scope
        return request

    def _el(self, element: Any, *, with_kids: bool = False) -> El:
        def get(pid: int) -> Any:
            try:
                return element.GetCachedPropertyValue(pid)
            except Exception:  # noqa: BLE001
                return None

        try:
            role = self._roles.get(int(get(_P_CONTROL_TYPE) or 0), "")
        except (TypeError, ValueError):
            role = ""
        value = get(_P_VALUE_VALUE)
        rng = None
        if get(_P_IS_RANGE_VALUE_AVAILABLE):
            try:
                rng = (float(get(_P_RANGE_VALUE_MINIMUM)), float(get(_P_RANGE_VALUE_MAXIMUM)),
                       float(get(_P_RANGE_VALUE_VALUE)))
            except (TypeError, ValueError):
                rng = None
        el = El(role=role, name=norm(get(_P_NAME)), value=str(value) if isinstance(value, str) else "", rng=rng,
                ref=element)
        if with_kids:
            el.kids = []
            try:
                arr = element.GetCachedChildren()
            except Exception:  # noqa: BLE001
                arr = None
            if arr:
                for i in range(int(arr.Length)):
                    el.kids.append(self._el(arr.GetElement(i), with_kids=True))
        return el

    def find(self, role: str | None = None, name: str | None = None, *, scope: Any = None) -> list[El]:
        """Every descendant of ``scope`` (default the window root) of this role (and name), as one ``FindAll``."""
        a = self.automation
        conds = []
        if role:
            control = self._ids.get(role)
            if control is None:
                return []
            conds.append(a.CreatePropertyCondition(_P_CONTROL_TYPE, control))
        if name is not None:
            conds.append(a.CreatePropertyCondition(_P_NAME, name))
        if not conds:
            cond = a.CreateTrueCondition()
        else:
            cond = conds[0]
            for extra in conds[1:]:
                cond = a.CreateAndCondition(cond, extra)
        self.calls += 1
        try:
            arr = (scope if scope is not None else self.root).FindAllBuildCache(_TREE_SCOPE_DESCENDANTS, cond,
                                                                                self.request)
        except Exception:  # noqa: BLE001 - a provider that fails reads as nothing found
            return []
        if not arr:
            return []
        return [self._el(arr.GetElement(i)) for i in range(int(arr.Length))]

    def subtree(self, el: El) -> El:
        """``el`` with its whole subtree cached (one round trip), children in ``kids``."""
        self.calls += 1
        try:
            built = el.ref.BuildUpdatedCache(self.subtree_request)
        except Exception:  # noqa: BLE001
            return El(role=el.role, name=el.name, value=el.value, rng=el.rng, kids=[], ref=el.ref)
        return self._el(built, with_kids=True)

    def count(self) -> int:
        """How many elements the window's tree has now (one ``FindAll`` of everything)."""
        self.calls += 1
        try:
            arr = self.root.FindAllBuildCache(_TREE_SCOPE_DESCENDANTS, self.automation.CreateTrueCondition(),
                                              self.count_request)
            return int(arr.Length) if arr else 0
        except Exception:  # noqa: BLE001
            return 0

    def ask_children(self, handles: Iterable[int]) -> int:
        """Ask each child window for its UIA root (its own ``WM_GETOBJECT``) and read from it, as
        ``perception/tree.py`` does with each child window's subtree: what makes a lazily built Chromium tree start
        building ("Lazily built trees"). One cached request per child (its first child, control type only), so the
        child's provider is asked for content and not only for an element. Returns how many answered."""
        asked = 0
        true = None
        for handle in handles:
            try:
                element = self.automation.ElementFromHandle(int(handle))
                asked += 1
            except Exception:  # noqa: BLE001 - a child that went away
                continue
            self.calls += 1
            try:
                if true is None:
                    true = self.automation.CreateTrueCondition()
                element.FindFirstBuildCache(_TREE_SCOPE_CHILDREN, true, self.count_request)
            except Exception:  # noqa: BLE001 - a child with nothing in it yet: it was still asked
                continue
        return asked


def local_find(scope: El, role: str | None = None, name: str | None = None) -> list[El]:
    """Elements of ``scope``'s cached subtree (not ``scope`` itself) of this role (and name)."""
    return [e for e in scope.walk() if (role is None or e.role == role) and (name is None or e.name == name)]


def _container(tree: UiaTree, path: tuple[Step, ...]) -> list[El]:
    """The elements at the end of a container path: the first step by ``FindAll`` from the root, its subtree in one
    request, the remaining steps inside it."""
    if not path:
        return []
    first = path[0]
    found = []
    for el in tree.find(first.role, first.name):
        scopes = [tree.subtree(el)]
        for step in path[1:]:
            scopes = [e for s in scopes for e in local_find(s, step.role, step.name)]
        found.extend(scopes)
    return found


# ---------------------------------------------------------------------------------------------------------------------
# What a read finds
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class Row:
    """One playlist row of Your Library, as read. ``name`` and ``owner`` stay in RAM (the moments writer encrypts what
    is kept); ``playing`` is None while the row wording is unknown (another language, not learned yet)."""

    name: str
    owner: str | None = None
    spotify_owned: bool | None = None
    playing: bool | None = None
    play_button: str | None = None   # the label the row's play button has now (the skills Invoke it in phase S)


@dataclass
class SpotifyRead:
    """One read of Spotify's window. ``status``: ``ok``, ``not_built`` (only the frame: the tree is not built yet),
    ``unrecognised`` (a drawn tree, at least :data:`DRAWN_MIN_ELEMENTS`, with none of the controls the reader knows:
    labels renamed by an update, or another language with no single 0-1 slider), ``no_library`` (player controls but
    no Library grid: the full-screen or Now Playing view), ``timeout``, ``error``, ``no_window``, ``held_game`` (a game
    in front: nothing read)."""

    status: str
    built: bool = False
    player: bool = False
    slider: float | None = None
    slider_source: str | None = None
    grid: bool = False
    rows: list[Row] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)
    label_source: str | None = None
    elements: int | None = None
    ms: float = 0.0
    #: A row-wording candidate from this read (another language, session playing): provisional, no play state.
    candidate: WordingGuess | None = None
    calls: int = 0
    error: str | None = None
    #: ``[(row name, [button labels])]`` as read, in RAM only (the watcher confirms a candidate against it; never
    #: logged or kept).
    raw: list[tuple[str, list[str]]] = field(default_factory=list, repr=False)

    @property
    def playing(self) -> Row | None:
        hits = [r for r in self.rows if r.playing]
        return hits[0] if len(hits) == 1 else None

    def counts(self) -> dict[str, Any]:
        """The content-free shape of the read, for the logs (no name, link or id)."""
        return {"status": self.status, "built": self.built, "player": self.player,
                "slider_found": self.slider is not None, "slider_source": self.slider_source, "grid": self.grid,
                "rows": len(self.rows),
                "playing_found": self.playing is not None, "links": len(self.links),
                "label_source": self.label_source, "elements": self.elements, "ms": round(self.ms, 1),
                "uia_calls": self.calls}


def _owner(button: str, name: str, labels: Labels) -> str | None:
    """The owner part of a row's open button, by the row formats in the data; None when no format fits."""
    for template in labels.row_open:
        if "{owner}" not in template:
            continue
        head, _, tail = template.replace("{name}", name).partition("{owner}")
        if button.startswith(head) and button.endswith(tail) and len(button) > len(head) + len(tail):
            return button[len(head): len(button) - len(tail) if tail else None]
    return None


def _template_of(label: str, name: str) -> str | None:
    """A button label with the row's name replaced by ``{name}`` (only when the name occurs exactly once)."""
    if not name or label.count(name) != 1:
        return None
    return label.replace(name, "{name}")


@dataclass
class WordingGuess:
    """A row-wording candidate from one playing read (PLAN.md L6), **provisional**: it gives no play state and is
    never kept on disk until a later read confirms it (:func:`judge_wording`). ``rows``: which wording each row's
    button carried in the reads so far (``"play"`` / ``"pause"``), by row name, in RAM only (never logged or kept)."""

    play: str
    pause: str
    grid: str = ""
    rows: dict[str, str] = field(default_factory=dict, repr=False)


def row_wording_candidate(rows: list[tuple[str, list[str]]]) -> WordingGuess | None:
    """A candidate for the row play button's wording from one read taken while Spotify's media session says
    playing (PLAN.md L6): the button position where every row's label, with its name replaced by ``{name}``, is the
    same wording except exactly one row's (the playing one), with at least :data:`LEARN_MIN_OTHERS` rows sharing it.
    None when no position, or more than one, fits (never a guess).

    One read cannot tell the play button from another button that differs on exactly one row (the open button of
    the one row by another owner, or the one pinned row, while the playing item is not a Library row in view), so
    this is only a candidate: :func:`judge_wording` confirms it when a row's button flips between the two wordings
    (the play button flips as music starts and stops; an owner never does)."""
    width = max((len(buttons) for _n, buttons in rows), default=0)
    fits: list[WordingGuess] = []
    for pos in range(width):
        templated = [(name, _template_of(buttons[pos], name)) for name, buttons in rows if pos < len(buttons)]
        if len(templated) < LEARN_MIN_OTHERS + 1 or any(t is None for _n, t in templated):
            continue
        counts: dict[str, int] = {}
        for _n, t in templated:
            counts[t] = counts.get(t, 0) + 1   # type: ignore[index]
        if len(counts) != 2:
            continue
        (a, na), (b, nb) = sorted(counts.items(), key=lambda kv: -kv[1])
        if nb == 1 and na >= LEARN_MIN_OTHERS:
            fits.append(WordingGuess(play=a, pause=b,   # type: ignore[arg-type]
                                     rows={n: ("pause" if t == b else "play") for n, t in templated}))
    return fits[0] if len(fits) == 1 else None


def learn_row_wording(rows: list[tuple[str, list[str]]]) -> tuple[str, str] | None:
    """``(play, pause)`` of :func:`row_wording_candidate` (a candidate only), or None."""
    guess = row_wording_candidate(rows)
    return (guess.play, guess.pause) if guess is not None else None


def _row_wording_now(buttons: list[str], name: str, play: str, pause: str) -> str | None:
    if pause.replace("{name}", name) in buttons:
        return "pause"
    if play.replace("{name}", name) in buttons:
        return "play"
    return None


def judge_wording(rows: list[tuple[str, list[str]]], play: str, pause: str,
                  seen: dict[str, str] | None = None) -> str | None:
    """What one read says about a row wording ``(play, pause)``: ``"contradict"`` when two or more rows carry the
    pause wording at once (two playing rows: not a play button); ``"confirm"`` when a row in ``seen`` (the wording
    each row carried before) now carries the other wording (it flipped: a play button does, an owner or a list's
    kind does not); None when the read says nothing either way."""
    now = {name: _row_wording_now(buttons, name, play, pause) for name, buttons in rows}
    if sum(1 for w in now.values() if w == "pause") >= 2:
        return "contradict"
    for name, was in (seen or {}).items():
        if now.get(name) is not None and now[name] != was:
            return "confirm"
    return None


def apply_wording(got: "SpotifyRead", play: str, pause: str, source: str = "learned") -> None:
    """Mark ``got``'s rows playing or not with a wording confirmed after the read was taken (its raw labels)."""
    labels = dict(got.raw)
    for row in got.rows:
        buttons = labels.get(row.name) or []
        state = _row_wording_now(buttons, row.name, play, pause)
        if state is not None:
            row.playing = state == "pause"
            row.play_button = (pause if row.playing else play).replace("{name}", row.name)
    got.label_source = source


def _rows_of(grid: El, labels: Labels) -> list[tuple[str, list[str]]]:
    """``(name, [button labels in order])`` per row group: DataItem > DataItem > Group under the grid."""
    path = labels.row_path or ("DataItem", "DataItem", "Group")
    level = [grid]
    for role in path:
        level = [k for e in level for k in (e.kids or []) if k.role == role]
    return [(g.name, [b.name for b in local_find(g, "Button")]) for g in level if g.name]


def _structural_grids(tree: UiaTree, labels: Labels) -> list[El]:
    """DataGrids shaped like Your Library (DataItem > DataItem > Group rows), for a Spotify whose grid label is not
    in the data: the only one, else none."""
    shaped = []
    for el in tree.find("DataGrid"):
        full = tree.subtree(el)
        if _rows_of(full, labels):
            shaped.append(full)
    return shaped if len(shaped) == 1 else []


def read_tree(tree: UiaTree, labels: list[Labels] | None = None, *, learned: dict[str, str] | None = None,
              want: Iterable[str] = ("slider", "library", "links"), session_playing: bool = False,
              known_names: Iterable[str] = (), deadline: float | None = None) -> SpotifyRead:
    """One read of a Spotify window's tree (see the module docstring); ``deadline`` (monotonic) bounds it: a read
    past it stops where it is with ``timeout``."""
    started = time.perf_counter()
    want = set(want)
    langs = labels if labels is not None else all_labels()
    deadline = deadline if deadline is not None else time.monotonic() + READ_BUDGET_S
    out = SpotifyRead(status="ok")

    def late() -> bool:
        return time.monotonic() >= deadline

    def done(status: str | None = None) -> SpotifyRead:
        if status:
            out.status = status
        out.ms = (time.perf_counter() - started) * 1000.0
        out.calls = tree.calls
        return out

    used: Labels | None = None
    bar: El | None = None
    for lab in langs:
        bars = _container(tree, lab.now_playing_bar)
        if bars:
            bar = bars[0]
            tail = lab.player_controls[len(lab.now_playing_bar):] if lab.player_controls[:len(lab.now_playing_bar)] \
                == lab.now_playing_bar else ()
            controls = [bar]
            for step in tail:
                controls = [e for c in controls for e in local_find(c, step.role, step.name)]
            if controls and any(local_find(c, "Button") for c in controls):
                out.player, used = True, lab
                break
        if late():
            return done("timeout")
    if "slider" in want or not out.player:
        if used is not None and bar is not None and "volume" in used.controls:
            role, name, _container_name = used.controls["volume"]
            hits = [e for e in local_find(bar, role, name) if e.rng is not None]
            if hits:
                out.slider, out.slider_source = _percent(hits[0].rng), "english"
        if out.slider is None and not late():
            shape = _shape_slider(tree)
            if shape is not None:
                out.slider, out.slider_source = shape, "shape"
    out.built = out.player or out.slider_source == "shape"
    if not out.built:
        if not late():
            out.elements = tree.count()
        if late():
            return done("timeout")
        return done("unrecognised" if (out.elements or 0) >= DRAWN_MIN_ELEMENTS else "not_built")
    if late():
        return done("timeout")
    if "library" in want or "links" in want:
        grid = None
        for lab in ([used] if used else []) + [lab for lab in langs if lab is not used]:
            grids = _container(tree, lab.library_grid)
            if grids:
                grid, used_rows = grids[0], lab
                break
        else:
            used_rows = used or (langs[0] if langs else None)
        if grid is None and learned and learned.get("grid") and not late():
            grids = [tree.subtree(e) for e in tree.find("DataGrid", learned["grid"])]
            grid = grids[0] if grids else None
        if grid is None and used_rows is not None and not late():
            grids = _structural_grids(tree, used_rows)
            grid = grids[0] if grids else None
        if grid is None:
            return done("timeout" if late() else "no_library")
        out.grid = True
        raw = _rows_of(grid, used_rows) if used_rows is not None else []
        out.raw = raw
        wording = _wording(raw, used_rows, learned)
        if wording is None and session_playing and len(raw) >= LEARN_MIN_OTHERS + 1:
            guess = row_wording_candidate(raw)
            if guess is not None:   # provisional: no play state until a later read confirms it (the watcher)
                guess.grid = grid.name
                out.candidate = guess
        out.label_source = wording[2] if wording else "none"
        for name, buttons in raw:
            row = Row(name=name)
            if used_rows is not None:
                for b in buttons:
                    owner = _owner(b, name, used_rows)
                    if owner is not None:
                        row.owner = owner
                        row.spotify_owned = owner == used_rows.spotify_owner
                        break
            if wording is not None:
                state = _row_wording_now(buttons, name, wording[0], wording[1])
                if state is not None:
                    row.playing = state == "pause"
                    row.play_button = wording[1 if row.playing else 0].replace("{name}", name)
            out.rows.append(row)
    if "links" in want and not late():
        # only rows whose owner parsed and is not Spotify (PLAN.md D1: Spotify-owned and unparsed rows digest-only)
        names = {r.name for r in out.rows if r.spotify_owned is False} | {norm(n) for n in known_names}
        names -= {r.name for r in out.rows if r.spotify_owned is not False}
        if names:
            for link in tree.find("Hyperlink"):
                if link.name in names and link.name not in out.links:
                    uri = playlist_uri(link.value)
                    if uri:
                        out.links[link.name] = uri
    if late():
        return done("timeout")
    return done("ok")


def _wording(raw: list[tuple[str, list[str]]], labels: Labels | None,
             learned: dict[str, str] | None) -> tuple[str, str, str] | None:
    """``(play, pause, source)``: the data's wording when any row carries it, else the learned one when any row
    carries that, else None."""
    def carried(play: str, pause: str) -> bool:
        return any(play.replace("{name}", n) in bs or pause.replace("{name}", n) in bs for n, bs in raw)

    if labels is not None and labels.row_play and labels.row_pause and carried(labels.row_play, labels.row_pause):
        return labels.row_play, labels.row_pause, "english" if labels.lang == "en" else labels.lang
    if learned and learned.get("play") and learned.get("pause") and carried(learned["play"], learned["pause"]):
        return learned["play"], learned["pause"], "learned"
    return None


def _percent(rng: tuple[float, float, float] | None) -> float | None:
    if rng is None:
        return None
    lo, hi, value = rng
    if hi <= lo:
        return None
    return round(max(0.0, min(1.0, (value - lo) / (hi - lo))) * 100.0, 1)


def _shape_slider(tree: UiaTree) -> float | None:
    """The window's only Slider whose range is the volume slider's (0-1 in the data); None with none or several."""
    s = data().get("slider") or {}
    lo, hi = float(s.get("range_min", 0.0)), float(s.get("range_max", 1.0))
    hits = [e for e in tree.find("Slider") if e.rng is not None
            and abs(e.rng[0] - lo) < 1e-6 and abs(e.rng[1] - hi) < 1e-6]
    return _percent(hits[0].rng) if len(hits) == 1 else None


@dataclass
class WarmResult:
    """What a warm-up did: element counts before and after, whether the player controls (or the shape slider) are
    there now, how long it took. ``cold``: the tree was not built when the warm-up began. ``drawn``: not built by
    the reader's labels, but a tree of at least :data:`DRAWN_MIN_ELEMENTS` that stopped growing (``unrecognised``:
    waiting longer would not help)."""

    before: int = 0
    after: int = 0
    built: bool = False
    cold: bool = False
    asked: int = 0
    polls: int = 0
    ms: float = 0.0
    error: str | None = None
    #: The warm-up stopped early because ``stop_fn`` said so (a game came to the front).
    stopped: bool = False
    drawn: bool = False

    @property
    def status(self) -> str:
        """``ok`` (built), ``unrecognised`` (drawn, labels not known), else the error or ``not_built``."""
        if self.built:
            return "ok"
        if self.drawn:
            return "unrecognised"
        return self.error or "not_built"

    def counts(self) -> dict[str, Any]:
        return {"elements_before": self.before, "elements": self.after, "built": self.built, "cold": self.cold,
                "children_asked": self.asked, "polls": self.polls, "warm_ms": round(self.ms, 1),
                "stopped": self.stopped, "drawn": self.drawn}


def _built(tree: UiaTree, langs: list[Labels]) -> bool:
    for lab in langs:
        for controls in [_container(tree, lab.player_controls)]:
            if any(local_find(c, "Button") for c in controls):
                return True
    return _shape_slider(tree) is not None


def _stop_now(stop_fn: Callable[[], bool] | None) -> bool:
    if stop_fn is None:
        return False
    try:
        return bool(stop_fn())
    except Exception:  # noqa: BLE001 - fails closed: stop
        return True


def warm_tree(tree: UiaTree, handles: Iterable[int], *, budget_s: float = WARM_BUDGET_S,
              labels: list[Labels] | None = None, clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep, step_s: float = WARM_STEP_S,
              stop_fn: Callable[[], bool] | None = None) -> WarmResult:
    """Warm a lazily built window (see the module docstring): if the player controls are already there, nothing to
    do; else ask every visible child window, then poll the element count until the tree is built (player controls,
    or the shape slider) and its count stopped growing, at most ``budget_s``. A condition poll keyed on counts and
    structure, never a settle sleep. ``stop_fn`` (the game check) is asked before asking the children and before
    each poll: True stops the warm-up there (``stopped``; never with a game in front, PLAN.md F7). A tree that is
    drawn (at least :data:`DRAWN_MIN_ELEMENTS`) but has none of the known controls ends the warm-up once its count
    stayed the same for :data:`WARM_DRAWN_STABLE_POLLS` polls (``drawn``): its labels are not known, and waiting the
    whole budget would not change that."""
    started = clock()
    langs = labels if labels is not None else all_labels()
    out = WarmResult()
    out.before = tree.count()
    if _built(tree, langs):
        out.after, out.built = out.before, True
        out.ms = (clock() - started) * 1000.0
        return out
    out.cold = True
    if _stop_now(stop_fn):
        out.after, out.stopped = out.before, True
        out.ms = (clock() - started) * 1000.0
        return out
    out.asked = tree.ask_children(handles)
    last, stable, built = out.before, 0, False
    while clock() - started < budget_s:
        if _stop_now(stop_fn):
            out.stopped = True
            break
        sleep(min(step_s, max(budget_s - (clock() - started), 0.0)))
        out.polls += 1
        now = tree.count()
        if now != last:
            last, stable = now, 0
        else:
            stable += 1
        if now > out.before and (stable == 0 or not built):
            built = _built(tree, langs)
        if built and stable >= WARM_STABLE_POLLS:
            break   # built, and stopped growing
        if not built and stable >= WARM_DRAWN_STABLE_POLLS and now >= DRAWN_MIN_ELEMENTS:
            out.drawn = True
            break   # drawn and stopped growing, but not by any labels the reader knows: unrecognised
    out.after = last
    out.built = built or _built(tree, langs)
    if out.built:
        out.drawn = False
    out.ms = (clock() - started) * 1000.0
    return out


# ---------------------------------------------------------------------------------------------------------------------
# The live window: finding it, its state, reads on their own UIA thread
# ---------------------------------------------------------------------------------------------------------------------

_images: dict[int, str] = {}
_images_lock = threading.Lock()


def _image_of(pid: int) -> str:
    """A process's image name (cached per pid), ``""`` when unreadable."""
    with _images_lock:
        if pid in _images:
            return _images[pid]
    name = ""
    try:
        import psutil

        name = psutil.Process(int(pid)).name()
    except Exception:  # noqa: BLE001
        name = ""
    with _images_lock:
        if len(_images) > 512:
            _images.clear()
        _images[pid] = name
    return name


def is_spotify_pid(pid: int) -> bool:
    """Whether a process is the program that draws Spotify's window."""
    return bool(pid) and _image_of(int(pid)).casefold() == process_image().casefold()


@dataclass(frozen=True)
class WindowRef:
    hwnd: int
    pid: int
    minimised: bool


def find_window() -> WindowRef | None:
    """Spotify's top window (a user window of its process, minimised included), or None when it has none (not
    running, or closed to the tray). The largest one wins when there are several."""
    try:
        import win32gui
        import win32process

        from yuki.perception.windows import is_user_window
    except Exception:  # noqa: BLE001
        return None
    found: list[tuple[int, int, int, bool]] = []

    def collect(hwnd: int, _extra: Any) -> bool:
        try:
            if not is_user_window(hwnd):
                return True
            pid = int(win32process.GetWindowThreadProcessId(hwnd)[1])
            if not is_spotify_pid(pid):
                return True
            minimised = bool(win32gui.IsIconic(hwnd))
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            found.append((0 if minimised else (right - left) * (bottom - top), int(hwnd), pid, minimised))
        except Exception:  # noqa: BLE001
            pass
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:  # noqa: BLE001
        return None
    if not found:
        return None
    _area, hwnd, pid, minimised = max(found)
    return WindowRef(hwnd=hwnd, pid=pid, minimised=minimised)


def window() -> int | None:
    """Spotify's top window handle (any state), or None."""
    ref = find_window()
    return ref.hwnd if ref else None


def classify_state(*, minimised: bool, front: bool, share: float | None) -> str:
    """A window's state from geometry facts: minimised, in front, else covered (less than the visibility module's
    minimum share can be seen, or not on any monitor) or visible (on screen, not in front: another monitor, partly
    covered)."""
    if minimised:
        return MINIMISED
    if front:
        return FRONT
    from yuki.perception.visibility import VISIBLE_MIN_SHARE

    return VISIBLE if share is not None and share >= VISIBLE_MIN_SHARE else COVERED


def window_state(hwnd: int) -> str | None:
    """``front`` / ``covered`` / ``visible`` / ``minimised`` for ``hwnd`` (geometry only: z-order, rectangles, the
    foreground window); None when it is gone."""
    try:
        import ctypes

        import win32gui

        if not win32gui.IsWindow(hwnd):
            return None
        if win32gui.IsIconic(hwnd):
            return MINIMISED
        fg = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        root = int(ctypes.windll.user32.GetAncestor(fg, 3) or fg) if fg else 0   # GA_ROOTOWNER
        if int(hwnd) in (fg, root):
            return FRONT
        from yuki.perception.visibility import layout

        surface = layout(foreground=fg).by_hwnd(int(hwnd))
        return classify_state(minimised=False, front=False, share=surface.visible_share if surface else None)
    except Exception:  # noqa: BLE001
        return None


def new_automation() -> Any:
    """A CUIAutomation object for the calling thread (short UIA timeouts where the OS has them)."""
    import comtypes.client

    from yuki.perception.tree import _uia_core

    module = _uia_core()
    try:
        automation = comtypes.client.CreateObject(_CUIAUTOMATION8, interface=module.IUIAutomation2)
        automation.ConnectionTimeout = 1000
        automation.TransactionTimeout = 1000
        return automation
    except Exception:  # noqa: BLE001
        return comtypes.client.CreateObject(_CUIAUTOMATION, interface=module.IUIAutomation)


def run_uia(job: Callable[[Any, dict], None], budget_s: float, *, name: str = "yuki-spotify-read",
            automation_fn: Callable[[], Any] | None = None) -> tuple[dict, bool]:
    """Run ``job(automation, shared)`` on its own thread with its own COM apartment; wait at most ``budget_s``.
    ``(shared, timed_out)``: a provider that blocks costs the budget, never the caller."""
    shared: dict = {}

    def run() -> None:
        coinit = False
        try:
            import comtypes

            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            coinit = True
        except Exception:  # noqa: BLE001 - already initialised, or no COM (tests with a fake)
            pass
        try:
            job((automation_fn or new_automation)(), shared)
        except BaseException as exc:  # noqa: BLE001 - reported, never raised
            shared["error"] = type(exc).__name__
        finally:
            if coinit:
                try:
                    import comtypes

                    comtypes.CoUninitialize()
                except Exception:  # noqa: BLE001
                    pass

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    thread.join(max(budget_s, 0.0))
    return shared, thread.is_alive()


def _child_handles(hwnd: int) -> list[int]:
    try:
        from yuki.perception.tree import child_window_handles

        return child_window_handles(int(hwnd))
    except Exception:  # noqa: BLE001
        return []


def warm(hwnd: int, budget_s: float = WARM_BUDGET_S, *, automation_fn: Callable[[], Any] | None = None,
         handles_fn: Callable[[int], list[int]] | None = None,
         stop_fn: Callable[[], bool] | None = None) -> WarmResult:
    """Warm Spotify's window (see :func:`warm_tree`), at most ``budget_s`` (plus a small hand-off margin)."""
    def job(automation: Any, shared: dict) -> None:
        tree = UiaTree(automation, hwnd)
        shared["result"] = warm_tree(tree, (handles_fn or _child_handles)(hwnd), budget_s=budget_s,
                                     stop_fn=stop_fn)

    shared, timed_out = run_uia(job, budget_s + 0.5, name="yuki-spotify-warm", automation_fn=automation_fn)
    result = shared.get("result")
    if isinstance(result, WarmResult):
        return result
    return WarmResult(error="timeout" if timed_out else (shared.get("error") or "error"))


def read(hwnd: int, budget_s: float = READ_BUDGET_S, *, want: Iterable[str] = ("slider", "library", "links"),
         learned: dict[str, str] | None = None, session_playing: bool = False, known_names: Iterable[str] = (),
         automation_fn: Callable[[], Any] | None = None) -> SpotifyRead:
    """One bounded read of Spotify's window (see :func:`read_tree`); never raises."""
    started = time.perf_counter()
    want, known = tuple(want), tuple(known_names)

    def job(automation: Any, shared: dict) -> None:
        tree = UiaTree(automation, hwnd)
        shared["result"] = read_tree(tree, want=want, learned=learned, session_playing=session_playing,
                                     known_names=known, deadline=time.monotonic() + budget_s)

    shared, timed_out = run_uia(job, budget_s + 0.1, automation_fn=automation_fn)
    result = shared.get("result")
    if isinstance(result, SpotifyRead):
        return result
    return SpotifyRead(status="timeout" if timed_out else "error", error=shared.get("error"),
                       ms=(time.perf_counter() - started) * 1000.0)


def library(hwnd: int, budget_s: float = READ_BUDGET_S, **kw: Any) -> list[Row] | None:
    """Your Library's rows in view; None when the window is not built or the read failed ([] = no rows)."""
    got = read(hwnd, budget_s, want=("library",), **kw)
    if got.status in ("ok",):
        return got.rows
    return [] if got.status == "no_library" else None


def links(hwnd: int, budget_s: float = READ_BUDGET_S, **kw: Any) -> dict[str, str] | None:
    """``{row name: spotify:playlist:{id}}`` for playlist links on screen whose name equals a Library row's name."""
    got = read(hwnd, budget_s, want=("library", "links"), **kw)
    return got.links if got.status == "ok" else None


def slider(hwnd: int, budget_s: float = READ_BUDGET_S, **kw: Any) -> float | None:
    """Spotify's own volume (0-100) from its slider; None when it cannot be read."""
    got = read(hwnd, budget_s, want=("slider",), **kw)
    return got.slider if got.built else None


# ---------------------------------------------------------------------------------------------------------------------
# "Is a game in front?" (PLAN.md D9: the Now board's game_front, else a local read of the foreground program)
# ---------------------------------------------------------------------------------------------------------------------


def foreground_is_game() -> bool:
    """Whether the program in front is a game, by machine facts (``gamegate.judge_program``). Fails closed: when it
    cannot be read, it is treated as a game (a passive read then waits)."""
    try:
        import ctypes

        import psutil
        import win32process

        fg = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        if not fg:
            return False
        pid = int(win32process.GetWindowThreadProcessId(fg)[1])
        proc = psutil.Process(pid)
        image = proc.name()
        try:
            exe = proc.exe()
        except Exception:  # noqa: BLE001 - an elevated program: its name still decides
            exe = ""
        from yuki.memory.gamegate import judge_program

        return bool(judge_program(image, exe)[0])
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------------------------------------------------------------
# The memory side
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class SpotifySettings:
    """``[spotify]`` of the privacy file (PLAN.md D.1 docs row); defaults when the table is missing."""

    enabled: bool = True
    library_reads: bool = True
    read_budget_s: float = READ_BUDGET_S
    warm_budget_s: float = WARM_BUDGET_S
    state_probe: bool = True
    probe_after_s: float = PROBE_AFTER_S

    @classmethod
    def from_dict(cls, table: dict[str, Any] | None) -> "SpotifySettings":
        t = dict(table or {})
        out = cls()
        for key in ("enabled", "library_reads", "state_probe"):
            if isinstance(t.get(key), bool):
                setattr(out, key, t[key])
        bounds = (("read_budget_s", 0.05, 2.0), ("warm_budget_s", 0.5, 10.0), ("probe_after_s", 10.0, 3600.0))
        for key, lo, hi in bounds:
            try:
                if key in t:
                    setattr(out, key, max(lo, min(hi, float(t[key]))))
            except (TypeError, ValueError):
                pass
        return out


class DesktopReader:
    """The live reader the watcher uses (tests pass a stub with the same methods)."""

    def find(self) -> WindowRef | None:
        return find_window()

    def state(self, hwnd: int) -> str | None:
        return window_state(hwnd)

    def warm(self, hwnd: int, budget_s: float, **kw: Any) -> WarmResult:
        return warm(hwnd, budget_s, stop_fn=kw.get("stop_fn"))

    def read(self, hwnd: int, budget_s: float, **kw: Any) -> SpotifyRead:
        return read(hwnd, budget_s, **kw)


def _app_ids(items: Iterable[Any]) -> list[str]:
    """The media session app ids in the timeline's change lists (tuples ``(app_id, ...)``, dicts or objects)."""
    out = []
    for item in items or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            out.append(str(item.get("app_id") or ""))
        elif isinstance(item, (tuple, list)) and item:
            out.append(str(item[0] or ""))
        else:
            out.append(str(getattr(item, "app_id", "") or ""))
    return [a for a in out if a]


class SpotifyWatch:
    """The memory side's watcher of Spotify's window (see the module docstring). Event-driven only: it acts on the
    Now board's layout, the timeline's front and media changes, and a one-shot probe timer the service's 1 s loop
    checks in :meth:`beat` (no thread of its own while idle; each job runs on a short-lived thread, one at a time).

    Args:
        emit: ``emit(moment: dict)`` for the moments writer (fields as the ``moments`` table, plus ``entity`` in clear
            for the writer to encrypt and digest, and ``entity_keep`` False for digest-only); None: no moments.
        log: ``log(type, **fields)``, content-free.
        store: the memory store (``nudge_state`` for the status, and its cipher for the encrypted learned labels and
            links).
        privacy: the service's ``PrivacyConfig`` (its ``[spotify]`` table), or None for the defaults.
        game_front_fn: ``() -> bool`` (the game gate's ``front_game``); default :func:`foreground_is_game` (D9's
            local read). When given, it is OR-ed with ``local_game_fn`` so a gate that lags the foreground change
            never lets a read start with a game just come to the front.
        local_game_fn: the cheap local read (default :func:`foreground_is_game`; tests pass a stub).
        reader: the window reader (default :class:`DesktopReader`).
        spawn: ``spawn(fn)`` runs a job off the caller's thread (default: a daemon thread); tests run it inline.
    """

    def __init__(self, *, emit: Callable[[dict[str, Any]], Any] | None = None,
                 log: Callable[..., Any] | None = None, store: Any = None, privacy: Any = None,
                 game_front_fn: Callable[[], bool] | None = None, reader: Any = None,
                 settings: SpotifySettings | None = None, spawn: Callable[[Callable[[], None]], Any] | None = None,
                 clock: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic,
                 local_game_fn: Callable[[], bool] | None = None) -> None:
        self._emit = emit
        self._log_fn = log
        self._store = store
        local = local_game_fn or foreground_is_game
        self._game_fns: tuple[Callable[[], bool], ...] = (game_front_fn, local) if game_front_fn else (local,)
        self._reader = reader or DesktopReader()
        self._settings_fixed = settings
        self._section: Any = None
        if settings is None and privacy is not None:
            try:
                from yuki.memory.timeline import PrivacySection

                self._section = PrivacySection(privacy, "spotify", SpotifySettings.from_dict)
            except Exception:  # noqa: BLE001
                self._section = None
        self._spawn = spawn or self._thread
        self._clock = clock
        self._mono = mono
        self._lock = threading.Lock()
        self._busy = False
        self._pending: list[str] = []
        self._seen_pids: set[int] = set()
        self._warm_pid: int | None = None
        self._warm_built = False
        self._warm_failed: tuple[int, float] | None = None
        self._identities: dict[int, dict[str, str]] = {}
        self._read_pids: set[int] = set()
        self._last_slider: float | None = None
        self._last_slider_at: float | None = None
        self._last_context: tuple[str, float] | None = None
        self._known: dict[str, None] = {}
        self._away: dict[str, Any] | None = None
        self._probe_due: float | None = None
        self._layout_state: str | None = None
        self._learned: dict[str, str] | None = None
        self._learned_loaded = False
        self._candidates: list[WordingGuess] = []
        self._status: dict[str, Any] | None = None
        #: When the media read saw Spotify's music start (the hub's ``at``), and whether it was already playing then.
        self._play_at: float | None = None
        self._play_initial = False
        self.counters = {"triggers": 0, "jobs": 0, "reads": 0, "warms": 0, "probes": 0, "held_game": 0,
                         "moments": 0, "links_learned": 0, "labels_learned": 0, "labels_dropped": 0, "errors": 0,
                         "rewarms": 0}
        #: :meth:`stop` was called: no job starts and no moment is written from then on.
        self._stopped = False

    # -- settings, logging, state -----------------------------------------------------------------

    def settings(self) -> SpotifySettings:
        if self._settings_fixed is not None:
            return self._settings_fixed
        if self._section is not None:
            try:
                got = self._section.get()
                if isinstance(got, SpotifySettings):
                    return got
            except Exception:  # noqa: BLE001
                pass
        return SpotifySettings()

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is not None:
            try:
                self._log_fn(type, **fields)
            except Exception:  # noqa: BLE001
                pass

    def _nudge_get(self, key: str) -> str | None:
        try:
            return self._store.nudge_state(key) if self._store is not None else None
        except Exception:  # noqa: BLE001
            return None

    def _nudge_set(self, key: str, value: str | None) -> None:
        try:
            if self._store is not None:
                self._store.set_nudge_state(key, value)
        except Exception:  # noqa: BLE001
            self.counters["errors"] += 1

    def learned_labels(self) -> dict[str, str] | None:
        """The confirmed row wording (``{"play", "pause", "grid", "at"}``), decrypted from
        ``nudge_state['spotify_labels']``; None when none was learned (or no cipher: then it lives in RAM only)."""
        if not self._learned_loaded:
            self._learned_loaded = True
            raw = self._nudge_get(LABELS_KEY)
            cipher = getattr(self._store, "cipher", None)
            try:
                from yuki.memory.crypto import PREFIX

                sealed = bool(raw) and str(raw).startswith(PREFIX)   # a value kept in clear is never trusted
                got = json.loads(cipher.decrypt(raw)) if sealed and cipher is not None else None
                ok = isinstance(got, dict) and isinstance(got.get("play"), str) and isinstance(got.get("pause"), str)
                self._learned = got if ok else None
            except Exception:  # noqa: BLE001 - unreadable (another key, or not encrypted): nothing learned
                self._learned = None
        return self._learned

    def status(self) -> dict[str, Any]:
        """The content-free status the doctor reads (also kept in ``nudge_state['spotify_desktop']``)."""
        if self._status is None:
            raw = self._nudge_get(STATUS_KEY)
            try:
                got = json.loads(raw) if raw else {}
            except ValueError:
                got = {}
            self._status = got if isinstance(got, dict) else {}
        return self._status

    def _save_status(self, **changes: Any) -> None:
        st = self.status()
        st.update(changes)
        self._nudge_set(STATUS_KEY, json.dumps(st, separators=(",", ":")))

    # -- triggers (cheap; any thread) ----------------------------------------------------------------

    def on_layout(self, surfaces: list[dict[str, Any]], foreground: int = 0, at: float | None = None) -> None:
        """The Now board's layout changed: a Spotify window seen for the first time in this process gets its warm-up;
        and when Spotify's place on screen changed (in front, on screen behind, gone from the layout: minimised,
        covered or closed), the daily probe's 60 s one-shot timer (re)starts (no polling: the layout is an event)."""
        try:
            spot = None
            for s in surfaces or []:
                pid = int(s.get("pid") or 0)
                if pid and (pid in self._seen_pids or is_spotify_pid(pid)):
                    spot = s
                    break
            if spot is not None:
                pid = int(spot.get("pid") or 0)
                if pid not in self._seen_pids:
                    if len(self._seen_pids) > 64:
                        self._seen_pids.clear()
                    self._seen_pids.add(pid)
                    if self._warm_pid != pid:
                        self._trigger("appear")
            if spot is None:
                place = "gone" if self._layout_state is not None else None
            else:
                share = spot.get("share")
                place = classify_state(minimised=False, front=int(spot.get("hwnd") or 0) == int(foreground or 0),
                                       share=float(share) if isinstance(share, (int, float)) else None)
            if place != self._layout_state:
                self._layout_state = place
                if place not in (None, FRONT):
                    self._arm_probe(None)
        except Exception:  # noqa: BLE001
            self.counters["errors"] += 1

    def on_front_change(self, old_app: str | None, new_app: str | None, **_facts: Any) -> None:
        """The app in front changed (``MomentHub.subscribe_front``: ``fn(prev_key, new_key, at=, hwnd=, prev_hwnd=,
        by_yuki=, request_id=)``, app keys as the timeline stores them): Spotify left the front → one slider read and
        the probe's clock starts; Spotify came to the front unwarmed → the warm-up. The extra facts are not needed."""
        try:
            me = app_key()
            if (old_app or "").casefold() == me and (new_app or "").casefold() != me:
                self._trigger("leave_front")
            elif (new_app or "").casefold() == me:
                self._away, self._probe_due = None, None
                if not self._warm_built:
                    self._trigger("appear")
        except Exception:  # noqa: BLE001 - never raises into the hub's fan-out
            self.counters["errors"] += 1

    def on_media_change(self, started: Iterable[Any] = (), stopped: Iterable[Any] = (), *, at: float | None = None,
                        **facts: Any) -> None:
        """Media sessions changed (``MomentHub.subscribe_media``: ``fn(started, stopped, at=, initial=)``, each session
        ``{"app_id", "app", "title"}``; the title is never looked at): Spotify's session started playing → the
        play-start read (warming first when needed). ``at`` (when the media read saw it start) becomes the playlist
        moment's ``since``; ``initial`` (it was already playing when the timeline started) is noted on it."""
        try:
            if any(self._is_my_session(a) for a in _app_ids(started)):
                self._play_at = float(at) if isinstance(at, (int, float)) else self._clock()
                self._play_initial = bool(facts.get("initial"))
                self._trigger("play_start")
        except Exception:  # noqa: BLE001
            self.counters["errors"] += 1

    def _is_my_session(self, app_id: str) -> bool:
        """Whether a session is Spotify's: matched to the Spotify window's own process once one was seen (its
        AppUserModelID, else its image), else by the two install forms in the data (PLAN.md L7)."""
        pid = self._warm_pid
        if pid:
            ident = self._identities.get(pid)
            if ident is None:
                ident = process_identity(pid)
                self._identities = {pid: ident}
            return session_matches(app_id, ident)
        return is_spotify_session(app_id)

    def beat(self, now: float | None = None) -> None:
        """The service's 1 s loop: only checks the one-shot probe timer (no read here)."""
        due = self._probe_due
        if due is not None and (self._mono() if now is None else now) >= due:
            self._probe_due = None
            self._trigger("probe_check")

    def stop(self) -> None:
        """The service is stopping (before the moments hub stops): no new job starts, the queued ones are dropped and a
        read still running writes no moment (fWire review r1 L3). Its job thread is a daemon; nothing waits for it."""
        self._stopped = True
        with self._lock:
            self._pending.clear()
        self._probe_due = None

    def _trigger(self, kind: str) -> None:
        if self._stopped:
            return
        s = self.settings()
        if not s.enabled:
            return
        self.counters["triggers"] += 1
        with self._lock:
            if kind not in self._pending:
                self._pending.append(kind)
            if self._busy:
                return
            self._busy = True
        self._spawn(self._drain)

    @staticmethod
    def _thread(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, name="yuki-memory-spotify", daemon=True).start()

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._pending or self._stopped:
                    self._pending.clear()
                    self._busy = False
                    return
                kind = self._pending.pop(0)
            try:
                self.counters["jobs"] += 1
                self._job(kind)
            except Exception as exc:  # noqa: BLE001 - a read never takes the service down
                self.counters["errors"] += 1
                self._log("spotify_read", trigger=kind, status="error", error=type(exc).__name__)

    # -- jobs (the job thread) -----------------------------------------------------------------------

    def _job(self, kind: str) -> None:
        s = self.settings()
        if self._game_front():
            self.counters["held_game"] += 1
            self._log("spotify_read", trigger=kind, status="held_game")
            if kind == "probe_check" and self._away is not None:
                self._reschedule()
            return
        ref = self._reader.find()
        if ref is None:
            self._log("spotify_read", trigger=kind, status="no_window")
            self._away, self._probe_due = None, None
            return
        if ref.pid != self._warm_pid:
            self._warm_pid, self._warm_built = ref.pid, False
            self._last_slider, self._last_slider_at = None, None
            self._save_status(found_at=round(self._clock()), pid_seen=True)
        state = self._reader.state(ref.hwnd)
        if kind in ("appear", "play_start") and state in PROBE_STATES and self._away is None:
            self._arm_probe(state)   # a Spotify that started minimised and never came to the front is probed too
        if kind == "appear":
            self._ensure_warm(ref, state, kind)
            return
        if kind == "play_start":
            self._save_status(session_seen_at=round(self._clock()))
            if not s.library_reads:
                return
            warm, how = self._ensure_warm(ref, state, kind)
            if self._held(kind):
                return

            def play_read() -> SpotifyRead:
                return self._reader.read(ref.hwnd, s.read_budget_s, want=("slider", "library", "links"),
                                         learned=self.learned_labels(), session_playing=True,
                                         known_names=tuple(self._known))

            got = play_read()
            if got.status == "not_built" and how == "warm":
                # marked warm, yet only the frame answered (Chromium dropped its tree, or the mark was stale): warm
                # once now and read again, so this play start's playlist is not lost
                self.counters["rewarms"] += 1
                self._warm_built = False
                warm, how = self._ensure_warm(ref, state, kind)
                if self._held(kind):
                    return
                if how == "ran":
                    got = play_read()
                    how = "rewarmed"
            self._after_read(kind, ref, state, got, warm, how)
            self._remember_links(got.links)
            self._learn(got)   # first: a candidate this read confirms gives this read its play state
            self._context(got, state)
            self._volume(got, state, kind)
            return
        if kind == "leave_front":
            warm, how = self._ensure_warm(ref, state, kind)
            if self._held(kind):
                return
            got = self._reader.read(ref.hwnd, s.read_budget_s, want=("slider",), learned=self.learned_labels())
            self._after_read(kind, ref, state, got, warm, how)
            self._volume(got, state, kind)
            if state in PROBE_STATES:
                self._arm_probe(state)
            return
        if kind == "probe_check":
            self._probe_check(ref, state, s)

    def _arm_probe(self, state: str | None) -> None:
        """(Re)start the daily probe's 60 s one-shot timer for a stretch away from the front (``state`` None: not
        known yet; the timer's check adopts the state it then finds, from the time armed)."""
        s = self.settings()
        if not s.enabled or not s.state_probe:
            return
        try:
            if all(self._probed_today(st) for st in PROBE_STATES):
                return
        except Exception:  # noqa: BLE001
            pass
        self._away = {"state": state, "since": self._mono(), "checks": 0}
        self._probe_due = self._mono() + s.probe_after_s

    def _game_front(self) -> bool:
        """Whether a game is in front: the game gate's answer OR the local read of the foreground program (fails
        closed: no passive read)."""
        for fn in self._game_fns:
            try:
                if fn():
                    return True
            except Exception:  # noqa: BLE001
                return True
        return False

    def _held(self, kind: str) -> bool:
        """A game came to the front while the window warmed: the read waits (logged)."""
        if not self._game_front():
            return False
        self.counters["held_game"] += 1
        self._log("spotify_read", trigger=kind, status="held_game")
        return True

    def _ensure_warm(self, ref: WindowRef, state: str | None, trigger: str) -> tuple[WarmResult | None, str]:
        """Warm the window unless it is warm. ``(result, how)``: ``how`` is ``warm`` (already warm: nothing run),
        ``skipped`` (a warm-up of this window failed less than :data:`WARM_RETRY_S` ago: not run, the read alone says
        what the tree is) or ``ran``."""
        if self._warm_built and self._warm_pid == ref.pid:
            return None, "warm"
        failed = self._warm_failed
        if failed is not None and failed[0] == ref.pid and self._mono() - failed[1] < WARM_RETRY_S:
            return None, "skipped"
        s = self.settings()
        result = self._reader.warm(ref.hwnd, s.warm_budget_s, stop_fn=self._game_front)
        self.counters["warms"] += 1
        # a drawn but unrecognised tree is built too (only its labels are unknown): warming it again would not help
        self._warm_built = bool(result.built or result.drawn)
        if result.stopped and not result.built:
            # a game came to the front: not a window that fails to build (the next trigger warms again)
            self.counters["held_game"] += 1
            status = "held_game"
        else:
            self._warm_failed = None if self._warm_built else (ref.pid, self._mono())
            status = result.status
        self._log("spotify_read", trigger="warm", cause=trigger, window_state=state, **result.counts(),
                  status=status)
        return result, "ran"

    def _after_read(self, trigger: str, ref: WindowRef, state: str | None, got: SpotifyRead,
                    warm: WarmResult | None, how: str = "warm") -> None:
        """Book-keeping and the content-free log of one read. ``how``: what the warm-up did in this job (``warm``,
        ``ran``, ``skipped``, ``rewarmed``: the window was marked warm, read only the frame, was warmed and read
        again)."""
        self.counters["reads"] += 1
        first = ref.pid not in self._read_pids
        self._read_pids.add(ref.pid)
        if got.status == "not_built":
            self._warm_built = False   # the next trigger warms again
        elif got.built:
            self._warm_built, self._warm_failed = True, None   # the tree answered in full: it is warm
        for row in got.rows:
            if row.spotify_owned is False:
                self._known[row.name] = None
        while len(self._known) > KNOWN_NAMES_MAX:
            self._known.pop(next(iter(self._known)))
        # cold: the tree was not built when this job began; None: not known (the warm-up was skipped)
        cold = bool(warm.cold) if warm is not None else (None if how == "skipped" else False)
        self._log("spotify_read", trigger=trigger, window_state=state, cold=cold, warm=how, first=first,
                  labels_candidate=got.candidate is not None, **got.counts())
        self._save_status(last_read={"at": round(self._clock()), "trigger": trigger, "status": got.status,
                                     "window_state": state, "rows": len(got.rows), "slider": got.slider is not None,
                                     "cold": cold, "label_source": got.label_source, "first": first})

    def _context(self, got: SpotifyRead, state: str | None) -> None:
        row = got.playing
        if row is None:
            return
        now = self._clock()
        last = self._last_context
        if last is not None and last[0] == row.name and now - last[1] < CONTEXT_REPEAT_S:
            return
        self._last_context = (row.name, now)
        keep = row.spotify_owned is False   # a Spotify-owned list, or an owner that cannot be parsed: digest only
        uri = self._link_uri(row.name) if keep else None
        facts: dict[str, Any] = {"owner": "spotify" if row.spotify_owned else ("other" if keep else "unknown"),
                                 "link_known": uri is not None, "window_state": state,
                                 "label_source": got.label_source}
        if self._play_initial:
            facts["initial"] = True   # it was already playing when the timeline started
        # ``entity`` and ``uri`` travel in RAM only, to the moments writer, which encrypts them (never logged here);
        # ``since``: when the media read saw the music start (the playlist began between then and this read)
        self._put({"kind": "media_context", "at": now, "app": app_key(), "entity": row.name, "entity_keep": keep,
                   "uri": uri, "since": self._play_at if self._play_at is not None and self._play_at <= now else None,
                   "means": "library_row", "source": "spotify", "evidence": "window_read", "facts": facts})

    def _volume(self, got: SpotifyRead, state: str | None, trigger: str = "leave_front") -> None:
        """Spotify's own slider against the last reading (the leave-front and the play-start reads both read it):
        a change is a ``volume_change`` (means ``in_app``) with ``facts.prev_read_at``, the previous reading's time,
        so the hub can attribute it (D18) over the whole span the change happened in, not only at ``at``."""
        if got.slider is None:
            return
        now = self._clock()
        before, before_at = self._last_slider, self._last_slider_at
        self._last_slider, self._last_slider_at = got.slider, now
        if before is None or abs(before - got.slider) < 0.5:
            return   # the first read is the baseline; an unchanged level is no change
        since = round(before_at, 3) if before_at is not None else None
        self._put({"kind": "volume_change", "at": now, "app": app_key(), "value": got.slider,
                   "value_from": before, "means": "in_app", "source": "spotify", "evidence": "window_read",
                   "since": since,
                   "facts": {"window_state": state, "slider_source": got.slider_source, "trigger": trigger,
                             "prev_read_at": since}})

    def _put(self, moment: dict[str, Any]) -> None:
        if self._emit is None or self._stopped:
            return
        try:
            self._emit(moment)
            self.counters["moments"] += 1
        except Exception:  # noqa: BLE001
            self.counters["errors"] += 1

    # -- row wording in another language (PLAN.md L6) -------------------------------------------------

    def _learn(self, got: SpotifyRead) -> bool:
        """Weigh a read's rows against the learned wording and the candidates (see the module docstring). True when a
        candidate was confirmed by this read (its rows then carry their play state)."""
        raw = got.raw
        if not raw:
            return False
        learned = self.learned_labels()
        if learned:
            if got.label_source not in (None, "none", "learned"):
                self._unlearn("data")              # the data's own wording shows now: it takes over
                return False
            if got.label_source == "learned":
                if judge_wording(raw, learned["play"], learned["pause"]) == "contradict":
                    self._unlearn("two_playing")   # two rows "playing" at once: not the play button
                return False
            # no row in view carries the learned wording (Spotify's language changed?): a confirmed candidate may
            # replace it below; until then it stays
        if got.label_source not in (None, "none"):
            self._candidates = []                  # the data's wording reads this Spotify: nothing to learn
            return False
        confirmed: WordingGuess | None = None
        kept: list[WordingGuess] = []
        for cand in self._candidates:
            verdict = judge_wording(raw, cand.play, cand.pause, cand.rows)
            if verdict == "confirm" and confirmed is None:
                confirmed = cand
            elif verdict == "contradict":
                self._log("spotify_read", trigger="labels", action="dropped", reason="two_playing",
                          candidates=len(self._candidates) - 1)
            else:
                kept.append(cand)
        if confirmed is not None:
            self._candidates = []
            self._keep_learned(confirmed)
            apply_wording(got, confirmed.play, confirmed.pause)
            return True
        new = got.candidate
        if new is not None:
            same = next((c for c in kept if (c.play, c.pause) == (new.play, new.pause)), None)
            if same is not None:
                same.rows.update(new.rows)
                while len(same.rows) > KNOWN_NAMES_MAX:
                    same.rows.pop(next(iter(same.rows)))
            else:
                kept.append(WordingGuess(new.play, new.pause, new.grid, dict(new.rows)))
                self._log("spotify_read", trigger="labels", action="candidate", candidates=len(kept))
        self._candidates = kept[-CANDIDATES_MAX:]
        return False

    def _keep_learned(self, guess: WordingGuess) -> None:
        learned = {"play": guess.play, "pause": guess.pause, "grid": guess.grid, "at": str(round(self._clock()))}
        self._learned, self._learned_loaded = learned, True
        self.counters["labels_learned"] += 1
        cipher = getattr(self._store, "cipher", None)
        if cipher is not None:   # kept encrypted (no cipher: RAM only, never in clear)
            self._nudge_set(LABELS_KEY, cipher.encrypt(json.dumps(learned, ensure_ascii=False)))
        self._save_status(labels={"learned": True, "at": round(self._clock())})
        self._log("spotify_read", trigger="labels", action="confirmed", candidates=0)

    def _unlearn(self, reason: str) -> None:
        self._learned, self._learned_loaded = None, True
        self.counters["labels_dropped"] += 1
        self._nudge_set(LABELS_KEY, None)
        self._save_status(labels={"learned": False, "dropped": reason, "at": round(self._clock())})
        self._log("spotify_read", trigger="labels", action="dropped", reason=reason, candidates=len(self._candidates))

    # -- learned links (encrypted in nudge_state) ----------------------------------------------------

    def _links(self) -> dict[str, dict[str, Any]]:
        raw = self._nudge_get(LINKS_KEY)
        cipher = getattr(self._store, "cipher", None)
        if not raw or cipher is None:
            return {}
        try:
            got = json.loads(cipher.decrypt(raw))
            return got if isinstance(got, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _link_key(self, name: str) -> str | None:
        cipher = getattr(self._store, "cipher", None)
        return cipher.digest("spotify_playlist", norm(name)) if cipher is not None else None

    def _link_uri(self, name: str) -> str | None:
        """The learned ``spotify:playlist:{id}`` of a playlist name (decrypted in this process only), or None."""
        key = self._link_key(name)
        entry = self._links().get(key) if key else None
        uri = entry.get("uri") if isinstance(entry, dict) else None
        return str(uri) if uri and playlist_uri_ok(str(uri)) else None

    def _remember_links(self, links: dict[str, str]) -> None:
        """Keep links the read learned (``{row name: spotify:playlist:{id}}``), encrypted, one write."""
        cipher = getattr(self._store, "cipher", None)
        if not links or cipher is None:
            return
        found = self._links()
        for name, uri in links.items():
            key = self._link_key(name)
            if key is None:
                continue
            entry = found.get(key) or {}
            if entry.get("uri") == uri:
                entry["n"] = int(entry.get("n") or 0) + 1
            else:
                entry = {"name": norm(name), "uri": uri, "n": 1}
                self.counters["links_learned"] += 1
            entry["at"] = round(self._clock())
            found[key] = entry
        if len(found) > KNOWN_NAMES_MAX:   # bounded: the least recently seen go first
            for key in sorted(found, key=lambda k: found[k].get("at") or 0)[: len(found) - KNOWN_NAMES_MAX]:
                found.pop(key, None)
        self._nudge_set(LINKS_KEY, cipher.encrypt(json.dumps(found, ensure_ascii=False)))

    # -- the daily state probe -----------------------------------------------------------------------

    def _reschedule(self) -> None:
        away = self._away
        if away is None:
            return
        away["checks"] = int(away.get("checks") or 0) + 1
        if away["checks"] >= PROBE_CHECKS:
            self._away, self._probe_due = None, None
            return
        self._probe_due = self._mono() + self.settings().probe_after_s

    def _probed_today(self, state: str) -> bool:
        probes = self.status().get("probes") or {}
        return (probes.get(state) or {}).get("day") == datetime.fromtimestamp(self._clock()).strftime("%Y-%m-%d")

    def _probe_check(self, ref: WindowRef, state: str | None, s: SpotifySettings) -> None:
        away = self._away
        if away is None or state is None or state == FRONT or not s.state_probe:
            self._away, self._probe_due = None, None
            return
        if away.get("state") is None:
            away["state"] = state   # armed by a layout change: in this state since then
        if state != away.get("state"):
            away.update(state=state, since=self._mono())
            self._reschedule()
            return
        if state in PROBE_STATES and not self._probed_today(state) \
                and self._mono() - float(away.get("since") or 0.0) >= s.probe_after_s:
            self._probe(ref, state, s)
        if any(not self._probed_today(st) for st in PROBE_STATES):
            self._reschedule()
        else:
            self._away, self._probe_due = None, None

    def _probe(self, ref: WindowRef, state: str, s: SpotifySettings) -> None:
        self.counters["probes"] += 1
        result = self._reader.warm(ref.hwnd, s.warm_budget_s, stop_fn=self._game_front)
        if self._held("probe"):
            return
        got = self._reader.read(ref.hwnd, s.read_budget_s, want=("slider", "library"),
                                learned=self.learned_labels())
        self._learn(got)   # a row flip seen here confirms a wording candidate too (nothing else is kept)
        probes = dict(self.status().get("probes") or {})
        probes[state] = {"day": datetime.fromtimestamp(self._clock()).strftime("%Y-%m-%d"), "status": got.status,
                         "rows": len(got.rows), "slider": got.slider is not None, "cold": bool(result.cold),
                         "built": bool(result.built)}
        self._save_status(probes=probes)
        counts = got.counts()
        counts.pop("links", None)
        self._log("spotify_probe", probe_state=state, cold=bool(result.cold), warm_ms=round(result.ms, 1),
                  elements_before=result.before, **counts)

    def stats(self) -> dict[str, Any]:
        return {**self.counters, "warm": self._warm_built, "known_names": len(self._known),
                "probe_pending": self._probe_due is not None, "label_candidates": len(self._candidates)}


def learned_links(store: Any) -> dict[str, str]:
    """``{playlist name: spotify:playlist:{id}}`` learned from links Spotify showed (decrypted in this process; for
    the skills' out-of-view route). ``{}`` when there are none."""
    cipher = getattr(store, "cipher", None)
    try:
        raw = store.nudge_state(LINKS_KEY)
        found = json.loads(cipher.decrypt(raw)) if raw and cipher is not None else {}
    except Exception:  # noqa: BLE001
        return {}
    return {str(v.get("name")): str(v.get("uri")) for v in found.values()
            if isinstance(v, dict) and v.get("name") and v.get("uri")}


#: The moments hub's own keyword arguments: a fact under one of these names would collide with them.
_HUB_KWARGS = frozenset({"at", "link", "keep_name", "since", "means", "front", "yuki_context"})


def hub_emit(hub: Any) -> Callable[[dict[str, Any]], None]:
    """``emit`` for :class:`SpotifyWatch` writing through the moments hub's live inputs (``yuki.memory.moments``,
    fMoments' ``MomentHub``):

    * ``media_context`` → ``hub.media_context(app, name, at=, link=<the learned spotify: URI or None>,
      keep_name=<False: digest only>, since=<when the music started>, **code-word facts)``;
    * ``volume_change`` (means ``in_app``) → ``hub.volume_change(app, value_from, value, means="in_app", at=,
      since=<the previous slider reading>, **code-word facts)``.

    The playlist name and its link go to the hub in RAM (it encrypts and digests them); facts are code words, numbers
    and booleans only (the hub drops anything else). A hub that raises is counted by the watcher (``errors``)."""
    def emit(moment: dict[str, Any]) -> None:
        facts = {k: v for k, v in (moment.get("facts") or {}).items()
                 if k not in _HUB_KWARGS and k != "prev_read_at" and v is not None}
        kind = moment.get("kind")
        if kind == "media_context":
            name = str(moment.get("entity") or "")
            if name:
                hub.media_context(moment.get("app"), name, at=moment.get("at"), link=moment.get("uri") or None,
                                  keep_name=bool(moment.get("entity_keep")), since=moment.get("since"), **facts)
        elif kind == "volume_change" and isinstance(moment.get("value"), (int, float)):
            hub.volume_change(moment.get("app"), moment.get("value_from"), float(moment["value"]),
                              means=str(moment.get("means") or "in_app"), at=moment.get("at"),
                              since=moment.get("since"), **facts)

    return emit


__all__ = [
    "COVERED", "DesktopReader", "El", "FRONT", "Labels", "MINIMISED", "PROBE_STATES", "Row", "SpotifyRead",
    "SpotifySettings", "SpotifyWatch", "Step", "UiaTree", "VISIBLE", "WarmResult", "WindowRef", "WordingGuess",
    "all_labels", "app_key", "apply_wording", "classify_state", "data", "find_window", "foreground_is_game", "hub_emit",
    "is_spotify_pid", "is_spotify_session", "judge_wording", "learn_row_wording", "learned_links", "library", "links",
    "local_find", "playlist_uri", "playlist_uri_ok", "process_identity", "process_image", "read", "read_tree",
    "row_wording_candidate", "run_uia", "session_matches", "slider", "spotify_session", "warm", "warm_tree", "window",
    "window_state",
]
