"""An app's own sound through Windows process loopback, and which apps take the microphone when.

Owner's decision 2026-10-03 (docs/HANDBOOK.md rule 9, docs/MEMORY.md "Calls: both sides"): a call is
recorded on both sides - the user's microphone (headphones only, as before) plus the call app's own
audio output - and a game's own voice chat is a track of its own. Never the whole output endpoint:
music, a game that is not the target and Yuki's own voice stay out, because only the target
process tree is captured.

* :class:`LoopbackSource` - process loopback capture of one process tree:
  ``ActivateAudioInterfaceAsync`` on ``VAD\\Process_Loopback`` with
  ``PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE``, ``IAudioClient::Initialize`` in shared mode
  (loopback, event callback, the engine converting to an explicit format: process loopback has no
  mix format) and ``IAudioCaptureClient`` reads, on its own thread (COM multithreaded, below-normal
  priority). It hands out 0.1 s blocks of 16 kHz mono float32 with the wall time of their first
  sample - the same ``start`` / ``read`` / ``stop`` as :class:`yuki.memory.calls.MicSource`, so the
  speech gate, the chunker and the spool are shared. Windows delivers nothing while the app is
  silent, so every packet is stamped from its QPC position and :class:`Restamper` keeps the block
  times true across the gaps.
* :class:`CaptureWatch` - which processes take the microphone, moment by moment: the active /
  inactive state of every capture audio session (``IAudioSessionEvents::OnStateChanged``, event
  driven; a coarse poll of :func:`yuki.perception.audio.capture_sessions` only when the events cannot
  be registered). Session state only: nothing is captured. A game that opens its capture stream only
  while push-to-talk is held shows up here as short active spans (not verified live: see
  ``scripts/loopback_probe.py``).
* :func:`process_table`, :func:`tree_root`, :func:`app_root_pid` - processes, their parents and image
  names from one Toolhelp snapshot: no process handle is opened, so a game protected by an
  anti-cheat driver is never touched (its own pid comes from its full-screen window).

Process loopback needs Windows 10 build 19041 or later (this PC: 26100). On an older build, or when
activation fails, the app's track is ``unavailable`` and the microphone goes on as before.
"""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from ctypes import POINTER, byref, c_float, c_int, c_long, c_uint32, c_ulong, c_ulonglong, c_ushort, c_void_p
from ctypes import c_wchar_p, wintypes
from dataclasses import dataclass, field
from typing import Any

import comtypes
import numpy as np
from comtypes import COMMETHOD, GUID, HRESULT, COMObject, IUnknown

from yuki.perception import audio as _audio

RATE = 16_000
#: Process loopback arrived in Windows 10 2004 (OBS's "Application Audio Capture" needs the same).
MIN_BUILD = 19041
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
STREAM_FLAGS = (AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR = 0x4
AUDCLNT_S_BUFFER_EMPTY = 0x08890001
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
VT_BLOB = 65
#: The shared buffer asked for (100 ns units): 1 s, so the reader may wake every few tens of ms.
BUFFER_HNS = 10_000_000
#: Windows' AudioSessionState.
SESSION_ACTIVE = 1


class LoopbackUnavailable(RuntimeError):
    """The app's own audio cannot be captured (old Windows, no such process, activation refused)."""


# ---------------------------------------------------------------------------
# Structures and formats (pure: unit-tested without any audio)
# ---------------------------------------------------------------------------


class WAVEFORMATEX(ctypes.Structure):
    """``mmreg.h``'s WAVEFORMATEX (byte-packed, 18 bytes)."""

    _pack_ = 1
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", c_int)]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", c_int), ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS)]


class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", c_ulong), ("pBlobData", c_void_p)]


class PROPVARIANT_BLOB(ctypes.Structure):
    """A PROPVARIANT holding a VT_BLOB (24 bytes on x64: the BLOB starts at offset 8)."""

    _fields_ = [("vt", c_ushort), ("wReserved1", c_ushort), ("wReserved2", c_ushort), ("wReserved3", c_ushort),
                ("blob", BLOB)]


@dataclass(frozen=True)
class Format:
    """A PCM format the audio engine is asked to deliver (it converts: AUTOCONVERTPCM)."""

    rate: int
    channels: int
    bits: int
    is_float: bool = False

    @property
    def block_align(self) -> int:
        return self.channels * self.bits // 8

    def wave(self) -> WAVEFORMATEX:
        return WAVEFORMATEX(WAVE_FORMAT_IEEE_FLOAT if self.is_float else WAVE_FORMAT_PCM, self.channels, self.rate,
                            self.rate * self.block_align, self.block_align, self.bits, 0)

    def label(self) -> str:
        return f"{self.rate} Hz {'float' if self.is_float else 'int'}{self.bits} x{self.channels}"


#: Asked for in this order; the first one the engine accepts is kept. 16 kHz mono float needs no work here.
FORMATS: tuple[Format, ...] = (Format(RATE, 1, 32, True), Format(RATE, 1, 16), Format(48_000, 2, 16),
                               Format(44_100, 2, 16))


