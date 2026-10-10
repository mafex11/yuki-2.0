"""Spotify and audio skills through Spotify's own desktop window (PLAN.md D0, D4, D17, D19; Phase S-1: ``volume``
and ``spotify.playlist``; evidence ``docs/research/audit-2026-10-09/round4/spotify-desktop-route.md``).

**Zero setup.** Nothing here needs an account, a key, a token or a sign-in: Spotify's window is driven through UI
Automation patterns (Invoke, RangeValue, Value), Windows' media session (play only, never a toggle), Core Audio (the
mixer and the PC's volume), Spotify's own ``spotify:`` links Yuki learned from links Spotify showed, and Spotify's own
Ctrl+Up / Ctrl+Down only with Spotify in front and no game in front.

This module is the driver the connection files name (``via = "driver"``, ``driver = "spotify_route"``):
``connections/spotify.toml`` (``spotify.playlist``, the shared Spotify data: request budgets, the mixer link) and
``connections/audio.toml`` (``volume``). Its :data:`ROUTES` are the rungs, its :data:`PROBES` the done-checks and
re-checks, and :func:`window_state` the ``game = "states"`` hook (which window states a no-focus rung may run in with a
game in front is route data, ``game_states``, set by the live checks; none is proven yet).

What every Spotify rung does, in order:

1. **This PC first** (M2): when Spotify's media session says playing and ``Spotify.exe`` has no active render session
   on this PC for :data:`THIS_PC_S`, Spotify is playing on another device (Spotify Connect: its window controls that
   device). Volume and playlist then say so and do nothing (Q11's default; outcome ``other_device``, never "done",
   never counted against a rung). The check runs with no window too (closed to the tray, its session still reports
   the phone), before any link or launch, and again once a window Yuki started appears, before its press.
2. **Warm-up** (H2): a Chromium window answers with its frame only until its child windows are asked; a window not
   seen built from this process yet is warmed within the request's own time (at most :data:`REQUEST_WARM_S`), and a
   window that doesn't build says "couldn't".
3. **By name inside its container** (M5): the volume slider inside the Now playing bar, a playlist's "Play {name}"
   button inside its row of DataGrid "Your Library" (labels are data in ``yuki/connect/sites/spotify.toml``, English
   ships; a learned row wording or the slider's shape for another language). ``FindAll`` from the window root, never
   ``ElementFromPoint``, never an ``IsOffscreen`` filter. Two rows with one name: asked, never guessed (the options
   name each row's owner, and an answer is matched back to its row by that whole option). A press on a Spotify that
   isn't in front is confirmed by its row within :data:`FLIP_WAIT_S`; a row that never flips is "not taken" (not run,
   so the restore rung may run and the Library rung isn't charged for a backgrounded window).
4. **No focus, no keys, no pointer on a no-focus rung** (H1): the slider moves by ``RangeValue.SetValue`` only
   (:func:`yuki.actions.slider.drive` with ``press=None``, so its focus-and-arrow-keys branch is unreachable); a row by
   ``Invoke``. The rungs that take focus (Spotify's keys, restoring a minimised Spotify, opening a learned link,
   launching Spotify) are separate routes whose data says ``game = "never"``, and every focus, key, link or launch goes
   through Yuki's guarded actions (``launch.focus_window``, ``input.hotkey``, ``engine.open_uri``,
   ``launch.launch_app``: each refuses with a game in front, D17).
5. **"Done" only on a seen result** (D19): a playlist is done when its row (or, after the link route, the page's
   button) reads "Pause {name}", Spotify's session says playing **and** Spotify sounds on this PC; the re-check reads
   the same three a few seconds later (no new track required, so resuming the paused current playlist is a true
   done). A row that flips with no sound on this PC is ``None`` with the plain line "it's playing on another device".
   A row reading "Pause {name}" while the session is paused is a stale label: the session's ``play`` (never Invoke,
   never a toggle). Spotify's volume is done on the slider's own read-back (and re-read 5 s later, "same source").

Volume beyond Spotify (one ``volume`` skill, D2): another app's level through the Windows mixer, the PC's level
through Core Audio, Yuki's own voice through its own mixer entry. **Spotify's own volume is never faked with the
mixer:** the mixer is a route for "Spotify's own volume" only when ``[spotify.mixer]`` records that Spotify's slider
moves its mixer entry **and** the mixer entry moves the slider (M6; and after each such set the slider is read back
when it can be read); a mute goes through the mixer (Spotify's own Mute button is a toggle whose state can't be read),
and a user whose usual control for Spotify is ``mixer`` gets the mixer.

Logs are content-free (``spotify_read`` with ``trigger`` ``skill``: the window state, warm, counts, label source;
route facts are code words and numbers). Names, links and ids stay in RAM; a playlist name is said aloud in the done
line and kept only as a keyed digest (``skill_runs.entity_digest``, D18's match for the memory side's
``media_context`` read).

The live desktop is reached only through :class:`Desk` (:data:`DESK`); the tests replace it with a stand-in.
Heavy imports are lazy (``import yuki.skills.spotify_route`` loads the standard library only).
"""

from __future__ import annotations

import threading
import time
import tomllib
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

#: The connection file whose ``[spotify]`` table this module reads (budgets, the mixer link).
CONNECTION_FILE = Path(__file__).with_name("connections") / "spotify.toml"

#: A cold window's warm-up inside a request, at most (PLAN.md D4, H2: within the request's own time, <= 2.5 s).
REQUEST_WARM_S = 2.5
#: Spotify's session playing with no active render session of Spotify.exe for this long: another device (M2).
THIS_PC_S = 2.0
#: Polling step while a skill waits (C.5: <= 6 s, 0.25 s steps; only while a skill waits).
POLL_S = 0.25
#: One skill read of Spotify's window on its own UIA thread (a warm window answers in ~0.1-0.3 s).
READ_S = 0.6
#: A slider set's whole UIA job (the RangeValue set, its read-back and hold re-read; slider.drive's own bounds), at
#: most; bounded by the request's time left, never below :data:`SET_FLOOR_S` (slider.drive's 1.5 s read-back and 0.5 s
#: hold re-read plus the tree read: a set cut short in the middle could only say "couldn't confirm").
SET_S = 4.0
SET_FLOOR_S = 2.5
#: A no-focus press on a Spotify that isn't in front (minimised or behind other windows): how long its row is re-read
#: for the flip to "Pause {name}" before the press counts as not taken (row reads only; the restore rung may then run).
FLIP_WAIT_S = 1.2
#: How long the link route waits for the playlist page, and the search route for its box and its rows.
PAGE_WAIT_S = 3.0
SEARCH_WAIT_S = 1.5
#: Spotify's own volume keys: presses at most, and the wait before each read-back.
KEYS_MAX = 12
KEY_WAIT_S = 0.15
#: How far a level may be from the one asked (points of 0-100).
LEVEL_TOLERANCE = 2.0
#: ``slider.drive``'s read-back bound and hold re-read (``None``: its own defaults; tests shorten them).
SLIDER_SETTLE_S: float | None = None
SLIDER_HOLD_S: float | None = None

ASK_AMOUNT = "Louder, quieter, or to what level?"
LINE_OTHER = "Spotify is playing on another device, so I left it."
LINE_OTHER_VOLUME = "Spotify is playing on another device, so I left its volume."
LINE_OTHER_AFTER = ("Spotify took it, but there's no sound from Spotify on this PC: it's playing on another device. "
                    "Say pause to stop it.")
LINE_PLAY_UNCONFIRMED = "I pressed play on it, but I couldn't confirm it's playing."
LINE_ALREADY_UNCONFIRMED = "Spotify shows it as already playing, but I couldn't confirm that, so I left it."
LINE_VOLUME_UNCONFIRMED = "I moved it, but I couldn't confirm the new level."
LINE_KEYS_SHORT = "I moved Spotify's volume to {now:g}%, but ran out of time before {target:g}%."

#: Route variants whose ``game_states`` say where the no-focus Spotify rungs were proven (the doctor's line).
PROVEN_ROUTES = (("volume", "spotify_in_app"), ("spotify.playlist", "library"))
STATES = ("covered", "visible", "minimised")


# ---------------------------------------------------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------------------------------------------------

_data_lock = threading.Lock()
_data_cache: tuple[tuple[float, int], dict[str, Any]] | None = None


def spotify_data() -> dict[str, Any]:
    """The ``[spotify]`` table of ``connections/spotify.toml`` (re-read when the file changes; ``{}`` when it can't be
    read): the request budgets, ``launch_args`` and ``[spotify.mixer]``."""
    global _data_cache
    try:
        st = CONNECTION_FILE.stat()
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        return {}
    with _data_lock:
        if _data_cache is not None and _data_cache[0] == stamp:
            return _data_cache[1]
    try:
        table = tomllib.loads(CONNECTION_FILE.read_text(encoding="utf-8")).get("spotify")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        table = None
    got = dict(table) if isinstance(table, Mapping) else {}
    with _data_lock:
        _data_cache = (stamp, got)
    return got


def _number(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(spotify_data().get(name, default))))
    except (TypeError, ValueError):
        return default


def mixer_linked() -> bool:
    """Whether Spotify's slider and its mixer entry are recorded as one control **both ways** (F live check 1: the
    slider moves the mixer; S-1: the mixer moves the slider). Only then is the mixer a route for Spotify's own volume
    (M6)."""
    mixer = spotify_data().get("mixer")
    return isinstance(mixer, Mapping) and mixer.get("slider_moves_mixer") is True \
        and mixer.get("mixer_moves_slider") is True


def proven(loaded: Any = None) -> dict[str, Any]:
    """What the live checks proved, from the data (the doctor's "Spotify routes proven" line): per skill, the window
    states its no-focus Spotify rung may run in with a game in front, and whether the mixer is linked both ways."""
    if loaded is None:
        from yuki.skills.catalog import load

        loaded = load()
    out: dict[str, Any] = {}
    for key, variant in PROVEN_ROUTES:
        skill = loaded.get(key) if loaded is not None else None
        route = next((r for r in (skill.routes if skill else ()) if r.variant == variant), None)
        states = set(route.game_states) if route is not None and route.game == "states" else set()
        out[key] = {s: s in states for s in STATES}
    out["mixer_linked"] = mixer_linked()
    return out


def _fold(text: Any) -> str:
    """A name compared as an identity (case and spacing folded), never by meaning."""
    return " ".join(str(text or "").split()).casefold()


def _sd() -> Any:
    from yuki.connect import spotify_desktop

    return spotify_desktop


def app_key() -> str:
    """Spotify's app key (``spotify``), as the timeline and the moments write it."""
    try:
        return _sd().app_key()
    except Exception:  # noqa: BLE001
        return "spotify"


def _langs() -> list[Any]:
    return list(_sd().all_labels())


def extra_control(key: str) -> list[tuple[Any, str, str, tuple[Any, ...]]]:
    """A control of ``[labels.<lang>.controls]`` that the reader itself doesn't use (the Library search button and
    box), per language: ``[(labels, role, name, container path)]``; ``[]`` when the data has none (the route that
    needs it then says "couldn't")."""
    sd = _sd()
    tables = (sd.data().get("labels") or {})
    out = []
    for lab in _langs():
        raw = ((tables.get(lab.lang) or {}).get("controls") or {}).get(key)
        try:
            role, name, container = str(raw[0]), str(raw[1]), str(raw[2])
        except (IndexError, TypeError):
            continue
        path = sd._steps((tables.get(lab.lang) or {}).get(container)) if container else ()
        out.append((lab, role, name, path))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# The live desktop (every call that could act goes through Yuki's guarded actions)
