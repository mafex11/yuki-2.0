"""fLive review round 2: the seams with fMoments' MomentHub, the "unrecognised" Spotify window, the re-warm of a play
start, the child-window ask, and the small Meet-tab, tabs and doctor fixes.

Offline: the hub here is a stand-in with the **same signatures and fan-out** as ``yuki.memory.moments.MomentHub`` on
the fMoments branch (3ef6f9c): ``subscribe_media`` subscribers get ``fn(started, stopped, at=at, **facts)``,
``subscribe_front`` subscribers ``fn(prev_key, new_key, at=at, hwnd=hwnd, prev_hwnd=prev_hwnd, **facts)``, each
wrapped in ``try/except: errors += 1``. Fake UI Automation trees with invented names; no window, no Spotify, no DB.
"""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path
from typing import Any

import pytest

from tests._fake_uia import Node
from tests.test_flive_spotify_reader import (CHILD, GERMANISH, HWND, FakeAutomation, GrowingAutomation, _stub_time,
                                             spotify_tree)
from tests.test_flive_spotify_watch import URI_A, Reader, Store, make
from yuki.connect import spotify_desktop as SD
from yuki.connect import templates
from yuki.memory import volumewatch as VW


@pytest.fixture(autouse=True)
def _classic_spotify_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SD, "process_identity", lambda pid: {"aumid": "", "family": "", "image": "Spotify.exe"})
    monkeypatch.setattr(SD, "is_spotify_pid", lambda pid: pid == 4242)


class Hub:
    """MomentHub's live-input surface as published by fMoments (signatures copied; bodies only record)."""

    def __init__(self) -> None:
        self._front_subs: list[Any] = []
        self._media_subs: list[Any] = []
        self.errors = 0
        self.calls: list[tuple[str, tuple, dict]] = []

    def subscribe_front(self, fn: Any) -> None:
        self._front_subs.append(fn)

    def subscribe_media(self, fn: Any) -> None:
        self._media_subs.append(fn)

    def on_media_change(self, started: Any, stopped: Any, *, at: float | None = None, by_yuki: bool = False,
                        request_id: str | None = None, **facts: Any) -> None:
        at = time.time() if at is None else float(at)
        for fn in list(self._media_subs):
            try:
                fn(started, stopped, at=at, **facts)
            except Exception:  # noqa: BLE001
                self.errors += 1

    def on_front_change(self, prev_key: str, new_key: str, *, at: float | None = None, hwnd: int = 0,
                        prev_hwnd: int = 0, **facts: Any) -> None:
        at = time.time() if at is None else float(at)
        for fn in list(self._front_subs):
            try:
                fn(prev_key, new_key, at=at, hwnd=hwnd, prev_hwnd=prev_hwnd, **facts)
            except Exception:  # noqa: BLE001
                self.errors += 1

    def volume_change(self, app: str | None, value_from: float | None, value: float, *, means: str,
                      at: float | None = None, front: str | None = None, yuki_context: bool = False,
                      since: float | None = None, **facts: Any) -> None:
        self.calls.append(("volume_change", (app, value_from, value),
                           {"means": means, "at": at, "front": front, "yuki_context": yuki_context, "since": since,
                            **facts}))

    def media_context(self, app: str | None, name: str, *, at: float | None = None, link: str | None = None,
                      keep_name: bool = True, since: float | None = None, **facts: Any) -> None:
        self.calls.append(("media_context", (app, name), {"at": at, "link": link, "keep_name": keep_name,
                                                          "since": since, **facts}))


# ---------------------------------------------------------------------------------------------------------------------
# M1(a): the Spotify reader's triggers take the hub's call as the hub makes it
# ---------------------------------------------------------------------------------------------------------------------


