"""appPT offscreen renders: the send countdown (counting, switching), the working pill "Working · step 2", the open
working / thinking bar with the brain's own live line, and the live run card, next to the Lattice mock's frames.

Everything renders **offscreen** (``QT_QPA_PLATFORM=offscreen``: no window is shown, nothing takes focus) with
**invented** data. The mock's frames come from the gitignored design folder's ``shots/`` (they may hold the user's
data, so they and everything written here stay in %TEMP%):

    python scripts/appPT_shots.py --out %TEMP%\\yuki-appPT

Writes ``<out>/<name>.png`` (ours) and ``<out>/sbs-<name>.png`` (the mock's crop | ours | a difference heat map) where
the mock has the same state.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import lattice_shots as LS  # noqa: E402

SEND = {"state": "counting", "who": "Sam", "where": "Discord DM", "app": "Discord",
        "text": "I'll hop on VC after this match", "seconds": 3.0, "who_alts": ["Maya", "Nico"],
        "where_alts": ["WhatsApp"], "typed": False, "from_memory": True}


def _crop(src: Path, box: str, dest: Path) -> Path | None:
    from PySide6.QtGui import QImage

    if not src.exists():
        return None
    x0, y0, x1, y1 = LS.BOXES[box]
    QImage(str(src)).copy(x0, y0, x1 - x0, y1 - y0).save(str(dest))
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path(os.environ.get("TEMP", ".")) / "yuki-appPT")
    ap.add_argument("--design", type=Path, default=Path("C:/Users/yukidev/yuki/docs/ui-mockups/revamp-2026-10/3-lattice"))
    ap.add_argument("--t", type=float, default=2.0)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    app = LS._app()
    from yuki.ui import lattice as L
    from yuki.ui.island import Island, IslandInputs, RunInfo

    L.REDUCED_OVERRIDE[0] = False
    shots = args.design / "shots"
    isl = Island(bindings={"voice": "ctrl+alt+y", "cancel": "alt+shift+x"})
    isl.screen_state = lambda: (5, "accepts")
    out: list[tuple[str, float | None]] = []

    def shoot(name: str, box: str = "island", mock: str = "") -> None:
        end = time.monotonic() + 0.8          # let the enter and the morph (300 ms) settle, as a person would see it
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)
        L.set_clock(lambda: args.t)
        img = LS.render_island(isl, args.t, box, None)
        img.save(str(args.out / f"{name}.png"))
        score = None
        if mock:
            crop = _crop(shots / mock, box, args.out / f"mock-{name}.png")
            if crop is not None:
                score = LS.side_by_side(crop, img, args.out / f"sbs-{name}.png")
        out.append((name, score))

    started = time.monotonic() - 12
    request = "message him this: I'll hop on VC after this match"
    # 1. the send countdown, open on its own (the mock's #desktop/resolve), then switching after a chip tap
    isl.strip.start_run(41, started=started, steps=3, line="Typing your message", request=request)
    isl.show_send(41, dict(SEND))
    shoot("send-countdown", mock="desktop-resolve.png")
    isl.show_send(41, {**SEND, "state": "swapped", "who": "Maya", "who_alts": ["Sam", "Nico"]})
    shoot("send-switching")
    isl.show_send(41, {**SEND, "state": "going"})
    # 2. working: the compact pill, then the open bar with the brain's own line
    isl.inputs = IslandInputs(run=RunInfo(41, started, steps=2, line="Opening Discord", request=request))
    isl.hovered = False
    isl.refresh(animate=False)
    shoot("working-pill", mock="desktop-working.png")
    isl.inputs = IslandInputs(run=RunInfo(41, started, steps=2, line="Checking first", request=request,
                                          thought="Checked Discord first: your last message to Sam was at 6:08 PM"))
    isl.hovered = True
    isl.refresh(animate=False)
    shoot("working-open-thought")
    isl.inputs = IslandInputs(run=RunInfo(41, started, steps=0, request=request,
                                          thought="Checking who you mean by him first"), heard="message him this")
    isl.refresh(animate=False)
    shoot("thinking-open-thought", mock="desktop-thinking.png")
    # 3. the live run card: the steps so far and the current one with its live line, no "of N"
    steps = [("Opening Discord", True, 0.9), ("Checking first", None, 0.0)]
    isl.attach_model(LS.FakeModel(41, steps))
    isl.inputs = IslandInputs(run=RunInfo(41, started, steps=2, line="Checking first", request=request,
                                          thought="Checked Discord first: your last message to Sam was at 6:08 PM"))
    isl.expanded = True
    isl.refresh(animate=False)
    shoot("run-live-thought", box="runcard", mock="desktop-run-run-running.png")
    isl.stop()
    for name, score in out:
        print(f"{name:28s} " + (f"mean |diff| {score:6.1f}" if score is not None else "(no mock frame)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
