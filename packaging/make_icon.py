"""Write packaging/yuki.ico: the tray's snowflake glyph (yuki.ui.app.tray_icon) at installer/exe sizes.

Run by build.ps1 when the icon is missing: ``python packaging/make_icon.py``.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ACCENT = (126, 180, 255, 255)   # yuki.ui.glass.ACCENT


def glyph(size: int) -> Image.Image:
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
        draw.line([x0, y0, x1, y1], fill=ACCENT, width=width)
        r = width / 2
        for x, y in ((x0, y0), (x1, y1)):     # round caps
            draw.ellipse([x - r, y - r, x + r, y + r], fill=ACCENT)
    return img


def main() -> None:
    out = Path(__file__).with_name("yuki.ico")
    big = glyph(256)
    big.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(out)


if __name__ == "__main__":
    main()
