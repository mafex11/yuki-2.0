"""fMoments (plan rev 4, Phase F item 4; D1, D7, D18): the moments table, its derived kinds and the meeting-join rule.

Offline: temp databases with invented rows (app keys, times, counts; no real names), stubbed game facts. Nothing reads
the desktop, a microphone or the real memory.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from yuki.memory import moments as M
from yuki.memory.store import MIGRATIONS, Store, TimelineRow

GAMES = {"valorant-win64-shipping"}
T0 = 1_790_000_000.0   # an invented epoch (late Sept 2026)


def is_game(process: str) -> bool:
    return (process or "").casefold() in GAMES


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


def call(store, process, via, at, *, app=None, end=None, service=None, kind="call", hold=None):
    cid = store.start_call(app=app or process.title(), process=process, via=via, service=service, at=at,
                           hold_since=hold, kind=kind)
    if end is not None:
        store.update_call(cid, ended_at=end, status="ended", last_alive_at=end)
    return cid


def tl(store, process, start, end, *, host=None, meeting=None, mic_s=0.0, state="present", by_yuki=False,
       path=None, active=None, app=None, withheld=None, title=None):
    row = TimelineRow(id=None, started_at=start, ended_at=end, state=state, process=process,
                      app=app or process.title(), title=title, host=host, path=path, page_key=None,
                      active_s=(end - start) if active is None else active, passive_s=0.0, away_s=0.0, media_s=0.0,
                      media_other={}, withheld=withheld, segments=1, fullscreen=False, meeting=meeting, mic_s=mic_s,
                      by_yuki=by_yuki, yuki_request=None)
    return store.save_timeline(row)


def extractor(store, **kw):
    kw.setdefault("is_game", is_game)
    kw.setdefault("runs_fn", lambda lo, hi: [])
    return M.Extractor(store, clock=lambda: T0 + 30 * 86400, **kw)


def joins(store, app=None):
    return [m for m in store.moments_between(kinds=["call_join"]) if app is None or m["app"] == app]


# -- the table (migration 40) ------------------------------------------------------------------------------------------


def test_migration_40_adds_the_moments_table_and_is_safe_to_open_again(tmp_path):
    s = Store.open(tmp_path / "m.db")
    s.close()
    s = Store.open(tmp_path / "m.db")   # a second open (the other process) changes nothing
    try:
        assert s._query("PRAGMA user_version")[0][0] == len(MIGRATIONS)
        cols = {r[1] for r in s._query("PRAGMA table_info(moments)")}
        assert set(Store.MOMENT_COLUMNS) | {"id"} == cols
        idx = {r[1] for r in s._query("PRAGMA index_list(moments)")}
        assert {"moments_at", "moments_kind"} <= idx
        assert {"timeline_updated", "timeline_meeting_rows"} <= {r[1] for r in s._query("PRAGMA index_list(timeline)")}
    finally:
        s.close()


def test_save_moments_replaces_by_source_key_and_partial_rows_only_end_a_span(store):
    row = M.Moment(kind="media_play", at=T0, source="media", source_id=1, app="spotify", entity="a song",
                   evidence="poll").row(store.cipher)
    assert store.save_moments([row]) == 1
    first = store.moments_between()[0]
    assert first["entity_ciphertext"] is None and len(first["entity_digest"]) == 64   # a title is never kept
    store.save_moments([dict(row, value=3.0)])
    again = store.moments_between()
    assert len(again) == 1 and again[0]["id"] == first["id"] and again[0]["seen_at"] == first["seen_at"]
    store.save_moments([M.Moment(kind="media_play", at=T0, source="media", source_id=1, ended_at=T0 + 9,
                                 partial=True).row(store.cipher)])
    assert store.moments_between()[0]["ended_at"] == T0 + 9
    store.save_moments([M.Moment(kind="media_play", at=T0, source="media", source_id=99, ended_at=T0 + 9,
                                 partial=True).row(store.cipher)])
    assert len(store.moments_between()) == 1    # an end with no start writes nothing
    assert store.moments_counts()["kinds"] == {"media_play": 1}
    assert store.prune_moments(T0 + 10) == 1 and store.moments_between() == []


def test_moments_between_filters(store):
    rows = [M.Moment(kind=k, at=T0 + i, source="calls", source_id=i, app=a).row(store.cipher)
            for i, (k, a) in enumerate([("call_join", "zen"), ("call_join", "discord"), ("game_voice", "x")])]
    store.save_moments(rows)
    assert [m["app"] for m in store.moments_between(kinds=["call_join"])] == ["zen", "discord"]
    assert [m["app"] for m in store.moments_between(app="discord")] == ["discord"]
    assert [m["kind"] for m in store.moments_between(T0 + 1, T0 + 3)] == ["call_join", "game_voice"]
    assert store.moments_between(sources=[]) == []
    assert store.clear_moments(sources=["calls"], kinds=["game_voice"]) == 1


# -- D7: the one meeting-join rule -------------------------------------------------------------------------------------


@pytest.mark.parametrize("via, seen, want", [
    ("screen", (), (True, "screen")), ("screen_stale", (), (True, "screen")), ("meeting", (), (True, "meeting")),
    ("mic", ("zen",), (True, "hold")), ("mic", (), (False, None)), ("game", ("zen",), (False, None)),
])
def test_is_call_join(via, seen, want):
    row = {"kind": "call", "process": "zen", "via": via}
    assert M.is_call_join(row, app="zen", seen_apps=seen, is_game=is_game) == want


def test_a_game_never_joins_a_call_and_the_users_own_apps_make_holds():
    valorant = {"kind": "call", "process": "valorant-win64-shipping", "via": "screen"}
    assert M.is_call_join(valorant, app="valorant-win64-shipping", seen_apps={"valorant-win64-shipping"},
                          is_game=is_game) == (False, None)
    game_row = {"kind": "game", "process": "zen", "via": "game"}
    assert M.is_call_join(game_row, app="zen", seen_apps={"zen"}, is_game=is_game) == (False, None)
    mic = {"kind": "call", "process": "teams", "via": "mic"}
    assert M.is_call_join(mic, app="teams", seen_apps=(), is_game=is_game, calls_apps={"teams"}) == (True, "hold")
    assert M.is_game_voice({"kind": "game", "process": "valorant-win64-shipping"}, is_game=is_game)
    assert not M.is_game_voice({"kind": "game", "process": "fl64"}, is_game=is_game)   # FL Studio is no game


def test_game_judge_uses_machine_facts_only():
    judge = M.game_judge(None)
    assert judge("VALORANT-Win64-Shipping") and judge("valorant-win64-shipping.exe")
    assert not judge("fl64") and not judge("systemsettings") and not judge("zen") and not judge("")


def test_seen_call_record_is_the_apps_with_a_seen_call_and_their_services(store):
    tl(store, "slack", T0 - 3600, T0 - 3000, app="Slack")
    call(store, "zen", "screen", T0, service="Google Meet", app="Google Meet")
    call(store, "discord", "mic", T0)
    call(store, "valorant-win64-shipping", "screen", T0)
    call(store, "com.tinyspeck.slackdesktop", "screen_stale", T0, app="Slack")
    apps, services = M.seen_call_record(store, is_game=is_game)
    assert apps == {"zen", "slack"} and services == {("zen", "google meet")}


def test_call_at_is_the_hold_when_it_came_first():
    assert M.call_at(100.0, 90.0) == 90.0
    assert M.call_at(100.0, 110.0) == 100.0
    assert M.call_at(100.0, None) == 100.0


def test_seen_hold_and_never_meetings_from_calls_and_timeline(store):
    d = 86400
    # zen: a seen call with the Meet service on day 1; a mic-only call and a host-less meeting row (holds) on days 2, 3
    call(store, "zen", "screen", T0 + 60, end=T0 + 1800, service="Google Meet", app="Google Meet")
    call(store, "zen", "mic", T0 + d + 61, end=T0 + d + 1800, hold=T0 + d + 30, app="Zen")
    tl(store, "zen", T0 + 2 * d + 35, T0 + 2 * d + 44, meeting="Zen", mic_s=9.0)
    # a Meet page meeting row on day 4: its service was named by zen's seen call
    tl(store, "zen", T0 + 3 * d + 6, T0 + 3 * d + 900, host="meet.google.com", meeting="Google Meet", mic_s=800.0)
    # never meetings: FL Studio and Settings holding the mic in front, VALORANT's voice chat
    for i, p in enumerate(("fl64", "systemsettings", "valorant-win64-shipping")):
        tl(store, p, T0 + 4 * d + i * 3600, T0 + 4 * d + i * 3600 + 600, meeting=p.title(), mic_s=600.0)
    call(store, "valorant-win64-shipping", "mic", T0 + 4 * d + 7200, end=T0 + 4 * d + 9000)
    call(store, "valorant-win64-shipping", "game", T0 + 5 * d, end=T0 + 5 * d + 3600, kind="game")
    call(store, "fl64", "game", T0 + 5 * d + 7200, end=T0 + 5 * d + 9000, kind="game")
    # a page meeting row of a site no one named (discord.com, discord's own pages): not a join by itself
    tl(store, "discord", T0 + 6 * d, T0 + 6 * d + 600, host="discord.com", meeting="discord.com", mic_s=500.0)
    counts = extractor(store).rebuild()
    got = [(m["app"], m["evidence"], round(m["at"] - T0)) for m in joins(store)]
    assert got == [("zen", "screen", 60), ("zen", "hold", d + 30), ("zen", "hold", 2 * d + 35),
                   ("zen", "site", 3 * d + 6)]
    assert [m["service"] for m in joins(store)] == ["Google Meet", None, None, "Google Meet"]
    gv = store.moments_between(kinds=["game_voice"])
    assert [(m["app"], m["evidence"]) for m in gv] == [("valorant-win64-shipping", "game_fact")]
    assert counts["game_voice"] == 1


def test_holds_need_a_seen_call_on_record_and_appear_once_one_is_seen(store):
    call(store, "discord", "mic", T0, end=T0 + 600)
    tl(store, "discord", T0 + 86400, T0 + 86400 + 300, meeting="Discord", mic_s=300.0)
    ex = extractor(store)
    ex.rebuild()
    assert joins(store) == []                      # no seen Discord call yet: holds are nothing
    call(store, "discord", "screen", T0 + 2 * 86400, end=T0 + 2 * 86400 + 600)
    ex.refresh()
    assert [(m["evidence"], round(m["at"] - T0)) for m in joins(store)] == [
        ("hold", 0), ("hold", 86400), ("screen", 2 * 86400)]


def test_holds_appear_after_a_restart_when_the_first_seen_call_came_meanwhile(store):
    call(store, "discord", "mic", T0, end=T0 + 600)
    extractor(store).rebuild()
    assert joins(store) == []
    call(store, "discord", "screen", T0 + 86400, end=T0 + 86400 + 600)   # written while the service was down
    extractor(store).refresh()                                         # a new process: a fresh extractor
    assert [m["evidence"] for m in joins(store)] == ["hold", "screen"]


def test_one_join_per_meeting_from_both_sources_at_the_earliest_with_the_seen_time(store):
    # a mic hold first (09:00:00), the Meet page seen at 09:00:40, the call row (screen) at 09:01, a long meeting with
    # fragments under the 5-minute gap: one join
    base = T0 + 9 * 3600
    call(store, "zen", "mic", base + 10, hold=base, end=base + 3000, app="Zen")
    call(store, "zen", "screen", base + 60, end=base + 3000, service="Google Meet", app="Google Meet")
    tl(store, "zen", base + 40, base + 400, host="meet.google.com", meeting="Google Meet", mic_s=360.0)
    tl(store, "zen", base + 500, base + 900, host="meet.google.com", meeting="Google Meet", mic_s=400.0)
    tl(store, "zen", base + 1100, base + 2900, host="meet.google.com", meeting="Google Meet", mic_s=1800.0)
    tl(store, "zen", base + 3600 * 3, base + 3600 * 3 + 300, host="meet.google.com", meeting="Google Meet",
       mic_s=300.0)   # three hours later: another meeting
    extractor(store).rebuild()
    got = joins(store)
    assert len(got) == 2
    first = got[0]
    assert first["at"] == base and first["evidence"] == "site" and first["source"] == "calls"
    assert first["facts"]["seen_at"] == round(base + 40, 3) and first["facts"]["calls"] == 2
    assert first["facts"]["timeline"] == 3 and first["ended_at"] == base + 3000
    assert first["service"] == "Google Meet"


def test_a_join_seen_only_by_the_call_worker_is_kept(store):
    # 8 Oct: calls row 82 (via screen) and no timeline meeting row that day
    call(store, "zen", "screen", T0 + 32212, end=T0 + 34413, service="Google Meet", app="Google Meet")
    extractor(store).rebuild()
    assert [(m["evidence"], m["source"], m["at"]) for m in joins(store)] == [("screen", "calls", T0 + 32212)]


def test_a_packaged_apps_call_row_is_the_timelines_app(store):
    tl(store, "slack", T0 - 3600, T0 - 3000, app="Slack")
    call(store, "com.tinyspeck.slackdesktop", "screen", T0, end=T0 + 300, app="Slack")
    tl(store, "slack", T0 + 120, T0 + 400, meeting="Slack", mic_s=200.0, app="Slack")
    extractor(store).rebuild()
    assert [(m["app"], m["evidence"], m["facts"]["calls"], m["facts"]["timeline"]) for m in joins(store)] == [
        ("slack", "screen", 1, 1)]


def test_yukis_own_stretches_make_joins_marked_yukis(store):
    """D18: a meeting stretch begun while Yuki acted is a join marked Yuki's (never dropped: a real join seen only by
    the timeline stays); a user's join right after Yuki's stretch, in the same chain, is Yuki's too (it began inside
    the run); a user's own stretch is the user's."""
    call(store, "zen", "screen", T0 - 86400, end=T0 - 86000)
    tl(store, "zen", T0, T0 + 300, meeting="Zen", mic_s=300.0, by_yuki=True)
    call(store, "zen", "screen", T0 + 60, end=T0 + 900)
    tl(store, "zen", T0 + 7200, T0 + 7500, meeting="Zen", mic_s=300.0, state="away")
    tl(store, "zen", T0 + 9000, T0 + 9300, meeting="Zen", mic_s=300.0)
    extractor(store).rebuild()
    got = [(round(m["at"] - T0), m["by"], m["evidence"], m["facts"]["calls"]) for m in joins(store)]
    assert got == [(-86400, "user", "screen", 1), (0, "yuki", "screen", 1), (9000, "user", "hold", 0)]


def test_user_meeting_rules_decide_sites_first(store, tmp_path):
    from yuki.memory.timeline import MeetingRules

    tl(store, "zen", T0, T0 + 600, host="meet.google.com", meeting="Google Meet", mic_s=600.0)
    tl(store, "zen", T0 + 86400, T0 + 86400 + 600, host="calls.example.org", meeting="calls.example.org", mic_s=600.0)
    call(store, "zen", "screen", T0 - 86400, end=T0 - 86000, service="Google Meet")
    rules = MeetingRules.from_dict({"always_sites": ["calls.example.org"], "never_sites": ["meet.google.com"],
                                    "names": {"calls.example.org": "Example Calls"}})
    extractor(store, meeting_rules=lambda: rules).rebuild()
    assert [(m["service"], m["evidence"]) for m in joins(store) if m["source"] == "timeline"] == [
        ("Example Calls", "site")]


def test_persisted_site_verdicts_name_calling_sites(store):
    store.set_nudge_state(M.SITE_VERDICTS_STATE, json.dumps(
        {"digest": "x", "verdicts": {"meet.google.com": [True, "Google Meet"], "bank.example": [False, ""]}}))
    tl(store, "zen", T0, T0 + 600, host="meet.google.com", meeting="meet.google.com", mic_s=600.0)
    tl(store, "zen", T0 + 86400, T0 + 86400 + 600, host="www.bank.example", meeting="www.bank.example", mic_s=60.0)
    extractor(store).rebuild()
    assert [(m["service"], m["evidence"]) for m in joins(store)] == [("Google Meet", "site")]


# -- incremental refresh, rebuild ------------------------------------------------------------------------------------


def test_refresh_reads_past_its_cursor_and_ends_open_calls(store):
    ex = extractor(store)
    ex.rebuild()
    assert ex.refresh() == {"message_sent": 0, "page": 0}   # nothing new: nothing written
    cid = call(store, "zen", "screen", T0 + 100, service="Google Meet")
    counts = ex.refresh()
    assert counts["call_join"] == 1 and joins(store)[0]["ended_at"] is None
    assert ex.refresh().get("call_join", 0) == 0             # the same row again is not re-taken
    store.update_call(cid, ended_at=T0 + 900, status="ended")
    ex.refresh()
    assert joins(store)[0]["ended_at"] == T0 + 900
    assert float(store.moment_cursor("calls")) > 0


def test_a_failed_refresh_reads_the_same_rows_again(store, monkeypatch):
    ex = extractor(store)
    ex.rebuild()
    call(store, "zen", "screen", T0 + 100)
    real = ex._rebuild_joins

    def boom(*a, **k):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(ex, "_rebuild_joins", boom)
    with pytest.raises(sqlite3.OperationalError):
        ex.refresh()
    monkeypatch.setattr(ex, "_rebuild_joins", real)
    assert ex.refresh()["call_join"] == 1


def test_rebuild_is_idempotent(store):
    call(store, "zen", "screen", T0, end=T0 + 600, service="Google Meet")
    call(store, "discord", "mic", T0 + 3600, end=T0 + 4000)
    call(store, "discord", "screen_stale", T0 + 86400, end=T0 + 90000)
    tl(store, "zen", T0 + 86400 * 2, T0 + 86400 * 2 + 600, host="meet.google.com", meeting="Google Meet", mic_s=60)

    def snap():
        return sorted((m["kind"], m["app"], m["at"], m["ended_at"], m["evidence"], m["source"], m["source_id"],
                       json.dumps(m["facts"], sort_keys=True)) for m in store.moments_between())

    ex = extractor(store)
    ex.rebuild()
    one = snap()
    ex.rebuild()
    assert snap() == one and len(one) == 4


# -- pages -------------------------------------------------------------------------------------------------------------


def yt(host, path):
    """A stand-in for the sites' URL templates (connect/sites, fLive): watch pages only."""
    if host == "www.youtube.com" and path.startswith("/watch?v="):
        return ("youtube.watch", path.split("=", 1)[1])
    return None