def test_spotify_triggers_accept_the_hubs_fan_out_exactly() -> None:
    hub = Hub()
    watch, reader, store, _moments, logs, clock = make()
    watch._emit = SD.hub_emit(hub)
    hub.subscribe_media(watch.on_media_change)
    hub.subscribe_front(watch.on_front_change)
    store.set_nudge_state(SD.LINKS_KEY, store.cipher.encrypt(json.dumps(
        {store.cipher.digest("spotify_playlist", "Late Lanterns"): {"name": "Late Lanterns",
                                                                    "uri": "spotify:playlist:Zz9Yy8Xx7Ww6Vv5Uu4Tt3S",
                                                                    "n": 1}})))
    started_at = clock.wall - 2.0
    hub.on_media_change([{"app_id": "Spotify.exe", "app": "spotify", "title": "Invented Song"}], [],
                        at=started_at, by_yuki=False, request_id=None, initial=False)
    assert hub.errors == 0
    assert reader.reads and reader.reads[-1]["session_playing"] is True     # the play-start read ran
    ctx = [c for c in hub.calls if c[0] == "media_context"]
    assert len(ctx) == 1
    _kind, (app, name), kw = ctx[0]
    assert app == "spotify" and name == "Late Lanterns" and kw["keep_name"] is True
    assert kw["link"] == "spotify:playlist:Zz9Yy8Xx7Ww6Vv5Uu4Tt3S"            # the URI itself, for the hub to encrypt
    assert kw["since"] == started_at and kw["link_known"] is True and kw["owner"] == "other"
    # Spotify leaves the front: the slider read; a moved slider is an in-app volume change since the last reading
    reader.result.slider = 25.0
    clock.advance(30)
    hub.on_front_change("spotify", "arc", at=clock.wall, hwnd=11, prev_hwnd=9001, by_yuki=False, request_id=None)
    assert hub.errors == 0
    vol = [c for c in hub.calls if c[0] == "volume_change"]
    assert len(vol) == 1
    _kind, (app, value_from, value), kw = vol[0]
    assert (app, value_from, value) == ("spotify", 40.0, 25.0) and kw["means"] == "in_app"
    assert kw["since"] is not None and kw["since"] < clock.wall and "prev_read_at" not in kw
    # the hub receives no name, link or title among its facts
    facts = json.dumps([c[2] for c in hub.calls if c[0] == "volume_change"])
    assert "Lanterns" not in facts and "Invented" not in json.dumps(logs)


def test_an_initial_media_read_is_noted_on_the_playlist_moment() -> None:
    hub = Hub()
    watch, _reader, _store, moments, _logs, _clock = make()
    hub.subscribe_media(watch.on_media_change)
    hub.on_media_change([{"app_id": "Spotify.exe", "app": "spotify", "title": "x"}], [], initial=True)
    ctx = [m for m in moments if m["kind"] == "media_context"]
    assert ctx and ctx[0]["facts"]["initial"] is True and ctx[0]["uri"] is None   # no link learned for it yet


def test_hub_emit_fits_the_hubs_signatures() -> None:
    hub = Hub()
    emit = SD.hub_emit(hub)
    emit({"kind": "media_context", "at": 10.0, "app": "spotify", "entity": "Invented List", "entity_keep": False,
          "uri": None, "since": 8.0, "facts": {"owner": "spotify", "link_known": False, "window_state": "front",
                                               "label_source": "english", "link": True, "at": 1}})
    emit({"kind": "volume_change", "at": 12.0, "app": "spotify", "value": 30.0, "value_from": 50.0,
          "means": "in_app", "since": 9.0, "facts": {"window_state": None, "slider_source": "english",
                                                     "trigger": "leave_front", "prev_read_at": 9.0}})
    (k1, a1, kw1), (k2, a2, kw2) = hub.calls
    assert k1 == "media_context" and a1 == ("spotify", "Invented List") and kw1["keep_name"] is False
    assert kw1["link"] is None and kw1["at"] == 10.0 and kw1["since"] == 8.0   # a colliding fact never overrides
    assert k2 == "volume_change" and a2 == ("spotify", 50.0, 30.0) and kw2["means"] == "in_app"
    assert kw2["since"] == 9.0 and "window_state" not in kw2 and "prev_read_at" not in kw2
    # both bind to the hub's published signatures
    sig = inspect.signature(Hub.media_context)
    sig.bind(hub, "spotify", "x", at=1.0, link=None, keep_name=True, since=None, owner="other")


