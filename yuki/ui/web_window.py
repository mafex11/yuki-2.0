"""The Yuki window as HTML in WebView2 (the E2 spike; E3/E4 grow it into the real window).

``python -m yuki.ui.web_window`` opens ``yuki/ui/web/index.html`` in a :class:`~yuki.ui.webview2.WebView2Host`
and feeds it over the message channel: the page says ``ready``, the window answers with a ``snapshot`` (for now
the fixture of invented data in ``yuki/ui/web/fixtures/home.json``; E4 sends the memory pipe's data and the
encrypted window snapshot instead), the page renders Home and reports ``painted`` with its timings.

The process imports no Qt, no memory and no agent: Python, ctypes and the WebView2 runtime only, so it costs
nothing while closed and as little as it can while open. Options for the spike's measurements (all optional):

* ``--offscreen``: a frameless tool window beyond the left edge of every monitor that never activates (never
  takes focus, never seen);
* ``--profile DIR``: WebView2's user data folder (default ``%LOCALAPPDATA%\\Yuki\\webview``);
* ``--browser-args=...``: Chromium switches (default :data:`DEFAULT_BROWSER_ARGS`, ``--disable-gpu``; an empty
  value runs with none, i.e. a separate GPU process);
* ``--t0 EPOCH``: when the "click" happened (the launcher's spawn time), for click-to-first-paint;
* ``--metrics FILE``: write the stage timings as JSON once the page has painted;
* ``--shot FILE``: a PNG of the page (our own content, never the screen) once it has painted;
* ``--exit-after S``: close S seconds after the page painted.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from yuki.ui.webview2 import APP_HOST, FONT_HOST, HostOptions, WebView2Host

def _ui_dir() -> Path:
    """``yuki/ui``: next to this module in the source tree and in the PyInstaller folder alike."""
    here = Path(__file__).resolve().parent
    if (here / "web" / "index.html").exists():
        return here
    return Path(getattr(sys, "_MEIPASS", "") or here) / "yuki" / "ui"


UI_DIR = _ui_dir()
WEB_DIR = UI_DIR / "web"
FONTS_DIR = UI_DIR / "fonts"
FIXTURE = WEB_DIR / "fixtures" / "home.json"
START_URL = f"https://{APP_HOST}/index.html"
#: Chromium switches the window runs with unless ``--browser-args`` says otherwise. ``--disable-gpu`` won the E2
#: spike (2026-10-03, docs/research/revamp-2026-10-02/25-build-E1-E2-E5.md §2): 134 MB private idle against 203 MB
#: with the separate GPU process, the same first paint (~1.0 s), the same CPU with the live field animating, the page
#: renders the same, and the window never opens a device on the GPU a game is using. Flip it here if E16 finds
#: scrolling or animation jank in software compositing (``--in-process-gpu`` measured 171 MB).
DEFAULT_BROWSER_ARGS = "--disable-gpu"


def default_profile() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki" / "webview"


def load_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    """The fixture the page renders: a ``yuki-home/1`` object (invented data)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != "yuki-home/1":
        raise ValueError(f"{path} is not a yuki-home/1 fixture")
    return data


def appearance() -> dict[str, Any]:
    """The accent and motion the page follows (``ui.json``, read only)."""
    try:
        from yuki.ui import ui_settings

        s = ui_settings.current()
        return {"accent": s.accent, "reduce_motion": bool(s.reduce_motion)}
    except Exception:  # noqa: BLE001 - a broken settings file reads as defaults
        return {"accent": "green", "reduce_motion": False}