def test_page_visits_join_present_and_away_rows_and_need_dwell_or_a_template(store):
    # one long visit split by present/away (same page key), a short templated visit, a short plain visit
    tl(store, "chrome", T0, T0 + 200, host="docs.example.com", path="/a", title="a")
    tl(store, "chrome", T0 + 200, T0 + 260, host="docs.example.com", path="/a", title="a", state="away", active=0)
    tl(store, "chrome", T0 + 260, T0 + 400, host="docs.example.com", path="/a", title="a")
    tl(store, "chrome", T0 + 400, T0 + 460, host="www.youtube.com", path="/watch?v=abc123", title="v")
    tl(store, "chrome", T0 + 460, T0 + 520, host="news.example.com", path="/b", title="b")
    tl(store, "chrome", T0 + 520, T0 + 600, host="bank.example", withheld="blocked_host", title=None)
    extractor(store, page_entity=yt).rebuild()
    pages = store.moments_between(kinds=["page"])
    assert [(p["facts"]["host"], p["value"], p["facts"].get("template")) for p in pages] == [
        ("docs.example.com", 340.0, None), ("youtube.com", 60.0, "youtube.watch")]
    long, video = pages
    assert long["facts"]["rows"] == 3 and long["ended_at"] == T0 + 400 and long["entity_ciphertext"] is None
    assert video["service"] == "youtube" and store.moment_entity(video) == "abc123"
    assert video["entity_digest"] == store.cipher.digest("moment", "page", "chrome", "abc123")