# ---------------------------------------------------------------------------------------------------------------------


class Desk:
    """The live desktop as the routes use it. Reads are read-only; ``press`` / ``live_slider`` are UI Automation
    patterns (no focus, no key, no pointer); ``focus``, ``hotkey``, ``open_uri`` and ``launch`` are Yuki's own guarded
    actions (D17: each refuses with a game in front). Tests pass a stand-in with the same methods."""

    # -- reads ---------------------------------------------------------------------------------------------------------

    def window(self) -> Any:
        """Spotify's top window (``WindowRef``: hwnd, pid, minimised), or None."""
        return _sd().find_window()

    def state(self, hwnd: int) -> str | None:
        return _sd().window_state(hwnd)

    def warm(self, hwnd: int, budget_s: float) -> Any:
        # a warm-up a request asked for runs with a game in front too: asking a non-game window's children for their
        # tree is neither input nor focus (D17: a requested skill may read and use patterns on a non-game window)
        return _sd().warm(hwnd, budget_s)

    def uia(self, job: Callable[[Any, dict], None], budget_s: float) -> tuple[dict, bool]:
        return _sd().run_uia(job, budget_s, name="yuki-spotify-skill")

    def spotify_status(self, pid: int | None) -> tuple[str, str]:
        """Spotify's media session: ``(status, app_id)``; status ``playing`` / ``paused`` / ``stopped`` / ...,
        ``none`` (no session) or ``unread``."""
        try:
            from yuki.perception.system import media_sessions

            rows = media_sessions(timeout_s=0.3)
        except Exception:  # noqa: BLE001
            return "unread", ""
        s = _sd().spotify_session([r for r in rows or [] if isinstance(r, dict)], pid=pid or None)
        if not s:
            return "none", ""
        return (str(s.get("status") or "").casefold() or "unknown"), str(s.get("app_id") or "")

    def render_active(self) -> bool | None:
        """Whether Spotify.exe has an active render session on this PC (it sounds here); None: couldn't read."""
        try:
            from yuki.perception.audio import render_sessions

            rows = render_sessions()
        except Exception:  # noqa: BLE001
            return None
        sd = _sd()
        image = sd.process_image().casefold()
        mine = [r for r in rows if sd._image_of(int(r.get("pid") or 0)).casefold() == image]
        return any(str(r.get("state") or "") == "active" for r in mine)

    def foreground(self) -> int:
        try:
            import ctypes

            return int(ctypes.windll.user32.GetForegroundWindow() or 0)
        except Exception:  # noqa: BLE001
            return 0

    def running(self) -> bool:
        """Whether Spotify's program runs at all (a window closed to the tray counts)."""
        try:
            import psutil

            image = _sd().process_image().casefold()
            return any(str(p.info.get("name") or "").casefold() == image for p in psutil.process_iter(["name"]))
        except Exception:  # noqa: BLE001
            return False

    def mixer_level(self, app: str) -> float | None:
        try:
            from yuki.actions.app_volume import matching, sessions

            found = matching(sessions(), app)
            return max(s.level for s in found) if found else None
        except Exception:  # noqa: BLE001
            return None

    def master_level(self) -> float | None:
        try:
            from yuki.perception.audio import master_volume

            got = master_volume() or {}
            return float(got["level"]) if "level" in got else None
        except Exception:  # noqa: BLE001
            return None

    def own_process(self) -> str:
        try:
            import psutil

            return str(psutil.Process().name())
        except Exception:  # noqa: BLE001
            return ""

    # -- patterns (no focus, no key, no pointer) -----------------------------------------------------------------------

    def press(self, ref: Any, pattern: str, value: Any = None) -> bool:
        from yuki.skills.uia_route import apply_pattern

        return apply_pattern(ref, pattern, value)

    def live_slider(self, ref: Any, automation: Any) -> Any:
        """The slider as :func:`yuki.actions.slider.drive` moves it: RangeValue read and set (its ``focus`` is
        guarded too, and ``drive`` never reaches it with ``press=None``)."""
        from yuki.actions.slider import _PATTERN_RANGE_VALUE, _LiveSlider
        from yuki.perception.tree import _uia_core

        pattern = ref.GetCurrentPattern(_PATTERN_RANGE_VALUE).QueryInterface(_uia_core().IUIAutomationRangeValuePattern)
        return _LiveSlider(ref, pattern, automation)

    # -- Core Audio (no key) -------------------------------------------------------------------------------------------

    def app_volume(self, app: str, *, level: float | None, change: float | None, mute: bool | None) -> Any:
        from yuki.actions.app_volume import set_app_volume

        return set_app_volume(app, level=level, change=change, mute=mute)

    def master(self, *, level: float | None, change: float | None, mute: bool | None) -> dict[str, Any] | None:
        from yuki.perception.audio import set_master_volume

        return set_master_volume(level=level, change=change, mute=mute)

    def media(self, verb: str, app_id: str, log: Callable[..., Any] | None = None) -> Any:
        """The media session's own command on Spotify's session (``play``: never a toggle); logged ``media_command``
        (verb, the aimed app key, outcome)."""
        from yuki.actions.media import command

        return command(verb, app_id, timeout_s=1.0, log=log)

    # -- focus-taking (guarded: each refuses with a game in front) -----------------------------------------------------

    def focus(self, hwnd: int) -> Any:
        from yuki.actions import launch

        return launch.focus_window(int(hwnd))

    def hotkey(self, keys: list[str], hwnd: int) -> Any:
        from yuki.actions import input as keys_mod

        return keys_mod.hotkey(*keys, expect_hwnd=int(hwnd))

    def open_uri(self, uri: str) -> Any:
        from yuki.skills.engine import open_uri

        return open_uri(uri)

    def launch(self, args: list[str], timeout_s: float = 8.0) -> Any:
        """Spotify started (guarded: refused with a game in front), its window waited for at most ``timeout_s`` (the
        request's time left); no content wait (the route's own warm-up reads the window within its time)."""
        from yuki.actions import launch

        image = _sd().process_image()
        name = image[:-4] if image.casefold().endswith(".exe") else image
        return launch.launch_app(name, args=list(args), timeout_s=max(0.5, float(timeout_s)), content_timeout_s=0.0)

    def sleep(self, s: float) -> None:
        time.sleep(s)


#: The desktop the routes use (tests replace it).
DESK: Any = Desk()


def _refused(result: Any) -> bool:
    details = getattr(result, "details", None)
    return isinstance(details, Mapping) and details.get("refused") == "game"


# ---------------------------------------------------------------------------------------------------------------------
# Looking inside Spotify's window (pure functions over a spotify_desktop.UiaTree; one UIA job each)
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class Look:
    """What a read of Spotify's window found for one playlist name (or the slider). ``status``: ``ok``,
    ``not_built`` (frame only: not warm), ``no_library`` (player controls, no grid: collapsed Library or full-screen
    view), ``no_rows`` (a grid with no rows), ``not_in_view`` (rows, none with that name), ``ambiguous`` (two fit:
    asked, never guessed), ``no_wording`` (a row with neither known play nor pause wording: a missing label),
    ``missing`` / ``timeout`` / ``error``. Names and elements stay in RAM."""

    status: str
    name: str = ""
    state: str | None = None          # "play" (the button reads "Play {name}") or "pause"
    rows: int = 0
    owners: list[str] = field(default_factory=list)
    button: Any = None
    where: str = ""                   # "grid" or "page"
    source: str = ""                  # english / <lang> / learned / shape
    pressed: bool | None = None
    slider: float | None = None       # percent 0-100
    details: dict[str, Any] = field(default_factory=dict)
    calls: int = 0
    ms: float = 0.0

    def counts(self) -> dict[str, Any]:
        """Content-free: code words and counts only."""
        return {"status": self.status, "rows": self.rows, "row": self.state or "", "where": self.where,
                "label_source": self.source, "pressed": self.pressed, "slider_found": self.slider is not None,
                "uia_calls": self.calls}


def _wordings(lab: Any, learned: Mapping[str, str] | None) -> list[tuple[str, str, str]]:
    """``[(play template, pause template, source)]``: the data's wording first, then the learned one."""
    out = []
    if lab is not None and lab.row_play and lab.row_pause:
        out.append((lab.row_play, lab.row_pause, "english" if lab.lang == "en" else lab.lang))
    if learned and learned.get("play") and learned.get("pause"):
        out.append((str(learned["play"]), str(learned["pause"]), "learned"))
    return out


def _grid(tree: Any, langs: list[Any], learned: Mapping[str, str] | None) -> tuple[Any, Any]:
    """Your Library's grid (its subtree cached) and the labels that found it: by its label, else a learned grid
    name, else the only DataGrid shaped like it."""
    sd = _sd()
    for lab in langs:
        grids = sd._container(tree, lab.library_grid)
        if grids:
            return grids[0], lab
    lab0 = langs[0] if langs else None
    if learned and learned.get("grid"):
        grids = [tree.subtree(e) for e in tree.find("DataGrid", str(learned["grid"]))]
        if grids:
            return grids[0], lab0
    if lab0 is not None:
        shaped = sd._structural_grids(tree, lab0)
        if shaped:
            return shaped[0], lab0
    return None, None


def _groups(grid: Any, lab: Any) -> list[Any]:
    path = (lab.row_path if lab is not None else ()) or ("DataItem", "DataItem", "Group")
    level = [grid]
    for role in path:
        level = [k for e in level for k in (e.kids or []) if k.role == role]
    return [g for g in level if g.name]


def _owner_of(group: Any, lab: Any) -> str | None:
    sd = _sd()
    for b in sd.local_find(group, "Button"):
        owner = sd._owner(b.name, group.name, lab) if lab is not None else None
        if owner is not None:
            return owner
    return None


def option_label(name: str, owner: str) -> str:
    """How the two-rows question offers one row (Yuki's own wording, built from the row's name and its owner as
    Spotify shows them); an answer is matched back to a row by this exact label (an identity, never by meaning)."""
    return f"{name} by {owner}"


