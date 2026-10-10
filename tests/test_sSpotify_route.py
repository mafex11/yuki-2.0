"""sSpotify: Spotify and audio skills through Spotify's desktop window (PLAN.md D4, D17, D19; B.2 offline bar "Spotify
route on the invented-name fixtures", H1, M1a/b, M2, M5, M6, H2, L13).

Offline: every run goes through the real engine (``yuki.skills.engine``), the real connection files
(``yuki/skills/connections/{spotify,audio}.toml``) and the real driver (``yuki.skills.spotify_route``), with the live
desktop replaced by a stand-in (:class:`FakeDesk`): Spotify's window is a synthetic UI Automation tree shaped like
``docs/research/audit-2026-10-09/round4/spotify-desktop-route.md`` §2 with invented names, read through the real reader
code (``yuki.connect.spotify_desktop.UiaTree``) and the shared fake automation. No window, key, click, sound, network or
real database: temp databases only.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from tests._fake_uia import Automation, Node
from yuki.connect import spotify_desktop as SD
from yuki.memory.api import MemoryClient
from yuki.memory.store import Store
from yuki.skills import connection as C
from yuki.skills import engine as E
from yuki.skills import registry as R
from yuki.skills import spotify_route as S
from yuki.skills.resolve import HELD_LINE, Live, resolve

HWND = 7301
PREV = 999
PID = 4242
STORE_ID = "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"
GAME = "skyrunner-win64-shipping"
SHIPPED = C.CONNECTIONS_DIR
LINK = "https://xpui.app.spotify.com/playlist/{id}"
IDS = {"Paper Comets": "Aa1Bb2Cc3Dd4Ee5Ff6Gg7H", "Moss Radio": "Hh8Ii9Jj0Kk1Ll2Mm3Nn4O"}
NAMES = ("Velvet Harbour", "Copper Kites", "Lantern Rain", "Paper Comets", "Moss Radio", "Tamsin Reyes",
         "Odile Fenn", "Not In Shelf")


class FakeAutomation(Automation):
    """The shared fake plus what the driver's reads use: the true condition and element identity."""

    def CreateTrueCondition(self) -> tuple:
        return ("true",)

    def CompareElements(self, a: Any, b: Any) -> bool:
        return getattr(a, "node", None) is getattr(b, "node", None)


# ---------------------------------------------------------------------------------------------------------------------
# Spotify's window (invented names)
# ---------------------------------------------------------------------------------------------------------------------

DEFAULT_ROWS = [("Velvet Harbour", "Tamsin Reyes", "play"), ("Copper Kites", "Odile Fenn", "play"),
                ("Lantern Rain", "Spotify", "play")]


class Window:
    """A Spotify window tree with handles on the nodes the tests look at."""

    def __init__(self, rows: list[tuple[str, str, str]] | None = None, *, volume: float = 0.42, library: bool = True,
                 home_cards: tuple[str, ...] = ("Copper Kites", "Not In Shelf"), page: tuple[str, str] | None = None,
                 play_word: str = "Play", pause_word: str = "Pause", volume_label: str = "Change volume",
                 search_box: bool = False, extra_slider: bool = False, frame_only: bool = False) -> None:
        self.rows = list(DEFAULT_ROWS if rows is None else rows)
        self.play_word, self.pause_word = play_word, pause_word
        self.row_buttons: dict[str, list[Node]] = {}
        self.card_buttons: dict[str, Node] = {}
        self.page_button: Node | None = None
        self.bare_play: list[Node] = []
        self.search_box: Node | None = None
        if frame_only:
            self.root = Node("Window", "Spotify Premium", (0, 0, 1600, 900)).add(
                Node("TitleBar", ""), Node("Button", "Minimise"), Node("Button", "Close"), Node("Pane", ""))
            self.slider = None
            return
        self.root = Node("Window", "Spotify Premium", (0, 0, 1600, 900))
        doc = Node("Document", "Invented Song • Invented Artist", value="https://xpui.app.spotify.com/index.html")
        shell = Node("Group", "")
        shell.add(Node("Button", "Home", invoke=True), Node("ComboBox", "What do you want to play?", value=""))
        self.grid = Node("DataGrid", "Your Library", value="")
        if library:
            panel = Node("Group", "Invented library panel")
            panel.add(Node("Button", "Collapse Your Library", invoke=True),
                      Node("Button", "Search in Your Library", invoke=True))
            if search_box:
                self.search_box = Node("Edit", "Search in Your Library", value="", readonly=False)
                panel.add(self.search_box)
            for name, owner, state in self.rows:
                self.grid.add(self._row(name, owner, state))
            panel.add(self.grid)
            shell.add(panel)
        main = Node("Group", "")
        shortcuts = Node("Group", "")
        for i in range(8):     # Home's shortcut grid: 8 Buttons named exactly "Play" (one "Pause")
            b = Node("Button", "Pause" if i == 3 else "Play", invoke=True)
            self.bare_play.append(b)
            shortcuts.add(b)
        main.add(shortcuts)
        cards = Node("DataGrid", "Jump back in", value="")
        for name in home_cards:
            b = Node("Button", f"{play_word} {name}", invoke=True)
            self.card_buttons[name] = b
            cards.add(Node("DataItem", "").add(Node("ListItem", name).add(
                b, Node("Hyperlink", name, value=LINK.replace("{id}", IDS.get(name, "Zz9Yy8Xx7Ww6Vv5Uu4Tt3S"))))))
        main.add(cards)
        if page is not None:
            name, state = page
            self.page_button = Node("Button", f"{play_word if state == 'play' else pause_word} {name}", invoke=True)
            tracks = Node("DataGrid", f"{name}", value="")
            tracks.add(Node("DataItem", "").add(Node("Button", "Play Invented Track by Invented Artist", invoke=True)))
            main.add(Node("Group", name).add(self.page_button, Node("Hyperlink", name,
                                                                     value=LINK.replace("{id}", IDS.get(name, "x"))),
                                             tracks))
        shell.add(main)
        bar = Node("Group", "Now playing bar")
        controls = Node("Group", "Player controls").add(Node("Button", "Previous", invoke=True),
                                                        Node("Button", "Pause", invoke=True),
                                                        Node("Button", "Next", invoke=True))
        self.slider = Node("Slider", volume_label, range=(volume, 0.0, 1.0), offscreen=True)
        bar.add(controls, Node("Button", "Mute", invoke=True),
                Node("Text", volume_label).add(self.slider))
        if extra_slider:
            bar.add(Node("Slider", "Invented equaliser", range=(0.5, 0.0, 1.0)))
        shell.add(bar)
        doc.add(shell)
        self.root.add(Node("Pane", "").add(doc))

    def _row(self, name: str, owner: str, state: str) -> Node:
        word = self.play_word if state == "play" else self.pause_word
        open_b = Node("Button", f"{name} Playlist • {owner}", invoke=True)
        play_b = Node("Button", f"{word} {name}", invoke=True)
        self.row_buttons.setdefault(name, []).append(play_b)
        return Node("DataItem", "", invoke=True, selected=False).add(
            Node("DataItem", "", selected=False).add(Node("Group", name).add(open_b, play_b)))

    def flip(self, button: Node, name: str) -> None:
        """A play button turns to the pause wording (Spotify started that playlist)."""
        button.name = f"{self.pause_word} {name}"


class FakeSlider:
    """Spotify's slider as ``slider.drive`` moves it (RangeValue on the window's own node). ``ignore``: the set is
    ignored (a backgrounded window, or a value put back); ``focus`` must never be called from a no-focus rung."""

    def __init__(self, node: Node, desk: "FakeDesk") -> None:
        self.node, self.desk = node, desk

    def read(self) -> tuple[float, float, float, bool, float]:
        return float(self.node.range[0]), 0.0, 1.0, False, 0.0

    def set(self, value: float) -> None:
        self.desk.sets.append(round(float(value), 4))
        if not self.desk.ignore_sets():
            self.node.range = (float(value), 0.0, 1.0)

    def focus(self) -> None:
        self.desk.slider_focus += 1
        raise AssertionError("SetFocus reached from a slider set")