def activation_params(pid: int, include_tree: bool = True) -> tuple[AUDIOCLIENT_ACTIVATION_PARAMS, PROPVARIANT_BLOB]:
    """The process-loopback activation parameters for ``pid`` and the VT_BLOB PROPVARIANT pointing at them
    (keep both alive until the activation completed)."""
    mode = (PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE if include_tree
            else PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE)
    params = AUDIOCLIENT_ACTIVATION_PARAMS(AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
                                           AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(int(pid), mode))
    pv = PROPVARIANT_BLOB()
    pv.vt = VT_BLOB
    pv.blob.cbSize = ctypes.sizeof(params)
    pv.blob.pBlobData = ctypes.addressof(params)
    return params, pv


def to_mono(raw: bytes, fmt: Format) -> np.ndarray:
    """One packet's bytes as mono float32 at ``fmt.rate``: channels averaged (an app's voice sits in the middle)."""
    if fmt.is_float:
        data = np.frombuffer(raw, dtype=np.float32)
    elif fmt.bits == 16:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    else:
        raise ValueError(f"unsupported format {fmt}")
    if fmt.channels > 1:
        usable = len(data) - len(data) % fmt.channels
        data = data[:usable].reshape(-1, fmt.channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32)


class Resampler:
    """Mono float32 at ``rate`` -> 16 kHz (PyAV's resampler, as :class:`yuki.memory.calls.MicSource` uses); a
    pass-through at 16 kHz."""

    def __init__(self, rate: int) -> None:
        self.rate = int(rate)
        self._r: Any = None
        if self.rate != RATE:
            import av

            self._r = av.AudioResampler(format="flt", layout="mono", rate=RATE)

    def __call__(self, mono: np.ndarray) -> np.ndarray:
        if self._r is None or not len(mono):
            return mono
        import av

        frame = av.AudioFrame.from_ndarray(mono.reshape(1, -1), format="flt", layout="mono")
        frame.sample_rate = self.rate
        out = [f.to_ndarray().reshape(-1) for f in self._r.resample(frame)]
        return np.concatenate(out).astype(np.float32) if out else np.zeros(0, np.float32)


class Restamper:
    """Stamped packets -> blocks of ``block`` samples (16 kHz mono) with the wall time of their first sample.

    ``push(at, samples)``: ``at`` is the packet's first sample in wall time (from its QPC position), or
    ``None`` when Windows could not say (``TIMESTAMP_ERROR``): then it follows the last packet. A packet
    later than the last one ended by more than ``tolerance_s`` follows a gap (the app was silent and
    Windows delivered nothing): up to ``fill_max_s`` of the gap becomes silence, so the speech gate sees
    the pause and a chunk in progress closes at its pause rule; past that the rest of the block is handed
    out short and the next block starts at the packet's own time (a jump the chunker treats as a break).
    A packet earlier than expected (clock jitter) is taken as contiguous. Times never drift: every block
    starts where the samples before it ended, or at a measured packet time.
    """

    def __init__(self, *, block: int = int(RATE * 0.1), fill_max_s: float = 3.0, tolerance_s: float = 0.02) -> None:
        self.block = int(block)
        self.fill_max_s = float(fill_max_s)
        self.tolerance_s = float(tolerance_s)
        self._parts: list[np.ndarray] = []
        self._n = 0
        self._at: float | None = None       # wall time of the first buffered sample
        self._next: float | None = None     # wall time just after the last sample pushed
        self.gaps = 0
        self.gap_s = 0.0
        self.jumps = 0
        self.early = 0

    def _take(self, n: int) -> np.ndarray:
        data = np.concatenate(self._parts) if len(self._parts) > 1 else self._parts[0]
        out, rest = data[:n], data[n:]
        self._parts = [rest] if len(rest) else []
        self._n = len(rest)
        return out

    def _emit_full(self) -> list[tuple[float, np.ndarray]]:
        out = []
        while self._n >= self.block and self._at is not None:
            out.append((self._at, self._take(self.block)))
            self._at += self.block / RATE
        if not self._n:
            self._at = None
        return out

    def _append(self, at: float, samples: np.ndarray) -> list[tuple[float, np.ndarray]]:
        if not len(samples):
            return []
        if self._at is None:
            self._at = at
        self._parts.append(np.asarray(samples, dtype=np.float32))
        self._n += len(samples)
        self._next = at + len(samples) / RATE
        return self._emit_full()

    def push(self, at: float | None, samples: np.ndarray) -> list[tuple[float, np.ndarray]]:
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if self._next is None:
            return self._append(at if at is not None else time.time() - len(samples) / RATE, samples)
        if at is None:
            return self._append(self._next, samples)
        gap = at - self._next
        if gap < -self.tolerance_s:
            self.early += 1
            return self._append(self._next, samples)
        if gap <= self.tolerance_s:
            return self._append(self._next, samples)
        self.gaps += 1
        self.gap_s += gap
        out: list[tuple[float, np.ndarray]] = []
        if gap <= self.fill_max_s + self.tolerance_s:
            out += self._append(self._next, np.zeros(int(round(gap * RATE)), np.float32))
            out += self._append(self._next, samples)
            return out
        out += self._append(self._next, np.zeros(int(round(self.fill_max_s * RATE)), np.float32))
        out += self.flush()
        self.jumps += 1
        out += self._append(at, samples)
        return out

    def flush(self) -> list[tuple[float, np.ndarray]]:
        """What is left (a short block), then nothing buffered."""
        if not self._n or self._at is None:
            self._parts, self._n, self._at = [], 0, None
            return []
        at = self._at
        out = [(at, self._take(self._n))]
        self._at = None
        return out


