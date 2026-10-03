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
   pixel (no resampling), and writes them into ``yuki.ico``: 16, 20, 24, 32, 40, 48, 64 and 256 px.

**The badge** (``mark`` ``badge`` or ``02g``, the user's pick of 3 Oct: YU over Ki in round dots, every size) has its
own source of truth, ``packaging/icon/badge/yuki-icon-grids.json`` (every dot of every size, from the gallery's
``marks-badge.js``), and its frames are committed PNGs, ``packaging/icon/badge-<ramp>/<size>.png``:

1. ``--export`` refreshes the grids from the gallery when it is here (Node), then draws every frame from the grids in
   a headless browser (:func:`find_browser`; ``packaging/icon/render.js``: the gallery's own SVG drawn 1:1 on a
   canvas, the way its ``export/yuki-icon-<size>.png`` were made, so the frames match them byte for byte).
2. A build packs the committed frames into ``yuki.ico`` unchanged (no browser, no Node, no gallery).

Every build also copies the icon to ``yuki/ui/yuki.ico``, the copy the app itself loads (the window's title bar and
taskbar button; :mod:`yuki.ui.webview2`).

``--installer-images`` draws the installer's wizard images (``packaging/installer/``) from the startup splash's still
frame (``yuki/ui/web/planet-splash.js``) in the same headless browser, at each size Inno Setup picks from per DPI.

``--mark`` / ``--ramp`` override icon.json for one run; ``--out`` writes elsewhere (a preview).
"""

from __future__ import annotations

import argparse
import json
import os
import io
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent
ROOT = PKG.parent
ICON_DIR = PKG / "icon"
CONFIG = ICON_DIR / "icon.json"
GALLERY = ROOT / "docs" / "ui-mockups" / "revamp-2026-10" / "logo"
SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
#: The badge's ids in the logo gallery, its grids (the source of truth) and the browser script that draws them.
BADGE_IDS = ("badge", "02g")
GRIDS = ICON_DIR / "badge" / "yuki-icon-grids.json"
RENDER_JS = ICON_DIR / "render.js"
#: The copy of the icon the app loads at run time (bundled with yuki/ui by yuki.spec).
APP_ICON = ROOT / "yuki" / "ui" / "yuki.ico"
WEB_DIR = ROOT / "yuki" / "ui" / "web"
INSTALLER_DIR = PKG / "installer"
#: The wizard images Inno Setup chooses from by the size of its image area (it keeps 164:314 and a square; the
#: sizes are its image areas at 100 %, 125 % and 150 %: 164x314, 202x386, 240x459 in the layout before 6.6,
#: 202x386, 269x515, 336x643 since; Inno Setup picks the best match per DPI): ``win`` is how many of the splash's 520 x 300
#: layout px span the width, ``cx``/``cy`` the layout point at the centre. The large image shows the planet over the
#: name; the small one (58 to 124 px square) only the planet, zoomed so its dots are a whole number of pixels apart
#: (size -> px between dots: crisp, no moire).
WIZARD = ((164, 314), (202, 386), (240, 459), (269, 515), (336, 643))
WIZARD_SMALL = {58: 2, 77: 3, 97: 3, 124: 4}
WIZARD_WIN = 214.0
SPLASH_PITCH = 5.2               # planet-splash.js P: the field's dot pitch in layout px
PLANET_CENTRE = (260.0, 113.6)   # planet-splash.js: column 50, row round(57 * 0.36) of the 5.2 px field


def is_badge(mark: str) -> bool:
    return mark.strip().casefold() in BADGE_IDS


def badge_dir(ramp: str) -> Path:
    return ICON_DIR / f"badge-{ramp}"


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


def dib(png: bytes) -> bytes:
    """One frame as the classic .ico bitmap: a 32-bit BGRA DIB (straight alpha, bottom-up) and its AND mask. Exactly
    the PNG's pixels; every Windows reader and resource tool takes it (some older ones refuse small PNG frames)."""
    from PIL import Image

    im = Image.open(io.BytesIO(png)).convert("RGBA")
    w, h = im.size
    px = im.tobytes()
    rows, mask_rows = [], []
    stride = ((w + 31) // 32) * 4
    for y in range(h - 1, -1, -1):
        row = px[y * w * 4:(y + 1) * w * 4]
        bgra = bytearray(row)
        bgra[0::4], bgra[2::4] = row[2::4], row[0::4]
        rows.append(bytes(bgra))
        bits = bytearray(stride)
        for x in range(w):
            if row[x * 4 + 3] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask_rows.append(bytes(bits))
    xor, mask = b"".join(rows), b"".join(mask_rows)
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(xor) + len(mask), 0, 0, 0, 0)
    return header + xor + mask


def write_ico(pngs: dict[int, bytes], out: Path) -> None:
    """An .ico of every size: bitmap frames under 256 px, a PNG frame at 256 (the usual layout), each exactly its
    source's pixels."""
    sizes = sorted(pngs)
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries, blobs = b"", b""
    for s in sizes:
        blob = pngs[s] if s >= 256 else dib(pngs[s])
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset + len(blobs))
        blobs += blob
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(header + entries + blobs)


