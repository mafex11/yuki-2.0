"""Proof that the glass refracts: glass shapes over a high-contrast pattern, a moving pill, and what it costs.

Offscreen only (ARCHITECTURE.md rule 7): Qt's ``offscreen`` platform, a standalone
Direct3D 11 QRhi, PNGs out.

    .venv\\Scripts\\python.exe -m yuki.ui.window.proof --out docs\\ui-mockups\\v2            # scenes + animation
    .venv\\Scripts\\python.exe -m yuki.ui.window.proof --out docs\\ui-mockups\\v2 --measure  # + GPU/CPU/RAM
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PySide6.QtCore import QPointF, QRectF, QSize, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QRadialGradient  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

DPR = 2.0
MB = 2 ** 20


def pattern(size: QSize, dpr: float = DPR) -> QImage:
    """A checkerboard with diagonals, rings, colour dots and big type: any bending or fringing shows."""
    w, h = size.width(), size.height()
    img = QImage(int(w * dpr), int(h * dpr), QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(dpr)
    img.fill(QColor(246, 246, 248))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    cell = 28
    for j in range(0, h // cell + 1):
        for i in range(0, w // cell + 1):
            if (i + j) % 2 == 0:
                p.fillRect(QRectF(i * cell, j * cell, cell, cell), QColor(22, 24, 32))
    p.setPen(QPen(QColor(235, 60, 50), 3.0))
    for k in range(-h, w, 56):
        p.drawLine(QPointF(k, 0), QPointF(k + h, h))
    colours = ("#2a78d6", "#1baf7a", "#eda100", "#e87ba4", "#8a5cf0")
    for n, c in enumerate(colours):
        g = QRadialGradient(QPointF(w * (0.12 + 0.19 * n), h * (0.25 + 0.5 * (n % 2))), 70)
        col = QColor(c)
        g.setColorAt(0.0, col)
        g.setColorAt(0.7, col)
        col2 = QColor(col)
        col2.setAlpha(0)
        g.setColorAt(1.0, col2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(w * (0.12 + 0.19 * n), h * (0.25 + 0.5 * (n % 2))), 70, 70)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for r in range(20, 260, 18):
        p.setPen(QPen(QColor(40, 110, 230), 2.0))
        p.drawEllipse(QPointF(w * 0.78, h * 0.62), r, r)
    f = QFont("Segoe UI")
    f.setPixelSize(64)
    f.setWeight(QFont.Weight.Bold)
    p.setFont(f)
    p.setPen(QColor(250, 250, 250))
    p.drawText(QRectF(0, h * 0.40, w, 90), Qt.AlignmentFlag.AlignCenter, "Liquid Glass bends light")
    p.end()
    return img


def _label_layer(size: QSize, labels: list[tuple[QRectF, str, QColor]], dpr: float = DPR) -> QImage:
    img = QImage(int(size.width() * dpr), int(size.height() * dpr), QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(dpr)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    f = QFont("Segoe UI Variable")
    f.setPixelSize(14)
    f.setWeight(QFont.Weight.DemiBold)
    p.setFont(f)
    for rect, text, ink in labels:
        p.setPen(ink)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return img


def proof_shapes(t, size: QSize, dpr: float = DPR):
    """The proof's glass: a panel, a card, pills, a round lens, a prominent pill, a field (logical px)."""
    from yuki.ui.window.refract import Shape

    w = size.width()
    spec = [
        (QRectF(40, 40, 460, 300), 26, "regular", "Panel (regular)"),
        (QRectF(w - 440, 50, 390, 220), 22, "raised", "Card (raised)"),
        (QRectF(560, 70, 220, 44), 22, "control", "Pill"),
        (QRectF(560, 140, 150, 44), 22, "accent", "Prominent"),
        (QRectF(600, 230, 170, 170), 85, "lens", "Lens"),
        (QRectF(80, 420, 520, 52), 26, "control", "Field / text pill"),
        (QRectF(80, 500, 300, 90), 20, "inset", "Row (inset)"),
        (QRectF(w - 520, 330, 460, 250), 28, "floor", "Window sheet (floor)"),
    ]
    shapes, labels = [], []
    for rect, radius, level, label in spec:
        dev = QRectF(rect.x() * dpr, rect.y() * dpr, rect.width() * dpr, rect.height() * dpr)
        shapes.append(Shape(dev, radius * dpr, t.optics[level], dpr))
        ink = t.text_on_accent if level == "accent" else t.text
        labels.append((rect, label, ink))
    return shapes, labels


