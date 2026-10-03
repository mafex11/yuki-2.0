"""The app icon is the 02g badge (the user's pick, 3 Oct): its grids are the source of truth, its committed frames are
the gallery's PNGs byte for byte, and packaging/yuki.ico (and the app's copy, yuki/ui/yuki.ico) holds every frame
pixel for pixel, read back by PIL and by Windows itself. The installer's wizard images are the splash's still frame
at the sizes Inno Setup picks from. A headless browser redraws the frames only when one is installed (never a
visible window, a throwaway profile). Offline; everything written goes to a temp folder."""

from __future__ import annotations

import ctypes
import importlib.util
import io
import json
import os
import struct
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging"
ICO = PKG / "yuki.ico"
FRAMES = PKG / "icon" / "badge-mint"
_REL = Path("docs") / "ui-mockups" / "revamp-2026-10" / "logo" / "export"
# the gallery is gitignored: in an agent worktree (.claude/worktrees/<name>) it is in the main checkout
GALLERY = next((p for p in (ROOT / _REL, ROOT.parents[2] / _REL) if p.exists()), ROOT / _REL)


def _make_icon():
    spec = importlib.util.spec_from_file_location("make_icon", PKG / "make_icon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _make_icon()


def _rgba(data: bytes) -> tuple[tuple[int, int], bytes]:
    from PIL import Image

    im = Image.open(io.BytesIO(data)).convert("RGBA")
    return im.size, im.tobytes()


def _ico_frames(path: Path) -> dict[int, bytes]:
    from PIL import Image

    ico = Image.open(path)
    out = {}
    for w, _h in ico.info["sizes"]:
        ico.size = (w, w)
        im = ico.convert("RGBA")
        out[w] = im.tobytes()
    return out


def test_the_picked_logo_is_the_badge():
    cfg = M.load_config()
    assert M.is_badge(cfg["mark"]) and cfg["ramp"] == "mint"
    assert M.is_badge("02g") and M.is_badge("Badge") and not M.is_badge("planet")


def test_the_grids_hold_every_size_with_its_tile_and_round_dots():
    g = json.loads(M.GRIDS.read_text(encoding="utf-8"))
    assert sorted(g["sizes"]) == sorted(M.SIZES)
    assert set(g["colourSets"]) == {"mint", "night", "bang"}
    for s in M.SIZES:
        icon = g["icons"][str(s)]
        assert icon["tile"]["fill"] == "#0B0B0C" and icon["tile"]["rx"] > 0
        roles = {d["role"] for d in icon["dots"]}
        assert {"ink", "acc"} <= roles                             # the letters and the one accent dot, at every size
        assert sum(d["role"] == "acc" for d in icon["dots"]) == 1   # exactly one mint i-dot
        assert all(0 <= d["x"] <= s and 0 <= d["y"] <= s for d in icon["dots"])
        assert all(d.get("shape") == "px" for d in icon["dots"]) == (s <= 20)   # the 1 px micro font at 16 and 20


def test_the_ico_holds_every_frame_pixel_for_pixel():
    frames = _ico_frames(ICO)
    assert sorted(frames) == list(M.SIZES)
    for s in M.SIZES:
        size, px = _rgba((FRAMES / f"{s}.png").read_bytes())
        assert size == (s, s) and frames[s] == px, s


def test_small_frames_are_bitmaps_and_256_is_png():
    data = ICO.read_bytes()
    _res, kind, n = struct.unpack_from("<HHH", data, 0)
    assert kind == 1 and n == len(M.SIZES)
    for i in range(n):
        w, _h, _c, _r, _p, bpp, size, off = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        blob = data[off:off + size]
        assert bpp == 32
        if w == 0:
            assert blob[:8] == b"\x89PNG\r\n\x1a\n"
        else:
            assert struct.unpack_from("<I", blob, 0)[0] == 40     # BITMAPINFOHEADER


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own icon reader")
def test_windows_reads_every_frame_exactly():
    from ctypes import wintypes as W

    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    H = ctypes.c_void_p

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", W.BOOL), ("x", W.DWORD), ("y", W.DWORD), ("hbmMask", H), ("hbmColor", H)]

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", W.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long), ("biPlanes", W.WORD),
                    ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                    ("a", ctypes.c_long), ("b", ctypes.c_long), ("c", W.DWORD), ("d", W.DWORD)]

    u.LoadImageW.restype = H
    u.LoadImageW.argtypes = [H, W.LPCWSTR, W.UINT, ctypes.c_int, ctypes.c_int, W.UINT]
    u.GetIconInfo.argtypes = [H, ctypes.POINTER(ICONINFO)]
    u.GetDC.restype = H
    u.GetDC.argtypes = [H]
    u.ReleaseDC.argtypes = [H, H]
    u.DestroyIcon.argtypes = [H]
    g.GetDIBits.argtypes = [H, H, W.UINT, W.UINT, ctypes.c_void_p, ctypes.POINTER(BIH), W.UINT]
    g.DeleteObject.argtypes = [H]
    for s in M.SIZES:
        h = u.LoadImageW(None, str(ICO), 1, s, s, 0x10)            # IMAGE_ICON, LR_LOADFROMFILE: Windows picks the frame
        assert h, s
        info = ICONINFO()
        assert u.GetIconInfo(h, ctypes.byref(info))
        bi = BIH(40, s, -s, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(s * s * 4)
        dc = u.GetDC(None)
        rows = g.GetDIBits(dc, info.hbmColor, 0, s, buf, ctypes.byref(bi), 0)
        u.ReleaseDC(None, dc)
        g.DeleteObject(info.hbmColor)
        g.DeleteObject(info.hbmMask)
        u.DestroyIcon(h)
        raw = bytearray(buf.raw)
        raw[0::4], raw[2::4] = raw[2::4], raw[0::4]
        assert rows == s and bytes(raw) == _rgba((FRAMES / f"{s}.png").read_bytes())[1], s


def test_the_app_loads_the_same_icon_and_the_build_bundles_it():
    assert M.APP_ICON.read_bytes() == ICO.read_bytes()
    from yuki.ui import web_window

    assert web_window.APP_ICON == M.APP_ICON
    spec = (PKG / "yuki.spec").read_text(encoding="utf-8")
    assert '(str(ROOT / "yuki" / "ui" / "yuki.ico"), "yuki/ui")' in spec and 'icon=ICON' in spec


def test_a_build_packs_the_committed_frames_unchanged(tmp_path):
    out = tmp_path / "yuki.ico"
    before = M.APP_ICON.read_bytes()
    assert M.main(["--out", str(out)]) == 0
    assert out.read_bytes() == ICO.read_bytes()                    # deterministic: the committed icon is this build
    assert M.APP_ICON.read_bytes() == before                       # a preview never touches the app's copy


def test_missing_frames_say_how_to_make_them(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "ICON_DIR", tmp_path)
    with pytest.raises(SystemExit, match="--export"):
        M.build("badge", "mint", tmp_path / "x.ico")


def test_bitmap_frames_keep_soft_alpha_exactly(tmp_path):
    from PIL import Image

    pngs = {}
    for s in (16, 20, 256):
        im = Image.new("RGBA", (s, s))
        im.putdata([((x * 13) % 256, (y * 7) % 256, 90, (x * y) % 256) for y in range(s) for x in range(s)])
        buf = io.BytesIO()
        im.save(buf, "PNG")
        pngs[s] = buf.getvalue()
    out = tmp_path / "t.ico"
    M.write_ico(pngs, out)
    frames = _ico_frames(out)
    for s, png in pngs.items():
        assert frames[s] == _rgba(png)[1], s


@pytest.mark.skipif(not GALLERY.exists(), reason="the logo gallery (gitignored) is not on this PC")
def test_the_frames_are_the_gallerys_pngs_byte_for_byte():
    for s in M.SIZES:
        assert (FRAMES / f"{s}.png").read_bytes() == (GALLERY / f"yuki-icon-{s}.png").read_bytes(), s
    assert json.loads(M.GRIDS.read_text(encoding="utf-8")) == json.loads((GALLERY / "yuki-icon-grids.json").read_text(encoding="utf-8"))


@pytest.mark.skipif(M.find_browser() is None, reason="no headless Chromium on this PC")
def test_the_frames_redraw_identically_from_the_grids():
    drawn = M.render_badge("mint")
    for s in M.SIZES:
        assert drawn[s] == (FRAMES / f"{s}.png").read_bytes(), s
    night = M.render_badge("night")
    g = json.loads(M.GRIDS.read_text(encoding="utf-8"))
    acc = next(d for d in g["icons"]["48"]["dots"] if d["role"] == "acc")
    (_w, _h), px = _rgba(night[48])
    i = (int(acc["y"]) * 48 + int(acc["x"])) * 4
    assert "#%02X%02X%02X" % tuple(px[i:i + 3]) == g["colourSets"]["night"]["acc"]   # recoloured by role


def test_the_installer_uses_the_badge_and_the_splash_images():
    from PIL import Image

    iss = (PKG / "installer.iss").read_text(encoding="utf-8")
    assert "SetupIconFile=yuki.ico" in iss
    def files(key: str) -> list[Path]:
        line = next(ln for ln in iss.splitlines() if ln.startswith(key + "="))
        return [PKG / p.strip() for p in line.split("=", 1)[1].split(",")]
    large, small = files("WizardImageFile"), files("WizardSmallImageFile")
    assert len(large) == len(M.WIZARD) and len(small) == len(M.WIZARD_SMALL)
    for p, (w, h) in zip(large, M.WIZARD):
        im = Image.open(p)
        assert im.format == "BMP" and im.mode == "RGB" and im.size == (w, h)
        assert abs(w / h - 164 / 314) < 0.01                       # Inno keeps 164:314
    for p, s in zip(small, M.WIZARD_SMALL):
        im = Image.open(p)
        assert im.format == "BMP" and im.size == (s, s)
    # the large image is the splash: a lit planet in the upper part, the name below it, a dark field around
    im = Image.open(large[-1]).convert("RGB")
    w, h = im.size
    lum = lambda box: sum(im.crop(box).tobytes()) / ((box[2] - box[0]) * (box[3] - box[1]))  # noqa: E731
    assert lum((w // 3, int(h * 0.30), 2 * w // 3, int(h * 0.50))) > 4 * lum((0, 0, w, int(h * 0.08)))


@pytest.mark.skipif(M.find_browser() is None, reason="no headless Chromium on this PC")
def test_the_installer_images_redraw_identically(tmp_path):
    for p in M.installer_images(tmp_path):
        assert p.read_bytes() == (M.INSTALLER_DIR / p.name).read_bytes(), p.name


def test_the_build_script_makes_the_icon_on_every_build():
    ps1 = (PKG / "build.ps1").read_text(encoding="utf-8")
    assert '& $Python (Join-Path $Pkg "make_icon.py")' in ps1