def test_volumewatch_hub_on_change_marks_yukis_own_sets() -> None:
    hub = Hub()
    on_change = VW.hub_on_change(hub)
    on_change({"kind": "volume_change", "at": 5.0, "app": "master", "value": 30.0, "value_from": 60.0,
               "means": "master", "by": "yuki", "source": "volume", "evidence": "endpoint_event",
               "facts": {"front": "valorant", "muted": False}})
    on_change({"kind": "volume_change", "at": 6.0, "app": "spotify", "value": 20.0, "value_from": 40.0,
               "means": "session", "by": "user", "facts": {"front": None, "muted": None}})
    (_k1, a1, kw1), (_k2, a2, kw2) = hub.calls
    assert a1 == ("master", 60.0, 30.0) and kw1["means"] == "master" and kw1["yuki_context"] is True
    assert kw1["front"] == "valorant" and kw1["muted"] is False
    assert a2 == ("spotify", 40.0, 20.0) and kw2["means"] == "session" and kw2["yuki_context"] is False
    assert "muted" not in kw2


# ---------------------------------------------------------------------------------------------------------------------
# M1(b): page_entity, the moments extractor's hook, on the one template file
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("host", "path", "want"), [
    ("www.youtube.com", "/watch?v=dQw4w9WgXcQ", ("youtube.watch", "dQw4w9WgXcQ")),
    ("youtube.com", "/watch?v=abc123&t=42s", ("youtube.watch", "abc123")),
    ("m.youtube.com", "/watch?list=PLx&v=xyz#t=1", ("youtube.watch", "xyz")),
    ("WWW.YouTube.com", "/shorts/Short1", ("youtube.shorts", "Short1")),
    ("www.youtube.com", "/@invented_channel", ("youtube.handle", "invented_channel")),
    ("www.youtube.com", "/channel/UCabcdef", ("youtube.channel", "UCabcdef")),
    ("www.youtube.com", "/", ("youtube.home", "home")),
    ("www.youtube.com", "", ("youtube.home", "home")),
    ("www.youtube.com", "/watch", None),
    ("www.youtube.com", "/results?search_query=invented", None),
    ("www.youtube.com", "/@", None),
    ("www.example.com", "/watch?v=abc", None),
    ("", "/watch?v=abc", None),
    ("www.youtube.com/evil", "/watch?v=abc", None),
])
def test_page_entity_reads_youtube_pages_from_the_template_file(host: str, path: str, want: Any) -> None:
    assert templates.page_entity(host, path) == want


def test_page_entity_fits_the_moments_extractor() -> None:
    """As fMoments' ``Extractor._pages`` uses it: ``tid, value = template``; the service is the part before the dot;
    the entity is ``str(value)`` (never ``"None"``)."""
    for path in ("/watch?v=abc", "/", "/@someone"):
        tid, value = templates.page_entity("www.youtube.com", path)
        assert tid.split(".", 1)[0] == "youtube" and isinstance(value, str) and value and value != "None"
    assert "page_entity" in templates.__all__


# ---------------------------------------------------------------------------------------------------------------------
# L1: a drawn window whose controls the reader does not know is "unrecognised", and its warm-up ends early
# ---------------------------------------------------------------------------------------------------------------------

UNKNOWN = {**GERMANISH, "volume": "Lautstaerke"}