# ---------------------------------------------------------------------------
# Processes from one Toolhelp snapshot (no process handle is opened)
# ---------------------------------------------------------------------------


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE = c_void_p(-1).value


def _kernel32() -> Any:
    """A private kernel32 (its argtypes never clash with other modules' declarations)."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = c_void_p
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32FirstW.argtypes = [c_void_p, POINTER(PROCESSENTRY32W)]
    k.Process32NextW.argtypes = [c_void_p, POINTER(PROCESSENTRY32W)]
    k.CloseHandle.argtypes = [c_void_p]
    k.CreateEventW.restype = c_void_p
    k.CreateEventW.argtypes = [c_void_p, c_int, c_int, c_wchar_p]
    k.SetEvent.argtypes = [c_void_p]
    k.WaitForSingleObject.argtypes = [c_void_p, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.QueryPerformanceCounter.argtypes = [POINTER(ctypes.c_longlong)]
    k.QueryPerformanceFrequency.argtypes = [POINTER(ctypes.c_longlong)]
    k.GetCurrentThread.restype = c_void_p
    k.SetThreadPriority.argtypes = [c_void_p, c_int]
    return k


_K32: Any = None


def k32() -> Any:
    global _K32
    if _K32 is None:
        _K32 = _kernel32()
    return _K32


def process_table() -> list[tuple[int, int, str]]:
    """``(pid, parent pid, image name)`` of every process (CreateToolhelp32Snapshot; no process is opened)."""
    k = k32()
    snap = k.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        return []
    out: list[tuple[int, int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k.Process32FirstW(snap, byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), str(entry.szExeFile)))
            ok = k.Process32NextW(snap, byref(entry))
    finally:
        k.CloseHandle(snap)
    return out


def tree_root(pid: int, parents: dict[int, int], names: dict[int, str], *, limit: int = 64) -> int:
    """The top of ``pid``'s run of ancestors that run the same image (an app's main process: Discord's helpers
    and renderers are its children). Stops at an unknown or different parent, a cycle, or ``limit`` steps."""
    name = (names.get(pid) or "").casefold()
    cur, seen = int(pid), {int(pid)}
    for _ in range(limit):
        parent = parents.get(cur)
        if parent is None or parent in seen or (names.get(parent) or "").casefold() != name or not name:
            return cur
        seen.add(parent)
        cur = parent
    return cur


def _descendants(root: int, children: dict[int, list[int]]) -> set[int]:
    out, todo = {root}, [root]
    while todo:
        for child in children.get(todo.pop(), ()):
            if child not in out:
                out.add(child)
                todo.append(child)
    return out


def app_root_pid(image: str, table: Sequence[tuple[int, int, str]], playing: Iterable[int] = ()) -> int | None:
    """The process whose tree is the app ``image`` ("Discord.exe"): every process running that image walked up to
    its root (:func:`tree_root`); of several roots, the one whose tree plays sound now (``playing``: pids with an
    output session), else the largest tree. None when nothing runs the image."""
    want = (image or "").casefold()
    if not want:
        return None
    names = {pid: name for pid, _parent, name in table}
    parents = {pid: parent for pid, parent, _name in table}
    pids = [pid for pid, _parent, name in table if name.casefold() == want]
    if not pids:
        return None
    roots = sorted({tree_root(p, parents, names) for p in pids})
    if len(roots) == 1:
        return roots[0]
    children: dict[int, list[int]] = {}
    for pid, parent, _name in table:
        if parent != pid:
            children.setdefault(parent, []).append(pid)
    sounding = set(int(p) for p in playing)
    trees = {r: _descendants(r, children) for r in roots}
    return min(roots, key=lambda r: (-len(trees[r] & sounding), -len(trees[r]), r))


# ---------------------------------------------------------------------------
# Core Audio: the loopback client (COM declarations used here only)
# ---------------------------------------------------------------------------

class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize", (["in"], c_int, "share_mode"), (["in"], wintypes.DWORD, "flags"),
                  (["in"], ctypes.c_longlong, "buffer_hns"), (["in"], ctypes.c_longlong, "period_hns"),
                  (["in"], POINTER(WAVEFORMATEX), "format"), (["in"], POINTER(GUID), "session")),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "frames")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(ctypes.c_longlong), "latency")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32), "frames")),
        COMMETHOD([], HRESULT, "IsFormatSupported", (["in"], c_int, "share_mode"),
                  (["in"], POINTER(WAVEFORMATEX), "format"), (["out"], POINTER(c_void_p), "closest")),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(c_void_p), "format")),
        COMMETHOD([], HRESULT, "GetDevicePeriod", (["out"], POINTER(ctypes.c_longlong), "default"),
                  (["out"], POINTER(ctypes.c_longlong), "minimum")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], c_void_p, "event")),
        COMMETHOD([], HRESULT, "GetService", (["in"], POINTER(GUID), "iid"), (["out"], POINTER(c_void_p), "service")),
    ]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer", (["out"], POINTER(c_void_p), "data"), (["out"], POINTER(c_uint32), "frames"),
                  (["out"], POINTER(wintypes.DWORD), "flags"), (["out"], POINTER(c_ulonglong), "device_position"),
                  (["out"], POINTER(c_ulonglong), "qpc_position")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "frames")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "frames")),
    ]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetActivateResult", (["out"], POINTER(c_long), "result"),
                  (["out"], POINTER(POINTER(IUnknown)), "interface")),
    ]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [
        COMMETHOD([], HRESULT, "ActivateCompleted", (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "op")),
    ]


class IAgileObject(IUnknown):
    """A marker: the object may be called from any apartment (the completion arrives on a system thread)."""

    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_: list = []


class _Completion(COMObject):
    """``IActivateAudioInterfaceCompletionHandler`` + ``IAgileObject``: keeps the activation's result and sets
    :attr:`done` (the capture thread waits on it)."""

    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self) -> None:
        super().__init__()
        self.done = threading.Event()
        self.hr: int | None = None
        self.client: Any = None
        self.error: str | None = None

    def ActivateCompleted(self, op: Any) -> None:   # noqa: N802 - the COM method's name
        # ``op`` is comtypes' own reference for the call (measured: it holds one while the wrapper lives and releases
        # it after), so no AddRef here - one would leak the operation, and with it the activated client.
        try:
            hr, unknown = op.GetActivateResult()
            self.hr = int(hr)
            if self.hr >= 0 and unknown:
                self.client = unknown.QueryInterface(IAudioClient)
            del unknown
        except Exception as exc:   # reported to the waiting thread, never raised into Windows
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.done.set()

    def take(self) -> Any:
        """The activated client, handed over once: the caller owns its only reference (released on the capture
        thread when it is done, never later by the cycle collector holding this handler)."""
        client, self.client = self.client, None
        return client


_ACTIVATE: Any = None


def _activate_function() -> Any:
    global _ACTIVATE
    if _ACTIVATE is None:
        fn = ctypes.WinDLL("Mmdevapi").ActivateAudioInterfaceAsync
        fn.argtypes = [c_wchar_p, POINTER(GUID), POINTER(PROPVARIANT_BLOB), c_void_p, POINTER(c_void_p)]
        fn.restype = c_long
        _ACTIVATE = fn
    return _ACTIVATE


def _hr(value: int) -> str:
    return f"0x{int(value) & 0xFFFFFFFF:08X}"


def activate_process_loopback(pid: int, include_tree: bool = True, timeout_s: float = 5.0) -> Any:
    """An ``IAudioClient`` capturing the sound of ``pid`` (and its process tree). Call on an MTA thread.

    Raises :class:`LoopbackUnavailable` (old Windows, refused, timed out)."""
    build = getattr(sys.getwindowsversion(), "build", 0) if sys.platform == "win32" else 0
    if build < MIN_BUILD:
        raise LoopbackUnavailable(f"windows_build_{build}")
    params, pv = activation_params(pid, include_tree)
    handler = _Completion()
    hptr = handler.QueryInterface(IActivateAudioInterfaceCompletionHandler)
    op = c_void_p()
    hr = _activate_function()(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, byref(IAudioClient._iid_), byref(pv),
                              ctypes.cast(hptr, c_void_p), byref(op))
    op_ptr = ctypes.cast(op, POINTER(IUnknown)) if op.value else None   # released when this function returns
    if hr < 0:
        raise LoopbackUnavailable(f"activate_call_failed {_hr(hr)}")
    if not handler.done.wait(timeout_s):
        raise LoopbackUnavailable("activate_timeout")
    del op_ptr, params
    client = handler.take()
    if handler.error:
        raise LoopbackUnavailable(f"activate_completion_error {handler.error}")
    if handler.hr is None or handler.hr < 0 or client is None:
        raise LoopbackUnavailable(f"activate_failed {_hr(handler.hr or 0)}")
    return client


_GET_BUFFER = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(c_void_p), POINTER(c_uint32), POINTER(wintypes.DWORD),
                                 POINTER(c_ulonglong), POINTER(c_ulonglong))
_RELEASE_BUFFER = ctypes.WINFUNCTYPE(c_long, c_void_p, c_uint32)


class CaptureReader:
    """``IAudioCaptureClient``'s GetBuffer/ReleaseBuffer called straight through its vtable (the hot path: ~100
    packets a second; comtypes' wrappers cost several times a plain ctypes call)."""

    def __init__(self, capture: Any) -> None:
        self._capture = capture                 # the comtypes pointer keeps the reference
        address = ctypes.cast(capture, c_void_p).value
        vtbl = ctypes.cast(c_void_p(address), POINTER(POINTER(c_void_p)))[0]
        self._this = c_void_p(address)
        self._get = _GET_BUFFER(vtbl[3])
        self._release = _RELEASE_BUFFER(vtbl[4])
        self._data, self._frames, self._flags = c_void_p(), c_uint32(), wintypes.DWORD()
        self._dev, self._qpc = c_ulonglong(), c_ulonglong()

    def packets(self, block_align: int, limit: int = 1000) -> Iterator[tuple[bytes | None, int, int, int]]:
        """``(bytes or None when SILENT, frames, flags, QPC position in 100 ns)`` until the buffer is empty."""
        for _ in range(limit):
            hr = self._get(self._this, byref(self._data), byref(self._frames), byref(self._flags), byref(self._dev),
                           byref(self._qpc))
            if hr < 0:
                raise OSError(f"GetBuffer {_hr(hr)}")
            if hr == AUDCLNT_S_BUFFER_EMPTY:
                return
            frames, flags = int(self._frames.value), int(self._flags.value)
            data = None
            if frames and not flags & AUDCLNT_BUFFERFLAGS_SILENT and self._data.value:
                data = ctypes.string_at(self._data.value, frames * block_align)
            self._release(self._this, frames)
            if not frames:
                return
            yield data, frames, flags, int(self._qpc.value)

    def close(self) -> None:
        self._capture = None


