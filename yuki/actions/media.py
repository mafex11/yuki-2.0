"""Media transport and the system volume without a single key (fGate, D17(4)).

Play, pause, next, previous and stop go to **one** media session through Windows' own media session commands
(``GlobalSystemMediaTransportControlsSession.try_play_async`` and its siblings): no key, no click, no window and no
focus, so they work with a game in front, where the input backstop (:func:`yuki.memory.gamegate.refuse_input`) refuses
every key. The system volume keys become the default output's Core Audio endpoint (``IAudioEndpointVolume``), for the
same reason. Before, "pause" and "next" were media keys sent with ``SendInput`` into whatever window was in front.

:func:`command` picks the session to aim at, by machine facts only:

* ``app`` given (an AUMID like ``SpotifyAB.SpotifyMusic_…!Spotify``, an image name like ``Spotify.exe``, or an app key
  like ``spotify``; one or several, e.g. both forms of one app kept as data): **only** that app's session. A named app
  with no session is ``no_session``; another app is never acted on in its place;
* else the session that is playing (the current one first when several play), else the current one (the one the
  media keys would act on);
* a session whose app is a game (:func:`yuki.memory.gamegate.judge_program`) is never aimed at.

It returns which session it aimed at (:class:`MediaCommand`: ``app_id`` for the done-check, ``app_key`` for logs), so
the caller checks that very session afterwards (critic M1c), never "the current one", which another app's title change
could fake. The command runs on the existing ``yuki-media`` thread through its own submit path
(:func:`yuki.perception.system.media_job`): it never trips the media read's "an earlier read is still waiting" check
and gives up after its own 1 s budget. No new thread, no poll; logged ``media_command`` (verb, the aimed app key,
outcome; never a title).

Public API::

    command(verb, app=None, *, timeout_s=1.0, log=None) -> MediaCommand     # verb in VERBS
    verb_for_key(key, want="") -> str | None      # a media key name ("playpause", "nexttrack"...) -> a verb
    endpoint_volume(key, times=1) -> dict | None   # "volumeup" / "volumedown" / "volumemute" on the endpoint
    app_key_of(app_id) -> str;  session_matches(app_id, app) -> bool;  pick_session(...)   # pure, for tests
"""

from __future__ import annotations

import ctypes
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

#: Verb -> the media session's own command (WinRT ``GlobalSystemMediaTransportControlsSession``).
VERBS: dict[str, str] = {
    "play": "try_play_async",
    "pause": "try_pause_async",
    "toggle": "try_toggle_play_pause_async",
    "stop": "try_stop_async",
    "next": "try_skip_next_async",
    "previous": "try_skip_previous_async",
}
#: The media keys (pyautogui names, :data:`yuki.voice.hands.MEDIA_KEYS`) that are transport, and their verb.
KEY_VERBS: dict[str, str] = {"playpause": "toggle", "stop": "stop", "nexttrack": "next", "prevtrack": "previous"}
#: A verb whose wanted state is already the session's status needs nothing (and a toggle would undo it).
_ALREADY: dict[str, tuple[str, ...]] = {"play": ("playing",), "pause": ("paused",), "stop": ("stopped", "closed")}
#: Next and previous are checked by the track changing, so their command also reads the title before (RAM only).
_TITLE_VERBS = ("next", "previous")
#: The title read before next/previous takes at most this much of the command's budget.
_TITLE_READ_S = 0.2
#: What one press of Windows' own volume keys moves the system volume by (points of 100).
VOLUME_STEP = 2.0

_ASYNC_COMPLETED = 1


@dataclass(frozen=True)
class MediaCommand:
    """What :func:`command` did. ``outcome``: ``sent`` (the session took it), ``pending`` (sent, no answer within the
    budget: it may still land, so nothing else may be sent for it), ``already`` (the session was already in that
    state: nothing sent), ``refused`` (the session said no), ``no_session`` (nothing to aim at), ``timeout`` (never
    reached the session), ``error`` (WinRT failed), ``unavailable`` (no media session support here)."""

    verb: str
    outcome: str
    app_id: str = ""        # the session aimed at (RAM only; logs carry app_key)
    app_key: str = ""
    status: str = ""        # its status before the command
    title: str = ""         # its title before (next/previous only; RAM only, never logged)
    sessions: int = 0
    skipped_games: int = 0
    error: str = ""         # the exception's type only

    @property
    def sent(self) -> bool:
        """The command reached the aimed session (or may still: ``pending``)."""
        return self.outcome in ("sent", "pending")

    @property
    def reached(self) -> bool:
        """The media session route answered for this request (sent, pending, already, refused or no session): a key
        must not be sent instead. False only when the route itself failed (timeout, error, unavailable)."""
        return self.outcome not in ("timeout", "error", "unavailable")

    def facts(self) -> dict[str, Any]:
        """Content-free: the verb, the aimed app's key, the outcome, counts (never a title)."""
        out: dict[str, Any] = {"verb": self.verb, "app": self.app_key, "outcome": self.outcome,
                               "sessions": self.sessions}
        if self.skipped_games:
            out["skipped_games"] = self.skipped_games
        if self.error:
            out["error"] = self.error
        return out