def proof_image(t, comp, size: QSize = QSize(1280, 640), base: QImage | None = None) -> QImage:
    base = base if base is not None else pattern(size)
    shapes, labels = proof_shapes(t, size)
    return comp.render(base, [("glass", shapes), ("image", _label_layer(size, labels))])


def _pill(t, x: float, y: float, dpr: float = DPR, level: str = "lens", w: float = 220, h: float = 64):
    from yuki.ui.window.refract import Shape

    return Shape(QRectF(x * dpr, y * dpr, w * dpr, h * dpr), h / 2 * dpr, t.optics[level], dpr)


def animation(t, comp, out: Path, name: str, size: QSize = QSize(720, 360), frames: int = 36,
              base: QImage | None = None) -> list[Path]:
    """A glass pill and a lens gliding over the pattern: PNG frames and a GIF."""
    base = base if base is not None else pattern(size)
    paths, pil_frames = [], []
    fdir = out / f"{name}_frames"
    fdir.mkdir(parents=True, exist_ok=True)
    for i in range(frames):
        a = 2 * math.pi * i / frames
        x = 250 + 200 * math.cos(a)
        y = 150 + 90 * math.sin(2 * a)
        shapes = [_pill(t, x - 110, y - 32, level="control"),
                  _pill(t, 360 - 60 + 150 * math.sin(a), 180 + 70 * math.cos(a) - 60, level="lens", w=120, h=120)]
        img = comp.render(base, [("glass", shapes)])
        p = fdir / f"{i:03d}.png"
        img.save(str(p))
        paths.append(p)
        pil_frames.append(p)
    try:
        from PIL import Image

        ims = [Image.open(p).convert("RGB").resize((size.width(), size.height()), Image.LANCZOS) for p in pil_frames]
        ims[0].save(out / f"{name}.gif", save_all=True, append_images=ims[1:], duration=50, loop=0, optimize=False)
    except Exception as exc:   # noqa: BLE001
        print("gif skipped:", exc)
    return paths


# -- measurement --------------------------------------------------------------------------------


def _gpu_engine_util(pid: int, seconds: float = 3.0) -> float | None:
    """This process's summed GPU engine utilisation (%) from Windows' performance counters, sampled."""
    import subprocess

    ps = (f"$s=(Get-Counter -Counter '\\GPU Engine(pid_{pid}_*)\\Utilization Percentage' -SampleInterval 1 "
          f"-MaxSamples {int(seconds)} -ErrorAction SilentlyContinue).CounterSamples; "
          f"if ($s) {{ ($s | Measure-Object -Property CookedValue -Sum).Sum / {int(seconds)} }} else {{ 'none' }}")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                           timeout=seconds + 20)
        return round(float(r.stdout.strip()), 2)
    except Exception:   # noqa: BLE001
        return None


