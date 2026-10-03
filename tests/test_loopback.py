"""Process loopback and the capture-session watch (yuki.memory.loopback), offline.

Pure parts (structures, formats, restamping across Windows' silent gaps, the process-tree root) and the COM
plumbing driven through in-process fakes: a fake ``IAudioClient`` / ``IAudioCaptureClient`` (comtypes objects
called through their real vtables, as the audio engine's would be) and a fake capture session. Nothing here
opens a live stream, activates process loopback on a real process or registers with Windows' audio sessions.
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import POINTER, c_void_p

import numpy as np
import pytest
from comtypes import COMObject

from yuki.memory import loopback as lb
from yuki.memory.loopback import (
    AUDCLNT_BUFFERFLAGS_SILENT, AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR, AUDCLNT_S_BUFFER_EMPTY, FORMATS, RATE,
    STREAM_FLAGS, CaptureWatch, Format, IAudioCaptureClient, IAudioClient, IAudioSessionEvents, LoopbackSource,
    LoopbackUnavailable, Restamper, activation_params, app_root_pid, to_mono, tree_root,
)
from yuki.perception import audio

# -- structures and formats ---------------------------------------------------------------------------------------


def test_structures_have_windows_sizes_and_the_activation_blob_points_at_its_params():
    assert ctypes.sizeof(lb.WAVEFORMATEX) == 18                       # mmreg.h, byte-packed
    assert ctypes.sizeof(lb.AUDIOCLIENT_ACTIVATION_PARAMS) == 12
    assert ctypes.sizeof(lb.PROPVARIANT_BLOB) == 24 and lb.PROPVARIANT_BLOB.blob.offset == 8
    assert ctypes.sizeof(lb.PROCESSENTRY32W) == 568                   # x64
    params, pv = activation_params(4242)
    assert pv.vt == lb.VT_BLOB and pv.blob.cbSize == 12 and pv.blob.pBlobData == ctypes.addressof(params)
    assert params.ActivationType == lb.AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
    assert params.ProcessLoopbackParams.TargetProcessId == 4242
    assert params.ProcessLoopbackParams.ProcessLoopbackMode == lb.PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
    assert activation_params(7, include_tree=False)[0].ProcessLoopbackParams.ProcessLoopbackMode == 1
    assert STREAM_FLAGS == 0x00020000 | 0x00040000 | 0x80000000 | 0x08000000


def test_formats_asked_for_and_their_wave_headers():
    assert FORMATS[0] == Format(RATE, 1, 32, True)                    # 16 kHz mono float: no work here
    w = FORMATS[0].wave()
    assert (w.wFormatTag, w.nChannels, w.nSamplesPerSec, w.wBitsPerSample, w.nBlockAlign, w.nAvgBytesPerSec,
            w.cbSize) == (lb.WAVE_FORMAT_IEEE_FLOAT, 1, 16000, 32, 4, 64000, 0)
    w = Format(48_000, 2, 16).wave()
    assert (w.wFormatTag, w.nBlockAlign, w.nAvgBytesPerSec) == (lb.WAVE_FORMAT_PCM, 4, 192000)


def test_packets_become_mono_float():
    x = np.array([0.5, -0.25, 0.125], np.float32)
    assert np.allclose(to_mono(x.tobytes(), Format(RATE, 1, 32, True)), x)
    pcm = (np.array([16384, -8192], np.int16)).tobytes()
    assert np.allclose(to_mono(pcm, Format(RATE, 1, 16)), [0.5, -0.25])
    stereo = np.array([[16384, 0], [-16384, -16384]], np.int16).tobytes()   # L/R averaged: voice sits in the middle
    assert np.allclose(to_mono(stereo, Format(48_000, 2, 16)), [0.25, -0.5])


def test_resampler_turns_48k_into_16k():
    r = lb.Resampler(48_000)
    out = np.concatenate([r(np.zeros(4800, np.float32)) for _ in range(10)])
    assert abs(len(out) - 16000) < 400                                 # one second in, one second out (filter delay)
    assert lb.Resampler(RATE)(np.ones(3, np.float32)).tolist() == [1.0, 1.0, 1.0]


# -- restamping: Windows delivers nothing while the app is silent ------------------------------------------------


def _blocks(stamper: Restamper, pushes) -> list[tuple[float, np.ndarray]]:
    out = []
    for at, n, value in pushes:
        out += stamper.push(at, np.full(n, value, np.float32))
    return out + stamper.flush()


def test_contiguous_packets_make_contiguous_blocks():
    s = Restamper()
    out = _blocks(s, [(100.0 + i * 0.01, 160, 1.0) for i in range(30)])   # 0.3 s in 10 ms packets
    assert [len(b) for _, b in out] == [1600, 1600, 1600]
    assert [round(at, 4) for at, _ in out] == [100.0, 100.1, 100.2]
    assert s.gaps == 0 and s.jumps == 0


def test_a_short_gap_is_filled_with_silence_so_times_stay_true():
    s = Restamper()
    out = _blocks(s, [(100.0, 1600, 1.0), (101.0, 1600, 2.0)])            # 0.9 s of nothing delivered
    audio = np.concatenate([b for _, b in out])
    assert out[0][0] == 100.0 and len(audio) == 2 * 1600 + 14400
    assert (audio[1600:1600 + 14400] == 0).all()                          # the pause the gate will see
    assert np.argmax(audio == 2.0) == 16000                               # the second packet sits at +1.0 s
    assert s.gaps == 1 and s.gap_s == pytest.approx(0.9)


def test_a_long_gap_fills_its_first_seconds_then_jumps_to_the_packet_time():
    s = Restamper(fill_max_s=3.0)
    out = _blocks(s, [(100.0, 800, 1.0), (160.0, 1600, 2.0)])             # a minute of silence
    times = [at for at, _ in out]
    assert out[-1][0] == pytest.approx(160.0)                             # the later packet keeps its own time
    filled = sum(len(b) for at, b in out if at < 160.0)
    assert filled == 800 + 3 * RATE                                       # 3 s of silence after the first packet
    assert s.jumps == 1 and times == sorted(times)


def test_early_packets_and_missing_timestamps_follow_the_last_one():
    s = Restamper()
    out = _blocks(s, [(100.0, 1600, 1.0), (99.5, 1600, 2.0), (None, 1600, 3.0)])
    assert [round(at, 3) for at, _ in out] == [100.0, 100.1, 100.2]
    assert s.early == 1 and s.gaps == 0


def test_qpc_positions_map_to_wall_time():
    assert lb.wall_of(10_000_000, 1000.0, 15_000_000) == pytest.approx(999.5)
    wall, q = lb.Qpc().now()
    assert abs(wall - time.time()) < 1.0 and q > 0


# -- which process tree: the app's root, from a snapshot (no process opened) --------------------------------------


TABLE = [
    (1, 0, "explorer.exe"),
    (10, 1, "Discord.exe"), (11, 10, "Discord.exe"), (12, 11, "Discord.exe"), (13, 10, "Discord.exe"),
    (20, 1, "chrome.exe"), (21, 20, "chrome.exe"), (30, 1, "chrome.exe"), (31, 30, "chrome.exe"), (32, 31, "chrome.exe"),
    (40, 41, "loop.exe"), (41, 40, "loop.exe"),
]


def test_tree_root_walks_up_while_the_parent_runs_the_same_image():
    parents = {p: pp for p, pp, _ in TABLE}
    names = {p: n for p, _, n in TABLE}
    assert tree_root(12, parents, names) == 10                            # renderer -> helper -> main process
    assert tree_root(10, parents, names) == 10                            # its parent is explorer
    assert tree_root(40, parents, names) in (40, 41)                      # a cycle stops
    assert tree_root(999, parents, names) == 999                          # unknown


def test_app_root_prefers_the_tree_that_plays_sound_then_the_largest():
    assert app_root_pid("discord.exe", TABLE) == 10
    assert app_root_pid("chrome.exe", TABLE) == 30                        # two browsers: the larger tree
    assert app_root_pid("chrome.exe", TABLE, playing=[21]) == 20           # ...unless the other one plays
    assert app_root_pid("zoom.exe", TABLE) is None and app_root_pid("", TABLE) is None


def test_process_table_reads_this_process_with_its_image():
    import os

    rows = lb.process_table()
    me = [r for r in rows if r[0] == os.getpid()]
    assert me and me[0][2].lower().startswith("python")


# -- the capture path through fake COM objects --------------------------------------------------------------------


class FakeCapture(COMObject):
    """``IAudioCaptureClient`` handing out queued packets, called through its vtable like the engine's."""

    _com_interfaces_ = [IAudioCaptureClient]

    def __init__(self, packets) -> None:
        super().__init__()
        self.packets = list(packets)
        self.buf = None
        self.released: list[int] = []

    def GetBuffer(self, this, data, frames, flags, dev, qpc):   # noqa: N802
        if not self.packets:
            frames[0] = 0
            return AUDCLNT_S_BUFFER_EMPTY
        if self.packets[0] == "error":
            return -2004287484   # AUDCLNT_E_DEVICE_INVALIDATED
        n, f, pos, raw = self.packets.pop(0)
        self.buf = ctypes.create_string_buffer(raw or b"\0", max(1, len(raw or b"")))
        data[0] = ctypes.addressof(self.buf)
        frames[0], flags[0], dev[0], qpc[0] = n, f, 0, pos
        return 0

    def ReleaseBuffer(self, this, n):   # noqa: N802
        self.released.append(int(n))
        return 0

    def GetNextPacketSize(self, this, frames):   # noqa: N802
        frames[0] = 0
        return 0


