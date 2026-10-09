"""fLive: Spotify's desktop window reader (PLAN.md D4, B.1 "Spotify window reader").

Offline: synthetic UI Automation trees with invented names, shaped like the 28 Library reads in
docs/research/audit-2026-10-09/round4/spotify-desktop-route.md §2, driven through the real reader code
(:class:`yuki.connect.spotify_desktop.UiaTree`) by the shared fake automation (tests/_fake_uia.py). Nothing touches a
window, Spotify, the network or the real database.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from tests._fake_uia import Automation, Node
from yuki.connect import spotify_desktop as SD

HWND = 7101
CHILD = 7102
PLAYLIST_LINK = "https://xpui.app.spotify.com/playlist/{id}"


class FakeAutomation(Automation):
    """The shared fake, plus the true condition the reader's element count uses."""

    def CreateTrueCondition(self) -> tuple:
        return ("true",)


# ---------------------------------------------------------------------------------------------------------------------
# Fixture trees (invented names)
# ---------------------------------------------------------------------------------------------------------------------

LIB = [("Rainy Commute", "Mira Okafor", False), ("Deep Focus Mix", "Spotify", False),
       ("Late Lanterns", "Mira Okafor", True), ("Garden Tapes", "Ivo Brandt", False),
       ("Morning Static", "Mira Okafor", False)]
IDS = {"Rainy Commute": "1A2b3C4d5E6f7G8h9I0jKl", "Late Lanterns": "Zz9Yy8Xx7Ww6Vv5Uu4Tt3S",
       "Deep Focus Mix": "Qq1Ww2Ee3Rr4Tt5Yy6Uu7I", "Not In Library": "Pp0Oo9Ii8Uu7Yy6Tt5Rr4E"}


def _row(name: str, owner: str, playing: bool, *, play: str = "Play {name}", pause: str = "Pause {name}",
         open_fmt: str = "{name} Playlist • {owner}", offscreen: bool = False) -> Node:
    group = Node("Group", name, offscreen=offscreen).add(
        Node("Button", open_fmt.replace("{name}", name).replace("{owner}", owner), invoke=True, offscreen=offscreen),
        Node("Button", (pause if playing else play).replace("{name}", name), invoke=True, offscreen=offscreen))
    return Node("DataItem", "", invoke=True, selected=False, offscreen=offscreen).add(
        Node("DataItem", "", selected=False, offscreen=offscreen).add(group))