def test_a_page_visit_grows_and_yukis_visits_are_yukis(store):
    ex = extractor(store, page_entity=yt)
    ex.rebuild()
    rid = tl(store, "zen", T0, T0 + 100, host="www.youtube.com", path="/watch?v=x1", by_yuki=True)
    ex.refresh()
    first = store.moments_between(kinds=["page"])
    assert len(first) == 1 and first[0]["by"] == "yuki" and first[0]["value"] == 100.0
    store.save_timeline(TimelineRow(id=rid, started_at=T0, ended_at=T0 + 700, state="present", process="zen",
                                    app="Zen", title=None, host="www.youtube.com", path="/watch?v=x1", page_key=None,
                                    active_s=700.0, passive_s=0.0, away_s=0.0, media_s=0.0, media_other={},
                                    withheld=None, segments=1, fullscreen=False, meeting=None, mic_s=0.0,
                                    by_yuki=True, yuki_request=None))
    ex.refresh()
    again = store.moments_between(kinds=["page"])
    assert len(again) == 1 and again[0]["value"] == 700.0 and again[0]["ended_at"] == T0 + 700


def test_many_changed_page_rows_group_in_one_pass_like_row_by_row(store):
    for i in range(80):   # alternating pages: 80 visits of 60 s, every 4th on the same page as the one before
        page = f"/p{i // 2}" if i % 4 < 2 else f"/q{i}"
        tl(store, "chrome", T0 + i * 60, T0 + i * 60 + 60, host="www.youtube.com", path="/watch?v=" + page)
    ex = extractor(store, page_entity=yt)
    ex.rebuild()
    batch = sorted((p["at"], p["ended_at"], p["value"]) for p in store.moments_between(kinds=["page"]))
    ex.SEQUENTIAL_PAGES = 10_000   # row by row
    ex.rebuild()
    single = sorted((p["at"], p["ended_at"], p["value"]) for p in store.moments_between(kinds=["page"]))
    assert batch == single and len(batch) == 60