def look_row(tree: Any, want: str, langs: list[Any] | None = None,
             learned: Mapping[str, str] | None = None) -> Look:
    """The Library row named ``want`` (identity, case and spacing folded) inside DataGrid "Your Library", and what
    its own button reads; or the row whose two-rows option (:func:`option_label`) is ``want`` (the answer to that
    question). Only that row's own button is ever returned (never a Home "Play" or a "Pause {name}" outside the
    grid)."""
    sd = _sd()
    langs = langs if langs is not None else _langs()
    if not sd._built(tree, langs):
        return Look("not_built")
    grid, lab = _grid(tree, langs, learned)
    if grid is None:
        return Look("no_library")
    groups = _groups(grid, lab)
    if not groups:
        return Look("no_rows")
    hits = [g for g in groups if _fold(g.name) == _fold(want)]
    if not hits:
        # the answer to the two-rows question: one of the options it offered, the whole label as an identity
        hits = [g for g in groups if (o := _owner_of(g, lab)) is not None
                and _fold(option_label(g.name, o)) == _fold(want)]
    if not hits:
        return Look("not_in_view", rows=len(groups))
    if len(hits) > 1:
        owners = [_owner_of(g, lab) for g in hits]
        return Look("ambiguous", rows=len(groups), name=hits[0].name,
                    owners=[o for o in owners if o] if all(owners) else [])
    group = hits[0]
    buttons = sd.local_find(group, "Button")
    for play, pause, source in _wordings(lab, learned):
        p = [b for b in buttons if b.name == play.replace("{name}", group.name)]
        q = [b for b in buttons if b.name == pause.replace("{name}", group.name)]
        if len(p) == 1 and not q:
            return Look("ok", name=group.name, state="play", button=p[0], where="grid", source=source,
                        rows=len(groups))
        if len(q) == 1 and not p:
            return Look("ok", name=group.name, state="pause", button=q[0], where="grid", source=source,
                        rows=len(groups))
    return Look("no_wording", name=group.name, rows=len(groups))


def _same(tree: Any, a: Any, b: Any) -> bool:
    try:
        return bool(tree.automation.CompareElements(a.ref, b.ref))
    except Exception:  # noqa: BLE001
        return False


#: How many containers up from a page's play button its page's own link is looked for (review r2 L2); the climb stops
#: sooner at the nearest container holding a link, or at app chrome.
PAGE_CLIMB = 8


def _page_link(tree: Any, button: Any, uri: str, langs: list[Any]) -> str:
    """Whether a page's play button belongs to the page of the learned link (review r2 L2: an identity check scoped to
    the button's own container, so a same-named album or artist header still in view while Spotify navigates is never
    pressed). One cached read of the window; climbing from the button, the nearest container that holds a Hyperlink
    outside every DataGrid must hold one whose value is the data's playlist link template with the learned id
    (round4 §2: a playlist page's header carries its own link; another page's header carries that page's links). The
    climb stops at app chrome, a container holding the Library grid (its label) or the volume slider (its 0-1 range):
    the Now Playing view's link to the playing context sits beside the page, never in its container.
    ``ok``; ``other`` (the nearest links are another page's); ``none`` (no link in its container); ``unread``."""
    sd = _sd()
    whole = tree.subtree(sd.El(role="", name="", ref=tree.root))
    parent: dict[int, Any] = {}
    for el in (whole, *whole.walk()):
        for kid in el.kids or []:
            parent[id(kid)] = el
    me = next((e for e in whole.walk() if e.role == button.role and e.name == button.name and _same(tree, e, button)),
              None)
    if me is None:
        return "unread"
    in_grid = {id(x) for g in whole.walk() if g.role == "DataGrid" for x in g.walk()}
    s = sd.data().get("slider") or {}
    lo, hi = float(s.get("range_min", 0.0)), float(s.get("range_max", 1.0))
    grids = {(step.role, step.name) for lab in langs for step in lab.library_grid[:1]}

    def chrome(el: Any) -> bool:
        return any((e.role, e.name) in grids or (e.role == "Slider" and e.rng is not None
                                                 and abs(e.rng[0] - lo) < 1e-6 and abs(e.rng[1] - hi) < 1e-6)
                   for e in el.walk())

    node = parent.get(id(me))
    for _ in range(PAGE_CLIMB):
        if node is None or node is whole or chrome(node):
            return "none"
        links = [e for e in node.walk() if e.role == "Hyperlink" and id(e) not in in_grid]
        if links:
            return "ok" if any(sd.playlist_uri(e.value) == uri for e in links) else "other"
        node = parent.get(id(node))
    return "none"


def look_page(tree: Any, want: str, langs: list[Any] | None = None,
              learned: Mapping[str, str] | None = None, link: str | None = None) -> Look:
    """A playlist page's own button for ``want`` (after the link route): a Button reading "Play {name}" or "Pause
    {name}" (the data's or the learned wording, names folded) that sits outside every DataGrid (the Library grid and
    Home's card grids are DataGrids; Home's 8 bare "Play" buttons never match a "Play {name}"). Exactly one, else
    ``missing`` / ``ambiguous``. With ``link`` (the learned ``spotify:playlist:{id}`` just opened), the button counts
    only when its own container shows that link (:func:`_page_link`), else ``not_shown`` (the page isn't in view
    yet)."""
    sd = _sd()
    langs = langs if langs is not None else _langs()
    if not sd._built(tree, langs):
        return Look("not_built")
    buttons = tree.find("Button")
    inside = [b for g in tree.find("DataGrid") for b in sd.local_find(tree.subtree(g), "Button")]
    lab = langs[0] if langs else None
    for play, pause, source in _wordings(lab, learned):
        fits = [(b, "play") for b in buttons if _fold(b.name) == _fold(play.replace("{name}", want))]
        fits += [(b, "pause") for b in buttons if _fold(b.name) == _fold(pause.replace("{name}", want))]
        # element identity is asked only of a grid button with the same name (one element has one name): a cached
        # name compare first keeps CompareElements calls to the few that could be the same element
        outside = [(b, s) for b, s in fits if not any(g.name == b.name and _same(tree, b, g) for g in inside)]
        if len(outside) == 1:
            button, state = outside[0]
            if link:
                shown = _page_link(tree, button, link, langs)
                if shown != "ok":
                    return Look("not_shown", name=want, details={"page_link": shown})
            tpl = play if state == "play" else pause
            head, _, tail = tpl.partition("{name}")
            name = button.name[len(head): len(button.name) - len(tail) if tail else None] or want
            return Look("ok", name=name, state=state, button=button, where="page", source=source)
        if len(outside) > 1:
            return Look("ambiguous", name=want)
    return Look("missing")


def look_slider(tree: Any, langs: list[Any] | None = None) -> tuple[Any, str]:
    """Spotify's volume slider: the data's Slider inside the Now playing bar (a RangeValue element), else the
    window's only Slider with the data's 0-1 range (another language). ``(element, source)`` or ``(None, why)``."""
    sd = _sd()
    langs = langs if langs is not None else _langs()
    for lab in langs:
        control = lab.controls.get("volume")
        if not control:
            continue
        bars = sd._container(tree, lab.path(control[2]) or lab.now_playing_bar)
        hits = [e for b in bars for e in sd.local_find(b, control[0], control[1]) if e.rng is not None]
        if len(hits) == 1:
            return hits[0], "english" if lab.lang == "en" else lab.lang
        if len(hits) > 1:
            return None, "ambiguous"
    s = sd.data().get("slider") or {}
    lo, hi = float(s.get("range_min", 0.0)), float(s.get("range_max", 1.0))
    hits = [e for e in tree.find("Slider") if e.rng is not None
            and abs(e.rng[0] - lo) < 1e-6 and abs(e.rng[1] - hi) < 1e-6]
    if len(hits) == 1:
        return hits[0], "shape"
    return None, "ambiguous" if hits else "missing"


# ---------------------------------------------------------------------------------------------------------------------
# UIA jobs (each on its own thread through Desk.uia; never acts after the caller stopped waiting)
# ---------------------------------------------------------------------------------------------------------------------


def _learned_wording(memory: Any) -> dict[str, str] | None:
    """The row wording the memory side learned for a Spotify in another language (``nudge_state['spotify_labels']``,
    encrypted; a value kept in clear is never trusted), or None."""
    if memory is None:
        return None
    from yuki.skills import engine

    def read(client: Any) -> Any:
        import json

        from yuki.memory.crypto import PREFIX

        store = engine._store_of(client)
        raw = store.nudge_state(_sd().LABELS_KEY)
        cipher = getattr(store, "cipher", None)
        if not raw or cipher is None or not str(raw).startswith(PREFIX):
            return None
        got = json.loads(cipher.decrypt(raw))
        ok = isinstance(got, dict) and isinstance(got.get("play"), str) and isinstance(got.get("pause"), str)
        return got if ok else None

    got = engine._mem(memory, "spotify_labels", read, timeout_s=0.3)
    return got if isinstance(got, dict) else None


def _pattern_refused(hwnd: int) -> bool:
    """D17's backstop for a pattern call: refused only on a game's own window (``front_ok``: a pattern call on any
    other window is neither input nor focus, so it may run with a game in front); the check fails closed."""
    try:
        from yuki.memory import gamegate

        return gamegate.refuse_input("uia_pattern", hwnd=int(hwnd), front_ok=True) is not None
    except Exception:  # noqa: BLE001
        return True


def _look(desk: Any, hwnd: int, want: str, *, learned: Mapping[str, str] | None, press: bool = False,
          page: bool = False, grid: bool = True, link: str | None = None) -> Look:
    """One read of the row (and, with ``page``, the page's button when the row isn't in view; with ``link``, only a
    page button whose own container shows that link); with ``press``, the "Play {name}" button found is invoked in
    the same job (decide and press on one read)."""
    if press and _pattern_refused(hwnd):
        return Look("refused")
    gone = threading.Event()
    gate = threading.Lock()       # "the caller stopped waiting" and "the press starts" are decided one at a time
    langs = _langs()
    t0 = time.perf_counter()

    def job(automation: Any, shared: dict) -> None:
        tree = _sd().UiaTree(automation, int(hwnd))
        look = look_row(tree, want, langs, learned) if grid else Look("not_in_view")
        if page and look.status in ("not_in_view", "no_library", "no_rows"):
            other = look_page(tree, want, langs, learned, link=link)
            if other.status in ("ok", "ambiguous", "not_shown"):
                look = other
        look.calls = tree.calls
        shared["look"] = look
        if press and look.status == "ok" and look.state == "play":
            with gate:
                if gone.is_set():
                    return
                shared["acting"] = True
            look.pressed = bool(desk.press(look.button.ref, "invoke"))

    # the link check reads the whole window once more (one cached request) before its press
    shared, late = desk.uia(job, READ_S + (0.4 if press else 0.0) + (READ_S if link else 0.0))
    with gate:
        gone.set()
        acting = bool(shared.get("acting"))
    look = shared.get("look")
    if not isinstance(look, Look):
        look = Look("timeout" if late else "error")
    elif acting and look.pressed is None:
        # the job overran in the middle of the Invoke: it may still land (never followed by another rung's press)
        look.details["press_late"] = True
    look.ms = round((time.perf_counter() - t0) * 1000.0, 1)
    return look


def _read_slider(desk: Any, hwnd: int) -> Look:
    """The slider's level now (no set)."""
    langs = _langs()

    def job(automation: Any, shared: dict) -> None:
        tree = _sd().UiaTree(automation, int(hwnd))
        if not _sd()._built(tree, langs):
            shared["look"] = Look("not_built", calls=tree.calls)
            return
        el, source = look_slider(tree, langs)
        if el is None:
            shared["look"] = Look(source, calls=tree.calls)
            return
        shared["look"] = Look("ok", slider=_sd()._percent(el.rng), source=source, calls=tree.calls)

    shared, late = desk.uia(job, READ_S)
    look = shared.get("look")
    return look if isinstance(look, Look) else Look("timeout" if late else "error")