def spotify_tree(rows: list[tuple[str, str, bool]] = LIB, *, library: bool = True, home: bool = True,
                 volume: float = 0.42, playing: bool = True, offscreen: bool = False, labels: dict | None = None,
                 extra_slider: bool = False, open_by_row: dict[str, str] | None = None) -> Node:
    """Spotify's window: the top bar, Your Library (DataGrid > DataItem > DataItem > Group), Home (cards with links,
    the shortcut grid's 8 "Play" buttons, "Pause {name}" buttons outside the grid) and the Now playing bar."""
    lab = {"bar": "Now playing bar", "controls": "Player controls", "grid": "Your Library", "play": "Play {name}",
           "pause": "Pause {name}", "open": "{name} Playlist • {owner}", "volume": "Change volume",
           "btn_play": "Play", "btn_pause": "Pause", "prev": "Previous", "next": "Next", **(labels or {})}
    o = offscreen
    window = Node("Window", "Spotify Premium", (0, 0, 1600, 900), offscreen=o)
    doc = Node("Document", "Invented Song • Invented Artist", (0, 0, 1600, 900),
               value="https://xpui.app.spotify.com/index.html", offscreen=o)
    shell = Node("Group", "", offscreen=o)
    shell.add(Node("Button", "Home", invoke=True, offscreen=o), Node("Button", "Search", invoke=True, offscreen=o),
              Node("ComboBox", "What do you want to play?", value="", offscreen=o))
    if library:
        panel = Node("Group", "", offscreen=o).add(
            Node("Button", "Collapse Your Library", invoke=True, offscreen=o),
            Node("Button", "Search in Your Library", invoke=True, offscreen=o))
        grid = Node("DataGrid", lab["grid"], value="", offscreen=o)
        for name, owner, is_playing in rows:
            grid.add(_row(name, owner, is_playing, play=lab["play"], pause=lab["pause"],
                          open_fmt=(open_by_row or {}).get(name, lab["open"]), offscreen=o))
        panel.add(grid)
        shell.add(panel)
    if home:
        main = Node("Group", "", offscreen=o)
        shortcuts = Node("Group", "", offscreen=o)
        for i in range(8):   # Home's shortcut grid: 8 Buttons named exactly "Play" (one "Pause")
            shortcuts.add(Node("Button", "Pause" if i == 3 else "Play", invoke=True, offscreen=o))
        main.add(shortcuts)
        cards = Node("DataGrid", "Jump back in", value="", offscreen=o)
        for name in ("Rainy Commute", "Late Lanterns", "Deep Focus Mix", "Not In Library"):
            item = Node("DataItem", "", offscreen=o).add(Node("ListItem", name, offscreen=o).add(
                Node("Button", f"Pause {name}" if name == "Not In Library" else f"Play {name}", invoke=True,
                     offscreen=o),
                Node("Hyperlink", name, invoke=True, offscreen=o,
                     value=PLAYLIST_LINK.replace("{id}", IDS[name]) + "?uid=abcdef")))
            cards.add(item)
        cards.add(Node("DataItem", "", offscreen=o).add(Node("ListItem", "Rainy Commute", offscreen=o).add(
            Node("Hyperlink", "Rainy Commute", value="https://xpui.app.spotify.com/album/AlbumIdAlbumIdAlbumId",
                 offscreen=o))))
        main.add(cards)
        shell.add(main)
    bar = Node("Group", lab["bar"], offscreen=o)
    controls = Node("Group", lab["controls"], offscreen=o).add(
        Node("Button", lab["prev"], invoke=True, offscreen=o),
        Node("Button", lab["btn_pause"] if playing else lab["btn_play"], invoke=True, offscreen=o),
        Node("Button", lab["next"], invoke=True, offscreen=o),
        Node("Text", "Change progress", offscreen=o).add(
            Node("Slider", "Change progress", range=(73.0, 0.0, 216.0), offscreen=True)))
    bar.add(controls, Node("Button", "Mute", invoke=True, offscreen=o),
            Node("Text", lab["volume"], offscreen=o).add(
                Node("Slider", lab["volume"], range=(volume, 0.0, 1.0), offscreen=True)))
    if extra_slider:
        bar.add(Node("Slider", "Invented equaliser", range=(0.5, 0.0, 1.0), offscreen=o))
    shell.add(bar)
    doc.add(shell)
    window.add(Node("Pane", "", offscreen=o).add(doc))
    return window


def frame_tree() -> Node:
    """What a cold Chromium window answers: its frame only (7 elements)."""
    return Node("Window", "Spotify Premium", (0, 0, 1600, 900)).add(
        Node("TitleBar", ""), Node("Button", "Minimise"), Node("Button", "Maximise"), Node("Button", "Close"),
        Node("Pane", ""), Node("Pane", ""), Node("Document", ""))


def tree_of(root: Node, automation_cls: type = FakeAutomation) -> SD.UiaTree:
    return SD.UiaTree(automation_cls({HWND: root}), HWND)


def read(root: Node, **kw: Any) -> SD.SpotifyRead:
    kw.setdefault("deadline", time.monotonic() + 5.0)
    return SD.read_tree(tree_of(root), **kw)


# ---------------------------------------------------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------------------------------------------------