# -- the headless browser (the badge's frames and the installer's images; never a visible window) ----------------


def find_browser() -> str | None:
    """A Chromium to draw with, headless: ``YUKI_SHOT_CHROME``, else Playwright's chrome-headless-shell (its own
    binary, never the user's browser profile), else Chrome or Edge. Each run gets a throwaway profile."""
    env = os.environ.get("YUKI_SHOT_CHROME")
    if env and Path(env).exists():
        return env
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    shells = sorted((local / "ms-playwright").glob("chromium_headless_shell-*/*/chrome-headless-shell.exe"), reverse=True)
    if shells:
        return str(shells[0])
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), str(local)):
        for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe"):
            if base and (Path(base) / rel).exists():
                return str(Path(base) / rel)
    return None


def browser_run(job: dict, scripts: list[Path], browser: str | None = None, timeout: float = 90.0) -> dict:
    """Run ``render.js`` (after ``scripts``) on ``job`` in a headless browser; its JSON answer."""
    exe = browser or find_browser()
    if exe is None:
        raise SystemExit("No Chromium to draw with: install Chrome, or set YUKI_SHOT_CHROME to a chrome-headless-shell")
    body = "".join(f"<script>{p.read_text(encoding='utf-8')}</script>\n" for p in [*scripts, RENDER_JS])
    page = ('<!doctype html><html><head><meta charset="utf-8"></head><body><pre id="out"></pre>\n'
            f"<script>window.JOB = {json.dumps(job)};</script>\n{body}</body></html>")
    with tempfile.TemporaryDirectory(prefix="yuki-icon-") as tmp:
        html = Path(tmp) / "render.html"
        html.write_text(page, encoding="utf-8")
        cmd = [exe, "--headless", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--force-device-scale-factor=1",
               f"--user-data-dir={Path(tmp) / 'profile'}", "--virtual-time-budget=20000", "--dump-dom", html.as_uri()]
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    import html as _html

    text = done.stdout or ""
    tag = '<pre id="out">'
    start = text.find(tag)
    end = text.find("</pre>", start)
    answer = _html.unescape(text[start + len(tag):end]) if start >= 0 and end > start else ""
    if not (answer.startswith("BEGIN") and answer.endswith("END")):
        raise SystemExit(f"The browser drew nothing ({done.returncode}): {(done.stderr or '')[-400:]}")
    res = json.loads(answer[5:-3])
    if "error" in res:
        raise SystemExit(f"Drawing failed in the browser: {res['error']}")
    return res


def _png(data_url: str) -> bytes:
    import base64

    return base64.b64decode(data_url.split(",", 1)[1])


def render_badge(ramp: str = "mint", grids: Path = GRIDS, browser: str | None = None) -> dict[int, bytes]:
    """Every size of the badge from its grids, as PNG bytes (``render.js``: the gallery's own drawing)."""
    data = json.loads(grids.read_text(encoding="utf-8"))
    if ramp not in data.get("colourSets", {}):
        raise SystemExit(f"unknown colour set: {ramp} (have {', '.join(data.get('colourSets', {}))})")
    res = browser_run({"kind": "badge", "grids": data, "set": ramp}, [], browser)
    return {int(s): _png(u) for s, u in res.items()}


