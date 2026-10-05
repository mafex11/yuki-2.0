"""Calls on meeting sites the privacy rules keep unread (live 2026-10-05: a Google Meet call in the Zen browser
was never seen, because the privacy file blocks meet.google.com and no screen of it ever reached the judge).

The watcher hands call detection only a blocked page's app, host and time, in memory. Which hosts are meeting
sites is decided from the privacy file's own lists, never per visit: the shipped list of blocked sites is judged
once as a whole (a stub here; the real one is a model), the user's [meetings] always_sites count by their word,
and a site the user blocked themselves never counts. A sighting of a calling site during the microphone hold
starts a call, the Ask card still decides recording, and the call lives by the site call's rules (review fixes
F1-F5 of 2026-10-05). Invented data, temp databases and stubbed device holders, windows and page reads only:
nothing reads the desktop, the microphone or the speakers, and the recorder's source factory fails the test if
anything tries to open the microphone.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import yuki.memory.callevidence as callevidence
from test_av_signals import DISCORD, Stubs, _facts, _recorder, _worker
from yuki.memory.callevidence import SITES_PROMPT, SITES_TOOL, CallEvidence, Verdict, shipped_site_entries

ZEN = r"C:\Program Files\Zen Browser\zen.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
ARC = r"C:\Users\someone\AppData\Local\Microsoft\WindowsApps\Arc.exe"

YOUTUBE = ("Search", "Home", "Shorts", "Subscriptions")
JOINED = ("Leave call", "Turn off microphone", "People", "Meeting details")
TEAMS_LOBBY = ("Join now", "Choose your video and audio options", "Computer audio")

#: Stand-in for the judge of the shipped site list (the real one is a model reading the list only).
CALLING = {"meet.google.com": "Google Meet", "zoom.us": "Zoom", "teams.microsoft.com": "Microsoft Teams",
           "teams.live.com": "Microsoft Teams"}


def sites_judge(entries):
    return {e: (e in CALLING, CALLING.get(e, "")) for e in entries}


def screen_judge(screen) -> Verdict:
    joined = "Leave call" in screen.controls
    return Verdict(in_call=joined, service="Google Meet" if joined else "")


@pytest.fixture()
def store(tmp_path):
    from yuki.memory.store import Store

    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


def worker(store, stubs, *, sites_fn=sites_judge, privacy=None, **cfg):
    w = _worker(store, stubs, **cfg)
    w.evidence = CallEvidence(judge_fn=screen_judge, sites_fn=sites_fn, rejudge_s=0.0, privacy=privacy,
                              fresh_s=cfg.get("evidence_fresh_s", 900.0))
    w.sites_fn = lambda key: 8   # every app here shows many sites: a browser
    return w


def key_of(exe: str) -> str:
    return exe.rsplit("\\", 1)[-1].rsplit(".", 1)[0].casefold()


def sighting(w, exe: str, at: float, host: str = "meet.google.com", reason: str = "blocked_host") -> None:
    """What the watcher hands over for a page the privacy rules kept unread: app, host, time only."""
    ev = w.evidence if hasattr(w, "evidence") else w
    ev.seen({"process": key_of(exe), "exe": exe, "hwnd": 7, "at": at, "host": host, "blocked": reason})


def see(w, exe: str, at: float, controls, host=None) -> None:
    w.evidence.seen({"process": key_of(exe), "exe": exe, "hwnd": 7, "at": at, "host": host, "title": "",
                     "controls": list(controls)})


def privacy_file(tmp_path, text: str):
    from yuki.memory.privacy import PrivacyConfig

    path = tmp_path / "privacy.toml"
    path.write_text(text, encoding="utf-8")
    return PrivacyConfig(path)


# -- the live case: a Meet call in Zen ---------------------------------------------------------------------------


def test_a_meet_call_in_zen_on_the_blocked_site_starts_a_call_row_that_asks(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 60)        # Zen took the microphone for the meeting
    w = worker(store, stubs)
    sighting(w, ZEN, now - 30)
    w.step(now)
    assert w.active is not None and w.active.key == "zen" and w.active.via == "screen"
    row = store.call_record(w.active.call_id)
    assert row.app == "Google Meet" and row.service == "Google Meet" and row.status == "asking"
    assert row.consent is None             # the card decides; nothing is recorded before an answer


def test_the_shipped_privacy_list_names_the_meeting_sites_of_the_live_case():
    entries = shipped_site_entries()
    assert {"meet.google.com", "zoom.us", "teams.microsoft.com", "teams.live.com"} <= set(entries)


def test_a_lobby_false_start_costs_one_card_and_never_is_kept_for_the_service(store):
    """A Meet lobby holds the microphone too and is the same host: the row starts, the card asks. "Never" on it is
    the service's standing answer, not the browser's."""
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 20)
    w = worker(store, stubs)
    sighting(w, ZEN, now - 10)
    w.step(now)
    first = w.active.call_id
    assert store.answer_call(first, "never")
    assert store.call_pref("Google Meet") == "never" and store.call_pref("zen") is None
    stubs.mic.clear()
    for t in (5, 100, 200):
        w.step(now + t)
    assert w.active is None
    stubs.hold("mic", ZEN, now + 300)       # the next Meet call
    sighting(w, ZEN, now + 305)
    w.step(now + 306)
    row = store.call_record(w.active.call_id)
    assert w.active.call_id != first and row.consent == "never" and row.status == "declined"


def test_a_browser_holding_the_mic_for_youtube_is_still_not_a_call(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 3600)
    w = worker(store, stubs)
    see(w, ZEN, now - 5, YOUTUBE, host="www.youtube.com")   # a readable page: judged as a screen
    for t in (0, 5, 600):
        w.step(now + t)
        assert w.active is None


def test_a_sighting_before_the_hold_is_not_evidence(store):
    now = time.time()
    stubs = Stubs()
    w = worker(store, stubs)
    sighting(w, ZEN, now - 3600)            # an hour-old Meet tab
    stubs.hold("mic", ZEN, now - 30)        # a new hold (a voice search)
    w.step(now)
    assert w.active is None


@pytest.mark.parametrize("host", ["paypal.com", "accounts.google.com", "netbanking.examplebank.com"])
def test_a_blocked_site_that_is_no_calling_service_starts_nothing(store, host):
    """paypal.com and accounts.google.com are shipped entries judged not calling; the bank is one the user added
    themselves: it never counts and is never judged."""
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 60)
    w = worker(store, stubs)
    sighting(w, ZEN, now - 5, host=host)
    for t in (0, 30):
        w.step(now + t)
        assert w.active is None


def test_the_site_call_ends_by_the_release_grace_when_the_browser_plays_nothing(store):
    """Once the app lets go and plays no sound (a "You left the meeting" page left in front), sightings of the
    site do not keep the call alive: release grace, then the end grace."""
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 60)
    w = worker(store, stubs, release_grace_s=90.0, end_grace_s=30.0)
    sighting(w, ZEN, now - 30)
    w.step(now)
    call_id = w.active.call_id
    w.step(now + 60)
    stubs.mic.clear()                       # Zen let go of the microphone
    for t in (65, 100, 140):
        sighting(w, ZEN, now + t)           # the page is still in front, read (or skipped) again
        w.step(now + t + 1)
        assert w.active is not None and w.active.call_id == call_id
    for t in (170, 200):
        sighting(w, ZEN, now + t)
        w.step(now + t + 1)
    assert w.active is None
    row = store.call_record(call_id)
    assert row.status == "ended" and row.stop_reason == "mic_released"


def test_a_muted_meet_call_goes_on_while_the_browser_plays_the_others(store):
    """Review F3: Meet releases the microphone on mute; the others keep talking (the browser plays sound) and the
    site is still seen: one call, one row, no second card. When the sound stops, it ends by the grace."""
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 60)
    stubs.play(4242, ZEN)                   # the meeting's sound
    w = worker(store, stubs, release_grace_s=90.0, end_grace_s=30.0)
    sighting(w, ZEN, now - 30)
    w.step(now)
    call_id = w.active.call_id
    stubs.mic.clear()                       # muted
    for t in range(5, 205, 5):
        sighting(w, ZEN, now + t)
        w.step(now + t)
        assert w.active is not None and w.active.call_id == call_id
    assert w.active.alive_by == "site_sound"
    stubs.hold("mic", ZEN, now + 210)       # unmuted: the same call
    w.step(now + 211)
    assert w.active.call_id == call_id
    stubs.mic.clear()
    stubs.render.clear()                    # left: the page plays nothing
    for t in (215, 260, 320, 340):
        w.step(now + t)
    assert w.active is None and store.call_record(call_id).stop_reason == "mic_released"


def test_a_muted_site_call_is_kept_by_sound_only_while_the_site_was_seen_within_screen_alive(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 60)
    stubs.play(4242, ZEN)
    w = worker(store, stubs, release_grace_s=90.0, end_grace_s=30.0, screen_alive_s=300.0)
    sighting(w, ZEN, now - 30)
    w.step(now)
    call_id = w.active.call_id
    stubs.mic.clear()                       # the browser goes on playing (another tab), the site is not seen again
    for t in range(10, 260, 10):
        w.step(now + t)
        assert w.active is not None and w.active.call_id == call_id
    for t in range(300, 400, 10):
        w.step(now + t)
    assert w.active is None


def test_a_regrab_inside_the_grace_is_the_same_call(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 60)
    w = worker(store, stubs, release_grace_s=90.0)
    sighting(w, ZEN, now - 30)
    w.step(now)
    call_id = w.active.call_id
    stubs.mic.clear()
    w.step(now + 30)
    stubs.hold("mic", ZEN, now + 50)        # unmuted 50 s later
    w.step(now + 55)
    assert w.active is not None and w.active.call_id == call_id


def test_a_site_evidence_goes_stale_and_is_renewed_by_another_sighting(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 60)
    w = worker(store, stubs, evidence_fresh_s=300.0)
    sighting(w, ZEN, now - 30)
    w.step(now)
    call_id = w.active.call_id
    w.step(now + 400)
    assert w.active.call_id == call_id and w.active.via == "screen_stale"
    sighting(w, ZEN, now + 401)
    w.step(now + 402)
    assert w.active.via == "screen"


@pytest.mark.parametrize("exe,host,service", [
    (CHROME, "meet.google.com", "Google Meet"),
    (EDGE, "meet.google.com", "Google Meet"),
    (ARC, "meet.google.com", "Google Meet"),
    (CHROME, "app.zoom.us", "Zoom"),
    (EDGE, "zoom.us", "Zoom"),
    (ARC, "teams.microsoft.com", "Microsoft Teams"),
])
def test_other_browsers_on_meeting_sites(store, exe, host, service):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", exe, now - 40)
    w = worker(store, stubs)
    sighting(w, exe, now - 10, host=host, reason="blocked_path" if "teams" in host else "blocked_host")
    w.step(now)
    assert w.active is not None and w.active.key == key_of(exe)
    assert store.call_record(w.active.call_id).service == service


# -- review F2: a readable lobby after the blocked join link -----------------------------------------------------


def test_a_readable_not_in_call_screen_of_the_same_site_after_the_sighting_starts_nothing(store):
    """Teams: the blocked meetup-join link is a sighting, then the readable pre-join lobby (holding the
    microphone) is judged not in a call: no row starts."""
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 60)
    w = worker(store, stubs)
    sighting(w, ZEN, now - 40, host="teams.microsoft.com", reason="blocked_path")
    see(w, ZEN, now - 20, TEAMS_LOBBY, host="teams.microsoft.com")
    for t in (0, 5, 30):
        w.step(now + t)
        assert w.active is None
    see(w, ZEN, now + 40, JOINED, host="teams.microsoft.com")   # joined: the readable screen starts it
    w.step(now + 41)
    assert w.active is not None


def test_a_site_call_ends_on_a_persistent_run_of_readable_not_in_call_screens_of_its_site(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 60)
    w = worker(store, stubs, neg_frames=3, neg_span_s=120.0)
    sighting(w, ZEN, now - 30, host="teams.microsoft.com", reason="blocked_path")
    w.step(now)
    call_id = w.active.call_id
    for t in (10, 70, 140):                 # the readable "call ended" page of the same site, re-read
        see(w, ZEN, now + t, ("Rejoin", "Dismiss"), host="teams.microsoft.com")
        w.step(now + t + 1)
    assert w.active is None and store.call_record(call_id).stop_reason == "screen_contradicts"


# -- review F4: one glance at a meeting page during a long unrelated hold ----------------------------------------


def test_one_glance_at_a_meeting_page_during_a_long_hold_ends_after_screen_alive_of_other_pages(store):
    t0 = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, t0)              # a tab with microphone permission holds it all along
    w = worker(store, stubs, screen_alive_s=900.0)
    see(w, ZEN, t0 + 100, YOUTUBE, host="www.youtube.com")
    w.step(t0 + 101)
    sighting(w, ZEN, t0 + 200)              # one glance at a meet.google.com page
    w.step(t0 + 201)
    call_id = w.active.call_id
    t = 230
    while t <= 1080:
        see(w, ZEN, t0 + t, YOUTUBE, host="www.youtube.com")
        w.step(t0 + t + 1)
        assert w.active is not None and w.active.call_id == call_id
        t += 30
    for t in range(1110, 2000, 30):         # past screen_alive_s since the glance: over, and no new row
        see(w, ZEN, t0 + t, YOUTUBE, host="www.youtube.com")
        w.step(t0 + t + 1)
    assert w.active is None and store.call_record(call_id).stop_reason == "screen_contradicts"
    sighting(w, ZEN, t0 + 2100)             # a real meeting later in the same hold: new evidence, a new row
    w.step(t0 + 2101)
    assert w.active is not None and w.active.call_id != call_id


def test_a_meet_call_goes_on_while_another_tab_is_looked_at_within_screen_alive(store):
    t0 = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, t0)
    w = worker(store, stubs, screen_alive_s=900.0)
    sighting(w, ZEN, t0 + 10)
    w.step(t0 + 11)
    call_id = w.active.call_id
    for t in range(40, 1600, 30):
        if t in (760, 1300):
            sighting(w, ZEN, t0 + t)        # back on the meeting tab now and then
        else:
            see(w, ZEN, t0 + t, ("Docs", "Edit", "Share"), host="docs.example.com")
        w.step(t0 + t + 1)
        assert w.active is not None and w.active.call_id == call_id


# -- the evidence itself (review F1: never a visited host sent anywhere) -----------------------------------------


def test_the_shipped_list_is_judged_once_as_a_whole_and_only_for_microphone_holders():
    asked: list[tuple[str, ...]] = []

    def counting(entries):
        asked.append(tuple(entries))
        return sites_judge(entries)

    ev = CallEvidence(judge_fn=screen_judge, sites_fn=counting)
    now = time.time()
    ev.seen({"process": "zen", "exe": ZEN, "at": now, "host": "meet.google.com", "blocked": "blocked_host"})
    ev.seen({"process": "chrome", "exe": CHROME, "at": now, "host": "netbanking.examplebank.com",
             "blocked": "blocked_host"})
    assert ev.judge(["discord"]) == 0 and asked == []                 # neither browser holds the microphone
    assert ev.judge(["zen"]) == 1 and asked == [shipped_site_entries()]
    ev.seen({"process": "zen", "exe": ZEN, "at": now + 5, "host": "meet.google.com", "blocked": "blocked_host"})
    ev.seen({"process": "chrome", "exe": CHROME, "at": now + 5, "host": "meet.google.com", "blocked": "blocked_host"})
    ev.seen({"process": "chrome", "exe": CHROME, "at": now + 6, "host": "accounts.google.com",
             "blocked": "blocked_host"})
    # the bank (the user's own entry) and the sign-in site are decided without asking again
    assert ev.judge(["zen", "chrome"]) == 2 and len(asked) == 1
    assert all("examplebank" not in e for e in asked[0])
    assert ev.state("zen", now - 10, now + 6) == ("fresh", "Google Meet")
    assert ev.state("chrome", now - 10, now + 7) == ("fresh", "Google Meet")
    assert ev.state("zen", now - 10, now + 5 + 901) == ("stale", "Google Meet")
    assert ev.state("zen", now + 100, now + 101) == (None, "")          # a later hold needs a new sighting
    assert ev.host_of("zen") == "meet.google.com" and ev.others_of("zen") is None
    life = ev.life("zen")
    assert life.site and life.positive_at == now + 5 and life.negatives == () and life.away_at is None


def test_the_model_is_sent_the_shipped_list_never_a_visited_host():
    sent = []

    class Client:
        class messages:  # noqa: N801 - the SDK's shape
            @staticmethod
            def create(**kw):
                sent.append(kw)
                items = [{"host": "meet.google.com", "calling": True, "service": "Google Meet"},
                         {"host": "paypal.com", "calling": False, "service": ""}]
                block = SimpleNamespace(type="tool_use", input={"sites": items})
                return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    settings = SimpleNamespace(estimate_cost=lambda model, usage: 0.0001)
    ev = CallEvidence(judge_fn=screen_judge, client=Client(), settings=settings, model="stub-model")
    now = time.time()
    for host in ("netbanking.examplebank.com", "accounts.google.com", "meet.google.com"):
        ev.seen({"process": "zen", "exe": ZEN, "at": now, "host": host, "blocked": "blocked_host"})
    assert ev.judge(["zen"]) == 3
    assert len(sent) == 1
    content = sent[0]["messages"][0]["content"]
    assert content == "sites:\n" + "\n".join(shipped_site_entries())
    assert "examplebank" not in content
    assert sent[0]["system"] == SITES_PROMPT and sent[0]["tools"] == [SITES_TOOL]
    assert ev.state("zen", now - 1, now + 1) == ("fresh", "Google Meet")


def test_the_users_always_and_never_sites_decide_without_the_model(tmp_path):
    privacy = privacy_file(tmp_path, '[web]\nblocked_host_suffixes = ["meet.example.org", "meet.google.com"]\n'
                                     '[meetings]\nalways_sites = ["meet.example.org"]\nnever_sites = ["meet.google.com"]\n'
                                     '[meetings.names]\n"meet.example.org" = "Example Meet"\n')
    ev = CallEvidence(judge_fn=screen_judge, sites_fn=pytest.fail, privacy=privacy)
    now = time.time()
    sighting(ev, ZEN, now, host="rooms.meet.example.org")
    sighting(ev, CHROME, now, host="meet.google.com")
    assert ev.judge(["zen", "chrome"]) == 2
    assert ev.state("zen", now - 1, now + 1) == ("fresh", "Example Meet")
    assert ev.state("chrome", now - 1, now + 1) == (None, "")


def test_a_sighting_keeps_nothing_of_the_page():
    ev = CallEvidence(judge_fn=pytest.fail, sites_fn=sites_judge)
    now = time.time()
    ev.seen({"process": "zen", "exe": ZEN, "at": now, "host": "meet.google.com", "blocked": "blocked_host",
             "title": "Meet - invented-room", "controls": ["Leave call"]})   # even if a caller passed them
    assert ev.stats()["screens"] == 0 and ev.stats()["sites"] == 1
    assert ev.judge(["zen"]) == 1                    # the screen judge (pytest.fail) is never asked
    assert ev.state("zen", now - 5, now + 1) == ("fresh", "Google Meet")


def test_a_failed_list_judgement_is_tried_again_later(monkeypatch):
    calls = []

    def flaky(entries):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("model timed out")
        return sites_judge(entries)

    ev = CallEvidence(judge_fn=screen_judge, sites_fn=flaky)
    now = time.time()
    sighting(ev, ZEN, now)
    assert ev.judge(["zen"]) == 0 and ev.state("zen", now - 5, now + 1) == (None, "")
    assert ev.judge(["zen"]) == 0 and len(calls) == 1          # not again before SITES_RETRY_S
    monkeypatch.setattr(callevidence, "SITES_RETRY_S", 0.0)
    assert ev.judge(["zen"]) == 1 and ev.state("zen", now - 5, now + 1) == ("fresh", "Google Meet")


def test_a_readable_in_call_screen_after_the_site_keeps_the_screen_rules():
    """A readable in-call frame newer than the site sighting is the call's life (negatives, health): not a site."""
    ev = CallEvidence(judge_fn=screen_judge, sites_fn=sites_judge)
    now = time.time()
    ev.seen({"process": "chrome", "exe": CHROME, "at": now, "host": "meet.google.com", "blocked": "blocked_host"})
    ev.seen({"process": "chrome", "exe": CHROME, "at": now + 5, "host": "teams.microsoft.com",
             "title": "", "controls": list(JOINED)})
    ev.judge(["chrome"])
    life = ev.life("chrome")
    assert not life.site and life.positive_at == now + 5 and life.host == "teams.microsoft.com"


