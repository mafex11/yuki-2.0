"""Calls from the screen, not the microphone (2026-09-28: the Zen browser held the microphone while it played
YouTube and was taken for a call; a Google Meet lobby counted as a meeting).

A microphone holder is only a hint: a call needs a screen of that app, captured during the hold, judged to show
a joined call (:mod:`yuki.memory.callevidence`). The judge here is a stub (the real one is a model); temp
databases and stubbed device holders only. Nothing reads the desktop, the microphone or the speakers.
"""

from __future__ import annotations

import time

import pytest

from test_av_signals import DISCORD, Stubs, _worker
from yuki.memory.callevidence import CallEvidence, Verdict

ZEN = r"C:\Program Files\Zen Browser\zen.exe"

LOBBY = ("Ready to join?", "Join now", "Other ways to join", "Turn off microphone")
JOINED = ("Leave call", "Turn off microphone", "People", "Meeting details")
VOICE_PANEL = ("Voice Connected", "Disconnect", "Noise Suppression", "#general")
TEXT_CHANNEL = ("#general", "#memes", "Message #memes", "User Settings")
YOUTUBE = ("Search", "Home", "Shorts", "Subscriptions")


def judge(screen):
    """Stand-in for the model: a leave or disconnect control means a joined call."""
    joined = any(c in ("Leave call", "Disconnect") for c in screen.controls)
    service = ("Google Meet" if screen.host == "meet.google.com" else "Discord") if joined else ""
    return Verdict(in_call=joined, service=service)


@pytest.fixture()
def store(tmp_path):
    from yuki.memory.store import Store

    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


def worker(store, stubs, *, browser: bool = False, **cfg):
    w = _worker(store, stubs, **cfg)
    w.evidence = CallEvidence(judge_fn=judge, fresh_s=cfg.get("evidence_fresh_s", 900.0), rejudge_s=0.0)
    w.sites_fn = (lambda key: 8 if key == "zen" else 1) if browser else (lambda key: 1)
    return w


def see(w, exe: str, at: float, controls, host=None) -> None:
    w.evidence.seen({"process": exe.rsplit("\\", 1)[-1].rsplit(".", 1)[0].casefold(), "exe": exe, "hwnd": 7,
                     "at": at, "host": host, "title": "", "controls": list(controls)})


def status(store) -> dict:
    from yuki.memory.api import MemoryClient

    return MemoryClient(store).call_status()["call"]


# -- Meet: the lobby is not a call, a joined meeting is ---------------------------------------------------------


def test_meet_lobby_is_not_a_call_a_joined_meet_is(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 30)       # the lobby's microphone preview
    stubs.play(9, ZEN)
    w = worker(store, stubs, browser=True)
    see(w, ZEN, now - 10, LOBBY, host="meet.google.com")
    w.step(now)
    assert w.active is None
    see(w, ZEN, now + 5, JOINED, host="meet.google.com")   # clicked "Join now"
    w.step(now + 6)
    assert w.active is not None and w.active.via == "screen"
    row = store.call_record(w.active.call_id)
    assert row.app == "Google Meet" and row.service == "Google Meet" and row.via == "screen"
    assert status(store)["evidence"] == "fresh"


def test_evidence_goes_stale_and_is_renewed_by_seeing_the_call_again(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 30)
    w = worker(store, stubs, browser=True, evidence_fresh_s=300.0, end_grace_s=30.0)
    see(w, ZEN, now, JOINED, host="meet.google.com")
    w.step(now + 1)
    call_id = w.active.call_id
    w.step(now + 400)                       # nothing seen of the call for 400 s: probably still in it
    assert w.active.call_id == call_id and w.active.via == "screen_stale"
    assert store.call_record(call_id).via == "screen_stale" and status(store)["evidence"] == "stale"
    see(w, ZEN, now + 401, JOINED, host="meet.google.com")   # looked at the Meet tab again (same screen)
    w.step(now + 402)
    assert w.active.via == "screen" and status(store)["evidence"] == "fresh"


LEFT = ("You left the meeting", "Rejoin", "Return to home screen")