class Qpc:
    """The performance counter in 100 ns units (the unit of ``GetBuffer``'s QPC position) and the wall clock."""

    def __init__(self) -> None:
        freq = ctypes.c_longlong(0)
        k32().QueryPerformanceFrequency(byref(freq))
        self.freq = int(freq.value) or 10_000_000

    def now(self) -> tuple[float, int]:
        """``(wall time, QPC in 100 ns)`` read back to back."""
        count = ctypes.c_longlong(0)
        k32().QueryPerformanceCounter(byref(count))
        wall = time.time()
        return wall, int(count.value) * 10_000_000 // self.freq


def wall_of(qpc_position: int, wall_now: float, qpc_now: int) -> float:
    """A QPC position (100 ns) as wall time, from one (wall, QPC) pair read together."""
    return wall_now - (int(qpc_now) - int(qpc_position)) / 1e7


def _below_normal() -> None:
    try:
        k = k32()
        k.SetThreadPriority(k.GetCurrentThread(), -1)   # THREAD_PRIORITY_BELOW_NORMAL
    except Exception:
        pass


@dataclass
class LoopbackStats:
    packets: int = 0
    silent_packets: int = 0
    frames: int = 0
    discontinuities: int = 0
    timestamp_errors: int = 0
    blocks: int = 0
    cpu_s: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