# -- the watcher: a blocked page's host only ---------------------------------------------------------------------


def _watcher(**rules):
    from yuki.memory.privacy import DEFAULT_PATH, PrivacyConfig, PrivacyRules
    from yuki.memory.watcher import Watcher

    w = Watcher(lambda: None, privacy=PrivacyConfig(DEFAULT_PATH))   # the shipped rules, never the user's file
    if rules:
        base = w.privacy.rules()
        fixed = PrivacyRules(**{**base.__dict__, **rules})
        w.privacy.rules = lambda: fixed   # type: ignore[method-assign]
    got: list[dict] = []
    w.on_screen = got.append
    return w, got


def _page(url: str, title: str = "Meet - invented-room"):
    from yuki.memory.extract.model import Extraction

    return Extraction(kind="page", app="Zen", title=title, url=url, thread_scope="",
                      controls=list(JOINED), body="Invented Person: hello")


@pytest.mark.parametrize("exe,url,host", [
    (ZEN, "https://meet.google.com/abc-defg-hij", "meet.google.com"),
    (CHROME, "https://meet.google.com/abc-defg-hij?authuser=0", "meet.google.com"),
    (EDGE, "https://app.zoom.us/wc/123456789/join", "app.zoom.us"),
    (ARC, "https://zoom.us/wc/join/123456789", "zoom.us"),
    (CHROME, "https://teams.microsoft.com/l/meetup-join/19%3ameeting_invented/0", "teams.microsoft.com"),
])
def test_the_watcher_hands_a_blocked_meeting_page_as_its_host_only(exe, url, host):
    from yuki.memory.watcher import ProcessFacts

    w, got = _watcher()
    facts = ProcessFacts(1, exe.rsplit("\\", 1)[-1], exe, "Browser", False)
    w._tell_screen(5, facts, _page(url), w.privacy.rules())
    assert len(got) == 1
    assert set(got[0]) == {"process", "exe", "hwnd", "at", "host", "blocked"}
    assert got[0]["host"] == host and got[0]["process"] == key_of(exe) and got[0]["exe"] == exe
    assert got[0]["blocked"] in ("blocked_host", "blocked_path")