def verb_for_key(key: str, want: str = "") -> str | None:
    """The verb for a media key: ``playpause`` with ``want`` "playing"/"paused" is play/pause (never a toggle when the
    wanted state is known), else toggle; ``stop`` -> stop (critic L4); next/previous track. None: not a transport
    key."""
    key = str(key or "").strip().lower()
    if key == "playpause":
        want = str(want or "").strip().lower()
        return {"playing": "play", "paused": "pause"}.get(want, "toggle")
    return KEY_VERBS.get(key)


def app_key_of(app_id: str) -> str:
    """The app key of a session's AppUserModelID, as memory keys apps (casefolded, no ``.exe``): the application part
    of a packaged id (``SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify`` -> ``spotify``), else the id itself
    (``Spotify.exe`` -> ``spotify``). A fixed machine format, no guessing."""
    part = str(app_id or "").strip().rsplit("!", 1)[-1].strip().casefold()
    return part[:-4].rstrip() if part.endswith(".exe") else part


def session_matches(app_id: str, app: str | Iterable[str] | None) -> bool:
    """Whether a session belongs to the named app: its exact id (any case), or the same app key
    (:func:`app_key_of` on both sides). ``app`` may list several forms (a Store id and a classic image name)."""
    if app is None:
        return False
    wants = [app] if isinstance(app, str) else list(app)
    sid = str(app_id or "").strip().casefold()
    key = app_key_of(app_id)
    for want in wants:
        w = str(want or "").strip()
        if not w:
            continue
        if w.casefold() == sid or (key and app_key_of(w) == key):
            return True
    return False


def _session_is_game(app_id: str) -> bool:
    """A session whose app is a game by the game gate's machine facts (never the live Direct3D state: a session is no
    window). Its image stem is the id's application part."""
    from yuki.memory.gamegate import judge_program

    image = str(app_id or "").strip().rsplit("!", 1)[-1]
    try:
        return bool(judge_program(image, d3d_fn=lambda: False)[0])
    except Exception:  # noqa: BLE001 - unknown: not a game
        return False


def pick_session(sessions: Sequence[tuple[str, str]], current_id: str, app: str | Iterable[str] | None = None,
                 is_game: Callable[[str], bool] = _session_is_game) -> tuple[int | None, int]:
    """Which session to aim at: ``(index into sessions or None, sessions skipped as games)``.

    ``sessions``: ``(app_id, status)`` each; ``current_id``: the current session's id ("" none). With ``app``: only
    that app's session (the playing one first if it has several). Else the playing one (the current one first when
    several play), else the current one. Game sessions are never picked."""
    skipped = 0
    allowed: list[int] = []
    for i, (app_id, _status) in enumerate(sessions):
        if is_game(app_id):
            skipped += 1
            continue
        allowed.append(i)
    current = str(current_id or "")
    if app is not None:
        mine = [i for i in allowed if session_matches(sessions[i][0], app)]
        playing = [i for i in mine if sessions[i][1] == "playing"]
        return (playing or mine or [None])[0], skipped
    playing = [i for i in allowed if sessions[i][1] == "playing"]
    if playing:
        first = next((i for i in playing if current and sessions[i][0] == current), playing[0])
        return first, skipped
    cur = next((i for i in allowed if current and sessions[i][0] == current), None)
    return cur, skipped


def _status_of(session: Any) -> str:
    from yuki.perception.system import _PLAYBACK_STATUS

    try:
        status = int(session.get_playback_info().playback_status)
    except Exception:  # noqa: BLE001
        return "unknown"
    return _PLAYBACK_STATUS.get(status, str(status))


def _title_of(session: Any, budget_s: float) -> str:
    try:
        operation = session.try_get_media_properties_async()
        if operation.wait(max(0.0, budget_s)) != _ASYNC_COMPLETED:
            return ""
        return str(operation.get_results().title or "")
    except Exception:  # noqa: BLE001 - the done-check then reads the status only
        return ""


def run_command(manager: Any, verb: str, app: str | Iterable[str] | None, deadline: float,
                is_game: Callable[[str], bool] | None = None) -> MediaCommand:
    """The command on a session manager (runs on the media thread; tests pass a stub manager)."""
    is_game = is_game or _session_is_game
    current = manager.get_current_session()
    current_id = str(current.source_app_user_model_id or "") if current else ""
    raw = list(manager.get_sessions())
    rows = [(str(s.source_app_user_model_id or ""), _status_of(s)) for s in raw]
    index, skipped = pick_session(rows, current_id, app, is_game)
    if index is None:
        return MediaCommand(verb, "no_session", sessions=len(rows), skipped_games=skipped)
    session = raw[index]
    app_id, status = rows[index]
    base = dict(verb=verb, app_id=app_id, app_key=app_key_of(app_id), status=status, sessions=len(rows),
                skipped_games=skipped)
    if status in _ALREADY.get(verb, ()):
        return MediaCommand(outcome="already", **base)
    title = _title_of(session, min(_TITLE_READ_S, deadline - time.monotonic())) if verb in _TITLE_VERBS else ""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return MediaCommand(outcome="timeout", title=title, **base)
    operation = getattr(session, VERBS[verb])()
    if operation.wait(remaining) != _ASYNC_COMPLETED:
        return MediaCommand(outcome="pending", title=title, **base)
    return MediaCommand(outcome="sent" if bool(operation.get_results()) else "refused", title=title, **base)