def test_same_site_not_in_call_ends_a_held_call_only_when_it_persists_another_site_never(store):
    """The 2026-10-03 contract (05 §7.1): one "not in a call" screen never ends a call whose microphone is held;
    a persistent run of them on the call's own site does (a "You left the meeting" page while the browser keeps
    the microphone). Another site of the same browser never counts. Either way the site can no longer start one."""
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 30)
    w = worker(store, stubs, browser=True, end_grace_s=30.0)
    see(w, ZEN, now, JOINED, host="meet.google.com")
    w.step(now + 1)
    call_id = w.active.call_id
    see(w, ZEN, now + 10, YOUTUBE, host="www.youtube.com")   # another tab: the meeting goes on
    w.step(now + 11)
    assert w.active is not None and w.active.via == "screen"
    see(w, ZEN, now + 20, LEFT, host="meet.google.com")
    w.step(now + 21)
    w.step(now + 60)                        # one reliable negative, the microphone still held: still the call
    assert w.active is not None and w.active.call_id == call_id and w.active.via == "screen_stale"
    assert w.evidence.state("zen", now - 30, now + 60) == (None, "")   # but this hold could no longer start one
    see(w, ZEN, now + 90, LEFT, host="meet.google.com")      # the page left in front, read again
    w.step(now + 91)
    assert w.active is not None             # two sights over 70 s: not yet persistent
    see(w, ZEN, now + 145, LEFT, host="meet.google.com")     # three sights over 125 s (>= neg_span_s)
    w.step(now + 146)
    assert w.active is None
    row = store.call_record(call_id)
    assert row.status == "ended" and row.stop_reason == "screen_contradicts" and len(store.calls()) == 1


def test_a_browser_holding_the_mic_while_showing_youtube_is_not_a_call(store):
    """The live case of 2026-09-28 09:01-09:11 (Zen, consent "always" inherited from Google Meet)."""
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 3600)
    stubs.play(9, ZEN)
    w = worker(store, stubs, browser=True)
    store.set_call_pref("zen", "always", name="Google Meet")   # the legacy pref: no longer makes a call
    see(w, ZEN, now - 5, YOUTUBE, host="www.youtube.com")
    for t in (0, 5, 60, 600):
        w.step(now + t)
        assert w.active is None


def test_a_joined_screen_from_before_the_hold_is_not_evidence(store):
    now = time.time()
    stubs = Stubs()
    w = worker(store, stubs, browser=True)
    see(w, ZEN, now - 3600, JOINED, host="meet.google.com")  # an hour-old meeting
    stubs.hold("mic", ZEN, now - 30)                          # a new hold (a voice search)
    w.step(now)
    assert w.active is None


# -- Discord: the voice-connected panel counts, the microphone alone does not ---------------------------------


