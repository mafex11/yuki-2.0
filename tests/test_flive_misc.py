"""fLive: site URL templates in one place, the Now board's live facts, the doctor's two checks, the shared Core Audio
declarations and Yuki's audio context (PLAN.md D0, D9, D.1, F item 7, critique M10).

Offline: temp databases, stubs; no window, device or real database is touched.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from yuki.connect import templates
from yuki.core import doctor
from yuki.memory.now import NowBoard, game_in_front


# -- templates ------------------------------------------------------------------------------------------------------


def test_youtube_page_entities_come_from_the_one_template_file() -> None:
    assert templates.page_of("https://www.youtube.com/watch?v=abc123XYZ_-&t=40") == templates.PageMatch(
        site="youtube", kind="watch", values={"id": "abc123XYZ_-"})
    assert templates.page_of("https://www.youtube.com/@InventedChannel").values == {"handle": "InventedChannel"}
    assert templates.page_of("https://m.youtube.com/shorts/s1").kind == "shorts"
    assert templates.page_of("https://www.youtube.com/channel/UCinvented").entity == "UCinvented"
    home = templates.page_of("https://www.youtube.com/")
    assert home is not None and home.kind == "home" and home.entity is None
    assert templates.page_of("https://www.youtube.com/watch") is None            # no id: no page entity
    assert templates.page_of("https://www.youtube.com/results?search_query=x") is None
    assert templates.page_of("https://example.com/watch?v=abc") is None
    assert templates.page_of(None) is None


def test_fill_builds_links_from_the_data() -> None:
    assert templates.fill("youtube", "watch", id="abc") == "https://www.youtube.com/watch?v=abc"
    assert templates.fill("youtube", "feed", id="UCx") == "https://www.youtube.com/feeds/videos.xml?channel_id=UCx"
    assert templates.fill("spotify", "playlist_uri", id="AbC") == "spotify:playlist:AbC"
    assert templates.fill("youtube", "watch") is None and templates.fill("nosuch", "watch", id="a") is None
    assert templates.match_template("spotify:playlist:{id}", "spotify:playlist:Q1") == {"id": "Q1"}
    assert templates.match_template("spotify:playlist:{id}", "spotify:album:Q1") is None
    assert set(templates.sites()) >= {"spotify", "youtube"}


def test_no_template_needs_an_account_or_key() -> None:
    for name in templates.sites():
        raw = (templates.SITES_DIR / f"{name}.toml").read_text(encoding="utf-8").lower()
        for word in ("client_id", "api_key", "token", "secret", "oauth", "/v1/me"):
            assert word not in raw, (name, word)


# -- the Now board ----------------------------------------------------------------------------------------------------


def test_board_serves_calling_tabs_game_front_and_signals() -> None:
    board = NowBoard()
    heard: list[Any] = []
    board.subscribe_layout(lambda surfaces, fg, at: heard.append((len(surfaces), fg)))
    board.set_layout([{"hwnd": 31, "kind": "window", "title": "x", "rect": [0, 0, 9, 9], "share": 1.0, "z": 0,
                       "pid": 5}], foreground=31, at=time.time(), ms=1.0)
    assert heard == [(1, 31)]
    board.set_calling_tab(31, {"process": "zen", "service": "Google Meet", "tab_id": "42.1.2", "name_digest": "ab" * 32,
                               "group": 0, "index": 2, "seen_at": time.time(), "known": True})
    board.set_game_front(False)
    board.set_signals(mic_holders=["zen"], playing=["spotify", "spotify"])
    snap = board.snapshot()
    assert snap["calling_tabs"]["31"]["tab_id"] == "42.1.2"
    assert snap["game_front"] is False and snap["signals"]["playing"] == ["spotify"]
    assert game_in_front(snap) is False
    assert game_in_front({**snap, "game_front_at": time.time() - 5}) is None       # older than 2 s: read locally
    board.game_front_fn = lambda: True
    assert board.snapshot()["game_front"] is True
    board.game_front_fn = lambda: 1 / 0
    assert board.snapshot()["game_front"] is None
    assert board.sizes()["calling_tabs"] == 1


def test_a_failing_layout_subscriber_never_breaks_the_board() -> None:
    board = NowBoard()
    board.subscribe_layout(lambda *a: 1 / 0)
    board.set_layout([], foreground=0, at=1.0, ms=0.1)
    assert board.counters["layouts"] == 1


# -- the doctor -------------------------------------------------------------------------------------------------------


def _db(tmp: Path) -> Path:
    from yuki.memory.store import Store

    path = tmp / "memory.db"
    Store.open(path).close()
    return path


def _moments(path: Path, rows: list[tuple[str, float, str]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS moments (id INTEGER PRIMARY KEY, at REAL NOT NULL, ended_at REAL, "
                 "seen_at REAL NOT NULL, kind TEXT NOT NULL, app TEXT, by TEXT NOT NULL DEFAULT 'user', "
                 "source TEXT NOT NULL DEFAULT 'x')")
    conn.executemany("INSERT INTO moments(at, seen_at, kind, by) VALUES (?,?,?,?)",
                     [(at, at + 1, kind, by) for kind, at, by in rows])
    conn.commit()
    conn.close()


def test_doctor_moments_flowing(tmp_path: Path) -> None:
    path = _db(tmp_path)
    priv = tmp_path / "privacy.toml"
    priv.write_text("", encoding="utf-8")
    assert doctor.check_moments(path, priv).status == doctor.SKIP          # not part of this install yet
    now = time.time()
    _moments(path, [("call_join", now - 3600, "user"), ("media_play", now - 120, "user"),
                    ("media_play", now - 100, "yuki"), ("volume_change", now - 60, "user")])
    c = doctor.check_moments(path, priv, now=now)
    assert c.status == doctor.OK
    assert c.detail.startswith("4 in the last day (media play 2, ")
    assert "1 of them by Yuki" in c.detail and "newest just now" in c.detail
    stale = doctor.check_moments(path, priv, now=now + 8 * 3600)
    assert stale.status == doctor.WARN and "nothing new" in stale.detail
    none = doctor.check_moments(path, priv, now=now + 3 * 86400)
    assert none.status == doctor.WARN and none.detail.startswith("none noted")
    priv.write_text("[moments]\nenabled = false\n", encoding="utf-8")
    assert doctor.check_moments(path, priv, now=now).status == doctor.SKIP


def test_doctor_spotify_desktop(tmp_path: Path) -> None:
    from yuki.memory.store import Store

    path = _db(tmp_path)
    c = doctor.check_spotify_desktop(path)
    assert c.status == doctor.SKIP and "no setup needed" in c.detail
    now = time.time()
    status = {"found_at": now - 600, "session_seen_at": now - 500,
              "last_read": {"at": now - 400, "trigger": "play_start", "status": "ok", "window_state": "minimised",
                            "rows": 12, "slider": True, "cold": False},
              "probes": {"covered": {"day": "2026-10-10", "status": "ok", "rows": 12, "slider": True},
                         "minimised": {"day": "2026-10-10", "status": "not_built", "rows": 0, "slider": False}}}
    s = Store.open(path)
    s.set_nudge_state("spotify_desktop", json.dumps(status))
    s.close()
    c = doctor.check_spotify_desktop(path, now=now)
    assert c.status == doctor.OK
    assert "last window read ok (12 Library rows, the volume slider), Spotify minimised" in c.detail
    assert "covered by another window: readable" in c.detail and "minimised: not readable" in c.detail
    status["last_read"]["status"] = "not_built"
    s = Store.open(path)
    s.set_nudge_state("spotify_desktop", json.dumps(status))
    s.close()
    bad = doctor.check_spotify_desktop(path, now=now)
    assert bad.status == doctor.WARN and "not built yet" in bad.detail and bad.fix


def test_doctor_lists_both_new_checks() -> None:
    import inspect

    src = inspect.getsource(doctor.run_checks)
    assert "check_moments" in src and "check_spotify_desktop" in src


# -- Core Audio declarations, Yuki's audio context ---------------------------------------------------------------------


def test_shared_session_interfaces_and_callbacks() -> None:
    from yuki.memory import loopback
    from yuki.perception import audio

    assert loopback.IAudioSessionEvents is audio.IAudioSessionEvents
    assert loopback.IAudioSessionNotification is audio.IAudioSessionNotification
    assert len(audio.IAudioSessionEvents._methods_) == 7
    assert len(audio.IMMNotificationClient._methods_) == 5
    assert len(audio.IAudioEndpointVolumeCallback._methods_) == 1
    assert len(audio.ISimpleAudioVolume._methods_) == 4
    assert str(audio.yuki_context()) == "{594B5549-A0D1-4C7F-9A55-59554B490001}"
    assert audio.guid_text(audio.yuki_context()) == "{594B5549-A0D1-4C7F-9A55-59554B490001}"
    assert audio.guid_text(None) == ""


def test_app_volume_passes_yukis_context() -> None:
    from yuki.actions.app_volume import AppSession, set_app_volume

    seen: list[Any] = []

    class Vol:
        level, muted = 0.5, False

        def SetMasterVolume(self, level: float, ctx: Any) -> None:
            seen.append(ctx)
            self.level = level

        def GetMasterVolume(self) -> float:
            return self.level

        def SetMute(self, mute: bool, ctx: Any) -> None:
            seen.append(ctx)
            self.muted = mute

        def GetMute(self) -> bool:
            return self.muted

    session = AppSession(app="Spotify.exe", pid=1, level=50.0, muted=False, state="active", device="d", volume=Vol())
    result = set_app_volume("Spotify.exe", level=30, _sessions=lambda: [session], _system=lambda: {"level": 40.0})
    assert result.ok
    assert len(seen) == 1 and str(seen[0]) == "{594B5549-A0D1-4C7F-9A55-59554B490001}"


def test_tab_identities_in_the_memory_apartment_are_content_free() -> None:
    from tests.test_flive_meet_tab import Auto, zen
    from yuki.memory.extract import uia

    got = uia.tab_identities(31, digest_fn=lambda n: "d" * 64, automation_fn=lambda: Auto({31: zen()}), now=5.0)
    assert got["tabs"] == 12 and got["groups"] == [3, 9] and got["selected_tabs"] == 1
    assert got["sighting"]["digest"] == "d" * 64 and got["sighting"]["position"] == {"group": 0, "index": 2,
                                                                                       "groups": 2}
    assert "Meet" not in json.dumps(got)
    census = uia.tab_census([(31, 0)], {"names": {"d" * 64: {"n": 1}}}, digest_fn=lambda n: "d" * 64 if "Meet" in n
                            else "e" * 64, automation_fn=lambda: Auto({31: zen()}))
    assert census["decision"] == "one" and census["tabs"] == 12


# -- review round 1 ---------------------------------------------------------------------------------------------------


def test_a_calling_tab_whose_window_comes_back_stays_on_the_board() -> None:
    """Review r1 L2: off the layout for a moment and back: gone_at is cleared (the tab is not dropped 600 s later)."""
    from yuki.memory import now as N

    b = N.NowBoard()
    b.set_calling_tab(5, {"process": "zen", "service": "Google Meet", "seen_at": 100.0, "known": True})
    b.set_layout([{"hwnd": 5, "pid": 1}], foreground=5, at=100.0, ms=1)
    b.set_layout([], foreground=0, at=101.0, ms=1)
    assert "gone_at" in b._calling_tabs[5]
    b.set_layout([{"hwnd": 5, "pid": 1}], foreground=5, at=102.0, ms=1)
    assert "gone_at" not in b._calling_tabs[5]
    assert "5" in b.snapshot(now=102.0 + N.GONE_TTL_S + 60)["calling_tabs"]
    b.set_layout([], foreground=0, at=200.0, ms=1)
    assert "5" not in b.snapshot(now=200.0 + N.GONE_TTL_S + 1)["calling_tabs"]


def test_board_signals_come_from_the_wired_producer_at_snapshot_time() -> None:
    """Review r1 L1: signals_fn (the call worker's holders, the timeline's playing apps) is asked per snapshot."""
    board = NowBoard()
    board.signals_fn = lambda: {"mic_holders": {"discord", "zen"}, "playing": ["spotify", "spotify", ""]}
    sig = board.snapshot()["signals"]
    assert sig["mic_holders"] == ["discord", "zen"] and sig["playing"] == ["spotify"] and sig["at"] is not None
    board.signals_fn = lambda: 1 / 0
    assert board.snapshot()["signals"] == {"mic_holders": [], "playing": [], "at": None}


def test_page_of_lists_the_sites_once(monkeypatch) -> None:
    """Review r1 L7: page_of runs per timeline row in a backfill; the folder is listed once, then cached."""
    templates.clear_cache()
    assert templates.page_of("https://www.youtube.com/watch?v=a1") is not None

    def no_listing(*_a, **_k):
        raise AssertionError("listed the sites folder again")

    monkeypatch.setattr(type(templates.SITES_DIR), "glob", no_listing)
    t0 = time.perf_counter()
    for i in range(5000):
        assert templates.page_of(f"https://www.youtube.com/watch?v=v{i}") is not None
        assert templates.page_of("https://example.com/x") is None
    assert time.perf_counter() - t0 < 2.0
    monkeypatch.undo()
    templates.clear_cache()
    assert "youtube" in templates.sites()


def test_doctor_moments_count_by_when_they_happened_not_when_written(tmp_path: Path) -> None:
    """Review r1 L8: a rebuild or backfill writes old moments again (fresh seen_at); they are not 'the last day'."""
    path = _db(tmp_path)
    priv = tmp_path / "privacy.toml"
    priv.write_text("", encoding="utf-8")
    now = time.time()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE moments (id INTEGER PRIMARY KEY, at REAL NOT NULL, ended_at REAL, "
                 "seen_at REAL NOT NULL, kind TEXT NOT NULL, app TEXT, by TEXT NOT NULL DEFAULT 'user', "
                 "source TEXT NOT NULL DEFAULT 'x')")
    conn.executemany("INSERT INTO moments(at, seen_at, kind, by) VALUES (?,?,?,?)",
                     [(now - 10 * 86400 + i, now - 30, "call_join", "user") for i in range(50)]
                     + [(now - 600, now - 590, "media_play", "user")])
    conn.commit()
    conn.close()
    c = doctor.check_moments(path, priv, now=now)
    assert c.status == doctor.OK and c.detail.startswith("1 in the last day (media play 1)"), c.detail
    assert "newest 10 min ago" in c.detail
    stale = doctor.check_moments(path, priv, now=now + 8 * 3600)    # only backfilled rows written lately
    assert stale.status == doctor.WARN