class FakeDesk:
    """The live desktop as ``spotify_route.Desk`` exposes it, scripted. Records every call."""

    def __init__(self, window: Window | None, *, state: str = "front", status: str = "paused",
                 audio: Any = True, app_id: str = STORE_ID) -> None:
        self.w = window
        self.state_ = state
        self.status = status
        self.app_id = app_id
        self.audio = audio
        self.warm_built = True
        self.calls: list[tuple] = []
        self.pressed: list[tuple[str, Node, Any]] = []
        self.sets: list[float] = []
        self.slider_focus = 0
        self.focused: list[int] = []
        self.hotkeys: list[tuple] = []
        self.opened: list[str] = []
        self.launched: list[list[str]] = []
        self.media_calls: list[tuple[str, str]] = []
        self.volumes: list[tuple] = []
        self.masters: list[tuple] = []
        self.mixer: dict[str, float] = {"chrome": 50.0, "spotify": 60.0, "Spotify.exe": 60.0, "pythonw.exe": 80.0}
        self.master_now = 30.0
        self.on_press: Callable[[str, Node, Any], None] | None = None
        self.on_open: Callable[[str], None] | None = None
        self.on_media: Callable[[str], None] | None = None
        self.ignore_when: Callable[[], bool] = lambda: False
        self.launch_timeouts: list[float] = []

    # reads
    def window(self) -> Any:
        if self.w is None:
            return None
        return SD.WindowRef(hwnd=HWND, pid=PID, minimised=self.state_ == "minimised")

    def state(self, hwnd: int) -> str | None:
        return self.state_ if self.w is not None else None

    def warm(self, hwnd: int, budget_s: float) -> Any:
        self.calls.append(("warm", round(budget_s, 3)))
        return SD.WarmResult(before=7, after=300 if self.warm_built else 7, built=self.warm_built,
                             cold=not self.warm_built, ms=12.0)

    def uia(self, job: Callable[[Any, dict], None], budget_s: float) -> tuple[dict, bool]:
        self.calls.append(("uia", budget_s))
        shared: dict = {}
        job(FakeAutomation({HWND: self.w.root}), shared)
        return shared, False

    def spotify_status(self, pid: int | None) -> tuple[str, str]:
        return self.status, (self.app_id if self.status != "none" else "")

    def render_active(self) -> bool | None:
        self.calls.append(("render",))
        return self.audio() if callable(self.audio) else self.audio

    def foreground(self) -> int:
        return PREV

    def running(self) -> bool:
        return self.w is not None

    def mixer_level(self, app: str) -> float | None:
        return self.mixer.get(app)

    def master_level(self) -> float | None:
        return self.master_now

    def own_process(self) -> str:
        return "pythonw.exe"

    # patterns
    def press(self, ref: Any, pattern: str, value: Any = None) -> bool:
        node = ref.node
        self.pressed.append((pattern, node, value))
        if pattern == "value":
            node.value = str(value)
        if self.on_press is not None:
            self.on_press(pattern, node, value)
        return True

    def live_slider(self, ref: Any, automation: Any) -> FakeSlider:
        return FakeSlider(ref.node, self)

    def ignore_sets(self) -> bool:
        return self.ignore_when()

    # Core Audio
    def app_volume(self, app: str, *, level: float | None, change: float | None, mute: bool | None) -> Any:
        self.volumes.append((app, level, change, mute))
        if app not in self.mixer:
            return E.RouteResult()   # no details: no session
        before = self.mixer[app]
        new = before if level is None and change is None else max(0.0, min(100.0, level if level is not None
                                                                           else before + change))
        if (level is not None or change is not None) and mute is None and abs(new - before) <= 0.6:
            # set_app_volume's N1: a level that can't move is reported as a no-op, nothing set
            return _Result(True, {"verified": True, "noop": True, "before": before, "after": before})
        self.mixer[app] = new
        return _Result(True, {"verified": True, "before": before, "after": new})

    def master(self, *, level: float | None, change: float | None, mute: bool | None) -> dict | None:
        self.masters.append((level, change, mute))
        before = self.master_now
        if level is not None or change is not None:
            self.master_now = max(0.0, min(100.0, level if level is not None else before + change))
        return {"before": before, "level": self.master_now, "muted": bool(mute), "muted_before": False}

    def media(self, verb: str, app_id: str, log: Any = None) -> Any:
        self.media_calls.append((verb, app_id))
        if log is not None:
            log("media_command", verb=verb, app_key="spotify", outcome="sent")
        if self.on_media is not None:
            self.on_media(verb)
        return _Media("sent")

    # focus-taking: each consults the D17 backstop first, as the real guarded actions do
    @staticmethod
    def _refused(action: str, **kw: Any) -> Any:
        from yuki.memory import gamegate

        why = gamegate.refuse_input(action, **kw)
        return _Result(False, {"refused": "game"}) if why is not None else None

    def focus(self, hwnd: int) -> Any:
        if (no := self._refused("focus", hwnd=hwnd)) is not None:
            return no
        self.focused.append(int(hwnd))
        if int(hwnd) == HWND:
            self.state_ = "front"
        elif self.state_ == "front":
            self.state_ = "covered"
        return _Result(True, {})

    def hotkey(self, keys: list[str], hwnd: int) -> Any:
        if (no := self._refused("keys", hwnd=hwnd)) is not None:
            return no
        self.hotkeys.append(tuple(keys))
        if self.w is not None and self.w.slider is not None:
            v = float(self.w.slider.range[0]) + (0.06 if keys[-1] == "up" else -0.06)
            self.w.slider.range = (max(0.0, min(1.0, v)), 0.0, 1.0)
        return _Result(True, {})

    def open_uri(self, uri: str) -> Any:
        if (no := self._refused("open_url")) is not None:
            return no
        self.opened.append(uri)
        if self.on_open is not None:
            self.on_open(uri)
        return _Result(True, {"via": "startfile"})

    def launch(self, args: list[str], timeout_s: float = 8.0) -> Any:
        if (no := self._refused("launch")) is not None:
            return no
        self.launched.append(list(args))
        self.launch_timeouts.append(float(timeout_s))
        return _Result(True, {})


class _Result:
    def __init__(self, ok: bool, details: dict) -> None:
        self.ok, self.details = ok, details


class _Media:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome


# ---------------------------------------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing reaches the desktop: no live game read, no live slider waits, a fresh warm-up memory per test."""
    from yuki.memory import gamegate

    monkeypatch.setattr(gamegate, "game_in_front", lambda: "")
    monkeypatch.setattr(S, "SLIDER_SETTLE_S", 0.02)
    monkeypatch.setattr(S, "SLIDER_HOLD_S", 0.0)
    S.forget_warm()
    C.clear_cache()


@pytest.fixture()
def loaded() -> C.Loaded:
    got = C.load([SHIPPED])
    assert got.errors == ()
    return got


@pytest.fixture()
def client(tmp_path: Path, loaded: C.Loaded):
    c = MemoryClient(Store.open(tmp_path / "memory.db"))
    R.seed(c.store, loaded)
    yield c
    c.close()


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += float(s)


def run(client: Any, loaded: C.Loaded, desk: FakeDesk, key: str, slots: dict, words: list[str], *,
        game: str = "", live: Live | None = None, logs: list | None = None,
        monkeypatch: pytest.MonkeyPatch | None = None) -> E.SkillResult:
    assert monkeypatch is not None
    monkeypatch.setattr(S, "DESK", desk)
    clock = Clock()
    plan = resolve(key, slots, words, store=client.store, loaded=loaded, live=live or Live(game=game))
    log = logs if logs is not None else []
    ctx = E.RunContext(origin="voice", words=words, memory=client, game=lambda: game, recheck="inline",
                       log=lambda event, **f: log.append((event, f)), clock=clock.now, sleep=clock.sleep)
    res = E.run(plan, ctx)
    res.clock = clock     # type: ignore[attr-defined]
    return res


def rows_of(client: Any, key: str) -> list[dict]:
    return [dict(r) for r in client.store._query("SELECT * FROM skill_runs WHERE skill = ? ORDER BY id", (key,))]


def playing_after_press(desk: FakeDesk, w: Window, *, audio: bool = True) -> None:
    """A press of a "Play {name}" button starts that playlist: its button flips, the session plays, Spotify sounds
    here (``audio``)."""
    def hook(pattern: str, node: Node, value: Any) -> None:
        if pattern == "invoke" and node.name.startswith(f"{w.play_word} "):
            node.name = f"{w.pause_word} {node.name[len(w.play_word) + 1:]}"
            desk.status = "playing"
            desk.audio = audio

    desk.on_press = hook


# ---------------------------------------------------------------------------------------------------------------------
# spotify.playlist
# ---------------------------------------------------------------------------------------------------------------------


def test_the_right_rows_play_button_is_invoked_inside_the_library_grid_only(client, loaded, monkeypatch) -> None:
    w = Window()                                    # "Copper Kites" is a Library row *and* a Home card
    desk = FakeDesk(w)
    playing_after_press(desk, w)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              monkeypatch=monkeypatch)
    assert res.outcome == "done" and res.done and res.route == "A:library:driver"
    assert res.done_line == "Playing Copper Kites on Spotify."
    assert [(p, n) for p, n, _v in desk.pressed] == [("invoke", w.row_buttons["Copper Kites"][0])]
    assert all(n is not w.card_buttons["Copper Kites"] and n not in w.bare_play for _p, n, _v in desk.pressed)
    assert res.recheck_verdict is True
    row = rows_of(client, "spotify.playlist")[0]
    assert (row["outcome"], row["checked"], row["recheck"], row["app"]) == ("done", 1, 1, "spotify")


