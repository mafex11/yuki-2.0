"""Screenshots of the Lattice window's pages from the file:// preview (invented data), in a headless browser.

Uses Playwright's ``chrome-headless-shell`` (its own binary, never the user's Chrome) with a throwaway profile in
%TEMP%, at 1440x900 by default. Each shot loads ``yuki/ui/web/preview.html`` with a page and knobs, lets virtual
time run so the title reveal and the field's sweep finish, and writes a PNG. JS errors the page logs are reported.
Output: ``%TEMP%\\yuki-web-shots\\<stamp>\\`` (outside the repo).

    python scripts/web_shots.py [--only home,work] [--size 1280x800] [--out DIR]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREVIEW = ROOT / "yuki" / "ui" / "web" / "preview.html"

#: name -> preview query (page and knobs)
SHOTS: dict[str, str] = {
    "home": "page=home",
    "home-empty": "page=home&empty=1&call=0",
    "home-thread": "page=home&thread=1",
    "home-state-working": "page=home&state=working",
    "home-reduced": "page=home&motion=reduced",
    "home-orange": "page=home&accent=orange&state=listening",
    "work": "page=work",
    "work-day": "page=work/day&scrub=1185",
    "me": "page=me",
    "me-person": "page=me/person:5",
    "playbook": "page=playbook",
    "activity": "page=activity",
    "activity-saw": "page=activity/saw",
    "activity-calls": "page=activity/calls",
    "activity-failed": "page=activity/failed",
    "settings": "page=settings",
    "home-scrolled": "page=home&scroll=620",
    "me-scrolled": "page=me&scroll=560",
    "settings-scrolled": "page=settings&scroll=600",
    "day-scrolled": "page=work/day&scroll=640&scrub=1185",
    "playbook-empty-memory": "page=playbook&empty=1",
    # nothing live (no run): the state the design's window shots are in, for side-by-sides (E11-E14)
    "home-idle": "page=home&runs=0", "work-idle": "page=work&runs=0", "me-idle": "page=me&runs=0",
    "playbook-idle": "page=playbook&runs=0", "activity-idle": "page=activity&runs=0",
}


def headless_shell() -> Path:
    env = os.environ.get("YUKI_SHOT_CHROME")
    if env:
        return Path(env)
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    found = sorted(base.glob("chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe"))
    found += sorted(base.glob("chromium_headless_shell-*/*/chrome-headless-shell.exe"))
    if not found:
        raise SystemExit("chrome-headless-shell not found (set YUKI_SHOT_CHROME)")
    return found[-1]


def shoot(exe: Path, name: str, query: str, out: Path, size: str, profile: Path) -> tuple[bool, list[str]]:
    url = PREVIEW.resolve().as_uri() + "?" + query
    png = out / f"{name}.png"
    cmd = [str(exe), "--headless", "--disable-gpu", "--hide-scrollbars", f"--window-size={size.replace('x', ',')}",
           f"--user-data-dir={profile}", "--no-first-run", "--allow-file-access-from-files", "--enable-logging=stderr", "--v=0",
           "--virtual-time-budget=5000", "--run-all-compositor-stages-before-draw", f"--screenshot={png}", url]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    errors = [ln.strip() for ln in (p.stderr or "").splitlines() if "CONSOLE" in ln and ("Error" in ln or "error" in ln or "Uncaught" in ln)]
    return png.exists(), errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--size", default="1440x900")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    exe = headless_shell()
    out = args.out or Path(tempfile.gettempdir()) / "yuki-web-shots" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="yuki-shot-profile-"))
    wanted = [s for s in args.only.split(",") if s] or list(SHOTS)
    bad = 0
    for name in wanted:
        ok, errors = shoot(exe, name if args.size == "1440x900" else f"{name}-{args.size}", SHOTS[name], out, args.size, profile)
        print(("ok   " if ok and not errors else "FAIL ") + name + ("" if not errors else "  " + " | ".join(errors[:3])))
        bad += (not ok) or bool(errors)
    print(out)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
