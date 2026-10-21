"""Situation: the desk as a picture in plain words, per monitor (P1.1, 18 Oct free-thinker plan).

The deciders used to get the raw window list (``windows.format_overview``): coordinates, in Z-order, never said to be
in Z-order. A model had to do rectangle maths to learn that Discord sits maximised on the main screen over the video
that plays. This module turns the reads Yuki already makes into that picture, in code, in a few milliseconds:

* :func:`snapshot` reads the desk once into a :class:`DeskState`: the monitors, every window (user windows, plus the
  popup that is in front or holds the keyboard) with its monitor, Z place, visible share, maximised / full screen /
  minimised, foreground and keyboard; the media sessions, each tied to the window it plays in when that can honestly
  be told; the apps' own volumes and the master volume. All OS facts: ``EnumWindows`` (``windows.list_windows``), the
  occlusion grid (``visibility.layout``, ~1.3 ms p50), ``EnumDisplayMonitors``, the WinRT media sessions, the Core
  Audio mixer. No screenshot, no pixel, no UI Automation, no title read for meaning.
* :func:`render` says it in plain words for a decider: per monitor, top window first, ``[hwnd]`` ids kept for the
  hands, "in front", "keyboard", "maximised", "playing", "N% visible, under [hwnd] App", then what plays, the
  volumes, the holds (game, call, meeting: the you-now record's words), and what the user just asked and what Yuki
  just did (the caller supplies those two). Lines give way by priority under ``max_chars``.

Which window plays (no guessing): a session's app is matched to windows by identity (the session's AppUserModelID
against the process's own package identity, else the same app key as the process image, ``actions.media.app_key_of``).
One window of that app: it plays there. Several: the one whose title contains the session's title (two strings the OS
and the app wrote, compared as text: the timeline's own test, ``timeline._shows``); else the window is not known and
the render says the app only.

``DeskState`` and its records are dataclasses that also read like dicts (``w.hwnd`` and ``w["hwnd"]`` both work), so
the lanes that code against ``{hwnd, app, ...}`` and the ones that use attributes get the same thing. RAM only;
nothing here logs content (:meth:`DeskState.facts` is the content-free summary for log lines).

Public API::

    snapshot(*, media=None, audio=None, master=None, holds=None, you=None) -> DeskState
    from_overview(payload, *, shares=None, media=None, audio=None, master=None, holds="", keyboard=None) -> DeskState
    warm() -> None                                 # at start: the first read's COM/WinRT costs, in the background
    render(state, *, purpose="talker" | "planner", max_chars=900, just_asked="", just_did="") -> str
    sound_line(state) -> str                       # the Sound line alone (the mixer's levels labelled as such)
    mixer_rows(rows, apps) -> list                 # pure: the mixer rows of the apps named
    monitor_of(rect, monitors) -> int | None       # pure: the monitor holding most of a rectangle
    coverer(state, window) -> Window | None        # pure: the window above that covers most of it
    front_window(raw=None) -> (hwnd, how)          # fa (A5): the window in front now, by the one front rule
    front_rule(raw, keyboard, root_owner, listed, popup) -> (hwnd, how)   # pure: that rule

morning21 (lane B, contract K3): a monitor is said by its place and its own model name ("[m1] second monitor (VG271U,
2560x1440; screen off)"): the ``[n]`` and ``[mN]`` ids are the hands' only, never words to say (integ29 LD3: no
"Monitor 1" a planner's question would copy, said by code word for word); "screen off" only from the display
driver's own recent read. The notification area's icons are one line ("In the tray: Twinkle Tray (monitor app),
Steam; hidden icons: ..."), from the last tray read (``windows.tray_icons``; nothing is read for a desk).

**The one front rule** (fa, final-F #4a): the front is ``GetForegroundWindow`` when it is a listed window or a popup
the desk keeps; a foreground on no list stands for the listed window holding the keyboard, else its listed root owner.
:func:`snapshot` and :func:`front_window` both apply it, so a diff's ``focus_to`` and a guard's "front now" read the
same window; ``DeskState.front_how`` says which case it was (a code word, logged).
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Iterable, Sequence

#: Visible share at or above which a window counts as fully visible (the 16 px grid's rounding at the edges).
FULLY_VISIBLE = 0.97
#: Below this visible share a window cannot be seen at all (``visibility.VISIBLE_MIN_SHARE``).
HIDDEN = 0.03
#: A media title compared with window titles: shorter ones say too little to tie a session to one window.
MEDIA_TITLE_MIN = 3
#: Characters of a window title said in the render.
TITLE_CHARS = 60
#: The media read's bound when :func:`snapshot` reads it itself (the session manager answers in 1-8 ms once warm).
MEDIA_TIMEOUT_S = 0.08
#: The you-now read's bound when :func:`snapshot` asks the memory service for the holds itself.
YOU_WAIT_S = 0.03


class _Rec:
    """Attribute records that also read like dicts (``r["hwnd"]``, ``r.get("hwnd")``, ``dict(r.items())``)."""

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and hasattr(self, key)

    def keys(self) -> list[str]:
        return [f.name for f in fields(self)]  # type: ignore[arg-type]

    def items(self) -> list[tuple[str, Any]]:
        return [(k, getattr(self, k)) for k in self.keys()]

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in self.items():
            if isinstance(v, _Rec):
                v = v.to_dict()
            elif isinstance(v, list):
                v = [x.to_dict() if isinstance(x, _Rec) else x for x in v]
            out[k] = v
        return out


@dataclass
class Monitor(_Rec):
    """One display: ``rect`` the whole monitor, ``work`` without the taskbar (virtual-desktop px). morning21 B3:
    ``label`` its own model name (EDID, ``VG271U``; ``""`` none), ``role`` its place (``main``, ``second``, ...:
    ``windows.monitor_roles``), ``power`` its screen as the display driver last read it (``off`` / ``on`` / ``""``
    not known; ``windows.display_power``)."""

    index: int
    rect: tuple[int, int, int, int]
    work: tuple[int, int, int, int]
    primary: bool = False
    dpi_scale: float = 1.0
    label: str = ""
    role: str = ""
    power: str = ""


@dataclass
class Window(_Rec):
    """One window on the desk.

    ``rect``: what DWM draws (no invisible borders) when it is on screen, else the window rectangle; ``monitor``: the
    index of the monitor holding most of it (``None`` when minimised or off every monitor); ``z``: 0 = top;
    ``visible``: the share of it no window above covers (0..1; 0 when minimised); ``keyboard``: keys typed now go to it.
    ``kind`` is ``window`` (one the user can switch to) or ``popup`` (only kept when it is in front or holds the
    keyboard: a menu, a picture-in-picture, a toast). ``playing``: a media session plays in this window.
    """

    hwnd: int
    app: str
    title: str
    rect: tuple[int, int, int, int]
    monitor: int | None
    z: int
    visible: float
    maximized: bool = False
    fullscreen: bool = False
    minimized: bool = False
    foreground: bool = False
    keyboard: bool = False
    kind: str = "window"
    pid: int = 0
    owner: int = 0
    playing: bool = False


@dataclass
class Media(_Rec):
    """One media session: its app, title, whether it plays, and the window it plays in (``None``: not known)."""

    app: str
    title: str
    playing: bool
    hwnd: int | None = None
    status: str = ""
    app_id: str = ""
    current: bool = False


@dataclass
class Audio(_Rec):
    """One app's own mixer volume (0-100), mute, and whether its sound is active now."""

    app: str
    level: float
    muted: bool
    playing: bool = False


