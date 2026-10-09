"""fGate (D17(4)): media transport and the system volume off keys, so "pause", "next" and "stop" still work with a game
in front, where the input backstop refuses every key.

Offline: a stub media session manager (invented app ids and titles), a stub Core Audio endpoint, a stub desk for the
hands; the real media thread is used only with a stub session manager. No real media session, speaker or key is
touched.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from yuki.actions import media
from yuki.actions.media import MediaCommand

STORE_SPOTIFY = "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"
CLASSIC_SPOTIFY = "Spotify.exe"
GAME = "valorant-win64-shipping"


# -- the stub session manager ---------------------------------------------------------------------------------------


class _Op:
    def __init__(self, result: Any, done: bool = True) -> None:
        self._result, self._done = result, done

    def wait(self, timeout: float) -> int:
        return 1 if self._done else 2

    def get_results(self) -> Any:
        return self._result


class _Session:
    def __init__(self, app_id: str, status: int, title: str = "", accepts: bool = True) -> None:
        self.source_app_user_model_id = app_id
        self.status = status            # 4 playing, 5 paused, 3 stopped
        self.title = title
        self.accepts = accepts
        self.called: list[str] = []

    def get_playback_info(self) -> Any:
        return type("Info", (), {"playback_status": self.status})()

    def try_get_media_properties_async(self) -> _Op:
        return _Op(type("Props", (), {"title": self.title, "artist": ""})())

    def __getattr__(self, name: str) -> Any:
        if name.startswith("try_") and name.endswith("_async"):
            def call() -> _Op:
                self.called.append(name)
                return _Op(self.accepts)
            return call
        raise AttributeError(name)


class _Manager:
    def __init__(self, sessions: list[_Session], current: _Session | None = None) -> None:
        self.sessions, self.current = sessions, current

    def get_sessions(self) -> list[_Session]:
        return list(self.sessions)

    def get_current_session(self) -> _Session | None:
        return self.current


def _no_game(app_id: str) -> bool:
    return False


# -- picking the session -----------------------------------------------------------------------------------------------


def test_verbs_for_keys_and_stop_maps_to_try_stop_async() -> None:
    assert media.verb_for_key("playpause") == "toggle"
    assert media.verb_for_key("playpause", "paused") == "pause"
    assert media.verb_for_key("playpause", "playing") == "play"
    assert media.verb_for_key("stop") == "stop" and media.VERBS["stop"] == "try_stop_async"     # critic L4
    assert media.verb_for_key("nexttrack") == "next" and media.verb_for_key("prevtrack") == "previous"
    assert media.verb_for_key("volumeup") is None


def test_app_keys_and_matching_both_spotify_forms() -> None:
    assert media.app_key_of(STORE_SPOTIFY) == "spotify" and media.app_key_of(CLASSIC_SPOTIFY) == "spotify"
    assert media.session_matches(STORE_SPOTIFY, "spotify")
    assert media.session_matches(CLASSIC_SPOTIFY, [STORE_SPOTIFY, CLASSIC_SPOTIFY])
    assert media.session_matches(STORE_SPOTIFY, STORE_SPOTIFY.upper())
    assert not media.session_matches("Chrome", "spotify") and not media.session_matches(STORE_SPOTIFY, None)


def test_pick_named_then_playing_then_current_and_never_a_game() -> None:
    rows = [("Chrome", "paused"), (STORE_SPOTIFY, "playing"), ("308046B0AF4A39CB", "paused")]
    assert media.pick_session(rows, "Chrome", "spotify", _no_game) == (1, 0)                    # the named app
    assert media.pick_session(rows, "Chrome", None, _no_game) == (1, 0)                         # the playing one
    paused = [("Chrome", "paused"), (STORE_SPOTIFY, "paused")]
    assert media.pick_session(paused, "Chrome", None, _no_game) == (0, 0)                       # else the current
    assert media.pick_session(paused, "", None, _no_game) == (None, 0)                          # nothing to aim at
    assert media.pick_session(rows, "Chrome", "zoomer", _no_game) == (None, 0)                  # named, absent: none
    game_rows = [("VALORANT-Win64-Shipping.exe", "playing"), ("Chrome", "paused")]
    is_game = lambda app_id: app_id.startswith("VALORANT")                                      # noqa: E731
    assert media.pick_session(game_rows, "VALORANT-Win64-Shipping.exe", None, is_game) == (None, 1)
    assert media.pick_session(game_rows, "Chrome", None, is_game) == (1, 1)
    both = [("Chrome", "playing"), (STORE_SPOTIFY, "playing")]
    assert media.pick_session(both, STORE_SPOTIFY, None, _no_game) == (1, 0)                    # current first


def test_the_real_game_list_marks_a_game_session() -> None:
    assert media._session_is_game("VALORANT-Win64-Shipping.exe") is True
    assert media._session_is_game(STORE_SPOTIFY) is False


def test_run_command_aims_at_one_session_and_returns_it() -> None:
    chrome, spotify = _Session("Chrome", 5, "A video"), _Session(STORE_SPOTIFY, 4, "Song A")
    manager = _Manager([chrome, spotify], current=chrome)
    later = time.monotonic() + 1.0
    result = media.run_command(manager, "next", None, later, _no_game)
    assert result.outcome == "sent" and result.app_id == STORE_SPOTIFY and result.app_key == "spotify"
    assert result.title == "Song A" and spotify.called == ["try_skip_next_async"] and chrome.called == []
    assert media.run_command(manager, "stop", "spotify", later, _no_game).outcome == "sent"
    assert spotify.called[-1] == "try_stop_async"
    already = media.run_command(manager, "play", "spotify", later, _no_game)
    assert already.outcome == "already" and spotify.called[-1] == "try_stop_async"              # nothing sent
    spotify.accepts = False
    assert media.run_command(manager, "pause", "spotify", later, _no_game).outcome == "refused"
    assert media.run_command(_Manager([]), "toggle", None, later, _no_game).outcome == "no_session"
    slow = _Session("Chrome", 4)
    slow.__class__ = type("Slow", (_Session,), {"try_pause_async": lambda self: _Op(True, done=False)})
    assert media.run_command(_Manager([slow], slow), "pause", None, later, _no_game).outcome == "pending"


def test_command_runs_on_the_media_thread_and_logs_content_free(monkeypatch) -> None:
    import yuki.perception.system as system

    spotify = _Session(STORE_SPOTIFY, 4, "A private song title")
    monkeypatch.setattr(system, "_media_session_manager", lambda deadline: _Manager([spotify], spotify))
    monkeypatch.setattr(media, "_session_is_game", _no_game)
    logged: list = []
    result = media.command("pause", log=lambda t, **f: logged.append((t, f)))
    assert result.outcome == "sent" and spotify.called == ["try_pause_async"]
    assert logged[0][0] == "media_command" and logged[0][1]["app"] == "spotify"
    assert "A private song title" not in repr(logged) and "title" not in logged[0][1]
    with pytest.raises(ValueError):
        media.command("louder")


def test_a_command_queued_behind_a_pending_read_does_not_trip_it_and_gives_up_after_1_s(monkeypatch) -> None:
    """Critic L4: the command never raises "an earlier read is still waiting" and never waits past its own 1 s; a
    command that only reaches the thread after its caller gave up does nothing."""
    import yuki.perception.system as system

    acted: list = []
    monkeypatch.setattr(system, "_media_session_manager", lambda deadline: acted.append("manager") or object())
    release = threading.Event()
    with system._media_lock:
        stuck = system._media_thread().submit(release.wait, 10)        # a read stuck inside WinRT
    monkeypatch.setattr(system, "_media_pending", stuck)
    with pytest.raises(TimeoutError) as stuck_read:
        system.media_sessions(timeout_s=0.05)
    assert "earlier media session read" in str(stuck_read.value)        # the read path refuses, as before
    t0 = time.monotonic()
    result = media.command("next")
    waited = time.monotonic() - t0
    assert result.outcome == "timeout" and 0.9 <= waited <= 1.6
    release.set()
    stuck.result(5)
    time.sleep(0.1)
    late = system._media_thread().submit(lambda: None)
    late.result(5)                                                      # the queued job has run by now
    assert acted == []                                                  # ...and did nothing
    assert system.media_job(lambda m, d: "ran", timeout_s=1.0) == "ran" and acted == ["manager"]


# -- the Core Audio endpoint ------------------------------------------------------------------------------------------


class _Endpoint:
    def __init__(self, level: float = 0.5, muted: bool = False) -> None:
        self.level, self.muted = level, muted
        self.ctx: list = []

    def GetMasterVolumeLevelScalar(self) -> float:
        return self.level

    def SetMasterVolumeLevelScalar(self, level: Any, ctx: Any) -> None:
        self.level = float(getattr(level, "value", level))
        self.ctx.append(ctx)

    def GetMute(self) -> bool:
        return self.muted

    def SetMute(self, mute: bool, ctx: Any) -> None:
        self.muted = bool(mute)


def test_volume_keys_become_the_endpoint() -> None:
    ep = _Endpoint(0.5, muted=True)
    after = media.endpoint_volume("volumeup", 3, _endpoint_fn=lambda: ep)
    assert after == {"level": 56.0, "muted": False}                    # 2 points a press, unmuted like the key
    assert media.endpoint_volume("volumedown", 50, _endpoint_fn=lambda: ep)["level"] == 0.0
    assert media.endpoint_volume("volumemute", 1, _endpoint_fn=lambda: ep)["muted"] is True
    assert media.endpoint_volume("volumemute", 2, _endpoint_fn=lambda: ep)["muted"] is True   # an even count: as is
    ep.level = 0.99
    assert media.endpoint_volume("volumeup", 5, _endpoint_fn=lambda: ep)["level"] == 100.0
    assert media.endpoint_volume("volumeup", 1, _endpoint_fn=lambda: (_ for _ in ()).throw(OSError())) is None
    with pytest.raises(ValueError):
        media.endpoint_volume("playpause")


# -- the talker's media_key -----------------------------------------------------------------------------------------


class _Desk:
    """A stand-in desktop with media sessions, a system volume, a game that may be in front and a media route."""

    def __init__(self, *, game: str = "", route: str = "ok") -> None:
        self.game = game
        self.route = route
        self.pressed: list = []
        self.volume = 60.0
        self.sessions = [{"app_id": STORE_SPOTIFY, "status": "playing", "title": "Song A", "current": False},
                         {"app_id": "Chrome", "status": "paused", "title": "A video", "current": True}]
        self.commands: list = []

    def activity_facts(self) -> dict[str, Any]:
        return {"media": [dict(s) for s in self.sessions], "errors": {}}

    def master_volume(self) -> dict[str, Any]:
        return {"level": round(self.volume, 1), "muted": False}

    def press(self, key: str, **kw: Any) -> Any:
        self.pressed.append(key)
        return type("R", (), {"ok": True, "summary": f"pressed {key}", "details": {}})()

    def game_front(self) -> str:
        return self.game

    def _spotify(self) -> dict[str, Any]:
        return self.sessions[0]

    def media_command(self, verb: str, app: Any = None, log: Any = None) -> MediaCommand:
        self.commands.append((verb, app))
        if self.route != "ok":
            return MediaCommand(verb, self.route)
        s = self._spotify()
        before = dict(s)
        if verb == "pause" and s["status"] == "paused":
            return MediaCommand(verb, "already", app_id=STORE_SPOTIFY, app_key="spotify", status="paused")
        if verb in ("pause", "stop"):
            s["status"] = "paused" if verb == "pause" else "stopped"
        elif verb == "next":
            s["title"] = "Song B" if self.route == "ok" else s["title"]
        if log is not None:
            log("media_command", verb=verb, app="spotify", outcome="sent")
        return MediaCommand(verb, "sent", app_id=STORE_SPOTIFY, app_key="spotify", status=before["status"],
                            title=before["title"] if verb in ("next", "previous") else "")

    def endpoint_volume(self, key: str, times: int = 1) -> dict[str, Any] | None:
        if self.route != "ok":
            return None
        self.volume += (2.0 if key == "volumeup" else -2.0) * times
        return self.master_volume()


def _run(desk: _Desk, **args: Any) -> tuple[bool, str, dict[str, Any]]:
    from yuki.voice.hands import Hands

    return Hands(desk)._run("media_key", args)


@pytest.mark.parametrize("key, want, verb", [("playpause", "paused", "pause"), ("nexttrack", "", "next"),
                                             ("stop", "", "stop")])
def test_transport_with_a_game_in_front_sends_no_key_and_still_acts(key: str, want: str, verb: str) -> None:
    desk = _Desk(game=GAME)
    args = {"key": key, **({"want": want} if want else {})}
    ok, report, facts = _run(desk, **args)
    assert ok and facts["verified"] is True and desk.pressed == [] and desk.commands[0][0] == verb
    assert facts["route"] == "media_session" and facts["media"]["app"] == "spotify"
    assert "Spotify" in report and "no key" in report


def test_previous_with_a_game_in_front_is_a_session_command_too() -> None:
    desk = _Desk(game=GAME)
    _run(desk, key="prevtrack")
    assert desk.pressed == [] and desk.commands[0][0] == "previous"


def test_already_paused_sends_nothing() -> None:
    desk = _Desk(game=GAME)
    desk.sessions[0]["status"] = "paused"
    ok, report, facts = _run(desk, key="playpause", want="paused")
    assert ok and facts.get("settled") and "already paused" in report and desk.pressed == []


def test_the_done_check_watches_the_aimed_session_not_another_apps_title() -> None:
    """Critic M1c: "next" aimed at Spotify; Spotify's track does not change but the current session's (Chrome's) title
    does: not verified (the old check on the current session would have called it done)."""
    desk = _Desk()
    original = desk.media_command

    def next_without_effect(verb: str, app: Any = None, log: Any = None) -> MediaCommand:
        result = original(verb, app, log)
        desk.sessions[0]["title"] = "Song A"                    # Spotify did not skip
        desk.sessions[1]["title"] = "Another video"             # ...but Chrome's tab changed its title
        return result

    desk.media_command = next_without_effect
    ok, report, facts = _run(desk, key="nexttrack")
    assert ok and facts["verified"] is False and "still" in report


def test_with_the_route_down_a_game_in_front_gets_no_key() -> None:
    desk = _Desk(game=GAME, route="error")
    ok, report, facts = _run(desk, key="playpause", want="paused")
    assert not ok and facts["refused"] == "game" and desk.pressed == [] and "in front" in report
    ok, report, facts = _run(desk, key="volumedown", times=2)
    assert not ok and facts["refused"] == "game" and desk.pressed == []


def test_with_the_route_down_and_no_game_the_key_stands_in() -> None:
    desk = _Desk(route="timeout")
    ok, report, facts = _run(desk, key="nexttrack")
    assert desk.pressed == ["nexttrack"]


def test_no_media_session_sends_nothing_even_without_a_game() -> None:
    desk = _Desk(route="no_session")
    ok, report, facts = _run(desk, key="stop")
    assert not ok and desk.pressed == [] and "nothing was sent" in report


def test_volume_keys_go_to_the_endpoint_with_a_game_in_front() -> None:
    desk = _Desk(game=GAME)
    ok, report, facts = _run(desk, key="volumedown", times=5)
    assert ok and facts["verified"] is True and desk.pressed == [] and facts["route"] == "core_audio"
    assert "the volume is now 50.0% (was 60.0%)" in report


def test_a_stand_in_desk_without_a_route_keeps_its_keys() -> None:
    """Rule 7: a stand-in desk never reaches the real media session; the old key path is unchanged for it."""
    from yuki.voice.offline import BrowserDesk

    desk = BrowserDesk(action_delay_s=0)
    from yuki.voice.hands import Hands

    ok, report, facts = Hands(desk)._run("media_key", {"key": "volumedown", "times": 5})
    assert ok and "the volume is now 50.0% (was 60.0%)" in report and "route" not in facts