class WindowSession:
    """One window's run: answers the page, collects the timings (UI thread only, except :meth:`write`)."""

    def __init__(self, fixture: dict[str, Any], *, t0: float | None, metrics_path: Path | None,
                 shot: Path | None, exit_after: float | None, memory_low_after: float | None = None) -> None:
        self.fixture = fixture
        self.memory_low_after = memory_low_after
        self.metrics: dict[str, Any] = {"t0": t0, "pid": os.getpid(), "frozen": bool(getattr(sys, "frozen", False)),
                                        "python": sys.version.split()[0]}
        self.metrics_path = metrics_path
        self.shot = shot
        self.exit_after = exit_after
        self.host: WebView2Host | None = None
        self.sent = 0
        self.events: list[dict[str, Any]] = []

    def snapshot_message(self) -> dict[str, Any]:
        return {"kind": "snapshot", "page": "home", "data": self.fixture, **appearance()}

    def on_message(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        op = message.get("op")
        if op == "ready" and self.host is not None:
            self.host.post(self.snapshot_message())
            self.sent += 1
            self.host.marks["snapshot_sent"] = time.time()
        elif op == "painted":
            self.on_painted(message)

    def on_painted(self, page: dict[str, Any]) -> None:
        host = self.host
        self.metrics["page"] = {k: v for k, v in page.items() if k != "op"}
        if host is not None:
            self.metrics["marks"] = dict(host.marks)
            self.metrics["browser_pid"] = host.browser_pid
            self.metrics["runtime"] = host.runtime
            fg = ctypes.windll.user32.GetForegroundWindow()
            self.metrics["took_focus"] = bool(fg) and int(fg) == host.hwnd
        self.metrics["derived"] = derived(self.metrics)
        self.write()
        if host is not None and self.shot is not None:
            host.capture(self.shot, lambda ok: self.metrics.__setitem__("shot", ok) or self.write())
        if host is not None and self.exit_after is not None:
            threading.Timer(max(0.0, self.exit_after), host.close).start()
        if host is not None and self.memory_low_after is not None:
            def low() -> None:
                self.metrics["memory_low"] = {"ok": host.set_memory_low(True), "at": time.time()}
                self.write()
            threading.Timer(max(0.0, self.memory_low_after), lambda: host.invoke(low)).start()

    def on_event(self, kind: str, **fields: Any) -> None:
        self.events.append({"kind": kind, **fields})
        self.metrics["events"] = self.events[-20:]

    def write(self) -> None:
        if self.metrics_path is None:
            return
        tmp = self.metrics_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.metrics, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, self.metrics_path)


def derived(m: dict[str, Any]) -> dict[str, float | None]:
    """Seconds from the click (``t0``) to each stage, and the page's own paint times."""
    t0 = m.get("t0")
    marks = m.get("marks") or {}
    page = m.get("page") or {}

    def since(epoch_s: Any) -> float | None:
        return round(float(epoch_s) - float(t0), 4) if t0 and isinstance(epoch_s, (int, float)) and epoch_s else None

    def since_ms(epoch_ms: Any) -> float | None:
        return since(epoch_ms / 1000.0) if isinstance(epoch_ms, (int, float)) and epoch_ms else None

    return {
        "python_started": since(marks.get("main")), "window": since(marks.get("window")),
        "environment": since(marks.get("environment")), "controller": since(marks.get("controller")),
        "navigation_completed": since(marks.get("navigation_completed")),
        "page_ready": since_ms(page.get("t_ready")), "fcp": since_ms(page.get("fcp")),
        "snapshot_rendered": since_ms(page.get("t_snapshot")), "element_painted": since_ms(page.get("t_element")),
        "first_paint": since_ms(page.get("t_element") or page.get("t_painted")),
        "frame_after_data": since_ms(page.get("t_painted")),
    }


def main(argv: list[str] | None = None) -> int:
    t_main = time.time()
    ap = argparse.ArgumentParser(prog="python -m yuki.ui.web_window", description=__doc__.split("\n")[0])
    ap.add_argument("--fixture", type=Path, default=FIXTURE)
    ap.add_argument("--offscreen", action="store_true")
    ap.add_argument("--profile", type=Path, default=None)
    ap.add_argument("--browser-args", default=DEFAULT_BROWSER_ARGS,
                    help=f"Chromium switches (default {DEFAULT_BROWSER_ARGS!r}; an empty value: none)")
    ap.add_argument("--t0", type=float, default=None)
    ap.add_argument("--metrics", type=Path, default=None)
    ap.add_argument("--shot", type=Path, default=None)
    ap.add_argument("--exit-after", type=float, default=None)
    ap.add_argument("--memory-low-after", type=float, default=None,
                    help="set WebView2's memory target to Low S seconds after the paint (the minimised-window lever)")
    ap.add_argument("--dev-tools", action="store_true")
    args = ap.parse_args(argv)
    session = WindowSession(load_fixture(args.fixture), t0=args.t0, metrics_path=args.metrics, shot=args.shot,
                            exit_after=args.exit_after, memory_low_after=args.memory_low_after)
    options = HostOptions(
        folders={APP_HOST: WEB_DIR, FONT_HOST: FONTS_DIR}, start_url=START_URL,
        user_data_dir=args.profile or default_profile(), placement="offscreen" if args.offscreen else "normal",
        browser_args=args.browser_args, dev_tools=args.dev_tools, on_message=session.on_message,
        on_event=session.on_event, marks={"main": t_main})
    host = WebView2Host(options)
    session.host = host
    code = host.run()
    session.metrics["exit_code"] = code
    session.write()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