def _set_slider(desk: Any, hwnd: int, *, to: float | None, by: float | None, budget_s: float = SET_S) -> Look:
    """Find the slider inside its container and move it by ``RangeValue.SetValue`` only:
    :func:`yuki.actions.slider.drive` with ``press=None`` (its focus and arrow keys are unreachable, H1), read back
    and held, in one UIA job of at most ``budget_s`` (:data:`SET_FLOOR_S` .. :data:`SET_S`). Nothing is set once the
    caller stopped waiting."""
    from yuki.actions import slider as slider_mod

    if _pattern_refused(hwnd):
        return Look("refused")
    langs = _langs()
    gone = threading.Event()
    gate = threading.Lock()

    def job(automation: Any, shared: dict) -> None:
        tree = _sd().UiaTree(automation, int(hwnd))
        if not _sd()._built(tree, langs):
            shared["look"] = Look("not_built", calls=tree.calls)
            return
        el, source = look_slider(tree, langs)
        if el is None:
            shared["look"] = Look(source, calls=tree.calls)
            return
        live = desk.live_slider(el.ref, automation)
        with gate:
            if gone.is_set():
                shared["look"] = Look("timeout", calls=tree.calls)
                return
            shared["acting"] = True
        kw: dict[str, Any] = {}
        if SLIDER_SETTLE_S is not None:
            kw["settle_s"] = SLIDER_SETTLE_S
        if SLIDER_HOLD_S is not None:
            kw["hold_s"] = SLIDER_HOLD_S
        ok, _summary, details = slider_mod.drive(live, to=to, by=by, press=None, name="Spotify's volume",
                                                 stopped=gone.is_set, **kw)
        shared["look"] = Look("ok", source=source, details=dict(details), calls=tree.calls,
                              slider=details.get("after_percent"))

    shared, late = desk.uia(job, max(SET_FLOOR_S, min(SET_S, float(budget_s))))
    with gate:
        gone.set()
        acting = bool(shared.get("acting"))
    look = shared.get("look")
    if isinstance(look, Look):
        return look
    return Look("acting_late" if acting else ("timeout" if late else "error"))


# ---------------------------------------------------------------------------------------------------------------------
# Shared steps of the Spotify rungs
# ---------------------------------------------------------------------------------------------------------------------

#: Spotify processes whose window was seen built from this process (the warm-up is done once per Spotify process).
_warmed: dict[int, float] = {}
_warmed_lock = threading.Lock()


def _rr(**kw: Any) -> Any:
    from yuki.skills.engine import RouteResult

    kw.setdefault("app", app_key())
    return RouteResult(**kw)


def _clock(call: Any) -> Callable[[], float]:
    return getattr(getattr(call, "ctx", None), "clock", None) or time.monotonic


def _sleeper(call: Any) -> Callable[[float], None]:
    return getattr(getattr(call, "ctx", None), "sleep", None) or time.sleep


def _left(call: Any) -> float:
    return max(0.0, float(call.deadline) - _clock(call)())


#: A run's "this PC" verdict, kept for its later rungs (by the run's context; a few seconds at most).
_where: dict[int, tuple[float, str, Any]] = {}
_where_lock = threading.Lock()
WHERE_KEEP_S = 10.0


def where_playing(desk: Any, status: str, clock: Callable[[], float], sleep: Callable[[float], None], *,
                  run: Any = None) -> str:
    """M2: ``here`` (Spotify sounds on this PC), ``other`` (its session plays and Spotify.exe has no active render
    session for :data:`THIS_PC_S`: another device), ``idle`` (its session isn't playing: nothing to tell) or
    ``unknown`` (Core Audio couldn't be read: two unread reads and no silent one). Polls only while the skill waits;
    a run's later rungs reuse its verdict (``run``: the run's context), so the 2 s are waited once per run."""
    if status != "playing":
        return "idle"
    key = id(run) if run is not None else None
    now = time.monotonic()
    if key is not None:
        with _where_lock:
            hit = _where.get(key)
        if hit is not None and now - hit[0] < WHERE_KEEP_S and hit[2]() is run:   # the same run, never a reused id
            return hit[1]
    wait_s = _number("this_pc_s", THIS_PC_S, 0.5, 4.0)
    start = clock()
    silent = unread = 0
    verdict = "unknown"
    while True:
        got = desk.render_active()
        if got is True:
            verdict = "here"
            break
        if got is False:
            silent += 1
        else:
            unread += 1
            if unread >= 2 and not silent:
                break
        if clock() - start + POLL_S > wait_s:
            verdict = "other" if silent else "unknown"
            break
        sleep(POLL_S)
    if key is not None:
        with _where_lock:
            for k in [k for k, (t, _v, _r) in _where.items() if now - t >= WHERE_KEEP_S]:
                _where.pop(k, None)
            try:
                _where[key] = (now, verdict, weakref.ref(run))
            except TypeError:   # a context that can't be weakly referenced: not kept
                pass
    return verdict


def _ensure_warm(desk: Any, win: Any, call: Any, facts: dict[str, Any], *, force: bool = False) -> bool:
    """The warm-up within the request's own time (H2): skipped for a Spotify process already seen built from here;
    False when the window doesn't build in time ("couldn't")."""
    with _warmed_lock:
        known = win.pid in _warmed
    if known and not force:
        facts["warm"] = "skipped"
        return True
    budget = min(_number("request_warm_s", REQUEST_WARM_S, 0.2, 3.0), _left(call))
    if budget < 0.1:
        facts["warm"] = "no_time"
        return False
    got = desk.warm(win.hwnd, budget)
    facts["warm"] = "rewarmed" if known else "ran"
    facts["built"] = bool(getattr(got, "built", False))
    facts["cold"] = bool(getattr(got, "cold", False))
    facts["warm_ms"] = round(float(getattr(got, "ms", 0.0) or 0.0), 1)
    if getattr(got, "drawn", False):
        facts["drawn"] = True      # drawn, but none of the labels known: a Spotify update or another language
    if getattr(got, "built", False):
        with _warmed_lock:
            if len(_warmed) > 32:
                _warmed.clear()
            _warmed[win.pid] = time.time()
        return True
    with _warmed_lock:
        _warmed.pop(win.pid, None)
    return False


def _cold(facts: Mapping[str, Any]) -> str:
    """Why a warm-up didn't give a usable window: ``unrecognised`` (drawn, labels unknown) or ``cold``."""
    return "unrecognised" if facts.get("drawn") else "cold"


def forget_warm() -> None:
    """Forget which Spotify processes were warm and the runs' "this PC" verdicts (tests; a Spotify restart gets a new
    pid anyway)."""
    with _warmed_lock:
        _warmed.clear()
    with _where_lock:
        _where.clear()


def _log_read(call: Any, trigger: str, state: str | None, facts: Mapping[str, Any], look: Look | None) -> None:
    """``spotify_read`` (D12) for a skill's read: the window state, warm, counts, the label source. No names."""
    try:
        from yuki.skills.engine import _emit

        fields = {"trigger": f"skill_{trigger}", "state": state or "unknown", "warm": facts.get("warm"),
                  "this_pc": facts.get("this_pc")}
        if look is not None:
            fields.update(look.counts())
            fields["ms"] = look.ms
        _emit(call.ctx, "spotify_read", **fields)
    except Exception:  # noqa: BLE001 - a log never breaks a run
        pass


def _logger(call: Any) -> Callable[..., None]:
    """The run's content-free log (``yuki.skills.engine._emit``) as a ``log(event, **fields)`` callable."""
    def log(event: str, **fields: Any) -> None:
        try:
            from yuki.skills.engine import _emit

            _emit(call.ctx, event, **fields)
        except Exception:  # noqa: BLE001
            pass

    return log


def _playlist_name(values: Mapping[str, Any]) -> str:
    v = values.get("playlist")
    if isinstance(v, Mapping):
        v = v.get("name")
    return " ".join(str(v or "").split())


def _entity_digest(call: Any, name: str) -> str | None:
    """The playlist as D18 matches it: the keyed digest the moments writer gives a ``media_context`` of Spotify with
    this row name (``cipher.digest("moment", "media_context", "spotify", name)``); None without a store."""
    memory = getattr(call.ctx, "memory", None)
    if memory is None or not name:
        return None
    from yuki.skills import engine

    def digest(client: Any) -> Any:
        cipher = getattr(engine._store_of(client), "cipher", None)
        return cipher.digest("moment", "media_context", app_key(), _sd().norm(name)) if cipher is not None else None

    got = engine._mem(memory, "spotify_digest", digest, timeout_s=0.3)
    return str(got) if got else None


def _other_device(facts: dict[str, Any], *, volume: bool = False) -> Any:
    return _rr(ran=False, outcome="other_device", say=LINE_OTHER_VOLUME if volume else LINE_OTHER, facts=facts)


def _ask_two(look: Look) -> Any:
    """Two rows with one name: asked, never guessed. Options only when every row's owner is known and they differ
    (an answer is matched back to its row by the option's own label: :func:`look_row`)."""
    owners = look.owners
    options = [option_label(look.name, o) for o in owners] if len(owners) > 1 and len(owners) == len(set(owners)) \
        else []
    return _rr(ran=False, outcome="ambiguous", options=options,
               ask=f"You have more than one playlist called {look.name}. Which one?")


def _after_look(call: Any, look: Look, status: str, app_id: str, facts: dict[str, Any]) -> Any:
    """What a row (or page button) read means, after any press made on it: an ask for two rows; "couldn't" for no
    row; a "Play {name}" pressed -> acted; "Pause {name}" with the session playing -> already playing (checked by the
    done-check: session playing and Spotify sounding here); "Pause {name}" with the session paused or stopped -> a
    stale label: the session's own ``play`` (M1a: never Invoke, never a toggle)."""
    facts.update({k: v for k, v in look.counts().items() if k in ("rows", "row", "where", "label_source")})
    if look.status == "ambiguous":
        return _ask_two(look)
    if look.status != "ok":
        return _rr(ran=False, outcome=look.status, facts=facts, unverified_line=LINE_PLAY_UNCONFIRMED)
    digest = _entity_digest(call, look.name)
    said = f"Playing {look.name} on Spotify."
    if look.state == "pause":
        # review r2 L1: the row reads "Pause {name}" (Spotify shows it playing). Nothing here acts, but the run stops
        # on this rung either way: ``ran=None`` with ``already`` -> the engine checks the no-op first ("already" when
        # it sees it), and a check that can't confirm it ends "couldn't confirm" (settled) instead of passing on to
        # the focus-taking link rung and the thinker for a playlist that is already playing. A check that reads "not
        # so" (the row back to "Play {name}", the session paused) still moves on: then it isn't playing.
        already = f"{look.name} is already playing on Spotify."
        if status == "playing":
            return _rr(ran=None, already=True, outcome="already", say=already, facts=facts, entity_digest=digest,
                       unverified_line=LINE_ALREADY_UNCONFIRMED)
        if not app_id:
            # no session to read or to send play to (unread, or none listed): only "couldn't confirm" is honest
            facts["media"] = "no_session"
            return _rr(ran=None, already=True, outcome="already_unread", say=already, facts=facts,
                       entity_digest=digest, unverified_line=LINE_ALREADY_UNCONFIRMED)
        mc = DESK.media("play", app_id, log=_logger(call))
        outcome = str(getattr(mc, "outcome", "") or "error")
        facts["media"] = outcome
        if outcome == "sent":
            return _rr(ran=True, outcome="resumed", say=said, facts=facts, entity_digest=digest,
                       unverified_line=LINE_PLAY_UNCONFIRMED)
        if outcome in ("pending", "timeout"):
            # sent with no answer in time: it may have gone through (the done-check decides; never a second rung)
            return _rr(ran=None, outcome="resumed", say=said, facts=facts, entity_digest=digest,
                       unverified_line=LINE_PLAY_UNCONFIRMED)
        if outcome == "already":
            return _rr(ran=None, already=True, outcome="already", say=already, facts=facts, entity_digest=digest,
                       unverified_line=LINE_ALREADY_UNCONFIRMED)
        return _rr(ran=False, outcome="media_" + outcome, facts=facts)
    if look.pressed and look.details.get("not_taken"):
        # a no-focus press on a Spotify not in front that its row never showed (Chromium took the Invoke and did
        # nothing): not run, so the window-state rule's restore may run, and the rung isn't charged
        return _rr(ran=False, outcome="not_taken", facts=facts)
    if look.pressed:
        return _rr(ran=True, outcome="pressed", say=said, facts=facts, entity_digest=digest,
                   unverified_line=LINE_PLAY_UNCONFIRMED)
    if look.details.get("press_late"):
        # the read overran in the middle of the Invoke: it may still land (the done-check decides; never a 2nd press)
        return _rr(ran=None, outcome="press_late", say=said, facts=facts, entity_digest=digest,
                   unverified_line=LINE_PLAY_UNCONFIRMED)
    return _rr(ran=False, outcome="press_failed" if look.pressed is False else "not_pressed", facts=facts)