@dataclass
class DeskState(_Rec):
    """The desk at one instant (:func:`snapshot`). ``errors``: the parts that could not be read (``media``, ``audio``,
    ``master``, ``windows``): a diff never reads a change into a part that was not read on both sides."""

    monitors: list[Monitor] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)
    media: list[Media] = field(default_factory=list)
    audio: list[Audio] = field(default_factory=list)
    master: dict[str, Any] | None = None
    at: float = 0.0
    foreground: int | None = None
    keyboard: int | None = None
    #: The you-now record's focus words for a game, a call, a meeting ("in VALORANT; on a Discord call"), "" for none.
    holds: str = ""
    errors: dict[str, str] = field(default_factory=dict)
    ms: float = 0.0
    #: fa (A5): how :attr:`foreground` was found by the one front rule (:func:`front_rule`): ``listed``, ``popup``,
    #: ``keyboard``, ``owner``, ``unlisted``, ``none``; ``""`` when not read by :func:`snapshot` (a code word, logged).
    front_how: str = ""
    #: morning21 B7: the notification area's icons (``windows.TrayIcon`` records or their dicts) from the last tray
    #: read (never read for this desk: ``windows.tray_icons``' cache).
    tray: list[Any] = field(default_factory=list)

    def window(self, hwnd: Any) -> Window | None:
        try:
            key = int(hwnd)
        except (TypeError, ValueError):
            return None
        return next((w for w in self.windows if w.hwnd == key), None)

    def facts(self) -> dict[str, Any]:
        """Content-free, for log lines: counts and timing only (never a title or an app's words)."""
        return {"monitors": len(self.monitors), "windows": sum(1 for w in self.windows if w.kind == "window"),
                "popups": sum(1 for w in self.windows if w.kind == "popup"),
                "playing": sum(1 for m in self.media if m.playing),
                "playing_mapped": sum(1 for m in self.media if m.playing and m.hwnd),
                "errors": sorted(self.errors), "ms": round(self.ms, 1), "front_how": self.front_how or None,
                "tray": len(self.tray), "screens_off": sum(1 for m in self.monitors if m.power == "off")}


# ---------------------------------------------------------------------------
# Pure geometry (no OS): usable on fake desks
# ---------------------------------------------------------------------------

def _area(r: Sequence[int]) -> int:
    return max(0, int(r[2]) - int(r[0])) * max(0, int(r[3]) - int(r[1]))


def _overlap(a: Sequence[int], b: Sequence[int]) -> int:
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def monitor_of(rect: Sequence[int] | None, monitors: Sequence[Monitor]) -> int | None:
    """The index of the monitor holding most of ``rect`` (what ``MonitorFromWindow`` answers), ``None`` off all."""
    if not rect or not monitors:
        return None
    best, best_area = None, 0
    for m in monitors:
        got = _overlap(rect, m.rect)
        if got > best_area:
            best, best_area = m.index, got
    return best


def coverer(state: DeskState, window: Window) -> Window | None:
    """The window above ``window`` (smaller ``z``) that covers most of it, or ``None`` (nothing above overlaps it).
    Pure geometry on the state's rectangles."""
    if window.minimized:
        return None
    best, best_area = None, 0
    for w in state.windows:
        if w.z >= window.z or w.minimized or w.hwnd == window.hwnd:
            continue
        got = _overlap(w.rect, window.rect)
        if got > best_area:
            best, best_area = w, got
    return best


