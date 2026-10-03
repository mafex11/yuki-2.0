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


class _FakeSessionVolume:
    """An app session's ISimpleAudioVolume, in memory (tests only)."""

    def __init__(self, mixer: "MixerStub", app: str) -> None:
        self.mixer, self.app = mixer, app

    def SetMasterVolume(self, level: float, _ctx: Any) -> None:  # noqa: N802 - the COM names
        self.mixer.levels[self.app] = round(float(level) * 100.0, 1)

    def GetMasterVolume(self) -> float:  # noqa: N802
        return self.mixer.levels[self.app] / 100.0

    def SetMute(self, mute: bool, _ctx: Any) -> None:  # noqa: N802
        self.mixer.muted[self.app] = bool(mute)

    def GetMute(self) -> bool:  # noqa: N802
        return self.mixer.muted.get(self.app, False)


class MixerStub:
    """A pretend Windows volume mixer and system volume: the per-app volume action runs its real logic on
    these in-memory sessions, so tests never reach the real mixer (rule 7)."""

    def __init__(self, levels: dict[str, float] | None = None, playing: set[str] | None = None,
                 system: float = 45.0) -> None:
        self.levels = dict(levels or {"chrome.exe": 100.0, "Spotify.exe": 40.0})
        self.muted: dict[str, bool] = {}
        self.playing = set(playing if playing is not None else {"chrome.exe"})
        self.system = system
        self.changes: list[tuple[str, float | None, bool | None]] = []

    def master_volume(self) -> dict[str, Any]:
        return {"level": float(self.system), "muted": False}

    def _sessions(self) -> list[Any]:
        from yuki.actions.app_volume import AppSession

        return [AppSession(app=a, pid=100 + i, level=lv, muted=self.muted.get(a, False),
                           state="active" if a in self.playing else "inactive", device="Speakers (stub)",
                           volume=_FakeSessionVolume(self, a)) for i, (a, lv) in enumerate(self.levels.items())]

    def audio_apps(self) -> list[dict[str, Any]]:
        return [{"app": s.app, "level": s.level, "muted": s.muted, "playing": s.state == "active", "sessions": 1}
                for s in sorted(self._sessions(), key=lambda s: (s.state != "active", s.app))]

    def app_volume(self, app: str, *, level: float | None = None, change: float | None = None,
                   mute: bool | None = None) -> Any:
        from yuki.actions.app_volume import set_app_volume

        result = set_app_volume(app, level=level, change=change, mute=mute, _sessions=self._sessions,
                                _system=self.master_volume)
        self.changes.append((app, result.details.get("after"), mute))
        return result


@dataclass
class StubResult:
    """An :class:`~yuki.actions.ActionResult`-shaped success."""

    ok: bool = True
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0


#: How long the thinker's real tool calls took on this PC (ms), from the live worker logs of 27-29 Sept 2026
#: (``tool_result.elapsed_ms``, 80 calls; ``launch_app`` from the Warp and Chrome launches measured live, 6.3 s and 1.8 s).
#: With :attr:`StubDesktop.measured_steps` a stub action or window read takes one of these, so the thinker's step lines
#: meet the pauses a real step makes (v3.14b); otherwise every action takes ``action_delay_s``, as before.
MEASURED_STEP_MS: dict[str, tuple[int, ...]] = {
    "click": (0, 79, 131, 146, 164, 229, 229, 263, 287, 297, 311, 324, 384, 505, 517, 830, 844, 857, 883, 7162),
    "look_at_window": (195, 501, 621, 1124),
    "open_url": (2120, 2492, 4159),
    "focus_window": (21, 21, 871),
    "run_powershell": (1999, 4836, 6983),
    "press": (832, 841),
    "hotkey": (229, 825),
    "tab": (546, 2124),
    "type_text": (958, 1390),
    "launch_app": (1800, 6300),
}
#: Stub calls measured under another tool's name (the same kind of work on the real machine).
_MEASURED_AS = {"select_tab": "tab", "window_action": "focus_window", "scroll": "hotkey", "hover": "click",
                "get_window_tree": "look_at_window", "read_page": "look_at_window"}


class StubDesktop:
    """A backend for :class:`~yuki.agent.tools.Dispatcher` that never touches the real machine.

    Args:
        action_delay_s: How long each "action" takes (a real one takes a while).
    """

    #: Actions and window reads take the durations measured on the real machine (:data:`MEASURED_STEP_MS`).
    measured_steps: bool = False

    def __init__(self, action_delay_s: float = 0.4) -> None:
        self.delay = action_delay_s
        self.calls: list[tuple[str, tuple, dict]] = []
        import random

        self._rng = random.Random(929)   # the same draws every run: replays stay comparable

    def _step_s(self, name: str) -> float:
        if not self.measured_steps:
            return self.delay
        samples = MEASURED_STEP_MS.get(_MEASURED_AS.get(name, name))
        return self._rng.choice(samples) / 1000.0 if samples else self.delay

    def _perceive(self, name: str) -> None:
        """A window read's own time, when steps are measured (a stub read is otherwise instant)."""
        if self.measured_steps:
            time.sleep(self._step_s(name))

    def _act(self, name: str, summary: str, *args: Any, **kwargs: Any) -> StubResult:
        self.calls.append((name, args, kwargs))
        wait = self._step_s(name)
        if wait:
            time.sleep(wait)  # a stand-in for a real action's duration, not a settle wait
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
        self._perceive("get_window_tree")
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

    def window_action(self, hwnd: int, action: str) -> StubResult:
        return self._act("window_action", f"window {hwnd} is now {action}d", hwnd, action)

    def hover(self, x: int, y: int, **kwargs: Any) -> StubResult:
        return self._act("hover", f"the pointer is at ({x},{y}); nothing new appeared near it", x, y, **kwargs)

    def app_volume(self, app: str, **kwargs: Any) -> StubResult:
        result = self._act("app_volume", f"{app}'s own volume set (stub)", app, **kwargs)
        result.details = {"verified": True}
        return result

    def audio_apps(self) -> list[dict[str, Any]]:
        return []