def test_a_skip_that_does_not_read_again_still_tells_the_site_and_a_new_page_forgets_it():
    from yuki.memory.watcher import ProcessFacts

    w, got = _watcher()
    facts = ProcessFacts(1, "zen.exe", ZEN, "Zen", False)
    w._tell_screen(5, facts, _page("https://meet.google.com/abc-defg-hij"), w.privacy.rules())
    w._note_blocked(5, "Meet - invented-room", "skipped", "blocked_host")
    w._tell_site_again(5)                    # what a "blocked_host_before" skip calls
    assert len(got) == 2 and got[1]["host"] == "meet.google.com" and "title" not in got[1]
    w._note_blocked(5, "Docs", "captured", "")   # the window moved on to a readable page
    w._tell_site_again(5)
    assert len(got) == 2


def _stub_desktop(monkeypatch, w, exe: str, title: str, url: str, *, foreground: int):
    """Stub every desktop read the watcher's two read paths make: windows, processes, layout and the page."""
    import yuki.memory.watcher as wmod
    from yuki.memory.watcher import ProcessFacts

    facts = ProcessFacts(1, exe.rsplit("\\", 1)[-1], exe, "Browser", False)
    state = {"url": url, "title": title}
    monkeypatch.setattr(wmod, "_user32", SimpleNamespace(IsWindow=lambda h: 1, GetForegroundWindow=lambda: foreground,
                                                         PostMessageW=lambda *a: 1))
    monkeypatch.setattr(wmod, "_visibility", SimpleNamespace(layout=lambda **kw: SimpleNamespace(
        by_hwnd=lambda h: SimpleNamespace(visible_share=1.0), covered=lambda h, b: False)))
    monkeypatch.setattr(wmod, "_window_pid", lambda h: 1)
    monkeypatch.setattr(wmod, "_window_title", lambda h: state["title"])
    monkeypatch.setattr(wmod, "_uia_threads_alive", lambda: 0)
    monkeypatch.setattr(wmod, "is_user_window", lambda h: True)
    monkeypatch.setattr(wmod, "is_fullscreen_front", lambda h: False)
    monkeypatch.setattr(wmod, "extract", lambda hwnd, **kw: _page(state["url"], state["title"]))
    w._processes = SimpleNamespace(get=lambda pid: facts)
    w._probe = None
    w.user_names = lambda store: []
    w._measure_opaque = lambda *a: False
    return state