def _key(name: str) -> str:
    """An app key: the image name casefolded without ``.exe`` (how memory keys apps)."""
    part = str(name or "").strip().casefold()
    return part[:-4].rstrip() if part.endswith(".exe") else part


def _squash(text: str) -> str:
    return " ".join(str(text or "").casefold().split())


def map_media(media: list[Media], windows: list[Window], identity: dict[int, str] | None = None) -> None:
    """Tie each session to the window it plays in, in place (``hwnd``; the window's ``playing``). Pure.

    ``identity``: ``{pid: the process's AppUserModelID, casefolded}`` for packaged apps, when read. A session's
    windows are those of its app by identity: the same AUMID, else the same app key (``actions.media.app_key_of`` of
    the session id against the image name). One such window: it plays there. Several: the one whose title contains
    the session's title (text the OS and the app wrote); none or several of those: not known."""
    identity = identity or {}
    for m in media:
        sid = str(m.app_id or "").strip().casefold()
        key = sid.rsplit("!", 1)[-1]
        key = key[:-4].rstrip() if key.endswith(".exe") else key
        mine = [w for w in windows if w.kind == "window"
                and ((sid and identity.get(w.pid) == sid) or (key and _key(w.app) == key))]
        if not mine:
            continue
        pick: Window | None = mine[0] if len(mine) == 1 else None
        if pick is None:
            title = _squash(m.title)
            if len(title) >= MEDIA_TITLE_MIN:
                named = [w for w in mine if title in _squash(w.title)]
                pick = named[0] if len(named) == 1 else None
        if pick is not None:
            m.hwnd = pick.hwnd
            if m.playing:
                pick.playing = True
        if not m.app or m.app.casefold() == key:
            m.app = mine[0].app   # the session says its app as its windows do


# ---------------------------------------------------------------------------
# The read (OS, read-only)
# ---------------------------------------------------------------------------

_user32 = ctypes.windll.user32
#: pid -> image name, for the popup in front (user windows carry theirs); capped, RAM only.
_names: dict[int, str] = {}


def _image(pid: int) -> str:
    if pid in _names:
        return _names[pid]
    try:
        import psutil

        name = psutil.Process(pid).name()
    except Exception:  # noqa: BLE001
        name = ""
    if len(_names) > 256:
        _names.clear()
    _names[pid] = name
    return name


def _keyboard_root(foreground: int) -> int:
    """The top-level window keys typed now land in: the root of the control the foreground thread has focused, else
    the foreground window itself."""
    if not foreground:
        return 0
    try:
        from yuki.perception.windows import _GA_ROOT, focused_control_hwnd

        focus = focused_control_hwnd(foreground)
        if focus:
            root = int(_user32.GetAncestor(ctypes.c_void_p(focus), _GA_ROOT) or 0)
            if root:
                return root
    except Exception:  # noqa: BLE001
        pass
    return foreground


#: ``GetAncestor(hwnd, GA_ROOTOWNER)``: the top of a window's owner chain.
_GA_ROOTOWNER = 3


def _root_owner(hwnd: int) -> int:
    try:
        return int(_user32.GetAncestor(ctypes.c_void_p(int(hwnd)), _GA_ROOTOWNER) or 0)
    except Exception:  # noqa: BLE001
        return 0


def front_rule(raw: int, keyboard: int, root_owner: int, listed: Callable[[int], bool],
               popup: Callable[[int], bool]) -> tuple[int, str]:
    """fa (final-F #4a, A5): the one front rule, pure. ``raw``: ``GetForegroundWindow``; ``keyboard``: the top-level
    window that holds the keyboard (:func:`_keyboard_root`); ``root_owner``: the top of ``raw``'s owner chain;
    ``listed(h)``: a window the user can see and switch to (``windows.is_user_window``, the window list's rule);
    ``popup(h)``: a popup the desk lists when it is in front (``visibility``'s kind ``popup``). Returns ``(front, how)``:

    * ``raw`` itself when it is listed (``listed``) or a popup in front (``popup``): as before;
    * else the listed window holding the keyboard (``keyboard``), else the listed root owner (``owner``): a foreground
      that is on no list (final-F one p1_spotify_down_focus_back: a Spotify window off the list held the front while
      the listed Spotify window held the keyboard, so the front read "nothing" and no tidy step was found) stands for
      the window people see in front;
    * else ``raw`` (``unlisted``); ``(0, "none")`` with no foreground.

    Window facts only. Never raises."""
    try:
        raw = int(raw or 0)
        if not raw:
            return 0, "none"
        if listed(raw):
            return raw, "listed"
        if popup(raw):
            return raw, "popup"
        if keyboard and int(keyboard) != raw and listed(int(keyboard)):
            return int(keyboard), "keyboard"
        if root_owner and int(root_owner) != raw and listed(int(root_owner)):
            return int(root_owner), "owner"
        return raw, "unlisted"
    except Exception:  # noqa: BLE001
        return int(raw or 0), "unlisted"


