"""Calls and meetings from measured signals (docs/MEMORY.md, "Calls: just my side" and "Timeline: full screen
and meetings"; docs/research/no-pattern-audit.md C): no app or site list decides.

Stubbed ConsentStore readings (who holds the microphone and camera, since when) and stubbed Core Audio render
sessions (which processes play sound); temp databases. Nothing here reads or touches the live desktop, the
microphone or the speakers: the timeline recorder's window, process and page reads are replaced by stubs.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from yuki.memory.avsignals import AvSignals, Holder
from yuki.memory.calls import CallConfig, CallWorker

DISCORD = r"C:\Apps\Discord\Discord.exe"
CHROME = r"C:\Apps\Chrome\chrome.exe"
HANDY = r"C:\Apps\Handy\handy.exe"
PYTHON = r"C:\Users\yukidev\yuki\.venv\Scripts\python.exe"


class Stubs:
    """What Windows would say: device holders and render sessions, changed by the test."""

    def __init__(self) -> None:
        self.mic: list[dict] = []
        self.camera: list[dict] = []
        self.render: list[dict] = []
        self.identities: dict[int, tuple[str, str]] = {}
        self.render_reads = 0

    def hold(self, device: str, path: str, since: float, *, packaged: str = "") -> None:
        rows = self.mic if device == "mic" else self.camera
        if packaged:
            rows.append({"app": packaged, "packaged": True, "path": "", "since": since})
        else:
            rows.append({"app": path.rsplit("\\", 1)[-1], "packaged": False, "path": path, "since": since})

    def play(self, pid: int, exe: str, family: str = "") -> None:
        self.identities[pid] = (exe, family)
        self.render.append({"pid": pid, "state": "active", "device": "Headphones"})

    def _render(self) -> list[dict]:
        self.render_reads += 1
        return list(self.render)

    def signals(self, exclude: frozenset[str] = frozenset()) -> AvSignals:
        return AvSignals(exclude, mic_fn=lambda: list(self.mic), camera_fn=lambda: list(self.camera),
                         render_fn=self._render, identity_fn=lambda pid: self.identities.get(pid, ("", "")),
                         watch=False, backstop_s=0.0, render_max_age_s=0.0)


@pytest.fixture()
def store(tmp_path):
    from yuki.memory.store import Store

    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


#: What a joined call's screen offers (the stub judge below reads it; the real one is a model).
IN_CALL = ("Leave call", "Mute", "Participants")


def stub_judge(screen):
    from yuki.memory.callevidence import Verdict

    joined = "Leave call" in screen.controls or "Disconnect" in screen.controls
    return Verdict(in_call=joined, service=screen.key.title() if joined else "")


def evidence():
    from yuki.memory.callevidence import CallEvidence

    return CallEvidence(judge_fn=stub_judge)


def shows(worker: CallWorker, exe: str, at: float, controls=IN_CALL, host=None, title="") -> None:
    """The watcher read a window of ``exe`` at ``at`` showing ``controls`` (a joined call by default)."""
    worker.evidence.seen({"process": exe.rsplit("\\", 1)[-1].rsplit(".", 1)[0].casefold(), "exe": exe, "hwnd": 1,
                          "at": at, "host": host, "title": title, "controls": list(controls)})


def _worker(store, stubs: Stubs, **cfg) -> CallWorker:
    config = CallConfig(**{"end_grace_s": 30.0, **cfg})
    worker = CallWorker(store, signals=stubs.signals(frozenset({PYTHON.casefold()})), config=config,
                        headphones_fn=lambda c: (False, {"form_factor": "speakers", "device": "Speakers"}),
                        source_factory=lambda c, app: pytest.fail("the microphone must not be opened"),
                        evidence=evidence())
    worker._tell_ui = lambda: None   # never wake the real UI's card watcher
    return worker


# -- calls ---------------------------------------------------------------------------------------------------


def test_mic_plus_sound_out_is_only_a_hint_the_screen_makes_the_call_and_the_card_asks(store):
    """2026-09-28: the old rule (microphone + sound out = a call) took a browser playing YouTube for one."""
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", DISCORD, now - 20)
    stubs.play(4242, DISCORD)
    w = _worker(store, stubs)
    w.step(now)
    assert w.active is None                                  # microphone + sound: a hint, not proof
    shows(w, DISCORD, now + 1)                               # the voice-connected screen
    w.step(now + 2)
    assert w.active is not None and w.active.key == "discord" and w.active.via == "screen"
    row = store.call_record(w.active.call_id)
    assert row.status == "asking" and row.consent is None   # asks every call
    assert row.app == "Discord"                              # the app's own name (image stem when no version info)


def test_mic_alone_is_not_a_call(store):
    """Handy (dictation) holds the microphone and plays nothing: no card."""
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", HANDY, now - 120)
    w = _worker(store, stubs)
    w.step(now)
    assert w.active is None
    stubs.play(7, r"C:\Apps\Spotify\Spotify.exe")   # another app playing is not Handy's sound
    w.step(now + 5)
    assert w.active is None


def test_a_browser_holding_the_mic_while_a_video_plays_is_not_a_call(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", CHROME, now - 600)   # a tab with microphone permission, a video playing in another
    stubs.play(900, CHROME)
    w = _worker(store, stubs)
    shows(w, CHROME, now - 5, controls=("Search", "Home", "Shorts"), host="www.youtube.com")
    for t in (0, 11, 60):
        w.step(now + t)
        assert w.active is None


def test_camera_with_mic_is_a_hint_too_and_sound_is_never_read(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", CHROME, now - 30)
    stubs.hold("camera", CHROME, now - 30)
    w = _worker(store, stubs)
    w.step(now)
    assert w.active is None               # a camera preview (a lobby) is not a joined call
    shows(w, CHROME, now + 1, host="meet.google.com")
    w.step(now + 2)
    assert w.active is not None and w.active.key == "chrome"
    assert stubs.render_reads == 0        # sound out is no longer read at all


def test_packaged_app_matches_by_package_family(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", "", now - 40, packaged="MSTeams")
    exe = r"C:\Program Files\WindowsApps\MSTeams_1.0\ms-teams.exe"
    stubs.play(55, exe, "MSTeams_8wekyb3d8bbwe")
    w = _worker(store, stubs)
    shows(w, exe, now + 1)                 # the watcher knows it as ms-teams; the holder by its package family
    w.step(now + 2)
    assert w.active is not None and w.active.key == "msteams"


def test_yuki_itself_is_never_a_call(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", PYTHON, now - 60)
    stubs.play(1, PYTHON)
    w = _worker(store, stubs)
    w.step(now)
    assert w.active is None


def test_user_listed_app_needs_no_sound(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", HANDY, now - 1)
    w = _worker(store, stubs, apps=frozenset({"handy"}))
    w.step(now)
    assert w.active is not None and w.active.key == "handy"


def test_call_goes_on_through_silence_and_ends_after_the_mic_is_let_go(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", DISCORD, now - 20)
    stubs.play(4242, DISCORD)
    w = _worker(store, stubs)
    shows(w, DISCORD, now - 1)
    w.step(now)
    call_id = w.active.call_id
    stubs.render.clear()                  # nobody talks: no output stream for a while
    w.step(now + 40)
    assert w.active is not None and w.active.call_id == call_id
    stubs.mic.clear()                     # left the call: the app let go of the microphone after now + 40
    w.step(now + 50)
    assert w.active is not None           # within release_grace_s (a re-grab would be the same call)
    w.step(now + 125)
    assert w.active is not None           # still within release_grace_s (90 s from the last poll it was held)
    w.step(now + 135)
    assert w.active is not None           # not alive any more, within end_grace_s
    w.step(now + 166)
    assert w.active is None and store.call_record(call_id).status == "ended"
    assert store.call_record(call_id).stop_reason == "mic_released"


def test_no_mic_holder_reads_no_sound(store):
    stubs = Stubs()
    stubs.play(4242, DISCORD)
    w = _worker(store, stubs)
    w.step(time.time())
    assert w.active is None and stubs.render_reads == 0


def test_signals_cache_until_backstop():
    stubs = Stubs()
    calls = []
    sig = AvSignals(frozenset(), mic_fn=lambda: calls.append(1) or [], camera_fn=list, render_fn=list,
                    watch=False, backstop_s=60.0)
    sig.read()
    sig.read()
    assert len(calls) == 1 and sig.stats()["holder_reads"] == 1
    assert stubs.render_reads == 0


def test_holder_matching():
    h = Holder("Discord.exe", False, DISCORD, 0.0)
    assert h.key == "discord"
    assert h.matches(exe=DISCORD.upper()) and h.matches(process_key="discord")
    assert not h.matches(exe=CHROME, process_key="chrome")
    p = Holder("MSTeams", True, "", 0.0)
    assert p.matches(family="MSTeams_8wekyb3d8bbwe") and p.matches(ids=("msteams_8wekyb3d8bbwe!msteams",))
    assert not p.matches(family="Microsoft.WindowsCalculator_8wekyb3d8bbwe")


# -- meetings (the timeline) -----------------------------------------------------------------------------------


def _facts(pid: int, exe: str, app: str):
    from yuki.memory.watcher import ProcessFacts

    return ProcessFacts(pid, exe.rsplit("\\", 1)[-1], exe, app, False)


def _recorder(store, tmp_path, stubs: Stubs, windows: dict[int, tuple], meetings: str = ""):
    """A TimelineRecorder with every window, process and page read stubbed (nothing is read from the desktop)."""
    from yuki.memory.privacy import PrivacyConfig
    from yuki.memory.timeline import TimelineRecorder

    path = tmp_path / "privacy.toml"
    if meetings:
        path.write_text(meetings, encoding="utf-8")
    rec = TimelineRecorder(store, privacy=PrivacyConfig(path), signals=stubs.signals())
    rec._w = SimpleNamespace(
        _window_pid=lambda h: windows[h][0], _window_title=lambda h: windows[h][2],
        is_fullscreen_front=lambda h: False, user_idle_s=lambda: 0.0, session_locked=lambda: False,
    )
    rec._procs = SimpleNamespace(get=lambda pid: next(f for p, f, _ in windows.values() if p == pid))
    rec._windows = SimpleNamespace(is_user_window=lambda h: True, process_package_ids=lambda pid: ("", ""),
                                   start_menu_index=lambda: None)
    rec._hook_names = lambda hwnd: None
    rec._unhook_names = lambda: None
    return rec


def _rows(store):
    return [(r.meeting, r.title, r.host) for r in store.timeline_between(0, time.time() + 10_000)]


def test_app_holding_the_mic_in_front_becomes_a_meeting_and_ends(store, tmp_path):
    t0 = time.time()
    stubs = Stubs()
    rec = _recorder(store, tmp_path, stubs, {1: (10, _facts(10, DISCORD, "Discord"), "#general - Discord")},
                    "[meetings]\nhold_s = 20\nend_grace_s = 10\n")
    rec._input_at = t0
    stubs.hold("mic", DISCORD, t0 + 1)
    rec._open(1, t0)
    assert rec._current.meeting is None and rec._current.title == "#general - Discord"
    rec._poll_av(t0 + 15)
    rec._check_meeting(t0 + 15)
    assert rec._current.meeting is None             # held 14 s: not yet
    rec._poll_av(t0 + 30)
    rec._check_meeting(t0 + 30)
    st = rec._current
    assert st.meeting == "Discord" and st.title == "" and st.hidden and st.process == "Discord.exe"
    assert rec.current_meeting() == {"process": "Discord.exe", "app": "Discord", "service": "Discord"}
    stubs.mic.clear()                               # hung up
    rec._poll_av(t0 + 35)
    rec._check_meeting(t0 + 35)
    assert rec._current.meeting == "Discord"        # within end_grace_s
    rec._poll_av(t0 + 45)
    rec._check_meeting(t0 + 45)
    assert rec._current.meeting is None and rec._current.title == "#general - Discord"
    rec._close(t0 + 60)
    assert _rows(store) == [(None, "#general - Discord", None), ("Discord", None, None),
                            (None, "#general - Discord", None)]


def test_camera_counts_and_never_apps_override(store, tmp_path):
    t0 = time.time()
    stubs = Stubs()
    obs = r"C:\Apps\OBS\obs64.exe"
    windows = {1: (10, _facts(10, obs, "OBS Studio"), "OBS"), 2: (20, _facts(20, DISCORD, "Discord"), "Discord")}
    rec = _recorder(store, tmp_path, stubs, windows, '[meetings]\nhold_s = 5\nnever_apps = ["obs64.exe"]\n')
    rec._input_at = t0
    stubs.hold("camera", obs, t0 - 60)
    stubs.hold("camera", DISCORD, t0 - 60)
    rec._open(1, t0)
    assert rec._current.meeting is None             # the user said OBS is never a meeting
    rec._close(t0 + 1)
    rec._open(2, t0 + 1)                            # Discord in front holding the camera for a minute
    assert rec._current.meeting == "Discord"


def _page(rec, url: str, title: str, now: float) -> None:
    st = rec._current
    rec._page_results.append((st.seq, st.hwnd, (title, url), 1.0, None))
    rec._take_pages(now)


def test_browser_meeting_is_the_site_that_took_the_mic(store, tmp_path):
    t0 = time.time()
    stubs = Stubs()
    rec = _recorder(store, tmp_path, stubs, {2: (20, _facts(20, CHROME, "Google Chrome"), "Meet")},
                    "[meetings]\nhold_s = 20\nend_grace_s = 0\n")
    rec._probe = SimpleNamespace(submit=lambda seq, hwnd: True)
    rec._input_at = t0
    rec._open(2, t0)
    _page(rec, "https://meet.google.com/abc-defg-hij", "Meet - abc-defg-hij", t0)
    stubs.hold("mic", CHROME, t0 + 2)                # "Join now" on that page
    assert rec._current.meeting is None
    rec._poll_av(t0 + 30)
    rec._check_meeting(t0 + 30)
    st = rec._current
    assert st.meeting == "meet.google.com" and st.host == "meet.google.com" and st.title == "" and st.path is None
    # the same site read again (title or path changed): still the meeting, nothing re-opened
    _page(rec, "https://meet.google.com/abc-defg-hij?pli=1", "Meet", t0 + 32)
    assert rec._current is st
    # another tab in front while Chrome keeps the microphone: not the meeting
    _page(rec, "https://en.wikipedia.org/wiki/Orbit", "Orbit - Wikipedia", t0 + 40)
    _page(rec, "https://en.wikipedia.org/wiki/Orbit", "Orbit - Wikipedia", t0 + 40)
    assert rec._current.meeting is None and rec._current.host == "en.wikipedia.org"
    # back on the meeting tab: a meeting again at once (the hold is long)
    rec._close(t0 + 50)
    rec._open(2, t0 + 50)
    _page(rec, "https://meet.google.com/abc-defg-hij", "Meet - abc-defg-hij", t0 + 50)
    assert rec._current.meeting == "meet.google.com"
    # left the call: the site is read as a page again
    stubs.mic.clear()
    rec._poll_av(t0 + 60)
    rec._check_meeting(t0 + 60)
    assert rec._current.meeting is None


def test_always_sites_override_and_display_names(store, tmp_path):
    t0 = time.time()
    stubs = Stubs()
    rec = _recorder(store, tmp_path, stubs, {2: (20, _facts(20, CHROME, "Google Chrome"), "x")},
                    '[meetings]\nalways_sites = ["meet.example.com"]\n[meetings.names]\n"meet.example.com" = "Example Meet"\n')
    rec._probe = SimpleNamespace(submit=lambda seq, hwnd: True)
    rec._input_at = t0
    rec._open(2, t0)
    _page(rec, "https://meet.example.com/room", "Room", t0)
    assert rec._current.meeting == "Example Meet" and not rec._current.signal_meeting


def test_old_meeting_lists_in_a_user_file_are_ignored(store, tmp_path):
    t0 = time.time()
    stubs = Stubs()
    zoom = r"C:\Apps\Zoom\Zoom.exe"
    rec = _recorder(store, tmp_path, stubs, {1: (10, _facts(10, zoom, "Zoom Workplace"), "Zoom")},
                    '[meetings]\nprocesses = ["Zoom.exe"]\nhost_suffixes = ["meet.google.com"]\n')
    rec._input_at = t0
    rec._open(1, t0)
    assert rec._current.meeting is None             # Zoom in front, not holding the microphone: no meeting
    stubs.hold("mic", zoom, t0 - 60)
    rec._poll_av(t0 + 1)
    rec._check_meeting(t0 + 1)
    assert rec._current.meeting == "Zoom Workplace" and rec._current.withheld == "meeting"
    assert rec._current.title == "" and rec._current.hidden