class FakeClient(COMObject):
    """``IAudioClient`` that refuses the formats it is told to (as an engine refusing a format would)."""

    _com_interfaces_ = [IAudioClient]

    def __init__(self, capture: FakeCapture, refuse: set) -> None:
        super().__init__()
        self.capture = capture
        self.refuse = refuse
        self.format = None
        self.flags = None
        self.started = self.stopped = False
        self._handed: list = []

    def Initialize(self, this, share, flags, buffer, period, fmt, session):   # noqa: N802
        f = fmt.contents
        key = (f.nSamplesPerSec, f.nChannels, f.wBitsPerSample)
        if key in self.refuse:
            return -2004287480   # AUDCLNT_E_UNSUPPORTED_FORMAT
        self.format, self.flags = key, int(flags)
        return 0

    def SetEventHandle(self, this, handle):   # noqa: N802
        return 0

    def GetService(self, this, iid, out):   # noqa: N802
        ptr = self.capture.QueryInterface(IAudioCaptureClient)
        ptr.AddRef()                    # the caller owns one reference
        self._handed.append(ptr)
        out[0] = ctypes.cast(ptr, c_void_p).value
        return 0

    def Start(self, this):   # noqa: N802
        self.started = True
        return 0

    def Stop(self, this):   # noqa: N802
        self.stopped = True
        return 0