# -- messages sent ----------------------------------------------------------------------------------------------------


def _thread(store, process):
    return store._write(lambda c: c.execute(
        "INSERT INTO threads(thread_key, app, title_ciphertext, first_seen, last_seen, process) VALUES (?,?,?,?,?,?)",
        (f"k-{process}", process.title(), store.cipher.encrypt("a chat"), T0, T0, process)).lastrowid)


def _message(store, thread, at, *, me=True, status="new", n=[0]):
    n[0] += 1
    return store._write(lambda c: c.execute(
        "INSERT INTO messages(thread_id, fingerprint, content_key, is_me, first_seen, status, at, text_ciphertext)"
        " VALUES (?,?,?,?,?,?,?,?)", (thread, f"f{n[0]}", f"c{n[0]}", int(me), at, status, at - 60,
                                      store.cipher.encrypt("hello there"))).lastrowid)


def test_message_sent_from_the_users_new_messages(store):
    th = _thread(store, "discord")
    store._write(lambda c: c.execute(
        "INSERT INTO contacts(name_key, name_ciphertext, created_at, updated_at) VALUES ('n', 'x', ?, ?)", (T0, T0)))
    store._write(lambda c: c.execute(
        "INSERT INTO contact_routes(contact_id, route_key, service, kind, chat_ciphertext, thread_ids, created_at,"
        " updated_at) VALUES (1, 'r', 'discord', 'dm', 'x', ?, ?, ?)", (json.dumps([th]), T0, T0)))
    _message(store, th, T0 + 10)
    _message(store, th, T0 + 20, me=False)
    _message(store, th, T0 + 30, status="history")
    ex = extractor(store)
    ex.rebuild()
    sent = store.moments_between(kinds=["message_sent"])
    assert [(m["app"], m["at"], m["facts"]["contact_id"], m["facts"]["label_lag_s"]) for m in sent] == [
        ("discord", T0 + 10, 1, 60.0)]
    assert sent[0]["entity_ciphertext"] is None and sent[0]["entity_digest"]
    _message(store, th, T0 + 40)
    assert ex.refresh()["message_sent"] == 1
    assert ex.refresh()["message_sent"] == 0