def test_the_background_read_path_tells_the_site_then_its_before_skip_tells_it_again(monkeypatch):
    """Review F5: the real background read (_capture_visible) with a stubbed page read: the first read is skipped
    as blocked_host and hands the host; the next one (same title) is skipped without reading and hands it again."""
    from yuki.memory.watcher import _BgJob

    w, got = _watcher()
    state = _stub_desktop(monkeypatch, w, ZEN, "Meet - invented-room", "https://meet.google.com/abc-defg-hij",
                          foreground=999)
    store = SimpleNamespace()
    first = w._capture_visible(store, _BgJob(hwnd=5, reason="layout"))
    assert (first.outcome, first.reason) == ("skipped", "blocked_host")
    assert len(got) == 1 and got[0]["host"] == "meet.google.com" and set(got[0]) == {
        "process", "exe", "hwnd", "at", "host", "blocked"}
    second = w._capture_visible(store, _BgJob(hwnd=5, reason="layout"))
    assert (second.outcome, second.reason) == ("skipped", "blocked_host_before")
    assert len(got) == 2 and got[1]["host"] == "meet.google.com"
    state["url"], state["title"] = "https://docs.example.com/d/1", "Notes"   # moved on to a readable page
    w._capture_visible(store, _BgJob(hwnd=5, reason="layout"))
    w._capture_visible(store, _BgJob(hwnd=5, reason="layout"))
    assert all(g.get("blocked") is None for g in got[2:]) and sum(1 for g in got if g.get("blocked")) == 2