def test_a_drawn_unrecognised_window_ends_its_warm_up_early_and_reads_unrecognised() -> None:
    root = spotify_tree(labels=UNKNOWN, extra_slider=True)   # no known labels, two 0-1 sliders
    auto = FakeAutomation({HWND: root, CHILD: Node("Pane", "renderer")})
    t, clock, sleep = _stub_time()
    warm = SD.warm_tree(SD.UiaTree(auto, HWND), [CHILD], budget_s=3.0, clock=clock, sleep=sleep)
    assert not warm.built and warm.drawn and warm.status == "unrecognised"
    assert warm.polls == SD.WARM_DRAWN_STABLE_POLLS and clock() - 1000.0 < 1.0      # not the whole 3 s
    got = SD.read_tree(SD.UiaTree(auto, HWND), deadline=time.monotonic() + 5.0)
    assert got.status == "unrecognised" and got.elements >= SD.DRAWN_MIN_ELEMENTS and not got.built


def test_a_tree_still_building_is_not_cut_short_as_unrecognised() -> None:
    t, clock, sleep = _stub_time()
    auto = GrowingAutomation(t, spotify_tree())   # 7 -> 24 -> 58 -> full, a step every 0.8 s
    warm = SD.warm_tree(SD.UiaTree(auto, HWND), [CHILD], budget_s=3.0, clock=clock, sleep=sleep)
    assert warm.built and not warm.drawn and warm.status == "ok"
    frame = GrowingAutomation(t, spotify_tree(), grows=False)   # the frame alone stays below the drawn size
    t[0] = 1000.0
    cold = SD.warm_tree(SD.UiaTree(frame, HWND), [CHILD], budget_s=3.0, clock=clock, sleep=sleep)
    assert not cold.built and not cold.drawn and cold.status == "not_built" and 2.99 <= clock() - 1000.0


def test_the_watcher_does_not_warm_an_unrecognised_window_again() -> None:
    reader = Reader()
    reader.warm = lambda hwnd, budget_s, **kw: (reader.warms.append(budget_s),  # type: ignore[method-assign]
                                                SD.WarmResult(before=76, after=76, cold=True, drawn=True, polls=6,
                                                              ms=900.0))[1]
    reader.result = SD.SpotifyRead(status="unrecognised", elements=76)
    watch, _r, store, moments, logs, _c = make(reader=reader)
    watch.on_media_change(started=["Spotify.exe"])
    watch.on_media_change(started=["Spotify.exe"])
    watch.on_front_change("spotify", "arc")
    assert len(reader.warms) == 1 and moments == []
    warm_log = [f for t, f in logs if t == "spotify_read" and f.get("trigger") == "warm"][0]
    assert warm_log["status"] == "unrecognised" and warm_log["drawn"] is True
    st = json.loads(store.state[SD.STATUS_KEY])
    assert st["last_read"]["status"] == "unrecognised"


def test_doctor_words_an_unrecognised_window(tmp_path: Path) -> None:
    from tests.test_flive_misc import _db
    from yuki.core import doctor
    from yuki.memory.store import Store as RealStore

    path = _db(tmp_path)
    s = RealStore.open(path)
    s.set_nudge_state("spotify_desktop", json.dumps({"found_at": time.time() - 60, "last_read": {
        "at": time.time() - 30, "status": "unrecognised", "window_state": "front", "rows": 0, "slider": False}}))
    s.close()
    c = doctor.check_spotify_desktop(path)
    assert c.status == doctor.WARN and "not recognised" in c.detail and "not built" not in c.detail
    assert "labels" in c.fix


# ---------------------------------------------------------------------------------------------------------------------
# L4: a play start on a window that went cold is warmed and read again; a skipped warm-up logs cold=None
# ---------------------------------------------------------------------------------------------------------------------