def measure(db: Path | None) -> dict:
    """First paint, RAM delta, GPU ms/frame, CPU% idle and animating, for a full window-sized scene."""
    import threading

    import psutil

    from yuki.ui.window import refract
    from yuki.ui.window.theme import WINDOW_SIZE, theme

    me = psutil.Process()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    del app
    gc.collect()
    mem0 = me.memory_info()
    t = theme("dark")
    size = QSize(WINDOW_SIZE[0] + 112, WINDOW_SIZE[1] + 112)
    t0 = time.perf_counter()
    wall = refract.load_wallpaper(size, DPR)
    t_wall = time.perf_counter() - t0
    if wall is None:
        wall = pattern(size)
    comp = refract.Compositor()
    t_dev = time.perf_counter() - t0
    shapes, _ = proof_shapes(t, size)
    # a window-like load: the floor sheet, a sidebar, 6 nav pills, 4 tiles, 2 panels, 12 pills
    from yuki.ui.window.refract import Shape

    def S(x, y, w, h, r, lvl):
        return Shape(QRectF(x * DPR, y * DPR, w * DPR, h * DPR), r * DPR, t.optics[lvl], DPR)

    floor = [S(56, 56, 1280, 840, 12, "floor")]
    d1 = [S(68, 68, 204, 816, 22, "regular")] + [S(300 + i * 250, 180, 236, 110, 18, "inset") for i in range(4)] + \
         [S(300, 310, 1010, 330, 24, "regular"), S(300, 660, 600, 220, 24, "regular"),
          S(920, 660, 390, 220, 24, "regular")]
    d2 = [S(80, 150 + i * 44, 180, 40, 20, "lens" if i == 0 else "control") for i in range(1)] + \
         [S(320 + i * 90, 700, 80, 26, 13, "control") for i in range(12)]
    layers = [("glass", floor), ("glass", d1), ("glass", d2)]
    first = comp.render(wall, layers)
    t_first = time.perf_counter() - t0
    gc.collect()
    mem1 = me.memory_info()
    # idle: nothing is scheduled, so nothing runs; sample CPU and GPU for 3 s
    me.cpu_percent(None)
    gpu_idle = _gpu_engine_util(os.getpid(), 3.0)
    cpu_idle = me.cpu_percent(None) / psutil.cpu_count()
    # animating: 60 frames/s for 4 s, a pill moving (every frame re-renders every pass; no readback)
    gpu_ms: list[float] = []
    stop = threading.Event()
    util: dict = {}

    def sample() -> None:
        util["gpu"] = _gpu_engine_util(os.getpid(), 3.0)

    th = threading.Thread(target=sample)
    me.cpu_percent(None)
    th.start()
    t_anim = time.perf_counter()
    n = 0
    while time.perf_counter() - t_anim < 4.0:
        x = 400 + 300 * math.sin(n / 20)
        moving = [S(x, 500, 180, 44, 22, "control")]
        frame_start = time.perf_counter()
        gpu_ms.append(comp.draw_only(wall, layers[:2] + [("glass", d2 + moving)]))
        n += 1
        spare = 1 / 60 - (time.perf_counter() - frame_start)
        if spare > 0:
            time.sleep(spare)
    elapsed = time.perf_counter() - t_anim
    cpu_anim = me.cpu_percent(None) / psutil.cpu_count()
    th.join()
    stop.set()
    comp.release()
    del comp, first
    gc.collect()
    mem3 = me.memory_info()
    gpu_ms_sorted = sorted(gpu_ms[5:]) or [0.0]
    res = {
        "canvas_px": [size.width() * DPR, size.height() * DPR],
        "shapes": len(floor) + len(d1) + len(d2) + 1,
        "first_paint_ms": round(t_first * 1000),
        "wallpaper_decode_ms": round(t_wall * 1000),
        "device_and_shaders_ms": round((t_dev - t_wall) * 1000),
        "ram_private_delta_mb": round((getattr(mem1, "private", mem1.rss) - getattr(mem0, "private", mem0.rss)) / MB, 1),
        "ram_wset_delta_mb": round((mem1.rss - mem0.rss) / MB, 1),
        "ram_after_release_mb": round((getattr(mem3, "private", mem3.rss) - getattr(mem0, "private", mem0.rss)) / MB, 1),
        "idle_cpu_pct": round(cpu_idle, 2),
        "idle_gpu_engine_pct": gpu_idle,
        "anim_fps": round(n / elapsed, 1),
        "anim_cpu_pct_of_machine": round(cpu_anim, 2),
        "anim_gpu_engine_pct": util.get("gpu"),
        "anim_gpu_ms_per_frame_median": round(gpu_ms_sorted[len(gpu_ms_sorted) // 2], 3),
        "anim_gpu_ms_per_frame_p95": round(gpu_ms_sorted[int(len(gpu_ms_sorted) * 0.95)], 3),
    }
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--only-measure", action="store_true")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    del app
    if args.only_measure:
        print(json.dumps(measure(args.db), indent=2))
        return 0
    from yuki.ui.window import refract
    from yuki.ui.window.theme import theme

    comp = refract.shared()
    for name in ("light", "dark"):
        t = theme(name)
        img = proof_image(t, comp)
        img.save(str(args.out / f"proof_grid_{name}.png"))
        print(f"  proof_grid_{name}.png  gpu {comp.last_gpu_ms:.2f} ms")
        wall = refract.load_wallpaper(QSize(1280, 640), DPR)
        if wall is not None:
            proof_image(t, comp, base=wall).save(str(args.out / f"proof_wallpaper_{name}.png"))
    animation(theme("light"), comp, args.out, "proof_moving_pill")
    print("  proof_moving_pill.gif")
    refract.release_shared()
    if args.measure:
        print(json.dumps(measure(args.db), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
