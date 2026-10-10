"""sWeb: the taste seeds (PLAN.md D5, Phase S; map-routines T1.2-T1.3): YouTube channels from the user's own page
moments, recomputed, never incremented; a Discord voice channel joined in the user's own words. No network, invented
names, a temp database; names only encrypted.
"""

from __future__ import annotations

import json
from datetime import datetime

from _sweb_world import open_store
from yuki.memory.moments import Moment
from yuki.skills import taste as T

DAY = 86400.0


def _at(days_ago: float, hour: int, *, now: float) -> float:
    base = datetime.fromtimestamp(now - days_ago * DAY).replace(hour=hour, minute=10, second=0, microsecond=0)
    return base.timestamp()


def _page(store, i: int, at: float, handle: str | None, *, by: str = "user", template: str = "youtube.handle",
          service: str = "youtube") -> None:
    m = Moment(kind="page", at=at, ended_at=at + 120, source="timeline", source_id=i, app="arc", service=service,
               entity=handle, keep_entity=handle is not None, by=by,
               facts={"template": template, "host": "youtube.com"} if template else {"host": "example.org"})
    store.save_moments([m.row(store.cipher)])


def test_youtube_channels_are_seeded_from_the_users_own_handle_pages(tmp_path) -> None:
    store = open_store(tmp_path)
    now = datetime(2026, 10, 7, 18, 0).timestamp()           # a Wednesday
    weekday_1310 = _at(1, 13, now=now)                       # Tuesday
    _page(store, 1, weekday_1310, "Quillon")
    _page(store, 2, _at(8, 13, now=now), "@Quillon")         # a week earlier, the same handle
    _page(store, 3, _at(4, 21, now=now), "OtterCove")       # Saturday 21:10
    _page(store, 4, _at(1, 9, now=now), "Quillon", by="yuki")        # Yuki's own visit: never counts
    _page(store, 5, _at(1, 9, now=now), "Thistle", template="youtube.watch")    # a video page, not a channel
    _page(store, 6, _at(70, 13, now=now), "Faraway")         # older than 60 days
    got = T.seed_youtube(store, now=now)
    assert got == {"channels": 2, "visits": 3, "removed": 0}
    rows = {store._dec(r["name_ciphertext"]): dict(r) for r in store._query("SELECT * FROM tastes")}
    assert set(rows) == {"Quillon", "OtterCove"}
    q = rows["Quillon"]
    assert q["kind"] == "yt_channel" and q["source"] == "observed"
    assert store._dec(q["ref_ciphertext"]) == "https://www.youtube.com/@Quillon"
    wd = json.loads(q["hist_weekday"])
    assert wd[13] > 1.5 and sum(wd) == wd[13] and json.loads(q["hist_weekend"]) == [0.0] * 24
    assert 1.5 < q["n_obs"] < 2.0                            # decayed: the week-old visit weighs less
    o = rows["OtterCove"]
    assert json.loads(o["hist_weekend"])[21] > 0 and sum(json.loads(o["hist_weekday"])) == 0
    for r in rows.values():                                  # never in clear
        assert "Quillon" not in str(r["name_ciphertext"]) and "Quillon" not in str(r["entity_key"])
    # the read side ranks them by the hour
    top = T.top(store, "yt_channel", now=weekday_1310 + 3 * DAY, n=1)
    assert top and top[0].name == "Quillon"


def test_the_seed_is_recomputed_not_incremented_and_drops_what_no_visit_supports(tmp_path) -> None:
    store = open_store(tmp_path)
    now = datetime(2026, 10, 7, 18, 0).timestamp()
    _page(store, 1, _at(1, 13, now=now), "Quillon")
    _page(store, 2, _at(2, 13, now=now), "OtterCove")
    first = T.seed_youtube(store, now=now)
    snapshot = [dict(r) for r in store._query("SELECT kind, entity_key, hist_weekday, n_obs FROM tastes ORDER BY id")]
    assert T.seed_youtube(store, now=now) == {**first, "removed": 0}
    assert [dict(r) for r in store._query("SELECT kind, entity_key, hist_weekday, n_obs FROM tastes ORDER BY id")] \
        == snapshot
    store._write(lambda conn: conn.execute("DELETE FROM moments WHERE source_id = 2"))
    assert T.seed_youtube(store, now=now)["removed"] == 1
    names = [store._dec(r[0]) for r in store._query("SELECT name_ciphertext FROM tastes")]
    assert names == ["Quillon"]


def test_a_joined_voice_channel_is_noted_by_the_hour_in_the_users_words(tmp_path) -> None:
    store = open_store(tmp_path)
    at = datetime(2026, 10, 6, 20, 30).timestamp()           # Tuesday 20:30
    assert T.note(store, "discord_voice", "lantern-club", at=at)
    assert T.note(store, "discord_voice", "  lantern-club ", at=at + 60)
    assert not T.note(store, "discord_voice", "   ", at=at)
    rows = store._query("SELECT * FROM tastes")
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "discord_voice" and r["source"] == "said" and r["n_obs"] == 2.0
    assert json.loads(r["hist_weekday"])[20] == 2.0 and store._dec(r["name_ciphertext"]) == "lantern-club"
    assert T.candidates(store, "discord_voice", now=at) == ["lantern-club"]


def test_seed_runs_every_seed_and_survives_a_database_without_moments(tmp_path) -> None:
    store = open_store(tmp_path)
    assert T.seed(store) == {"yt_channel": {"channels": 0, "visits": 0, "removed": 0}}

    class NoMoments:
        def moments_between(self, *a, **k):
            raise RuntimeError("no such table: moments")

    assert T.seed_youtube(NoMoments())["error"] == 1
