"""One token source: ``yuki/ui/web/tokens.json`` (the Lattice design) -> ``tokens.css`` and ``yuki/ui/tokens.py``.

The Lattice design (docs/ui-mockups/revamp-2026-10/3-lattice/, gitignored) ships ``tokens.json`` as the single
source for the WebView2 window and the native Qt surfaces. This generator writes both sides from it:

* ``yuki/ui/web/tokens.css``: a ``:root`` block with the CSS custom properties the Lattice pages use
  (``--bg``, ``--raise-2``, ``--live``, ``--r-3``, ``--s-4``, ``--t-md``, ``--d-island-morph`` ...), the three
  bundled faces as ``@font-face`` rules, and one ``:root[data-accent="..."]`` block per non-default accent (the
  page sets ``data-accent`` from ``ui.json``, live over the pipe);
* ``yuki/ui/tokens.py``: the same values as plain Python dicts, read by :mod:`yuki.ui.window.theme` (and through
  it every native surface) and by the onboarding wizard. No Qt import, so anything can read it.

Run ``python -m yuki.ui.tokens_build`` after editing the JSON; ``--check`` exits 1 when either generated file is
out of date (the tests run it). Both outputs are committed, so nothing generates at run time.
"""

from __future__ import annotations

import hashlib
import json
import pprint
import sys
from pathlib import Path
from typing import Any

UI_DIR = Path(__file__).resolve().parent
WEB_DIR = UI_DIR / "web"
SOURCE = WEB_DIR / "tokens.json"
CSS_OUT = WEB_DIR / "tokens.css"
PY_OUT = UI_DIR / "tokens.py"

#: The virtual host the window maps ``yuki/ui/fonts`` to (``yuki.ui.webview2``); the relative URL after it is the
#: same folder seen from ``yuki/ui/web`` (a design preview opened from disk, no host).
FONT_HOST = "https://fonts.yuki.example"

#: Fallback stacks after each bundled family (Nirmala UI: Devanagari; the rest as in the Lattice ``style.css``).
FAMILY_FALLBACKS = {
    "display": "'JetBrains Mono', monospace",
    "ui": "'Segoe UI', 'Nirmala UI', system-ui, sans-serif",
    "mono": "ui-monospace, 'Cascadia Mono', Consolas, monospace",
}
#: ``@font-face`` ranges per family (variable files), as in the Lattice ``style.css``.
FACE_RANGES = {
    "Doto": "font-weight: 100 900;",
    "Instrument Sans": "font-weight: 400 700; font-stretch: 75% 100%;",
    "JetBrains Mono": "font-weight: 100 800;",
}


def kebab(name: str) -> str:
    """``raise2`` -> ``raise-2``, ``liveInk`` -> ``live-ink``, ``islandMorph`` -> ``island-morph`` (token keys are
    our own camelCase names: a fixed format, converted character by character)."""
    out: list[str] = []
    prev = ""
    for ch in name:
        if ch.isupper():
            out.append("-" + ch.lower())
        elif ch.isdigit() and prev and not prev.isdigit():
            out.append("-" + ch)
        else:
            out.append(ch)
        prev = ch
    return "".join(out).lstrip("-")


def _step(key: str, letter: str) -> str:
    """A scale step's name: ``r3`` -> ``3`` (radius), ``s4`` -> ``4`` (space); any other key in kebab case."""
    if key[:1] == letter and key[1:].isdigit():
        return key[1:]
    return kebab(key)