_FAKES: list[FakeClient] = []


@pytest.fixture(autouse=True)
def _release_fakes():
    """Every interface pointer a fake handed out is released while the fakes are alive, then collected now (never
    later, inside another test, in whatever order a garbage collection of their cycles would pick)."""
    yield
    import gc

    for client in _FAKES:
        client._handed.clear()
    _FAKES.clear()
    gc.collect()


def _source(packets, refuse=()):
    made: list[FakeClient] = []
    capture_holder: list[FakeCapture] = []

    def activate(pid, tree, timeout):
        if not capture_holder:
            capture_holder.append(FakeCapture(packets))
        client = FakeClient(capture_holder[0], set(refuse))
        made.append(client)
        _FAKES.append(client)
        return client.QueryInterface(IAudioClient)

    return LoopbackSource(4242, activate_fn=activate, drain_s=0.01), made, capture_holder


def _read_all(src: LoopbackSource, want: int, timeout_s: float = 5.0) -> list[tuple[float, np.ndarray]]:
    out, n, until = [], 0, time.monotonic() + timeout_s
    while n < want and time.monotonic() < until:
        got = src.read(0.2)
        if got is not None:
            out.append(got)
            n += len(got[1])
    return out


def test_loopback_source_negotiates_reads_restamps_and_releases():
    q0 = lb.Qpc().now()[1]
    tone = (0.3 * np.sin(2 * np.pi * 440 * np.arange(160) / RATE)).astype(np.float32)
    pcm = (tone * 32767).astype(np.int16).tobytes()
    packets = [(160, 0, q0 + k * 100_000, pcm) for k in range(20)]                 # 0.2 s
    packets += [(160, AUDCLNT_BUFFERFLAGS_SILENT, q0 + (20 + k) * 100_000, None) for k in range(10)]   # 0.1 s silent
    packets += [(160, 0, q0 + 5_000_000 + k * 100_000, pcm) for k in range(10)]    # after 0.2 s of nothing
    packets += [(160, AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR, 0, pcm)]                # no time: follows on
    src, made, cap = _source(packets, refuse={(RATE, 1, 32)})                      # float refused: int16 taken
    src.start()
    try:
        assert src.format == Format(RATE, 1, 16) and src.opened_as == "process_loopback 16000 Hz int16 x1"
        assert src.tried[0].startswith("initialize 0x88890008") and len(made) == 2   # a fresh client per try
        assert made[-1].flags == STREAM_FLAGS and made[-1].started
        blocks = _read_all(src, 9600)                                               # 6 full blocks (0.6 s)
    finally:
        src.stop()
    audio = np.concatenate([b for _, b in blocks])
    wall0 = blocks[0][0]
    assert abs(wall0 - lb.wall_of(q0, *lb.Qpc().now())) < 1.0
    assert np.allclose(audio[:160], tone, atol=1e-3)                               # int16 back to float
    assert (audio[3200:4800] == 0).all()                                           # the SILENT packets
    assert (audio[4800:8000] == 0).all()                                           # the 0.2 s gap, filled
    assert np.allclose(audio[8000:8160], tone, atol=1e-3)                          # back at +0.5 s
    assert src.stats.packets == 41 and src.stats.silent_packets == 10 and src.stats.timestamp_errors == 1
    assert cap[0].released == [160] * 41                                           # every packet released
    assert made[-1].stopped and not src.alive