def export_badge(ramp: str, gallery: Path = GALLERY, browser: str | None = None) -> Path:
    """Refresh the grids from the gallery (when it and Node are here), then draw and save every frame."""
    node = shutil.which("node")
    if node is not None and (gallery / "marks-badge.js").exists():
        cmd = [node, str(ICON_DIR / "export_mark.js"), "--logo", str(gallery), "--mark", "badge", "--out", str(GRIDS.parent)]
        done = subprocess.run(cmd, capture_output=True, text=True)
        if done.returncode != 0:
            raise SystemExit((done.stderr or done.stdout or "export failed").strip())
        print(done.stdout.strip())
    elif not GRIDS.exists():
        raise SystemExit(f"No badge grids at {GRIDS} and no gallery to export them from")
    out = badge_dir(ramp)
    out.mkdir(parents=True, exist_ok=True)
    for s, png in render_badge(ramp, browser=browser).items():
        (out / f"{s}.png").write_bytes(png)
    print(f"drew the badge ({ramp}) at {', '.join(str(s) for s in SIZES)} px into {out}")
    return out


def installer_images(out_dir: Path = INSTALLER_DIR, browser: str | None = None) -> list[Path]:
    """The installer's wizard images from the splash's still frame: 24-bit BMPs (every Inno Setup 6 reads them),
    each drawn at its own size."""
    from PIL import Image

    specs = [{"file": f"wizard-{w}x{h}.bmp", "w": w, "h": h, "win": WIZARD_WIN, "cx": 260.0, "cy": 150.0}
             for w, h in WIZARD]
    specs += [{"file": f"wizard-small-{s}.bmp", "w": s, "h": s, "win": s * SPLASH_PITCH / px, "cx": PLANET_CENTRE[0],
               "cy": PLANET_CENTRE[1], "text": False} for s, px in WIZARD_SMALL.items()]
    res = browser_run({"kind": "installer", "images": specs, "set": "mint"},
                      [WEB_DIR / "dots.js", WEB_DIR / "planet-splash.js"], browser)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for spec in specs:
        im = Image.open(io.BytesIO(_png(res[spec["file"]]))).convert("RGBA")
        flat = Image.new("RGB", im.size, (0x0B, 0x0B, 0x0C))
        flat.paste(im, mask=im.getchannel("A"))
        path = out_dir / spec["file"]
        flat.save(path, format="BMP")
        written.append(path)
    print(f"drew {len(written)} installer images into {out_dir}")
    return written


def badge_frames(ramp: str) -> dict[int, bytes]:
    src = badge_dir(ramp)
    missing = [s for s in SIZES if not (src / f"{s}.png").exists()]
    if missing:
        raise SystemExit(f"No badge frames for {ramp} in {src} ({missing}): run make_icon.py --export first")
    return {s: (src / f"{s}.png").read_bytes() for s in SIZES}


def build(mark: str, ramp: str, out: Path, *, allow_export: bool = True) -> Path:
    if is_badge(mark):
        write_ico(badge_frames(ramp), out)
        return out
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
    ap.add_argument("--export", action="store_true",
                    help="export the mark from the gallery (SVGs: Node; the badge: its grids, then frames in a headless browser)")
    ap.add_argument("--installer-images", action="store_true",
                    help="draw the installer's wizard images from the splash's still frame (a headless browser)")
    ap.add_argument("--browser", default=None, help="the Chromium to draw with (default: found by find_browser)")
    ap.add_argument("--out", type=Path, default=PKG / "yuki.ico")
    args = ap.parse_args(argv)
    if args.installer_images:
        installer_images(browser=args.browser)
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
        if is_badge(mark):
            export_badge(ramp, browser=args.browser)
        else:
            export(mark, ramp)
    print(build(mark, ramp, args.out))
    if args.out.resolve() == (PKG / "yuki.ico").resolve():
        APP_ICON.parent.mkdir(parents=True, exist_ok=True)
        if not APP_ICON.exists() or APP_ICON.read_bytes() != args.out.read_bytes():
            shutil.copyfile(args.out, APP_ICON)
            print(f"copied to {APP_ICON} (the icon the app loads)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