def front_window(raw: int | None = None) -> tuple[int, str]:
    """fa (A5): the window in front now by the one front rule (:func:`front_rule`), read-only Win32 (a few ms): the
    reader for a guard that compares the front now with a snapshot's ``foreground`` (``hands._front_now``, launch's
    in-view check), so both read the same window. ``(0, "none")`` when there is no foreground. Never raises."""
    try:
        from yuki.perception.visibility import _classify, frame_bounds
        from yuki.perception.windows import is_user_window

        fg = int(raw if raw is not None else (_user32.GetForegroundWindow() or 0))

        def popup(h: int) -> bool:
            rect = frame_bounds(h)
            kind = _classify(h, rect) if rect is not None else None
            return bool(kind and kind[0] == "popup")

        return front_rule(fg, _keyboard_root(fg), _root_owner(fg), is_user_window, popup)
    except Exception:  # noqa: BLE001
        try:
            fg = int(_user32.GetForegroundWindow() or 0)
        except Exception:  # noqa: BLE001
            fg = 0
        return (fg, "unlisted") if fg else (0, "none")


def _app_name(process_name: str) -> str:
    name = str(process_name or "").strip()
    return name[:-4] if name.casefold().endswith(".exe") else name


def _media_from(raw: Iterable[Any]) -> list[Media]:
    out: list[Media] = []
    for s in raw or []:
        if not isinstance(s, dict) and not hasattr(s, "get"):
            continue
        app_id = str(s.get("app_id") or "")
        status = str(s.get("status") or "unknown")
        key = app_id.strip().rsplit("!", 1)[-1]
        out.append(Media(app=_app_name(key), title=str(s.get("title") or ""), playing=status == "playing",
                         status=status, app_id=app_id, current=bool(s.get("current"))))
    return out


def _audio_from(raw: Iterable[Any]) -> list[Audio]:
    out: list[Audio] = []
    for a in raw or []:
        if not hasattr(a, "get") or not a.get("app"):
            continue
        out.append(Audio(app=_app_name(str(a.get("app"))), level=float(a.get("level") or 0.0),
                         muted=bool(a.get("muted")), playing=bool(a.get("playing"))))
    return out


def _mixer_key(name: Any) -> str:
    """An app as the mixer matches it (``app_volume._key``: casefolded, no ``.exe``, letters and digits only)."""
    part = str(name or "").casefold().strip()
    part = part[:-4] if part.endswith(".exe") else part
    return "".join(ch for ch in part if ch.isalnum())


def mixer_rows(rows: Iterable[Any], apps: Iterable[Any]) -> list[Any]:
    """The mixer rows (``app_volume.audio_apps``' dicts) of the apps named in ``apps``, matched as the volume hand
    matches an app (``app_volume.matching``: the same process name, else one name holding the other, 3 characters
    or more). Pure."""
    rows = [r for r in rows or [] if hasattr(r, "get") and r.get("app")]
    out: list[Any] = []
    for want in dict.fromkeys(_mixer_key(a) for a in apps or ()):
        if not want:
            continue
        exact = [r for r in rows if _mixer_key(r.get("app")) == want]
        got = exact or [r for r in rows if len(_mixer_key(r.get("app"))) >= 3
                        and (_mixer_key(r.get("app")) in want or want in _mixer_key(r.get("app")))]
        out.extend(r for r in got if not any(r is x for x in out))
    return out


def _holds_from(you: Any) -> str:
    try:
        from yuki.memory.younow import _focus_phrase, holds_now

        return _focus_phrase(you, set(holds_now(you, time.time())), talker=True) if you else ""
    except Exception:  # noqa: BLE001
        return ""


