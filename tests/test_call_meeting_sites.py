"""Calls on meeting sites the privacy rules keep unread (live 2026-10-05: a Google Meet call in the Zen browser
was never seen, because the privacy file blocks meet.google.com and no screen of it ever reached the judge).

The watcher hands call detection only a blocked page's app, host and time; the site is judged once from its host
(a stub here; the real one is a model); a sighting of a calling site during the microphone hold starts a call,
the Ask card still decides recording, and the call lives by the microphone hold alone. Invented data, temp
databases and stubbed device holders only: nothing reads the desktop, the microphone or the speakers, and the
recorder's source factory fails the test if anything tries to open the microphone.
"""

from __future__ import annotations

import time

import pytest

from test_av_signals import DISCORD, Stubs, _worker
from yuki.memory.callevidence import SITE_PROMPT, SITE_TOOL, CallEvidence, Verdict

ZEN = r"C:\Program Files\Zen Browser\zen.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
ARC = r"C:\Users\someone\AppData\Local\Microsoft\WindowsApps\Arc.exe"

YOUTUBE = ("Search", "Home", "Shorts", "Subscriptions")
JOINED = ("Leave call", "Turn off microphone", "People", "Meeting details")

#: Stand-in for the site judge (the real one is a model reading the host only).
CALLING = {"meet.google.com": "Google Meet", "zoom.us": "Zoom", "app.zoom.us": "Zoom",
           "teams.microsoft.com": "Microsoft Teams"}


def site_judge(host: str) -> tuple[bool, str]:
    return (host in CALLING), CALLING.get(host, "")


def screen_judge(screen) -> Verdict:
    joined = "Leave call" in screen.controls
    return Verdict(in_call=joined, service="Google Meet" if joined else "")


@pytest.fixture()
def store(tmp_path):
    from yuki.memory.store import Store

    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


def worker(store, stubs, *, site_fn=site_judge, **cfg):
    w = _worker(store, stubs, **cfg)
    w.evidence = CallEvidence(judge_fn=screen_judge, site_fn=site_fn, rejudge_s=0.0,
                              fresh_s=cfg.get("evidence_fresh_s", 900.0))
    w.sites_fn = lambda key: 8   # every app here shows many sites: a browser
    return w


def key_of(exe: str) -> str:
    return exe.rsplit("\\", 1)[-1].rsplit(".", 1)[0].casefold()


def sighting(w, exe: str, at: float, host: str = "meet.google.com", reason: str = "blocked_host") -> None:
    """What the watcher hands over for a page the privacy rules kept unread: app, host, time only."""
    w.evidence.seen({"process": key_of(exe), "exe": exe, "hwnd": 7, "at": at, "host": host, "blocked": reason})


def see(w, exe: str, at: float, controls, host=None) -> None:
    w.evidence.seen({"process": key_of(exe), "exe": exe, "hwnd": 7, "at": at, "host": host, "title": "",
                     "controls": list(controls)})


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


def test_a_blocked_site_that_is_no_calling_service_starts_nothing(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 60)
    w = worker(store, stubs)
    sighting(w, ZEN, now - 5, host="payments.example.com")
    for t in (0, 30):
        w.step(now + t)
        assert w.active is None


def test_the_site_call_ends_by_the_microphone_hold_even_while_the_page_is_still_in_front(store):
    """A host cannot tell a muted meeting from a "You left the meeting" page: once the app lets go, sightings of
    the site do not keep the call alive (release grace, then the end grace)."""
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


# -- the evidence itself ---------------------------------------------------------------------------------------