def _this_pc(call: Any, facts: dict[str, Any], pid: int | None) -> tuple[str, str, str]:
    """Spotify's session (its window's process, or with no window any Spotify session: closed to the tray, Spotify
    still controls the device that plays) and M2's verdict: ``(status, app_id, where)``."""
    status, app_id = DESK.spotify_status(pid)
    facts["session"] = status if status.isalpha() else "unknown"
    where = where_playing(DESK, status, _clock(call), _sleeper(call), run=call.ctx)
    facts["this_pc"] = where
    return status, app_id, where


def _prologue(call: Any, facts: dict[str, Any], *, volume: bool = False) -> tuple[Any, str, str, Any]:
    """Spotify's window, its session and the "this PC" check: ``(window, status, app_id, early result or None)``.
    The check runs with no window too (closed to the tray and playing on the phone: "another device", never a
    hand-off)."""
    win = DESK.window()
    status, app_id, where = _this_pc(call, facts, win.pid if win is not None else None)
    if where == "other":
        return win, status, app_id, _other_device(facts, volume=volume)
    if win is None:
        facts["window"] = "none"
        return None, status, app_id, _rr(ran=False, outcome="no_window", facts=facts)
    return win, status, app_id, None


def _confirm_flip(call: Any, hwnd: int, want: str, learned: Mapping[str, str] | None, look: Look,
                  state: str | None, facts: dict[str, Any]) -> Look:
    """After a no-focus press on a Spotify that isn't in front: its row re-read (row reads only, at most
    :data:`FLIP_WAIT_S` and the request's time left) until it reads "Pause {name}". Still "Play {name}" at the end ->
    ``details["not_taken"]`` (a backgrounded Chromium window took the Invoke and did nothing). A row that can't be read
    any more (moved, timed out) is left as pressed: the done-check decides. In front: nothing to wait for here."""
    if not look.pressed or state == "front":
        return look
    clock, sleep = _clock(call), _sleeper(call)
    until = clock() + min(FLIP_WAIT_S, _left(call))
    seen: str | None = None
    reads = 0
    while (reads == 0 or seen == "play") and clock() + POLL_S <= until:
        sleep(POLL_S)
        again = _look(DESK, hwnd, want, learned=learned, grid=look.where != "page", page=look.where == "page")
        reads += 1
        seen = again.state if again.status == "ok" and _fold(again.name) == _fold(look.name) else None
    facts["flip"] = seen or "unread"
    facts["flip_reads"] = reads
    if seen == "play":
        look.details["not_taken"] = True
    return look


# ---------------------------------------------------------------------------------------------------------------------
# spotify.playlist: the rungs
# ---------------------------------------------------------------------------------------------------------------------


def play_library(call: Any) -> Any:
    """Rung A, no focus: the row of DataGrid "Your Library" named as asked; its "Play {name}" button by Invoke. Not
    while Spotify plays on another device; a cold window is warmed within the request's time."""
    want = _playlist_name(call.values)
    if not want:
        return _rr(ran=False, outcome="no_name")
    facts: dict[str, Any] = {}
    win, status, app_id, early = _prologue(call, facts)
    if early is not None:
        return early
    state = DESK.state(win.hwnd)
    facts["state"] = state or "unknown"
    if not _ensure_warm(DESK, win, call, facts):
        _log_read(call, "library", state, facts, None)
        return _rr(ran=False, outcome=_cold(facts), facts=facts)
    learned = _learned_wording(call.ctx.memory)
    look = _look(DESK, win.hwnd, want, learned=learned, press=True)
    if look.status == "not_built" and facts.get("warm") == "skipped":
        # seen warm before, frame only now: one fresh warm-up within the time left, then once more
        if _ensure_warm(DESK, win, call, facts, force=True):
            look = _look(DESK, win.hwnd, want, learned=learned, press=True)
    look = _confirm_flip(call, win.hwnd, want, learned, look, state, facts)
    _log_read(call, "library", state, facts, look)
    return _after_look(call, look, status, app_id, facts)


def play_restored(call: Any) -> Any:
    """The window-state rule (D4), focus-taking, never with a game in front: Spotify minimised and the no-focus route
    couldn't read or press it -> restore it, play the row, put the previous window back in front. A minimised window
    whose rows read fine but hold no such row is not restored (restoring wouldn't help)."""
    want = _playlist_name(call.values)
    if not want:
        return _rr(ran=False, outcome="no_name")
    facts: dict[str, Any] = {}
    win, status, app_id, early = _prologue(call, facts)
    if early is not None:
        return early
    state = DESK.state(win.hwnd)
    facts["state"] = state or "unknown"
    if state != "minimised":
        return _rr(ran=False, outcome="not_minimised", facts=facts)
    learned = _learned_wording(call.ctx.memory)
    first = _look(DESK, win.hwnd, want, learned=learned)
    if first.status in ("not_in_view", "no_rows", "no_wording"):
        return _rr(ran=False, outcome=first.status, facts=facts)
    if first.status == "ambiguous":
        return _ask_two(first)
    if first.status == "ok" and first.state == "pause":
        return _after_look(call, first, status, app_id, facts)
    prev = DESK.foreground()
    got = DESK.focus(win.hwnd)
    if _refused(got):
        return _rr(ran=False, held="game", outcome="refused", facts=facts)
    if not getattr(got, "ok", False):
        return _rr(ran=False, outcome="focus_failed", facts=facts)
    facts["restored"] = True
    try:
        if not _ensure_warm(DESK, win, call, facts, force=True):
            return _rr(ran=False, outcome=_cold(facts), facts=facts)
        look = _look(DESK, win.hwnd, want, learned=learned, press=True)
        _log_read(call, "restored", "front", facts, look)
        status, app_id = DESK.spotify_status(win.pid)
        return _after_look(call, look, status, app_id, facts)
    finally:
        if prev and prev != win.hwnd:
            facts["put_back"] = bool(getattr(DESK.focus(prev), "ok", False))


def link_for(call: Any, want: str) -> tuple[str | None, str]:
    """The learned ``spotify:playlist:{id}`` for this name and Spotify's own spelling of the name: the slot's taste
    link when it has one, else the links the memory side learned from links Spotify showed (identity of names).
    ``(None, want)`` when none (or two different links for one name: never a guess)."""
    sd = _sd()
    value = call.values.get("playlist")
    if isinstance(value, Mapping) and sd.playlist_uri_ok(value.get("ref")):
        return str(value.get("ref")), str(value.get("name") or want)
    memory = getattr(call.ctx, "memory", None)
    if memory is None:
        return None, want
    from yuki.skills import engine

    links = engine._mem(memory, "spotify_links", lambda c: sd.learned_links(engine._store_of(c)), timeout_s=0.5)
    hits = [(n, u) for n, u in (links or {}).items() if _fold(n) == _fold(want) and sd.playlist_uri_ok(u)]
    if hits and len({u for _n, u in hits}) == 1:
        return hits[0][1], hits[0][0]
    return None, want


def play_link(call: Any) -> Any:
    """Out of view: the ``spotify:`` link Yuki learned for it (focus-taking: it brings Spotify forward; never with a
    game in front), then the page's own "Play {name}" button by Invoke."""
    want = _playlist_name(call.values)
    if not want:
        return _rr(ran=False, outcome="no_name")
    uri, name = link_for(call, want)
    facts: dict[str, Any] = {"link": bool(uri)}
    if not uri:
        return _rr(ran=False, outcome="no_link", facts=facts)
    win = DESK.window()
    checked = win.pid if win is not None else None
    _status, _app_id, where = _this_pc(call, facts, checked)
    if where == "other":
        return _other_device(facts)
    opened = DESK.open_uri(uri)
    if _refused(opened):
        return _rr(ran=False, held="game", outcome="refused", facts=facts)
    if not getattr(opened, "ok", False):
        return _rr(ran=False, outcome="open_failed", facts=facts)
    clock, sleep = _clock(call), _sleeper(call)
    until = clock() + min(PAGE_WAIT_S, max(_left(call), 1.0))
    learned = _learned_wording(call.ctx.memory)
    look = Look("missing")
    while True:
        win = DESK.window()
        if win is not None:
            if win.pid != checked:
                # the link started Spotify (or a new Spotify process): the "this PC" check again before any press
                checked = win.pid
                _status, _app_id, where = _this_pc(call, facts, win.pid)
                if where == "other":
                    return _other_device(facts)
            with _warmed_lock:
                warm = win.pid in _warmed
            if not warm:
                _ensure_warm(DESK, win, call, facts)
            # review r2 L2: only the opened page's own button (its container shows the learned link), never a
            # same-named header of the view still in front while Spotify navigates
            look = _look(DESK, win.hwnd, name, learned=learned, press=True, page=True, grid=False, link=uri)
            if look.status in ("ok", "ambiguous"):
                break
        if clock() + POLL_S > until:
            break
        sleep(POLL_S)
    _log_read(call, "link", "front", facts, look)
    if look.status not in ("ok", "ambiguous"):
        return _rr(ran=False, outcome="page_" + look.status, facts=facts)
    status, app_id = DESK.spotify_status(win.pid)
    return _after_look(call, look, status, app_id, facts)