def test_rows_found_playing_row_marked_owner_parsed_slider_by_label() -> None:
    got = read(spotify_tree(), session_playing=True)
    assert got.status == "ok" and got.built and got.player and got.grid
    assert [r.name for r in got.rows] == [n for n, _o, _p in LIB]
    assert got.playing is not None and got.playing.name == "Late Lanterns"
    assert got.playing.play_button == "Pause Late Lanterns"
    by = {r.name: r for r in got.rows}
    assert by["Deep Focus Mix"].spotify_owned is True
    assert by["Rainy Commute"].spotify_owned is False and by["Rainy Commute"].owner == "Mira Okafor"
    assert by["Rainy Commute"].playing is False and by["Rainy Commute"].play_button == "Play Rainy Commute"
    assert got.slider == 42.0 and got.slider_source == "english"
    assert got.label_source == "english"


def test_a_link_is_a_playlist_uri_only_when_its_name_equals_a_library_row() -> None:
    got = read(spotify_tree())
    # Rainy Commute and Late Lanterns: Library rows owned by a person -> learned
    assert got.links == {"Rainy Commute": f"spotify:playlist:{IDS['Rainy Commute']}",
                         "Late Lanterns": f"spotify:playlist:{IDS['Late Lanterns']}"}
    # not a Library row ("Not In Library"), a Spotify-owned row (digest only), an album link: never


def test_playlist_uri_parses_the_link_template_and_nothing_else() -> None:
    assert SD.playlist_uri(PLAYLIST_LINK.replace("{id}", "AbC123") + "?uid=1&x=2") == "spotify:playlist:AbC123"
    assert SD.playlist_uri("https://xpui.app.spotify.com/album/AbC123") is None
    assert SD.playlist_uri("https://example.com/playlist/AbC123") is None
    assert SD.playlist_uri("https://xpui.app.spotify.com/playlist/") is None
    assert SD.playlist_uri(None) is None


def test_no_library_view_is_no_library_not_an_error() -> None:
    got = read(spotify_tree(library=False, home=False))
    assert got.status == "no_library" and got.built and got.player and not got.grid and got.rows == []
    assert got.slider == 42.0


def test_frame_only_is_not_built_never_no_library() -> None:
    got = read(frame_tree())
    assert got.status == "not_built" and not got.built and got.rows == []
    assert got.elements == 7


class GrowingAutomation(FakeAutomation):
    """A cold Chromium window: the root answers its frame (7 elements) until a child window is asked, then the same
    root's tree grows over about 3 stub seconds (frame -> 24 -> 58 -> the full tree, a step every 0.8 s)."""

    def __init__(self, clock: list[float], full: Node, *, grows: bool = True) -> None:
        self.clock = clock
        self.grows = grows
        self.asked_at: float | None = None
        self.stages = [frame_tree(), self._partial(17), self._partial(51), full]
        self.root_node = Node("Window", "Spotify Premium", (0, 0, 1600, 900))
        super().__init__({HWND: self.root_node, CHILD: Node("Pane", "renderer")})
        self._refresh()

    @staticmethod
    def _partial(extra: int) -> Node:
        root = frame_tree()
        holder = Node("Group", "")
        for i in range(extra - 1):
            holder.add(Node("Text", f"building {i}"))
        root.children[-1].add(holder)
        return root

    def _refresh(self) -> None:
        if self.asked_at is None or not self.grows:
            stage = self.stages[0]
        else:
            stage = self.stages[min(int((self.clock[0] - self.asked_at) / 0.8), 3)]
        self.root_node.children = stage.children
        for node in self.root_node.walk():
            for child in node.children:
                self.parents[id(child)] = node

    def ElementFromHandle(self, hwnd: int) -> Any:
        if hwnd == CHILD:
            if self.asked_at is None:
                self.asked_at = self.clock[0]
            return super().ElementFromHandle(CHILD)
        live = super().ElementFromHandle(HWND)
        find, refresh = live.FindAllBuildCache, self._refresh

        def find_now(scope: int, condition: Any, request: Any) -> Any:
            refresh()   # the provider's tree as it is at this instant
            return find(scope, condition, request)

        live.FindAllBuildCache = find_now
        return live