def test_names_are_identities_case_and_spacing_folded_never_meanings(client, loaded, monkeypatch) -> None:
    w = Window()
    desk = FakeDesk(w)
    playing_after_press(desk, w)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "copper  kites"}, ["play copper  kites"],
              monkeypatch=monkeypatch)
    assert res.done and res.done_line == "Playing Copper Kites on Spotify."      # Spotify's own spelling said
    desk2 = FakeDesk(Window())
    res2 = run(client, loaded, desk2, "spotify.playlist", {"playlist": "Copper"}, ["play Copper"],
               monkeypatch=monkeypatch)
    assert not res2.done and desk2.pressed == []                                   # a part of a name is no name


def test_home_play_buttons_and_an_outside_pause_are_never_pressed(client, loaded, monkeypatch) -> None:
    w = Window(home_cards=("Not In Shelf",))       # a Home card only, no Library row, no learned link
    desk = FakeDesk(w)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Not In Shelf"}, ["play Not In Shelf"],
              monkeypatch=monkeypatch)
    assert desk.pressed == [] and desk.opened == [] and desk.launched == [] and desk.focused == []
    assert res.outcome == "handed_over" and not res.done                # the thinker takes over (no game in front)
    assert [t["outcome"] for t in res.tried][:2] == ["not_in_view", "not_minimised"]


def test_two_rows_with_the_same_name_ask_and_never_guess(client, loaded, monkeypatch) -> None:
    w = Window(rows=[("Lantern Rain", "Tamsin Reyes", "play"), ("Lantern Rain", "Odile Fenn", "play")])
    desk = FakeDesk(w)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Lantern Rain"}, ["play Lantern Rain"],
              monkeypatch=monkeypatch)
    assert res.outcome == "asked" and desk.pressed == []
    assert res.options == ["Lantern Rain by Tamsin Reyes", "Lantern Rain by Odile Fenn"]


def _learn_link(client: Any, name: str) -> str:
    """What the memory side keeps for a link Spotify showed (encrypted, ``nudge_state['spotify_links']``)."""
    store = client.store
    uri = f"spotify:playlist:{IDS[name]}"
    key = store.cipher.digest("spotify_playlist", name)
    store.set_nudge_state(SD.LINKS_KEY, store.cipher.encrypt(json.dumps({key: {"name": name, "uri": uri, "n": 1}})))
    return uri


def test_an_out_of_view_name_with_a_learned_link_takes_the_link_route(client, loaded, monkeypatch) -> None:
    uri = _learn_link(client, "Paper Comets")
    w = Window(home_cards=())
    desk = FakeDesk(w)
    page = Window(home_cards=("Copper Kites",), page=("Paper Comets", "play"))

    def opened(u: str) -> None:
        desk.w = page                                 # Spotify comes forward on the playlist's page

    desk.on_open = opened
    playing_after_press(desk, page)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "paper comets"}, ["play paper comets"],
              monkeypatch=monkeypatch)
    assert res.outcome == "done" and res.route == "A:link:driver"
    assert desk.opened == [uri]
    assert [n for _p, n, _v in desk.pressed] == [page.page_button]        # the page's own button, outside every grid
    assert res.done_line == "Playing Paper Comets on Spotify."
    assert res.recheck_verdict is True


def test_the_link_route_with_a_game_in_front_is_held(client, loaded, monkeypatch) -> None:
    _learn_link(client, "Paper Comets")
    desk = FakeDesk(Window(home_cards=()), state="covered")
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Paper Comets"}, ["play Paper Comets"],
              game=GAME, monkeypatch=monkeypatch)
    assert res.outcome == "held_game" and res.say == HELD_LINE and res.held == "game"
    assert desk.opened == [] and desk.pressed == [] and desk.focused == [] and desk.launched == []
    assert res.handoff is None


def test_already_playing_only_when_the_row_the_session_and_the_sound_agree(client, loaded, monkeypatch) -> None:
    w = Window(rows=[("Velvet Harbour", "Tamsin Reyes", "pause")])
    desk = FakeDesk(w, status="playing", audio=True)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Velvet Harbour"}, ["play Velvet Harbour"],
              monkeypatch=monkeypatch)
    assert res.outcome == "already" and res.verified is True and res.done
    assert res.done_line == "Velvet Harbour is already playing on Spotify."
    assert desk.pressed == [] and desk.media_calls == []


def test_a_stale_pause_row_with_the_session_paused_gets_the_sessions_play_never_invoke(client, loaded,
                                                                                       monkeypatch) -> None:
    w = Window(rows=[("Velvet Harbour", "Tamsin Reyes", "pause")])
    desk = FakeDesk(w, status="paused", audio=False)

    def resumed(verb: str) -> None:
        desk.status, desk.audio = "playing", True

    desk.on_media = resumed
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Velvet Harbour"}, ["play Velvet Harbour"],
              monkeypatch=monkeypatch)
    assert desk.media_calls == [("play", STORE_ID)]                    # play on Spotify's own session, never toggle
    assert desk.pressed == []
    assert res.outcome == "done" and res.done


def test_resuming_the_paused_current_playlist_passes_the_recheck_no_new_track_needed(client, loaded,
                                                                                     monkeypatch) -> None:
    w = Window(rows=[("Moss Radio", "Odile Fenn", "pause")])
    desk = FakeDesk(w, status="paused", audio=False)
    desk.on_media = lambda verb: (setattr(desk, "status", "playing"), setattr(desk, "audio", True))
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Moss Radio"}, ["play Moss Radio"],
              monkeypatch=monkeypatch)
    assert res.done and res.recheck_verdict is True
    row = rows_of(client, "spotify.playlist")[0]
    assert row["recheck"] == 1                                            # no false done counted (M1b)


def test_playing_on_another_device_is_said_before_any_invoke_or_slider_set(client, loaded, monkeypatch) -> None:
    w = Window()
    desk = FakeDesk(w, status="playing", audio=False)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              monkeypatch=monkeypatch)
    assert res.outcome == "other_device" and not res.done and res.say == S.LINE_OTHER
    assert desk.pressed == [] and desk.media_calls == []
    assert res.clock.t - 1000.0 >= S.THIS_PC_S - S.POLL_S                 # no sound here for the whole 2 s
    # volume too: never a slider set while Spotify plays elsewhere
    desk2 = FakeDesk(Window(), status="playing", audio=False)
    res2 = run(client, loaded, desk2, "volume", {"target": "Spotify", "change": "down"}, ["turn Spotify down"],
               monkeypatch=monkeypatch)
    assert res2.outcome == "other_device" and res2.say == S.LINE_OTHER_VOLUME and desk2.sets == []
    assert desk2.volumes == []
    # "playing elsewhere" never counts against a rung
    trust = R.trust(client.store, "spotify.playlist")
    assert all(t.failed == 0 and t.streak == 0 for t in trust.values())


def test_sound_found_within_the_two_seconds_is_this_pc(client, loaded, monkeypatch) -> None:
    reads = iter([False, False, True])
    w = Window(rows=[("Velvet Harbour", "Tamsin Reyes", "pause")])
    desk = FakeDesk(w, status="playing", audio=lambda: next(reads, True))
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Velvet Harbour"}, ["play Velvet Harbour"],
              monkeypatch=monkeypatch)
    assert res.outcome == "already"


def test_a_row_that_flips_with_no_sound_on_this_pc_is_never_done(client, loaded, monkeypatch) -> None:
    w = Window()
    desk = FakeDesk(w, status="paused")             # paused, the phone active: Spotify can't tell before acting
    playing_after_press(desk, w, audio=False)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              monkeypatch=monkeypatch)
    assert res.outcome == "unverified" and not res.done
    assert res.say == S.LINE_OTHER_AFTER
    row = rows_of(client, "spotify.playlist")[0]
    assert row["outcome"] == "unverified" and row["checked"] is None


def test_a_cold_window_that_does_not_build_in_time_is_couldnt(client, loaded, monkeypatch) -> None:
    desk = FakeDesk(Window(frame_only=True))
    desk.warm_built = False
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              monkeypatch=monkeypatch)
    assert not res.done and desk.pressed == []
    assert res.tried[0]["outcome"] == "cold"
    warms = [c[1] for c in desk.calls if c[0] == "warm"]
    assert warms and all(0 < b <= S.REQUEST_WARM_S for b in warms)


