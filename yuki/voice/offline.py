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

    def window_action(self, hwnd: int, action: str) -> StubResult:
        return self._act("window_action", f"window {hwnd} is now {action}d", hwnd, action)


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

    def hotkey(self, *keys: str, expect_hwnd: Any = None, **_: Any) -> StubResult:
        refused = self._guard(expect_hwnd)
        if refused:
            return StubResult(ok=False, summary=refused)
        result = self._act("hotkey", f"pressed {'+'.join(keys)}", *keys, expect_hwnd=expect_hwnd)
        result.summary += self._keys(tuple(keys))
        return result

    def press(self, key: str, *, times: int = 1, expect_hwnd: Any = None, **_: Any) -> StubResult:
        refused = self._guard(expect_hwnd)
        if refused:
            return StubResult(ok=False, summary=refused)
        result = self._act("press", f"pressed {key}" + (f" x{times}" if times > 1 else ""), key, times=times,
                           expect_hwnd=expect_hwnd)
        result.summary += self._keys((key,), times)
        return result


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
