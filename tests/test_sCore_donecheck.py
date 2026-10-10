"""sCore: the done-check probes and their waits (PLAN.md D19; map-skills S1.5; C.5 "done-check polling only while
waiting, <= 6 s, 0.25 s steps"). Every live source is stubbed: no media session, no audio device, no window is read.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from yuki.skills import donecheck as D
from yuki.skills.connection import Check


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def _ctx(clock: Clock, **kw: Any) -> D.ProbeContext:
    return D.ProbeContext(since=100.0, clock=clock.now, sleep=clock.sleep, **kw)


def test_wait_polls_every_quarter_second_only_while_waiting_and_stops_at_true() -> None:
    clock = Clock()
    answers = iter([False, False, True])
    ctx = _ctx(clock, probes={"foreground": lambda c, a: (next(answers), {})})
    got = D.wait(Check(probe="foreground", within_s=4.0), ctx)
    assert got.verdict is True and got.reads == 3 and clock.sleeps == [D.POLL_S, D.POLL_S]


def test_wait_gives_the_last_reads_verdict_at_the_deadline_capped_at_six_seconds() -> None:
    clock = Clock()
    ctx = _ctx(clock, probes={"foreground": lambda c, a: (False, {"status": "other_app"})})
    got = D.wait(Check(probe="foreground", within_s=6.0), ctx, deadline_s=1.0)
    assert got.verdict is False and clock.t <= 1.0 and got.evidence == {"status": "other_app"}
    clock2 = Clock()
    got = D.wait(Check(probe="foreground", within_s=6.0), _ctx(clock2, probes={"foreground": lambda c, a: (None, {})}))
    assert got.verdict is None and clock2.t <= D.MAX_WAIT_S


def test_a_read_back_check_is_the_routes_own_and_read_once() -> None:
    clock = Clock()
    got = D.wait(Check(probe="read_back"), _ctx(clock, result=SimpleNamespace(verified=True)))
    assert got.verdict is True and got.reads == 1 and clock.sleeps == []
    assert D.wait(Check(probe="read_back"), _ctx(clock, result=SimpleNamespace(verified=None))).verdict is None
    assert D.check({"probe": "read_back"}, since=0).verdict is None        # the thinker's gate can't read a route's


def test_all_is_true_only_when_every_part_is() -> None:
    clock = Clock()
    yes, no, unk = (lambda c, a: (True, {})), (lambda c, a: (False, {})), (lambda c, a: (None, {}))
    both = Check(probe="all", parts=(Check(probe="uia_name"), Check(probe="media_session")), within_s=0)
    assert D.read(both, _ctx(clock, probes={"uia_name": yes, "media_session": yes}))[0] is True
    assert D.read(both, _ctx(clock, probes={"uia_name": yes, "media_session": no}))[0] is False
    assert D.read(both, _ctx(clock, probes={"uia_name": yes, "media_session": unk}))[0] is None


def test_templates_are_filled_and_a_missing_value_couldnt_read() -> None:
    seen: list = []
    ctx = _ctx(Clock(), values={"list": {"name": "Invented Mix"}},
               probes={"uia_name": lambda c, a: (seen.append(dict(a)) or True, {})})
    assert D.read(Check(probe="uia_name", args={"name": "Pause {list.name}"}), ctx)[0] is True
    assert seen == [{"name": "Pause Invented Mix"}]
    assert D.read(Check(probe="uia_name", args={"name": "Pause {other}"}), ctx) == (None, {"status": "no_value"})


def test_a_probe_that_breaks_couldnt_read() -> None:
    def boom(c: Any, a: Any) -> Any:
        raise RuntimeError("invented")

    v, ev = D.read(Check(probe="level"), _ctx(Clock(), probes={"level": boom}))
    assert v is None and ev["error_kind"] == "RuntimeError"


def test_media_session_reads_the_named_apps_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(D, "_sessions", lambda: [{"app_id": "InventedPlayer.exe", "status": "playing",
                                                  "title": "Invented Song"},
                                                 {"app_id": "Other.exe", "status": "paused", "title": "x"}])
    ctx = _ctx(Clock())
    assert D.probe_media_session(ctx, {"app": "InventedPlayer.exe", "status": "playing"})[0] is True
    assert D.probe_media_session(ctx, {"app": "Other.exe", "status": "playing"})[0] is False
    assert D.probe_media_session(ctx, {"app": "Missing.exe", "status": "playing"})[0] is None
    ctx.result = SimpleNamespace(facts_ram={"title_before": "Invented Song"})
    assert D.probe_media_session(ctx, {"app": "InventedPlayer.exe", "changed": "track"})[0] is False
    ctx.result = SimpleNamespace(facts_ram={"title_before": "Earlier Song"})
    v, ev = D.probe_media_session(ctx, {"app": "InventedPlayer.exe", "changed": "track"})
    assert v is True and "Invented Song" not in repr(ev)                  # no title leaves the probe


def test_audio_active_and_mic_held_match_by_process(monkeypatch: pytest.MonkeyPatch) -> None:
    import yuki.perception.audio as audio

    monkeypatch.setattr(D, "_image_of", lambda pid: {1: "InventedPlayer.exe", 2: "Other.exe"}.get(pid, ""))
    monkeypatch.setattr(audio, "render_sessions", lambda: [{"pid": 1, "state": "active"}, {"pid": 2, "state": "inactive"}])
    monkeypatch.setattr(audio, "capture_sessions", lambda: [{"pid": 2, "state": "active"}])
    ctx = _ctx(Clock())
    assert D.probe_audio_active(ctx, {"process": "InventedPlayer.exe"})[0] is True
    assert D.probe_audio_active(ctx, {"process": "Other.exe"})[0] is False
    assert D.probe_mic_held(ctx, {"process": "InventedPlayer.exe"})[0] is False     # not held: contradicted
    # review M4: held now proves nothing by itself; only an onset against the state before the rung acted does
    assert D.probe_mic_held(ctx, {"process": "Other.exe"}) == (None, {"sessions": 1, "status": "no_baseline"})
    before = D.take_baseline(Check(probe="mic_held", args={"process": "Other.exe"}), {})
    assert before == {"mic_held:other.exe": True}
    held_before = _ctx(Clock(), baseline=before)
    assert D.probe_mic_held(held_before, {"process": "Other.exe"})[0] is None       # held before, still held
    onset = _ctx(Clock(), baseline={"mic_held:other.exe": False})
    assert D.probe_mic_held(onset, {"process": "Other.exe"})[0] is True             # taken after the run began


def test_take_baseline_reads_only_onset_probes_and_all_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list = []
    readers = {"mic_held": lambda args: reads.append(dict(args)) or False}
    checks = [Check(probe="all", parts=(Check(probe="foreground", args={"process": "A.exe"}),
                                        Check(probe="mic_held", args={"process": "{app}"}))),
              Check(probe="mic_held", args={"process": "{app}"}), None]
    got = D.take_baseline(checks, {"app": "Invented.exe"}, readers=readers)
    assert got == {"mic_held:invented.exe": False} and reads == [{"process": "Invented.exe"}]   # read once
    pinned_spec = {"probe": "mic_held", "process": "Invented.exe", "within_s": 2}
    assert D.take_baseline(pinned_spec, {}, readers={"mic_held": lambda a: (_ for _ in ()).throw(OSError())}) == \
        {"mic_held:invented.exe": None}                       # couldn't read: no onset can be shown


def test_message_sent_needs_this_text_and_never_says_no(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    rows: list = []

    class Client:
        def sent_messages(self, app: Any, since: float) -> list:
            calls.append((app, since))
            return list(rows) if since <= 100.0 else []

        store = SimpleNamespace(moments_between=lambda lo, hi: [{"kind": "volume_change", "app": "demoplayer"}])

    ctx = _ctx(Clock(), memory_call=lambda what, fn: fn(Client()))
    args = {"app": "discord", "text": "see you at  Seven"}
    # review M3: nothing captured yet (the watcher lags, or memory failed and gave []) is None, never False
    assert D.probe_message_sent(ctx, args) == (None, {"n": 0, "status": "not_seen_yet"})
    rows.append({"text": "an older invented line", "thread": "x"})
    assert D.probe_message_sent(ctx, args)[0] is None                    # another message of theirs: not this one
    rows.append({"text": "See you at seven", "thread": "x"})
    v, ev = D.probe_message_sent(ctx, args)
    assert v is True and ev == {"n": 2, "matched": 1} and calls[0] == ("discord", 100.0)
    assert "seven" not in repr(ev).casefold()                            # content-free evidence
    assert D.probe_message_sent(ctx, {"app": "discord"})[0] is None      # no text to match: can't tie it to this run
    assert D.probe_moment(ctx, {"kind": "volume_change", "app": "demoplayer"})[0] is True
    assert D.probe_moment(ctx, {"kind": "page", "app": "demoplayer"})[0] is None    # moments lag: never "no"
    assert D.probe_message_sent(_ctx(Clock()), args)[0] is None          # no memory: couldn't read


def test_uia_reads_that_timed_out_end_the_wait() -> None:
    clock = Clock()
    reads: list = []

    def blocked(ctx: Any, args: Any) -> tuple:
        reads.append(1)
        return None, {"status": "timeout", "ms": 600.0}

    got = D.wait(Check(probe="uia_name", within_s=4.0), _ctx(clock, probes={"uia_name": blocked}))
    assert got.verdict is None and got.reads == D.MAX_LATE_READS == 2      # no thread pile-up on a blocked provider
    nested = D.wait(Check(probe="all", within_s=4.0, parts=(Check(probe="uia_name"), Check(probe="foreground"))),
                    _ctx(Clock(), probes={"uia_name": blocked, "foreground": lambda c, a: (False, {})}))
    assert nested.reads == 2
    answers = iter([(None, {"status": "timeout"}), (True, {"status": "found"})])   # a cold provider's second read
    cold = D.wait(Check(probe="uia_name", within_s=4.0), _ctx(Clock(), probes={"uia_name": lambda c, a: next(answers)}))
    assert cold.verdict is True and cold.reads == 2


def test_chat_open_has_no_generic_reader_and_says_so() -> None:
    assert D.probe_chat_open(_ctx(Clock()), {}) == (None, {"status": "no_reader"})


def test_recheck_waits_then_reads() -> None:
    clock = Clock()
    got = D.recheck(Check(probe="foreground", after_s=3.0), _ctx(clock, probes={"foreground": lambda c, a: (True, {})}))
    assert got.verdict is True and clock.sleeps[0] == 3.0
