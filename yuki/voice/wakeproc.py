"""The wake word in its own process: the spotter never waits for the app's other threads (docs/VOICE.md §22, v3.12).

Live 2026-09-28 the name was heard 3.6-8.4 s after it was said (``name_end_before_hit_ms`` 3572-8431, v3.11 had
fixed the name's timing, not this): the spotter shared the app process's interpreter lock with the tray, the Yuki
window's painting, the thinker and the standby conversation, and fell seconds behind the microphone (measured
offline: the real listener on a real-time pretend mic hit 0.3-0.45 s after the name alone, 0.8-2.6 s with three busy
Python threads beside it, its queue backing up). The user said "Yuki" again, nothing happened, and when it finally
woke, seconds of old audio (the second "Yuki" included) were replayed.

Here the whole listener (:class:`~yuki.voice.wake.WakeListener`: the sound gate, the spotter, the second opinion)
runs in a child process with its own interpreter, at above-normal priority. The app only forwards the shared
microphone's 20 ms blocks (each with its monotonic time: Windows' ``time.monotonic`` is the system-wide performance
counter, the same in both processes) over the child's stdin, and reads its events from stdout.

Wire format (fixed, binary in / JSON lines out):

* in: ``b"A"`` + ``<d at><I n>`` + ``n`` bytes of 16 kHz int16 mono; ``b"P"`` pause, ``b"R"`` resume, ``b"Q"`` quit.
* out: one JSON object per line: ``{"ev": "ready"}``, ``{"ev": "heard", "at", "keyword", "name_end", "rescued"}``,
  ``{"ev": "log", "kind", "fields"}`` (the listener's own content-free log lines), ``{"ev": "stats", ...}``.

:class:`ProcessWakeListener` is a drop-in for :class:`~yuki.voice.wake.WakeListener` in the session (``start``,
``stop``, ``pause``, ``resume``, ``running``, ``ready``, ``name_end``, ``rescued`` and the counters). If the child
cannot start, the session falls back to the in-process listener. Nothing leaves the PC; audio is never written.
"""

from __future__ import annotations

import json
import queue
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Callable

_HEADER = struct.Struct("<dI")
#: Blocks waiting to go to the child at most (20 ms each: 10 s). Beyond, the oldest are dropped (it fell behind).
QUEUE_BLOCKS = 500
#: Blocks sent together (100 ms), as the listener decodes them.
BATCH_BLOCKS = 5
#: One microphone block: 20 ms of 16 kHz int16 mono.
BLOCK_BYTES = 16000 // 50 * 2


class _Log:
    """The child's log: each line goes to the parent as JSON (the parent writes the voice log)."""

    def __init__(self, out: Any, lock: threading.Lock) -> None:
        self._out, self._lock = out, lock

    def event(self, kind: str, **fields: Any) -> None:
        _send(self._out, self._lock, {"ev": "log", "kind": kind, "fields": fields})


def _send(out: Any, lock: threading.Lock, obj: dict[str, Any]) -> None:
    line = (json.dumps(obj, default=str) + "\n").encode("utf-8")
    with lock:
        try:
            out.write(line)
            out.flush()
        except (OSError, ValueError):
            pass


class _PipeMic:
    """What :class:`~yuki.voice.wake.WakeListener` needs of a microphone: subscribers, fed from the pipe."""

    def __init__(self) -> None:
        self._subs: dict[int, Callable[[bytes, float], None]] = {}

    def subscribe(self, fn: Callable[[bytes, float], None]) -> int:
        self._subs[len(self._subs) + 1] = fn
        return len(self._subs)

    def unsubscribe(self, token: int) -> None:
        self._subs.pop(token, None)

    def push(self, pcm: bytes, at: float) -> None:
        for fn in list(self._subs.values()):
            fn(pcm, at)


def _std_pipe(which: int, mode: str) -> Any:
    """The process's own standard handle as a binary file: a windowless process (pythonw, the frozen ``Yuki.exe``)
    has no ``sys.stdin``/``sys.stdout`` (or a stand-in to devnull), but the pipes the parent gave it are there."""
    import ctypes
    import msvcrt
    import os

    kernel32 = ctypes.windll.kernel32
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    handle = kernel32.GetStdHandle(which)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return (sys.stdin if "r" in mode else sys.stdout).buffer
    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY if "r" in mode else os.O_WRONLY)
    return os.fdopen(fd, mode, buffering=0)