def test_the_warm_up_runs_once_per_spotify_process(client, loaded, monkeypatch) -> None:
    w = Window()
    desk = FakeDesk(w)
    playing_after_press(desk, w)
    run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
        monkeypatch=monkeypatch)
    w2 = Window()
    desk2 = FakeDesk(w2)
    playing_after_press(desk2, w2)
    run(client, loaded, desk2, "spotify.playlist", {"playlist": "Velvet Harbour"}, ["play Velvet Harbour"],
        monkeypatch=monkeypatch)
    assert sum(1 for c in desk.calls if c[0] == "warm") == 1
    assert sum(1 for c in desk2.calls if c[0] == "warm") == 0             # same Spotify process: already warm


def test_a_missing_label_is_couldnt_never_done(client, loaded, monkeypatch) -> None:
    w = Window(play_word="Abspielen", pause_word="Anhalten", home_cards=())   # a wording the data doesn't know
    desk = FakeDesk(w)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              monkeypatch=monkeypatch)
    assert not res.done and desk.pressed == []
    assert res.tried[0]["outcome"] == "no_wording"
    # the slider: no label and two 0-1 sliders (no single shape) -> couldn't
    desk2 = FakeDesk(Window(volume_label="Lautstärke ändern", extra_slider=True))
    res2 = run(client, loaded, desk2, "volume", {"target": "Spotify", "level": 40}, ["Spotify to 40"],
               monkeypatch=monkeypatch)
    assert not res2.done and desk2.sets == [] and res2.tried[0]["outcome"] == "slider_ambiguous"


def test_a_learned_row_wording_lets_another_language_play(client, loaded, monkeypatch) -> None:
    store = client.store
    store.set_nudge_state(SD.LABELS_KEY, store.cipher.encrypt(json.dumps(
        {"play": "Abspielen {name}", "pause": "Anhalten {name}", "grid": "Your Library"})))
    w = Window(play_word="Abspielen", pause_word="Anhalten", home_cards=())
    desk = FakeDesk(w)
    playing_after_press(desk, w)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              monkeypatch=monkeypatch)
    assert res.done and [n for _p, n, _v in desk.pressed] == [w.row_buttons["Copper Kites"][0]]


def test_the_collapsed_library_or_a_podcasts_filter_is_couldnt(client, loaded, monkeypatch) -> None:
    desk = FakeDesk(Window(library=False, home_cards=()))
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              monkeypatch=monkeypatch)
    assert not res.done and desk.pressed == [] and res.tried[0]["outcome"] == "no_library"
    podcasts = Window(rows=[("Invented Talk Show", "Podcast host", "play")], home_cards=())
    desk2 = FakeDesk(podcasts)
    res2 = run(client, loaded, desk2, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
               monkeypatch=monkeypatch)
    assert not res2.done and desk2.pressed == [] and res2.tried[0]["outcome"] == "not_in_view"


def _search_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """The search labels a later data update would add to sites/spotify.toml (invented here for the test)."""
    base = SD.data()
    data = json.loads(json.dumps(base))
    en = data["labels"]["en"]
    en["library_panel"] = [["Group", "Invented library panel"]]
    en["controls"]["library_search"] = ["Button", "Search in Your Library", "library_panel"]
    en["controls"]["library_filter"] = ["Edit", "Search in Your Library", "library_panel"]
    monkeypatch.setattr(SD, "data", lambda: data)


def test_search_in_your_library_plays_the_row_and_leaves_the_filter_cleared(client, loaded, monkeypatch) -> None:
    _search_labels(monkeypatch)
    w = Window(home_cards=(), search_box=True)
    desk = FakeDesk(w)
    hidden = w._row("Moss Radio", "Odile Fenn", "play")

    def hook(pattern: str, node: Node, value: Any) -> None:
        if pattern == "value" and node is w.search_box:
            w.grid.children[:] = [hidden] if value else [w._row(n, o, s) for n, o, s in w.rows]
        if pattern == "invoke" and node.name == "Play Moss Radio":
            node.name = "Pause Moss Radio"
            desk.status, desk.audio = "playing", True

    desk.on_press = hook
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Moss Radio"}, ["play Moss Radio"],
              monkeypatch=monkeypatch)
    assert res.route == "A:search:driver" and res.outcome == "done"
    assert w.search_box.value == ""                                     # the filter is cleared afterwards (L13)
    values = [v for p, n, v in desk.pressed if p == "value"]
    assert values == ["Moss Radio", ""]


def test_search_without_its_labels_in_the_data_says_couldnt(client, loaded, monkeypatch) -> None:
    w = Window(home_cards=(), search_box=True)
    desk = FakeDesk(w)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Moss Radio"}, ["play Moss Radio"],
              monkeypatch=monkeypatch)
    assert not res.done and desk.pressed == []
    assert any(t["outcome"] == "no_label" for t in res.tried)


def test_a_minimised_spotify_is_restored_only_with_no_game_and_the_previous_window_put_back(client, loaded,
                                                                                            monkeypatch) -> None:
    w = Window()
    desk = FakeDesk(w, state="minimised")
    ignored = {"on": True}

    def hook(pattern: str, node: Node, value: Any) -> None:
        if pattern == "invoke" and not ignored["on"]:
            node.name = "Pause Copper Kites"
            desk.status, desk.audio = "playing", True

    desk.on_press = hook
    real_focus = desk.focus

    def focus(hwnd: int) -> Any:
        if hwnd == HWND:
            ignored["on"] = False                       # once in front, the press lands
        return real_focus(hwnd)

    desk.focus = focus                                  # type: ignore[method-assign]
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              monkeypatch=monkeypatch)
    assert res.route == "A:restore:driver" and res.done
    assert desk.focused == [HWND, PREV]                  # restored, then the previous window put back in front
    # with a game in front: nothing is restored, the run is held
    desk2 = FakeDesk(Window(), state="minimised")
    res2 = run(client, loaded, desk2, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
               game=GAME, monkeypatch=monkeypatch)
    assert res2.outcome == "held_game" and desk2.focused == [] and desk2.pressed == []


def test_a_readable_minimised_window_without_the_row_is_not_restored(client, loaded, monkeypatch) -> None:
    desk = FakeDesk(Window(home_cards=()), state="minimised")
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Not In Shelf"}, ["play Not In Shelf"],
              monkeypatch=monkeypatch)
    assert desk.focused == [] and not res.done


def test_spotify_not_running_is_launched_only_with_no_game_in_front(client, loaded, monkeypatch) -> None:
    desk = FakeDesk(None)
    w = Window()

    def launched(args: list[str], timeout_s: float = 8.0) -> Any:
        desk.launched.append(list(args))
        desk.launch_timeouts.append(timeout_s)
        desk.w = w
        return _Result(True, {})

    desk.launch = launched                              # type: ignore[method-assign]
    playing_after_press(desk, w)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              monkeypatch=monkeypatch)
    assert desk.launched == [["--minimized"]] and res.route == "A:launch:driver" and res.done
    desk2 = FakeDesk(None)
    res2 = run(client, loaded, desk2, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
               game=GAME, monkeypatch=monkeypatch)
    assert res2.outcome == "held_game" and desk2.launched == []


# ---------------------------------------------------------------------------------------------------------------------
# volume
# ---------------------------------------------------------------------------------------------------------------------


def test_spotify_volume_moves_its_own_slider_by_rangevalue_and_reads_it_back(client, loaded, monkeypatch) -> None:
    w = Window(volume=0.42)
    desk = FakeDesk(w)
    res = run(client, loaded, desk, "volume", {"target": "Spotify", "change": "down"}, ["turn Spotify down a bit"],
              monkeypatch=monkeypatch)
    assert res.outcome == "done" and res.route == "A:spotify_in_app:driver"
    assert res.done_line == "Spotify's own volume, 32%."
    assert desk.sets == [0.32] and desk.volumes == [] and desk.slider_focus == 0 and desk.hotkeys == []
    assert res.recheck_verdict is True
    row = rows_of(client, "volume")[0]
    assert (row["app"], row["value"], row["checked"]) == ("spotify", 32.0, 1)


def test_spotify_volume_to_a_level_and_already_there(client, loaded, monkeypatch) -> None:
    desk = FakeDesk(Window(volume=0.42))
    res = run(client, loaded, desk, "volume", {"target": "Spotify", "level": 40}, ["Spotify to 40"],
              monkeypatch=monkeypatch)
    assert res.done and res.done_line == "Spotify's own volume, 40%."
    desk2 = FakeDesk(Window(volume=0.40))
    res2 = run(client, loaded, desk2, "volume", {"target": "Spotify", "level": 40}, ["Spotify to 40"],
               monkeypatch=monkeypatch)
    assert res2.outcome == "already" and res2.done_line == "Spotify's own volume is already at 40%."