# -- D18: Yuki's own runs ---------------------------------------------------------------------------------------------


def test_attribute_matches_app_kind_and_window_with_the_kinds_lag():
    run = M.RunWindow(request_id="worker-7", app="discord", end_kind="call_join", t0=100.0, t_done=110.0)
    m = M.Moment(kind="call_join", at=160.0, source="calls", source_id=1, app="discord")
    assert M.attribute(m, [run]).by == "yuki" and M.attribute(m, [run]).request_id == "worker-7"
    assert M.attribute(M.Moment(kind="call_join", at=171.0, source="calls", source_id=1, app="discord"), [run]).by \
        == "user"   # past t_done + 60 s
    assert M.attribute(M.Moment(kind="call_join", at=105.0, source="calls", source_id=1, app="zen"), [run]).by \
        == "user"   # another app
    assert M.attribute(M.Moment(kind="message_sent", at=105.0, source="m", source_id=1, app="discord"), [run]).by \
        == "user"   # another kind
    vol = M.RunWindow(request_id="v1", app="spotify", end_kind="volume_change", t0=100.0, t_done=101.0)
    assert M.attribute(M.Moment(kind="volume_change", at=104.5, source="volume", app="spotify", means="session"),
                       [vol]).by == "user"   # 3 s lag for Core Audio
    slider = M.RunWindow(request_id="v2", app="spotify", end_kind="volume_change", t0=100.0, t_done=101.0, value=40.0)
    late_read = M.Moment(kind="volume_change", at=900.0, source="spotify", app="spotify", means="in_app", value=40.0,
                         facts={"since": 50.0})
    assert M.attribute(late_read, [slider]).by == "yuki"          # the level the run set, read later
    assert M.attribute(replace_value(late_read, 55.0), [slider]).by == "user"
    pl = M.RunWindow(request_id="p", app="spotify", end_kind="media_context", t0=100.0, entity_digest="d1")
    ctx = M.Moment(kind="media_context", at=500.0, source="spotify", app="spotify", entity_digest="d1",
                   facts={"since": 90.0})
    assert M.attribute(ctx, [pl]).by == "yuki"
    assert M.attribute(M.Moment(kind="media_context", at=500.0, source="spotify", app="spotify", entity_digest="d2",
                                facts={"since": 90.0}), [pl]).by == "user"