class LoopbackSource:
    """Process loopback capture of ``pid``'s tree as 16 kHz mono float32 blocks (see the module docstring).

    ``start()`` returns once the stream runs (or raises :class:`LoopbackUnavailable` with the reason); with
    ``wait=False`` it returns at once and the capture thread opens the stream on its own (the call worker's poll
    never waits on Windows): :attr:`ready` says when it runs, :attr:`alive` False with :attr:`error` set says it
    could not open or died (a GetBuffer error, an exception). ``read(timeout_s)`` gives ``(wall time of the
    first sample, samples)`` or None; ``stop()`` ends the capture thread and releases every COM object and handle
    (safe to call again; a new source is made per start). ``activate_fn(pid, include_tree, timeout_s)`` returns
    the ``IAudioClient`` (tests pass a fake; the default is :func:`activate_process_loopback`)."""

    def __init__(self, pid: int, *, include_tree: bool = True, block_s: float = 0.1, drain_s: float = 0.04,
                 activate_timeout_s: float = 5.0, formats: Sequence[Format] = FORMATS,
                 activate_fn: Callable[[int, bool, float], Any] | None = None, label: str = "",
                 wait: bool = True) -> None:
        self.pid = int(pid)
        self.include_tree = include_tree
        self.drain_s = float(drain_s)
        self.activate_timeout_s = float(activate_timeout_s)
        self.formats = tuple(formats)
        self.activate_fn = activate_fn or activate_process_loopback
        self.label = label
        self.wait = bool(wait)
        self._restamper = Restamper(block=int(RATE * block_s))
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._event: Any = None
        #: Guards the event handle: the capture thread closes it, stop() may set it at the same moment.
        self._event_lock = threading.Lock()
        self.format: Format | None = None
        self.error: str | None = None
        self.tried: list[str] = []
        self.device_used: str | None = None
        self.opened_as: str | None = None
        self.overflows = 0
        self.stats = LoopbackStats()

    # -- the caller's side -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="yuki-memory-loopback", daemon=True)
        self._thread.start()
        if not self.wait:
            return
        if not self._ready.wait(self.activate_timeout_s + 2.0):
            self.stop()
            raise LoopbackUnavailable("start_timeout")
        if self.error is not None:
            self.stop()
            raise LoopbackUnavailable(self.error)

    def read(self, timeout_s: float) -> tuple[float, np.ndarray] | None:
        try:
            return self._q.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def stop(self) -> None:
        self._stop.set()
        with self._event_lock:   # never a handle the capture thread has just closed
            if self._event:
                try:
                    k32().SetEvent(self._event)
                except Exception:
                    pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # a stream that runs stops within one wait; one still opening (Windows has not answered the activation)
            # is left to end by itself: the caller's poll is never held up by it
            thread.join(3.0 if self.ready else 0.2)
        while not self._q.empty():   # what was not read is dropped: nothing is kept
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def ready(self) -> bool:
        """The stream runs (it opened, and the capture thread has not ended)."""
        return self._ready.is_set() and self.error is None and self.alive

    # -- the capture thread --------------------------------------------------------

    def _open(self) -> tuple[Any, Format]:
        """Activate and initialise with the first format the engine accepts (a refused Initialize leaves the
        client unusable, so each try activates afresh)."""
        last = "no format"
        for fmt in self.formats:
            client = self.activate_fn(self.pid, self.include_tree, self.activate_timeout_s)
            wave = fmt.wave()
            try:
                client.Initialize(AUDCLNT_SHAREMODE_SHARED, STREAM_FLAGS, BUFFER_HNS, 0, byref(wave), None)
            except comtypes.COMError as exc:
                last = f"initialize {_hr(exc.hresult)} for {fmt.label()}"
                self.tried.append(last)
                del client
                continue
            self.tried.append(f"ok {fmt.label()}")
            return client, fmt
        raise LoopbackUnavailable(last)

    def _run(self) -> None:
        coinit = False
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            coinit = True
        except OSError:
            pass
        _below_normal()
        c0 = time.thread_time()
        client = capture = reader = None
        try:
            client, fmt = self._open()
            if self._stop.is_set():
                raise LoopbackUnavailable("stopped while opening")
            with self._event_lock:
                self._event = k32().CreateEventW(None, False, False, None)
            client.SetEventHandle(self._event)
            raw = client.GetService(byref(IAudioCaptureClient._iid_))
            capture = ctypes.cast(c_void_p(raw), POINTER(IAudioCaptureClient))
            reader = CaptureReader(capture)
            client.Start()
            self.format = fmt
            self.device_used = f"process loopback pid {self.pid}" + (" (tree)" if self.include_tree else "")
            self.opened_as = f"process_loopback {fmt.label()}"
            self._ready.set()
            self._loop(reader, fmt)
            try:
                client.Stop()
            except Exception:
                pass
        except Exception as exc:
            if not (self._stop.is_set() and isinstance(exc, LoopbackUnavailable)):
                self.error = (f"{type(exc).__name__}: {exc}" if not isinstance(exc, LoopbackUnavailable)
                              else str(exc))
        finally:
            for block in self._restamper.flush():
                self._q.put(block)
            if reader is not None:
                reader.close()
            reader = capture = client = None   # comtypes releases them here, before COM goes
            with self._event_lock:
                event, self._event = self._event, None
                if event:
                    try:
                        k32().CloseHandle(event)
                    except Exception:
                        pass
            self.stats.cpu_s += time.thread_time() - c0
            self._ready.set()
            if coinit:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass

    def _loop(self, reader: CaptureReader, fmt: Format) -> None:
        qpc = Qpc()
        resample = Resampler(fmt.rate)
        st = self.stats
        k = k32()
        while not self._stop.is_set():
            k.WaitForSingleObject(self._event, 200)
            if self._stop.is_set():
                break
            wall_now, qpc_now = qpc.now()
            for data, frames, flags, position in reader.packets(fmt.block_align):
                st.packets += 1
                st.frames += frames
                if flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY:
                    st.discontinuities += 1
                    self.overflows += 1
                at: float | None
                if flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR or not position:
                    st.timestamp_errors += 1
                    at = None
                else:
                    at = wall_of(position, wall_now, qpc_now)
                if data is None:
                    st.silent_packets += 1
                    samples = np.zeros(int(round(frames * RATE / fmt.rate)), np.float32)
                else:
                    samples = resample(to_mono(data, fmt))
                for block in self._restamper.push(at, samples):
                    st.blocks += 1
                    self._q.put(block)
            self._stop.wait(self.drain_s)   # let a few packets gather: fewer wake-ups