def _read_exact(inp: Any, n: int) -> bytes:
    """``n`` bytes from an unbuffered pipe (a raw read may return fewer)."""
    parts, got = [], 0
    while got < n:
        chunk = inp.read(n - got)
        if not chunk:
            break
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)


def child_main(argv: list[str] | None = None) -> int:
    """The child process: a :class:`WakeListener` on the parent's audio, events to stdout."""
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.2)
    ap.add_argument("--boost", type=float, default=2.0)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    try:
        import ctypes

        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x8000)  # ABOVE_NORMAL
    except Exception:  # noqa: BLE001 - only a head start
        pass
    from yuki.voice.wake import NameCheck, WakeListener, WakeSpotter

    inp, out = _std_pipe(-10, "rb"), _std_pipe(-11, "wb")
    lock = threading.Lock()
    log = _Log(out, lock)
    mic = _PipeMic()
    box: dict[str, Any] = {}

    def on_wake(at: float, keyword: str) -> None:
        listener = box["listener"]
        _send(out, lock, {"ev": "heard", "at": at, "keyword": keyword, "name_end": listener.name_end,
                          "rescued": bool(listener.rescued), "sent": time.monotonic()})

    listener = WakeListener(mic, lambda: WakeSpotter(threshold=args.threshold, boost=args.boost), on_wake=on_wake,
                            log=log, check=NameCheck() if args.check else None)
    box["listener"] = listener
    listener.start()

    def announce_ready() -> None:
        listener.ready.wait(60)
        _send(out, lock, {"ev": "ready"})

    threading.Thread(target=announce_ready, daemon=True).start()
    try:
        while True:
            kind = _read_exact(inp, 1)
            if not kind or kind == b"Q":
                break
            if kind == b"P":
                listener.pause()
            elif kind == b"R":
                listener.resume()
            elif kind == b"A":
                head = _read_exact(inp, _HEADER.size)
                if len(head) < _HEADER.size:
                    break
                at, n = _HEADER.unpack(head)
                pcm = _read_exact(inp, n)
                if len(pcm) < n:
                    break
                # Back to the microphone's own 20 ms blocks (the listener batches and gates on those).
                step = BLOCK_BYTES
                for k in range(0, len(pcm), step):
                    mic.push(pcm[k:k + step], at + k / 2 / 16000)
    finally:
        listener.stop()
        _send(out, lock, {"ev": "stats", "decode_s": listener.decode_s, "audio_s": listener.audio_s,
                          "heard_s": listener.heard_s, "near_misses": listener.near_misses,
                          "errors": listener.errors, "rescues": listener.rescues})
    return 0


