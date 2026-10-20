"""The "Y" app icon (the user's pick of 4 Oct 2026: just a bold Y for now): every dot of every size, hand-tuned.

One bold Y in the Lattice dot style: round bone dots on the umber tile, and one ember dot at the Y's joint (20 Oct:
the website's colours, the user's pick, as on its favicon; the Mint set still draws the 4 Oct look, bone and mint on
the graphite tile, whose frames stay in ``packaging/icon/y-mint/``). Each size has its own layout (below), all on
whole pixels so every frame is crisp:

* 24 px and up: round dots, 3 px or more across, an odd width centred on a pixel centre (x.5) or an even width on a
  pixel corner (whole numbers), so a dot's edge always falls on a pixel edge;
* 16 and 20 px: the smallest crisp dot, a 2 x 2 block of whole pixels (``shape`` ``px``).

The arms run at 45 degrees (whole-pixel steps), the stem straight down, and the dots along both are spaced alike.

``python packaging/make_icon.py --mark y --export`` writes ``packaging/icon/y/yuki-icon-grids.json`` from this file
(the grids are the icon's source of truth, in the badge's layout) and draws the frames from it; a build only packs
the committed frames. Run this file directly to rewrite the grids without drawing.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "yuki-icon-grids.json"
SIZES = (256, 64, 48, 40, 32, 24, 20, 16)

NAME = "Yuki app icon · Y"
ABOUT = (
    "Dot grids per icon size, in pixels from the top-left of the icon. Draw the tile first: a rounded square with corner "
    "radius rx, anti-aliased, in fill; if stroke is set, add a 1 px stroke inset by .5 (rx - .5). Then draw every dot in "
    "order: shape \"px\" fills a block of whole pixels 2r wide centred on x, y; any other dot is an anti-aliased circle of "
    "radius r. Colours are the set baseSet names (Ember); to draw another set, colour each dot by its role from "
    "colourSets[set][role] (ink is the Y, acc its joint), and the tile in the set's tile (fill) and rim (stroke) when "
    "it has them."
)
#: The badge's colour sets and tiles (one family of icons). (20 Oct) Ember is the website's: bone ink, the ember
#: joint, the darkest step of its ember dot ramp as the field, the umber dot step as the base, and the umber tile with
#: the umber rim of its favicon (``yuki-website/public/favicon.svg``, this layout at 64 px). Mint carries its graphite
#: tile and rim, so it still draws the committed y-mint frames byte for byte.
COLOUR_SETS = {
    "mint": {"ink": "#F2EFE9", "acc": "#6FEBC4", "field": "#284A50", "base": "#26292C", "tile": "#0B0B0C",
             "rim": "#2A2D31"},
    "night": {"ink": "#BFC8CC", "acc": "#F6F0E4", "field": "#3A4044", "base": "#26292C"},
    "bang": {"ink": "#F2EFE9", "acc": "#FFB85C", "field": "#1F3B47", "base": "#26292C"},
    "ember": {"ink": "#F2EFE9", "acc": "#FF6A1A", "field": "#5A2E15", "base": "#292521", "tile": "#090705",
              "rim": "#2F2A26"},
}
#: The set the grids are written in (each dot's ``c``, the tile's fill and stroke): the one built (icon.json ``ramp``).
BASE_SET = "ember"
FILL, STROKE = COLOUR_SETS[BASE_SET]["tile"], COLOUR_SETS[BASE_SET]["rim"]
TILE_RX = {256: 56, 64: 13, 48: 10, 40: 9, 32: 7, 24: 5, 20: 4, 16: 3}
STROKE_FROM = 32                 # the 1 px rim from 32 px up; below it the rim would eat the tile

# Each size: the joint (cx, jy), one arm step (dx, dy) and how many dots per arm, the stem step and how many stem
# dots under the joint, the dot radius r, and its shape. The extents in the comments are the Y's ink box.
LAYOUT = {
    # 4 x the 64 px drawing on an even grid: 28 px dots, 34 px apart on the arms, 32 on the stem; ink 42..214 x 30..226
    256: dict(cx=128, jy=116, dx=24, dy=24, arm=3, sy=32, stem=3, r=14),
    # 7 px dots (on pixel centres), 8.5 apart on the arms, 8 on the stem; ink 10..53 x 7..56
    64: dict(cx=31.5, jy=28.5, dx=6, dy=6, arm=3, sy=8, stem=3, r=3.5),
    # 5 px dots, 7.1 / 6 apart; ink 6..41 x 5..43
    48: dict(cx=23.5, jy=22.5, dx=5, dy=5, arm=3, sy=6, stem=3, r=2.5),
    # from here down two dots per arm, so the dots stay big: 6 px dots, 8.5 / 7 apart; ink 5..35 x 4..36
    40: dict(cx=20, jy=19, dx=6, dy=6, arm=2, sy=7, stem=2, r=3),
    # 4 px dots, 5.7 / 5 apart; ink 6..26 x 5..27
    32: dict(cx=16, jy=15, dx=4, dy=4, arm=2, sy=5, stem=2, r=2),
    # 3 px round dots (the smallest round dot), 4.2 / 4 apart; ink 4..19 x 4..21
    24: dict(cx=11.5, jy=11.5, dx=3, dy=3, arm=2, sy=4, stem=2, r=1.5),
    # 2 x 2 pixel blocks, a 1 px gap everywhere; ink 3..17 x 3..17
    20: dict(cx=10, jy=10, dx=3, dy=3, arm=2, sy=3, stem=2, r=1, shape="px"),
    # one block per arm, set wide (4 across, 3 down) so the Y opens up; ink 3..13 x 2..13
    16: dict(cx=8, jy=6, dx=4, dy=3, arm=1, sy=3, stem=2, r=1, shape="px"),
}


def _num(v: float) -> float | int:
    return int(v) if float(v).is_integer() else v


def icon(size: int) -> dict:
    """One size: its tile and its dots, the arms from the tips in, then the stem, then the joint (drawn last)."""
    L = LAYOUT[size]
    cx, jy, dx, dy = L["cx"], L["jy"], L["dx"], L["dy"]
    pts = [(cx - k * dx, jy - k * dy) for k in range(L["arm"], 0, -1)]
    pts += [(cx + k * dx, jy - k * dy) for k in range(L["arm"], 0, -1)]
    pts += [(cx, jy + k * L["sy"]) for k in range(1, L["stem"] + 1)]
    ink, acc = COLOUR_SETS[BASE_SET]["ink"], COLOUR_SETS[BASE_SET]["acc"]
    extra = {"shape": L["shape"]} if L.get("shape") else {}
    dots = [{"x": _num(x), "y": _num(y), "r": _num(L["r"]), "c": ink, "role": "ink", **extra} for x, y in pts]
    dots.append({"x": _num(cx), "y": _num(jy), "r": _num(L["r"]), "c": acc, "role": "acc", **extra})
    tile = {"fill": FILL, "rx": TILE_RX[size], "stroke": STROKE if size >= STROKE_FROM else None}
    return {"tile": tile, "dots": dots}


def grids() -> dict:
    return {"name": NAME, "about": ABOUT, "sizes": list(SIZES), "baseSet": BASE_SET, "colourSets": COLOUR_SETS,
            "icons": {str(s): icon(s) for s in SIZES}}


def text() -> str:
    """The grids as JSON in the badge's layout (one dot per line), so a diff shows each dot that moved."""
    g, c = grids(), (lambda v: json.dumps(v, separators=(",", ":"), ensure_ascii=False))
    icons = []
    for s in g["sizes"]:
        ic = g["icons"][str(s)]
        dots = ",\n".join("        " + c(d) for d in ic["dots"])
        icons.append(f'    {c(str(s))}: {{\n      "tile": {c(ic["tile"])},\n      "dots": [\n{dots}\n      ]\n    }}')
    return ('{\n  "name": ' + c(g["name"]) + ',\n  "about": ' + c(g["about"]) + ',\n  "sizes": ' + c(g["sizes"]) +
            ',\n  "baseSet": ' + c(g["baseSet"]) + ',\n  "colourSets": ' + c(g["colourSets"]) +
            ',\n  "icons": {\n' + ",\n".join(icons) + "\n  }\n}\n")


def write(out: Path = OUT) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(text().encode("utf-8"))      # bytes: the same file (LF) on every PC
    return out


if __name__ == "__main__":
    print(f"wrote the Y grids to {write()}")