def _stub_time() -> tuple[list[float], Any, Any]:
    t = [1000.0]

    def clock() -> float:
        return t[0]

    def sleep(dt: float) -> None:
        t[0] += dt

    return t, clock, sleep


def test_frame_only_window_reads_none_under_budget_and_full_after_warm() -> None:
    t, clock, sleep = _stub_time()
    auto = GrowingAutomation(t, spotify_tree())
    cold = SD.read_tree(SD.UiaTree(auto, HWND), deadline=time.monotonic() + 0.3)
    assert cold.status == "not_built" and cold.elements == 7
    warm = SD.warm_tree(SD.UiaTree(auto, HWND), [CHILD], budget_s=3.0, clock=clock, sleep=sleep)
    assert warm.cold and warm.built and warm.asked == 1 and warm.before == 7 and warm.after > 58
    assert clock() - 1000.0 <= 3.0 + 1e-6
    full = SD.read_tree(SD.UiaTree(auto, HWND), deadline=time.monotonic() + 5.0, session_playing=True)
    assert full.status == "ok" and full.playing is not None and len(full.rows) == len(LIB)


def test_warm_stops_at_its_budget_when_the_tree_never_builds() -> None:
    t, clock, sleep = _stub_time()
    auto = GrowingAutomation(t, spotify_tree(), grows=False)
    warm = SD.warm_tree(SD.UiaTree(auto, HWND), [CHILD], budget_s=3.0, clock=clock, sleep=sleep)
    assert not warm.built and warm.cold
    assert 2.99 <= clock() - 1000.0 <= 3.0 + 1e-6


def test_warm_of_a_built_window_is_a_noop() -> None:
    t, clock, sleep = _stub_time()
    warm = SD.warm_tree(tree_of(spotify_tree()), [CHILD], budget_s=3.0, clock=clock, sleep=sleep)
    assert warm.built and not warm.cold and warm.asked == 0 and warm.polls == 0 and clock() == 1000.0


def test_home_play_buttons_and_pause_buttons_outside_the_grid_are_never_matched() -> None:
    root = spotify_tree()
    got = read(root, session_playing=True)
    names = [r.name for r in got.rows]
    assert "Not In Library" not in names                      # its "Pause {name}" button sits on a Home card
    assert sum(1 for r in got.rows if r.playing) == 1         # only the grid's own playing row
    # the shortcut grid's eight "Play" buttons are outside "Now playing bar" > "Player controls"
    controls = SD._container(tree_of(root), SD.all_labels()[0].player_controls)
    assert len(controls) == 1
    assert [b.name for b in SD.local_find(controls[0], "Button")] == ["Previous", "Pause", "Next"]


def test_offscreen_elements_are_still_searched() -> None:
    got = read(spotify_tree(offscreen=True), session_playing=True)
    assert got.status == "ok" and len(got.rows) == len(LIB) and got.slider == 42.0
    assert got.playing is not None


GERMANISH = {"bar": "Wiedergabeleiste", "controls": "Steuerung", "grid": "Deine Sammlung",
             "play": "{name} abspielen", "pause": "{name} anhalten", "open": "{name} Liste • {owner}",
             "volume": "Lautstaerke aendern", "btn_play": "Abspielen", "btn_pause": "Anhalten", "prev": "Zurueck",
             "next": "Weiter"}