def test_the_foreground_read_path_tells_the_site_then_its_before_skip_tells_it_again(monkeypatch):
    from yuki.memory.watcher import _Request

    w, got = _watcher()
    _stub_desktop(monkeypatch, w, ZEN, "Meet - invented-room", "https://meet.google.com/abc-defg-hij", foreground=5)
    store = SimpleNamespace()
    first = w._capture(store, _Request(hwnd=5, trigger="title"))
    assert (first.outcome, first.reason) == ("skipped", "blocked_host")
    assert len(got) == 1 and got[0]["host"] == "meet.google.com" and "title" not in got[0]
    second = w._capture(store, _Request(hwnd=5, trigger="focus"))
    assert (second.outcome, second.reason) == ("skipped", "blocked_host_before")
    assert len(got) == 2 and got[1]["host"] == "meet.google.com"


@pytest.mark.parametrize("rules,url", [
    ({"adult_host_suffixes": frozenset({"adult.example"})}, "https://www.adult.example/v/1"),
    ({"user_blocked_domains": frozenset({"private.example"})}, "https://private.example/room"),
    ({}, "about:preferences"),
])
def test_other_privacy_reasons_hand_nothing(rules, url):
    from yuki.memory.watcher import ProcessFacts

    w, got = _watcher(**rules)
    facts = ProcessFacts(1, "zen.exe", ZEN, "Zen", False)
    w._tell_screen(5, facts, _page(url), w.privacy.rules())
    assert got == []