def test_loopback_source_resamples_48k_stereo_when_only_that_is_accepted():
    q0 = lb.Qpc().now()[1]
    raw = np.zeros((480, 2), np.int16).tobytes()
    packets = [(480, 0, q0 + k * 100_000, raw) for k in range(30)]
    src, made, _cap = _source(packets, refuse={(RATE, 1, 32), (RATE, 1, 16)})
    src.start()
    try:
        assert src.format == Format(48_000, 2, 16)
        blocks = _read_all(src, 3200)
    finally:
        src.stop()
    assert sum(len(b) for _, b in blocks) >= 3200                               # 0.3 s at 48k: 2+ blocks at 16k
    assert blocks[1][0] - blocks[0][0] == pytest.approx(0.1)


def test_an_activation_that_fails_is_unavailable_with_its_reason():
    def refuse(pid, tree, timeout):
        raise LoopbackUnavailable("activate_failed 0x80070005")

    src = LoopbackSource(4242, activate_fn=refuse)
    with pytest.raises(LoopbackUnavailable, match="activate_failed"):
        src.start()
    assert not src.alive


def test_repeated_start_stop_leaves_no_thread_behind():
    before = threading.active_count()
    q0 = lb.Qpc().now()[1]
    for _ in range(5):
        src, _made, _cap = _source([(160, 0, q0, np.zeros(160, np.float32).tobytes())])
        src.start()
        src.stop()
        assert not src.alive
    assert threading.active_count() <= before + 1