class BrowserDesk(StubDesktop):
    """A pretend desktop for the v3.3 hands cases: Chrome with tabs in front, Discord, Spotify (tests only).

    Replays the live test of 2026-09-25 18:54-18:57 without touching the machine: the
    cursor sits over Chrome's tab bar; the tabs are the video, GitHub (showing) and an
    older YouTube tab; a page scrolls only when the scroll reaches it; a new page gets
    its title a moment after it is at its address. ``era`` picks the scroll it behaves
    like: ``"before"`` the v3.2 one (the wheel at the point, success whatever moved),
    ``"after"`` the v3.3 one (the page's own scroll position, read back). Every action
    is recorded (``calls``) and, with ``timeline``, put on it as ``action``.
    """

    CHROME, DISCORD, SPOTIFY = 0x3003, 0x2002, 0x1002
    TAB_BAR_BOTTOM = 40
    TITLE_DELAY_S = 1.2

    def __init__(self, era: str = "after", timeline: Any = None, action_delay_s: float = 0.1) -> None:
        super().__init__(action_delay_s=action_delay_s)
        self.era = era
        self.timeline = timeline
        self.tabs: list[dict[str, Any]] = [
            {"name": "Getting the most out of Opus 5.5 - YouTube", "url": "https://www.youtube.com/watch?v=opus55",
             "scroll": 0.0, "titled_at": 0.0},
            {"name": "GitHub - yukidev/yuki", "url": "https://github.com/yukidev/yuki", "scroll": 0.0, "titled_at": 0.0},
            {"name": "YouTube - High memory usage - 817 MB", "url": "https://www.youtube.com/", "scroll": 0.0,
             "titled_at": 0.0},
        ]
        self.selected = 1
        self.front = self.CHROME
        self.state = {self.CHROME: "normal", self.DISCORD: "normal", self.SPOTIFY: "normal"}
        self.cursor = (1135, 20)   # over the tab bar, as live
        self.playing = True
        self.volume = 60.0

    # -- the pretend state ----------------------------------------------------

    def _act(self, name: str, summary: str, *args: Any, **kwargs: Any) -> StubResult:
        if self.timeline is not None:
            self.timeline.add("action", name=name, args=[str(a)[:80] for a in args],
                              kwargs={k: str(v)[:40] for k, v in kwargs.items()})
        return super()._act(name, summary, *args, **kwargs)

    #: A text field that has the keyboard in the window in front (v3.14 type hand), ``None`` when none does:
    #: ``{"window", "role", "name", "text"}``. Set by a test or a scenario.
    focus_field: dict[str, Any] | None = None

    def focus_snapshot(self, *, timeout_s: float = 0.6, ready_wait_s: float | None = None) -> Any:
        """What has the keyboard, shaped like :class:`yuki.perception.tree.FocusSnapshot` (only what the hands read)."""
        from types import SimpleNamespace

        field = self.focus_field
        if field is None or int(field.get("window", 0)) != self.front:
            return SimpleNamespace(ok=True, role="", name="", window=self.front, accepts_text=False, is_password=False)
        return SimpleNamespace(ok=True, role=field.get("role", "Edit"), name=field.get("name", ""),
                               window=int(field["window"]), accepts_text=True, is_password=False)

    def type_text(self, text: str, *, expect_hwnd: Any = None, **kwargs: Any) -> StubResult:
        """Typing into the focused field, read back like the real input layer (``readback``)."""
        refused = self._guard(expect_hwnd)
        if refused:
            return StubResult(ok=False, summary=refused)
        result = self._act("type_text", f"typed {len(text)} chars", text, expect_hwnd=expect_hwnd, **kwargs)
        field = self.focus_field
        if field is not None and int(field.get("window", 0)) == self.front:
            field["text"] = str(field.get("text", "")) + text
            result.summary = f"typed {len(text)} chars; field now contains \"{field['text']}\""
            result.details = {"readback": "arrived", "focus_changed": False}
        else:
            result.details = {"readback": "unreadable"}
        return result

    def desk_state(self, t0: float) -> dict[str, Any]:
        """The pretend Chrome now (test reports): the tab showing, every tab's scroll, when titles came."""
        return {"selected": self.selected, "front": self.front,
                "tabs": [{"name": t["name"], "scroll": t["scroll"], "titled_t": round(t["titled_at"] - t0, 2)
                          if t["titled_at"] else None} for t in self.tabs]}

    def tab_name(self, i: int) -> str:
        tab = self.tabs[i]
        return tab["name"] if time.monotonic() >= tab["titled_at"] else tab["url"]

    def title(self, hwnd: int) -> str:
        if hwnd == self.CHROME:
            return f"{self.tab_name(self.selected)} - Google Chrome"
        return {self.DISCORD: "#general | Friends - Discord", self.SPOTIFY: "Spotify Premium"}[hwnd]

    # -- perception -----------------------------------------------------------

    def get_desktop_overview(self) -> dict[str, Any]:
        order = [self.front] + [h for h in (self.CHROME, self.DISCORD, self.SPOTIFY) if h != self.front]
        names = {self.CHROME: "chrome.exe", self.DISCORD: "Discord.exe", self.SPOTIFY: "Spotify.exe"}
        windows = []
        for i, h in enumerate(order):
            if self.state[h] == "closed":
                continue
            minimized = self.state[h] == "minimized"
            windows.append({"hwnd": h, "process_name": names[h], "title": self.title(h),
                            "is_foreground": h == self.front and not minimized, "is_minimized": minimized,
                            "pid": 100 + i, "bounds": (-32000, -32000, -31840, -31972) if minimized
                            else (0, 0, 1920, 1040)})
        return {"windows": windows, "foreground_hwnd": self.front, "cursor": self.cursor,
                "screen_size": (1920, 1080), "captured_at": time.time(), "monitors": [],
                "virtual_bounds": (0, 0, 1920, 1080)}

    def format_overview(self, overview: dict[str, Any]) -> str:
        lines = [f"screen space 1920x1080 @0,0 (one screen) | cursor {tuple(overview['cursor'])}"]
        for w in overview["windows"]:
            mark = (" FOREGROUND" if w["is_foreground"] else "") + (" (minimized)" if w["is_minimized"] else "")
            lines.append(f"[{w['hwnd']}] {w['process_name']} \"{w['title']}\" @0,0 1920x1040{mark}")
        return "\n".join(lines)

    def activity_facts(self) -> dict[str, Any]:
        return {"media": [{"app_id": "Chrome", "status": "playing" if self.playing else "paused",
                           "title": "Getting the most out of Opus 5.5", "artist": "Anthropic", "current": True}],
                "microphone": [], "camera": [],
                "foreground": {"hwnd": self.front, "process_name": "chrome.exe", "title": self.title(self.front)}}

    def master_volume(self) -> dict[str, Any]:
        return {"level": round(self.volume, 1), "muted": False}

    def read_tabs(self, hwnd: int, **_: Any) -> Any:
        from yuki.perception.tabs import Tab, TabStrip

        if int(hwnd) != self.CHROME:
            return TabStrip(hwnd=int(hwnd))
        return TabStrip(hwnd=self.CHROME, tabs=[
            Tab(index=i + 1, name=self.tab_name(i), selected=i == self.selected,
                bounds=(60 + i * 220, 0, 60 + (i + 1) * 220, self.TAB_BAR_BOTTOM)) for i in range(len(self.tabs))],
            nodes=40, ms=3.0)

    def get_window_tree(self, hwnd: int, **_: Any) -> dict[str, Any]:
        self._perceive("get_window_tree")
        elements: list[dict[str, Any]] = [{"id": 0, "role": "Window", "name": self.title(hwnd), "depth": 0,
                                           "bounds": (0, 0, 1920, 1040), "center": (960, 520)}]
        if int(hwnd) == self.CHROME:
            for i in range(len(self.tabs)):
                left = 60 + i * 220
                elements.append({"id": len(elements), "role": "TabItem", "name": self.tab_name(i), "depth": 3,
                                 "bounds": (left, 0, left + 220, self.TAB_BAR_BOTTOM),
                                 "center": (left + 110, self.TAB_BAR_BOTTOM // 2),
                                 "states": ["selected"] if i == self.selected else []})
            tab = self.tabs[self.selected]
            elements.append({"id": len(elements), "role": "Document", "name": self.tab_name(self.selected),
                             "value": tab["url"], "depth": 4, "bounds": (0, 110, 1920, 1040), "center": (960, 575)})
        return {"hwnd": hwnd, "title": self.title(hwnd), "process_name": "chrome.exe", "elements": elements,
                "truncated": False, "status": "ok", "note": "", "captured_at": time.time(), "elapsed_ms": 5.0,
                "passes": 1, "child_windows": 0, "first_pass_elements": len(elements)}

    def format_window_tree(self, tree: dict[str, Any]) -> str:
        lines = [f"window {tree['hwnd']} \"{tree['title']}\""]
        for e in tree["elements"][1:]:
            left, top, right, bottom = e["bounds"]
            extra = f" {e['value']}" if e.get("value") else ""
            mark = " (selected)" if "selected" in (e.get("states") or []) else ""
            lines.append(f"[{e['id']}] {e['role']} \"{e['name']}\"{extra} @{left},{top} "
                         f"{right - left}x{bottom - top}{mark}")
        return "\n".join(lines)

    # -- actions ----------------------------------------------------------------

    def _guard(self, expect: Any) -> str | None:
        if expect is not None and int(expect) != self.front:
            return f"Foreground window changed; input not sent (expected hwnd {expect}, got {self.front})"
        return None

    def window_action(self, hwnd: int, action: str, **_: Any) -> StubResult:
        if int(hwnd) not in self.state:
            return StubResult(ok=False, summary=f"hwnd {hwnd} is not a window (it may have closed)")
        result = self._act("window_action", "", hwnd, action)
        self.state[int(hwnd)] = {"minimize": "minimized", "close": "closed"}.get(action, "normal")
        if action in ("minimize", "close") and self.front == int(hwnd):
            self.front = next(h for h, st in self.state.items() if st == "normal" and h != int(hwnd))
        elif action in ("maximize", "restore", "focus"):
            self.front = int(hwnd)
        result.summary = (f"\"{self.title(int(hwnd))}\" is now {self.state[int(hwnd)]}"
                          + ("; in the foreground" if self.front == int(hwnd) else ""))
        return result

    def focus_window(self, hwnd: int, **_: Any) -> StubResult:
        return self.window_action(hwnd, "focus")

    def select_tab(self, hwnd: int, index: int, *, expect_name: str | None = None) -> StubResult:
        if int(hwnd) != self.CHROME or not 1 <= int(index) <= len(self.tabs):
            return StubResult(ok=False, summary=f"window {hwnd} has no tab {index}")
        if expect_name is not None and self.tab_name(int(index) - 1) != expect_name:
            return StubResult(ok=False, summary=f"the tabs changed: tab {index} is now "
                                                f"\"{self.tab_name(int(index) - 1)}\"; nothing was selected")
        result = self._act("select_tab", "", hwnd, index)
        self.selected = int(index) - 1
        result.summary = (f"tab {index} of {len(self.tabs)}, \"{self.tab_name(self.selected)}\" is now the one "
                          f"showing (window title now \"{self.title(self.CHROME)}\")")
        result.details = {"verified": True, "hwnd": hwnd, "index": index}
        return result

    def click(self, x: int, y: int, **kwargs: Any) -> StubResult:
        result = self._act("click", f"clicked at ({x},{y})", x, y, **kwargs)
        if self.front == self.CHROME and y < self.TAB_BAR_BOTTOM:
            i = (int(x) - 60) // 220
            if 0 <= i < len(self.tabs):
                self.selected = i
                result.summary += f"; the selected tab is now \"{self.tab_name(i)}\""
        return result

    def scroll(self, x: int, y: int, *, dy: int = 0, dx: int = 0, expect_hwnd: Any = None, **_: Any) -> StubResult:
        refused = self._guard(expect_hwnd)
        if refused:
            return StubResult(ok=False, summary=refused)
        tab = self.tabs[self.selected]
        where = "down" if dy < 0 else "up"
        on_page = self.front == self.CHROME and 0 <= x < 1920 and 110 <= y < 1040
        if self.era == "before":
            # v3.2: the wheel at the point; success whatever moved.
            if on_page:
                tab["scroll"] = min(100.0, max(0.0, tab["scroll"] - dy * 2.0))
            return self._act("scroll", f"scrolled {abs(dy)} notch(es) {where} at ({x},{y})", x, y, dy=dy,
                             expect_hwnd=expect_hwnd)
        # v3.3: the page's own scroll position (the scrollable element at the point, else the
        # window's largest one), read back.
        before = tab["scroll"]
        tab["scroll"] = min(100.0, max(0.0, before - dy * 2.0))
        moved = tab["scroll"] != before
        result = self._act("scroll", "", x, y, dy=dy, expect_hwnd=expect_hwnd)
        name = self.tab_name(self.selected)
        result.ok = moved
        result.summary = (f"scrolled Document \"{name}\" {where}: {before:.0f}% -> {tab['scroll']:.0f}% of the way "
                          f"(UI Automation; the cursor was not moved)" if moved else
                          f"Document \"{name}\" is already at the {'bottom' if dy < 0 else 'top'}: nothing to scroll")
        result.details = {"verified": moved, "scrolls": [{"moved": moved, "before": before, "after": tab["scroll"]}]}
        return result

    def open_url(self, url: str, *, app: str | None = None, **_: Any) -> StubResult:
        result = self._act("open_url", "", url, app=app)
        name = "YouTube" if "youtube" in url else url  # the page's title once it arrives (stub data)
        self.tabs.append({"name": name, "url": url, "scroll": 0.0,
                          "titled_at": time.monotonic() + self.TITLE_DELAY_S})
        self.selected = len(self.tabs) - 1
        self.front = self.CHROME
        self.state[self.CHROME] = "normal"
        result.summary = (f"Google Chrome is now showing {url} (page still loading, no title yet): hwnd "
                          f"{self.CHROME} (chrome.exe) \"{self.title(self.CHROME)}\"; in the foreground")
        result.details = {"page_verified": True, "page": {"title": url, "url": url}, "hwnd": self.CHROME}
        return result

    def _keys(self, keys: tuple[str, ...], times: int = 1) -> str:
        note = ""
        if keys in (("k",), ("space",), ("playpause",)) and self.selected == 0:
            for _ in range(times):
                self.playing = not self.playing
        if keys == ("ctrl", "t"):
            self.tabs.append({"name": "New Tab", "url": "chrome://newtab/", "scroll": 0.0, "titled_at": 0.0})
            self.selected = len(self.tabs) - 1
            note = "; the page changed to \"New Tab\""
        if keys[0] in ("volumedown", "volumeup"):
            self.volume += (2.0 if keys[0] == "volumeup" else -2.0) * times
        return note

    def window_mode(self, hwnd: int) -> str:
        """How the window is shown (the input layer's ``window_state``): the stub's Chrome is maximized."""
        return "maximized"

    def _watched(self, before: tuple[int, str], result: StubResult) -> StubResult:
        """The input layer's own watch of a key's effect (yuki.actions.input: focus, window, title, and v3.14b the
        window's state: normal / maximized / fullscreen), stub-sized."""
        front, title, *rest = before
        details = dict(foreground_changed=self.front != front, focus_changed=False,
                       foreground_title_changed=self.front == front and self.title(self.front) != title)
        if rest:
            mode = self.window_mode(front)
            details.update(window_state_hwnd=front, window_state_before=rest[0], window_state_after=mode,
                           window_state_changed=mode != rest[0])
            if mode != rest[0]:
                result.summary += f"; the window is now {mode} (was {rest[0]})"
        result.details.update(details)
        return result

    def hotkey(self, *keys: str, expect_hwnd: Any = None, **_: Any) -> StubResult:
        refused = self._guard(expect_hwnd)
        if refused:
            return StubResult(ok=False, summary=refused)
        before = (self.front, self.title(self.front), self.window_mode(self.front))
        result = self._act("hotkey", f"pressed {'+'.join(keys)}", *keys, expect_hwnd=expect_hwnd)
        result.summary += self._keys(tuple(keys))
        return self._watched(before, result)

    def press(self, key: str, *, times: int = 1, expect_hwnd: Any = None, **_: Any) -> StubResult:
        refused = self._guard(expect_hwnd)
        if refused:
            return StubResult(ok=False, summary=refused)
        before = (self.front, self.title(self.front), self.window_mode(self.front))
        result = self._act("press", f"pressed {key}" + (f" x{times}" if times > 1 else ""), key, times=times,
                           expect_hwnd=expect_hwnd)
        result.summary += self._keys((key,), times)
        return self._watched(before, result)


class TabsDesk(BrowserDesk):
    """Chrome with the tabs of the live session of 2026-09-26 21:22-21:31 (tests only).

    Each tab has an identity that survives moves (as UIA's RuntimeId does); ``tab(hwnd, id, action)``
    selects or closes one by it, as :func:`yuki.actions.tabs.tab_action` does; Ctrl+W closes the tab
    showing, whatever it is, and Ctrl+Shift+T brings back the most recently closed one, as Chrome does.
    ``closed`` records every tab that went, in order, so a test can say which ones were closed.
    """

    LIVE_TABS = [
        ("Inbox (12) - sam@example.com - Corvana Mail", "https://mail.google.com/mail/u/0/#inbox"),
        ("Your secure link to Claude.ai is here - sam@example.com - Corvana Mail",
         "https://mail.google.com/mail/u/0/#inbox/FMfcg"),
        ("Sign in successful | Claude Platform", "https://platform.claude.com/login/success"),
        ("Claude", "https://claude.ai/new"),
        ("Wasureranneyo - Nannmonee Music Video - YouTube", "https://www.youtube.com/watch?v=nannmonee&list=PL1"),
        ("Settings | ElevenLabs", "https://elevenlabs.io/app/settings"),
        ("- YouTube", "https://www.youtube.com/watch?v=E9U9wLU_dQI"),  # "This video isn't available anymore"
    ]

    def __init__(self, timeline: Any = None, action_delay_s: float = 0.05) -> None:
        super().__init__(era="after", timeline=timeline, action_delay_s=action_delay_s)
        self.tabs = [{"name": n, "url": u, "scroll": 0.0, "titled_at": 0.0, "id": f"42.2492528.4.{i}"}
                     for i, (n, u) in enumerate(self.LIVE_TABS)]
        self.selected = 4  # the playlist video, playing
        self.closed: list[dict[str, Any]] = []
        self._next_id = 100

        self.mixer = MixerStub({"chrome.exe": 100.0, "Discord.exe": 70.0}, playing={"chrome.exe"})
        self.mixer.master_volume = self.master_volume  # the system volume is this desk's own

    def activity_facts(self) -> dict[str, Any]:
        facts = super().activity_facts()
        facts["media"] = [{"app_id": "Chrome", "status": "playing" if self.playing else "paused",
                           "title": "Wasureranneyo - Nannmonee Music Video", "artist": "Nannmonee", "current": True}]
        return facts

    def audio_apps(self) -> list[dict[str, Any]]:
        return self.mixer.audio_apps()

    def app_volume(self, app: str, **kwargs: Any) -> Any:
        self._act("app_volume", "", app, **kwargs)
        return self.mixer.app_volume(app, **kwargs)

    def master_volume(self) -> dict[str, Any]:
        return {"level": round(self.volume, 1), "muted": False}

    def read_tabs(self, hwnd: int, **_: Any) -> Any:
        from yuki.perception.tabs import Tab, TabStrip

        if int(hwnd) != self.CHROME:
            return TabStrip(hwnd=int(hwnd))
        return TabStrip(hwnd=self.CHROME, tabs=[
            Tab(index=i + 1, name=self.tab_name(i), selected=i == self.selected, tab_id=t["id"],
                bounds=(60 + i * 220, 0, 60 + (i + 1) * 220, self.TAB_BAR_BOTTOM)) for i, t in enumerate(self.tabs)],
            nodes=40, ms=3.0)

    def _close_at(self, i: int) -> dict[str, Any]:
        tab = self.tabs.pop(i)
        self.closed.append(tab)
        if self.selected >= len(self.tabs) or self.selected > i:
            self.selected = max(0, self.selected - 1)
        return tab

    def tab(self, hwnd: int, tab_id: str, action: str) -> StubResult:
        result = self._act("tab", "", hwnd, tab_id, action)
        i = next((k for k, t in enumerate(self.tabs) if t["id"] == str(tab_id).strip("[]")), None)
        if i is None:
            result.ok, result.summary = False, f"window {hwnd} has no tab with id {tab_id} now; nothing was done"
            result.details = {"verified": False}
            return result
        name = self.tabs[i]["name"]
        if action == "close":
            self._close_at(i)
            result.summary = f"closed tab \"{name}\" [{tab_id}]; the strip, read again, has {len(self.tabs)} tabs"
        else:
            self.selected = i
            result.summary = f"tab \"{name}\" [{tab_id}] is now the one showing"
        result.details = {"verified": True, "tabs_after": [t["name"] for t in self.tabs]}
        return result

    def _keys(self, keys: tuple[str, ...], times: int = 1) -> str:
        if keys == ("ctrl", "w") and self.tabs:
            tab = self._close_at(self.selected)
            return f"; closed the tab showing, \"{tab['name']}\""
        if keys == ("ctrl", "shift", "t") and self.closed:
            tab = dict(self.closed.pop())
            tab["id"] = f"42.2492528.4.{self._next_id}"  # a restored tab is a new element
            self._next_id += 1
            self.tabs.append(tab)
            self.selected = len(self.tabs) - 1
            return f"; reopened \"{tab['name']}\""
        return super()._keys(keys, times)


# ---------------------------------------------------------------------------
# vfix2 (report 47): a chat app and a music player shaped like the real Discord DM and Spotify trees of the
# 3 Oct session (roles, patterns, states and boxes as UI Automation gave them; names, songs and messages invented)
# ---------------------------------------------------------------------------


class ChatPlayerDesk(StubDesktop):
    """A Discord DM and Spotify, for the report 47 replays (tests and scripts only; nothing real is touched).

    * The DM (``CHAT``): a ``List`` "Messages in @name" of ``ListItem`` messages (author, time, text; newest at the
      bottom) and the message box, an ``Edit`` "Message @name". ``type_text`` with ``press_enter`` into it sends:
      the message is appended (``sent``) at the desk's clock.
    * Spotify (``PLAYER``): the "Now playing bar" ``Group`` with toggle ``Button`` s "Lyrics" / "Queue" / "Enter Full
      screen" (state ``off``/``on``), ``Button`` "Mute", the ``Text`` "Change volume" that hides the real volume
      slider (a ``Slider`` with RangeValue, here only through :meth:`set_slider`), and the "Now playing view". With
      the lyrics open full size (``lyrics_full``) the bar is hidden until the pointer hovers its area, as live.
    """

    CHAT, PLAYER = 0x2001, 0x2002
    BAR_TOP = 890

    def __init__(self, *, friend: str = "Sam", handle: str = "sam_99", history: list[tuple[str, float, str]] | None = None,
                 volume: float = 30.0, mixer_level: float = 100.0, clock: Any = None, action_delay_s: float = 0.0) -> None:
        super().__init__(action_delay_s=action_delay_s)
        self.friend, self.handle = friend, handle
        self.clock = clock or time.time
        #: (author, epoch, text), oldest first: "me" is the user
        self.history: list[tuple[str, float, str]] = list(history or [])
        self.sent: list[str] = []
        self.composer = ""
        self.volume = float(volume)        # Spotify's own player volume (its slider), 0-100
        self.toggles = {"Lyrics": False, "Queue": False, "Enter Full screen": False}
        self.lyrics_full = False
        self.bar_revealed = False
        self.clicks: list[str] = []
        #: N6: the user's own playlists in the sidebar, and which one plays (``None``: whatever played before)
        self.playlists = ["Gym Mix", "J-Pop Nights", "Focus Lofi"]
        self.playing: str | None = None
        self.mixer = MixerStub({"Spotify.exe": mixer_level, "Discord.exe": 70.0}, playing={"Spotify.exe"}, system=40.0)

    # -- perception -------------------------------------------------------------

    #: A browser for the list replays (``with_browser``): its title follows what was opened in it.
    BROWSER = 0x2003
    with_browser = False
    browser_title = "New Tab - Google Chrome"

    def _windows(self) -> list[tuple[int, str, str]]:
        wins = [(self.CHAT, "Discord.exe", f"@{self.handle} - Discord"), (self.PLAYER, "Spotify.exe", "Spotify Premium")]
        if self.with_browser:
            wins.insert(0, (self.BROWSER, "chrome.exe", self.browser_title))
        return wins

    def launch_app(self, query: str, **kwargs: Any) -> StubResult:
        if "chrome" in query.casefold():
            self.with_browser = True
        result = self._act("launch_app", f"launched {query}; its window is in front", query, **kwargs)
        result.details = {"hwnd": self.BROWSER if self.with_browser else 0, "verified": True}
        return result

    def open_url(self, url: str, **kwargs: Any) -> StubResult:
        self.with_browser = True
        if "youtube" in url.casefold():
            self.browser_title = "YouTube - Google Chrome"
        result = self._act("open_url", f"opened {url}; window {self.BROWSER} \"{self.browser_title}\" is in front",
                           url, **kwargs)
        result.details = {"hwnd": self.BROWSER, "page_verified": True, "verified": True}
        return result

    def get_desktop_overview(self) -> dict[str, Any]:
        wins = self._windows()
        return {"windows": [{"hwnd": h, "process_name": p, "title": t, "is_foreground": i == 0, "is_minimized": False,
                             "pid": 200 + i, "bounds": (0, 0, 1920, 1040)} for i, (h, p, t) in enumerate(wins)],
                "foreground_hwnd": wins[0][0], "cursor": (960, 540), "screen_size": (1920, 1080),
                "captured_at": time.time(), "monitors": [], "virtual_bounds": (0, 0, 1920, 1080)}

    def _el(self, out: list[dict[str, Any]], role: str, name: str, bounds: tuple[int, int, int, int], **kw: Any) -> None:
        cx, cy = (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2
        out.append({"id": len(out), "role": role, "name": name, "bounds": bounds, "center": (cx, cy),
                    "is_interactive": bool(kw.get("patterns")) or role in ("Edit", "Button", "Slider"), **kw})

    def get_window_tree(self, hwnd: int, **_: Any) -> dict[str, Any]:
        self._perceive("get_window_tree")
        title = dict((h, t) for h, _p, t in self._windows()).get(int(hwnd), "")
        out: list[dict[str, Any]] = []
        self._el(out, "Window", title, (0, 0, 1920, 1040))
        if int(hwnd) == self.CHAT:
            self._el(out, "List", f"Messages in @{self.handle}", (240, 60, 1640, 960))
            shown = self.history[-12:]
            top = 960 - 60 * len(shown)
            for i, (who, at, text) in enumerate(shown):
                author = "You" if who == "me" else who
                stamp = time.strftime("%I:%M %p", time.localtime(at)).lstrip("0")
                y = top + 60 * i
                self._el(out, "ListItem", f"{author}, {stamp}, {text}", (260, y, 1620, y + 58))
                self._el(out, "Text", text, (320, y + 24, 1600, y + 50))
            self._el(out, "Edit", f"Message @{self.handle}", (260, 980, 1620, 1020), patterns=["value"],
                     value=self.composer)
            process = "Discord.exe"
        elif int(hwnd) == self.BROWSER:
            self._el(out, "Document", self.browser_title.replace(" - Google Chrome", ""), (0, 110, 1920, 1040))
            for i, name in enumerate(("Ocean drive - 4 minutes", "Night walk - 7 minutes", "City lights - 3 minutes")):
                self._el(out, "Hyperlink", name, (300 + 420 * i, 300, 700 + 420 * i, 520), patterns=["invoke"])
            process = "chrome.exe"
        else:
            # the library in the sidebar, as live: each playlist a Button "<name> Playlist • <owner>" with a play Button
            self._el(out, "Group", "Your Library", (111, 120, 430, 860))
            for i, name in enumerate(self.playlists):
                y = 220 + 56 * i
                self._el(out, "Button", f"{name} Playlist • you", (117, y, 425, y + 50), patterns=["invoke"])
                self._el(out, "Button", f"Play {name}", (123, y + 6, 160, y + 44), patterns=["invoke"])
            if not self.lyrics_full or self.bar_revealed:
                self._el(out, "Group", "Now playing bar", (111, self.BAR_TOP, 1501, 945))
                song = f"{self.playing}, track 1 by Various" if self.playing else "Song A by Artist B"
                self._el(out, "Group", f"Now playing: {song}", (117, 896, 529, 939))
                for name, x in (("Lyrics", 1278), ("Queue", 1302)):
                    self._el(out, "Button", name, (x, 905, x + 25, 930), patterns=["toggle"],
                             states=["on" if self.toggles[name] else "off"])
                self._el(out, "Button", "Mute", (1351, 905, 1376, 930), patterns=["invoke"])
                # live the read showed only this Text; the real slider (a web range input) sat inside it, unread
                self._el(out, "Text", "Change volume", (1374, 912, 1418, 947))
                self._el(out, "Button", "Enter Full screen", (1476, 905, 1501, 930), patterns=["toggle"],
                         states=["on" if self.toggles["Enter Full screen"] else "off"])
            if self.toggles["Lyrics"]:
                self._el(out, "Group", "Lyrics: la la la, the words of Song A", (111, 127, 1501, 880 if not
                                                                                   self.lyrics_full else 1000))
            process = "Spotify.exe"
        return {"hwnd": hwnd, "title": title, "process_name": process, "elements": out, "truncated": False,
                "status": "ok", "note": "", "captured_at": time.time(), "elapsed_ms": 5.0, "passes": 1,
                "child_windows": 0, "first_pass_elements": len(out)}

    def format_window_tree(self, tree: dict[str, Any]) -> str:
        lines = [f"window {tree['hwnd']} \"{tree['title']}\""]
        for e in tree["elements"][1:]:
            left, top, right, bottom = e["bounds"]
            extra = f" = {e['value']}" if e.get("value") not in (None, "") else ""
            states = f" ({', '.join(e['states'])})" if e.get("states") else ""
            pats = f" [{', '.join(e['patterns'])}]" if e.get("patterns") else ""
            lines.append(f"[{e['id']}] {e['role']} \"{e['name']}\"{extra}{states}{pats} @{left},{top} "
                         f"{right - left}x{bottom - top}")
        return "\n".join(lines)

    def window_at(self, x: int, y: int) -> int:
        """Every window fills the screen: the one in front is at any point."""
        return int(getattr(self, "front", 0) or self._windows()[0][0])

    def focus_window(self, hwnd: int, **kwargs: Any) -> StubResult:
        self.front = int(hwnd)
        result = self._act("focus_window", f"window {hwnd} is in front", hwnd, **kwargs)
        result.details = {"hwnd": int(hwnd), "verified": True}
        return result

    # -- actions ----------------------------------------------------------------

    def type_text(self, text: str, *, press_enter: bool = False, expect_hwnd: Any = None, **kwargs: Any) -> StubResult:
        result = self._act("type_text", "", text, press_enter=press_enter, expect_hwnd=expect_hwnd, **kwargs)
        self.composer += text
        if press_enter:
            self.sent.append(self.composer)
            self.history.append(("me", float(self.clock()), self.composer))
            self.composer = ""
            result.summary = f"typed {len(text)} chars and pressed Enter; the message box is empty again"
        else:
            result.summary = f"typed {len(text)} chars; the field reads them back"
        result.details = {"readback": "arrived" if not press_enter else None, "hwnd": self.CHAT}
        return result

    def click(self, x: int, y: int, **kwargs: Any) -> StubResult:
        result = self._act("click", f"clicked at ({x}, {y})", x, y, **kwargs)
        tree = self.get_window_tree(self.PLAYER)
        hits = [e for e in tree["elements"][1:] if e["role"] == "Button"
                and e["bounds"][0] <= x < e["bounds"][2] and e["bounds"][1] <= y < e["bounds"][3]]
        hit = min(hits, key=lambda e: (e["bounds"][2] - e["bounds"][0]) * (e["bounds"][3] - e["bounds"][1]),
                  default=None)   # the innermost button under the point (a play button inside its row)
        if hit is not None:
            self.clicks.append(hit["name"])
            if hit["name"].startswith("Play ") and hit["name"][5:] in self.playlists:
                self.playing = hit["name"][5:]
                result.details = {"state_changed": True, "verified": True}
            if hit["name"] in self.toggles:
                self.toggles[hit["name"]] = not self.toggles[hit["name"]]
                if hit["name"] == "Lyrics":
                    self.lyrics_full = self.toggles["Lyrics"] and self.toggles["Enter Full screen"]
                result.details = {"state_changed": True, "verified": True}
        return result

    def hover(self, x: int, y: int, **kwargs: Any) -> StubResult:
        result = self._act("hover", f"the pointer is at ({x},{y})", x, y, **kwargs)
        if self.lyrics_full and y >= self.BAR_TOP - 40:
            self.bar_revealed = True
            result.summary += "; the player bar appeared under it"
        return result

    def set_slider(self, hwnd: int, element: dict[str, Any], *, to: float | None = None, by: float | None = None,
                   press: Any = None) -> StubResult:
        from yuki.actions.slider import drive

        desk = self

        class _Live:
            def read(self) -> tuple[float, float, float, bool, float]:
                return desk.volume, 0.0, 100.0, False, 2.0

            def set(self, value: float) -> None:
                desk.volume = float(value)

            def focus(self) -> None:
                return None

        self._act("set_slider", "", hwnd, element, to=to, by=by)
        if int(hwnd) != self.PLAYER or "volume" not in str(element.get("name") or "").casefold():
            return StubResult(ok=False, summary="no slider value under it (UI Automation shows no RangeValue there). "
                                                "Nothing was dragged or clicked", details={"verified": False})
        ok, summary, details = drive(_Live(), to=to, by=by, press=None, name="Slider \"Change volume\"",
                                     sleep=lambda _s: None, settle_s=0.0)
        return StubResult(ok=ok, summary=summary, details=details)

    def app_volume(self, app: str, **kwargs: Any) -> Any:
        self._act("app_volume", "", app, **kwargs)
        return self.mixer.app_volume(app, **kwargs)

    def audio_apps(self) -> list[dict[str, Any]]:
        return self.mixer.audio_apps()

    def master_volume(self) -> dict[str, Any]:
        return self.mixer.master_volume()


# ---------------------------------------------------------------------------
# A pretend room for the speakers tests (docs/VOICE.md §11): what Yuki plays on
# a stand-in speaker comes back into a stand-in microphone, the way it would
# from real speakers, and nothing touches a real device (rule 7).
# ---------------------------------------------------------------------------


class Room:
    """The acoustic path from a speaker to a microphone, on the monotonic clock (tests only).

    Audio played at heard-time ``t`` arrives in the mic at ``t + acoustic_s`` through a
    reverberant impulse response (``rt60``) at ``echo_db`` relative to what was played,
    after a speaker nonlinearity. Played audio is added into a growing buffer; the mic
    reads it back by capture time.
    """

    RATE = 16_000

    def __init__(self, *, acoustic_s: float = 0.004, rt60: float = 0.3, echo_db: float = -8.0,
                 drr_db: float = 3.0, seed: int = 1, t0: float | None = None) -> None:
        import threading

        import numpy as np

        self._np = np
        self.t0 = time.monotonic() if t0 is None else t0
        self.acoustic_s = acoustic_s
        rng = np.random.default_rng(seed)
        n = int(rt60 * self.RATE)
        t = np.arange(n) / self.RATE
        tail = rng.standard_normal(n) * np.exp(-6.9 * t / rt60)
        tail[: int(0.004 * self.RATE)] = 0.0
        # The reverberant tail carries 10^(-drr/10) of the direct sound's energy (a desk speaker a
        # metre from the mic: the direct sound a little louder than the room's reflections).
        tail *= np.sqrt(10 ** (-drr_db / 10) / np.sum(tail ** 2))
        ir = tail
        ir[0] += 1.0
        self.ir = (ir / np.sqrt(np.sum(ir ** 2)) * 10 ** (echo_db / 20)).astype(np.float32)
        self._buf = np.zeros(self.RATE * 30, np.float32)
        self._lock = threading.Lock()
        self.played_s = 0.0

    def _grow(self, end: int) -> None:
        if end > len(self._buf):
            extra = max(end - len(self._buf), self.RATE * 30)
            self._buf = self._np.concatenate([self._buf, self._np.zeros(extra, self._np.float32)])

    def play(self, samples: Any, heard_at: float) -> None:
        """``samples`` (float32, 16 kHz) leave the speaker at ``heard_at`` (monotonic)."""
        np = self._np
        if not len(samples):
            return
        wet = np.convolve(np.tanh(samples * 1.6) / 1.6, self.ir).astype(np.float32)
        i = int(round((heard_at + self.acoustic_s - self.t0) * self.RATE))
        if i < 0:
            wet, i = wet[-i:], 0
        with self._lock:
            self._grow(i + len(wet))
            self._buf[i:i + len(wet)] += wet
            self.played_s += len(samples) / self.RATE

    def pick(self, at: float, n: int) -> Any:
        """What reaches the mic's membrane during ``[at, at + n / RATE)``."""
        i = int(round((at - self.t0) * self.RATE))
        if i < 0:
            return self._np.zeros(n, self._np.float32)
        with self._lock:
            self._grow(i + n)
            return self._buf[i:i + n].copy()


class RoomSpeaker:
    """A stand-in :class:`yuki.voice.audio.Speaker` that plays into a :class:`Room`, in real time.

    It reports ``reported_latency_s`` (what a device would claim) while the audio really
    leaves ``true_latency_s`` after it is written (behind what is queued): the echo guard
    must cope with the difference. ``timeline`` is fed exactly as the real speaker feeds it.
    """

    def __init__(self, rate: int, device: str | None = None, *, room: Room, reported_latency_s: float = 0.02,
                 true_latency_s: float = 0.045, buffer_s: float = 0.03) -> None:
        self.rate = rate
        self.device = device
        self.device_used = device or "Speakers (room)"
        self.room = room
        self.latency_s = reported_latency_s
        self.true_latency_s = true_latency_s
        self.buffer_s = buffer_s
        self.timeline: Any = None
        self._clock = 0.0
        self._resample: Any = None
        self.chunks: list[tuple[float, bytes]] = []
        self.opened = False

    def open(self) -> dict[str, Any]:
        self.opened = True
        return {"device": self.device_used, "output_latency_ms": round(self.latency_s * 1000, 1)}

    def write(self, pcm: bytes, *, gate: bool = True) -> None:
        import numpy as np

        if not self.opened or not pcm:
            return
        if self.timeline is not None:
            self.timeline.played(pcm, self.rate, gate=gate)
        now = time.monotonic()
        start = max(now + self.true_latency_s, self._clock)
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if self.rate != Room.RATE:
            import soxr

            if self._resample is None:
                self._resample = soxr.ResampleStream(self.rate, Room.RATE, 1, dtype="float32")
            samples = self._resample.resample_chunk(samples)
        self.room.play(samples, start)
        self.chunks.append((start, pcm))
        self._clock = start + len(pcm) / 2 / self.rate
        wait = self._clock - self.true_latency_s - self.buffer_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)  # a blocking device: the write returns once the buffer has room

    def close(self) -> None:
        wait = self._clock - time.monotonic()
        if self.opened and wait > 0:
            time.sleep(min(wait, 2.0))  # a real stop() plays what is queued first
        self.opened = False

    def switch(self, device: str | None, *, between: Any = None) -> dict[str, Any]:
        self.close()
        if between is not None:
            between()
        self.device = device
        self.device_used = device or "Speakers (room)"
        return self.open()


def clip_mic(device: str | None = None, *, room: Room | None = None, script: list | None = None,
             noise_dbfs: float = -60.0, reported_input_latency_s: float = 0.01,
             true_input_latency_s: float = 0.02) -> Any:
    """A stand-in :class:`yuki.voice.audio.SharedMic` fed in real time from clips and a :class:`Room`.

    The real ``SharedMic`` (its fan-out, backlog, echo guard and levels) with only the device
    replaced: a thread delivers 20 ms blocks of the script's clips, plus what the room carries
    from the speaker, plus low noise. ``script`` is a list of ``(clip, after_s, wait_for)``:
    each clip starts ``after_s`` after the previous one ended, counted from when
    ``wait_for()`` (if given) became true. A block is stamped ``true_input_latency_s`` after
    its sound (the mic reports ``reported_input_latency_s``). ``.lines`` records when each
    clip started (monotonic).
    """
    import threading

    import numpy as np

    from yuki.voice.audio import BLOCK_SAMPLES, SharedMic

    class ClipMic(SharedMic):
        def __init__(self) -> None:
            super().__init__(device)
            self.room = room
            self.script = list(script or [])
            self.noise = 10 ** (noise_dbfs / 20)
            self._run = False
            self._thread: Any = None
            self.lines: list[dict[str, Any]] = []
            self.done = threading.Event()

        @property
        def is_open(self) -> bool:
            return self._run

        def open(self) -> dict[str, Any]:
            if self._run:
                return {"device": self.device_used}
            self.device_used = "Microphone (clips)"
            self.opened_as = "clips"
            self.input_latency_s = reported_input_latency_s
            self.format = {"device_rate": 16000, "channels": 1,
                           "input_latency_ms": round(reported_input_latency_s * 1000, 1)}
            self._run = True
            self._thread = threading.Thread(target=self._feed, name="clip-mic", daemon=True)
            self._thread.start()
            return {"device": self.device_used, "opened_as": self.opened_as}

        def close(self, *, keep_backlog: bool = False) -> None:
            self._run = False
            if not keep_backlog:
                with self._lock:
                    self._backlog.clear()

        def _feed(self) -> None:
            rng = np.random.default_rng(3)
            clock = time.monotonic()
            queue = list(self.script)
            current, pos, ready_since = None, 0, None
            while self._run:
                if current is None and queue:
                    clip, after_s, wait_for = queue[0]
                    if ready_since is None and (wait_for is None or wait_for()):
                        ready_since = clock
                    if ready_since is not None and clock - ready_since >= after_s:
                        queue.pop(0)
                        current, pos, ready_since = clip, 0, None
                        self.lines.append({"started_at": clock, "seconds": len(clip) / 16000})
                block = rng.standard_normal(BLOCK_SAMPLES).astype(np.float32) * self.noise
                if current is not None:
                    part = current[pos:pos + BLOCK_SAMPLES]
                    block[:len(part)] += part
                    pos += BLOCK_SAMPLES
                    if pos >= len(current):
                        current = None
                        if not queue:
                            self.done.set()
                if self.room is not None:
                    block += self.room.pick(clock - true_input_latency_s, BLOCK_SAMPLES)
                self.feed(block, clock)
                clock += BLOCK_SAMPLES / 16000
                delay = clock + true_input_latency_s - time.monotonic()
                if delay > 0:
                    time.sleep(delay)

    return ClipMic()