def _search_job(desk: Any, hwnd: int, *, step: str, text: str = "") -> dict[str, Any]:
    """One step of the Library search (labels ``controls.library_search`` / ``controls.library_filter`` in the site
    data): ``open`` (press the search button when the box isn't there), ``fill`` (Value.SetValue of the box),
    ``clear`` (set it back to empty and read it back). Patterns only, and none once the caller stopped waiting
    (review r2 L3: a fill landing after the clear would leave Your Library filtered; ``late`` says one may still be
    going)."""
    sd = _sd()
    if _pattern_refused(hwnd):
        return {"refused": True}
    buttons = extra_control("library_search")
    boxes = extra_control("library_filter")
    gone = threading.Event()
    gate = threading.Lock()       # "the caller stopped waiting" and "a pattern call starts" decided one at a time

    def act(shared: dict, ref: Any, pattern: str, *value: Any) -> bool | None:
        with gate:
            if gone.is_set():
                return None
            shared["acting"] = True
        try:
            return bool(desk.press(ref, pattern, *value))
        finally:
            shared["acting"] = False

    def find(tree: Any, specs: list) -> list[Any]:
        # every label inside its own container (M5): a spec with no container path is never searched from the root
        for _lab, role, name, path in specs:
            if not path:
                continue
            hits: list[Any] = []
            for e in (e for s in sd._container(tree, path) for e in sd.local_find(s, role, name)):
                if not any(_same(tree, e, h) for h in hits):     # nested containers find one element twice
                    hits.append(e)
            if hits:
                return hits
        return []

    def job(automation: Any, shared: dict) -> None:
        tree = sd.UiaTree(automation, int(hwnd))
        box = find(tree, boxes)
        if step == "open":
            if len(box) == 1:
                shared["box"] = True
                return
            btn = find(tree, buttons)
            shared["pressed"] = len(btn) == 1 and bool(act(shared, btn[0].ref, "invoke"))
        elif step == "fill":
            shared["filled"] = len(box) == 1 and bool(act(shared, box[0].ref, "value", text))
        elif step == "clear":
            if len(box) != 1:
                shared["cleared"] = None
                return
            if act(shared, box[0].ref, "value", "") is None:
                return
            again = find(sd.UiaTree(automation, int(hwnd)), boxes)
            shared["cleared"] = len(again) == 1 and not str(again[0].value or "").strip()

    shared, _late = desk.uia(job, READ_S)
    with gate:
        gone.set()
        if shared.get("acting"):
            shared["late"] = True          # the job overran in the middle of a pattern call: it may still land
    return shared


def play_search(call: Any) -> Any:
    """No learned link: "Search in Your Library" (unverified; patterns only, no focus): open the search box, set its
    value to the name, play the row it shows, and clear the filter afterwards (L13). Needs the search labels in the
    site data; without them it says "couldn't"."""
    want = _playlist_name(call.values)
    if not want:
        return _rr(ran=False, outcome="no_name")
    if not extra_control("library_search") or not extra_control("library_filter"):
        return _rr(ran=False, outcome="no_label")
    facts: dict[str, Any] = {}
    win, status, app_id, early = _prologue(call, facts)
    if early is not None:
        return early
    state = DESK.state(win.hwnd)
    facts["state"] = state or "unknown"
    if not _ensure_warm(DESK, win, call, facts):
        return _rr(ran=False, outcome=_cold(facts), facts=facts)
    clock, sleep = _clock(call), _sleeper(call)
    learned = _learned_wording(call.ctx.memory)
    opened = _search_job(DESK, win.hwnd, step="open")
    if not (opened.get("box") or opened.get("pressed")):
        return _rr(ran=False, outcome="no_search", facts=facts)
    look = Look("missing")
    facts_ram: dict[str, Any] = {}
    late_fill = False
    try:
        until = clock() + min(SEARCH_WAIT_S, max(_left(call), 0.5))
        filled = False
        while not filled:
            got = _search_job(DESK, win.hwnd, step="fill", text=want)
            filled, late_fill = bool(got.get("filled")), late_fill or bool(got.get("late"))
            if filled or clock() + POLL_S > until:
                break
            sleep(POLL_S)
        if not filled:
            return _rr(ran=False, outcome="no_search_box", facts=facts)
        until = clock() + min(SEARCH_WAIT_S, max(_left(call), 0.5))
        while True:
            look = _look(DESK, win.hwnd, want, learned=learned, press=True)
            if look.status in ("ok", "ambiguous", "no_wording") or clock() + POLL_S > until:
                break
            sleep(POLL_S)
        if look.status == "ok":
            # clearing the filter takes the row out of view, so the row's own "Pause {name}" is waited for here,
            # before the filter is cleared (the done-check then reads the session and the sound; the row as seen)
            seen = look.state
            reads, last_ok = 0, False
            until = clock() + min(4.0, max(_left(call), 1.0))
            while look.pressed and seen != "pause" and clock() + POLL_S <= until:
                sleep(POLL_S)
                again = _look(DESK, win.hwnd, want, learned=learned)
                reads, last_ok = reads + 1, again.status == "ok"
                seen = again.state if again.status == "ok" else seen
            facts_ram["row_seen"] = seen or ""
            if look.pressed and state != "front" and reads and last_ok and seen == "play":
                look.details["not_taken"] = True      # as _confirm_flip: a backgrounded window ignored the Invoke
    finally:
        cleared = _search_job(DESK, win.hwnd, step="clear")
        if late_fill or cleared.get("late"):
            # a fill (or the clear) overran in the middle of its call and may land after this clear: once more, one
            # poll step later (L13: Your Library is never left filtered)
            sleep(POLL_S)
            cleared = _search_job(DESK, win.hwnd, step="clear")
        facts["filter_cleared"] = cleared.get("cleared")
    _log_read(call, "search", state, facts, look)
    got = _after_look(call, look, status, app_id, facts)
    got.facts_ram.update(facts_ram)
    return got


def play_launch(call: Any) -> Any:
    """Spotify isn't running (or has no window): launch it with its own ``--minimized`` argument (data; focus-taking,
    never with a game in front: D17(2)), warm it within the time left and play the row. Spotify's start minimised is
    unverified; a window that isn't built in time says "couldn't" (the thinker takes over)."""
    want = _playlist_name(call.values)
    if not want:
        return _rr(ran=False, outcome="no_name")
    if DESK.window() is not None:
        return _rr(ran=False, outcome="running")
    facts: dict[str, Any] = {}
    # closed to the tray and playing on the phone: its session still says so with no window (M2, before any launch)
    _status, _app_id, where = _this_pc(call, facts, None)
    if where == "other":
        return _other_device(facts)
    left = _left(call)
    if left <= POLL_S:
        return _rr(ran=False, outcome="no_time", facts=facts)
    raw = spotify_data().get("launch_args")
    args = [str(a) for a in raw] if isinstance(raw, list) else []
    facts["launched"] = True
    got = DESK.launch(args, timeout_s=left)
    if _refused(got):
        return _rr(ran=False, held="game", outcome="refused", facts=facts)
    if not getattr(got, "ok", False):
        return _rr(ran=False, outcome="launch_failed", facts=facts)
    sleep = _sleeper(call)
    win = None
    while win is None:
        win = DESK.window()
        if win is not None or _left(call) <= POLL_S:
            break
        sleep(POLL_S)
    if win is None or not _ensure_warm(DESK, win, call, facts):
        return _rr(ran=False, outcome="cold_after_launch", facts=facts)
    # the session as the started Spotify shows it: still the phone playing -> left there, nothing pressed
    status, app_id, where = _this_pc(call, facts, win.pid)
    if where == "other":
        return _other_device(facts)
    learned = _learned_wording(call.ctx.memory)
    state = DESK.state(win.hwnd)
    look = _look(DESK, win.hwnd, want, learned=learned, press=True)
    look = _confirm_flip(call, win.hwnd, want, learned, look, state, facts)    # started minimised: taken or not
    _log_read(call, "launch", state, facts, look)
    status, app_id = DESK.spotify_status(win.pid)
    return _after_look(call, look, status, app_id, facts)


# ---------------------------------------------------------------------------------------------------------------------
# spotify.playlist: the checks
# ---------------------------------------------------------------------------------------------------------------------


def _playing_facts(ctx: Any, args: Mapping[str, Any]) -> tuple[Look | None, str, bool | None, dict[str, Any]]:
    want = str(args.get("name") or "")
    win = DESK.window()
    if win is None or not want:
        return None, "", None, {"status": "no_window" if win is None else "no_name"}
    memory_learned = None
    call_memory = getattr(ctx, "memory_call", None)
    if call_memory is not None:
        memory_learned = _learned_from_probe(call_memory)
    look = _look(DESK, win.hwnd, want, learned=memory_learned, page=True)
    seen = ((getattr(getattr(ctx, "result", None), "facts_ram", None) or {}).get("row_seen"))
    if look.status != "ok" and seen in ("play", "pause"):
        # the route itself took the row out of view (the Library search's filter, cleared afterwards): the row's
        # wording as the route saw it, read before it cleared the filter
        look = Look("ok", state=str(seen), where="seen")
    status, _app_id = DESK.spotify_status(win.pid)
    audio = DESK.render_active()
    ev = {"row": look.state or look.status, "where": look.where, "session": status if status.isalpha() else "",
          "audio": audio}
    return look, status, audio, ev


def _learned_from_probe(memory_call: Callable[[str, Callable[[Any], Any]], Any]) -> dict[str, str] | None:
    class _Proxy:
        def call(self, what: str, fn: Callable[[Any], Any], timeout_s: float = 0.3) -> Any:
            return memory_call(what, fn)

    return _learned_wording(_Proxy())


def _set_line(ctx: Any, line: str) -> None:
    """The plain line said if the check ends unconfirmed (the route's result carries it to the engine)."""
    result = getattr(ctx, "result", None)
    if result is not None and hasattr(result, "unverified_line"):
        try:
            result.unverified_line = line
        except Exception:  # noqa: BLE001
            pass


def _not_pressed(ctx: Any) -> bool:
    """Whether the route's result is a no-op being checked (the row already read "Pause {name}": nothing pressed)."""
    return bool(getattr(getattr(ctx, "result", None), "already", False))