def snapshot(*, media: Iterable[Any] | None = None, audio: Iterable[Any] | None = None,
             master: dict[str, Any] | None | bool = None, holds: str | None = None, you: Any = None) -> DeskState:
    """Read the desk now (read-only, a few ms; measured on this desk: windows + layout ~3-4 ms, media 1-8 ms, mixer
    ~9 ms, master ~4 ms). Never raises: a part that fails is left empty and named in ``errors``.

    A caller that already read a part passes it and it is not read again: ``media`` (``system.media_sessions``'
    dicts), ``audio`` (``app_volume.audio_apps``' dicts; ``[]`` to skip the mixer), ``master``
    (``audio.master_volume``'s dict; ``False`` to skip it, ``True`` or ``None`` to read it). final-B (19 Oct): a
    look after acting reads the sound only for what the act was about, so ``audio`` may also name apps (strings): the
    mixer is read and only those apps' rows are kept (:func:`mixer_rows`; dicts in the same list are kept as given).
    ``holds``: the focus words, when the caller has them; else ``you`` (a you-now record) is rendered; else the memory
    service is asked (bounded :data:`YOU_WAIT_S`); pass ``holds=""`` to skip (a diff needs none)."""
    started = time.perf_counter()
    state = DeskState(at=time.time())
    try:
        from yuki.perception.visibility import layout
        from yuki.perception.windows import list_monitors, list_windows

        state.monitors = [Monitor(index=m.index, rect=tuple(m.bounds), work=tuple(m.work_area), primary=m.is_primary,
                                  dpi_scale=float(m.dpi_scale), label=str(getattr(m, "label", "") or ""),
                                  role=str(getattr(m, "role", "") or ""), power=str(getattr(m, "power", "") or ""))
                          for m in list_monitors()]
        infos = list_windows()
        lay = layout()
        raw = int(lay.foreground or _user32.GetForegroundWindow() or 0)
        kb = _keyboard_root(raw)
        surfaces = {s.hwnd: s for s in lay.surfaces}
        rank = {s.hwnd: i for i, s in enumerate(lay.surfaces)}
        # fa (A5): the one front rule (:func:`front_rule`) over this read's own lists: a foreground on no list stands
        # for the listed window holding the keyboard, else its listed root owner (focus_to, focus_by_act, the render
        # and the tidy guard then all read that one window)
        ids = {int(i.hwnd) for i in infos}
        fg, state.front_how = front_rule(
            raw, kb, _root_owner(raw) if raw and raw not in ids else 0, lambda h: int(h) in ids,
            lambda h: getattr(surfaces.get(int(h)), "kind", "") == "popup")
        state.foreground, state.keyboard = (fg or None), (kb or None)
        out: list[Window] = []
        for info in infos:
            s = surfaces.get(info.hwnd)
            rect = tuple(s.rect) if s is not None else tuple(info.bounds)
            minimized = bool(info.is_minimized)
            out.append(Window(
                hwnd=int(info.hwnd), app=_app_name(info.process_name), title=info.title, rect=rect,
                monitor=None if minimized else monitor_of(rect, state.monitors),
                z=rank.get(info.hwnd, len(lay.surfaces) + len(out)),
                visible=float(s.visible_share) if s is not None else 0.0,
                maximized=bool(info.is_maximized) and not minimized, fullscreen=bool(info.is_fullscreen),
                minimized=minimized, foreground=info.hwnd == fg, keyboard=info.hwnd == kb, kind="window",
                pid=int(info.pid), owner=int(info.owner_hwnd or 0)))
        listed = {w.hwnd for w in out}
        for s in lay.surfaces:   # the popup in front or with the keyboard (a menu, a picture-in-picture, a toast)
            if s.kind == "popup" and s.hwnd not in listed and s.hwnd in (fg, kb):
                out.append(Window(hwnd=int(s.hwnd), app=_app_name(_image(s.pid)), title=s.title, rect=tuple(s.rect),
                                  monitor=monitor_of(s.rect, state.monitors), z=rank[s.hwnd],
                                  visible=float(s.visible_share), foreground=s.hwnd == fg, keyboard=s.hwnd == kb,
                                  kind="popup", pid=int(s.pid), owner=int(s.owner or 0)))
        out.sort(key=lambda w: w.z)
        state.windows = out
    except Exception as exc:  # noqa: BLE001
        state.errors["windows"] = type(exc).__name__
    try:
        from yuki.perception.windows import tray_icons

        state.tray = tray_icons(refresh=False)   # morning21 B7: the last tray read only (nothing is read here)
    except Exception:  # noqa: BLE001
        state.tray = []

    if media is None:
        try:
            from yuki.perception.system import media_sessions

            media = media_sessions(timeout_s=MEDIA_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - an earlier read still waiting, WinRT down
            media = []
            state.errors["media"] = type(exc).__name__
    state.media = _media_from(media)
    if state.media and state.windows:
        identity: dict[int, str] = {}
        try:
            from yuki.perception.windows import _facts_for

            for w in state.windows:
                if w.pid and w.pid not in identity:
                    identity[w.pid] = _facts_for(w.pid)[0]
        except Exception:  # noqa: BLE001 - then by app key only
            pass
        map_media(state.media, state.windows, identity)

    named: list[Any] = []
    if audio is not None:
        audio = [audio] if isinstance(audio, str) else list(audio)
        named = [a for a in audio if isinstance(a, str) and a.strip()]
        audio = [a for a in audio if not isinstance(a, str)]
    if audio is None or named:
        try:
            from yuki.actions.app_volume import audio_apps

            read = audio_apps(limit=12 if audio is None else 32)
        except Exception as exc:  # noqa: BLE001
            read = []
            state.errors["audio"] = type(exc).__name__
        audio = read if audio is None else [*audio, *mixer_rows(read, named)]
    state.audio = _audio_from(audio)
    if master is None or master is True:
        try:
            from yuki.perception.audio import master_volume

            master = master_volume()
        except Exception:  # noqa: BLE001
            master = None
        if master is None:
            state.errors["master"] = "unread"
    state.master = dict(master) if isinstance(master, dict) else None

    if holds is not None:
        state.holds = str(holds)
    elif you is not None:
        state.holds = _holds_from(you)
    else:
        try:
            from yuki.memory.now import shared_client

            client = shared_client()
            record = client.you(sections=("focus",), wait_s=YOU_WAIT_S) if client is not None else None
            state.holds = _holds_from(record) if record else ""
        except Exception:  # noqa: BLE001 - no service: no holds said
            state.holds = ""
    state.ms = (time.perf_counter() - started) * 1000.0
    return state


def warm() -> None:
    """Pay the first read's one-off costs in the background (returns at once): the WinRT media manager (~120 ms cold)
    and the Core Audio COM import (~250 ms cold), so the first :func:`snapshot` before an act is ms; and the first
    tray read (morning21 B7: the desk's tray line has names from the first request on). Never raises."""
    import threading

    def run() -> None:
        try:
            from yuki.perception.system import warm_activity

            warm_activity()
            from yuki.actions.app_volume import audio_apps

            audio_apps(limit=1)
        except Exception:  # noqa: BLE001
            pass
        try:
            from yuki.perception.windows import warm_tray

            warm_tray()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=run, name="yuki-situation-warm", daemon=True).start()


