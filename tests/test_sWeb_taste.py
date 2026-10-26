"""sWeb: the taste notes (PLAN.md D5, Phase S; map-routines T1.2-T1.3): a Discord voice channel joined in the user's own
words. No network, invented names, a temp database; names only encrypted.

r30: the YouTube channel seed tests went with ``taste.seed_youtube`` (removed by lWire, 10 Oct; ``taste.seed`` /
``recompute`` and ``youtube_feed`` replaced it).
"""

from __future__ import annotations

import json
from datetime import datetime

from _sweb_world import open_store
from yuki.skills import taste as T


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