def load(path: Path = SOURCE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def source_digest(path: Path = SOURCE) -> str:
    """SHA-256 of the JSON's text with line endings normalised (a CRLF checkout reads the same)."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def accent_choices(tokens: dict[str, Any]) -> tuple[str, dict[str, dict[str, str]]]:
    """``(default name, {name: {"label", "live", "liveInk", "liveDim"}})``."""
    acc = tokens.get("accent") or {}
    choices = {str(k): dict(v) for k, v in (acc.get("choices") or {}).items()}
    if not choices:   # a plain Lattice file: its one live colour
        c = tokens["color"]
        choices = {"green": {"label": "Mint", "live": c["live"], "liveInk": c["liveInk"], "liveDim": c["liveDim"]}}
    default = str(acc.get("default") or next(iter(choices)))
    return default, choices


# -- CSS ------------------------------------------------------------------------------------------------


def _px(v: Any) -> str:
    return f"{v}px"


def css_variables(tokens: dict[str, Any]) -> list[tuple[str, str]]:
    """Every ``(--name, value)`` of the ``:root`` block, in a stable order."""
    out: list[tuple[str, str]] = []
    default, choices = accent_choices(tokens)
    live = choices[default]
    color = dict(tokens["color"])
    color.update({"live": live["live"], "liveInk": live["liveInk"], "liveDim": live["liveDim"]})
    for key, value in color.items():
        if isinstance(value, str) and key != "rules":
            out.append((f"--{kebab(key)}", value))
    out.append(("--accent", color["lit"]))   # primary keys are lit (bone), never coloured
    for key, value in tokens["radius"].items():
        out.append((f"--r-{_step(key, 'r')}", _px(value)))
    for key, value in tokens["space"].items():
        out.append((f"--s-{_step(key, 's')}", _px(value)))
    t = tokens["type"]
    fam = t["family"]
    for role in ("display", "ui", "mono"):
        out.append((f"--f-{role}", f"'{fam[role]}', {FAMILY_FALLBACKS[role]}"))
    for key, value in t["scale"].items():
        out.append((f"--t-{kebab(key)}", _px(value)))
    for group in ("island", "card"):
        for key, value in t[group].items():
            out.append((f"--t-{group}-{kebab(key)}", _px(value)))
    m = tokens["motion"]
    for key, value in m["durationMs"].items():
        out.append((f"--d-{kebab(key)}", f"{value}ms"))
    out.append(("--ease", m["ease"]))
    out.append(("--fps-cap", str(m["fpsCap"])))
    out.append(("--dpr-cap", str(m["dprCap"])))
    f = tokens["field"]
    out.append(("--field-pitch", _px(f["window"]["pitchPx"])))
    out.append(("--halo-w", _px(f["halo"]["widthPx"])))
    out.append(("--halo-h", _px(f["halo"]["heightPx"])))
    out.append(("--halo-pitch", _px(f["halo"]["pitchPx"])))
    for key, value in tokens["face"]["pitchPx"].items():
        out.append((f"--face-pitch-{kebab(key)}", _px(value)))
    isl = tokens["island"]
    out += [("--island-h", _px(isl["heightPx"])), ("--island-mini-h", _px(isl["miniHeightPx"])),
            ("--island-min-w", _px(isl["minWidthPx"])), ("--island-max-w", _px(isl["maxWidthPx"])),
            ("--island-top", _px(isl["topPx"])), ("--island-caption-indent", _px(isl["captionIndentPx"]))]
    card = tokens["card"]
    out += [("--card-w", _px(card["widthPx"])), ("--card-right", _px(card["rightPx"])),
            ("--card-bottom", _px(card["bottomPx"])),
            ("--card-pad", " ".join(_px(v) for v in card["paddingPx"])), ("--crop-mark", _px(card["cropMarkPx"]))]
    return out


def render_css(tokens: dict[str, Any], digest: str) -> str:
    lines = ["/* Lattice design tokens. GENERATED by `python -m yuki.ui.tokens_build` from tokens.json",
             f"   (sha256 {digest[:16]}). Edit tokens.json and run the generator; never edit this file. */", ""]
    for family, file in tokens["type"]["files"].items():
        name = Path(file).name
        lines.append(f"@font-face {{ font-family: '{family}'; src: url({FONT_HOST}/{name}) format('truetype'), "
                     f"url(../fonts/{name}) format('truetype'); {FACE_RANGES.get(family, '')} font-display: block; }}")
    lines += ["", ":root {"]
    lines += [f"  {name}: {value};" for name, value in css_variables(tokens)]
    lines.append("}")
    default, choices = accent_choices(tokens)
    for name, c in choices.items():
        if name == default:
            continue
        lines += ["", f':root[data-accent="{name}"] {{',
                  f"  --live: {c['live']};", f"  --live-ink: {c['liveInk']};", f"  --live-dim: {c['liveDim']};", "}"]
    return "\n".join(lines) + "\n"


# -- Python ---------------------------------------------------------------------------------------------


def render_python(tokens: dict[str, Any], digest: str) -> str:
    default, choices = accent_choices(tokens)
    files = {family: Path(file).name for family, file in tokens["type"]["files"].items()}
    groups: list[tuple[str, str, Any]] = [
        ("COLOR", "Colours: sRGB hex, or ``rgba(...)`` strings where the design carries alpha.", tokens["color"]),
        ("TYPE", "Families, Doto axes, the type scale and surface sizes (px at 1x).", tokens["type"]),
        ("RADIUS", "Corner radii (px).", tokens["radius"]),
        ("SPACE", "The spacing scale (px).", tokens["space"]),
        ("MOTION", "Durations (ms), the ease, the 30 fps cap, the DPR cap, reduced-motion behaviour.",
         tokens["motion"]),
        ("FIELD", "The dot field and the halo under the island.", tokens["field"]),
        ("FACE", "The 17x7 dot-matrix face.", tokens["face"]),
        ("ISLAND", "The island's geometry (px).", tokens["island"]),
        ("CARD", "Corner cards' geometry (px).", tokens["card"]),
        ("ACCENTS", "Settings > Accent: the live colour per choice (``ui.json`` ``accent``).", choices),
        ("FONT_FILES", "Bundled font file per family (in ``yuki/ui/fonts``).", files),
    ]
    out = ['"""Lattice design tokens for the native surfaces and anything else in Python.',
           "",
           "GENERATED by ``python -m yuki.ui.tokens_build`` from ``yuki/ui/web/tokens.json`` (the one token source,",
           "shared with the window's ``tokens.css``). Edit the JSON and run the generator; never edit this file.",
           '"""', "", "from __future__ import annotations", "", "from typing import Any", "",
           "#: SHA-256 of the tokens.json this was generated from (line endings normalised).",
           f'SOURCE_SHA256 = "{digest}"', "",
           "#: The accent a fresh install uses (``ui.json`` ``accent``).", f'DEFAULT_ACCENT = "{default}"', ""]
    for name, doc, value in groups:
        out.append(f"#: {doc}")
        body = pprint.pformat(value, indent=1, width=112, sort_dicts=False)
        out.append(f"{name}: dict[str, Any] = {body}")
        out.append("")
    return "\n".join(out)


# -- entry ----------------------------------------------------------------------------------------------


def build(source: Path = SOURCE) -> dict[Path, str]:
    """The generated files' texts, by path."""
    tokens = load(source)
    digest = source_digest(source)
    return {CSS_OUT: render_css(tokens, digest), PY_OUT: render_python(tokens, digest)}


def stale(source: Path = SOURCE) -> list[Path]:
    """Generated files that differ from what the JSON gives now (line endings ignored)."""
    out = []
    for path, text in build(source).items():
        try:
            current = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        except OSError:
            current = None
        if current != text:
            out.append(path)
    return out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--check" in args:
        bad = stale()
        for path in bad:
            print(f"out of date: {path} (run python -m yuki.ui.tokens_build)")
        return 1 if bad else 0
    for path, text in build().items():
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
