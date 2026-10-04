"""Before the away part of the live check (report 73, step A0): is it safe to drive the desktop now? Read-only.

Prints: the user's idle time, the foreground app (exe name from a short list, else "other") and whether it is full
screen, whether a call or game looks active (the tray's last call status and the timeline's full-screen flag from
the memory service's newest stats record), and whether Yuki is idle. Never prints titles or text.

    .venv\\Scripts\\python.exe scripts\\live\\preflight.py

GO only when: idle >= 120 s, nothing full screen, no call, Yuki idle.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import sys
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import app_name, files, jsonl  # noqa: E402


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def idle_s() -> float:
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0


def foreground() -> tuple[str, bool]:
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        name = psutil.Process(pid.value).name()
    except psutil.Error:
        name = ""
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

    mon = user32.MonitorFromWindow(hwnd, 2)   # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO(ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    m = mi.rcMonitor
    # full screen covers the whole monitor, taskbar included; a maximized window stops at the work area
    full = rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom
    return app_name(name) or "-", bool(full)


def main() -> int:
    idle = idle_s()
    app, full = foreground()
    since = time.time() - 1800
    st = [r for r in jsonl(files("memory-*.jsonl", since), since, time.time() + 60) if r.get("type") == "stats"]
    tl = (st[-1].get("timeline") or {}) if st else {}
    calls = [r for r in jsonl(files("sessions/*[0-9].jsonl", since - 6 * 3600), since - 6 * 3600, time.time() + 60)
             if r.get("type") == "ui" and r.get("event") == "call_status"]
    last_call = calls[-1] if calls else {}
    in_call = bool(last_call.get("recording") or tl.get("meeting")
                   or (last_call.get("call_id") and last_call.get("status") not in (None, "ended")))
    games = [p.info["name"] for p in psutil.process_iter(["name"])
             if (p.info["name"] or "").lower() in ("valorant-win64-shipping.exe", "genshinimpact.exe", "cs2.exe")]
    print(f"idle_s={idle:.0f} foreground={app} fullscreen_hint={full} timeline_fullscreen={tl.get('fullscreen')} "
          f"meeting={tl.get('meeting')} call_status_seen={bool(last_call)} in_call={in_call} game_running={bool(games)}")
    try:
        from tray import brief_state, client   # the tray pipe, read-only op
        c = client()
        s = brief_state(c)
        c.close()
    except SystemExit as exc:
        s = {"error": str(exc)}
    print(f"yuki={json.dumps(s)}")
    # the service's own full-screen flag decides (the rect hint also fires for a maximized window when the taskbar
    # auto-hides); a full-screen hint with the user idle for minutes is likely a video left open: check by eye
    go = idle >= 120 and not tl.get("fullscreen") and not in_call and not games \
        and s.get("worker_busy") is False and not s.get("conversation")
    print("GO" if go else "NO-GO (wait, or ask the user)")
    return 0 if go else 1


if __name__ == "__main__":
    sys.exit(main())
