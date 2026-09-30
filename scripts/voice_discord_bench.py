"""The voice thinker (the real agent on Bedrock) on a Discord stub desk, offline (docs/VOICE.md v3.15).

The live session of 2026-09-30 21:29-21:31: tagging a friend in a Discord channel took 69 s, 8 thinker steps and
$0.54 (the mention pasted through the clipboard gets no autocomplete; a message row clicked as if it were the
suggestion; every step re-sent a ~21.8k-character window view; an escalation on a read-back the report had made).
This desk models Discord's structure with made-up names (never the user's data): the server sidebar, the channel
list, the messages (ListItems), the members, the message box (a rich-text Edit whose Value holds its text, its
parts as children: a mention is a chip element), the mention popup that opens only when "@" and a name are
TYPED by keys (a paste opens none), Enter/Tab accepting the selected suggestion, Enter sending otherwise, Ctrl+K's
quick switcher. Each run reports success (the desk's own state: a message sent in the channel with a mention
chip of the person), steps, wall time, per-step first-move time (``first_tool_ms`` of each model call), input
characters per step, escalation, cost.

Usage::

    .venv\\Scripts\\python.exe scripts/voice_discord_bench.py [in_channel|full|both] [rounds]

Memory is an empty temp database (never the real one); nothing touches the machine.
"""

from __future__ import annotations

import inspect
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import voice_scenarios as vs  # noqa: E402

from yuki.agent.loop import Agent  # noqa: E402
from yuki.agent.memory import MemoryAccess  # noqa: E402
from yuki.config import Settings  # noqa: E402
from yuki.log.events import AskUser, ErrorEvent, Final, SessionLogger, ToolCall  # noqa: E402
from yuki.perception.tree import UIElement, WindowTree, format_window_tree  # noqa: E402
from yuki.voice.mouth import brain_request  # noqa: E402
from yuki.voice.offline import StubDesktop, StubResult  # noqa: E402

try:   # the input layer of this tree: whether it knows suggestion lists (v3.15)
    from yuki.actions import input as _input

    NEW_INPUT = "accept_suggestion" in inspect.signature(_input.type_text).parameters
except Exception:  # noqa: BLE001
    _input, NEW_INPUT = None, False

SERVERS = ["Study Hall", "Pixel Forge", "Mafex's Hangout", "Night Owls", "Code Dojo", "Chill Zone",
           "Anime Club", "Gaming Den"]
CHANNELS = {
    "Mafex's Hangout": ["rules", "notice-board", "ping-zone", "after-hours", "anime", "vent-room",
                         "slime-videos", "bot", "memes", "spam", "private-stuff", "neko", "no-mic",
                         "art-corner", "asmr", "food-corner", "cool-places"],
}
MEMBERS = ["Hemanth", "Harry Tran", "Mafex", "Riya", "Kabir", "Zed", "Ananya", "Vik", "Yash", "Ishaan", "Meera",
           "Dev", "Sana", "Arjun", "Noor", "Kavya"]
USERNAMES = {"Hemanth": "hemanth_q", "Harry Tran": "harryt", "Mafex": "mafex11", "Riya": "riya.k", "Kabir": "kabz",
             "Zed": "zedd", "Ananya": "ann", "Vik": "vikram", "Yash": "yash_b", "Ishaan": "ish", "Meera": "meera.s",
             "Dev": "devv", "Sana": "sana_x", "Arjun": "arjunr", "Noor": "noor", "Kavya": "kavya7"}


def _messages(server: str, channel: str) -> list[tuple[str, str, str]]:
    """``(author, time, text)`` rows of a channel, made up (deterministic)."""
    words = ["kal milte hai phir", "ok bro that works for me", "check this out, it is actually insane",
             "lol no way that happened", "anyone up for a game tonight?", "gg well played everyone",
             "restart karke aata hu ek minute", "join vc when you are free", "brb getting food",
             "send the link in this channel please", "nice one, saved it", "wait what did I miss",
             "haha that clip is too good", "done, pushed the fix", "see you tomorrow then"]
    rows = []
    for i in range(30):
        author = MEMBERS[(i * 3 + len(channel)) % len(MEMBERS)]
        rows.append((author, f"Yesterday at {9 + i % 12}:{(i * 7) % 60:02d} PM",
                     f"{words[(i + len(server)) % len(words)]} {words[(i * 5) % len(words)]}"))
    return rows