def replace_value(m, value):
    from dataclasses import replace

    return replace(m, value=value)


def test_skill_runs_windows_mark_derived_joins_once_the_table_exists(store):
    assert M.skill_runs_between(store, 0, T0) == []   # phase S's table is not there yet
    store._write(lambda c: c.execute(
        "CREATE TABLE skill_runs (id INTEGER PRIMARY KEY, request_id TEXT, app TEXT, end_kind TEXT, t0 REAL,"
        " t_done REAL)"))
    store._write(lambda c: c.execute(
        "INSERT INTO skill_runs(request_id, app, end_kind, t0, t_done) VALUES ('voice-3', 'discord', 'call_join', ?, ?)",
        (T0 - 5, T0 + 2)))
    call(store, "discord", "screen", T0, end=T0 + 600)
    call(store, "discord", "screen", T0 + 86400, end=T0 + 86400 + 600)
    M.Extractor(store, is_game=is_game, clock=lambda: T0 + 2 * 86400).rebuild()
    assert [(m["by"], m["request_id"]) for m in joins(store)] == [("yuki", "voice-3"), ("user", None)]


# -- the MCP never sees the new table ----------------------------------------------------------------------------------


def test_moments_stay_out_of_the_mcp():
    src = (Path(__file__).resolve().parents[1] / "yuki" / "mcp" / "server.py").read_text(encoding="utf-8")
    assert "moments" not in src and "moment_" not in src


