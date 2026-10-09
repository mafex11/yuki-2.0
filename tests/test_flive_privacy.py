"""fLive privacy fuzz (PLAN.md §6, B.1 "Logs"): every log event the new live sources write (``spotify_read``,
``spotify_probe``, ``tab_strip``, ``tab_census``, ``volume_event``) is content-free: fed
random invented names, owners, playlist ids and tab titles, no log value, status document or Now-board entry holds
any of them; every string a log keeps is a code word (or the calling service's product name).

Offline: stub readers, a temp store stub with a real cipher.
"""

from __future__ import annotations

import json
import os
import random
import string
import time
from typing import Any

import pytest

from tests.test_flive_meet_tab import Auto, El, GROUP, PANE, TAB_ITEM, TOOLBAR
from yuki.connect import spotify_desktop as SD
from yuki.log.events import _code_word
from yuki.memory.callevidence import CallEvidence, SiteTabs
from yuki.memory.crypto import FieldCipher
from yuki.memory.now import NowBoard
from yuki.memory.volumewatch import VolumeWatch
from yuki.perception import tabs as T


def _word(rng: random.Random) -> str:
    alphabet = string.ascii_letters + "äöüßéñ漢字ğ  -'"
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(4, 18))).strip() or "Xy"


class Store:
    def __init__(self) -> None:
        self.cipher = FieldCipher(os.urandom(32))
        self.state: dict[str, str] = {}

    def nudge_state(self, name: str) -> str | None:
        return self.state.get(name)

    def set_nudge_state(self, name: str, value: str | None) -> None:
        self.state[name] = value  # type: ignore[assignment]


ALLOWED_TEXT_FIELDS = {"service"}   # the calling service's product name (from the shipped list's verdict)


def _assert_content_free(logs: list[tuple[str, dict[str, Any]]], secrets: list[str]) -> None:
    blob = json.dumps(logs, ensure_ascii=False, default=str)
    for s in secrets:
        assert s not in blob, s

    def check(key: str, value: Any) -> None:
        if isinstance(value, str):
            assert key in ALLOWED_TEXT_FIELDS or _code_word(value), (key, value)
        elif isinstance(value, (list, tuple)):
            for v in value:
                check(key, v)
        elif isinstance(value, dict):
            for k, v in value.items():
                check(k, v)
        else:
            assert value is None or isinstance(value, (int, float, bool)), (key, value)

    for _type, fields in logs:
        for k, v in fields.items():
            check(k, v)


