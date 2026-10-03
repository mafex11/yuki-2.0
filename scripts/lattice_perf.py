"""Cost of the native Lattice surfaces, offscreen (no window on screen): memory the island and the cards add to a
tray-like process, CPU and frames while the island is live, and idle.

    python scripts/lattice_perf.py

Prints the process's private memory before and after building the island, the cards and the ask bar; then, for each
case, the share of ONE core the process used, the frames the loop ran per second, the average time inside a frame and
the repaints per second (the face and the halo each count). Offscreen Qt (software raster, no compositor): on screen,
Windows also has to push each repaint of the layered windows, which this does not measure.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t), ("PrivateUsage", ctypes.c_size_t)]


def mem_mb() -> tuple[float, float]:
    """(private, working set) of this process in MB."""
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    fn = ctypes.windll.kernel32.K32GetProcessMemoryInfo
    fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    fn(wintypes.HANDLE(-1), ctypes.byref(pmc), pmc.cb)
    return pmc.PrivateUsage / 2**20, pmc.WorkingSetSize / 2**20


def cpu_s() -> float:
    k, u, c, e = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    fn = ctypes.windll.kernel32.GetProcessTimes
    fn.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    fn(wintypes.HANDLE(-1), ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u))
    f = lambda t: (t.dwHighDateTime << 32 | t.dwLowDateTime) / 1e7  # noqa: E731
    return f(k) + f(u)


def run_for(app, seconds: float) -> None:
    """The real event loop for ``seconds`` (event-driven, as the tray runs: nothing polls)."""
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()


def measure(app, name: str, seconds: float = 10.0) -> None:
    from yuki.ui import lattice as L

    loop = L.Loop.shared()
    run_for(app, 1.0)                     # settle into the state
    c0, w0 = cpu_s(), time.monotonic()
    t0, ms0, d0 = loop.ticks, loop.ms, loop.draws
    loop.max_ms = 0.0
    run_for(app, seconds)
    wall = time.monotonic() - w0
    cpu = (cpu_s() - c0) / wall * 100
    ticks = loop.ticks - t0
    print(f"{name:34s} CPU {cpu:5.1f}% of one core | loop {ticks / wall:5.1f} fps | "
          f"{(loop.ms - ms0) / max(1, ticks):5.2f} ms/frame (worst {loop.max_ms:4.1f}) | "
          f"{(loop.draws - d0) / wall:5.1f} repaints/s")


def main() -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    from yuki.ui.window.theme import app_font

    app.setFont(app_font())
    import yuki.ui.app  # noqa: F401  - what the tray imports anyway

    run_for(app, 0.5)
    p0, ws0 = mem_mb()
    from datetime import datetime, timedelta

    from yuki.ui import lattice as L
    from yuki.ui.chat_model import ChatModel
    from yuki.ui.island import Island, IslandInputs, RunInfo
    from yuki.ui.lattice_cards import ReturnCard
    from yuki.ui.nudges import NudgeCard
    from yuki.ui.overlay import Overlay

    L.REDUCED_OVERRIDE[0] = False
    isl = Island(bindings={"voice": "ctrl+alt+y", "cancel": "alt+shift+x"})
    isl.screen_state = lambda: (5, "accepts")
    card, ret, ov = NudgeCard(), ReturnCard(), Overlay()
    ov.attach_thread(ChatModel())
    for w in (card, ret, ov):
        w.warm()
    call = {"status": "recording", "app": "Discord",
            "session_started_at": (datetime.now().astimezone() - timedelta(minutes=40)).isoformat()}
    isl.update(voice="listening", call=call)
    run_for(app, 2.0)
    p1, ws1 = mem_mb()
    print(f"memory: private {p0:.1f} -> {p1:.1f} MB (+{p1 - p0:.1f}), working set {ws0:.1f} -> {ws1:.1f} MB "
          f"(+{ws1 - ws0:.1f}) after the island (live), a card, the return card and the ask bar")
    measure(app, "listening (face + halo)")
    isl.update(voice="speaking", spoken="The exports now sort by date.")
    measure(app, "speaking (face + halo)")
    isl.update(voice="off", spoken="", run=RunInfo(1, time.monotonic(), steps=2, line="Renaming the files"))
    measure(app, "working (face + halo + clock)")
    isl.update(run=None)
    measure(app, "quiet in a call (timer + rec dot)")
    isl.update(call=None)
    measure(app, "idle (the mini island)")
    isl.screen_state = lambda: (3, "d3d_full_screen")
    isl.on_foreground(0)
    measure(app, "game-quiet (island hidden)")
    L.REDUCED_OVERRIDE[0] = True
    isl.screen_state = lambda: (5, "accepts")
    isl.update(voice="listening", call=call)
    isl.on_foreground(0)
    measure(app, "listening, reduced motion")
    isl.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