def test_spotify_volume_is_never_faked_with_the_mixer(client, loaded, monkeypatch) -> None:
    desk = FakeDesk(Window(volume=0.42), state="covered")
    desk.ignore_when = lambda: True                    # its slider ignores the set
    res = run(client, loaded, desk, "volume", {"target": "Spotify", "level": 20}, ["Spotify to 20"],
              monkeypatch=monkeypatch)
    assert not res.done and desk.volumes == []          # the mixer is never set for Spotify's own volume
    assert res.outcome == "handed_over"                 # no game in front: the thinker with the skill pinned
    assert {t["route"]: t["outcome"] for t in res.tried}["A:mixer:driver"] == "own_control"


def test_the_mixer_route_needs_both_link_directions_recorded_as_data(client, loaded, monkeypatch) -> None:
    for linked, used in ((False, False), ("one_way", False), (True, True)):
        mixer = {"slider_moves_mixer": linked in (True, "one_way"), "mixer_moves_slider": linked is True}
        monkeypatch.setattr(S, "spotify_data", lambda m=mixer: {"mixer": m})
        w = Window(volume=0.60)
        desk = FakeDesk(w, state="covered")
        desk.ignore_when = lambda: True
        real = desk.app_volume

        def follows(app: str, **kw: Any) -> Any:
            got = real(app, **kw)
            w.slider.range = (desk.mixer[app] / 100.0, 0.0, 1.0)   # one control: the slider follows
            return got

        desk.app_volume = follows                       # type: ignore[method-assign]
        res = run(client, loaded, desk, "volume", {"target": "Spotify", "level": 30}, ["Spotify to 30"],
                  game=GAME, monkeypatch=monkeypatch)
        assert (desk.volumes != []) is used
        if used:
            assert res.done and res.route == "A:spotify_mixer:driver" and desk.volumes[0][0] == "Spotify.exe"
        else:
            assert res.outcome == "held_game"


def test_a_linked_mixer_whose_slider_did_not_follow_is_contradicted(client, loaded, monkeypatch) -> None:
    monkeypatch.setattr(S, "spotify_data", lambda: {"mixer": {"slider_moves_mixer": True, "mixer_moves_slider": True}})
    desk = FakeDesk(Window(volume=0.60), state="covered")
    desk.ignore_when = lambda: True
    res = run(client, loaded, desk, "volume", {"target": "Spotify", "level": 30}, ["Spotify to 30"], game=GAME,
              monkeypatch=monkeypatch)
    assert res.outcome == "failed" and not res.done and desk.volumes and res.tried[-1]["outcome"] == "contradicted"


def test_h1_game_in_front_reverting_slider_gives_after_the_round_with_no_focus_keys_or_pointer(
        client, tmp_path, monkeypatch) -> None:
    folder = _proven_copy(tmp_path, volume=["covered"], library=["covered"])
    loaded = C.load([folder])
    R.seed(client.store, loaded)
    desk = FakeDesk(Window(volume=0.42), state="covered")
    desk.ignore_when = lambda: True                    # SetValue reverts (a backgrounded CEF)
    res = run(client, loaded, desk, "volume", {"target": "Spotify", "change": "down"}, ["Spotify down"], game=GAME,
              monkeypatch=monkeypatch)
    assert res.outcome == "held_game" and res.say == HELD_LINE
    assert desk.sets                                    # RangeValue was tried (patterns are allowed mid-game)
    assert desk.slider_focus == 0 and desk.hotkeys == [] and desk.focused == []
    assert desk.opened == [] and desk.launched == [] and desk.volumes == []


def test_spotifys_keys_only_with_spotify_in_front(client, loaded, monkeypatch) -> None:
    w = Window(volume=0.42)
    desk = FakeDesk(w, state="front")
    desk.ignore_when = lambda: True                    # the slider ignores RangeValue; its keys still work
    res = run(client, loaded, desk, "volume", {"target": "Spotify", "level": 60}, ["Spotify to 60"],
              monkeypatch=monkeypatch)
    assert res.route == "A:spotify_keys:driver" and res.done and desk.hotkeys
    assert set(desk.hotkeys) == {("ctrl", "up")} and desk.focused == []
    desk2 = FakeDesk(Window(volume=0.42), state="covered")
    desk2.ignore_when = lambda: True
    res2 = run(client, loaded, desk2, "volume", {"target": "Spotify", "level": 60}, ["Spotify to 60"],
               monkeypatch=monkeypatch)
    assert desk2.hotkeys == [] and not res2.done


def test_a_minimised_spotify_slider_is_restored_set_and_the_window_put_back(client, loaded, monkeypatch) -> None:
    w = Window(volume=0.42)
    desk = FakeDesk(w, state="minimised")
    desk.ignore_when = lambda: desk.state_ == "minimised"    # ignored while minimised, taken once in front
    res = run(client, loaded, desk, "volume", {"target": "Spotify", "level": 55}, ["Spotify to 55"],
              monkeypatch=monkeypatch)
    assert res.route == "A:spotify_restore:driver" and res.done
    assert desk.focused == [HWND, PREV] and desk.slider_focus == 0


def test_mute_goes_through_the_mixer_and_a_mixer_usual_uses_it(client, loaded, monkeypatch) -> None:
    desk = FakeDesk(Window())
    res = run(client, loaded, desk, "volume", {"target": "Spotify", "mute": "on"}, ["mute Spotify"],
              monkeypatch=monkeypatch)
    assert res.done and desk.volumes == [("spotify", None, None, True)] and desk.sets == []
    desk2 = FakeDesk(Window())
    res2 = run(client, loaded, desk2, "volume", {"target": "Spotify", "level": 30, "control": "mixer"},
               ["Spotify mixer to 30"], monkeypatch=monkeypatch)
    assert res2.done and res2.route == "A:mixer:driver" and desk2.volumes[0][:2] == ("spotify", 30.0)
    assert res2.done_line == "Spotify's volume, 30% in the Windows mixer."


def test_other_apps_the_pc_and_yukis_voice(client, loaded, monkeypatch) -> None:
    desk = FakeDesk(Window())
    res = run(client, loaded, desk, "volume", {"target": "Chrome", "change": -20}, ["Chrome down 20"],
              monkeypatch=monkeypatch)
    assert res.done and res.route == "A:mixer:driver" and desk.volumes == [("chrome", None, -20.0, None)]
    assert res.done_line == "Chrome's volume, 30%."
    row = rows_of(client, "volume")[-1]
    assert row["app"] == "chrome"
    res2 = run(client, loaded, desk, "volume", {"target": "system", "level": 25}, ["the PC to 25"],
               monkeypatch=monkeypatch)
    assert res2.done and res2.route == "A:system:driver" and res2.done_line == "The PC's volume, 25%."
    assert rows_of(client, "volume")[-1]["app"] == "master"
    res3 = run(client, loaded, desk, "volume", {"target": "own_voice", "level": 70}, ["your voice to 70"],
               monkeypatch=monkeypatch)
    assert res3.done and res3.route == "A:own_voice:driver" and desk.volumes[-1][0] == "pythonw.exe"


def test_no_amount_asks(client, loaded, monkeypatch) -> None:
    desk = FakeDesk(Window())
    res = run(client, loaded, desk, "volume", {"target": "Spotify"}, ["Spotify volume"], monkeypatch=monkeypatch)
    assert res.outcome == "asked" and res.ask == S.ASK_AMOUNT and desk.sets == []


def test_other_apps_volume_runs_with_a_game_in_front(client, loaded, monkeypatch) -> None:
    desk = FakeDesk(Window())
    res = run(client, loaded, desk, "volume", {"target": "Chrome", "change": "down"}, ["Chrome quieter"], game=GAME,
              monkeypatch=monkeypatch)
    assert res.done and desk.volumes and desk.focused == [] and desk.hotkeys == []


# ---------------------------------------------------------------------------------------------------------------------
# The window-state rule is data; H1 by construction; D18; logs
# ---------------------------------------------------------------------------------------------------------------------


def _proven_copy(tmp_path: Path, *, volume: list[str], library: list[str]) -> Path:
    """The shipped connection files with the live checks' result for the no-focus Spotify rungs filled in."""
    folder = tmp_path / "proven"
    folder.mkdir()
    for name in ("spotify.toml", "audio.toml"):
        text = (SHIPPED / name).read_text(encoding="utf-8")
        states = library if name == "spotify.toml" else volume
        variant = 'variant = "library"' if name == "spotify.toml" else 'variant = "spotify_in_app"'
        head, _, tail = text.partition(variant)
        tail = tail.replace("game_states = []", f"game_states = {json.dumps(states)}", 1)
        (folder / name).write_text(head + variant + tail, encoding="utf-8")
    return folder


