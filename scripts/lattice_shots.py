"""Side-by-side renders of the native Lattice surfaces (the island, the run card, the corner cards, the ask bar, the
tray's hover card) next to the Lattice mock's frames.

Everything renders **offscreen** (``QT_QPA_PLATFORM=offscreen``; no window is shown, nothing takes focus) with
**invented** data, at the mock's crop sizes, composited at the same place the mock draws it. The mock's frames come
from a separate capture (headless Chromium over the gitignored design folder; they hold the user's data, so they and
everything written here stay in %TEMP%):

    python scripts/lattice_shots.py --mock %TEMP%\\yuki-2c\\mock --out %TEMP%\\yuki-2c\\qt

Writes ``<out>/<name>.png`` (ours) and ``<out>/sbs-<name>.png`` (the mock's crop | ours | a difference heat map).
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

#: The mock's crop boxes (x0, y0, x1, y1 in its 1440 x 900 frames), as the capture wrote them.
BOXES = {"island": (340, 0, 1100, 300), "runcard": (400, 0, 1040, 720), "card": (980, 420, 1440, 852),
         "askbar": (320, 50, 1120, 800), "consent": (980, 0, 1440, 432)}
VIEW_W, VIEW_H = 1440, 900


def _app():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from yuki.ui.window.theme import app_font

    app.setFont(app_font())
    return app


def _grab(widget):
    """The widget's own pixels (with alpha), as the compositor would get them."""
    from PySide6.QtGui import QImage, QPainter

    img = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    widget.render(img)
    return img