@pytest.mark.parametrize("seed", range(5))
def test_spotify_watch_logs_and_status_never_hold_names_links_or_ids(seed: int,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    rng = random.Random(seed)
    monkeypatch.setattr(SD, "process_identity", lambda pid: {"aumid": "", "family": "", "image": "Spotify.exe"})
    names = [_word(rng) for _ in range(6)]
    owners = [_word(rng) for _ in range(6)]
    ids = ["".join(rng.choice(string.ascii_letters + string.digits) for _ in range(22)) for _ in range(6)]
    rows = [SD.Row(n, o, i % 3 == 0, i == 2) for i, (n, o) in enumerate(zip(names, owners))]
    links = {n: f"spotify:playlist:{ids[i]}" for i, n in enumerate(names) if i % 3 != 0}
    # a row wording that holds people's names (the worst case of review r1 M1), confirmed by a row flip on the
    # second read: whatever is kept must hold none of them in clear
    play_t, pause_t = "{name} " + owners[0], "{name} " + owners[1]

    def raw(playing: int) -> list[tuple[str, list[str]]]:
        return [(n, [(pause_t if i == playing else play_t).replace("{name}", n)]) for i, n in enumerate(names)]

    reads = [0]

    class Reader:
        def find(self) -> SD.WindowRef:
            return SD.WindowRef(hwnd=1, pid=2, minimised=False)

        def state(self, hwnd: int) -> str:
            return "minimised"

        def warm(self, hwnd: int, budget_s: float, **kw: Any) -> SD.WarmResult:
            return SD.WarmResult(before=7, after=300, built=True, cold=True, ms=900.0)

        def read(self, hwnd: int, budget_s: float, **kw: Any) -> SD.SpotifyRead:
            reads[0] += 1
            playing = reads[0] % len(names)
            cand = SD.WordingGuess(play_t, pause_t, names[1],
                                   {n: "pause" if i == playing else "play" for i, n in enumerate(names)})
            return SD.SpotifyRead(status="ok", built=True, player=True, slider=rng.uniform(0, 100),
                                  slider_source="shape", grid=True, rows=rows, links=links,
                                  label_source="none", elements=300, ms=150.0,
                                  candidate=cand if kw.get("session_playing") else None, raw=raw(playing))

    store, logs, moments = Store(), [], []
    mono = [100.0]
    watch = SD.SpotifyWatch(emit=moments.append, log=lambda t, **f: logs.append((t, f)), store=store,
                            game_front_fn=lambda: False, local_game_fn=lambda: False, reader=Reader(),
                            spawn=lambda fn: fn(), mono=lambda: mono[0])
    watch.on_front_change("x", "spotify")
    watch.on_media_change(started=["Spotify.exe"])
    watch.on_media_change(started=["Spotify.exe"])          # another row plays: the wording is confirmed
    assert watch.stats()["labels_learned"] == 1
    watch.on_front_change("spotify", "x")
    mono[0] += 61
    watch.beat()
    _assert_content_free(logs, names + owners + ids + ["spotify:playlist"])
    status = store.state.get(SD.STATUS_KEY, "")
    for s in names + owners + ids:
        assert s not in status
    links_raw = store.state.get(SD.LINKS_KEY, "")
    for s in names + ids:
        assert s not in links_raw          # encrypted
    labels_raw = store.state.get(SD.LABELS_KEY, "")
    assert labels_raw.startswith("enc:")
    for s in names + owners:
        assert s not in labels_raw         # encrypted (review r1 M1)
    assert not any(s in json.dumps(watch.stats()) for s in names + owners)
    assert any(t == "spotify_probe" for t, _f in logs)


@pytest.mark.parametrize("seed", range(5))
def test_tab_logs_board_and_identities_never_hold_tab_titles(seed: int) -> None:
    rng = random.Random(100 + seed)
    titles = [_word(rng) for _ in range(10)]
    pinned = El(GROUP, "", (), [El(TAB_ITEM, t, (42, seed, i), selected=i == 1) for i, t in enumerate(titles[:3])])
    normal = El(GROUP, "", (), [El(TAB_ITEM, t, (42, seed, 10 + i)) for i, t in enumerate(titles[3:])])
    root = El(PANE, "", (), [El(TOOLBAR, "", (), [pinned, normal])])
    store, logs, board = Store(), [], NowBoard()
    ev = CallEvidence(sites_fn=lambda e: {"meet.google.com": (True, "Google Meet")},
                      entries=("meet.google.com",), state=store)
    ev._judge_entries()

    def reader(hwnd: int, *, digest_fn: Any) -> dict:
        from yuki.memory.extract import uia

        return uia.tab_identities(hwnd, digest_fn=digest_fn, automation_fn=lambda: Auto({hwnd: root}))

    tabs = SiteTabs(ev, store=store, board=board, log=lambda t, **f: logs.append((t, f)), reader=reader,
                    census_fn=lambda w, i, *, digest_fn: T.census_job(Auto({7: root}), w, i, digest_fn))
    tabs.seen({"process": "zen", "hwnd": 7, "at": time.time(), "host": "meet.google.com", "blocked": "blocked_host"})
    tabs.census("Google Meet", windows=[(7, 0)])
    _assert_content_free(logs, titles)
    blob = json.dumps(store.state, ensure_ascii=False) + json.dumps(board.snapshot(), ensure_ascii=False)
    for t in titles:
        assert t not in blob
    assert [f for t, f in logs if t == "tab_strip"][0]["tab_known"] is True


def test_volume_events_are_content_free() -> None:
    logs = []
    w = VolumeWatch(lambda m: None, log=lambda t, **f: logs.append((t, f)), register=False, quiet_s=0.02,
                    front_fn=lambda: "arc")
    w.start()
    try:
        w._sessions[1] = (None, None, None, 9, "spotify")
        w.feed("session", 1, 20.0, False, "", 0.0)
        w.feed("session", 1, 60.0, False, "", 0.0)
        w.feed("endpoint", 10.0, False, "{594B5549-A0D1-4C7F-9A55-59554B490001}", 0.0)
        w.feed("endpoint", 30.0, True, "{594B5549-A0D1-4C7F-9A55-59554B490001}", 0.0)
        end = time.monotonic() + 2
        while len([1 for t, _f in logs if t == "volume_event"]) < 2 and time.monotonic() < end:
            time.sleep(0.02)
    finally:
        w.stop()
    _assert_content_free(logs, [])
    assert len([1 for t, _f in logs if t == "volume_event"]) == 2