def test_a_site_is_judged_once_per_host_and_only_for_microphone_holders():
    asked: list[str] = []

    def counting(host):
        asked.append(host)
        return site_judge(host)

    ev = CallEvidence(judge_fn=screen_judge, site_fn=counting)
    now = time.time()
    ev.seen({"process": "zen", "exe": ZEN, "at": now, "host": "meet.google.com", "blocked": "blocked_host"})
    ev.seen({"process": "chrome", "exe": CHROME, "at": now, "host": "bank.example.com", "blocked": "blocked_host"})
    assert ev.judge(["discord"]) == 0 and asked == []                 # neither browser holds the microphone
    assert ev.judge(["zen"]) == 1 and asked == ["meet.google.com"]
    ev.seen({"process": "zen", "exe": ZEN, "at": now + 5, "host": "meet.google.com", "blocked": "blocked_host"})
    ev.seen({"process": "chrome", "exe": CHROME, "at": now + 5, "host": "meet.google.com", "blocked": "blocked_host"})
    # the bank site is judged now that Chrome holds the microphone; Meet's verdict is kept, never asked again
    assert ev.judge(["zen", "chrome"]) == 1 and asked == ["meet.google.com", "bank.example.com"]
    assert ev.state("zen", now - 10, now + 6) == ("fresh", "Google Meet")
    assert ev.state("chrome", now - 10, now + 6) == ("fresh", "Google Meet")
    assert ev.state("zen", now - 10, now + 5 + 901) == ("stale", "Google Meet")
    assert ev.state("zen", now + 100, now + 101) == (None, "")          # a later hold needs a new sighting
    assert ev.host_of("zen") == "meet.google.com" and ev.others_of("zen") is None
    life = ev.life("zen")
    assert life.site and life.positive_at == now + 5 and life.negatives == ()
    assert ev.front() == ("zen", now + 5) or ev.front() == ("chrome", now + 5)


def test_a_sighting_keeps_nothing_of_the_page():
    ev = CallEvidence(judge_fn=pytest.fail, site_fn=site_judge)
    now = time.time()
    ev.seen({"process": "zen", "exe": ZEN, "at": now, "host": "meet.google.com", "blocked": "blocked_host",
             "title": "Meet - invented-room", "controls": ["Leave call"]})   # even if a caller passed them
    assert ev.stats()["screens"] == 0 and ev.stats()["sites"] == 1
    assert ev.judge(["zen"]) == 1                    # the screen judge (pytest.fail) is never asked
    assert ev.state("zen", now - 5, now + 1) == ("fresh", "Google Meet")


def test_a_failed_site_judgement_is_tried_again():
    calls = []

    def flaky(host):
        calls.append(host)
        if len(calls) == 1:
            raise TimeoutError("model timed out")
        return site_judge(host)

    ev = CallEvidence(judge_fn=screen_judge, site_fn=flaky)
    now = time.time()
    ev.seen({"process": "zen", "exe": ZEN, "at": now, "host": "meet.google.com", "blocked": "blocked_host"})
    assert ev.judge(["zen"]) == 0 and ev.state("zen", now - 5, now + 1) == (None, "")
    assert ev.judge(["zen"]) == 1 and ev.state("zen", now - 5, now + 1) == ("fresh", "Google Meet")


def test_a_readable_in_call_screen_after_the_site_keeps_the_screen_rules():
    """A readable in-call frame newer than the site sighting is the call's life (negatives, health): not a site."""
    ev = CallEvidence(judge_fn=screen_judge, site_fn=site_judge)
    now = time.time()
    ev.seen({"process": "chrome", "exe": CHROME, "at": now, "host": "meet.google.com", "blocked": "blocked_host"})
    ev.seen({"process": "chrome", "exe": CHROME, "at": now + 5, "host": "teams.microsoft.com",
             "title": "", "controls": list(JOINED)})
    ev.judge(["chrome"])
    life = ev.life("chrome")
    assert not life.site and life.positive_at == now + 5 and life.host == "teams.microsoft.com"


def test_the_model_site_judge_is_sent_the_host_only():
    from types import SimpleNamespace

    sent = []

    class Client:
        class messages:  # noqa: N801 - the SDK's shape
            @staticmethod
            def create(**kw):
                sent.append(kw)
                block = SimpleNamespace(type="tool_use", input={"calling": True, "service": "Google Meet"})
                return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    settings = SimpleNamespace(estimate_cost=lambda model, usage: 0.0001)
    ev = CallEvidence(judge_fn=screen_judge, client=Client(), settings=settings, model="stub-model")
    now = time.time()
    ev.seen({"process": "zen", "exe": ZEN, "at": now, "host": "meet.google.com", "blocked": "blocked_host"})
    assert ev.judge(["zen"]) == 1
    assert len(sent) == 1 and sent[0]["messages"] == [{"role": "user", "content": "host: meet.google.com"}]
    assert sent[0]["system"] == SITE_PROMPT and sent[0]["tools"] == [SITE_TOOL]
    assert ev.state("zen", now - 1, now + 1) == ("fresh", "Google Meet")


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