def from_overview(payload: dict[str, Any], *, shares: dict[int, float] | None = None,
                  media: Iterable[Any] | None = None, audio: Iterable[Any] | None = None,
                  master: dict[str, Any] | None | bool = None, holds: str = "", keyboard: int | None = None,
                  identity: dict[int, str] | None = None) -> DeskState:
    """A :class:`DeskState` from an overview already read (``Dispatcher.overview_text``'s payload: the plain
    ``windows.DesktopOverview``), so a caller that holds one reads no window twice, and a stand-in desk (the live
    harness, a test backend) gets the same picture as the real one. Pure: no OS call.

    ``shares``: ``{hwnd: visible share}`` when measured (the Now board's layout); else computed here from the
    rectangles top first (``visibility.visible_shares``, the same 16 px grid, on window rectangles). ``keyboard``: the
    top-level window holding the keyboard when known, else the foreground. ``media`` / ``audio`` / ``master``: the
    reads as :func:`snapshot` takes them (``None``: not read, said in ``errors``)."""
    started = time.perf_counter()
    state = DeskState(at=float(payload.get("captured_at") or time.time()))
    monitors = []
    for m in payload.get("monitors") or []:
        if not hasattr(m, "get"):
            continue
        rect = tuple(int(v) for v in (m.get("bounds") or m.get("rect") or (0, 0, 0, 0)))
        work = tuple(int(v) for v in (m.get("work_area") or m.get("work") or rect))
        monitors.append(Monitor(index=int(m.get("index") or 0), rect=rect, work=work,
                                primary=bool(m.get("is_primary", m.get("primary", False))),
                                dpi_scale=float(m.get("dpi_scale") or 1.0), label=str(m.get("label") or ""),
                                role=str(m.get("role") or ""), power=str(m.get("power") or "")))
    if not monitors and payload.get("virtual_bounds"):
        rect = tuple(int(v) for v in payload["virtual_bounds"])
        monitors.append(Monitor(index=0, rect=rect, work=rect, primary=True))
    state.monitors = monitors
    state.tray = [t for t in payload.get("tray") or [] if hasattr(t, "get") or hasattr(t, "name")]
    fg = int(payload.get("foreground_hwnd") or 0)
    kb = int(keyboard or fg or 0)
    state.foreground, state.keyboard = (fg or None), (kb or None)
    raw = [w for w in payload.get("windows") or [] if hasattr(w, "get") and w.get("hwnd") is not None]
    shown = [w for w in raw if not w.get("is_minimized")]
    if shares is None:
        try:
            from yuki.perception.visibility import visible_shares

            measured = visible_shares([tuple(int(v) for v in w.get("bounds") or (0, 0, 0, 0)) for w in shown],
                                      [m.rect for m in monitors])
        except Exception:  # noqa: BLE001
            measured = [1.0 if w.get("is_foreground") else 0.0 for w in shown]
        shares = {int(w["hwnd"]): s for w, s in zip(shown, measured)}
    out: list[Window] = []
    for z, w in enumerate(raw):
        hwnd = int(w["hwnd"])
        rect = tuple(int(v) for v in w.get("bounds") or (0, 0, 0, 0))
        minimized = bool(w.get("is_minimized"))
        out.append(Window(hwnd=hwnd, app=_app_name(str(w.get("process_name") or w.get("app") or "")),
                          title=str(w.get("title") or ""), rect=rect,
                          monitor=None if minimized else monitor_of(rect, monitors), z=z,
                          visible=0.0 if minimized else float(shares.get(hwnd, 0.0)),
                          maximized=bool(w.get("is_maximized")) and not minimized,
                          fullscreen=bool(w.get("is_fullscreen")), minimized=minimized, foreground=hwnd == fg,
                          keyboard=hwnd == kb, kind="window", pid=int(w.get("pid") or 0),
                          owner=int(w.get("owner_hwnd") or 0)))
    state.windows = out
    if media is None:
        state.errors["media"] = "unread"
    state.media = _media_from(media or [])
    if state.media and out:
        map_media(state.media, out, identity)
    if audio is None:
        state.errors["audio"] = "unread"
    state.audio = _audio_from(audio or [])
    if master is None or master is True:   # ``True`` (read it) is no read here: this is pure
        state.errors["master"] = "unread"
    state.master = dict(master) if isinstance(master, dict) else None
    state.holds = str(holds or "")
    state.ms = (time.perf_counter() - started) * 1000.0
    return state


# ---------------------------------------------------------------------------
# The words
# ---------------------------------------------------------------------------