def command(verb: str, app: str | Iterable[str] | None = None, *, timeout_s: float = 1.0,
            log: Callable[..., None] | None = None) -> MediaCommand:
    """Play / pause / toggle / stop / next / previous on one media session (see the module docstring), on the
    ``yuki-media`` thread within ``timeout_s``. Never raises; never sends a key."""
    verb = str(verb or "").strip().lower()
    if verb not in VERBS:
        raise ValueError(f"unknown media verb {verb!r} ({', '.join(VERBS)})")
    t0 = time.perf_counter()
    try:
        from yuki.perception.system import media_job

        result = media_job(lambda manager, deadline: run_command(manager, verb, app, deadline), timeout_s=timeout_s)
    except TimeoutError:
        result = MediaCommand(verb, "timeout")
    except ImportError as exc:
        result = MediaCommand(verb, "unavailable", error=type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - a WinRT failure is an outcome, never a crash
        result = MediaCommand(verb, "error", error=type(exc).__name__)
    if log is not None:
        try:
            log("media_command", **result.facts(), ms=round((time.perf_counter() - t0) * 1000.0, 1))
        except Exception:  # noqa: BLE001
            pass
    return result


# ---------------------------------------------------------------------------
# The system volume through Core Audio (no key)
# ---------------------------------------------------------------------------


def _audio_context() -> Any:
    """Yuki's own Core Audio event context, when one is defined (``yuki.core.inputtag.YUKI_AUDIO_CONTEXT``: a volume
    watcher tells Yuki's changes from the user's by it); None (NULL) otherwise."""
    try:
        from comtypes import GUID

        from yuki.core import inputtag

        value = getattr(inputtag, "YUKI_AUDIO_CONTEXT", None)
        if value is None:
            return None
        guid = value if isinstance(value, GUID) else GUID(str(value))
        return ctypes.pointer(guid)
    except Exception:  # noqa: BLE001
        return None


def _endpoint() -> Any:
    """The default output device's ``IAudioEndpointVolume`` (the declarations in :mod:`yuki.perception.audio`)."""
    import comtypes

    from yuki.perception import audio

    device = audio._enumerator().GetDefaultAudioEndpoint(audio.E_RENDER, audio.E_CONSOLE)
    iid = audio.IAudioEndpointVolume._iid_
    raw = device.Activate(ctypes.byref(iid), comtypes.CLSCTX_ALL, None)
    return ctypes.cast(ctypes.c_void_p(raw), ctypes.POINTER(audio.IAudioEndpointVolume))


def endpoint_volume(key: str, times: int = 1, *,
                    _endpoint_fn: Callable[[], Any] | None = None) -> dict[str, Any] | None:
    """What Windows' volume keys do, on the default output's endpoint instead of a key: ``volumeup`` / ``volumedown``
    move the master level :data:`VOLUME_STEP` points per press (clamped to 0-100) and unmute, as the keys do;
    ``volumemute`` toggles mute once per press. Returns ``{"level": 0-100, "muted": bool}`` read back after, or None
    when Core Audio could not (the caller may then fall back to the key, never with a game in front)."""
    key = str(key or "").strip().lower()
    if key not in ("volumeup", "volumedown", "volumemute"):
        raise ValueError(f"not a volume key: {key!r}")
    times = max(1, int(times or 1))
    try:
        volume = (_endpoint_fn or _endpoint)()
        ctx = _audio_context()
        if key == "volumemute":
            if times % 2:
                volume.SetMute(not bool(volume.GetMute()), ctx)
        else:
            level = float(volume.GetMasterVolumeLevelScalar())
            step = VOLUME_STEP / 100.0 * times * (1.0 if key == "volumeup" else -1.0)
            volume.SetMasterVolumeLevelScalar(ctypes.c_float(min(1.0, max(0.0, level + step))), ctx)
            if bool(volume.GetMute()):
                volume.SetMute(False, ctx)
        return {"level": round(float(volume.GetMasterVolumeLevelScalar()) * 100.0, 1), "muted": bool(volume.GetMute())}
    except Exception:  # noqa: BLE001 - None: the caller reports it (or uses the key with no game in front)
        return None


__all__ = ["VERBS", "KEY_VERBS", "VOLUME_STEP", "MediaCommand", "verb_for_key", "app_key_of", "session_matches",
           "pick_session", "run_command", "command", "endpoint_volume"]
