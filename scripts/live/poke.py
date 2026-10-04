"""ACTS ON THE DESKTOP. A deliberate "the user took over" for report 73's takeover steps (report 63), used only
while the user is away and Yuki is acting. There is no Yuki command that pretends to be the user's hand, so this
is the one raw input script in the runbook.

    poke.py mouse     move the cursor about 160 px right over 0.15 s and leave it there (a sustained move)
    poke.py key       tap Shift once (a harmless key; goes to whatever window is in front)

Each poke appends ``{"ts", "kind"}`` (times only) to %TEMP%\\yuki-live\\pokes.jsonl so takeover.py can tell a
deliberate pause from a spurious one. Moving the cursor back afterwards is not needed (the next step re-aims).
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from pathlib import Path

LOG = Path(os.environ.get("TEMP", ".")) / "yuki-live" / "pokes.jsonl"
VK_SHIFT = 0x10
KEYEVENTF_KEYUP = 0x0002


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def mouse(dx: int = 160, steps: int = 6, span_s: float = 0.15) -> None:
    user32 = ctypes.windll.user32
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    for i in range(1, steps + 1):
        user32.SetCursorPos(pt.x + dx * i // steps, pt.y)
        time.sleep(span_s / steps)


def key() -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_SHIFT, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)


def main() -> int:
    kind = (sys.argv[1:] or [""])[0]
    if kind not in ("mouse", "key"):
        print(__doc__)
        return 2
    at = time.time()
    (mouse if kind == "mouse" else key)()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": at, "kind": kind}) + "\n")
    print(f"poked {kind} at {time.strftime('%H:%M:%S', time.localtime(at))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
