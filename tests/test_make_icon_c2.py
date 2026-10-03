"""The app icon pipeline (packaging/make_icon.py): the logo is one parameter; with none picked the current icon is kept;
a picked mark's SVGs become a multi-size .ico, each size drawn from its own SVG. Offscreen, in a temp folder."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]


def _make_icon():
    spec = importlib.util.spec_from_file_location("make_icon", ROOT / "packaging" / "make_icon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dot_svg(size: int) -> str:
    r = max(1.5, size / 8)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
            f'<rect width="{size}" height="{size}" rx="{size / 5}" fill="#0B0B0C"/>'
            f'<circle cx="{size / 2}" cy="{size / 2}" r="{r}" fill="#6FEBC4"/></svg>')


def test_no_pick_keeps_the_current_icon(tmp_path, monkeypatch):
    M = _make_icon()
    monkeypatch.setattr(M, "CONFIG", tmp_path / "icon.json")
    (tmp_path / "icon.json").write_text('{"mark": "", "ramp": "mint"}', encoding="utf-8")
    ico = tmp_path / "yuki.ico"
    ico.write_bytes(b"current icon")
    assert M.main(["--out", str(ico)]) == 0
    assert ico.read_bytes() == b"current icon"


def test_a_picked_mark_becomes_every_size_with_round_dots(tmp_path, monkeypatch):
    from PIL import Image

    M = _make_icon()
    monkeypatch.setattr(M, "ICON_DIR", tmp_path)
    monkeypatch.setattr(M, "CONFIG", tmp_path / "icon.json")
    (tmp_path / "icon.json").write_text('{"mark": "testmark", "ramp": "mint"}', encoding="utf-8")
    src = tmp_path / "testmark-mint"
    src.mkdir()
    for s in M.SIZES:
        (src / f"{s}.svg").write_text(_dot_svg(s), encoding="utf-8")
    out = tmp_path / "yuki.ico"
    assert M.main(["--out", str(out)]) == 0
    ico = Image.open(out)
    assert sorted(ico.info["sizes"]) == [(s, s) for s in M.SIZES]
    for s in (16, 48, 256):
        ico.size = (s, s)
        im = ico.convert("RGBA")
        c = im.getpixel((s // 2, s // 2))
        assert c[1] > 200 and c[3] == 255                     # the mint dot in the middle, drawn at this size
        corner = im.getpixel((0, 0))
        assert corner[3] < 255                                # rounded tile corners: drawn, not resampled
