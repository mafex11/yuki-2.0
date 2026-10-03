"""Write packaging/yuki.ico: the app icon (the exe, the installer, the shortcuts).

The logo is one parameter, ``mark`` in ``packaging/icon/icon.json`` (a mark id or number from the logo gallery,
``docs/ui-mockups/revamp-2026-10/logo``, for example ``planet`` or ``06a``), with its colour set ``ramp`` (``mint``,
``night`` or ``bang``). While ``mark`` is empty (no logo picked yet) the current ``yuki.ico`` is kept as it is.

Once a mark is picked:

1. ``python packaging/make_icon.py --export`` runs the gallery's own dot code (``packaging/icon/export_mark.js``,
   Node) and writes one SVG per size into ``packaging/icon/<mark>-<ramp>/``: the hand-tuned round-dot lists at 48 px
   and under (round dots at every size, never square pixels), the sampled dot field at 64 and 256 px. Commit them, so
   a build needs neither Node nor the (gitignored) gallery.
2. ``python packaging/make_icon.py`` (build.ps1 runs it on every build) draws each size from its own SVG, pixel for
   pixel (no resampling), and writes them into ``yuki.ico`` as PNG entries: 16, 20, 24, 32, 40, 48, 64 and 256 px.

``--mark`` / ``--ramp`` override icon.json for one run; ``--out`` writes elsewhere (a preview).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent
ICON_DIR = PKG / "icon"
CONFIG = ICON_DIR / "icon.json"
GALLERY = PKG.parent / "docs" / "ui-mockups" / "revamp-2026-10" / "logo"
SIZES = (16, 20, 24, 32, 40, 48, 64, 256)


def load_config() -> dict:
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def svg_dir(mark: str, ramp: str) -> Path:
    return ICON_DIR / f"{mark}-{ramp}"


def export(mark: str, ramp: str, gallery: Path = GALLERY, out: Path | None = None) -> Path:
    """Run the gallery's dot code (Node) for ``mark`` in ``ramp``: one SVG per size."""
    node = shutil.which("node")
    if node is None:
        raise SystemExit("Node is needed to export a mark (https://nodejs.org); or commit the SVGs from another PC.")
    if not (gallery / "dots.js").exists():
        raise SystemExit(f"The logo gallery is not here: {gallery}")
    out = out or svg_dir(mark, ramp)
    cmd = [node, str(ICON_DIR / "export_mark.js"), "--logo", str(gallery), "--mark", mark, "--ramp", ramp,
           "--out", str(out), "--sizes", ",".join(str(s) for s in SIZES)]
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit((done.stderr or done.stdout or "export failed").strip())
    print(done.stdout.strip())
    return out


def render_png(svg: Path, size: int) -> bytes:
    """One SVG drawn at exactly ``size`` x ``size`` px (Qt's SVG renderer, offscreen), as PNG bytes."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    _app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        raise SystemExit(f"Not a valid SVG: {svg}")
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(p, QRectF(0, 0, size, size))
    p.end()
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    del _app
    return bytes(data)


def write_ico(pngs: dict[int, bytes], out: Path) -> None:
    """An .ico of PNG entries (Windows Vista and later read PNG at every size)."""
    sizes = sorted(pngs)
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries, blobs = b"", b""
    for s in sizes:
        blob = pngs[s]
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset + len(blobs))
        blobs += blob
    out.write_bytes(header + entries + blobs)


def build(mark: str, ramp: str, out: Path, *, allow_export: bool = True) -> Path:
    src = svg_dir(mark, ramp)
    if not all((src / f"{s}.svg").exists() for s in SIZES):
        if not allow_export:
            raise SystemExit(f"No SVGs for {mark} ({ramp}) in {src}: run make_icon.py --export first")
        export(mark, ramp)
    write_ico({s: render_png(src / f"{s}.svg", s) for s in SIZES}, out)
    return out


def legacy_glyph(out: Path) -> None:
    """The old snowflake icon, only when no yuki.ico exists at all and no logo has been picked."""
    from PIL import Image, ImageDraw

    accent = (126, 180, 255, 255)
    size = 256
    scale = size / 64.0
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([3 * scale, 3 * scale, 61 * scale, 61 * scale], radius=16 * scale,
                           fill=(16, 17, 20, 235), outline=(255, 255, 255, 40), width=max(1, round(2 * scale)))
    width = max(2, round(5 * scale))
    c, arm = 32 * scale, 17 * scale
    lines = [(c, c - arm, c, c + arm)]
    for dx, dy in ((arm, arm), (arm, -arm)):
        lines.append((c - dx * 0.87, c - dy * 0.5, c + dx * 0.87, c + dy * 0.5))
    for x0, y0, x1, y1 in lines:
        draw.line([x0, y0, x1, y1], fill=accent, width=width)
        r = width / 2
        for x, y in ((x0, y0), (x1, y1)):
            draw.ellipse([x - r, y - r, x + r, y + r], fill=accent)
    img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mark", help="a mark id or number from the logo gallery (overrides icon.json)")
    ap.add_argument("--ramp", help="the colour set: mint | night | bang (overrides icon.json)")
    ap.add_argument("--export", action="store_true", help="export the mark's SVGs from the gallery (needs Node)")
    ap.add_argument("--out", type=Path, default=PKG / "yuki.ico")
    args = ap.parse_args(argv)
    cfg = load_config()
    mark = (args.mark if args.mark is not None else str(cfg.get("mark") or "")).strip()
    ramp = (args.ramp or str(cfg.get("ramp") or "mint")).strip()
    if not mark:
        if not args.out.exists():
            legacy_glyph(args.out)
            print(f"no logo picked yet: wrote the old icon to {args.out}")
        else:
            print(f"no logo picked yet (packaging/icon/icon.json \"mark\"): keeping {args.out}")
        return 0
    if args.export:
        export(mark, ramp)
    print(build(mark, ramp, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