def test_non_english_slider_by_shape_wording_candidate_then_confirmed_by_a_row_flip() -> None:
    root = spotify_tree(labels=GERMANISH)
    before = read(root, session_playing=False)
    assert before.status == "ok" and before.built and not before.player
    assert before.slider == 42.0 and before.slider_source == "shape"
    assert before.grid and len(before.rows) == len(LIB)
    assert before.label_source == "none" and all(r.playing is None for r in before.rows)   # no playlist route yet
    assert before.candidate is None
    first = read(root, session_playing=True)
    cand = first.candidate
    assert cand is not None and (cand.play, cand.pause, cand.grid) == ("{name} abspielen", "{name} anhalten",
                                                                       "Deine Sammlung")
    # a candidate is provisional: no play state from it
    assert first.label_source == "none" and first.playing is None and all(r.playing is None for r in first.rows)
    assert cand.rows["Late Lanterns"] == "pause" and cand.rows["Rainy Commute"] == "play"
    assert SD.judge_wording(first.raw, cand.play, cand.pause, cand.rows) is None      # the same read: no flip
    # another playlist starts: Late Lanterns flips back to "abspielen" -> confirmed
    other = [(n, o, n == "Rainy Commute") for n, o, _p in LIB]
    second = read(spotify_tree(other, labels=GERMANISH), session_playing=True)
    assert SD.judge_wording(second.raw, cand.play, cand.pause, cand.rows) == "confirm"
    SD.apply_wording(second, cand.play, cand.pause)
    assert second.label_source == "learned" and second.playing is not None and second.playing.name == "Rainy Commute"
    learned = {"play": cand.play, "pause": cand.pause, "grid": cand.grid}
    after = read(root, session_playing=False, learned=learned)
    assert after.label_source == "learned" and after.playing is not None and after.playing.name == "Late Lanterns"
    assert after.candidate is None
    assert all(r.spotify_owned is None for r in after.rows)   # an unknown row format: owner never parsed


#: Review r1 M1: the playing item is not a Library row in view, and exactly one row differs at the open button.
MIRA_IVO = [("Rainy Commute", "Mira Okafor", False), ("Garden Tapes", "Mira Okafor", False),
            ("Morning Static", "Ivo Brandt", False)]


def test_an_owner_button_candidate_is_never_confirmed() -> None:
    root = spotify_tree(MIRA_IVO, labels=GERMANISH)
    got = read(root, session_playing=True)
    cand = got.candidate
    assert cand is not None and "Mira Okafor" in cand.play          # the owner position fits this one read...
    assert got.playing is None and all(r.playing is None for r in got.rows)   # ...but gives no play state
    # every later read: paused, the same album still playing, another playlist out of view, scrolled, renamed
    for rows in (MIRA_IVO, [(n, o, False) for n, o, _p in MIRA_IVO] + [("New Mix", "Ivo Brandt", False)],
                 MIRA_IVO[:2] + [("Night Bus", "Ivo Brandt", False)], MIRA_IVO[1:]):
        for playing in (True, False):
            later = read(spotify_tree(rows, labels=GERMANISH), session_playing=playing)
            assert SD.judge_wording(later.raw, cand.play, cand.pause, cand.rows) != "confirm"
    # two rows by Ivo in view: two "playing" rows at once -> contradicted
    two = read(spotify_tree(MIRA_IVO + [("Night Bus", "Ivo Brandt", False)], labels=GERMANISH))
    assert SD.judge_wording(two.raw, cand.play, cand.pause, cand.rows) == "contradict"


def test_a_pinned_row_candidate_is_contradicted_when_another_row_is_pinned() -> None:
    pinned = {"Garden Tapes": "{name} Angeheftete Liste • {owner}"}
    rows = [("Rainy Commute", "Mira Okafor", False), ("Garden Tapes", "Mira Okafor", False),
            ("Morning Static", "Mira Okafor", False)]
    got = read(spotify_tree(rows, labels=GERMANISH, open_by_row=pinned), session_playing=True)
    cand = got.candidate
    assert cand is not None and "Angeheftete" in cand.pause and got.playing is None
    both = dict(pinned, **{"Morning Static": "{name} Angeheftete Liste • {owner}"})
    later = read(spotify_tree(rows, labels=GERMANISH, open_by_row=both), session_playing=True)
    assert SD.judge_wording(later.raw, cand.play, cand.pause, cand.rows) == "contradict"


def test_shape_fallback_refuses_two_candidate_sliders() -> None:
    got = read(spotify_tree(labels=GERMANISH, extra_slider=True))
    assert got.slider is None