def test_the_window_state_rule_comes_from_data(client, tmp_path, monkeypatch) -> None:
    shipped = C.load([SHIPPED])
    desk = FakeDesk(Window(), state="covered")
    res = run(client, shipped, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              game=GAME, monkeypatch=monkeypatch)
    assert res.outcome == "held_game" and desk.pressed == []           # nothing proven yet
    loaded = C.load([_proven_copy(tmp_path, volume=["covered"], library=["covered"])])
    R.seed(client.store, loaded)
    w = Window()
    desk2 = FakeDesk(w, state="covered")
    playing_after_press(desk2, w)
    res2 = run(client, loaded, desk2, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
               game=GAME, monkeypatch=monkeypatch)
    assert res2.done and res2.route == "A:library:driver"              # covered proven: patterns only, mid-game
    desk3 = FakeDesk(Window(), state="minimised")
    res3 = run(client, loaded, desk3, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
               game=GAME, monkeypatch=monkeypatch)
    assert res3.outcome == "held_game" and desk3.pressed == [] and desk3.focused == []   # minimised not proven
    assert S.proven(loaded)["spotify.playlist"] == {"covered": True, "visible": False, "minimised": False}
    assert S.proven(shipped) == {"volume": {"covered": False, "visible": False, "minimised": False},
                                 "spotify.playlist": {"covered": False, "visible": False, "minimised": False},
                                 "mixer_linked": False}


def _calls_in(fn: ast.FunctionDef) -> list[ast.Call]:
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call)]


def test_no_key_hover_focus_link_or_launch_is_reachable_from_a_no_focus_rung() -> None:
    """H1 by construction: walk the driver's own call graph from every no-focus rung; none reaches a Desk method that
    takes focus or sends input, and every slider move passes ``press=None`` (slider.drive's keys branch) and never a
    hover or a hotkey."""
    src = Path(S.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    forbidden = {"focus", "hotkey", "open_uri", "launch", "SetFocus", "set_slider", "hover", "press_key",
                 "SendInput", "startfile"}
    for name in S.NO_FOCUS_ROUTES:
        seen: set[str] = set()
        todo = [name]
        while todo:
            fn = todo.pop()
            if fn in seen or fn not in funcs:
                continue
            seen.add(fn)
            for call in _calls_in(funcs[fn]):
                target = call.func
                attr = target.attr if isinstance(target, ast.Attribute) else (
                    target.id if isinstance(target, ast.Name) else "")
                assert attr not in forbidden, f"{name} reaches {attr} through {fn}"
                if attr == "drive":
                    kws = {k.arg: k.value for k in call.keywords}
                    assert isinstance(kws.get("press"), ast.Constant) and kws["press"].value is None
                    assert "hover" not in kws and "hotkey" not in kws
                if attr in funcs:
                    todo.append(attr)
    # the data agrees: every route that says needs_focus = false calls a no-focus rung, and every focus-taking rung
    # says game = "never"
    loaded = C.load([SHIPPED])
    for skill in loaded.skills:
        for route in skill.routes:
            if route.via == "driver":
                assert (route.call in S.NO_FOCUS_ROUTES) is (not route.needs_focus), route.key
                if route.needs_focus:
                    assert route.game == "never", route.key


def test_d18_the_run_names_spotify_and_the_playlist_the_memory_side_will_read(client, loaded, monkeypatch) -> None:
    from yuki.memory.moments import Moment, attribute, skill_runs_between

    w = Window()
    desk = FakeDesk(w)
    playing_after_press(desk, w)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              monkeypatch=monkeypatch)
    assert res.done
    row = rows_of(client, "spotify.playlist")[0]
    m = Moment(kind="media_context", at=row["t0"] + 3.0, source="spotify", app="spotify", entity="Copper Kites",
               facts={"since": row["t0"] - 1.0})
    m.digest_with(client.store.cipher)
    assert row["entity_digest"] == m.entity_digest
    got = attribute(m, skill_runs_between(client.store, row["t0"] - 5, row["t0"] + 60), now=row["t0"] + 4.0)
    assert got.by == "yuki"


def test_logs_and_the_ledger_carry_no_names(client, loaded, monkeypatch) -> None:
    logs: list = []
    w = Window()
    desk = FakeDesk(w)
    playing_after_press(desk, w)
    run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"], logs=logs,
        monkeypatch=monkeypatch)
    run(client, loaded, FakeDesk(Window()), "volume", {"target": "Spotify", "level": 40}, ["Spotify to 40"],
        logs=logs, monkeypatch=monkeypatch)
    _learn_link(client, "Paper Comets")
    run(client, loaded, FakeDesk(Window(home_cards=())), "spotify.playlist", {"playlist": "Paper Comets"},
        ["play Paper Comets"], logs=logs, monkeypatch=monkeypatch)
    text = json.dumps(logs, default=str)
    assert "spotify_read" in text and "route_try" in text
    for name in NAMES + (IDS["Paper Comets"],):
        assert name not in text
    ledger = json.dumps([dict(r) for r in client.store._query("SELECT * FROM skill_runs")], default=str)
    for name in NAMES:
        assert name not in ledger


def test_the_state_hook_reads_spotifys_window_state(monkeypatch) -> None:
    monkeypatch.setattr(S, "DESK", FakeDesk(Window(), state="visible"))
    assert S.window_state("spotify") == "visible" and S.window_state("audio") == "visible"
    monkeypatch.setattr(S, "DESK", FakeDesk(None))
    assert S.window_state("spotify") is None


def test_probe_evidence_is_content_free(monkeypatch) -> None:
    w = Window(rows=[("Velvet Harbour", "Tamsin Reyes", "pause")])
    monkeypatch.setattr(S, "DESK", FakeDesk(w, status="playing", audio=True))
    from yuki.skills.donecheck import ProbeContext

    verdict, ev = S.probe_playing_here(ProbeContext(since=0.0), {"name": "Velvet Harbour"})
    assert verdict is True and "Velvet" not in json.dumps(ev)
    verdict, ev = S.probe_still_playing(ProbeContext(since=0.0), {"name": "Velvet Harbour"})
    assert verdict is True


def test_the_driver_module_holds_no_ui_label_or_link_as_code() -> None:
    """Labels and link templates live in sites/spotify.toml only (D4): the driver's code names none of them."""
    src = Path(S.__file__).read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    body = ast.parse(code)
    strings = [n.value for n in ast.walk(body) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    doc = ast.get_docstring(body, clean=False) or ""
    docs = {ast.get_docstring(n, clean=False) for n in ast.walk(body) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    literals = [s for s in strings if s != doc and s not in docs]
    for label in ("Change volume", "Your Library", "Now playing bar", "Player controls", "Play {name}",
                  "Pause {name}", "xpui.app.spotify.com", "Search in Your Library"):
        assert not any(label in s for s in literals), label


def test_the_this_pc_check_reads_core_audio_once_per_run_and_stops_early_when_unreadable(client, loaded,
                                                                                         monkeypatch) -> None:
    desk = FakeDesk(Window(home_cards=()), status="playing", audio=None)    # Core Audio unreadable
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Not In Shelf"}, ["play Not In Shelf"],
              monkeypatch=monkeypatch)
    assert not res.done
    assert sum(1 for c in desk.calls if c[0] == "render") == 2            # two unread reads, then "unknown", once
    assert res.clock.t - 1000.0 < S.THIS_PC_S                            # no 2 s wait per rung


def test_a_pattern_call_on_a_games_own_window_is_refused(client, loaded, monkeypatch) -> None:
    from yuki.memory import gamegate

    monkeypatch.setattr(gamegate, "window_game", lambda hwnd: GAME)          # never Spotify, but fail safe
    desk = FakeDesk(Window())
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              monkeypatch=monkeypatch)
    assert desk.pressed == [] and not res.done and res.tried[0]["outcome"] == "refused"
    desk2 = FakeDesk(Window())
    res2 = run(client, loaded, desk2, "volume", {"target": "Spotify", "level": 20}, ["Spotify to 20"],
               monkeypatch=monkeypatch)
    assert desk2.sets == [] and desk2.hotkeys == [] and not res2.done


def test_a_relative_change_that_moved_short_is_never_done_twice(client, loaded, monkeypatch) -> None:
    w = Window(volume=0.42)
    desk = FakeDesk(w)
    real_live = desk.live_slider

    class Short(FakeSlider):
        def set(self, value: float) -> None:          # takes half the way, then stays
            self.desk.sets.append(round(float(value), 4))
            self.node.range = ((float(self.node.range[0]) + float(value)) / 2.0, 0.0, 1.0)

    desk.live_slider = lambda ref, automation: Short(ref.node, desk)        # type: ignore[method-assign]
    res = run(client, loaded, desk, "volume", {"target": "Spotify", "change": -20}, ["Spotify down 20"],
              monkeypatch=monkeypatch)
    assert res.outcome == "failed" and not res.done
    assert len(desk.sets) == 1 and desk.hotkeys == [] and desk.focused == []   # no second rung after it acted
    assert real_live is not None