def test_a_play_start_on_a_window_that_went_cold_warms_and_reads_again() -> None:
    watch, reader, _store, moments, logs, clock = make()
    watch.on_front_change("arc", "spotify")                  # warmed once: marked warm
    assert len(reader.warms) == 1
    full = reader.result
    answers = [SD.SpotifyRead(status="not_built", elements=7), full]
    reader.read = lambda hwnd, budget_s, **kw: (reader.reads.append(kw), answers.pop(0))[1]  # type: ignore
    clock.advance(5)
    watch.on_media_change(started=["Spotify.exe"])
    assert len(reader.warms) == 2 and answers == []          # warmed again, read again
    ctx = [m for m in moments if m["kind"] == "media_context"]
    assert len(ctx) == 1 and ctx[0]["entity"] == "Late Lanterns"
    read_log = [f for t, f in logs if t == "spotify_read" and f.get("trigger") == "play_start"][-1]
    assert read_log["warm"] == "rewarmed" and read_log["status"] == "ok" and read_log["cold"] is True
    assert watch.stats()["rewarms"] == 1


def test_a_read_after_a_recent_failed_warm_up_logs_cold_unknown() -> None:
    watch, reader, _store, _moments, logs, clock = make()
    reader.warm_built = False
    reader.result = SD.SpotifyRead(status="not_built", elements=7)
    watch.on_media_change(started=["Spotify.exe"])           # warm-up ran and failed
    clock.advance(30)
    watch.on_media_change(started=["Spotify.exe"])           # within WARM_RETRY_S: no warm-up, no retry
    assert len(reader.warms) == 1
    reads = [f for t, f in logs if t == "spotify_read" and f.get("trigger") == "play_start"]
    assert reads[0]["warm"] == "ran" and reads[0]["cold"] is True
    assert reads[1]["warm"] == "skipped" and reads[1]["cold"] is None


def test_a_full_read_marks_the_window_warm() -> None:
    watch, reader, _store, _moments, _logs, clock = make()
    reader.warm_built = False
    watch.on_media_change(started=["Spotify.exe"])           # the warm-up did not build, yet the read answered in full
    clock.advance(30)
    watch.on_front_change("spotify", "arc")
    watch.on_front_change("arc", "spotify")                  # warm now: no warm-up
    assert len(reader.warms) == 1 and watch.stats()["warm"] is True


# ---------------------------------------------------------------------------------------------------------------------
# L5: the child windows are asked for content, as perception/tree.py does
# ---------------------------------------------------------------------------------------------------------------------


def test_ask_children_reads_from_each_child_window() -> None:
    asked: list[int] = []

    class Recording(FakeAutomation):
        def ElementFromHandle(self, hwnd: int) -> Any:
            live = super().ElementFromHandle(hwnd)
            if hwnd != HWND:
                first = live.FindFirstBuildCache

                def find_first(scope: int, condition: Any, request: Any) -> Any:
                    asked.append(hwnd)
                    return first(scope, condition, request)

                live.FindFirstBuildCache = find_first
            return live

    auto = Recording({HWND: spotify_tree(), CHILD: Node("Pane", "renderer").add(Node("Document", "")),
                      CHILD + 1: Node("Pane", "")})
    tree = SD.UiaTree(auto, HWND)
    assert tree.ask_children([CHILD, CHILD + 1, 999999]) == 2   # a child that went away is skipped
    assert asked == [CHILD, CHILD + 1]


# ---------------------------------------------------------------------------------------------------------------------
# L2, L3: the Meet tab's log says how many tabs were selected; a calling site without a product name has no service
# ---------------------------------------------------------------------------------------------------------------------


