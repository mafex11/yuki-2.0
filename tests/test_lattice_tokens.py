"""One token source (Phase E1): yuki/ui/web/tokens.json -> tokens.css (the HTML window) and yuki.ui.tokens (the Qt
theme and every native surface). The generated files are current, carry every token, and the native side really
reads them (no palette left hard-coded). Offscreen Qt only."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from yuki.ui import tokens, tokens_build  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    app = QCoreApplication.instance()
    if app is not None and not isinstance(app, QApplication):
        pytest.skip("a GUI-less QCoreApplication is already running in this process")
    return app or QApplication([])


def test_generated_files_are_current():
    assert tokens_build.stale() == [], "run: python -m yuki.ui.tokens_build"
    assert tokens.SOURCE_SHA256 == tokens_build.source_digest()


def test_python_tokens_mirror_the_json():
    data = json.loads(tokens_build.SOURCE.read_text(encoding="utf-8"))
    assert tokens.COLOR == data["color"] and tokens.RADIUS == data["radius"] and tokens.SPACE == data["space"]
    assert tokens.MOTION == data["motion"] and tokens.FIELD == data["field"] and tokens.FACE == data["face"]
    # the Lattice palette the brief names (20 Oct: in the website's umber and ember)
    c = tokens.COLOR
    assert (c["bg"], c["dot"], c["text"], c["live"]) == ("#090705", "#292521", "#F2EFE9", "#FF6A1A")
    assert tokens.DEFAULT_ACCENT == "orange" and tokens.ACCENTS["orange"]["live"] == c["live"]
    assert set(tokens.ACCENTS) == {"green", "orange"}


def test_css_carries_every_colour_and_scale_and_the_accents():
    css = tokens_build.CSS_OUT.read_text(encoding="utf-8")
    for key, value in tokens.COLOR.items():
        if isinstance(value, str) and key != "rules":
            assert f"--{tokens_build.kebab(key)}: {value};" in css, key
    for needle in ("--raise-2: #1A1613;", "--r-3: 14px;", "--s-4: 16px;", "--t-md: 14px;", "--t-4xl: 96px;",
                   "--d-island-morph: 300ms;", "--ease: cubic-bezier(.2,.7,.1,1);", "--field-pitch: 12px;",
                   "--halo-w: 560px;", "--face-pitch-island: 8px;", "--island-h: 74px;", "--card-w: 416px;",
                   "--f-ui: 'Instrument Sans'", "--accent: #F2EFE9;"):
        assert needle in css, needle
    assert ':root[data-accent="green"] {' in css and "--live: #6FEBC4;" in css
    assert ':root[data-accent="orange"]' not in css                # the default lives in :root itself


def test_css_fonts_point_at_the_bundled_files():
    css = tokens_build.CSS_OUT.read_text(encoding="utf-8")
    fonts = ROOT / "yuki" / "ui" / "fonts"
    for family, file in tokens.FONT_FILES.items():
        assert f"font-family: '{family}'" in css
        assert f"url({tokens_build.FONT_HOST}/{file})" in css and f"url(../fonts/{file})" in css
        assert (fonts / file).exists()
        licence = fonts / file.replace(".ttf", "-OFL.txt")
        assert "SIL Open Font License" in licence.read_text(encoding="utf-8")


def test_kebab_names():
    k = tokens_build.kebab
    assert (k("raise2"), k("liveInk"), k("dotFace"), k("islandMorph"), k("text"), k("2xl"), k("line2Mono")) == \
        ("raise-2", "live-ink", "dot-face", "island-morph", "text", "2xl", "line-2-mono")


def test_the_qt_theme_reads_the_tokens(qapp):
    from yuki.ui.window import theme as th

    t = th.theme(accent="green")
    assert t.bg.name() == "#090705" and t.card.name() == "#090705" and t.border.name() == "#211d1a"
    assert t.text.name() == "#f2efe9" and t.text_2.name() == "#b4b0a8" and t.text_3.name() == "#8a8780"
    assert t.accent.name() == "#6febc4" and t.ring.name() == "#6febc4" and t.text_on_accent.name() == "#062419"
    assert t.led_off.name() == "#292521"                           # an unlit LED is a resting dot
    assert t.level("popover").border.name() == "#2f2a26"           # floating surfaces: the field with a border
    o = th.theme(accent="orange")
    assert o.accent.name() == "#ff6a1a" and o.text_on_accent.name() == "#1f0d03"
    assert th.ACCENTS == {k: v["live"] for k, v in tokens.ACCENTS.items()}
    assert th.ACCENT_LABELS == {"orange": "Ember", "green": "Mint"}


def test_the_accent_setting_keeps_working(tmp_path):
    from yuki.ui import ui_settings

    assert ui_settings.ACCENT_NAMES == tuple(tokens.ACCENTS)
    path = tmp_path / "ui.json"
    path.write_text(json.dumps({"accent": "green"}), encoding="utf-8")   # what existing installs hold
    assert ui_settings.load(path).accent == "orange"                  # (20 Oct) the old default saved: ember, once
    ui_settings.update(path, accent="green")
    assert ui_settings.load(path).accent == "green"                   # a Mint chosen after that stays
    ui_settings.update(path, accent="teal")
    assert ui_settings.load(path).accent == "orange"


def test_native_surfaces_and_the_wizard_paint_from_the_tokens(qapp):
    from yuki.onboarding import dialog
    from yuki.ui import surface

    assert surface.CARD_BG.name() == tokens.COLOR["bg"].lower()
    assert surface.CARD_BORDER.name() == tokens.COLOR["surfaceBorder"].lower()
    assert dialog.BG == tokens.COLOR["bg"] and dialog.TEXT == tokens.COLOR["text"]
    assert dialog.ACCENT_LED == tokens.ACCENTS[tokens.DEFAULT_ACCENT]["live"] and dialog.UI_FAMILY == "Instrument Sans"


def test_no_palette_left_hard_coded_in_the_native_surfaces():
    """The round-2 Horizon colours were copied into four places (08 §6 point 2); none may come back."""
    horizon = ("#07080A", "#0C0E11", "#E4E6E9", "#A4AAB3", "#6B717B", "#748F42", "#E4571C", "#13161B", "#1B1F26",
               "#262B33", "#16191F", "#393E47", "#E5736B")
    files = [ROOT / "yuki" / "ui" / name for name in ("surface.py", "overlay.py", "status.py", "nudges.py",
                                                       "calls.py", "memory.py", "ui_settings.py")]
    files += [ROOT / "yuki" / "ui" / "window" / "theme.py", ROOT / "yuki" / "onboarding" / "dialog.py"]
    for path in files:
        text = path.read_text(encoding="utf-8").upper()
        for hexa in horizon:
            assert hexa not in text, f"{hexa} in {path.name}"


def test_bundled_fonts_load_and_mono_waits_until_asked(qapp):
    from PySide6.QtGui import QFontInfo

    from yuki.ui import fonts
    from yuki.ui.window import theme as th

    assert fonts.load_fonts() and fonts.ui_available() and fonts.dot_available()
    assert th.family() == "Instrument Sans"
    body = th.variable_font(14)
    assert QFontInfo(body).family() == "Instrument Sans" and "Nirmala UI" in body.families()
    mono = th.mono_font(12)
    assert fonts.mono_available() and QFontInfo(mono).family() == "JetBrains Mono"