def probe_playing_here(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """The playlist's done-check (D19): its row (or the page's button) reads "Pause {name}", Spotify's session says
    playing **and** Spotify sounds on this PC -> True. The row flipped and the session plays but no sound here -> None
    (``other_device``, said plainly: it started on another device). The row still reads "Play {name}", or the session
    is paused or stopped -> False. The row reads "Pause {name}" and Windows lists no Spotify session at all: the two
    disagree -> None. Anything unread -> None. The line said when it ends unconfirmed names what was done (a press,
    or nothing: the row already read "Pause {name}")."""
    look, status, audio, ev = _playing_facts(ctx, args)
    if look is None:
        return None, ev
    unconfirmed = LINE_ALREADY_UNCONFIRMED if _not_pressed(ctx) else LINE_PLAY_UNCONFIRMED
    if look.status == "ok" and look.state == "pause" and status == "playing":
        if audio is True:
            _set_line(ctx, unconfirmed)
            return True, ev
        if audio is False:
            ev["reason"] = "other_device"
            _set_line(ctx, LINE_OTHER if _not_pressed(ctx) else LINE_OTHER_AFTER)
            return None, ev
        _set_line(ctx, unconfirmed)
        return None, ev
    _set_line(ctx, unconfirmed)
    if look.status == "ok" and (look.state == "play" or status in ("paused", "stopped")):
        return False, ev
    return None, ev


def probe_still_playing(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """The playlist's re-check, a few seconds after the done (D19): the session still playing, Spotify still sounding
    on this PC and the row still "Pause {name}". No new track is required (resuming the current playlist is a true
    done, M1b). The session stopped, no sound here, or the row back to "Play {name}" -> False (a false done)."""
    look, status, audio, ev = _playing_facts(ctx, args)
    if look is None:
        return None, ev
    if status in ("paused", "stopped", "none"):
        return False, ev
    if status == "playing" and audio is False:
        return False, ev
    if look.status == "ok" and look.state == "play":
        return False, ev
    if look.status == "ok" and look.state == "pause" and status == "playing" and audio is True:
        return True, ev
    return None, ev


# ---------------------------------------------------------------------------------------------------------------------
# volume: the rungs
# ---------------------------------------------------------------------------------------------------------------------


def _amount(values: Mapping[str, Any]) -> tuple[float | None, float | None, bool | None]:
    """``(level, change, mute)`` from the filled slots (numbers 0-100 / signed points; mute "on" / "off")."""
    def num(v: Any) -> float | None:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    level, change = num(values.get("level")), num(values.get("change"))
    mute_raw = values.get("mute")
    mute = True if mute_raw in ("on", True) else (False if mute_raw in ("off", False) else None)
    if level is not None:
        change = None
    return level, change, mute


def _no_amount(level: float | None, change: float | None, mute: bool | None) -> bool:
    return level is None and change is None and mute is None


def _slider_result(look: Look, facts: dict[str, Any], *, how: str = "") -> Any:
    """A slider set's outcome: already there -> a true no-op (read back); reached -> acted and verified; moved short
    -> acted, contradicted; didn't move at all (a backgrounded window ignored it) -> couldn't run (the next rung may
    try); not found -> couldn't."""
    facts.update(label_source=look.source, slider=look.status)
    if look.status == "acting_late":
        return _rr(ran=None, outcome="set_late", facts=facts, unverified_line=LINE_VOLUME_UNCONFIRMED)
    if look.status != "ok":
        return _rr(ran=False, outcome="slider_" + look.status, facts=facts)
    d = look.details
    after = d.get("after_percent")
    after = float(after) if isinstance(after, (int, float)) else None
    suffix = f" {how}" if how else ""
    if d.get("noop"):
        return _rr(ran=False, already=True, verified=True, value=after, outcome="already", facts=facts,
                   say=f"Spotify's own volume is already at {after:g}%.")
    moved = isinstance(d.get("before"), (int, float)) and isinstance(d.get("after"), (int, float)) \
        and abs(float(d["after"]) - float(d["before"])) > 1e-9
    if d.get("verified"):
        return _rr(ran=True, verified=True, value=after, outcome="set", facts=facts,
                   say=f"Spotify's own volume, {after:g}%{suffix}.", unverified_line=LINE_VOLUME_UNCONFIRMED)
    if moved:
        return _rr(ran=True, verified=False, value=after, outcome="off_target", facts=facts)
    facts["reverted"] = bool(d.get("reverted"))
    return _rr(ran=False, outcome="not_taken", facts=facts)


def volume_slider(call: Any) -> Any:
    """Rung A for Spotify's own volume, no focus (H1): Slider "Change volume" inside the Now playing bar, moved by
    ``RangeValue.SetValue`` only and read back. Not while Spotify plays on another device; a cold window is warmed
    within the request's time. A mute is left to the mixer (Spotify's Mute button is a toggle whose state can't be
    read)."""
    level, change, mute = _amount(call.values)
    if level is None and change is None:
        if mute is not None:
            return _rr(ran=False, outcome="mute_not_in_app")
        return _rr(ran=False, ask=ASK_AMOUNT, outcome="no_amount")
    facts: dict[str, Any] = {}
    win, _status, _app_id, early = _prologue(call, facts, volume=True)
    if early is not None:
        return early
    state = DESK.state(win.hwnd)
    facts["state"] = state or "unknown"
    if not _ensure_warm(DESK, win, call, facts):
        _log_read(call, "volume", state, facts, None)
        return _rr(ran=False, outcome=_cold(facts), facts=facts)
    look = _set_slider(DESK, win.hwnd, to=level, by=change, budget_s=_left(call))
    _log_read(call, "volume", state, facts, look)
    return _slider_result(look, facts)


def volume_keys(call: Any) -> Any:
    """Spotify's own Ctrl+Up / Ctrl+Down (focus-taking route data: never with a game in front), only while Spotify is
    already in front (nothing is brought forward for it), each press read back on the slider."""
    level, change, mute = _amount(call.values)
    if level is None and change is None:
        return _rr(ran=False, outcome="mute_not_in_app" if mute is not None else "no_amount")
    facts: dict[str, Any] = {}
    win, _status, _app_id, early = _prologue(call, facts, volume=True)
    if early is not None:
        return early
    state = DESK.state(win.hwnd)
    facts["state"] = state or "unknown"
    if state != "front":
        return _rr(ran=False, outcome="not_front", facts=facts)
    first = _read_slider(DESK, win.hwnd)
    if first.status != "ok" or first.slider is None:
        return _rr(ran=False, outcome="slider_" + first.status, facts=facts)
    v0 = v = float(first.slider)
    target = max(0.0, min(100.0, level if level is not None else v0 + float(change or 0.0)))
    if abs(target - v0) <= LEVEL_TOLERANCE / 2:
        return _rr(ran=False, already=True, verified=True, value=v0, outcome="already", facts=facts,
                   say=f"Spotify's own volume is already at {v0:g}%.")
    sleep = _sleeper(call)
    presses, step = 0, 0.0
    out_of_time = False
    toward = 1.0 if target > v0 else -1.0
    while presses < KEYS_MAX:
        if _left(call) < KEY_WAIT_S + READ_S:
            out_of_time = True             # no press whose read-back can't be waited for within the request
            break
        got = DESK.hotkey(["ctrl", "up" if toward > 0 else "down"], win.hwnd)
        if _refused(got):
            facts["presses"] = presses
            return _rr(ran=presses > 0, held="game", outcome="refused", facts=facts)
        if not getattr(got, "ok", False):
            break
        presses += 1
        sleep(KEY_WAIT_S)
        again = _read_slider(DESK, win.hwnd)
        if again.status != "ok" or again.slider is None:
            break
        moved = float(again.slider) - v
        v = float(again.slider)
        if abs(moved) < 1e-9 or moved * toward < 0:
            break
        step = abs(moved)
        if abs(v - target) <= max(LEVEL_TOLERANCE, step * 0.75) or (v - target) * toward > 0:
            break
    facts["presses"] = presses
    reached = abs(v - target) <= max(LEVEL_TOLERANCE, step * 0.75) and (v - v0) * toward > 0
    if reached:
        return _rr(ran=True, verified=True, value=v, outcome="set", facts=facts, say=f"Spotify's own volume, {v:g}%.")
    if out_of_time and (v - v0) * toward > 0:
        # moved the right way and stopped by the request's time, not by Spotify: said plainly, never counted against
        # the rung (verified unknown), never "done"
        return _rr(ran=True, verified=None, value=v, outcome="out_of_time", facts=facts,
                   unverified_line=LINE_KEYS_SHORT.format(now=v, target=target))
    if abs(v - v0) > 1e-9:
        return _rr(ran=True, verified=False, value=v, outcome="off_target", facts=facts)
    return _rr(ran=False, outcome="no_time" if out_of_time else "keys_did_nothing", facts=facts)


def volume_restored(call: Any) -> Any:
    """The window-state rule for Spotify's volume (focus-taking, never with a game in front): Spotify minimised and
    the no-focus rung couldn't set it -> restore Spotify, set the slider by RangeValue, put the previous window back
    in front."""
    level, change, mute = _amount(call.values)
    if level is None and change is None:
        return _rr(ran=False, outcome="mute_not_in_app" if mute is not None else "no_amount")
    facts: dict[str, Any] = {}
    win, _status, _app_id, early = _prologue(call, facts, volume=True)
    if early is not None:
        return early
    state = DESK.state(win.hwnd)
    facts["state"] = state or "unknown"
    if state != "minimised":
        return _rr(ran=False, outcome="not_minimised", facts=facts)
    first = _read_slider(DESK, win.hwnd)
    if first.status in ("missing", "ambiguous"):
        # the minimised window reads, and its slider isn't there by its label or shape: restoring wouldn't help
        return _rr(ran=False, outcome="slider_" + first.status, facts=facts)
    prev = DESK.foreground()
    got = DESK.focus(win.hwnd)
    if _refused(got):
        return _rr(ran=False, held="game", outcome="refused", facts=facts)
    if not getattr(got, "ok", False):
        return _rr(ran=False, outcome="focus_failed", facts=facts)
    facts["restored"] = True
    try:
        if not _ensure_warm(DESK, win, call, facts, force=True):
            return _rr(ran=False, outcome=_cold(facts), facts=facts)
        look = _set_slider(DESK, win.hwnd, to=level, by=change, budget_s=_left(call))
        _log_read(call, "volume_restored", "front", facts, look)
        return _slider_result(look, facts)
    finally:
        if prev and prev != win.hwnd:
            facts["put_back"] = bool(getattr(DESK.focus(prev), "ok", False))


def _mixer_after(result: Any) -> tuple[bool, bool | None, float | None, float | None]:
    """``(ran, verified, before, after)`` of ``set_app_volume``'s result."""
    d = getattr(result, "details", None) or {}
    verified = d.get("verified") if isinstance(d.get("verified"), bool) else None
    ran = "before" in d or bool(verified)
    before = float(d["before"]) if isinstance(d.get("before"), (int, float)) else None
    after = float(d["after"]) if isinstance(d.get("after"), (int, float)) else None
    return ran, verified, before, after


def _mixer_noop(result: Any, after: float | None, say: Callable[[float], str],
                facts: Mapping[str, Any] | None = None, app: str | None = None) -> Any:
    """``set_app_volume``'s N1 no-op (a level asked that can't move: already there, or at its top or bottom; nothing
    was set) as a true no-op (``already``, read back), said as one (``say(level)``); None when it wasn't a no-op."""
    d = getattr(result, "details", None) or {}
    if not d.get("noop") or after is None:
        return None
    kw: dict[str, Any] = {"app": app} if app else {}
    return _rr(ran=False, already=True, verified=True, value=after, outcome="already", facts=dict(facts or {}),
               say=say(after), **kw)


def volume_spotify_mixer(call: Any) -> Any:
    """Spotify's own volume through its mixer entry (no window, no focus: runs with a game in front), **only** when
    the data records the slider and the mixer entry as one control both ways (M6). After the set the slider is read
    back when it can be read: a slider that didn't follow contradicts the link (never "done")."""
    if not mixer_linked():
        return _rr(ran=False, outcome="not_linked")
    level, change, mute = _amount(call.values)
    if level is None and change is None:
        return _rr(ran=False, outcome="mute_not_in_app" if mute is not None else "no_amount")
    image = _sd().process_image()
    win = DESK.window()
    facts: dict[str, Any] = {"linked": True}
    _status, _app_id, where = _this_pc(call, facts, win.pid if win is not None else None)
    if where == "other":
        return _other_device(facts, volume=True)
    before_slider = _read_slider(DESK, win.hwnd) if win is not None else None
    result = DESK.app_volume(image, level=level, change=change, mute=None)
    ran, verified, before, after = _mixer_after(result)
    if not ran:
        return _rr(ran=False, outcome="no_session", facts=facts)
    noop = _mixer_noop(result, after, lambda v: f"Spotify's own volume is already at {v:g}% (its mixer entry).", facts)
    if noop is not None:
        return noop
    followed = None
    if before_slider is not None and before_slider.status == "ok" and before_slider.slider is not None \
            and before is not None and after is not None and abs(after - before) > 1e-9:
        again = _read_slider(DESK, win.hwnd)
        if again.status == "ok" and again.slider is not None:
            followed = (float(again.slider) - float(before_slider.slider)) * (after - before) > 0
    facts["slider_followed"] = "unread" if followed is None else ("yes" if followed else "no")
    if followed is False:
        verified = False
    return _rr(ran=True, verified=verified, value=after, outcome="set" if verified else "not_verified", facts=facts,
               facts_ram={"level_app": image}, say=f"Spotify's own volume, {after:g}% (its mixer entry)."
               if after is not None else None)


def _title(app: str) -> str:
    try:
        from yuki.skills.uia_route import app_words

        return app_words(app) or app
    except Exception:  # noqa: BLE001
        return app


def volume_mixer(call: Any) -> Any:
    """Another app's own level through the Windows mixer (Core Audio, no focus: runs with a game in front). An app
    with its own control in the route data (``own_control``: Spotify) never gets its level from the mixer for an
    in-app request (never faked); it does for a mute (its own Mute is an unreadable toggle) and when the user's
    control for it is ``mixer``."""
    values = call.values
    target = str(values.get("target") or "")
    control = str(values.get("control") or "in_app")
    own = {str(a) for a in call.args.get("own_control") or []}
    not_apps = {str(a) for a in call.args.get("not_apps") or []}
    if not target or target in not_apps:
        return _rr(ran=False, outcome="not_an_app", app=target or None)
    level, change, mute = _amount(values)
    if _no_amount(level, change, mute):
        return _rr(ran=False, ask=ASK_AMOUNT, outcome="no_amount", app=target)
    if target in own and control != "mixer" and (level is not None or change is not None):
        return _rr(ran=False, outcome="own_control", app=target)
    facts: dict[str, Any] = {"own_control": target in own}
    win = before_slider = None
    if target in own and target == app_key():
        win = DESK.window()
        # review r2 M1: the "this PC" check before Spotify's mixer entry is set (a mute, or the user's "mixer" usual).
        # Spotify keeps a quiet local session while it plays on another device; setting that one would read back fine
        # and end "done" while the other device keeps playing (Q11: left alone, never "done"). The run's cached
        # verdict makes this free after an earlier rung.
        _status, _app_id, where = _this_pc(call, facts, win.pid if win is not None else None)
        if where == "other":
            return _other_device(facts, volume=True)
        before_slider = _read_slider(DESK, win.hwnd) if win is not None else None
    result = DESK.app_volume(target, level=level, change=change, mute=mute)
    ran, verified, before, after = _mixer_after(result)
    if not ran:
        return _rr(ran=False, outcome="no_app", app=target, facts=facts)
    where = " in the Windows mixer." if target in own else "."
    noop = _mixer_noop(result, after, lambda v: f"{_title(target)}'s volume is already at {v:g}%{where}", facts,
                       app=target)
    if noop is not None:
        return noop
    if before_slider is not None and before_slider.status == "ok" and win is not None:
        again = _read_slider(DESK, win.hwnd)          # after a mixer set for Spotify: the slider read back (facts)
        facts["slider_moved"] = "unread" if again.slider is None else (
            "yes" if abs(float(again.slider) - float(before_slider.slider or 0.0)) > 1e-9 else "no")
    who = _title(target)
    if level is None and change is None:
        say = f"{who} {'muted' if mute else 'unmuted'}."
    else:
        say = f"{who}'s volume, {after:g}%" + (" in the Windows mixer." if target in own else ".")
    return _rr(ran=True, verified=verified, value=after, outcome="set" if verified else "not_verified", app=target,
               facts=facts, facts_ram={"level_app": target}, say=say if verified else None)


def volume_system(call: Any) -> Any:
    """The whole PC's level through Core Audio (no key: runs with a game in front)."""
    level, change, mute = _amount(call.values)
    if _no_amount(level, change, mute):
        return _rr(ran=False, ask=ASK_AMOUNT, outcome="no_amount", app="master")
    if mute is None:
        # app_volume's N1 for the PC: a level that can't move (already there, or at its top or bottom) is a no-op,
        # read and said as one, and nothing is set
        now0 = DESK.master_level()
        if now0 is not None:
            want = max(0.0, min(100.0, float(level) if level is not None else now0 + float(change or 0.0)))
            if abs(want - now0) <= 0.6:
                return _rr(ran=False, already=True, verified=True, value=now0, outcome="already", app="master",
                           facts_ram={"level_app": "master"}, say=f"The PC's volume is already at {now0:g}%.")
    got = DESK.master(level=level, change=change, mute=mute)
    if not got:
        return _rr(ran=False, outcome="unavailable", app="master")
    ok = True
    now = float(got.get("level") or 0.0)
    if level is not None or change is not None:
        target = level if level is not None else max(0.0, min(100.0, float(got["before"]) + float(change or 0.0)))
        ok = abs(now - float(target)) <= 1.0
    if mute is not None:
        ok = ok and bool(got.get("muted")) == mute
    if level is None and change is None:
        say = "The PC is muted." if mute else "The PC is unmuted."
    else:
        say = f"The PC's volume, {now:g}%."
    return _rr(ran=True, verified=ok, value=now, outcome="set" if ok else "not_verified", app="master",
               facts_ram={"level_app": "master"}, say=say if ok else None)


def volume_own_voice(call: Any) -> Any:
    """Yuki's own voice: this process's own mixer entry (Core Audio, no focus)."""
    level, change, mute = _amount(call.values)
    if _no_amount(level, change, mute):
        return _rr(ran=False, ask=ASK_AMOUNT, outcome="no_amount")
    image = DESK.own_process()
    if not image:
        return _rr(ran=False, outcome="no_process")
    result = DESK.app_volume(image, level=level, change=change, mute=mute)
    ran, verified, _before, after = _mixer_after(result)
    if not ran:
        return _rr(ran=False, outcome="no_session", app=image)
    noop = _mixer_noop(result, after, lambda v: f"My voice is already at {v:g}%.", app=image)
    if noop is not None:
        return noop
    if level is None and change is None:
        say = "My voice is muted." if mute else "My voice is unmuted."
    else:
        say = f"My voice, {after:g}%."
    return _rr(ran=True, verified=verified, value=after, outcome="set" if verified else "not_verified", app=image,
               facts_ram={"level_app": image}, say=say if verified else None)


# ---------------------------------------------------------------------------------------------------------------------
# volume: the re-checks
# ---------------------------------------------------------------------------------------------------------------------


def _volume_seen(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """The thinker's finish gate for a pinned ``volume`` (no route result to compare with): a ``volume_change``
    moment of the target app (``system``: the PC's ``master``) written since the hand-off -> True; none yet -> None
    (moments lag; never False)."""
    target = str(args.get("target") or "")
    if not target or target == "own_voice":
        return None, {"status": "no_target"}
    from yuki.skills.donecheck import probe_moment

    return probe_moment(ctx, {"kind": "volume_change", "app": "master" if target == "system" else target})


def probe_slider_still(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """Spotify's slider re-read a few seconds after a set (the same source: flagged ``same_source`` in the data): at
    the level the route read back -> True; elsewhere -> False (put back, or moved by someone); unread -> None. As the
    thinker's finish gate (a hand-off: no route result), the target's volume change seen since (:func:`_volume_seen`)."""
    if getattr(ctx, "result", None) is None:
        return _volume_seen(ctx, args)
    want = getattr(getattr(ctx, "result", None), "value", None)
    if not isinstance(want, (int, float)):
        return None, {"status": "no_value"}
    win = DESK.window()
    if win is None:
        return None, {"status": "no_window"}
    look = _read_slider(DESK, win.hwnd)
    if look.status != "ok" or look.slider is None:
        return None, {"status": look.status}
    return abs(float(look.slider) - float(want)) <= LEVEL_TOLERANCE, {"status": "read"}


def probe_level_still(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """A mixer entry's (or the PC's) level re-read by Core Audio a few seconds after a set: at the level read back ->
    True; elsewhere -> False; unread -> None. As the thinker's finish gate: :func:`_volume_seen`."""
    result = getattr(ctx, "result", None)
    if result is None:
        return _volume_seen(ctx, args)
    want = getattr(result, "value", None)
    app = (getattr(result, "facts_ram", None) or {}).get("level_app")
    if not isinstance(want, (int, float)) or not app:
        return None, {"status": "no_value"}
    now = DESK.master_level() if app == "master" else DESK.mixer_level(str(app))
    if now is None:
        return None, {"status": "unread"}
    return abs(float(now) - float(want)) <= LEVEL_TOLERANCE, {"status": "read"}


# ---------------------------------------------------------------------------------------------------------------------
# The engine's hooks
# ---------------------------------------------------------------------------------------------------------------------


def window_state(app: str = "") -> str | None:
    """The ``game = "states"`` hook (``state_reader = "spotify_route"``): Spotify's window state now (front / covered /
    visible / minimised), whatever skill asks (the volume skill's only windowed rung is Spotify's slider); None when
    Spotify has no window (such rungs then stay off with a game in front)."""
    try:
        win = DESK.window()
        return DESK.state(win.hwnd) if win is not None else None
    except Exception:  # noqa: BLE001
        return None


#: The rungs (``call = "<name>"`` in the connection files).
ROUTES: dict[str, Callable[[Any], Any]] = {
    "play_library": play_library,
    "play_restored": play_restored,
    "play_link": play_link,
    "play_search": play_search,
    "play_launch": play_launch,
    "volume_slider": volume_slider,
    "volume_spotify_mixer": volume_spotify_mixer,
    "volume_keys": volume_keys,
    "volume_restored": volume_restored,
    "volume_mixer": volume_mixer,
    "volume_system": volume_system,
    "volume_own_voice": volume_own_voice,
}

#: The done-checks and re-checks (``probe = "driver"``, ``call = "<name>"``).
PROBES: dict[str, Callable[[Any, Mapping[str, Any]], tuple[bool | None, dict[str, Any]]]] = {
    "playing_here": probe_playing_here,
    "still_playing": probe_still_playing,
    "slider_still": probe_slider_still,
    "level_still": probe_level_still,
}

#: The rungs that neither take focus nor send input (their data says ``needs_focus = false``); a test walks their
#: code and refuses any key, focus, link or launch reachable from them (H1).
NO_FOCUS_ROUTES: tuple[str, ...] = ("play_library", "play_search", "volume_slider", "volume_spotify_mixer",
                                    "volume_mixer", "volume_system", "volume_own_voice")


__all__ = [
    "ASK_AMOUNT", "CONNECTION_FILE", "DESK", "Desk", "KEYS_MAX", "LINE_OTHER", "LINE_OTHER_AFTER", "LINE_OTHER_VOLUME",
    "LINE_PLAY_UNCONFIRMED", "Look", "NO_FOCUS_ROUTES", "POLL_S", "PROBES", "REQUEST_WARM_S", "ROUTES", "THIS_PC_S",
    "app_key", "extra_control", "forget_warm", "link_for", "look_page", "look_row", "look_slider", "mixer_linked",
    "proven", "spotify_data", "where_playing", "window_state",
]