# -- who takes the microphone ------------------------------------------------------------------------------------


def test_capture_watch_polls_when_given_sessions_and_reports_changes():
    seen: list[tuple[int, bool]] = []
    sessions = [{"pid": 10, "state": "active"}, {"pid": 20, "state": "inactive"}]
    w = CaptureWatch(lambda at, pid, on: seen.append((pid, on)), poll_s=0.01, sessions_fn=lambda: list(sessions),
                     exclude_pids={30})
    w.start()
    try:
        deadline = time.monotonic() + 2
        while (10, True) not in seen and time.monotonic() < deadline:
            time.sleep(0.01)
        assert w.mode == "poll" and (10, True) in seen and w.active_pids() == {10}
        sessions[:] = [{"pid": 10, "state": "active"}, {"pid": 20, "state": "active"}, {"pid": 30, "state": "active"}]
        while (20, True) not in seen and time.monotonic() < deadline:
            time.sleep(0.01)
        sessions[:] = [{"pid": 20, "state": "inactive"}]                 # 10 gone, 20 stopped
        while (20, False) not in seen and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        w.stop()
    assert (10, False) in seen and (20, False) in seen and all(pid != 30 for pid, _ in seen)


class FakeSession(COMObject):
    """A capture session of process 4321 (``IAudioSessionControl2``): keeps the events sink it is given."""

    _com_interfaces_ = [audio.IAudioSessionControl2]

    def __init__(self, state: int = 0) -> None:
        super().__init__()
        self.state = state
        self.sink = None
        self.unregistered = 0

    def GetState(self, this, state):   # noqa: N802
        state[0] = self.state
        return 0

    def GetProcessId(self, this, pid):   # noqa: N802
        pid[0] = 4321
        return 0

    def RegisterAudioSessionNotification(self, this, client):   # noqa: N802
        self.sink = int(client)
        return 0

    def UnregisterAudioSessionNotification(self, this, client):   # noqa: N802
        self.unregistered += 1
        return 0


def test_capture_session_events_reach_the_watch_through_com():
    seen: list[tuple[int, bool]] = []
    w = CaptureWatch(lambda at, pid, on: seen.append((pid, on)))
    session = FakeSession(state=0)
    w.watch_session(session.QueryInterface(audio.IAudioSessionControl))
    assert session.sink and seen == []                                   # inactive: nothing to say yet
    sink = ctypes.cast(c_void_p(session.sink), POINTER(IAudioSessionEvents))
    sink.AddRef()
    sink.OnStateChanged(1)                                                 # push-to-talk down: the stream starts
    w._drain()
    sink.OnStateChanged(0)                                                 # released
    w._drain()
    sink.OnStateChanged(1)
    sink.OnSessionDisconnected(0)                                          # the app went away
    w._drain()
    assert seen == [(4321, True), (4321, False), (4321, True), (4321, False)]
    w._unregister()
    assert session.unregistered == 1


# -- review fixes (report 60, "Review fixes") ------------------------------------------------------------------


class FakeOperation(COMObject):
    """``IActivateAudioInterfaceAsyncOperation`` handing out ``client`` as a COM out-parameter would (AddRef'd)."""

    _com_interfaces_ = [lb.IActivateAudioInterfaceAsyncOperation]

    def __init__(self, client) -> None:
        super().__init__()
        self.client = client

    def GetActivateResult(self, this, result, interface):   # noqa: N802
        from comtypes import IUnknown

        result[0] = 0
        ptr = self.client.QueryInterface(IUnknown)
        ptr.AddRef()                                   # the caller's reference
        ctypes.cast(interface, POINTER(c_void_p))[0] = ctypes.cast(ptr, c_void_p).value
        return 0


def _count(obj) -> int:
    import gc

    gc.collect()
    return int(obj._refcnt.value)