def test_discord_mic_only_is_not_a_call_the_voice_panel_is(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", DISCORD, now - 60)
    stubs.play(4242, DISCORD)
    w = worker(store, stubs)
    see(w, DISCORD, now - 30, TEXT_CHANNEL, host="discord.com")
    w.step(now)
    assert w.active is None
    see(w, DISCORD, now + 1, VOICE_PANEL, host="discord.com")
    w.step(now + 2)
    assert w.active is not None and w.active.key == "discord" and w.active.via == "screen"
    row = store.call_record(w.active.call_id)
    assert row.service is None              # an app's own call: its standing answer is the app's


# -- consent prefs: a site's call inside a browser keeps its own -------------------------------------------------


def test_always_on_a_meet_card_is_kept_for_the_service_not_the_browser(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZEN, now - 30)
    w = worker(store, stubs, browser=True)
    see(w, ZEN, now, JOINED, host="meet.google.com")
    w.step(now + 1)
    assert store.answer_call(w.active.call_id, "always")
    assert store.call_pref("Google Meet") == "always" and store.call_pref("zen") is None
    # the next Meet call is answered by it; a Zen microphone hold on YouTube is still no call
    w.step(now + 2)
    stubs.mic.clear()
    w.step(now + 100)
    assert w.active is None
    stubs.hold("mic", ZEN, now + 200)
    see(w, ZEN, now + 205, JOINED, host="meet.google.com")
    w.step(now + 206)
    row = store.call_record(w.active.call_id)
    assert row.consent == "always" and row.consent_by == "pref"


def test_always_on_a_discord_card_stays_the_apps(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", DISCORD, now - 30)
    w = worker(store, stubs)
    see(w, DISCORD, now, VOICE_PANEL, host="discord.com")
    w.step(now + 1)
    assert store.answer_call(w.active.call_id, "always")
    assert store.call_pref("discord") == "always"


def test_legacy_browser_pref_moves_to_its_service(store):
    now = time.time()
    call_id = store.start_call(app="Google Meet", process="zen", via="meeting", service="Google Meet", at=now - 99)
    store.update_call(call_id, status="ended", ended_at=now - 50)
    # the answer as it was kept before the fix: on the process

    store.update_call(call_id, consent="always", consent_by="card")
    store.set_call_pref("zen", "always", name="Google Meet")
    d = store.start_call(app="Discord", process="discord", via="mic", at=now - 40)
    store.update_call(d, status="ended", ended_at=now - 30, consent="always", consent_by="card")
    store.set_call_pref("discord", "always", name="Discord")
    w = worker(store, Stubs(), browser=True)
    assert w.rekey_prefs() == [("zen", "google meet")]
    assert store.call_pref("zen") is None and store.call_pref("Google Meet") == "always"
    assert store.call_pref("discord") == "always"               # an app's own answer stays
    assert w.rekey_prefs() == []                                 # idempotent


# -- the evidence itself ---------------------------------------------------------------------------------------


def test_verdicts_are_cached_by_screen_content_and_only_holders_are_judged():
    asked = []

    def counting(screen):
        asked.append(screen.key)
        return judge(screen)

    ev = CallEvidence(judge_fn=counting)
    now = time.time()
    ev.seen({"process": "discord", "exe": DISCORD, "at": now, "host": "discord.com", "controls": list(VOICE_PANEL)})
    ev.seen({"process": "notepad", "exe": r"C:\Windows\notepad.exe", "at": now, "controls": ["File", "Edit"]})
    assert ev.judge(["discord"]) == 1 and asked == ["discord"]          # notepad holds no microphone: never judged
    ev.seen({"process": "discord", "exe": DISCORD, "at": now + 5, "host": "discord.com",
             "controls": list(VOICE_PANEL)})
    assert ev.judge(["discord"]) == 0 and asked == ["discord"]          # the same screen: no second judgement
    assert ev.state("discord", now - 10, now + 6) == ("fresh", "Discord")
    assert ev.state("discord", now - 10, now + 5 + 901) == ("stale", "Discord")
    assert ev.state("discord", now + 100, now + 101) == (None, "")      # a later hold needs a new screen


def test_screen_controls_are_structural_visible_and_capped():
    from yuki.memory.extract import CONTROLS_MAX, screen_controls
    from yuki.memory.extract.model import Node, Snapshot, number

    root = Node("Window", name="Meet", children=[
        Node("Button", name="Leave call"),
        Node("Text", name="a sentence of chat text"),
        Node("Button", name="Hidden", offscreen=True),
        Node("Text", name="Rohan:", sr_only=True),
        Node("Text", name="Meeting details", heading=2),
        Node("Group", name="clickable tile", is_invokable=True),
        Node("Button", name="leave call"),
    ] + [Node("ListItem", name=f"row {i}") for i in range(200)])
    number(root)
    names = screen_controls(Snapshot(hwnd=1, title="Meet", process_name="zen.exe", root=root))
    assert names[:3] == ["Leave call", "Meeting details", "clickable tile"]
    assert "a sentence of chat text" not in names and "Hidden" not in names and "Rohan:" not in names
    assert len(names) <= CONTROLS_MAX


def test_the_watcher_hands_its_screens_over_and_skips_blocked_sites():
    from yuki.memory.extract.model import Extraction
    from yuki.memory.privacy import PrivacyConfig
    from yuki.memory.watcher import ProcessFacts, Watcher

    w = Watcher(lambda: None, privacy=PrivacyConfig())
    got = []
    w.on_screen = got.append
    facts = ProcessFacts(1, "Discord.exe", DISCORD, "Discord", False)
    ex = Extraction(kind="page", app="Discord", title="#general", url="https://discord.com/channels/1",
                    thread_scope="", controls=list(VOICE_PANEL))
    w._tell_screen(5, facts, ex, w.privacy.rules())
    assert got and got[0]["process"] == "discord" and got[0]["host"] == "discord.com"
    assert got[0]["controls"] == list(VOICE_PANEL)
    ex.stats["password_fields"] = 1          # a sign-in page: nothing of it leaves the watcher
    w._tell_screen(5, facts, ex, w.privacy.rules())
    assert len(got) == 1


def test_a_busy_call_screen_is_judged_at_most_once_a_minute_but_joining_is_never_delayed():
    asked = []

    def counting(screen):
        asked.append(screen.controls[-1])
        return judge(screen)

    ev = CallEvidence(judge_fn=counting, rejudge_s=60.0)
    now = time.time()
    shot = {"process": "discord", "exe": DISCORD, "host": "discord.com"}
    ev.seen({**shot, "at": now, "controls": list(TEXT_CHANNEL) + ["msg 1"]})
    ev.judge(["discord"])
    ev.seen({**shot, "at": now + 2, "controls": list(VOICE_PANEL) + ["msg 2"]})   # joined 2 s later
    ev.judge(["discord"])
    assert ev.state("discord", now - 5, now + 3)[0] == "fresh" and len(asked) == 2
    for i in range(3, 10):                                                          # new chat messages
        ev.seen({**shot, "at": now + i, "controls": list(VOICE_PANEL) + [f"msg {i}"]})
        ev.judge(["discord"])
    assert len(asked) == 2                     # the call's evidence stands; no model call per message