def test_cli_stats_on_a_copy_and_a_database_before_migration_40(tmp_path, capsys):
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE x(a)")
    conn.close()
    assert M.main(["stats", "--db", str(old)]) == 0
    assert "no moments table" in capsys.readouterr().out
    s = Store.open(tmp_path / "new.db")
    call(s, "zen", "screen", time.time() - 3600, end=time.time() - 3000)
    s.close()
    assert M.main(["rebuild", "--db", str(tmp_path / "new.db")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["call_join"] == 1 and out["table"]["kinds"] == {"call_join": 1}


# -- review round 1: a bounded rebuild, Yuki's joins, no host in a log -------------------------------------------------

_TL_INSERT = ("INSERT INTO timeline(started_at, ended_at, state, process, app, title_ciphertext, host,"
              " path_ciphertext, page_key, active_s, passive_s, away_s, media_s, media_other, withheld, segments,"
              " updated_at, fullscreen, meeting, mic_s, by_yuki) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def bulk_tl(store, rows, *, title_bytes=0):
    """Timeline rows in one transaction: ``(start, end, process, host, path, meeting, mic_s, state)`` each (updated_at =
    end, as the recorder writes it). ``title_bytes``: a stand-in title ciphertext that size (never read here)."""
    filler = "x" * title_bytes if title_bytes else None
    vals = []
    for start, end, process, host, path, meeting, mic_s, state in rows:
        key = store.timeline_key(process, host, path, None, None, meeting=meeting)
        vals.append((start, end, state, process, process.title(), filler, host, store._enc(path) if path else None, key,
                     end - start, 0.0, 0.0, 0.0, None, None, 1, end, 0, meeting, mic_s, 0))
    store._write(lambda c: c.executemany(_TL_INSERT, vals))


def history(start, n):
    """``n`` invented rows from ``start``: browser visits of 1 to 5 rows of one page (present and away), other apps in
    between, a Meet stretch every 97 rows; one long visit (40 rows) that any chunk edge cuts."""
    rows, t, i = [], start, 0
    while len(rows) < n:
        if i % 97 == 0:
            rows.append((t, t + 400, "zen", "meet.google.com", "/abc", "Google Meet", 400.0, "present"))
            t += 401
        span = 40 if i == 7 else 1 + (i * 7) % 5
        page = f"/watch?v=v{i % 23}" if i % 3 else f"/p{i}"
        for k in range(span):
            d = 30.0 + (i * 13 + k) % 120
            rows.append((t, t + d, "chrome", "www.youtube.com", page, None, 0.0, "away" if k % 2 else "present"))
            t += d + 1.0
        rows.append((t, t + 50, "code", None, None, None, 0.0, "present"))
        t += 61.0
        i += 1
    return rows[:n]


def snap_all(store):
    return sorted((m["kind"], m["app"], m["at"], m["ended_at"], m["evidence"], m["source"], m["source_id"], m["value"],
                   m["by"], m["service"], m["entity_digest"], json.dumps(m["facts"], sort_keys=True))
                  for m in store.moments_between())


def test_a_rebuild_reads_chunk_by_chunk_and_gives_what_one_read_gives(store):
    call(store, "zen", "screen", T0 + 500, end=T0 + 900, service="Google Meet")
    bulk_tl(store, history(T0, 900))
    whole = extractor(store, page_entity=yt)
    whole.READ_CHUNK = 100_000
    whole.rebuild()
    one = snap_all(store)
    assert {m[0] for m in one} == {"call_join", "page"} and len(one) > 50
    for chunk, sequential in ((7, 3), (50, 64), (333, 10_000)):   # chunk edges everywhere, span and row-by-row paths
        ex = extractor(store, page_entity=yt)
        ex.READ_CHUNK, ex.SEQUENTIAL_PAGES = chunk, sequential
        ex.rebuild()
        assert snap_all(store) == one, (chunk, sequential)
    assert ex.refresh().get("page", 0) == 0          # nothing new after the rebuild: nothing written


def test_a_rebuild_reads_only_the_window_and_only_the_columns_the_rules_use(store, monkeypatch):
    old = T0 + 30 * 86400 - 200 * 86400               # far outside the 90-day window
    bulk_tl(store, history(old, 300) + history(T0, 300), title_bytes=500)
    asked: list[tuple[str, int, list]] = []
    real = store._query

    def spy(sql, params=()):
        rows = real(sql, params)
        asked.append((sql, len(rows), rows))
        return rows

    monkeypatch.setattr(store, "_query", spy)
    ex = extractor(store, page_entity=None)
    ex.READ_CHUNK = 64
    ex.rebuild()
    timeline_reads = [(s, n, rows) for s, n, rows in asked if " FROM timeline" in s]
    assert timeline_reads and not any("SELECT *" in s or "title_ciphertext" in s for s, _n, _r in timeline_reads)
    assert not any("path_ciphertext" in s for s, _n, _r in timeline_reads)   # no template asked: no path read
    scans = [(n, rows) for s, n, rows in timeline_reads if s.startswith("SELECT updated_at, id, started_at")]
    assert scans and max(n for n, _r in scans) <= 64
    cutoff = T0 + 30 * 86400 - 90 * 86400
    assert all(float(r["ended_at"]) >= cutoff for _n, rows in scans for r in rows)
    asked.clear()
    ex2 = extractor(store, page_entity=yt)
    ex2.rebuild()
    paths = [n for s, n, _r in asked if "path_ciphertext" in s]
    assert paths and set(paths) == {1}                 # a template asked: one row's path per visit, nothing more


def test_a_rebuilds_memory_does_not_grow_with_the_history(tmp_path):
    import tracemalloc

    peaks = []
    for n in (2000, 8000):
        s = Store.open(tmp_path / f"h{n}.db")
        try:
            bulk_tl(s, history(T0 + 30 * 86400 - 80 * 86400, n), title_bytes=2000)   # 2 kB of ciphertext a row
            ex = M.Extractor(s, is_game=is_game, runs_fn=lambda lo, hi: [], clock=lambda: T0 + 30 * 86400)
            ex.READ_CHUNK = 500
            tracemalloc.start()
            ex.rebuild()
            peaks.append(tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()
        finally:
            s.close()
    assert peaks[1] < 6 * 2**20, peaks                 # every row at once would be > 16 MB of ciphertext alone
    assert peaks[1] < 1.6 * peaks[0] + 2**20, peaks    # 4x the history, about the same peak


def test_a_failed_rebuild_keeps_the_stored_cursors(store, monkeypatch):
    call(store, "zen", "screen", T0 + 100, end=T0 + 400)
    ex = extractor(store)
    ex.refresh()
    before = {n: store.moment_cursor(n) for n in ("calls", "timeline", "messages")}
    assert before["calls"] not in (None, "0")
    monkeypatch.setattr(ex, "_rebuild_joins", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("busy")))
    store.set_moment_cursor("rule", "0")
    with pytest.raises(sqlite3.OperationalError):
        ex.rebuild()
    assert {n: store.moment_cursor(n) for n in ("calls", "timeline", "messages")} == before
    assert store.moment_cursor("rule") == "0"          # the hub rebuilds again


def test_a_calling_site_with_no_product_name_gives_no_service_and_no_host_in_a_log(store, tmp_path):
    from yuki.memory.timeline import MeetingRules

    store.set_nudge_state(M.SITE_VERDICTS_STATE, json.dumps({"calls.example.org": [True, ""]}))
    tl(store, "zen", T0, T0 + 600, host="calls.example.org", meeting="calls.example.org", mic_s=600.0)
    tl(store, "zen", T0 + 86400, T0 + 86400 + 600, host="talk.example.net", meeting="talk.example.net", mic_s=60.0)
    tl(store, "zen", T0 + 2 * 86400, T0 + 2 * 86400 + 600, host="room.example.io", meeting="room.example.io",
       mic_s=60.0)
    rules = MeetingRules.from_dict({"always_sites": ["talk.example.net", "room.example.io"],
                                    "names": {"room.example.io": "Weekly Sync with Priya"}})
    lines = []
    ex = extractor(store, meeting_rules=lambda: rules, log=lambda t, **f: lines.append((t, f)))
    ex.refresh()                                          # not a rebuild: each new join logs a line
    assert [(m["service"], m["evidence"]) for m in joins(store)] == [
        (None, "site"), (None, "site"), ("Weekly Sync with Priya", "site")]
    text = json.dumps(lines)
    assert len([t for t, _f in lines if t == "moment"]) == 3
    for secret in ("calls.example.org", "talk.example.net", "room.example.io", "Weekly Sync with Priya"):
        assert secret not in text
