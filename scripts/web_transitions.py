"""Tab-switch frames of the Lattice window next to the Lattice mock's, at the same virtual-time steps.

For each transition (``home -> work`` and so on) it loads the page, switches tab at ``--at`` ms, and screenshots the
frames ``--steps`` ms after the switch (first, mid, last...), in Playwright's ``chrome-headless-shell`` with a
throwaway profile (never the user's Chrome). Ours: ``yuki/ui/web/preview.html`` (invented data, ``goto`` knob). The
mock: a wrapper written to %TEMP% that loads the gitignored Lattice folder by absolute path and sets its hash at the
same moment (``--mock DIR``; its data is real, so its frames stay in %TEMP% like everything here).

    python scripts/web_transitions.py --mock C:\\path\\to\\docs\\ui-mockups\\revamp-2026-10\\3-lattice
Output: ``%TEMP%\\yuki-web-transitions\\<stamp>\\<pair>-<ours|mock>-<ms>.png``
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from web_shots import PREVIEW, headless_shell  # noqa: E402

#: (name, from page, to page): our page names and the mock's routes
PAIRS = [("home-work", "home", "work", "window/home", "window/work"),
         ("work-day", "work", "work/day", "window/work", "window/work/day"),
         ("home-me", "home", "me", "window/home", "window/me"),
         ("me-playbook", "me", "playbook", "window/me", "window/playbook"),
         ("playbook-activity", "playbook", "activity", "window/playbook", "window/activity"),
         ("activity-settings", "activity", "settings", "window/activity", "window/settings")]


def mock_wrapper(mock: Path, out: Path, start: str, target: str, at: int) -> Path:
    """The mock's index.html with absolute asset paths and one line that switches tab at ``at`` ms."""
    page = out / f"mock-{start.replace('/', '_')}-{target.replace('/', '_')}.html"
    base = mock.resolve().as_uri()
    data = (mock.parent / "data" / "yuki-data.js").resolve().as_uri()
    # the switch is an inline script in a temp file outside the repo: the mock has no CSP
    page.write_text(f"""<!doctype html><html lang="en" data-motion="full"><head><meta charset="utf-8">
<title>mock</title><link rel="stylesheet" href="{base}/style.css"></head><body>
<canvas id="field" aria-hidden="true"></canvas><div id="app"></div>
<script>location.hash = '#{start}';</script>
<script src="{data}"></script><script src="{base}/content.js"></script><script src="{base}/field.js"></script>
<script src="{base}/app.js"></script>
<script>setTimeout(function () {{ location.hash = '#{target}'; }}, Math.max(0, {at} - performance.now()));</script>
</body></html>""", encoding="utf-8")
    return page


def shot(exe: Path, url: str, png: Path, budget: int, profile: Path, size: str) -> bool:
    cmd = [str(exe), "--headless", "--disable-gpu", "--hide-scrollbars", f"--window-size={size.replace('x', ',')}",
           f"--user-data-dir={profile}", "--no-first-run", "--allow-file-access-from-files",
           f"--virtual-time-budget={budget}", "--run-all-compositor-stages-before-draw", f"--screenshot={png}", url]
    subprocess.run(cmd, capture_output=True, timeout=120)
    return png.exists()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", type=Path, default=None)
    ap.add_argument("--at", type=int, default=2500, help="the tab switch, ms after load")
    ap.add_argument("--steps", default="0,90,200,350,600,1500", help="frames, ms after the switch")
    ap.add_argument("--only", default="")
    ap.add_argument("--size", default="1440x900")
    args = ap.parse_args()
    exe = headless_shell()
    out = Path(tempfile.gettempdir()) / "yuki-web-transitions" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="yuki-transition-profile-"))
    steps = [int(s) for s in args.steps.split(",") if s.strip()]
    for name, a, b, ma, mb in PAIRS:
        if args.only and name not in args.only.split(","):
            continue
        ours = PREVIEW.resolve().as_uri() + f"?page={a}&goto={b}&gotoAt={args.at}&runs=0&call=0"
        wrapper = mock_wrapper(args.mock, out, ma, mb, args.at) if args.mock else None
        for ms in steps:
            ok = shot(exe, ours, out / f"{name}-ours-{ms:04d}.png", args.at + ms, profile, args.size)
            line = f"{name} +{ms} ms ours {'ok' if ok else 'FAIL'}"
            if wrapper is not None:
                ok2 = shot(exe, wrapper.as_uri(), out / f"{name}-mock-{ms:04d}.png", args.at + ms, profile, args.size)
                line += f"  mock {'ok' if ok2 else 'FAIL'}"
            print(line, flush=True)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