def test_property_done_only_when_row_session_and_sound_agree(client, loaded, monkeypatch) -> None:
    """Over random Spotify behaviours (a press that lands or not, sound here or not, the session's status before,
    the window state, a game in front or not): "done" (or "already") is said only when, at the end, the row reads
    "Pause {name}", the session plays and Spotify sounds on this PC; and nothing is ever pressed while Spotify plays
    on another device."""
    import random

    rnd = random.Random(20261010)
    outcomes: list[str] = []
    for trial in range(120):
        lands = rnd.random() < 0.6
        audio_after = rnd.choice([True, True, False, None])
        status0 = rnd.choice(["paused", "playing", "stopped", "none"])
        audio0 = rnd.choice([True, False, None])
        row0 = rnd.choice(["play", "play", "pause"])
        state = rnd.choice(["front", "covered", "visible", "minimised"])
        game = rnd.choice(["", "", GAME])
        w = Window(rows=[("Copper Kites", "Odile Fenn", row0), ("Velvet Harbour", "Tamsin Reyes", "play")],
                   home_cards=())
        desk = FakeDesk(w, state=state, status=status0, audio=audio0)

        def hook(pattern: str, node: Node, value: Any, desk=desk, w=w, lands=lands, audio_after=audio_after) -> None:
            if pattern == "invoke" and lands and node.name == "Play Copper Kites":
                node.name = "Pause Copper Kites"
                desk.status, desk.audio = "playing", audio_after

        def resumed(verb: str, desk=desk, lands=lands, audio_after=audio_after) -> None:
            if lands:
                desk.status, desk.audio = "playing", audio_after

        desk.on_press, desk.on_media = hook, resumed
        S.forget_warm()
        res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
                  game=game, monkeypatch=monkeypatch)
        row_now = w.row_buttons["Copper Kites"][0].name
        outcomes.append(res.outcome)
        if res.done:
            assert row_now == "Pause Copper Kites" and desk.status == "playing" and desk.audio is True, trial
        if status0 == "playing" and audio0 is False:
            assert desk.pressed == [] and desk.media_calls == [] and res.outcome in ("other_device", "held_game"), trial
        if game:
            assert desk.focused == [] and desk.opened == [] and desk.launched == [], trial
    # the property is not vacuous: the trials reach dones, no-ops, unconfirmed runs, other devices and holds
    assert {"done", "already", "unverified", "other_device", "held_game"} <= set(outcomes), sorted(set(outcomes))


def test_a_hand_off_keeps_a_machine_check_the_thinkers_own_steps_can_raise(loaded, monkeypatch) -> None:
    """Rung C (the thinker with the skill pinned): the pinned check is read by the finish gate with no route result.
    A playlist: its row, session and sound; a volume: the target's volume change seen since the hand-off."""
    from yuki.skills.donecheck import check

    pl = E._done_spec(loaded.get("spotify.playlist"), {"playlist": {"name": "Velvet Harbour"}})
    assert pl["call"] == "playing_here" and pl["name"] == "Velvet Harbour"
    monkeypatch.setattr(S, "DESK", FakeDesk(Window(rows=[("Velvet Harbour", "Tamsin Reyes", "pause")]),
                                            status="playing", audio=True))
    assert check(pl, since=0.0).verdict is True
    monkeypatch.setattr(S, "DESK", FakeDesk(Window(), status="paused", audio=False))
    assert check(pl, since=0.0, within_s=0.0).verdict is False

    vol = E._done_spec(loaded.get("volume"), {"target": "chrome", "change": -10.0, "control": "in_app"})
    assert vol["call"] == "slider_still" and vol["target"] == "chrome"
    seen = [{"kind": "volume_change", "app": "chrome"}]
    store = type("St", (), {"moments_between": staticmethod(lambda a, b: seen)})()
    client = type("Cl", (), {"store": store})()
    assert check(vol, since=0.0, memory_call=lambda what, fn: fn(client)).verdict is True
    seen.clear()
    assert check(vol, since=0.0, within_s=0.0, memory_call=lambda what, fn: fn(client)).verdict is None
    sys_spec = E._done_spec(loaded.get("volume"), {"target": "system", "level": 30.0, "control": "in_app"})
    seen.append({"kind": "volume_change", "app": "master"})
    assert check(sys_spec, since=0.0, memory_call=lambda what, fn: fn(client)).verdict is True


def test_d18_the_run_names_the_app_it_acts_on_from_its_start(loaded) -> None:
    """The ledger row is written at t0 with the first rung's app (a Core Audio or slider moment during the run is
    then matched to it): Spotify for Spotify's volume, the named app for the mixer, ``master`` for the PC."""
    for slots, want in (({"target": "Spotify", "level": 30}, "spotify"), ({"target": "Chrome", "level": 30}, "chrome"),
                        ({"target": "system", "level": 30}, "master")):
        plan = resolve("volume", slots, [f"{slots['target']} to 30"], loaded=loaded, live=Live())
        assert E._first_app(plan.skill, plan.routes, plan.values) == want
    plan = resolve("spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"], loaded=loaded, live=Live())
    assert E._first_app(plan.skill, plan.routes, plan.values) == "spotify"


# ---------------------------------------------------------------------------------------------------------------------
# Review round 1
# ---------------------------------------------------------------------------------------------------------------------


def test_r1_m1_closed_to_the_tray_with_the_phone_playing_is_another_device_before_anything(client, loaded,
                                                                                          monkeypatch) -> None:
    """Spotify has no window (closed to the tray) and its session reports the phone's playback: "another device",
    with 0 presses, 0 opens and 0 launches, for a playlist (with and without a learned link) and for Spotify's
    volume (review r1 M1, B.2 M2, Q11)."""
    w = Window()
    desk = FakeDesk(None, status="playing", audio=False)

    def launched(args: list[str], timeout_s: float = 8.0) -> Any:
        desk.launched.append(list(args))
        desk.w = w
        return _Result(True, {})

    desk.launch = launched                              # type: ignore[method-assign]
    playing_after_press(desk, w, audio=False)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              monkeypatch=monkeypatch)
    assert res.outcome == "other_device" and res.say == S.LINE_OTHER and not res.done
    assert desk.pressed == [] and desk.opened == [] and desk.launched == [] and desk.media_calls == []
    # with a learned link: never opened
    _learn_link(client, "Paper Comets")
    desk2 = FakeDesk(None, status="playing", audio=False)
    desk2.on_open = lambda u: setattr(desk2, "w", Window(home_cards=(), page=("Paper Comets", "play")))
    res2 = run(client, loaded, desk2, "spotify.playlist", {"playlist": "Paper Comets"}, ["play Paper Comets"],
               monkeypatch=monkeypatch)
    assert res2.outcome == "other_device" and desk2.opened == [] and desk2.pressed == [] and desk2.launched == []
    # Spotify's volume: "another device", never a hand-off, nothing set
    desk3 = FakeDesk(None, status="playing", audio=False)
    res3 = run(client, loaded, desk3, "volume", {"target": "Spotify", "change": "down"}, ["Spotify down"],
               monkeypatch=monkeypatch)
    assert res3.outcome == "other_device" and res3.say == S.LINE_OTHER_VOLUME
    assert desk3.sets == [] and desk3.volumes == [] and desk3.hotkeys == [] and desk3.focused == []
    # never counted against a rung
    for key in ("spotify.playlist", "volume"):
        assert all(t.failed == 0 and t.streak == 0 for t in R.trust(client.store, key).values())


def test_r1_m1_a_spotify_started_by_the_run_is_checked_again_before_its_press(client, loaded, monkeypatch) -> None:
    """Not running (no session) before; once the launch or the learned link starts it, its session reports the
    phone's playback with no sound here: "another device", nothing pressed (review r1 M1)."""
    w = Window()
    desk = FakeDesk(None, status="none", audio=False)

    def launched(args: list[str], timeout_s: float = 8.0) -> Any:
        desk.launched.append(list(args))
        desk.launch_timeouts.append(timeout_s)
        desk.w, desk.status = w, "playing"             # the phone was playing all along
        return _Result(True, {})

    desk.launch = launched                              # type: ignore[method-assign]
    playing_after_press(desk, w, audio=False)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              monkeypatch=monkeypatch)
    assert desk.launched == [["--minimized"]] and res.outcome == "other_device" and desk.pressed == []
    assert desk.launch_timeouts and 0 < desk.launch_timeouts[0] <= E.BUDGET_S      # bounded by the time left (L4)
    uri = _learn_link(client, "Paper Comets")
    page = Window(home_cards=(), page=("Paper Comets", "play"))
    desk2 = FakeDesk(None, status="none", audio=False)

    def opened(u: str) -> None:
        desk2.w, desk2.status = page, "playing"

    desk2.on_open = opened
    playing_after_press(desk2, page, audio=False)
    res2 = run(client, loaded, desk2, "spotify.playlist", {"playlist": "Paper Comets"}, ["play Paper Comets"],
               monkeypatch=monkeypatch)
    assert desk2.opened == [uri] and desk2.pressed == [] and res2.outcome == "other_device"