# ---------------------------------------------------------------------------
# Who takes the microphone: capture sessions' state, event driven
# ---------------------------------------------------------------------------


class IAudioSessionEvents(IUnknown):
    _iid_ = GUID("{24918ACC-64B3-37C1-8CA9-74A66E9957A8}")
    _methods_ = [
        COMMETHOD([], HRESULT, "OnDisplayNameChanged", (["in"], c_wchar_p, "name"), (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "OnIconPathChanged", (["in"], c_wchar_p, "path"), (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "OnSimpleVolumeChanged", (["in"], c_float, "volume"), (["in"], wintypes.BOOL, "mute"),
                  (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "OnChannelVolumeChanged", (["in"], wintypes.DWORD, "count"),
                  (["in"], POINTER(c_float), "volumes"), (["in"], wintypes.DWORD, "changed"),
                  (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "OnGroupingParamChanged", (["in"], POINTER(GUID), "param"),
                  (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "OnStateChanged", (["in"], c_int, "state")),
        COMMETHOD([], HRESULT, "OnSessionDisconnected", (["in"], c_int, "reason")),
    ]


class IAudioSessionNotification(IUnknown):
    _iid_ = GUID("{641DD20B-4D41-49CC-ABA3-174B9477BB08}")
    _methods_ = [
        COMMETHOD([], HRESULT, "OnSessionCreated", (["in"], POINTER(_audio.IAudioSessionControl), "session")),
    ]


class _SessionEvents(COMObject):
    """One capture session's events, queued for the watch thread (nothing is done on Windows' thread)."""

    _com_interfaces_ = [IAudioSessionEvents]

    def __init__(self, events: queue.Queue, key: int) -> None:
        super().__init__()
        self._events = events
        self.key = key

    def OnDisplayNameChanged(self, name: Any, ctx: Any) -> None:   # noqa: N802 - COM names
        return None

    def OnIconPathChanged(self, path: Any, ctx: Any) -> None:   # noqa: N802
        return None

    def OnSimpleVolumeChanged(self, volume: Any, mute: Any, ctx: Any) -> None:   # noqa: N802
        return None

    def OnChannelVolumeChanged(self, count: Any, volumes: Any, changed: Any, ctx: Any) -> None:   # noqa: N802
        return None

    def OnGroupingParamChanged(self, param: Any, ctx: Any) -> None:   # noqa: N802
        return None

    def OnStateChanged(self, state: int) -> None:   # noqa: N802
        self._events.put(("state", self.key, int(state), time.time()))

    def OnSessionDisconnected(self, reason: int) -> None:   # noqa: N802
        self._events.put(("gone", self.key, int(reason), time.time()))


class _SessionCreated(COMObject):
    """New capture sessions on one device, queued for the watch thread (it registers for their events there)."""

    _com_interfaces_ = [IAudioSessionNotification]

    def __init__(self, events: queue.Queue) -> None:
        super().__init__()
        self._events = events

    def OnSessionCreated(self, session: Any) -> None:   # noqa: N802
        # comtypes holds a reference for the wrapper while it lives (measured), so the queued wrapper keeps the
        # session alive and releases it when dropped: no AddRef here (one would leak every session).
        try:
            self._events.put(("created", session, 0, time.time()))
        except Exception:
            pass


def _address(ptr: Any) -> int:
    return int(ctypes.cast(ptr, c_void_p).value or 0)


class CaptureWatch:
    """Which processes are taking the microphone, as Core Audio's capture sessions say (see the module docstring).

    ``on_change(at, pid, active)`` is called on the watch's own thread whenever a process's capture goes active
    (any of its sessions running) or inactive (none). ``start()`` registers for the session events on every
    active capture device (``mode`` "events"); when that fails it polls ``sessions_fn`` every ``poll_s`` (``mode``
    "poll"; tests pass a stub, which also selects polling). ``stop()`` unregisters everything, reports every
    still-active process inactive at that moment, and ends the thread. ``exclude_pids``: never reported.
    """

    def __init__(self, on_change: Callable[[float, int, bool], None], *, poll_s: float = 2.0,
                 sessions_fn: Callable[[], list[dict]] | None = None, exclude_pids: Iterable[int] = (),
                 log: Callable[..., Any] | None = None, events: bool = True) -> None:
        self.on_change = on_change
        self.poll_s = float(poll_s)
        self._sessions_fn = sessions_fn
        self.exclude = {int(p) for p in exclude_pids}
        self._log_fn = log
        self._use_events = events and sessions_fn is None
        self.mode = "stopped"
        self._events: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state: dict[Any, tuple[int, bool]] = {}   # session key -> (pid, active)
        self._active: set[int] = set()
        self._managers: list[tuple[Any, Any, Any]] = []
        self._sessions: dict[int, tuple[Any, Any, Any]] = {}
        self._next_key = 0
        self.counters = {"events": 0, "changes": 0, "sessions": 0, "polls": 0, "errors": 0}

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is not None:
            try:
                self._log_fn(type, **fields)
            except Exception:
                pass

    # -- control -------------------------------------------------------------

    def start(self, timeout_s: float = 3.0) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="yuki-memory-micwatch", daemon=True)
        self._thread.start()
        self._ready.wait(timeout_s)

    def stop(self, timeout_s: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)

    def active_pids(self) -> set[int]:
        with self._lock:
            return set(self._active)

    # -- aggregation (pure: the same for events and polls) ------------------------

    def _set(self, key: Any, pid: int, active: bool, at: float) -> None:
        if pid in self.exclude or pid <= 0:
            return
        with self._lock:
            self._state[key] = (pid, active)
            now_active = any(p == pid and a for p, a in self._state.values())
            was = pid in self._active
            if now_active == was:
                return
            if now_active:
                self._active.add(pid)
            else:
                self._active.discard(pid)
        self.counters["changes"] += 1
        try:
            self.on_change(at, pid, now_active)
        except Exception as exc:
            self.counters["errors"] += 1
            self._log("error", where="loopback.capture_watch", error=f"{type(exc).__name__}: {exc}")

    def _forget(self, key: Any, at: float) -> None:
        with self._lock:
            hit = self._state.get(key)
        if hit is not None:
            self._set(key, hit[0], False, at)
            with self._lock:
                self._state.pop(key, None)

    # -- the thread ------------------------------------------------------------

    def _run(self) -> None:
        coinit = False
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            coinit = True
        except OSError:
            pass
        try:
            if self._use_events:
                try:
                    self._register()
                    self.mode = "events"
                except Exception as exc:
                    self.counters["errors"] += 1
                    self._log("call_micwatch_events_failed", error=f"{type(exc).__name__}: {exc}")
                    self._unregister()
                    self.mode = "poll"
            else:
                self.mode = "poll"
            self._ready.set()
            while not self._stop.is_set():
                if self.mode == "events":
                    self._drain()
                    self._stop.wait(0.25)
                else:
                    self._poll()
                    self._stop.wait(self.poll_s)
        except Exception as exc:
            self.counters["errors"] += 1
            self._log("error", where="loopback.capture_watch_run", error=f"{type(exc).__name__}: {exc}")
        finally:
            self._ready.set()
            try:
                self._drain()
            except Exception:
                pass
            self._unregister()
            now = time.time()
            with self._lock:
                keys = list(self._state)
            for key in keys:
                self._forget(key, now)
            self.mode = "stopped"
            if coinit:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass

    def _register(self) -> None:
        audio = _audio
        enum = comtypes.CoCreateInstance(audio._CLSID_MMDeviceEnumerator, audio.IMMDeviceEnumerator,
                                         comtypes.CLSCTX_ALL)
        devices = enum.EnumAudioEndpoints(audio.E_CAPTURE, 0x1)   # DEVICE_STATE_ACTIVE
        for i in range(devices.GetCount()):
            device = devices.Item(i)
            raw = device.Activate(byref(audio.IAudioSessionManager2._iid_), comtypes.CLSCTX_ALL, None)
            manager = ctypes.cast(c_void_p(raw), POINTER(audio.IAudioSessionManager2))
            sink = _SessionCreated(self._events)
            sink_ptr = sink.QueryInterface(IAudioSessionNotification)
            manager.RegisterSessionNotification(_address(sink_ptr))
            self._managers.append((manager, sink, sink_ptr))
            sessions = manager.GetSessionEnumerator()   # this also starts the notifications
            for j in range(sessions.GetCount()):
                self.watch_session(sessions.GetSession(j))

    def watch_session(self, control: Any) -> None:
        """Follow one capture session (an ``IAudioSessionControl``): its process and state now, then its events."""
        try:
            pid = int(control.QueryInterface(_audio.IAudioSessionControl2).GetProcessId())
        except comtypes.COMError:
            pid = 0
        state = int(control.GetState())
        key = self._next_key
        self._next_key += 1
        sink = _SessionEvents(self._events, key)
        sink_ptr = sink.QueryInterface(IAudioSessionEvents)
        control.RegisterAudioSessionNotification(_address(sink_ptr))
        self._sessions[key] = (control, sink, sink_ptr)
        self.counters["sessions"] += 1
        with self._lock:
            self._state[key] = (pid, False)
        self._set(key, pid, state == SESSION_ACTIVE, time.time())

    def _drain(self) -> None:
        while True:
            try:
                kind, what, value, at = self._events.get_nowait()
            except queue.Empty:
                return
            self.counters["events"] += 1
            if kind == "created":
                try:
                    self.watch_session(what)
                except Exception as exc:
                    self.counters["errors"] += 1
                    self._log("error", where="loopback.capture_watch_session", error=f"{type(exc).__name__}: {exc}")
            elif kind == "state":
                with self._lock:
                    hit = self._state.get(what)
                if hit is not None:
                    self._set(what, hit[0], value == SESSION_ACTIVE, at)
            elif kind == "gone":
                self._forget(what, at)

    def _poll(self) -> None:
        self.counters["polls"] += 1
        try:
            sessions = self._sessions_fn() if self._sessions_fn is not None else _audio.capture_sessions()
        except Exception as exc:
            self.counters["errors"] += 1
            self._log("error", where="loopback.capture_watch_poll", error=f"{type(exc).__name__}: {exc}")
            return
        now = time.time()
        seen: dict[int, bool] = {}
        for s in sessions:
            pid = int(s.get("pid") or 0)
            seen[pid] = seen.get(pid, False) or s.get("state") == "active"
        with self._lock:
            known = {k for k in self._state if isinstance(k, tuple)}
        for pid, active in seen.items():
            self._set(("poll", pid), pid, active, now)
        for key in known - {("poll", pid) for pid in seen}:
            self._forget(key, now)

    def _unregister(self) -> None:
        for control, _sink, sink_ptr in list(self._sessions.values()):
            try:
                control.UnregisterAudioSessionNotification(_address(sink_ptr))
            except Exception:
                pass
        self._sessions.clear()
        for manager, _sink, sink_ptr in self._managers:
            try:
                manager.UnregisterSessionNotification(_address(sink_ptr))
            except Exception:
                pass
        self._managers.clear()


__all__ = [
    "LoopbackSource", "LoopbackUnavailable", "LoopbackStats", "CaptureWatch", "CaptureReader", "Restamper",
    "Resampler", "Format", "FORMATS", "WAVEFORMATEX", "AUDIOCLIENT_ACTIVATION_PARAMS", "PROPVARIANT_BLOB",
    "activation_params", "activate_process_loopback", "to_mono", "wall_of", "Qpc", "process_table", "tree_root",
    "app_root_pid", "IAudioClient", "IAudioCaptureClient", "IAudioSessionEvents", "IAudioSessionNotification",
    "MIN_BUILD", "STREAM_FLAGS", "RATE",
]
