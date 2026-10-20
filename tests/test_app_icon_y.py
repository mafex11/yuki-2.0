"""The Y app icon (the user's pick, 4 Oct): one bold Y in round dots with an accent joint (ember on the umber tile since
20 Oct), hand-tuned per size in packaging/icon/y/y_grids.py. The committed grids are that layout, every dot sits on
whole pixels (round dots 3 px or more from 24 px up, 2 x 2 pixel blocks at 16 and 20), the committed frames are drawn
crisp (each dot's centre pure bone or ember, the tile untouched next to it) and match a fresh drawing byte for byte,
and yuki.ico packs them pixel for pixel. Offline; the redraw uses a headless browser only when one is installed."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load("make_icon", PKG / "make_icon.py")
Y = _load("y_grids", M.Y_LAYOUT)
GRIDS = M.grids_file("y")
FRAMES = M.frames_dir("y", "ember")


def _hex(px: tuple) -> str:
    return "#%02X%02X%02X" % tuple(px[:3])


def _frame(s: int):
    from PIL import Image

    return Image.open(FRAMES / f"{s}.png").convert("RGBA")


def test_y_is_the_picked_mark_and_its_grids_are_its_layout():
    cfg = M.load_config()
    assert M.grid_mark(cfg["mark"]) == "y" and M.grid_mark("Y") == "y"
    assert GRIDS.read_text(encoding="utf-8") == Y.text()          # committed = the layout (no drift, no gallery needed)
    g = json.loads(GRIDS.read_text(encoding="utf-8"))
    assert sorted(g["sizes"]) == sorted(M.SIZES) and set(g["colourSets"]) == {"mint", "night", "bang", "ember"}


@pytest.mark.parametrize("s", (16, 20, 24, 32, 40, 48, 64, 256))
def test_every_dot_is_on_whole_pixels_and_big_enough(s):
    icon = json.loads(GRIDS.read_text(encoding="utf-8"))["icons"][str(s)]
    assert icon["tile"]["fill"] == "#090705" and (icon["tile"]["stroke"] is not None) == (s >= 32)
    for d in icon["dots"]:
        assert float(d["x"] - d["r"]).is_integer() and float(d["y"] - d["r"]).is_integer(), d   # edges on pixel edges
        assert 0 < d["x"] - d["r"] and d["x"] + d["r"] < s and 0 < d["y"] - d["r"] and d["y"] + d["r"] < s
        if s >= 24:
            assert d.get("shape") is None and 2 * d["r"] >= 3, d                  # round dots, 3 px or more
        else:
            assert d.get("shape") == "px" and d["r"] == 1, d                      # 2 x 2 blocks at 16 and 20


@pytest.mark.parametrize("s", (16, 20, 24, 32, 40, 48, 64, 256))
def test_the_dots_draw_a_symmetric_y_with_one_accent_joint(s):
    dots = json.loads(GRIDS.read_text(encoding="utf-8"))["icons"][str(s)]["dots"]
    acc = [d for d in dots if d["role"] == "acc"]
    assert len(acc) == 1 and all(d["role"] in ("ink", "acc") for d in dots)
    cx, jy = acc[0]["x"], acc[0]["y"]
    pos = {(d["x"], d["y"]) for d in dots}
    assert all((2 * cx - x, y) in pos for x, y in pos)                            # mirror image about the stem
    stem = [y for x, y in pos if x == cx and y > jy]
    left = [(x, y) for x, y in pos if x < cx]
    assert len(stem) >= 2 and left and all(y < jy for _x, y in left)                # arms above the joint, stem below
    assert min(y for _x, y in left) < jy and max(stem) - jy >= jy - min(y for _x, y in left)  # a stem as long as the arms rise
    # dots never touch: at least 1 px of tile between any two
    for i, a in enumerate(dots):
        for b in dots[i + 1:]:
            if a.get("shape") == "px":
                assert max(abs(a["x"] - b["x"]), abs(a["y"] - b["y"])) >= 2 * a["r"] + 1, (a, b)
            else:
                assert ((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5 >= a["r"] + b["r"] + 1, (a, b)


@pytest.mark.parametrize("s", (16, 20, 24, 32, 40, 48, 64, 256))
def test_the_frames_are_crisp(s):
    icon = json.loads(GRIDS.read_text(encoding="utf-8"))["icons"][str(s)]
    im = _frame(s)
    assert im.size == (s, s)
    fill = icon["tile"]["fill"]
    for d in icon["dots"]:
        x0, y0, x1, y1 = (int(d["x"] - d["r"]), int(d["y"] - d["r"]), int(d["x"] + d["r"]), int(d["y"] + d["r"]))
        mid = im.getpixel((int(d["x"]), int(d["y"])))
        assert _hex(mid) == d["c"] and mid[3] == 255, (s, d)                          # the dot's centre is pure colour
        for px in ((x0 - 1, int(d["y"])), (x1, int(d["y"])), (int(d["x"]), y0 - 1), (int(d["x"]), y1)):
            assert _hex(im.getpixel(px)) == fill, (s, d, px)                          # its edge stops on a pixel edge
        if d.get("shape") == "px":                                                    # a block: every pixel exact
            assert {_hex(im.getpixel((x, y))) for x in range(x0, x1) for y in range(y0, y1)} == {d["c"]}


def test_the_y_reads_on_dark_and_light_taskbars():
    # the tile is opaque behind the whole Y and the Y is far brighter than the tile, so the taskbar never shows through
    for s in M.SIZES:
        im = _frame(s)
        icon = json.loads(GRIDS.read_text(encoding="utf-8"))["icons"][str(s)]
        xs = [d["x"] for d in icon["dots"]]
        ys = [d["y"] for d in icon["dots"]]
        r = icon["dots"][0]["r"]
        box = (int(min(xs) - r) - 1, int(min(ys) - r) - 1, int(max(xs) + r) + 1, int(max(ys) + r) + 1)
        assert im.crop(box).getchannel("A").getextrema()[0] == 255, s


def test_the_ico_packs_the_y_frames_pixel_for_pixel():
    from PIL import Image

    ico = Image.open(PKG / "yuki.ico")
    assert sorted(w for w, _h in ico.info["sizes"]) == list(M.SIZES)
    for s in M.SIZES:
        ico.size = (s, s)
        assert ico.convert("RGBA").tobytes() == _frame(s).tobytes(), s
    assert M.APP_ICON.read_bytes() == (PKG / "yuki.ico").read_bytes()


@pytest.mark.skipif(M.find_browser() is None, reason="no headless Chromium on this PC")
def test_the_frames_redraw_identically_from_the_grids():
    drawn = M.render_grids(GRIDS, "ember")
    for s in M.SIZES:
        assert drawn[s] == (FRAMES / f"{s}.png").read_bytes(), s


def test_export_writes_the_layout_then_draws_it(tmp_path, monkeypatch):
    # --export for the Y needs no gallery and no Node: the layout writes the grids, the browser draws them
    monkeypatch.setattr(M, "ICON_DIR", tmp_path)
    drawn = {}

    def fake_render(grids, ramp="mint", browser=None):
        drawn["grids"] = grids.read_text(encoding="utf-8")
        from PIL import Image

        out = {}
        for s in M.SIZES:
            buf = io.BytesIO()
            Image.new("RGBA", (s, s), (11, 11, 12, 255)).save(buf, "PNG")
            out[s] = buf.getvalue()
        return out

    monkeypatch.setattr(M, "render_grids", fake_render)
    out = M.export_grids("y", "mint")
    assert out == tmp_path / "y-mint" and all((out / f"{s}.png").exists() for s in M.SIZES)
    assert drawn["grids"] == Y.text()