def test_tab_strip_log_carries_how_many_tabs_report_selected() -> None:
    from tests.test_flive_meet_tab import ENTRIES, _sites
    from tests.test_flive_meet_tab import Store as TabStore
    from yuki.memory.callevidence import CallEvidence, SiteTabs

    store, logs = TabStore(), []
    ev = CallEvidence(sites_fn=_sites([]), entries=ENTRIES, state=store)
    ev._judge_entries()
    tabs = SiteTabs(ev, store=store, log=lambda t, **f: logs.append((t, f)),
                    reader=lambda hwnd, *, digest_fn: {"sighting": None, "tabs": 14, "selected_tabs": 2,
                                                       "groups": [3, 11], "nodes": 40, "complete": True})
    tabs.seen({"process": "zen", "hwnd": 3, "at": 1.0, "host": "meet.google.com", "blocked": "blocked_host"})
    log = [f for t, f in logs if t == "tab_strip"][0]
    assert log["tabs"] == 14 and log["selected_tabs"] == 2 and log["tab_known"] is False and log["reason"] is None


def test_a_calling_site_without_a_product_name_has_no_service(tmp_path: Path) -> None:
    from tests.test_call_meeting_sites import privacy_file
    from yuki.memory.callevidence import CallEvidence

    ev = CallEvidence(sites_fn=lambda e: {"zoom.us": (True, ""), "meet.google.com": (True, "Google Meet"),
                                          "whereby.com": (True, "whereby.com")},
                      entries=("zoom.us", "meet.google.com", "whereby.com", "examplebank.com"))
    ev._judge_entries()
    assert ev.calling_service("us02web.zoom.us") is None                 # never "zoom.us" in a name's place
    assert ev.calling_site("us02web.zoom.us") == (True, None)
    assert ev.calling_service("whereby.com") is None                     # a "name" that is the entry itself
    assert ev.calling_service("meet.google.com") == "Google Meet"
    assert ev.calling_site("netbanking.examplebank.com") == (False, None)
    # what the moments hub is given: the product name, else the host itself (which it stores as no service)
    assert ev.site_front_service("meet.google.com") == "Google Meet"
    assert ev.site_front_service("US02web.zoom.us ") == "us02web.zoom.us"
    assert ev.site_front_service("netbanking.examplebank.com") is None
    # the user's always_sites: their name, else no product name (never the site)
    privacy = privacy_file(tmp_path, '[meetings]\nalways_sites = ["meet.example.org", "rooms.example.net"]\n'
                                     '[meetings.names]\n"meet.example.org" = "Example Meet"\n')
    ev2 = CallEvidence(sites_fn=pytest.fail, privacy=privacy, entries=())
    assert ev2.calling_service("a.meet.example.org") == "Example Meet"
    assert ev2.calling_site("x.rooms.example.net") == (True, None)
    assert ev2.site_front_service("x.rooms.example.net") == "x.rooms.example.net"


# ---------------------------------------------------------------------------------------------------------------------
# L6, L7: a gone window still reads "could not read"; the doctor reads the privacy file the service reads
# ---------------------------------------------------------------------------------------------------------------------


def test_walk_tabs_raises_for_a_gone_window_unless_diagnostics_are_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_flive_meet_tab import Auto
    from yuki.perception import tabs as T

    with pytest.raises(OSError):
        T.walk_tabs(Auto({}), 5, time.monotonic() + 1)
    diag: dict = {}
    assert T.walk_tabs(Auto({}), 5, time.monotonic() + 1, diagnostics=diag) == ([], 0, False)
    assert diag["reason"] == "error"
    # the brain's read_tabs words it as before: "could not read", not "shows no tabs"
    monkeypatch.setattr(T, "_on_uia_thread", lambda job, budget_s, name: job(Auto({}), {}))
    strip = T.read_tabs(5)
    assert strip.error and T.format_tabs(strip).startswith("could not read the tabs of window 5")


def test_doctor_reads_the_privacy_file_under_yuki_memory_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.core import doctor

    (tmp_path / "privacy.toml").write_text("[moments]\nenabled = false\n", encoding="utf-8")
    monkeypatch.setenv("YUKI_MEMORY_DIR", str(tmp_path))
    assert doctor._privacy_table("moments") == {"enabled": False}
    c = doctor.check_moments(tmp_path / "missing.db")
    assert c.status == doctor.SKIP and "switched off" in c.detail