class DiscordDesk(StubDesktop):
    DISCORD, CHROME = 0x2002, 0x3003

    def __init__(self, *, server: str = "Study Hall", channel: str = "general") -> None:
        super().__init__(action_delay_s=0.0)
        self.measured_steps = True
        self.server, self.channel = server, channel
        self.box: list[tuple[str, str]] = []       # ("text", s) | ("mention", name)
        self.popup: list[str] | None = None
        self.selected = 0
        self.focus = "box"                         # box | switcher | other
        self.switcher: str | None = None           # the quick switcher's query while open
        self.sent: list[dict[str, Any]] = []
        self.pastes = 0
        self.clicked_rows = 0
        self._layout: dict[int, tuple[str, Any]] = {}

    # -- state -----------------------------------------------------------------------------

    def channels(self) -> list[str]:
        return CHANNELS.get(self.server, ["general", "clips", "music", "off-topic", "help", "voice-text"])

    def title(self, hwnd: int) -> str:
        if int(hwnd) == self.CHROME:
            return "New Tab - Google Chrome"
        return f"#{self.channel} | {self.server} - Discord"

    def box_text(self) -> str:
        return "".join(f"@{v}" if k == "mention" else v for k, v in self.box)

    def _tail_query(self) -> str | None:
        """The mention being typed at the end of the box ("@Hem" -> "Hem"), or None."""
        if not self.box or self.box[-1][0] != "text":
            return None
        text = self.box[-1][1]
        at = text.rfind("@")
        if at < 0 or (at > 0 and not text[at - 1].isspace()):
            return None
        query = text[at + 1:]
        return query if query and " " not in query else None

    def _refresh_popup(self, typed: bool) -> None:
        query = self._tail_query() if typed else None
        if query is None:
            self.popup = None
            return
        found = [m for m in MEMBERS if m.casefold().startswith(query.casefold())
                 or USERNAMES[m].casefold().startswith(query.casefold())]
        self.popup, self.selected = (found or None), 0

    def _accept(self) -> str | None:
        if not self.popup:
            return None
        name = self.popup[self.selected]
        kind, text = self.box[-1]
        self.box[-1] = (kind, text[:text.rfind("@")])
        if not self.box[-1][1]:
            self.box.pop()
        self.box += [("mention", name), ("text", " ")]
        self.popup = None
        return name

    def _send(self) -> None:
        if self.box_text().strip():
            self.sent.append({"server": self.server, "channel": self.channel, "parts": list(self.box),
                              "text": self.box_text()})
        self.box, self.popup = [], None

    # -- the tree ----------------------------------------------------------------------------

    def get_desktop_overview(self) -> dict[str, Any]:
        wins = [{"hwnd": self.DISCORD, "process_name": "Discord.exe", "title": self.title(self.DISCORD),
                 "is_foreground": True, "is_minimized": False, "pid": 99, "bounds": (0, 0, 1920, 1040)},
                {"hwnd": self.CHROME, "process_name": "chrome.exe", "title": self.title(self.CHROME),
                 "is_foreground": False, "is_minimized": False, "pid": 98, "bounds": (0, 0, 1920, 1040)}]
        return {"windows": wins, "foreground_hwnd": self.DISCORD, "cursor": (960, 540), "screen_size": (1920, 1080),
                "captured_at": time.time(), "monitors": [], "virtual_bounds": (0, 0, 1920, 1080)}

    def format_overview(self, overview: dict[str, Any]) -> str:
        lines = [f"Open windows ({len(overview['windows'])}), foreground first:"]
        for w in overview["windows"]:
            mark = " [foreground]" if w["is_foreground"] else ""
            lines.append(f"- [{w['hwnd']}] {w['process_name']} \"{w['title']}\"{mark}")
        return "\n".join(lines)

    def activity_facts(self) -> dict[str, Any]:
        return {"media": [], "microphone": [], "camera": [],
                "foreground": {"hwnd": self.DISCORD, "process_name": "Discord.exe", "title": self.title(self.DISCORD)}}

    def get_window_tree(self, hwnd: int, **_: Any) -> WindowTree:
        self._perceive("get_window_tree")
        els: list[UIElement] = []
        self._layout = {}

        def add(role: str, name: str, parent: int, depth: int, *, x: int, y: int, w: int = 200, h: int = 30,
                value: str | None = None, interactive: bool = False, patterns: tuple[str, ...] = (),
                states: tuple[str, ...] = (), focused: bool = False, act: tuple[str, Any] | None = None,
                position: tuple[int, int] | None = None) -> int:
            i = len(els)
            els.append(UIElement(id=i, role=role, name=name, value=value, center=(x, y),
                                 bounds=(x - w // 2, y - h // 2, x + w // 2, y + h // 2), is_interactive=interactive,
                                 is_scrollable=False, is_focused=focused, shortcut=None, depth=depth,
                                 patterns=patterns, states=states, parent=parent, position=position))
            if act is not None:
                self._layout[i] = act
            return i

        if int(hwnd) == self.CHROME:
            add("Window", self.title(hwnd), -1, 0, x=960, y=520, w=1920, h=1040)
            add("Edit", "Address and search bar", 0, 3, x=700, y=60, interactive=True, patterns=("value",))
            return self._tree(hwnd, els)
        root = add("Window", self.title(hwnd), -1, 0, x=960, y=520, w=1920, h=1040)
        side = add("Group", "Servers sidebar", root, 2, x=36, y=520, w=72, h=1000)
        dm = add("TreeItem", "Direct Messages", side, 3, x=36, y=40, interactive=True, patterns=("select",),
                 act=("server", "Direct Messages"))
        del dm
        for i, s in enumerate(SERVERS):
            add("TreeItem", s, side, 3, x=36, y=110 + i * 56, w=48, h=48, interactive=True, patterns=("select",),
                states=("selected",) if s == self.server else (), act=("server", s), position=(i + 1, len(SERVERS)))
        head = add("Button", f"{self.server}, server actions", root, 2, x=190, y=24, interactive=True,
                   patterns=("invoke",))
        del head
        chan = add("List", "Channels", root, 2, x=190, y=520, w=230, h=900)
        for i, c in enumerate(self.channels()):
            row = add("ListItem", "", chan, 3, x=190, y=80 + i * 34, w=220, h=32, position=(i + 1, len(self.channels())))
            add("Hyperlink", f"{c} (text channel)", row, 4, x=180, y=80 + i * 34, interactive=True,
                patterns=("invoke",), states=("selected",) if c == self.channel else (), act=("channel", c))
            add("Button", "Invite to Channel", row, 4, x=280, y=80 + i * 34, w=20, interactive=True,
                patterns=("invoke",))
        panel = add("Group", "User status and settings", root, 2, x=190, y=1010, w=230, h=50)
        for b, bx in (("Mute", 250), ("Deafen", 280), ("User Settings", 310)):
            add("Button", b, panel, 3, x=bx, y=1010, w=24, interactive=True, patterns=("toggle",) if b != "User Settings"
                else ("invoke",))
        doc = add("Document", f"#{self.channel} | {self.server}", root, 2, x=960, y=520, w=1200, h=1000,
                  value=f"https://discord.com/channels/7612/{abs(hash(self.channel)) % 10**6}")
        msgs = add("List", f"Messages in {self.channel}", doc, 3, x=960, y=480, w=1200, h=860)
        rows = _messages(self.server, self.channel) + [
            ("You", "Today at 9:31 PM", m["text"]) for m in self.sent
            if m["server"] == self.server and m["channel"] == self.channel]
        for i, (who, when, text) in enumerate(rows[-30:]):
            y = 60 + i * 26
            row = add("ListItem", f"{who}{when}{text}", msgs, 4, x=960, y=y, w=1180, h=24, act=("row", i))
            add("Text", who, row, 5, x=420, y=y, w=90)
            add("Text", when, row, 5, x=520, y=y, w=120)
            add("Text", text, row, 5, x=800, y=y, w=500)
        if self.popup:
            pop = add("List", "", doc, 3, x=900, y=900, w=900, h=40 * len(self.popup))
            for i, m in enumerate(self.popup):
                add("ListItem", f"{m} {USERNAMES[m]}", pop, 4, x=900, y=850 + i * 40, w=880, h=38,
                    interactive=True, patterns=("select",), states=("selected",) if i == self.selected else (),
                    act=("suggestion", i), position=(i + 1, len(self.popup)))
        box = add("Edit", f"Message #{self.channel}", doc, 3, x=960, y=1000, w=1180, h=44,
                  value=self.box_text() or "﻿\n", interactive=True, patterns=("value",),
                  focused=self.focus == "box", act=("box", None))
        para = add("Group", "", box, 4, x=960, y=1000, w=1180, h=40)
        for kind, v in self.box:
            if kind == "mention":
                add("Button", f"@{v}", para, 5, x=600, y=1000, w=90, interactive=True, patterns=("invoke",))
            elif v.strip():
                add("Text", v, para, 5, x=800, y=1000, w=400)
        members = add("List", "Members", root, 2, x=1800, y=520, w=230, h=900)
        for i, m in enumerate(MEMBERS):
            add("ListItem", m, members, 3, x=1800, y=80 + i * 40, w=220, h=38, interactive=True,
                patterns=("select",), act=("member", m), position=(i + 1, len(MEMBERS)))
        if self.switcher is not None:
            dlg = add("Group", "Quick switcher", root, 2, x=960, y=400, w=600, h=400)
            add("Edit", "Quick switcher", dlg, 3, x=960, y=260, w=560, value=self.switcher, interactive=True,
                patterns=("value",), focused=True)
            for i, (kind, name) in enumerate(self._switch_hits()[:6]):
                add("ListItem", f"{name} ({kind})", dlg, 3, x=960, y=320 + i * 40, w=560, interactive=True,
                    patterns=("select",), states=("selected",) if i == 0 else (), act=("switch", i))
        return self._tree(hwnd, els)

    def _tree(self, hwnd: int, els: list[UIElement]) -> WindowTree:
        return WindowTree(hwnd=int(hwnd), title=self.title(hwnd),
                          process_name="Discord.exe" if int(hwnd) == self.DISCORD else "chrome.exe",
                          elements=els, truncated=False, captured_at=time.time(), elapsed_ms=180.0)

    def format_window_tree(self, tree: Any) -> str:
        return format_window_tree(tree)

    def _switch_hits(self) -> list[tuple[str, str]]:
        q = (self.switcher or "").casefold().lstrip("#")
        hits = [("channel", f"#{c} in {s}") for s, cs in CHANNELS.items() for c in cs if q and q in c.casefold()]
        hits += [("server", s) for s in SERVERS if q and q in s.casefold()]
        return hits

    # -- actions ---------------------------------------------------------------------------

    def _at(self, x: int, y: int) -> tuple[str, Any] | None:
        self.get_window_tree(self.DISCORD) if not self._layout else None
        best = None
        tree = self.get_window_tree(self.DISCORD)
        for e in tree.elements:
            left, top, right, bottom = e.bounds
            if left <= x <= right and top <= y <= bottom and e.id in self._layout:
                if best is None or (right - left) * (bottom - top) < best[0]:
                    best = ((right - left) * (bottom - top), self._layout[e.id])
        return best[1] if best else None

    def click(self, x: int, y: int, **kwargs: Any) -> StubResult:
        result = self._act("click", "", x, y, **kwargs)
        hit = self._at(int(x), int(y))
        before = (self.server, self.channel, self.focus, self.box_text())
        kind, arg = hit if hit else ("none", None)
        if kind == "server" and arg in SERVERS:
            self.server, self.channel, self.focus = arg, self.channels()[0], "box"
            self.box, self.popup = [], None
        elif kind == "channel":
            self.channel, self.focus, self.box, self.popup = arg, "box", [], None
        elif kind == "box":
            self.focus = "box"
        elif kind == "suggestion":
            self.selected = int(arg)
            self._accept()
        elif kind == "row":
            self.clicked_rows += 1
        changed = (self.server, self.channel, self.focus, self.box_text()) != before
        result.summary = (f"clicked at ({x},{y}); " + ("the window changed" if changed else "nothing changed")
                          + f"; title now \"{self.title(self.DISCORD)}\"")
        result.details = {"focus_changed": changed and before[2] != self.focus, "page_changed": before[:2] != (
            self.server, self.channel), "foreground_title_changed": before[:2] != (self.server, self.channel),
            "hwnd": self.DISCORD}
        if not any(result.details[k] for k in ("focus_changed", "page_changed")) and changed:
            result.details["focus_changed"] = True
        return result

    def _popup_facts(self) -> dict[str, Any] | None:
        if not self.popup:
            return None
        return {"open": True, "how": "controller_for", "items": [f"{m} {USERNAMES[m]}" for m in self.popup][:8],
                "selected": self.selected}

    def type_text(self, text: str, *, press_enter: bool = False, clear: bool = False, expect_hwnd: Any = None,
                  accept_suggestion: bool = False, **_: Any) -> StubResult:
        result = self._act("type_text", "", text)
        target = self.focus
        if clear:
            if target == "box":
                self.box, self.popup = [], None
            elif target == "switcher":
                self.switcher = ""
        paste = False
        reason = "short text"
        if _input is not None and not accept_suggestion:
            class _Target:
                ok, has_value, accepts_text = True, True, True
            paste, reason = _input._choose_method(text, _Target(), None)
        method = "clipboard" if paste else "keystrokes"
        details: dict[str, Any] = {"method": method, "method_reason": reason, "chars": len(text)}
        if target == "switcher":
            self.switcher = (self.switcher or "") + text
            details.update(readback="arrived", readback_text=self.switcher)
            result.summary = f"typed {len(text)} chars via {method} into Edit \"Quick switcher\"; field now contains {self.switcher!r}"
            if press_enter:
                self._switch_go()
                result.summary += f"; then pressed Enter; title now \"{self.title(self.DISCORD)}\""
            result.details = details
            return result
        if target != "box":
            result.ok, result.summary = False, "keyboard focus is not on a text field; nothing was typed"
            return result
        if self.box and self.box[-1][0] == "text":
            self.box[-1] = ("text", self.box[-1][1] + text)
        else:
            self.box.append(("text", text))
        self._refresh_popup(typed=not paste)
        if paste:
            self.pastes += 1
        details.update(readback="arrived", readback_text=self.box_text())
        parts = [f"typed {len(text)} chars via {method} into Edit \"Message #{self.channel}\" (takes text): {text!r}",
                 f"field now contains {self.box_text()!r}"]
        popup = self._popup_facts()
        if NEW_INPUT and popup:
            details["suggestions"] = popup
            if accept_suggestion:
                name = self._accept()
                details["suggestion_accepted"] = f"{name} {USERNAMES[name]}"
                details["readback_text"] = self.box_text()
                parts.append(f"a suggestion list opened ({len(popup['items'])} items); pressed Enter to accept "
                             f"the selected one, \"{name} {USERNAMES[name]}\"; the field now holds a mention "
                             f"element \"@{name}\"")
                popup = None
            else:
                parts.append(f"a suggestion list is open for this field ({len(popup['items'])} items: "
                             + ", ".join(f'"{i}"' for i in popup["items"][:5])
                             + f"; \"{popup['items'][popup['selected']]}\" selected): Enter or Tab would accept it")
        if press_enter:
            if NEW_INPUT and popup:
                parts.append("Enter was not pressed: it would accept the selected suggestion, not send")
            elif popup:
                self._accept()                  # Discord: Enter picks the suggestion (the old layer did not know)
                parts.append("then pressed Enter")
            else:
                self._send()
                parts.append("then pressed Enter")
        result.summary = "; ".join(parts)
        result.details = details
        return result

    def _switch_go(self) -> None:
        hits = self._switch_hits()
        if hits:
            kind, name = hits[0]
            if kind == "channel":
                chan, server = name[1:].split(" in ", 1)
                self.server, self.channel = server, chan
            else:
                self.server, self.channel = name, self.channels()[0]
        self.switcher, self.focus, self.box, self.popup = None, "box", [], None

    def _key(self, key: str) -> str:
        k = key.casefold()
        if self.switcher is not None:
            if k == "enter":
                self._switch_go()
                return "went to the top result"
            if k == "escape":
                self.switcher, self.focus = None, "box"
                return "closed the quick switcher"
            return "nothing changed"
        if k in ("enter", "tab") and self.popup:
            name = self._accept()
            return f"accepted the suggestion \"{name}\""
        if k == "enter" and self.focus == "box":
            self._send()
            return "sent the message" if self.sent else "nothing to send"
        if k == "escape":
            self.popup = None
            return "closed the popup"
        if k == "down" and self.popup:
            self.selected = min(self.selected + 1, len(self.popup) - 1)
            return f"selected \"{self.popup[self.selected]}\""
        if k == "up" and self.popup:
            self.selected = max(self.selected - 1, 0)
            return f"selected \"{self.popup[self.selected]}\""
        return "nothing changed"

    def press(self, key: str, *, times: int = 1, expect_hwnd: Any = None, **_: Any) -> StubResult:
        result = self._act("press", "", key)
        before = (self.server, self.channel, self.box_text(), len(self.sent))
        what = [self._key(key) for _ in range(max(1, int(times)))][-1]
        after = (self.server, self.channel, self.box_text(), len(self.sent))
        result.summary = f"pressed {key}: {what}; title now \"{self.title(self.DISCORD)}\""
        result.details = {"focus_changed": before != after, "page_changed": before[:2] != after[:2]}
        return result

    def hotkey(self, *keys: str, expect_hwnd: Any = None, **_: Any) -> StubResult:
        result = self._act("hotkey", "", *keys)
        combo = [k.casefold() for k in keys]
        if combo == ["ctrl", "k"]:
            self.switcher, self.focus = "", "switcher"
            result.summary = "pressed ctrl+k; focus now Edit \"Quick switcher\" (takes text)"
            result.details = {"focus_changed": True}
            return result
        if len(combo) == 1:
            return self.press(combo[0])
        result.summary = f"pressed {'+'.join(keys)}; nothing changed"
        result.details = {"focus_changed": False, "page_changed": False}
        return result

    def desk_state(self) -> dict[str, Any]:
        return {"server": self.server, "channel": self.channel, "sent": self.sent, "pastes": self.pastes,
                "clicked_rows": self.clicked_rows}


TASKS = {
    # tonight's third job: already in the channel
    "in_channel": dict(start=("Mafex's Hangout", "after-hours"),
                       request="Tag Hemanth in a message and ask when he will join the Discord VC",
                       heard=["Tag Hemanth in a message and ask when he'll join the VC."], said=["On it."]),
    # the whole intent in one hand-off (v3.15, item 3c)
    "full": dict(start=("Study Hall", "general"),
                 request="Message Hemanth in the after-hours channel on the Mafex's Hangout server asking "
                         "when he'll join the VC",
                 heard=["Message Hemanth in after-hours on the Mafex server asking when he'll join."],
                 said=["On it."]),
}


def _steps(log_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for path in log_dir.rglob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "llm_response":
                u = d.get("usage") or {}
                rows.append({"first_tool_ms": d.get("first_tool_ms"), "latency_ms": d.get("latency_ms"),
                             "input": (u.get("input_tokens") or 0), "cache_read": u.get("cache_read_input_tokens")})
            elif d.get("type") == "perception" and d.get("kind") in ("auto_view", "request_view"):
                rows.append({"view": d.get("kind"), "chars": d.get("size_chars")})
    return rows


def one(task: str, spec: dict, kwargs: dict) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="yuki-discord-"))
    settings = Settings(log_dir=tmp / "sessions", requests_csv=tmp / "requests.csv")
    memory = MemoryAccess(path=vs._empty_db(tmp))
    desk = DiscordDesk(server=spec["start"][0], channel=spec["start"][1])
    agent = Agent(settings, SessionLogger(settings.sessions_dir, console=None, session_id="bench-worker"),
                  backend=desk, lane="worker", memory=memory, prewarm=False)
    text = brain_request(spec["request"], spec["heard"], said=spec["said"])
    t0 = time.monotonic()
    tools, final, error = [], "", ""
    for ev in agent.run(text, origin_hwnd=desk.DISCORD, lean=True, view_hwnd=desk.DISCORD, **kwargs):
        if isinstance(ev, ToolCall):
            tools.append(ev.name)
        elif isinstance(ev, AskUser):
            agent.answer("Just do it.")
        elif isinstance(ev, Final):
            final = ev.text
        elif isinstance(ev, ErrorEvent):
            error = ev.text
        if time.monotonic() - t0 > 150:
            agent.cancel()
    wall = time.monotonic() - t0
    summary = agent.last_summary or {}
    state = desk.desk_state()
    sent = [m for m in state["sent"] if m["server"] == "Mafex's Hangout" and m["channel"] == "after-hours"]
    tagged = any(("mention", "Hemanth") in m["parts"] for m in sent)
    steps = _steps(tmp / "sessions")
    calls = [s for s in steps if "first_tool_ms" in s]
    views = [s["chars"] for s in steps if "view" in s]
    firsts = sorted(s["first_tool_ms"] for s in calls if s["first_tool_ms"] is not None)
    return {"task": task, "success": bool(sent) and tagged, "sent": len(sent), "tagged": tagged,
            "pastes": state["pastes"], "clicked_rows": state["clicked_rows"], "tools": tools,
            "steps": len([t for t in tools if t != "done"]), "wall_s": round(wall, 1), "calls": len(calls),
            "first_tool_ms_p50": firsts[len(firsts) // 2] if firsts else None,
            "first_tool_ms": [round(f) for f in firsts], "input_tokens": [s["input"] for s in calls],
            "view_chars": views, "escalated": (summary.get("escalated") or {}).get("reason"),
            "cost": summary.get("cost_usd"), "final": final[:120], "error": error[:120]}


def main(argv: list[str]) -> None:
    which = argv[0] if argv else "both"
    rounds = int(argv[1]) if len(argv) > 1 else 1
    s = Settings()
    kwargs: dict[str, Any] = {"effort": s.voice_brain_effort or None,
                              "thinking": None if s.voice_brain_thinking else False}
    if "voice" in inspect.signature(Agent.run).parameters:
        kwargs["voice"] = True
    if s.voice_brain_escalate_effort or s.voice_brain_escalate_thinking is not None:
        kwargs["escalate"] = (s.voice_brain_escalate_effort or None, s.voice_brain_escalate_thinking)
    print(f"input layer knows suggestion lists: {NEW_INPUT}; profile {kwargs}", flush=True)
    for task, spec in TASKS.items():
        if which not in ("both", task):
            continue
        for _ in range(rounds):
            print(json.dumps(one(task, spec, kwargs), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