def test_learn_row_wording_needs_exactly_one_odd_row_at_exactly_one_position() -> None:
    rows = [("A1", ["A1 x • me", "go A1"]), ("B2", ["B2 x • me", "go B2"]), ("C3", ["C3 x • me", "stop C3"])]
    assert SD.learn_row_wording(rows) == ("go {name}", "stop {name}")
    # one row with another owner too: two positions fit -> nothing learned (never a guess)
    rows2 = [("A1", ["A1 x • me", "go A1"]), ("B2", ["B2 x • me", "go B2"]), ("C3", ["C3 x • you", "stop C3"])]
    assert SD.learn_row_wording(rows2) is None
    # nothing playing: no odd row
    assert SD.learn_row_wording([(n, [f"{n} x • me", f"go {n}"]) for n in ("A1", "B2", "C3")]) is None
    # too few rows to call the rest "the rest"
    assert SD.learn_row_wording([("A1", ["go A1"]), ("B2", ["stop B2"])]) is None


def test_judge_wording_flips_and_contradictions() -> None:
    seen = {"A1": "play", "B2": "play", "C3": "pause"}
    rows = [("A1", ["go A1"]), ("B2", ["go B2"]), ("C3", ["stop C3"])]
    assert SD.judge_wording(rows, "go {name}", "stop {name}", seen) is None
    assert SD.judge_wording([("A1", ["stop A1"]), ("C3", ["go C3"])], "go {name}", "stop {name}", seen) == "confirm"
    assert SD.judge_wording([("A1", ["stop A1"]), ("D4", ["go D4"])], "go {name}", "stop {name}", seen) == "confirm"
    assert SD.judge_wording([("D4", ["stop D4"]), ("E5", ["go E5"])], "go {name}", "stop {name}", seen) is None
    assert SD.judge_wording([("A1", ["stop A1"]), ("C3", ["stop C3"])], "go {name}", "stop {name}",
                            seen) == "contradict"


class SlowAutomation(FakeAutomation):
    """Every FindAll takes 0.12 s (a slow provider)."""

    def ElementFromHandle(self, hwnd: int) -> Any:
        live = super().ElementFromHandle(hwnd)
        orig = live.FindAllBuildCache

        def slow(scope: int, condition: Any, request: Any) -> Any:
            time.sleep(0.12)
            return orig(scope, condition, request)

        live.FindAllBuildCache = slow
        return live


def test_the_read_budget_holds_on_a_slow_provider() -> None:
    started = time.monotonic()
    got = SD.read_tree(SD.UiaTree(SlowAutomation({HWND: spotify_tree()}), HWND),
                       deadline=time.monotonic() + SD.READ_BUDGET_S)
    elapsed = time.monotonic() - started
    assert got.status == "timeout"
    assert elapsed < SD.READ_BUDGET_S + 0.2


class BlockingAutomation(FakeAutomation):
    def ElementFromHandle(self, hwnd: int) -> Any:
        time.sleep(2.0)   # a provider that blocks
        return super().ElementFromHandle(hwnd)


def test_a_blocked_provider_costs_the_budget_never_the_caller() -> None:
    started = time.monotonic()
    got = SD.read(HWND, 0.3, automation_fn=lambda: BlockingAutomation({HWND: spotify_tree()}))
    assert got.status == "timeout"
    assert time.monotonic() - started < 0.6


def test_live_read_wrapper_on_its_own_thread() -> None:
    got = SD.read(HWND, 1.0, automation_fn=lambda: FakeAutomation({HWND: spotify_tree()}), session_playing=True)
    assert got.status == "ok" and got.playing is not None
    rows = SD.library(HWND, 1.0, automation_fn=lambda: FakeAutomation({HWND: spotify_tree()}))
    assert rows is not None and len(rows) == len(LIB)
    assert SD.slider(HWND, 1.0, automation_fn=lambda: FakeAutomation({HWND: spotify_tree(volume=0.7)})) == 70.0
    assert SD.slider(HWND, 1.0, automation_fn=lambda: FakeAutomation({HWND: frame_tree()})) is None