def test_r4_activation_keeps_no_extra_reference_to_the_operation_or_the_client():
    """Review finding 4: the completion handler must not AddRef the operation (comtypes holds its own reference for
    the call's wrapper, measured), and the activated client is handed over once and released by its owner."""
    client = FakeClient(FakeCapture([]), set())
    op = FakeOperation(client)
    system_ref = op.QueryInterface(lb.IActivateAudioInterfaceAsyncOperation)   # what Windows holds
    handler = lb._Completion()
    handler_ref = handler.QueryInterface(lb.IActivateAudioInterfaceCompletionHandler)
    handler_ref.ActivateCompleted(system_ref)                   # called through the vtable, as Windows calls it
    assert handler.done.is_set() and handler.hr == 0 and handler.error is None
    assert _count(op) == 1                                       # only Windows' own
    assert _count(client) == 1                                   # the handler's, until it is taken
    taken = handler.take()
    assert handler.client is None and _count(client) == 1
    del taken
    assert _count(client) == 0                                   # released by its owner, not the cycle collector
    del handler_ref, system_ref


def test_r4_a_new_capture_session_is_held_once_and_let_go_on_unregister():
    watch = CaptureWatch(lambda at, pid, on: None)
    session = FakeSession()
    system_ref = session.QueryInterface(audio.IAudioSessionControl)   # what Windows holds
    sink = lb._SessionCreated(watch._events)
    sink.QueryInterface(lb.IAudioSessionNotification).OnSessionCreated(system_ref)
    watch._drain()                                               # follows it: one reference of ours
    assert _count(session) == 2 and session.sink
    watch._unregister()
    assert _count(session) == 1                                  # only Windows' own left
    del system_ref


def test_r5_a_capture_error_ends_the_thread_with_its_reason_and_closes_the_handle():
    q0 = lb.Qpc().now()[1]
    packets = [(160, 0, q0, np.zeros(160, np.float32).tobytes()), "error"]
    src, _made, _cap = _source(packets)
    src.start()
    deadline = time.monotonic() + 5
    while src.alive and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not src.alive and not src.ready and "GetBuffer 0x88890004" in (src.error or "")
    assert src._event is None                                    # closed by the thread, under the lock
    src.stop()                                                   # never sets a closed handle
    assert not src.alive


def test_r8_a_source_opening_in_the_background_never_holds_the_caller():
    gate = threading.Event()

    def slow(pid, tree, timeout):
        gate.wait(5)
        client = FakeClient(FakeCapture([]), set())
        _FAKES.append(client)
        return client.QueryInterface(IAudioClient)

    src = LoopbackSource(4242, activate_fn=slow, wait=False, drain_s=0.01)
    t = time.perf_counter()
    src.start()
    assert time.perf_counter() - t < 0.2 and src.alive and not src.ready
    gate.set()
    deadline = time.monotonic() + 5
    while not src.ready and time.monotonic() < deadline:
        time.sleep(0.01)
    assert src.ready and src.opened_as.startswith("process_loopback")
    src.stop()
    assert not src.alive

    def refuse(pid, tree, timeout):
        raise LoopbackUnavailable("activate_failed 0x80070005")

    failed = LoopbackSource(4242, activate_fn=refuse, wait=False)
    failed.start()                                               # returns at once; the failure shows after
    deadline = time.monotonic() + 5
    while failed.alive and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not failed.alive and "activate_failed" in failed.error


def test_r8_stopping_a_source_still_opening_returns_at_once():
    gate = threading.Event()

    def hung(pid, tree, timeout):
        gate.wait(5)
        raise LoopbackUnavailable("activate_timeout")

    src = LoopbackSource(4242, activate_fn=hung, wait=False)
    src.start()
    t = time.perf_counter()
    src.stop()
    assert time.perf_counter() - t < 1.0                         # never the 3 s join of a running stream
    gate.set()
    deadline = time.monotonic() + 5
    while src.alive and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not src.alive and src.error is None                   # stopped while opening: not a failure