class ProcessWakeListener:
    """:class:`~yuki.voice.wake.WakeListener`'s interface, with the listener itself in a child process (module doc).

    Args:
        mic: The shared microphone (its 20 ms blocks are forwarded).
        on_wake: ``(at, keyword)`` on a hit, called on this object's reader thread (as the in-process one does).
        log: The voice log; the child's own lines are written to it.
        threshold / boost: The spotter's settings. check: Load the second opinion (the phoneme model) in the child.
        command: The child's argv (default: this installation's ``-m yuki.voice.wakeproc``).
    """

    def __init__(self, mic: Any, on_wake: Callable[[float, str], None], log: Any = None, *, threshold: float = 0.2,
                 boost: float = 2.0, check: bool = True, command: list[str] | None = None) -> None:
        self.mic = mic
        self._on_wake = on_wake
        self._log = log
        self._args = ["--threshold", str(threshold), "--boost", str(boost), *(["--check"] if check else [])]
        self._command = command
        self._proc: subprocess.Popen | None = None
        self._token: int | None = None
        self._q: queue.Queue[bytes | None] = queue.Queue()
        self._pending = 0
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self.ready = threading.Event()
        self.name_end: float | None = None
        self.rescued = False
        self.rescues = 0
        self.decode_s = self.audio_s = self.heard_s = 0.0
        self.near_misses = 0
        self.errors = 0
        self.dropped_blocks = 0
        self._paused = False
        self._stopped = threading.Event()
        self._batch: list[bytes] = []
        self._batch_at = 0.0

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None and not self._stopped.is_set()

    def _argv(self) -> list[str]:
        if self._command is not None:
            return list(self._command) + self._args
        from yuki.core.procs import module_command

        return module_command("yuki.voice.wakeproc", *self._args)

    def start(self) -> None:
        if self.running:
            return
        self._stopped.clear()
        self.ready.clear()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(self._argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, creationflags=flags, bufsize=1 << 16)
        # Buffered both ways: an unbuffered pipe's readline reads byte by byte, each read a wait for the interpreter
        # lock in a busy app (measured: a hit line took ~6 s to be read with three busy threads beside it).
        for target, name in ((self._write_loop, "yuki-voice-wake-send"), (self._read_loop, "yuki-voice-wake-read")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        self._token = self.mic.subscribe(self._on_block)

    def _on_block(self, pcm: bytes, at: float) -> None:
        """On the mic's callback thread: collect 100 ms, then queue it as one message (never a blocking write here).

        Few crossings of the interpreter lock per second on this side too (a busy app makes each one wait)."""
        if self._paused:
            self._batch.clear()
            return
        if not self._batch:
            self._batch_at = at
        self._batch.append(pcm)
        if len(self._batch) < BATCH_BLOCKS:
            return
        chunk, self._batch = b"".join(self._batch), []
        with self._lock:
            if self._pending >= QUEUE_BLOCKS // BATCH_BLOCKS:
                try:
                    self._q.get_nowait()
                    self._pending -= 1
                    self.dropped_blocks += BATCH_BLOCKS
                except queue.Empty:
                    pass
            self._pending += 1
        self._q.put(b"A" + _HEADER.pack(float(self._batch_at), len(chunk)) + chunk)

    def _write_loop(self) -> None:
        proc = self._proc
        while proc is not None:
            item = self._q.get()
            if item is None:
                break
            if item[:1] == b"A":
                with self._lock:
                    self._pending = max(0, self._pending - 1)
            try:
                proc.stdin.write(item)
                if self._q.empty():
                    proc.stdin.flush()
            except (OSError, ValueError, AttributeError):
                break
        try:
            if proc is not None and proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for raw in proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            ev = msg.get("ev")
            if ev == "ready":
                self.ready.set()
            elif ev == "heard":
                self.name_end = msg.get("name_end")
                self.rescued = bool(msg.get("rescued"))
                self.rescues += int(self.rescued)
                try:
                    self._on_wake(float(msg.get("at") or 0.0), str(msg.get("keyword") or ""))
                except Exception as exc:  # noqa: BLE001 - one bad wake must not end listening
                    if self._log is not None:
                        self._log.event("wake", state="error", error=f"{type(exc).__name__}: {exc}")
            elif ev == "log" and self._log is not None:
                fields = msg.get("fields") if isinstance(msg.get("fields"), dict) else {}
                self._log.event(str(msg.get("kind") or "wake"), **fields, process=True)
            elif ev == "stats":
                for key in ("decode_s", "audio_s", "heard_s"):
                    setattr(self, key, float(msg.get(key) or 0.0))
                self.near_misses = int(msg.get("near_misses") or 0)
                self.errors = int(msg.get("errors") or 0)
        self._stopped.set()

    def _control(self, code: bytes) -> None:
        self._q.put(code)

    def pause(self) -> None:
        self._paused = True
        self._control(b"P")

    def resume(self) -> None:
        self._paused = False
        self._control(b"R")

    def stop(self) -> None:
        if self._token is not None:
            self.mic.unsubscribe(self._token)
            self._token = None
        proc, self._proc = self._proc, None
        if proc is None:
            return
        self._q.put(b"Q")
        self._q.put(None)
        for thread in self._threads:
            thread.join(timeout=3.0)
        self._threads = []
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        self._stopped.set()


if __name__ == "__main__":
    sys.exit(child_main())