def _minimised_ignoring(w: Window, *, lands_after_reads: int | None = None) -> FakeDesk:
    """Spotify minimised: a no-focus Invoke is ignored (or, with ``lands_after_reads``, lands that many reads later);
    once restored to the front a press lands at once."""
    desk = FakeDesk(w, state="minimised")
    pending: dict[str, Any] = {"node": None, "reads": 0}

    def flip(node: Node) -> None:
        node.name = "Pause " + node.name[len("Play "):]
        desk.status, desk.audio = "playing", True

    def hook(pattern: str, node: Node, value: Any) -> None:
        if pattern != "invoke" or not node.name.startswith("Play "):
            return
        if desk.state_ == "front":
            flip(node)
        elif lands_after_reads is not None:
            pending["node"] = node

    real_uia = desk.uia

    def uia(job: Any, budget_s: float) -> Any:
        if pending["node"] is not None:
            pending["reads"] += 1
            if pending["reads"] >= int(lands_after_reads or 0):
                flip(pending["node"])
                pending["node"] = None
        return real_uia(job, budget_s)

    desk.on_press = hook
    desk.uia = uia                                      # type: ignore[method-assign]
    return desk


def test_r1_m2_an_ignored_no_focus_press_is_not_taken_and_never_disables_the_library_rung(client, loaded,
                                                                                        monkeypatch) -> None:
    """Two plays with Spotify minimised where the no-focus Invoke is ignored: each time the row is re-read (bounded),
    the press counts as not taken, restore plays it, and the Library rung isn't charged; a later play with Spotify in
    front still uses the Library rung (review r1 M2)."""
    for _ in range(2):
        w = Window()
        desk = _minimised_ignoring(w)
        S.forget_warm()
        res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
                  monkeypatch=monkeypatch)
        assert res.done and res.route == "A:restore:driver"
        assert res.tried[0]["route"] == "A:library:driver" and res.tried[0]["outcome"] == "not_taken"
        assert desk.focused == [HWND, PREV]
        assert res.clock.t - 1000.0 <= E.BUDGET_S + 4.0                       # the flip wait is bounded
    lib = R.trust(client.store, "spotify.playlist")["A:library:driver"]
    assert lib.failed == 0 and lib.streak == 0 and lib.disabled_at is None
    w = Window()
    desk = FakeDesk(w, state="front")
    playing_after_press(desk, w)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              monkeypatch=monkeypatch)
    assert res.done and res.route == "A:library:driver" and len(desk.pressed) == 1


def test_r1_m2_a_no_focus_press_that_flips_within_the_wait_is_the_library_rungs_done(client, loaded,
                                                                                   monkeypatch) -> None:
    w = Window()
    desk = _minimised_ignoring(w, lands_after_reads=2)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              monkeypatch=monkeypatch)
    assert res.done and res.route == "A:library:driver" and desk.focused == [] and len(desk.pressed) == 1


def test_r1_l1_a_mixer_or_pc_level_that_cannot_move_is_said_as_a_no_op(client, loaded, monkeypatch) -> None:
    desk = FakeDesk(Window())
    desk.mixer["chrome"] = 100.0
    res = run(client, loaded, desk, "volume", {"target": "Chrome", "change": 10}, ["Chrome louder"],
              monkeypatch=monkeypatch)
    assert res.outcome == "already" and res.done_line == "Chrome's volume is already at 100%."
    row = rows_of(client, "volume")[-1]
    assert row["outcome"] == "already" and row["checked"] is None
    desk.master_now = 100.0
    res2 = run(client, loaded, desk, "volume", {"target": "system", "change": 10}, ["the PC louder"],
               monkeypatch=monkeypatch)
    assert res2.outcome == "already" and res2.done_line == "The PC's volume is already at 100%."
    assert desk.masters == []                                              # nothing set
    desk.mixer["pythonw.exe"] = 0.0
    res3 = run(client, loaded, desk, "volume", {"target": "own_voice", "change": -10}, ["your voice quieter"],
               monkeypatch=monkeypatch)
    assert res3.outcome == "already" and res3.done_line == "My voice is already at 0%."


class _LateDesk(FakeDesk):
    """A UIA job that overruns its budget in the middle of the Invoke: the caller stops waiting while the press is
    still going (released by the test afterwards)."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        import threading

        self.release = threading.Event()
        self.started = threading.Event()

    def uia(self, job: Callable[[Any, dict], None], budget_s: float) -> tuple[dict, bool]:
        import threading

        shared: dict = {}
        t = threading.Thread(target=job, args=(FakeAutomation({HWND: self.w.root}), shared), daemon=True)
        t.start()
        self.started.wait(1.0)
        t.join(0.05)
        return shared, t.is_alive()

    def press(self, ref: Any, pattern: str, value: Any = None) -> bool:
        self.started.set()
        self.release.wait(2.0)
        return super().press(ref, pattern, value)


def test_r1_l2_a_press_still_going_when_the_read_overran_is_ran_none_never_a_second_rung(monkeypatch) -> None:
    from types import SimpleNamespace

    desk = _LateDesk(Window())
    look = S._look(desk, HWND, "Copper Kites", learned=None, press=True)
    try:
        assert look.status == "ok" and look.details.get("press_late") is True
        monkeypatch.setattr(S, "DESK", desk)
        call = SimpleNamespace(ctx=SimpleNamespace(memory=None, log=None))
        got = S._after_look(call, look, "paused", STORE_ID, {})
        assert got.ran is None and got.outcome == "press_late"
    finally:
        desk.release.set()
    # a media "timeout" may have gone through too: ran None (the done-check decides)
    stale = S.Look("ok", name="Velvet Harbour", state="pause", where="grid")
    desk2 = FakeDesk(Window())
    desk2.media = lambda verb, app_id, log=None: _Media("timeout")         # type: ignore[method-assign]
    monkeypatch.setattr(S, "DESK", desk2)
    got2 = S._after_look(SimpleNamespace(ctx=SimpleNamespace(memory=None, log=None)), stale, "paused", STORE_ID, {})
    assert got2.ran is None


def test_r1_l3_the_answer_to_the_two_rows_question_plays_the_row_it_names(client, loaded, monkeypatch) -> None:
    w = Window(rows=[("Lantern Rain", "Tamsin Reyes", "play"), ("Lantern Rain", "Odile Fenn", "play")])
    desk = FakeDesk(w)
    playing_after_press(desk, w)
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Lantern Rain"}, ["play Lantern Rain"],
              monkeypatch=monkeypatch)
    assert res.outcome == "asked" and desk.pressed == []
    answer = res.options[1]
    res2 = run(client, loaded, desk, "spotify.playlist", {"playlist": answer}, [answer], monkeypatch=monkeypatch)
    assert res2.done and res2.route == "A:library:driver"
    assert [n for _p, n, _v in desk.pressed] == [w.row_buttons["Lantern Rain"][1]]
    assert res2.done_line == "Playing Lantern Rain on Spotify."
    # two rows with one owner too: no options offered (none could be acted on)
    w3 = Window(rows=[("Lantern Rain", "Tamsin Reyes", "play"), ("Lantern Rain", "Tamsin Reyes", "play")])
    res3 = run(client, loaded, FakeDesk(w3), "spotify.playlist", {"playlist": "Lantern Rain"}, ["play Lantern Rain"],
               monkeypatch=monkeypatch)
    assert res3.outcome == "asked" and res3.options == []


def test_r1_l4_spotifys_keys_stop_when_the_request_time_is_up(client, loaded, monkeypatch) -> None:
    monkeypatch.setattr(S, "KEY_WAIT_S", 0.5)
    desk = FakeDesk(Window(volume=0.10), state="front")
    desk.ignore_when = lambda: True                    # the slider ignores RangeValue: the keys rung runs
    res = run(client, loaded, desk, "volume", {"target": "Spotify", "level": 100}, ["Spotify to 100"],
              monkeypatch=monkeypatch)
    assert res.route == "A:spotify_keys:driver" and res.outcome == "unverified" and not res.done
    assert 0 < len(desk.hotkeys) < S.KEYS_MAX and "ran out of time" in res.say
    assert res.clock.t - 1000.0 <= E.BUDGET_S


def test_r1_l4_a_slider_set_job_is_bounded_by_the_time_left() -> None:
    desk = FakeDesk(Window(volume=0.42))
    S._set_slider(desk, HWND, to=30.0, by=None, budget_s=0.4)
    S._set_slider(desk, HWND, to=50.0, by=None, budget_s=30.0)
    assert [b for c, *rest in desk.calls if c == "uia" for b in rest] == [S.SET_FLOOR_S, S.SET_S]
