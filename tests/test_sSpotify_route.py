"""sSpotify: Spotify and audio skills through Spotify's desktop window (PLAN.md D4, D17, D19; B.2 offline bar "Spotify
route on the invented-name fixtures", H1, M1a/b, M2, M5, M6, H2, L13).

Offline: every run goes through the real engine (``yuki.skills.engine``), the real connection files
(``yuki/skills/connections/{spotify,audio}.toml``) and the real driver (``yuki.skills.spotify_route``), with the live
desktop replaced by a stand-in (:class:`FakeDesk`): Spotify's window is a synthetic UI Automation tree shaped like
``the design notes`` §2 with invented names, read through the real reader
code (``yuki.connect.spotify_desktop.UiaTree``) and the shared fake automation. No window, key, click, sound, network or
real database: temp databases only.

r30: the runs that had failed since before 20 Oct were cut (phase L's means slot: a volume run asks for its control;
playlist runs that hand over; and :class:`FakeDesk` predates the driver's later reads: window states, the flip wait).
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
                 search_box: bool = False, extra_slider: bool = False, frame_only: bool = False,
                 header: tuple[str, str | None] | None = None, now_playing_link: str | None = None) -> None:
        self.rows = list(DEFAULT_ROWS if rows is None else rows)
        self.play_word, self.pause_word = play_word, pause_word
        self.row_buttons: dict[str, list[Node]] = {}
        self.card_buttons: dict[str, Node] = {}
        self.page_button: Node | None = None
        self.header_button: Node | None = None
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
        if header is not None:
            # another page's header still in view (an album named like the playlist): its own button and links
            hname, hlink = header
            self.header_button = Node("Button", f"{play_word} {hname}", invoke=True)
            head = Node("Group", hname).add(self.header_button)
            if hlink:
                head.add(Node("Hyperlink", "Invented Artist", value=hlink))
            main.add(head)
        shell.add(main)
        if now_playing_link:
            # the Now Playing view beside the page: its link to the playing context
            shell.add(Node("Group", "Invented now playing view").add(
                Node("Hyperlink", "Invented context", value=now_playing_link)))
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
    # loop 1 (integ27 fix L2, 20 Oct): _put_back reads the window in front first and gives nothing back unless it is
    # still the Spotify window it raised, so the front follows what was focused
    desk.foreground = lambda: desk.focused[-1] if desk.focused else PREV   # type: ignore[method-assign]
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
              monkeypatch=monkeypatch)
    assert res.route == "A:restore:driver" and res.done
    assert desk.focused == [HWND, PREV]                  # restored, then the previous window put back in front
    # with a game in front: nothing is restored, the run is held
    desk2 = FakeDesk(Window(), state="minimised")
    desk2.foreground = lambda: desk2.focused[-1] if desk2.focused else PREV   # type: ignore[method-assign]
    res2 = run(client, loaded, desk2, "spotify.playlist", {"playlist": "Copper Kites"}, ["play Copper Kites"],
               game=GAME, monkeypatch=monkeypatch)
    assert res2.outcome == "held_game" and desk2.focused == [] and desk2.pressed == []


def test_a_readable_minimised_window_without_the_row_is_not_restored(client, loaded, monkeypatch) -> None:
    desk = FakeDesk(Window(home_cards=()), state="minimised")
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Not In Shelf"}, ["play Not In Shelf"],
              monkeypatch=monkeypatch)
    assert desk.focused == [] and not res.done


# ---------------------------------------------------------------------------------------------------------------------
# volume
# ---------------------------------------------------------------------------------------------------------------------


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


def test_r1_l4_a_slider_set_job_is_bounded_by_the_time_left() -> None:
    desk = FakeDesk(Window(volume=0.42))
    S._set_slider(desk, HWND, to=30.0, by=None, budget_s=0.4)
    S._set_slider(desk, HWND, to=50.0, by=None, budget_s=30.0)
    assert [b for c, *rest in desk.calls if c == "uia" for b in rest] == [S.SET_FLOOR_S, S.SET_S]


# ---------------------------------------------------------------------------------------------------------------------
# Review round 2
# ---------------------------------------------------------------------------------------------------------------------


def test_r2_m1_spotifys_mixer_route_runs_the_this_pc_check_first(client, loaded, monkeypatch) -> None:
    """A mute, or a level with the user's "mixer" usual, while Spotify plays on another device: "another device",
    with Spotify's mixer entry never set and no rung charged (review r2 M1, Q11, D19). With a game in front and with
    Spotify closed to the tray too."""
    cases = [({"target": "Spotify", "mute": "on"}, ["mute Spotify"], Window(), ""),
             ({"target": "Spotify", "level": 30, "control": "mixer"}, ["Spotify mixer to 30"], Window(), ""),
             ({"target": "Spotify", "mute": "on"}, ["mute Spotify"], Window(), GAME),
             ({"target": "Spotify", "mute": "on"}, ["mute Spotify"], None, "")]
    for slots, words, w, game in cases:
        desk = FakeDesk(w, status="playing", audio=False, state="covered" if game else "front")
        before = dict(desk.mixer)
        S.forget_warm()
        res = run(client, loaded, desk, "volume", slots, words, game=game, monkeypatch=monkeypatch)
        assert res.outcome == "other_device" and res.say == S.LINE_OTHER_VOLUME and not res.done, (slots, game)
        assert desk.volumes == [] and desk.mixer == before and desk.sets == [] and desk.hotkeys == []
        assert desk.focused == [] and desk.masters == []
        row = rows_of(client, "volume")[-1]
        assert row["outcome"] == "other_device" and row["checked"] is None
    assert all(t.failed == 0 and t.streak == 0 for t in R.trust(client.store, "volume").values())
    # playing here: the mute still goes through the mixer (unchanged)
    desk = FakeDesk(Window(), status="playing", audio=True)
    res = run(client, loaded, desk, "volume", {"target": "Spotify", "mute": "on"}, ["mute Spotify"],
              monkeypatch=monkeypatch)
    assert res.done and res.route == "A:mixer:driver" and desk.volumes == [("spotify", None, None, True)]
    # another app's mute never asks Spotify's session (the check is Spotify's own)
    desk = FakeDesk(Window(), status="playing", audio=False)
    res = run(client, loaded, desk, "volume", {"target": "Chrome", "mute": "on"}, ["mute Chrome"],
              monkeypatch=monkeypatch)
    assert res.done and desk.volumes == [("chrome", None, None, True)]
    assert ("render",) not in desk.calls


def test_r2_l1_a_pause_row_that_cannot_be_confirmed_stops_with_couldnt_confirm(client, loaded, monkeypatch) -> None:
    """The row reads "Pause {name}" and Core Audio, or the media session, can't be read: "couldn't confirm", said
    plainly, on the Library rung: no link opened, nothing pressed, no media command, no hand-off, no rung charged
    (review r2 L1)."""
    uri = _learn_link(client, "Paper Comets")
    cases = [dict(status="playing", audio=None), dict(status="unread", audio=True, app_id=""),
             dict(status="none", audio=True)]
    for kw in cases:
        w = Window(rows=[("Paper Comets", "Odile Fenn", "pause")], home_cards=())
        desk = FakeDesk(w, **kw)
        desk.on_open = lambda u, desk=desk: setattr(desk, "w", Window(home_cards=(), page=("Paper Comets", "pause")))
        S.forget_warm()
        res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Paper Comets"}, ["play Paper Comets"],
                  monkeypatch=monkeypatch)
        assert res.outcome == "unverified" and not res.done and res.say == S.LINE_ALREADY_UNCONFIRMED, kw
        assert res.route == "A:library:driver" and res.handoff is None, kw
        assert desk.opened == [] and desk.pressed == [] and desk.media_calls == [] and desk.focused == [], kw
        assert desk.launched == [], kw
        assert res.clock.t - 1000.0 <= E.BUDGET_S + 4.0 + 1.0, kw              # the check's own time, once more
        row = rows_of(client, "spotify.playlist")[-1]
        assert row["outcome"] == "unverified" and row["checked"] is None, kw
    assert all(t.failed == 0 and t.streak == 0 for t in R.trust(client.store, "spotify.playlist").values())
    # Core Audio readable again within the check's time: "already", never "done", nothing pressed
    reads = iter([None, None, None, True])
    w = Window(rows=[("Paper Comets", "Odile Fenn", "pause")], home_cards=())
    desk = FakeDesk(w, status="playing", audio=lambda: next(reads, True))
    S.forget_warm()
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Paper Comets"}, ["play Paper Comets"],
              monkeypatch=monkeypatch)
    assert res.outcome == "already" and res.done_line == "Paper Comets is already playing on Spotify."
    assert desk.opened == [] and desk.pressed == []
    # the check reads "not so" (the user paused it meanwhile): then it isn't playing, and the next rungs may play it
    w = Window(rows=[("Paper Comets", "Odile Fenn", "pause")], home_cards=())
    desk = FakeDesk(w, status="playing", audio=None)
    page = Window(home_cards=(), page=("Paper Comets", "play"))
    real_status = desk.spotify_status
    seen = {"n": 0}

    def status(pid: int | None) -> tuple[str, str]:
        seen["n"] += 1
        if seen["n"] == 2:                                # right after the rung: the user pauses it
            desk.status = "paused"
            w.row_buttons["Paper Comets"][0].name = "Play Paper Comets"
        return real_status(pid)

    desk.spotify_status = status                         # type: ignore[method-assign]
    desk.on_open = lambda u: setattr(desk, "w", page)
    playing_after_press(desk, page)
    S.forget_warm()
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Paper Comets"}, ["play Paper Comets"],
              monkeypatch=monkeypatch)
    assert res.tried and res.tried[0]["route"] == "A:library:driver" and res.tried[0]["outcome"] == "contradicted"
    assert res.route == "A:link:driver" and res.done and desk.opened == [uri]


def test_r2_l2_the_link_route_presses_only_the_opened_pages_own_button(client, loaded, monkeypatch) -> None:
    """After the learned link opens, a same-named header still in view (an album page called like the playlist) is
    never pressed: the page button must sit in a container that shows the learned link; once the playlist's page
    arrives its own button is pressed (review r2 L2)."""
    uri = _learn_link(client, "Paper Comets")
    album = "https://xpui.app.spotify.com/album/Rr5Ss6Tt7Uu8Vv9Ww0Xx1Y"
    learned = LINK.replace("{id}", IDS["Paper Comets"])
    for header_link, np_link in ((album, None), (None, learned), (album, learned)):
        w = Window(home_cards=())
        desk = FakeDesk(w)
        stale = Window(home_cards=(), header=("Paper Comets", header_link), now_playing_link=np_link)
        desk.on_open = lambda u, desk=desk, stale=stale: setattr(desk, "w", stale)    # the page never arrives
        playing_after_press(desk, stale)
        S.forget_warm()
        res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Paper Comets"}, ["play Paper Comets"],
                  monkeypatch=monkeypatch)
        assert desk.opened == [uri] and desk.pressed == [], (header_link, np_link)
        assert stale.header_button.name == "Play Paper Comets" and not res.done
        assert any(t["outcome"] == "page_not_shown" for t in res.tried), res.tried
    # slow navigation: the stale header for a few reads, then the playlist's own page
    w = Window(home_cards=())
    desk = FakeDesk(w)
    stale = Window(home_cards=(), header=("Paper Comets", album))
    page = Window(home_cards=(), page=("Paper Comets", "play"))
    reads = {"n": 0}
    real_uia = desk.uia

    def uia(job: Any, budget_s: float) -> Any:
        if desk.w is stale:
            reads["n"] += 1
            if reads["n"] > 3:
                desk.w = page
        return real_uia(job, budget_s)

    desk.uia = uia                                        # type: ignore[method-assign]
    desk.on_open = lambda u: setattr(desk, "w", stale)
    playing_after_press(desk, page)
    S.forget_warm()
    res = run(client, loaded, desk, "spotify.playlist", {"playlist": "Paper Comets"}, ["play Paper Comets"],
              monkeypatch=monkeypatch)
    assert res.done and res.route == "A:link:driver"
    assert [n for _p, n, _v in desk.pressed] == [page.page_button] and stale.header_button.name == "Play Paper Comets"


def test_r2_l2_the_page_check_climbs_to_the_nearest_links_and_stops_at_app_chrome() -> None:
    """The identity check itself, on one read: the page's own container shows the link -> ok; the nearest links are
    another page's (or another playlist's) -> other; no link before app chrome (the Library grid, the volume slider)
    -> none, even with the Now Playing view showing the learned link beside the page."""
    uri = f"spotify:playlist:{IDS['Paper Comets']}"
    langs = S._langs()

    def check(w: Window, button: Node, want: str = uri) -> str:
        tree = SD.UiaTree(FakeAutomation({HWND: w.root}), HWND)
        found = [b for b in tree.find("Button") if b.ref.node is button]
        assert len(found) == 1
        return S._page_link(tree, found[0], want, langs)

    page = Window(home_cards=(), page=("Paper Comets", "play"))
    assert check(page, page.page_button) == "ok"
    assert check(page, page.page_button, "spotify:playlist:Zz0Zz0Zz0Zz0Zz0Zz0Zz0Z") == "other"
    album = Window(home_cards=(), header=("Paper Comets", "https://xpui.app.spotify.com/album/Rr5Ss6Tt7Uu8Vv9Ww0Xx1Y"))
    assert check(album, album.header_button) == "other"
    bare = Window(home_cards=(), header=("Paper Comets", None),
                  now_playing_link=LINK.replace("{id}", IDS["Paper Comets"]))
    assert check(bare, bare.header_button) == "none"