def _title(text: str, cap: int = TITLE_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def name_of(w: Window | None, *, title: bool = True, cap: int = TITLE_CHARS) -> str:
    """``[hwnd] App "title"`` (the hands' id kept; the title cut to ``cap``), or ``[hwnd] App`` without the title."""
    if w is None:
        return "a window"
    head = f"[{w.hwnd}] {w.app or 'unknown app'}"
    t = _title(w.title, cap) if title and cap > 0 else ""
    return f'{head} "{t}"' if t and _squash(t) != _squash(w.app) else head


def _percent(share: float) -> int:
    return int(round(max(0.0, min(1.0, float(share))) * 100))


def _window_words(state: DeskState, w: Window, *, planner: bool, cap: int = TITLE_CHARS) -> str:
    parts: list[str] = []
    if w.foreground:
        parts.append("in front")
    if w.keyboard:
        parts.append("has the keyboard")
    if w.fullscreen:
        parts.append("full screen")
    elif w.maximized:
        parts.append("maximised")
    media = next((m for m in state.media if m.hwnd == w.hwnd), None)
    if w.playing:
        parts.append("playing")
    elif media is not None and media.status in ("paused", "stopped"):
        parts.append(media.status)
    if w.kind == "popup":
        parts.append("a pop-up" + (f" of [{w.owner}]" if w.owner else ""))
    if not w.foreground:
        if w.visible >= FULLY_VISIBLE:
            parts.append("fully visible")
        else:
            above = coverer(state, w)
            under = f", under {name_of(above, title=False)}" if above is not None else ""
            parts.append(("hidden" if w.visible < HIDDEN else f"{_percent(w.visible)}% visible") + under)
    if planner:
        left, top, right, bottom = w.rect
        parts.append(f"@{left},{top} {right - left}x{bottom - top}")
    return f"{name_of(w, cap=cap)} " + ", ".join(parts)


def _priority(w: Window) -> int:
    if w.foreground or w.keyboard or w.playing:
        return 1
    if w.visible >= 0.25:
        return 2
    if w.visible >= HIDDEN:
        return 3
    return 4


#: Characters of the caller's "Just asked" / "Just did" words said in the render.
SAID_CHARS = 120
#: How the render says less when the desk does not fit, tried in turn: (title chars, asked/did chars, short header,
#: lowest priority that may give way). Hidden and minimised windows and the volumes go first; titles get shorter
#: before a window the user can see (25% or more) gives way.
_LEVELS = ((TITLE_CHARS, SAID_CHARS, False, 3), (30, SAID_CHARS, False, 3), (30, SAID_CHARS, False, 2),
           (12, 80, True, 2), (0, 60, True, 2))
#: The last resort shortens the longest lines, never below this many characters each.
_LINE_MIN = 24


def _where(state: DeskState, m: Media) -> tuple[str, str]:
    """A playing session's app as said, and where it plays: its window when tied to one; "no window open" only when
    the app is known (a mixer entry of that app) and has no window; else "which window is not known". A session id
    that names no window's app and no mixer app (a hashed id) is said as an id, not as an app's name."""
    if m.hwnd:
        return m.app, f" in [{m.hwnd}]"
    key = _key(m.app)
    if any(_key(w.app) == key for w in state.windows):
        return m.app, " (which window is not known)"
    if any(_key(a.app) == key for a in state.audio):
        return m.app, " (no window open)"
    if "audio" in state.errors or not m.app:
        return m.app or "an app", " (which window is not known)"
    return f"an app (id {m.app})", " (which window is not known)"


#: How the Sound line labels the apps' levels: they are the Windows mixer's (``app_volume``), never an app's own
#: slider (final-B, 19 Oct: the agent read "Spotify 100%" as Spotify's own volume, at 50, and set it to 70 for
#: "down a bit"). The app names stay bare: the volume hands take an app "as the Sound line names it".
MIXER_LABEL = "mixer levels (not an app's own slider): "


def sound_line(state: DeskState) -> str:
    """The Sound line (``""`` with nothing read): ``Sound: master 50%; mixer levels (not an app's own slider): Spotify
    100% sounding; chrome 25% muted.`` The one wording of it, for the render and for a caller that says it outside
    the render's budget (``Hands._situation_text``)."""
    parts: list[str] = []
    if state.master:
        muted = " muted" if state.master.get("muted") else ""
        parts.append(f"master {float(state.master.get('level') or 0):g}%{muted}")
    apps = [f"{a.app} {a.level:g}%{' muted' if a.muted else ''}{' sounding' if a.playing else ''}" for a in state.audio]
    if apps:
        parts.append(MIXER_LABEL + "; ".join(apps))
    return ("Sound: " + "; ".join(parts) + ".") if parts else ""


def _items(state: DeskState, *, planner: bool, cap: int, said: int, short: bool, just_asked: str,
           just_did: str) -> list[tuple[int, int, str, str]]:
    """The render's pieces: ``(priority, order, group, text)``; a group joins its pieces on one line. Priority 0 and
    1 never give way (the monitors, the front, the keyboard, what plays, the holds, the asked and done lines)."""
    items: list[tuple[int, int, str, str]] = []

    def add(prio: int, group: str, text: str) -> None:
        if text:
            items.append((prio, len(items), group, text))

    multi = len(state.monitors) > 1
    if not state.windows:
        add(0, "head", "Desk now: no windows open.")
    else:
        add(0, "head", "Desk now ([n] = window id, [mN] = monitor number N):" if short else
            "Desk now (each monitor's windows top first; [n] is the window id and [mN] monitor number N, for your "
            "hands):")
    shown = [w for w in state.windows if not w.minimized]
    for m in state.monitors:
        width, height = m.rect[2] - m.rect[0], m.rect[3] - m.rect[1]
        # morning21 B3 (contract K3): its place and its own model name, the words to say it by ("second, VG271U");
        # the index is the hands' id only. "screen off" only from the display driver's own recent read
        role = m.role or ("main" if m.primary else "")
        bits = [b for b in (m.label, f"{width}x{height}") if b]
        if planner:
            bits.append(f"@{m.rect[0]},{m.rect[1]}")
            if m.work != m.rect:
                bits.append(f"work area {m.work[2] - m.work[0]}x{m.work[3] - m.work[1]}")
        group = f"m{m.index}"
        power = "; screen off" if m.power == "off" else ""
        said = f"{role} monitor" if role and multi else "monitor"
        add(0, group, f"[m{m.index}] {said} ({', '.join(bits)}{power}):")
        mine = [w for w in shown if w.monitor == m.index]
        if not mine:
            add(0, group, "nothing open")
        for w in mine:
            add(_priority(w), group, _window_words(state, w, planner=planner, cap=cap) + ";")
    off = [w for w in shown if w.monitor is None or w.monitor not in {m.index for m in state.monitors}]
    for w in off:
        add(4, "off", ("Off every monitor: " if w is off[0] else "") + name_of(w, cap=cap) + ";")
    minimized = [w for w in state.windows if w.minimized]
    if minimized:
        add(4, "min", "Minimised: " + ", ".join(name_of(w, cap=cap) + (" (playing)" if w.playing else "")
                                                 for w in minimized) + ".")
    playing = [m for m in state.media if m.playing]
    if playing:
        said_media = []
        for m in playing:
            app, where = _where(state, m)
            title = f' "{_title(m.title, min(50, cap))}"' if m.title and cap > 0 else ""
            said_media.append(f"{app}{title}{where}")
        add(1, "media", "Playing: " + "; ".join(said_media) + ".")
    paused = [m for m in state.media if not m.playing and m.status in ("paused", "stopped") and not m.hwnd]
    if paused:
        add(3, "media", "Paused: " + "; ".join(
            f'{m.app}{" " + chr(34) + _title(m.title, min(40, cap)) + chr(34) if m.title and cap > 0 else ""}'
            for m in paused) + ".")
    add(3, "sound", sound_line(state))
    if state.tray:
        try:
            from yuki.perception.windows import format_tray

            tray = format_tray(state.tray)
        except Exception:  # noqa: BLE001
            tray = ""
        add(3, "tray", f"In the tray: {tray}." if tray else "")
    if state.holds:
        add(0, "holds", f"Holding them: {state.holds}.")
    if just_asked:
        add(0, "asked", f"Just asked: {_title(just_asked, said)}")
    if just_did:
        add(0, "did", f"Just did: {_title(just_did, said)}")
    return items


def _assemble(kept: list[tuple[int, int, str, str]]) -> str:
    lines: list[str] = []
    groups: dict[str, int] = {}
    for _prio, _order, group, text in sorted(kept, key=lambda t: t[1]):
        if group in groups:
            lines[groups[group]] += " " + text
        else:
            groups[group] = len(lines)
            lines.append(text)
    return "\n".join(line[:-1] + "." if line.endswith(";") else line for line in lines)


def _fit(items: list[tuple[int, int, str, str]], max_chars: int, lowest: int = 2) -> tuple[str, bool]:
    """Drop the least important pieces (priority ``lowest`` and up, never under 2; lowest and latest first) until the
    text fits; a note says how many gave way. ``(text, fits)``."""
    kept = list(items)
    text = _assemble(kept)
    dropped = 0

    def note(n: int) -> str:
        return f"\n({n} less important fact(s) left out)" if n else ""

    for victim in sorted((t for t in items if t[0] >= max(lowest, 2)), key=lambda t: (-t[0], -t[1])):
        if len(text) + len(note(dropped)) <= max_chars:
            break
        kept.remove(victim)
        dropped += 1
        text = _assemble(kept)
    text += note(dropped)
    return text, len(text) <= max_chars


def _squeeze(text: str, max_chars: int, soft: Sequence[str]) -> str:
    """The last resort: cut lines to one width (the largest that fits, never under :data:`_LINE_MIN`) so each line
    keeps its start and none is lost: first only the ``soft`` lines (the caller's words, the holds, the note), then
    every line; a hard cut only if even that does not fit."""
    lines = text.split("\n")

    def cut(width: int, which: Callable[[str], bool]) -> list[str]:
        return [line if len(line) <= width or not which(line) else line[: width - 1].rstrip() + "…" for line in lines]

    for which in (lambda line: line.startswith(tuple(soft)), lambda line: True):
        if len("\n".join(lines)) <= max_chars:
            break
        lo, hi, best = _LINE_MIN, max(len(line) for line in lines), _LINE_MIN
        while lo <= hi:
            mid = (lo + hi) // 2
            if len("\n".join(cut(mid, which))) <= max_chars:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        lines = cut(best, which)
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


def render(state: DeskState, *, purpose: str = "talker", max_chars: int = 900, just_asked: str = "",
           just_did: str = "") -> str:
    """The desk in plain words (content: RAM only, for a model, never a log line). Per monitor, top window first,
    each ``[hwnd] App "title"`` with what holds of it; then the minimised windows, what plays, the volumes, the holds,
    and ``just_asked`` / ``just_did`` (each said up to :data:`SAID_CHARS`). ``purpose="planner"`` adds each window's
    frame rectangle and the monitors' rectangles (the brain aims by them); the talker gets none.

    To fit ``max_chars``: hidden and minimised windows give way first, then partly seen ones and the volumes; then the
    titles get shorter (:data:`_LEVELS`); last, the longest lines are cut. Never dropped: the monitors, the window in
    front, the keyboard's and the playing ones, what plays, the holds, the asked and done lines."""
    planner = purpose == "planner"
    text = ""
    for cap, said, short, lowest in _LEVELS:
        text, fits = _fit(_items(state, planner=planner, cap=cap, said=said, short=short, just_asked=just_asked,
                                 just_did=just_did), max_chars, lowest)
        if fits:
            return text
    return _squeeze(text, max_chars, ("Just asked:", "Just did:", "Holding them:", "("))


__all__ = ["MIXER_LABEL", "SAID_CHARS", "Audio", "DeskState", "Media", "Monitor", "Window", "coverer", "from_overview",
           "map_media", "mixer_rows", "monitor_of", "name_of", "render", "snapshot", "sound_line", "warm"]