def test_a_readable_page_is_still_handed_as_a_screen():
    from yuki.memory.watcher import ProcessFacts

    w, got = _watcher()
    facts = ProcessFacts(1, "Discord.exe", DISCORD, "Discord", False)
    w._tell_screen(5, facts, _page("https://discord.com/channels/1", title="#general"), w.privacy.rules())
    assert len(got) == 1 and "blocked" not in got[0] and got[0]["controls"] == list(JOINED)


# -- the timeline's meeting row (review F5) ----------------------------------------------------------------------


def _page_result(rec, url: str, title: str, now: float) -> None:
    st = rec._current
    rec._page_results.append((st.seq, st.hwnd, (title, url), 1.0, None))
    rec._take_pages(now)


def test_the_timeline_keeps_a_blocked_meet_page_in_zen_as_a_meeting_of_its_site(store, tmp_path):
    t0 = time.time()
    stubs = Stubs()
    rec = _recorder(store, tmp_path, stubs, {2: (20, _facts(20, ZEN, "Zen Browser"), "Meet - invented-room")},
                    "[meetings]\nhold_s = 20\nend_grace_s = 0\n")
    ev = CallEvidence(judge_fn=screen_judge, sites_fn=sites_judge)
    rec._evidence = ev
    rec._probe = SimpleNamespace(submit=lambda seq, hwnd: True)
    rec._input_at = t0
    rec._open(2, t0)
    _page_result(rec, "https://meet.google.com/abc-defg-hij", "Meet - invented-room", t0)
    stubs.hold("mic", ZEN, t0 + 2)
    sighting(ev, ZEN, t0 + 3)
    ev.judge(["zen"])
    rec._poll_av(t0 + 30)
    rec._check_meeting(t0 + 30)
    st = rec._current
    assert st.meeting == "meet.google.com" and st.title == "" and st.path is None


@pytest.mark.parametrize("host,meeting", [("meet.google.com", True), ("paypal.com", False)])
def test_the_timeline_takes_a_browser_with_an_unknown_page_for_a_meeting_only_on_a_calling_site(
        store, tmp_path, host, meeting):
    """A browser whose page the timeline could not read (no page answer) holds the microphone: its call evidence
    (CallEvidence.state, here a site sighting) decides, as for any app."""
    t0 = time.time()
    stubs = Stubs()
    rec = _recorder(store, tmp_path, stubs, {2: (20, _facts(20, ZEN, "Zen Browser"), "Zen Browser")},
                    "[meetings]\nhold_s = 20\nend_grace_s = 0\n")
    ev = CallEvidence(judge_fn=screen_judge, sites_fn=sites_judge)
    rec._evidence = ev
    rec._input_at = t0
    stubs.hold("mic", ZEN, t0 + 1)
    rec._open(2, t0)
    sighting(ev, ZEN, t0 + 3, host=host)
    ev.judge(["zen"])
    for dt in (30, 60):
        rec._poll_av(t0 + dt)
        rec._check_meeting(t0 + dt)
    assert (rec._current.meeting is not None) is meeting
    assert (rec.counters["mic_no_call"] == 0) is meeting