def test_reads_never_use_element_from_point_or_set_anything() -> None:
    """The reader is read-only: no call or attribute of a pattern that acts, takes focus or sends input (code, not
    its docstrings)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(SD))
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    used |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    for banned in ("ElementFromPoint", "SetFocus", "SetValue", "Invoke", "Toggle", "Select", "SendInput",
                   "SetForegroundWindow", "ShowWindow", "startfile", "ShellExecute", "SetCursorPos"):
        assert banned not in used, banned
    assert "subprocess" not in imported and "yuki.actions" not in imported


# ---------------------------------------------------------------------------------------------------------------------
# Window states (stub geometry) and the media session
# ---------------------------------------------------------------------------------------------------------------------


def test_window_states_from_geometry() -> None:
    from yuki.perception.visibility import visible_shares

    monitors = [(0, 0, 1920, 1080), (1920, 0, 3840, 1080)]
    # fully covered by a maximised window on the same screen
    covered = visible_shares([(0, 0, 1920, 1080), (200, 100, 1400, 900)], monitors)[1]
    assert SD.classify_state(minimised=False, front=False, share=covered) == SD.COVERED
    # maximised on the other screen, about 1% overlapped by a window above it
    other = visible_shares([(3820, 0, 3840, 1080), (1920, 0, 3840, 1080)], monitors)[1]
    assert other > 0.95
    assert SD.classify_state(minimised=False, front=False, share=other) == SD.VISIBLE
    assert SD.classify_state(minimised=False, front=True, share=1.0) == SD.FRONT
    assert SD.classify_state(minimised=True, front=False, share=None) == SD.MINIMISED
    assert SD.classify_state(minimised=False, front=False, share=None) == SD.COVERED   # off every monitor


def test_spotify_session_for_the_store_and_the_classic_app_ids() -> None:
    store_id = "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"
    assert SD.is_spotify_session(store_id)
    assert SD.is_spotify_session("Spotify.exe")
    assert SD.is_spotify_session("C:\\Users\\x\\AppData\\Roaming\\Spotify\\Spotify.exe")
    assert not SD.is_spotify_session("Chrome")
    assert not SD.is_spotify_session("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Other")
    assert not SD.is_spotify_session("308046B0AF4A39CB")   # another app's id
    # matched to the Spotify window's own process
    assert SD.is_spotify_session(store_id, aumid=store_id)
    assert not SD.is_spotify_session("Spotify.exe", aumid=store_id)
    assert SD.is_spotify_session(store_id, family="SpotifyAB.SpotifyMusic_zpdnekdrzrea0")
    assert SD.is_spotify_session("Spotify.exe", image="Spotify.exe")


def test_spotify_session_picks_the_window_process_session(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = [{"app_id": "Chrome", "status": "playing"},
                {"app_id": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", "status": "paused"}]
    monkeypatch.setattr(SD, "process_identity", lambda pid: {
        "aumid": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", "family": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0",
        "image": "Spotify.exe"})
    assert SD.spotify_session(sessions, pid=4242)["status"] == "paused"
    monkeypatch.setattr(SD, "process_identity", lambda pid: {"aumid": "", "family": "", "image": "Spotify.exe"})
    assert SD.spotify_session(sessions + [{"app_id": "Spotify.exe", "status": "playing"}], pid=4242)["status"] \
        == "playing"
    assert SD.spotify_session([{"app_id": "Chrome"}]) is None


def test_labels_are_data_with_containers() -> None:
    en = SD.all_labels()[0]
    assert en.lang == "en"
    assert en.player_controls[0] == SD.Step("Group", "Now playing bar")
    assert en.library_grid == (SD.Step("DataGrid", "Your Library"),)
    assert en.controls["volume"] == ("Slider", "Change volume", "now_playing_bar")
    assert en.row_play == "Play {name}" and en.row_pause == "Pause {name}"
    assert SD.process_image() == "Spotify.exe" and SD.app_key() == "spotify"
