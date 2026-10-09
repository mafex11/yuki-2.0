"""fMoments, the live side: the hub's inputs (media, front changes, calling-site sightings, volume, Spotify's playlist),
the timeline's two new hooks, the call worker's nudge, the service wiring, content-free logs and the footprint.

Offline: temp databases, stubbed media sessions and window facts, invented names. Nothing reads the desktop.
"""

from __future__ import annotations

import ast
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuki.memory import moments as M
from yuki.memory.store import Store

ROOT = Path(__file__).resolve().parents[1]
#: Invented content that must never reach a log line or a cleartext column.
SECRETS = ("Midnight Rain (Live)", "Lo-fi Study Beats", "spotify:playlist:37i9dQZF1DX8Uebhn9wzrS",
           "meet.google.com", "Weekly Sync with Priya")


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


class Clock:
    def __init__(self, t: float = 1_790_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def hub(store, *, cfg=None, log=None, calling=None, acting=None, runs=None, clock=None):
    return M.MomentHub(store, log=log, config=(lambda: cfg or M.MomentsConfig()), clock=clock or Clock(),
                       calling_service=calling, acting_fn=acting or (lambda: None),
                       runs_fn=runs or (lambda lo, hi: []), is_game=lambda p: False, page_entity=None)


class LogSink:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def __call__(self, type: str, **fields) -> None:
        self.records.append((type, fields))

    def of(self, type: str) -> list[dict]:
        return [f for t, f in self.records if t == type]


def cleartext(store) -> str:
    """Every column of every moment row except the ciphertexts, as one string."""
    rows = store._query("SELECT * FROM moments")
    return " ".join(str(v) for r in rows for k, v in dict(r).items() if not k.endswith("ciphertext"))


# -- media -------------------------------------------------------------------------------------------------------------


def test_media_play_keeps_only_a_digest_and_ends_when_it_stops(store):
    sink = LogSink()
    h = hub(store, log=sink)
    song = {"app_id": "spotifyab.spotifymusic_x!spotify", "app": "spotify", "title": SECRETS[0]}
    h.on_media_change([song], [], at=100.0, initial=True)
    h.beat(101.0)
    rows = store.moments_between(kinds=["media_play"])
    assert len(rows) == 1 and rows[0]["app"] == "spotify" and rows[0]["entity_ciphertext"] is None
    assert rows[0]["facts"] == {"initial": True} and rows[0]["evidence"] == "poll" and rows[0]["ended_at"] is None
    assert rows[0]["entity_digest"] == store.cipher.digest("moment", "media_play", "spotify", SECRETS[0])
    h.on_media_change([], [song], at=160.0)
    h.beat(161.0)
    assert store.moments_between(kinds=["media_play"])[0]["ended_at"] == 160.0
    assert SECRETS[0] not in cleartext(store)
    line = sink.of("moment")[0]
    assert line["kind"] == "media_play" and re.fullmatch(r"[0-9a-f]{12}", line["entity_digest"])


def test_media_titles_kept_only_when_the_user_says_so_and_never_after_a_private_stretch(store):
    from yuki.memory.store import TimelineRow

    h = hub(store, cfg=M.MomentsConfig(media_titles=True))
    h.on_media_change([{"app_id": "a", "app": "spotify", "title": "kept title"}], [], at=time.time())
    store.save_timeline(TimelineRow(
        id=None, started_at=time.time() - 120, ended_at=time.time() - 60, state="present", process="chrome",
        app="Chrome", title=None, host=None, path=None, page_key=None, active_s=60, passive_s=0, away_s=0, media_s=0,
        media_other={}, withheld="private_window", segments=1, fullscreen=False, meeting=None, mic_s=0,
        by_yuki=False, yuki_request=None))
    h.on_media_change([{"app_id": "b", "app": "chrome", "title": "private title"}], [], at=time.time())
    h.beat()
    rows = {r["app"]: r for r in store.moments_between(kinds=["media_play"])}
    assert store.moment_entity(rows["spotify"]) == "kept title"
    assert rows["chrome"]["entity_ciphertext"] is None


def test_media_while_yuki_acts_is_yukis_and_subscribers_hear_every_change(store):
    heard = []
    h = hub(store)
    h.subscribe_media(lambda started, stopped, **f: heard.append((len(started), len(stopped), f["at"])))
    h.subscribe_media(lambda *a, **k: 1 / 0)   # a broken subscriber breaks nothing
    h.on_media_change([{"app_id": "a", "app": "spotify", "title": "t"}], [], at=50.0, by_yuki=True,
                      request_id="voice-9")
    h.beat(51.0)
    row = store.moments_between(kinds=["media_play"])[0]
    assert (row["by"], row["request_id"]) == ("yuki", "voice-9") and heard == [(1, 0, 50.0)]


def test_nothing_is_written_when_moments_are_off(store):
    h = hub(store, cfg=M.MomentsConfig(enabled=False), calling=lambda host: "Google Meet")
    h.on_media_change([{"app_id": "a", "app": "spotify", "title": "t"}], [], at=1.0)
    h.site({"process": "zen", "hwnd": 5, "host": "meet.google.com", "blocked": "blocked_host", "at": 2.0})
    h.volume_change("spotify", 10, 20, means="session", at=3.0)
    h.media_context("spotify", "a playlist", at=4.0)
    h.nudge()
    h.beat(5.0)
    assert store.moments_between() == [] and h.beats == 0


# -- calling sites in front ---------------------------------------------------------------------------------------------


def test_site_front_once_per_window_and_host_closed_by_a_front_change(store):
    sink = LogSink()
    asked = []

    def calling(host):
        asked.append(host)
        return "Google Meet" if host == "meet.google.com" else None

    h = hub(store, log=sink, calling=calling)
    meet = {"process": "zen", "hwnd": 7, "host": "meet.google.com", "blocked": "blocked_host", "at": 10.0,
            "title": SECRETS[4]}
    h.site(meet)
    h.site(dict(meet, at=12.0))                         # the same window read again: no second moment
    h.site({"process": "zen", "hwnd": 8, "host": "bank.example", "blocked": "blocked_host", "at": 13.0})
    h.site({"process": "zen", "hwnd": 9, "host": "docs.example", "at": 13.5})   # a readable page: nothing
    h.on_front_change("zen", "discord", at=20.0, hwnd=11, prev_hwnd=7)
    h.beat(21.0)
    rows = store.moments_between(kinds=["site_front"])
    assert [(r["app"], r["service"], r["at"], r["ended_at"], r["facts"]["sighting"]) for r in rows] == [
        ("zen", "Google Meet", 10.0, 20.0, "first")]
    assert asked == ["meet.google.com", "bank.example"]   # a calling service is asked once per sighting change
    assert sink.of("site_front") == [{"app": "zen", "service": "Google Meet", "sighting": "first", "by": "user",
                                      "request_id": None, "tab_known": None}]
    assert SECRETS[4] not in cleartext(store) and "meet.google.com" not in cleartext(store)


def test_site_front_ends_when_the_same_window_shows_another_page_and_yukis_sighting_is_yukis(store):
    acting = SimpleNamespace(token="t", request="open my meeting", started_at=0.0, request_id="worker-4")
    h = hub(store, calling=lambda host: "Google Meet", acting=lambda: acting)
    h.site({"process": "zen", "hwnd": 7, "host": "meet.google.com", "blocked": "blocked_host", "at": 10.0})
    h.site({"process": "zen", "hwnd": 7, "host": "news.example", "at": 15.0})
    h.beat(16.0)
    row = store.moments_between(kinds=["site_front"])[0]
    assert row["ended_at"] == 15.0 and (row["by"], row["request_id"]) == ("yuki", "worker-4")


def test_site_front_needs_the_call_evidence_answer(store):
    h = hub(store, calling=None)
    h.site({"process": "zen", "hwnd": 7, "host": "meet.google.com", "blocked": "blocked_host", "at": 10.0})
    h.beat(11.0)
    assert store.moments_between() == []


# -- volume and Spotify's playlist ---------------------------------------------------------------------------------------


def test_volume_change_by_means_and_yukis_own_context(store):
    h = hub(store)
    h.volume_change("Spotify.exe", 30, 45, means="session", at=10.0, front="Discord.exe")
    h.volume_change(None, 50, 40, means="master", at=11.0, yuki_context=True)
    h.volume_change("spotify", None, 62.4, means="in_app", at=12.0, since=5.0)
    h.beat(13.0)
    rows = store.moments_between(kinds=["volume_change"])
    assert [(r["app"], r["value_from"], r["value"], r["means"], r["by"], r["source"]) for r in rows] == [
        ("spotify", 30.0, 45.0, "session", "user", "volume"), (None, 50.0, 40.0, "master", "yuki", "volume"),
        ("spotify", None, 62.4, "in_app", "user", "spotify")]
    assert rows[0]["facts"] == {"front": "discord"} and rows[2]["facts"] == {"since": 5.0}
    off = hub(store, cfg=M.MomentsConfig(volume_events=False))
    off.volume_change("x", 1, 2, means="session", at=20.0)
    off.beat(21.0)
    assert len(store.moments_between(kinds=["volume_change"])) == 3


def test_a_slider_read_is_yukis_when_it_shows_the_level_a_run_set(store):
    runs = [M.RunWindow(request_id="r1", app="spotify", end_kind="volume_change", t0=100.0, t_done=101.0, value=40.0)]
    h = hub(store, runs=lambda lo, hi: runs)
    h.volume_change("spotify", None, 40.0, means="in_app", at=500.0, since=90.0)
    h.beat(501.0)
    assert store.moments_between(kinds=["volume_change"])[0]["request_id"] == "r1"


def test_media_context_name_encrypted_spotify_owned_digest_only_link_encrypted(store):
    sink = LogSink()
    h = hub(store, log=sink)
    h.media_context("spotify", SECRETS[1], at=10.0, link=SECRETS[2], owner="user")
    h.media_context("spotify", "Today's Top Hits", at=20.0, keep_name=False, link=SECRETS[2])
    h.beat(21.0)
    mine, owned = store.moments_between(kinds=["media_context"])
    assert store.moment_entity(mine) == SECRETS[1]
    assert store.cipher.decrypt(mine["facts"]["uri_ciphertext"]) == SECRETS[2]
    assert owned["entity_ciphertext"] is None and "uri_ciphertext" not in owned["facts"]
    assert owned["entity_digest"] == store.cipher.digest("moment", "media_context", "spotify", "Today's Top Hits")
    text = cleartext(store).replace(mine["facts"]["uri_ciphertext"], "")
    assert SECRETS[1] not in text and SECRETS[2] not in text
    assert all(SECRETS[1] not in str(f) and SECRETS[2] not in str(f) for _t, f in sink.records)


# -- the beat ------------------------------------------------------------------------------------------------------------


def test_beat_refreshes_when_due_or_nudged_and_counts_its_own_cpu(store):
    clock = Clock(1_790_000_000.0)
    h = hub(store, cfg=M.MomentsConfig(refresh_s=60), clock=clock)
    h.beat(clock.t)
    assert h.refreshes == 1
    h.beat(clock.t + 1)
    assert h.refreshes == 1                       # not due yet
    store.start_call(app="Zen", process="zen", via="screen", at=clock.t - 30)
    h.nudge("calls")
    h.beat(clock.t + 2)
    assert h.refreshes == 2 and len(store.moments_between(kinds=["call_join"])) == 1
    h.beat(clock.t + 70)
    assert h.refreshes == 3
    s = h.stats()
    assert s["cpu_ms"] >= 0 and s["refresh_ms"]["max"] is not None and s["inputs"]["nudge_calls"] == 1
    assert set(s) >= {"beats", "refreshes", "written_live", "queued", "errors", "kinds", "rebuild"}


def test_start_rebuilds_once_per_rule_version_off_the_loop(store):
    store.start_call(app="Zen", process="zen", via="screen", at=time.time() - 600)
    h = M.MomentHub(store, config=lambda: M.MomentsConfig(), acting_fn=lambda: None, runs_fn=lambda lo, hi: [],
                    is_game=lambda p: False, page_entity=None)
    h.start()
    h._backfill.join(10)
    assert h.stats()["rebuild"] == "done" and store.moment_cursor("rule") == str(M.RULE_VERSION)
    assert len(store.moments_between(kinds=["call_join"])) == 1
    again = M.MomentHub(store, config=lambda: M.MomentsConfig(), acting_fn=lambda: None, runs_fn=lambda lo, hi: [],
                        is_game=lambda p: False, page_entity=None)
    again.start()
    assert again._backfill is None                # the rules did not change: no rebuild


def test_the_writer_never_blocks_and_keys_are_unique(store):
    w = M.MomentWriter(store)
    keys = [w.key(10.0) for _ in range(5)]
    assert keys == sorted(set(keys)) and len(keys) == 5
    w.MAX_QUEUE = 3
    for i in range(5):
        w.put(M.Moment(kind="volume_change", at=float(i), source="volume", source_id=i))
    assert w.pending() == 3 and w.dropped == 2
    real = store.save_moments
    store.save_moments = lambda rows: (_ for _ in ()).throw(RuntimeError("database is locked"))
    with pytest.raises(RuntimeError):
        w.flush()
    assert w.pending() == 3                        # a failed write keeps the batch for the next beat
    store.save_moments = real
    assert len(w.flush()) == 3 and w.flush() == []


def test_open_spans_are_bounded(store):
    h = hub(store, calling=lambda host: "Meet")
    h.MAX_OPEN = 4
    for i in range(10):
        h.on_media_change([{"app_id": f"a{i}", "app": "x", "title": f"t{i}"}], [], at=float(i))
        h.site({"process": "zen", "hwnd": i + 1, "host": "meet.example", "blocked": "x", "at": float(i)})
    assert len(h._media_open) == 4 and len(h._site_open) == 4


# -- the timeline's hooks ----------------------------------------------------------------------------------------------


def _facts(pid, exe, app):
    from yuki.memory.watcher import ProcessFacts

    return ProcessFacts(pid, exe.rsplit("\\", 1)[-1], exe, app, False)


def _recorder(store, tmp_path, windows, *, front=None, media=None, facts_for=None, aumids=None):
    from yuki.memory.privacy import PrivacyConfig
    from yuki.memory.timeline import TimelineRecorder

    rec = TimelineRecorder(store, privacy=PrivacyConfig(tmp_path / "privacy.toml"), signals=SimpleNamespace(
        read=lambda: SimpleNamespace(mic=(), camera=())), on_front_change=front, on_media_change=media)
    rec._w = SimpleNamespace(_window_pid=lambda h: windows[h][0], _window_title=lambda h: windows[h][2],
                             is_fullscreen_front=lambda h: False, user_idle_s=lambda: 0.0,
                             session_locked=lambda: False)
    rec._procs = SimpleNamespace(get=lambda pid: next(f for p, f, _ in windows.values() if p == pid))
    ns = dict(is_user_window=lambda h: True, process_package_ids=lambda pid: ((aumids or {}).get(pid, ""), ""),
              start_menu_index=lambda: None)
    if facts_for is not None:   # the uncached read; the cached one fills a per-pid cache nobody prunes here
        ns["_read_pid_facts"] = facts_for
        ns["_facts_for"] = lambda pid: pytest.fail("the cached per-pid read")
    rec._windows = SimpleNamespace(**ns)
    rec._hook_names = lambda hwnd: None
    rec._unhook_names = lambda: None
    rec._probe_page = lambda st, now: False
    return rec


def test_front_changes_are_told_once_per_app_or_window(store, tmp_path):
    seen = []
    windows = {1: (10, _facts(10, r"C:\A\Spotify.exe", "Spotify"), "Spotify"),
               2: (20, _facts(20, r"C:\A\zen.exe", "Zen"), "a page"),
               3: (21, _facts(21, r"C:\A\zen.exe", "Zen"), "another window")}
    rec = _recorder(store, tmp_path, windows, front=lambda prev, new, **f: seen.append((prev, new, f["hwnd"],
                                                                                        f["prev_hwnd"])))
    t = time.time()
    rec._open(1, t)
    rec._open(1, t + 1)          # the same window again (a title change): no front change
    rec._open(2, t + 2)
    rec._open(3, t + 3)          # another window of the same app: told (a site in front ends per window)
    assert seen == [("", "spotify", 1, 0), ("spotify", "zen", 2, 1), ("zen", "zen", 3, 2)]
    assert rec.counters["front_changes"] == 3


def test_a_broken_front_hook_never_breaks_the_timeline(store, tmp_path):
    windows = {1: (10, _facts(10, r"C:\A\zen.exe", "Zen"), "x")}
    rec = _recorder(store, tmp_path, windows, front=lambda *a, **k: 1 / 0)
    rec._open(1, time.time())
    assert rec._current is not None and rec.counters["errors"] == 1


def test_media_reads_tell_starts_and_stops_and_a_failed_read_tells_nothing(store, tmp_path, monkeypatch):
    from yuki.perception import system

    told = []
    sessions = {"value": [{"app_id": "Spotify.exe", "status": "playing", "title": "song 1"}]}

    def fake(timeout_s=0.2):
        if sessions["value"] is None:
            raise TimeoutError("still waiting")
        return list(sessions["value"])

    monkeypatch.setattr(system, "media_sessions", fake)
    windows = {1: (10, _facts(10, r"C:\A\Spotify.exe", "Spotify"), "Spotify")}
    rec = _recorder(store, tmp_path, windows,
                    media=lambda started, stopped, **f: told.append(([s["title"] for s in started],
                                                                     [s["title"] for s in stopped],
                                                                     [s["app"] for s in started], f["initial"])))
    rec._poll_media()
    assert told == [(["song 1"], [], ["spotify"], True)]
    rec._poll_media()
    assert len(told) == 1                                    # nothing changed
    sessions["value"] = None
    for _ in range(3):
        rec._poll_media()                                    # failed reads: no start, no stop
    assert len(told) == 1 and rec._playing == []
    sessions["value"] = [{"app_id": "Spotify.exe", "status": "playing", "title": "song 2"},
                         {"app_id": "Spotify.exe", "status": "paused", "title": "old"}]
    rec._poll_media()
    assert told[-1] == (["song 2"], ["song 1"], ["spotify"], False)


def test_a_packaged_media_session_is_named_by_its_window_or_its_process(store, tmp_path, monkeypatch):
    from yuki.perception import system

    aumid = "spotifyab.spotifymusic_zpdnekdrzrea0!spotify"
    told = []
    monkeypatch.setattr(system, "media_sessions",
                        lambda timeout_s=0.2: [{"app_id": aumid, "status": "playing", "title": "x"}])
    import psutil

    monkeypatch.setattr(psutil, "pids", lambda: [4, 99])
    facts = {4: ("", r"c:\windows\system32\svchost.exe"), 99: (aumid, r"c:\program files\windowsapps\spotify.exe")}
    windows = {1: (99, _facts(99, r"C:\W\Spotify.exe", "Spotify"), "Spotify")}
    rec = _recorder(store, tmp_path, windows, facts_for=lambda pid: facts[pid],
                    media=lambda started, stopped, **f: told.append(started[0]["app"] if started else None))
    rec._poll_media()                         # never in front yet: its running process names it
    assert told == ["spotify"] and rec._app_ids[aumid] == "spotify"
    rec2 = _recorder(store, tmp_path, windows, aumids={99: aumid},
                     media=lambda started, stopped, **f: told.append(started[0]["app"] if started else None))
    rec2._open(1, time.time())                # in front once: its window's identity names it
    assert rec2._app_ids[aumid] == "spotify"


# -- the call worker's nudge ----------------------------------------------------------------------------------------------


def test_the_call_worker_nudges_after_a_call_starts_or_ends(store):
    from yuki.memory.calls import CallConfig, CallWorker

    told = []
    w = CallWorker(store, config=CallConfig(), holders_fn=lambda: [], source_factory=lambda cfg, app: None,
                   on_call_change=told.append)
    w._log("call_detected", call_id=1)
    w._log("call_consent", call_id=1)
    w._log("game_voice_detected", call_id=2)
    w._log("call_ended", call_id=1)
    assert told == ["calls", "calls", "calls"]
    w.on_call_change = lambda source: 1 / 0
    w._log("call_ended", call_id=1)            # a broken hook never breaks the worker


# -- the service wiring (read from the source: the service itself reads the live desktop) ----------------------------


def test_the_service_builds_beats_reports_and_stops_the_hub():
    src = (ROOT / "yuki" / "memory" / "service.py").read_text(encoding="utf-8")
    ast.parse(src)
    for needle in ("MomentHub(store, privacy=privacy, log=log, db_path=db_path", "moments.start()",
                   "on_media_change=moments.on_media_change", "on_front_change=moments.on_front_change",
                   "on_call_change=moments.nudge", "moments.beat()", "moments=moments.stats()",
                   "moments.log_sources()", "moments.stop()"):
        assert needle in src, needle
    assert src.index("MomentHub(") < src.index("TimelineRecorder(") < src.index("CallWorker(")


# -- logs, footprint -----------------------------------------------------------------------------------------------------


def test_logs_stay_content_free_under_a_fuzz_of_names(store):
    sink = LogSink()
    h = hub(store, log=sink, calling=lambda host: "Google Meet")
    for i, s in enumerate(SECRETS):
        h.on_media_change([{"app_id": f"a{i}", "app": "spotify", "title": s}], [], at=10.0 + i)
        h.site({"process": "zen", "hwnd": 100 + i, "host": s.replace(" ", "").lower(), "blocked": "x", "at": 20.0 + i,
                "title": s})
        h.media_context("spotify", s, at=30.0 + i, link=s)
        h.volume_change("spotify", 1, 2, means="in_app", at=40.0 + i, note=s)
    h.beat(60.0)
    h.log_sources()
    text = repr(sink.records)
    stored = cleartext(store)
    for s in SECRETS:
        assert s not in text and s.replace(" ", "").lower() not in text
        assert s not in stored                    # nor in a cleartext column (a caller's extra "note" is dropped)
    for _t, f in sink.records:
        for k, v in f.items():
            if k.endswith("_digest") and v is not None:
                assert re.fullmatch(r"[0-9a-f]{12}", v)


def test_footprint_of_an_hour_of_synthetic_events(store):
    import tracemalloc

    clock = Clock(1_790_000_000.0)
    h = hub(store, clock=clock, calling=lambda host: "Bench Meet" if host == "meet.example" else None)
    tracemalloc.start()
    c0 = time.thread_time()
    for i in range(3600):
        clock.t += 1
        if i % 15 == 0 and i % 45 == 0:
            h.on_media_change([{"app_id": "x", "app": "benchapp", "title": f"t{i}"}], [], at=clock.t)
        if i % 300 == 0:
            h.volume_change("benchapp", 10, 20, means="session", at=clock.t)
            h.on_front_change("a", "b", at=clock.t, hwnd=i)
        if i % 900 == 0:
            h.site({"process": "zen", "hwnd": 7, "host": "meet.example", "blocked": "x", "at": clock.t})
        h.beat(clock.t)
    cpu = time.thread_time() - c0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 15 * 2**20                     # < 15 MB (B.1)
    assert cpu / 3600.0 < 0.01                   # < 1% of one core over the simulated hour (generous on a test box)


# -- review round 1: spans that end, a rebuild that fails, no host as a service ----------------------------------------


def test_a_media_stop_finds_its_start_whatever_app_the_session_resolved_to(store):
    h = hub(store)
    aumid = "pkg.example_x!app"
    h.on_media_change([{"app_id": aumid, "app": "pkg.example_x!app", "title": SECRETS[0]}], [], at=10.0, initial=True)
    h.on_media_change([], [{"app_id": aumid, "app": "spotify", "title": SECRETS[0]}], at=20.0)   # learned meanwhile
    h.beat(21.0)
    row = store.moments_between(kinds=["media_play"])[0]
    assert row["ended_at"] == 20.0 and h.stats()["media_open"] == 0
    h.on_media_change([{"app_id": aumid, "app": "spotify", "title": "b"}], [], at=30.0)
    h.on_media_change([{"app_id": aumid, "app": "spotify", "title": "b"}], [], at=40.0)   # a start never stopped
    h.beat(41.0)
    rows = store.moments_between(kinds=["media_play"])
    assert [(r["at"], r["ended_at"]) for r in rows] == [(10.0, 20.0), (30.0, 40.0), (40.0, None)]


def test_stop_ends_open_spans_and_the_next_start_ends_what_a_crash_left_open(store):
    from yuki.memory.store import TimelineRow

    h = hub(store, calling=lambda host: "Google Meet")
    h.on_media_change([{"app_id": "a", "app": "spotify", "title": "t"}], [], at=10.0, initial=True)
    h.site({"process": "zen", "hwnd": 7, "host": "meet.google.com", "blocked": "blocked_host", "at": 12.0})
    h.beat(13.0)
    h.stop(now=30.0)
    got = {r["kind"]: (r["ended_at"], r["facts"]) for r in store.moments_between()}
    assert got == {"media_play": (30.0, {"initial": True, "ended": "stop"}),
                   "site_front": (30.0, {"sighting": "first", "ended": "stop"})}
    assert h.stats()["media_open"] == 0 and h.stats()["site_open"] == 0
    crashed = hub(store)
    crashed.on_media_change([{"app_id": "a", "app": "spotify", "title": "u"}], [], at=40.0)
    crashed.beat(41.0)                                    # ... and the process dies: no stop()
    store.save_timeline(TimelineRow(
        id=None, started_at=41.0, ended_at=55.0, state="present", process="code", app="Code", title=None, host=None,
        path=None, page_key=None, active_s=14, passive_s=0, away_s=0, media_s=0, media_other={}, withheld=None,
        segments=1, fullscreen=False, meeting=None, mic_s=0, by_yuki=False, yuki_request=None))
    sink = LogSink()
    nxt = hub(store, log=sink)
    nxt.start()
    if nxt._backfill is not None:
        nxt._backfill.join(10)
    row = [r for r in store.moments_between(kinds=["media_play"]) if r["at"] == 40.0][0]
    assert row["ended_at"] == 55.0 and row["facts"]["ended"] == "restart"   # last seen alive, never "now"
    assert {"source": "restart", "ended": 1} in sink.of("moment_source")
    assert store.end_open_moments(99.0, sources=["media"], kinds=["media_play"], reason="restart") == 0


def test_a_failed_rebuild_is_retried_off_the_loop_and_the_loop_waits_for_it(store):
    clock = Clock(1_790_000_000.0)
    store.start_call(app="Zen", process="zen", via="screen", at=clock.t - 600)
    h = hub(store, clock=clock)
    h.extractor.refresh(clock.t)
    cursors = {n: store.moment_cursor(n) for n in ("calls", "timeline", "messages")}
    store.set_moment_cursor("rule", "0")
    real = h.extractor._rebuild_joins
    h.extractor._rebuild_joins = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database is locked"))
    h.start()
    h._backfill.join(10)
    assert h.stats()["rebuild"] == "error" and store.moment_cursor("rule") == "0"
    assert {n: store.moment_cursor(n) for n in ("calls", "timeline", "messages")} == cursors
    first = h._backfill
    h.extractor._rebuild_joins = real
    h.beat(clock.t + 1)                                   # before the retry: no full read on the loop, no rebuild
    assert h.refreshes == 0 and h._backfill is first
    clock.t += M.REBUILD_RETRY_S + 1
    h.beat(clock.t)                                       # the retry, on its own thread again
    assert h._backfill is not first and h.refreshes == 0
    h._backfill.join(10)
    assert h.stats()["rebuild"] == "done" and store.moment_cursor("rule") == str(M.RULE_VERSION)
    assert len(store.moments_between(kinds=["call_join"])) == 1
    h.beat(clock.t + 1)
    assert h.refreshes == 1


def test_a_calling_service_that_answers_with_the_host_gives_no_service(store):
    sink = LogSink()
    h = hub(store, log=sink, calling=lambda host: host[4:] if host.startswith("www.") else host)
    h.site({"process": "zen", "hwnd": 7, "host": "www.meet.google.com", "blocked": "blocked_host", "at": 10.0})
    h.beat(11.0)
    row = store.moments_between(kinds=["site_front"])[0]
    assert row["service"] is None and "meet.google.com" not in cleartext(store)
    assert "meet.google.com" not in repr(sink.records)
