"""Offline stand-ins for voice tests: a pretend desktop the brain can look at and "act" on.

Never used by the app. The offline scenario (``scripts/voice_offline.py``) and
the unit tests give the real :class:`~yuki.agent.loop.Agent` this backend, so a
voice request goes all the way through the real brain (real Bedrock calls,
real tool choices) while nothing on the live desktop is read or touched
(ARCHITECTURE.md rule 7). Every action succeeds and says so; the "machine"
is a few windows and a Downloads folder described in text.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

#: The pretend machine's windows: hwnd, process, title.
WINDOWS = [
    (0x1001, "chrome.exe", "Inbox (3) - sam@example.com - Gmail - Google Chrome"),
    (0x1002, "Spotify.exe", "Spotify Premium"),
    (0x1003, "explorer.exe", "Downloads - File Explorer"),
]

#: What any shell command "prints" on the pretend machine: its only data.
DOWNLOADS = """\
    Directory: C:\\Users\\yukidev\\Downloads

LastWriteTime        Length Name
-------------        ------ ----
2026-09-03  10:12    88412 Invoice_Acme_2026-09-03.pdf      (text: "Acme Cloud ... Total due: Rs 12,400")
2026-09-11  18:40    91230 Invoice_Figma_2026-09-11.pdf     (text: "Figma Professional ... Total: Rs 4,300")
2026-09-19  09:05    77801 Invoice_Zoho_2026-09-19.pdf      (text: "Zoho Books ... Amount payable: Rs 31,500")
2026-08-28  14:22   120114 Invoice_Acme_2026-08-28.pdf      (text: "Acme Cloud ... Total due: Rs 11,900")
2026-09-20  21:10  2201873 holiday-photos.zip
"""


@dataclass
class StubResult:
    """An :class:`~yuki.actions.ActionResult`-shaped success."""

    ok: bool = True
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0


class StubDesktop:
    """A backend for :class:`~yuki.agent.tools.Dispatcher` that never touches the real machine.

    Args:
        action_delay_s: How long each "action" takes (a real one takes a while).
    """

    def __init__(self, action_delay_s: float = 0.4) -> None:
        self.delay = action_delay_s
        self.calls: list[tuple[str, tuple, dict]] = []

    def _act(self, name: str, summary: str, *args: Any, **kwargs: Any) -> StubResult:
        self.calls.append((name, args, kwargs))
        if self.delay:
            time.sleep(self.delay)  # a stand-in for a real action's duration, not a settle wait
        return StubResult(summary=summary)

    # -- perception -----------------------------------------------------------

    def get_desktop_overview(self) -> dict[str, Any]:
        return {
            "windows": [{"hwnd": h, "process_name": p, "title": t, "is_foreground": i == 0, "is_minimized": False,
                         "pid": 100 + i, "bounds": (0, 0, 1920, 1040)} for i, (h, p, t) in enumerate(WINDOWS)],
            "foreground_hwnd": WINDOWS[0][0], "cursor": (960, 540), "screen_size": (1920, 1080),
            "captured_at": time.time(), "monitors": [], "virtual_bounds": (0, 0, 1920, 1080),
        }

    def format_overview(self, overview: dict[str, Any]) -> str:
        lines = [f"Open windows ({len(overview['windows'])}), foreground first:"]
        for w in overview["windows"]:
            mark = " [foreground]" if w["is_foreground"] else ""
            lines.append(f"- [{w['hwnd']}] {w['process_name']} \"{w['title']}\"{mark}")
        return "\n".join(lines)

    def get_window_tree(self, hwnd: int, **_: Any) -> dict[str, Any]:
        title = next((t for h, _p, t in WINDOWS if h == hwnd), "")
        return {"hwnd": hwnd, "title": title, "process_name": "", "elements": [], "truncated": False,
                "status": "ok", "note": "", "captured_at": time.time(), "elapsed_ms": 5.0, "passes": 1,
                "child_windows": 0, "first_pass_elements": 0}

    def format_window_tree(self, tree: dict[str, Any]) -> str:
        return f"window {tree['hwnd']} \"{tree['title']}\": (a test machine: no element tree; use the shell)"

    def system_facts(self) -> dict[str, Any]:
        return {"time": time.strftime("%Y-%m-%d %H:%M"), "cpu_percent": 7.0, "memory_used_gb": 9.1}

    def activity_facts(self) -> dict[str, Any]:
        return {"media": [], "microphone": [], "camera": [],
                "foreground": {"hwnd": WINDOWS[0][0], "process_name": WINDOWS[0][1], "title": WINDOWS[0][2]}}

    # -- actions ----------------------------------------------------------------

    def run_powershell(self, command: str, *, timeout_s: float = 20.0) -> StubResult:
        del timeout_s
        result = self._act("run_powershell", "exit code 0", command)
        result.details = {"stdout": DOWNLOADS, "stderr": "", "exit_code": 0}
        return result

    def launch_app(self, query: str, **kwargs: Any) -> StubResult:
        return self._act("launch_app", f"launched {query}; its window is in front", query, **kwargs)

    def focus_window(self, hwnd: int, **kwargs: Any) -> StubResult:
        return self._act("focus_window", f"window {hwnd} is in front", hwnd, **kwargs)

    def open_url(self, url: str, **kwargs: Any) -> StubResult:
        return self._act("open_url", f"opened {url} in the default browser", url, **kwargs)

    def click(self, x: int, y: int, **kwargs: Any) -> StubResult:
        return self._act("click", f"clicked at ({x}, {y})", x, y, **kwargs)

    def type_text(self, text: str, **kwargs: Any) -> StubResult:
        return self._act("type_text", f"typed {len(text)} chars", text, **kwargs)

    def hotkey(self, *keys: str, **kwargs: Any) -> StubResult:
        return self._act("hotkey", f"pressed {'+'.join(keys)}", *keys, **kwargs)

    def press(self, key: str, **kwargs: Any) -> StubResult:
        return self._act("press", f"pressed {key}", key, **kwargs)

    def scroll(self, x: int, y: int, **kwargs: Any) -> StubResult:
        return self._act("scroll", f"scrolled at ({x}, {y})", x, y, **kwargs)