def composite(backdrop: Path | None, box: str, layers: list[tuple]) -> "QImage":
    """``layers``: ``(image, x, y, opacity)`` in viewport coordinates; returns the crop for ``box``."""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor, QImage, QPainter

    x0, y0, x1, y1 = BOXES[box]
    out = QImage(x1 - x0, y1 - y0, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(QColor("#0D0E0F"))
    p = QPainter(out)
    if backdrop is not None and backdrop.exists():
        bg = QImage(str(backdrop))
        p.drawImage(0, 0, bg, x0, y0, x1 - x0, y1 - y0)
    for img, x, y, op in layers:
        p.setOpacity(op)
        p.drawImage(int(x - x0), int(y - y0), img)
    p.setOpacity(1.0)
    p.end()
    return out


def side_by_side(mock: Path, ours: "QImage", dest: Path) -> float:
    """mock | ours | heat map of the difference; returns the mean absolute difference (0-255)."""
    from PySide6.QtGui import QColor, QImage, QPainter

    m = QImage(str(mock)).convertToFormat(QImage.Format.Format_RGB32) if mock.exists() else None
    o = ours.convertToFormat(QImage.Format.Format_RGB32)
    w, h = o.width(), o.height()
    out = QImage(w * 3 + 16, h, QImage.Format.Format_RGB32)
    out.fill(QColor("#202020"))
    p = QPainter(out)
    if m is not None:
        p.drawImage(0, 0, m)
    p.drawImage(w + 8, 0, o)
    total = 0.0
    if m is not None and m.width() >= w and m.height() >= h:
        diff = QImage(w, h, QImage.Format.Format_RGB32)
        step = 1
        n = 0
        for y in range(0, h, step):
            for x in range(0, w, step):
                a, b = m.pixel(x, y), o.pixel(x, y)
                d = (abs(((a >> 16) & 255) - ((b >> 16) & 255)) + abs(((a >> 8) & 255) - ((b >> 8) & 255))
                     + abs((a & 255) - (b & 255))) // 3
                total += d
                n += 1
                v = min(255, d * 3)
                diff.setPixel(x, y, (0xFF << 24) | (v << 16) | (v // 3 << 8) | (v // 3))
        total /= max(1, n)
        p.drawImage(2 * w + 16, 0, diff)
    p.end()
    out.save(str(dest))
    return total


# -- invented content ---------------------------------------------------------------------------------------

def _call(minutes: float) -> dict:
    from datetime import datetime, timedelta

    start = datetime.now().astimezone() - timedelta(minutes=minutes)
    return {"status": "recording", "app": "Discord", "session_started_at": start.isoformat(timespec="seconds")}


class FakeModel:
    """A tiny chat model stand-in: one response with steps (invented)."""

    def __init__(self, job: int, steps: list[tuple[str, bool | None, float]]):
        from yuki.ui.chat_model import Item, Step

        now = time.time()
        self.item = Item("a1", "assistant", now - 40, status="running", jobs=[job])
        t = now - 38
        for k, (label, ok, secs) in enumerate(steps):
            st = Step(f"s{k}", "tool", "tool", label, t, ended=None if ok is None else t + secs, ok=ok, job=job)
            self.item.steps.append(st)
            t += secs + 1.0

    def subscribe(self, fn):
        return lambda: None

    def item_for_job(self, job):
        return self.item


def island_state(island, state: str, t: float, animate: bool = False):
    """Put the island in ``state`` with invented data, as of animation time ``t``."""
    from yuki.ui import lattice as L
    from yuki.ui.island import FinalInfo, IslandInputs, RunInfo, Steer

    L.set_clock(lambda: t - 1.9)        # the state began ~1.9 s before the frame (the mock's frames: at 2.0 s)
    call = _call(46.07)
    base = IslandInputs(listening="on", call=call)
    run = RunInfo(41, time.monotonic() - 46, steps=2, line="Renaming the export so the files sort by date.",
                  request="Rename the garden planner's exports so they sort by date, oldest first, and keep the names short.")
    inputs = {
        "hidden-armed": IslandInputs(listening="on"),
        "listening": IslandInputs(voice="listening", call=call,
                                  caption=[("rename", 0.98), ("the", 0.95), ("exports", 0.41)]),
        "hearing": IslandInputs(voice="hearing", call=call,
                                caption=[("rename", 0.98), ("the", 0.95), ("exports", 0.41), ("so", 0.9),
                                         ("they", 0.93), ("sort", 0.88)]),
        "thinking": IslandInputs(voice="thinking", call=call, transient="Working out which export you mean",
                                 heard="rename the exports"),
        "working": IslandInputs(call=call, run=run),
        "speaking": IslandInputs(voice="speaking", call=call, spoken="The exports now sort by date, oldest first."),
        "needs-you": IslandInputs(call=call, question="Which export, the planner's or the photos'?", question_id=41,
                                  question_voice=True),
        "quiet-in-call": base,
        "voice-stopped": IslandInputs(voice_error="The microphone was disconnected. I can use the laptop's one."),
        "done-flash": IslandInputs(call=call, final=FinalInfo("Renamed 12 exports.", "done", "3 steps · 9 s · 4¢")),
    }[state]
    island.expanded = False
    island.inputs = inputs
    island.refresh(animate=animate)
    return island


# -- transitions: the same moments as the mock's frames (+0, +60, +150, +300, +600, +1300 ms after the switch) ----

SWITCH_T = 1.5
TRANSITIONS = [("quiet-in-call", "listening"), ("hidden-armed", "listening"), ("listening", "thinking"),
               ("thinking", "working"), ("working", "run"), ("working", "done-flash"),
               ("quiet-in-call", "game-quiet"), ("listening", "needs-you")]
OFFSETS = (0, 60, 150, 300, 600, 1300)


def _paused(anims):
    for a in anims:
        if a.state() == a.State.Running:
            a.pause()


def _step(island, a: float, b: float, anims: list, t0: float) -> None:
    """Frames from ``a`` to ``b`` on the design's 33 ms loop, the paused animations moved to the same moment."""
    from yuki.ui import lattice as L

    w = island.window
    t = a
    while True:
        L.set_clock(lambda tt=t: tt)
        if w.face.isVisible():
            w.face.frame(t)
        if w.halo_win.isVisible():
            w.halo_win.halo.frame(t)
        for anim in anims:
            if anim.state() != anim.State.Stopped:
                anim.setCurrentTime(int(round(max(0.0, t - t0) * 1000)))
        if t >= b - 1e-6:
            break
        t = min(b, t + L.FRAME_MS / 1000.0)


def island_transition(app, src: str, dst: str, ms: int, backdrop: Path | None):
    from yuki.ui import lattice as L
    from yuki.ui.island import Island

    isl = Island(bindings={"voice": "ctrl+alt+y", "cancel": "alt+shift+x"})
    isl.screen_state = lambda: (5, "accepts")
    island_state(isl, src, SWITCH_T)
    app.processEvents()
    w = isl.window
    anims = [w._morph, w._move, w.halo_win._fade]
    for a in anims:
        a.stop()
    _step(isl, SWITCH_T - 1.9, SWITCH_T, [], SWITCH_T)
    L.set_clock(lambda: SWITCH_T)
    if dst == "run":
        isl.toggle_card()
    elif dst == "game-quiet":
        isl.screen_state = lambda: (3, "d3d_full_screen")
        isl.on_foreground(0)
    else:
        island_state(isl, dst, SWITCH_T + 1.9, animate=True)
        L.set_clock(lambda: SWITCH_T)
    _paused(anims)
    _step(isl, SWITCH_T, SWITCH_T + ms / 1000.0, anims, SWITCH_T)
    layers = []
    if w.isVisible():
        dy = w.pos().y() - w._origin.y() if w._move.state() != w._move.State.Stopped else 0
        op = w.windowOpacity()
        layers.append((_grab(w), VIEW_W // 2 - w.width() // 2, 12 + dy, op))
        hw = w.halo_win
        if hw.isVisible():
            layers.append((_grab(hw), VIEW_W // 2 - hw.width() // 2, 12 + dy + w._rect.height() + 2,
                           hw.windowOpacity() * (op if op < 1 else 1)))
    img = composite(backdrop, "runcard" if dst == "run" else "island", layers)
    isl.stop()
    w.close()
    w.halo_win.close()
    return img


def card_transition(app, which: str, ms: int, backdrop: Path | None):
    """A corner card coming in (the design's ``cardin``: 520 ms, a 10 px rise), at ``ms`` after it was shown."""
    from yuki.ui.nudges import NudgeCard

    c = NudgeCard(linger_ms=60_000)
    if which == "offer-todo":
        todo = {"do": "todo_add", "label": "Add to list", "steps": ["Add “Sort the planner exports” to your list, due Mon 5 Oct."],
                "wont": "Won’t open, type into or send anything.", "undo": True, "expires_at": _soon()}
        n = {"id": 7, "kind": "notice", "trigger": "thread", "at": _ago(3), "offer": todo,
             "text": "Next on the garden planner is the export cleanup: 34 old files into one folder, sorted by date, "
                     "so the backup stops failing. Due Monday."}
    else:
        n = {"id": 9, "kind": "question", "at": _ago(1), "ref": "question:4",
             "text": "Is the garden planner a side project or work? You named it in both your chats and your mail."}
    from PySide6.QtCore import QPoint, Qt

    c.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    c.set_nudge(n)
    target = QPoint(VIEW_W - 20 - c.width(), VIEW_H - 64 - c.height())
    c.fade_in(target)
    c._anim.pause()
    c._anim.setCurrentTime(ms)
    dy = c.pos().y() - target.y()
    img = composite(backdrop, "card", [(_grab(c), target.x(), target.y() + dy, c.windowOpacity())])
    c.close()
    return img


def run_card(island, mode: str, t: float):
    from yuki.ui import lattice as L
    from yuki.ui.island import FinalInfo, RunInfo, Steer

    L.set_clock(lambda: t - 1.9)
    run = RunInfo(41, time.monotonic() - 46, steps=2, line="Renaming the export so the files sort by date.",
                  request="Rename the garden planner's exports so they sort by date, oldest first, and keep the "
                          "names short.")
    run.steers = [Steer("no, only the ones from this week", "read at step 1"),
                  Steer("also skip the drafts folder", "added, not yet read")]
    run.chips = {2: [("lesson", "using your lesson: rename in the app, never in Explorer")],
                 1: [("memory", "from memory: the exports live in Documents/Planner, 34 files")]}
    steps = [("Opening the folder — Documents/Planner", True, 0.86), ("Renaming the files — 12 exports", None, 0.0)]
    island.attach_model(FakeModel(41, steps))
    from yuki.ui.island import IslandInputs

    if mode == "running":
        island.inputs = IslandInputs(call=_call(46.07), run=run)
    else:
        outcome = {"stopped": "stopped", "handed": "handed", "done": "done"}[mode]
        text = {"stopped": "Stopped. Nothing else ran.", "handed": "Handed back. The planner is yours.",
                "done": "Renamed 12 exports."}[mode]
        island.inputs = IslandInputs(call=_call(46.07), final=FinalInfo(text, outcome, "", run))
    island.expanded = True
    island.refresh(animate=False)


def render_island(island, t: float, box: str = "island", backdrop: Path | None = None):
    from yuki.ui import lattice as L

    L.set_clock(lambda: t)
    w = island.window
    w.face.render_still(t)
    hw = w.halo_win
    if hw.isVisible():
        hw.halo.render_still(t)
    layers = []
    origin_x = VIEW_W // 2 - w.width() // 2
    img = _grab(w)
    layers.append((img, origin_x, 12, 0.92 if (w.spec and w.spec.mini) else 1.0))
    if hw.isVisible():
        layers.append((_grab(hw), VIEW_W // 2 - hw.width() // 2, 12 + w._rect.height() + 2, 1.0))
    return composite(backdrop, box, layers)


def _soon(minutes: float = 30) -> str:
    from datetime import datetime, timedelta

    return (datetime.now().astimezone() + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _ago(minutes: float) -> str:
    from datetime import datetime, timedelta

    return (datetime.now().astimezone() - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def cards() -> list[tuple]:
    """The corner cards with invented content: ``(name, card, crop box, corner)``."""
    from yuki.ui.calls import CallConsentCard
    from yuki.ui.lattice_cards import ReturnCard
    from yuki.ui.nudges import NudgeCard

    out = []
    todo = {"do": "todo_add", "label": "Add to list", "steps": ["Add “Sort the planner exports” to your list, due Mon 5 Oct."],
            "wont": "Won’t open, type into or send anything.", "undo": True, "expires_at": _soon()}
    c = NudgeCard(linger_ms=60_000)
    c.set_nudge({"id": 7, "kind": "notice", "trigger": "thread", "at": _ago(3),
                 "text": "Next on the garden planner is the export cleanup: 34 old files into one folder, sorted by "
                         "date, so the backup stops failing. Due Monday.", "offer": todo})
    out.append(("card-offer-todo", c, "card", "bottom"))
    ask = {"do": "ask", "label": "Edit in ask bar", "steps": ["Put “open the planner and restore the tabs” in the ask bar, unsent."],
           "wont": "Won’t send anything until you press Enter.", "expires_at": _soon()}
    c = NudgeCard(linger_ms=60_000)
    c.set_nudge({"id": 8, "kind": "notice", "trigger": "stuck", "at": _ago(2),
                 "text": "The planner closed with five tabs open on the export docs. You were halfway through them.",
                 "offer": ask})
    out.append(("card-offer-askbar", c, "card", "bottom"))
    c = NudgeCard(linger_ms=60_000)
    c.set_nudge({"id": 9, "kind": "question", "at": _ago(1), "ref": "question:4",
                 "text": "Is the garden planner a side project or work? You named it in both your chats and your mail."})
    out.append(("card-question", c, "card", "bottom"))
    c = NudgeCard(linger_ms=60_000)
    c.set_nudge({"id": 10, "kind": "nudge", "at": _ago(1),
                 "text": "Two hours at the planner without a break. Stand up for five minutes?"})
    out.append(("card-nudge", c, "card", "bottom"))
    r = ReturnCard()
    r.show_run(title="Renamed 12 exports in the planner.", at="21:31",
               changed=["Renamed 12 files in Documents/Planner to start with their date"],
               needs="Nothing. The backup runs tonight.",
               learned={"id": "pb:3", "text": "Next time I’ll rename in the planner itself, not in Explorer.",
                        "why": "Renaming in Explorer broke the planner’s links twice before it worked."},
               cost="1 min 9 s · 8 steps · 54¢", linger_ms=60_000)
    r.hide()
    out.append(("card-return", r, "card", "bottom"))
    k = CallConsentCard()
    k.set_call({"id": 3, "app": "Discord", "detected_at": _ago(0.5), "headphones": True, "can_remember_choice": True})
    out.append(("card-consent", k, "consent", "top"))
    return out


def askbar_shots(t: float) -> list[tuple]:
    """The ask bar with an invented thread: the resolved Intent card, and words an offer left unsent."""
    from yuki.ui import lattice as L
    from yuki.ui.chat_model import ChatModel
    from yuki.ui.overlay import Overlay

    L.set_clock(lambda: t)
    model = ChatModel()
    now = time.time()
    model.apply({"type": "user", "job": 1, "text": "What did I do today?", "at": now - 1500})
    model.apply({"type": "started", "job": 1, "lane": "front_desk", "at": now - 1499})
    for k, label in enumerate(("Reading your day", "Checking your calls", "Reading the planner")):
        model.apply({"type": "tool_called", "job": 1, "name": f"t{k}", "label": label, "at": now - 1498 + k})
        model.apply({"type": "tool_finished", "job": 1, "name": f"t{k}", "ok": True, "summary": "", "at": now - 1497 + k})
    model.apply({"type": "finished", "job": 1, "text": "A planner day: about two hours on the export cleanup, a long "
                 "call with Riya, and some time in the photo library. You're still on the call, 18 minutes in.",
                 "at": now - 1490})
    out = []
    ov = Overlay()
    ov.attach_thread(model)
    ov.call_line = lambda: ("46:04", "Discord")
    ov.input.setPlainText("message her this: I’ll send the export tonight")
    ov.show_resolved("Message Riya on WhatsApp", {"persons": [{"name": "Riya", "routes": [
        {"label": "WhatsApp chat", "app": "WhatsApp", "mine": 64}]}], "where": "the chat in front 4 min ago"})
    out.append(("askbar", ov))
    ov2 = Overlay()
    ov2.attach_thread(model)
    ov2.call_line = lambda: ("46:04", "Discord")
    ov2.input.setPlainText("open the planner and restore the export tabs")
    ov2.show_unsent("from an offer · 21:31")
    out.append(("askbar-unsent", ov2))
    return out


def askbar_shot(ov, t: float, backdrop: Path | None):
    from PySide6.QtCore import Qt

    ov.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    ov.show()
    ov.fit()
    ov.face.render_still(t)
    img = _grab(ov)
    return composite(backdrop, "askbar", [(img, (VIEW_W - ov.width()) // 2, 64, 1.0)])


def card_shot(card, box: str, corner: str, backdrop: Path | None):
    """The card placed as the design places it: 20 px from the right; 64 px from the bottom (or from the top)."""
    card.setAttribute(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    card.show()
    card.fit_height()
    img = _grab(card)
    x = VIEW_W - 20 - card.width()
    y = 64 if corner == "top" else VIEW_H - 64 - card.height()
    return composite(backdrop, box, [(img, x, y, 1.0)])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", type=Path, default=Path(os.environ.get("TEMP", ".")) / "yuki-2c" / "mock")
    ap.add_argument("--out", type=Path, default=Path(os.environ.get("TEMP", ".")) / "yuki-2c" / "qt")
    ap.add_argument("--only", default="")
    ap.add_argument("--t", type=float, default=2.0)
    ap.add_argument("--transitions", action="store_true", help="the state changes, frame by frame, next to the mock's")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    app = _app()
    from yuki.ui import lattice as L
    from yuki.ui.island import Island

    L.REDUCED_OVERRIDE[0] = False
    crops = args.mock / "crops"
    backdrop = args.mock / "backdrop-desk.png"
    if args.transitions:
        results = []
        for src, dst in TRANSITIONS:
            if args.only and args.only not in f"{src}-{dst}":
                continue
            for ms_ in OFFSETS:
                img = island_transition(app, src, dst, ms_, backdrop)
                name = f"tr-{src}-{dst}-{ms_}"
                img.save(str(args.out / f"{name}.png"))
                suffix = "-island" if dst == "run" else ""
                mock_dst = "run" if dst == "run" else dst
                cand = crops / f"tr-{src}-{mock_dst}-{ms_}.png"
                score = side_by_side(cand, img, args.out / f"sbs-{name}.png")
                results.append((name, score))
                del suffix
        for which in ("offer-todo", "question"):
            if args.only and args.only not in which:
                continue
            for ms_ in OFFSETS:
                img = card_transition(app, which, ms_, backdrop)
                name = f"tr-hidden-armed-{which}-{ms_}"
                img.save(str(args.out / f"{name}.png"))
                score = side_by_side(crops / f"{name}.png", img, args.out / f"sbs-{name}.png")
                results.append((name, score))
        for name, score in results:
            print(f"{name:44s} mean |diff| {score:6.1f}")
        return 0
    island = Island(bindings={"voice": "ctrl+alt+y", "cancel": "alt+shift+x"})
    island.screen_state = lambda: (5, "accepts")
    results = []
    ms = int(round(args.t * 1000))
    for state in ("hidden-armed", "listening", "hearing", "thinking", "working", "speaking", "needs-you",
                  "quiet-in-call", "voice-stopped", "done-flash"):
        if args.only and args.only not in state:
            continue
        island_state(island, state, args.t)
        app.processEvents()
        img = render_island(island, args.t, "island", backdrop)
        name = f"island-{state}-{ms}"
        img.save(str(args.out / f"{name}.png"))
        score = side_by_side(crops / f"static-{state}-{ms}.png", img, args.out / f"sbs-{name}.png")
        results.append((name, score))
    for mode, mock in (("running", "run_run=running"), ("stopped", "run_run=stopped"), ("handed", "run_run=handed"),
                       ("done", "run_run=done")):
        if args.only and args.only not in "run-" + mode:
            continue
        run_card(island, mode, args.t)
        app.processEvents()
        img = render_island(island, args.t, "runcard", backdrop)
        name = f"run-{mode}-{ms}"
        img.save(str(args.out / f"{name}.png"))
        cand = list(crops.glob(f"static-run*{mode}*-{ms}.png")) + list(crops.glob(f"static-{mock}-{ms}.png"))
        score = side_by_side(cand[0] if cand else crops / "none.png", img, args.out / f"sbs-{name}.png")
        results.append((name, score))
    island.stop()
    for name, card, box, where in cards():
        if args.only and args.only not in name:
            continue
        app.processEvents()
        img = card_shot(card, box, where, backdrop)
        img.save(str(args.out / f"{name}.png"))
        mock_name = {"card-offer-todo": "static-offer-todo", "card-question": "static-question",
                     "card-nudge": "static-nudge", "card-return": "static-return", "card-consent": "static-consent",
                     "card-offer-askbar": "static-offer-askbar", "card-offer-window": "static-offer-window",
                     "card-offer-draft": "static-offer-draft"}.get(name, name)
        score = side_by_side(crops / f"{mock_name}-{ms}.png", img, args.out / f"sbs-{name}.png")
        results.append((name, score))
        card.close()
    for name, ov in askbar_shots(args.t):
        if args.only and args.only not in name:
            continue
        app.processEvents()
        img = askbar_shot(ov, args.t, backdrop)
        img.save(str(args.out / f"{name}.png"))
        mock = {"askbar": "static-askbar", "askbar-unsent": "static-offer-askbar"}[name]
        cand = crops / f"{mock}-{ms}.png"
        if name == "askbar-unsent":   # the mock's unsent ask bar is its own route
            cand = next(iter(sorted(crops.glob("static-askbar*q=*"))), crops / "none.png")
        score = side_by_side(cand, img, args.out / f"sbs-{name}.png")
        results.append((name, score))
        ov.close()
    for name, score in results:
        print(f"{name:40s} mean |diff| {score:6.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
