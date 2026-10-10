"""sWeb's test world: invented people, chats, capture threads and a stand-in desk (no real desktop, no real memory).

Every name below is invented. The desk is a stand-in for the voice hands (windows, a dispatcher, the judge), so the
real step runner (``yuki.skills.uia_route.SkillRunner``) runs the quick-switcher and typing steps against it. The
"live" plumbing of the drivers (focus, links, the Now board, the microphone, tab reads) is replaced per test; a guard
fixture makes the real one unreachable.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from yuki.memory import contacts as Ct
from yuki.memory.store import Store

NOW = time.time()
DAY = 86400.0

#: Invented names (never a real person's): asserted absent from every log line.
SECRETS = ("Vesper Lark", "vesper.lark", "Orrin Tal", "Juno Pike", "lantern-club", "Mira Dune", "Mira Solt",
           "otter cove", "see you at nine", "Thistle Hollow", "Quillon")


def msg(store: Store, thread_id: int, n: int, *, me: bool, at: float, text: str = "",
        sender: str | None = None) -> None:
    store._write(lambda conn: conn.execute(
        "INSERT INTO messages(thread_id, capture_id, fingerprint, content_key, sender_ciphertext, is_me, time_label,"
        " at, text_ciphertext, first_seen, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (thread_id, None, store.cipher.digest("fp", str(thread_id), str(n), text), store.cipher.digest("ck", str(n)),
         store._enc(sender), int(me), None, at, store._enc(text or f"line {n}"), at, "new")))


def thread(store: Store, app: str, title: str, url: str | None, process: str, scope: str, *, mine: int = 0,
           age_days: float = 1.0) -> int:
    tid = store.upsert_thread(app, title, url, process=process, scope=scope, kind="conversation")
    for n in range(mine):
        msg(store, tid, n, me=True, at=NOW - age_days * DAY - n * 60)
    store._write(lambda conn: conn.execute("UPDATE threads SET last_seen = ?, first_seen = ? WHERE id = ?",
                                           (NOW - age_days * DAY, NOW - 30 * DAY, tid)))
    return tid


def contact(store: Store, name: str, aliases: tuple[str, ...] = ()) -> int:
    return store._write(lambda conn: conn.execute(
        "INSERT INTO contacts(name_key, name_ciphertext, relation_ciphertext, aliases_ciphertext, tone_ciphertext,"
        " fact_ids, run_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (Ct.name_key(store, name), store.cipher.encrypt(name), None,
         store._enc(json.dumps([{"name": a, "how": "handle"} for a in aliases])), None, "[]", None, NOW - 9 * DAY,
         NOW - 9 * DAY)).lastrowid)


def route(store: Store, contact_id: int | None, service: str, kind: str, chat: str, handle: str | None,
          thread_ids: list[int]) -> int:
    return store._write(lambda conn: conn.execute(
        "INSERT INTO contact_routes(contact_id, route_key, service, kind, chat_ciphertext, handle_ciphertext,"
        " thread_ids, evidence, run_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (contact_id, Ct.route_key(store, service, chat), service, kind, store.cipher.encrypt(chat),
         store._enc(handle) if handle else None, json.dumps(thread_ids), "[]", None, NOW - 9 * DAY,
         NOW - 9 * DAY)).lastrowid)


def world(store: Store) -> dict[str, Any]:
    """Vesper: a busy Discord DM (with its address) and an older WhatsApp chat (a phone handle). Orrin: Slack only (an
    address). Juno: a Discord DM memory has no address for (the quick switcher). Two Miras (a question). A Discord
    server channel the user writes in (a place). Thistle: a WhatsApp chat with no phone handle (the search route)."""
    w: dict[str, Any] = {}
    w["vesper_dc"] = thread(store, "Discord", "@vesper.lark - Discord", "https://discord.com/channels/@me/111122223333",
                            "Discord.exe", "@vesper.lark", mine=40, age_days=0.5)
    w["vesper_wa"] = thread(store, "WhatsApp", "WhatsApp", None, "WhatsApp.Root.exe", "Vesper Lark", mine=4,
                            age_days=12)
    w["orrin_sl"] = thread(store, "Slack", "Orrin Tal (DM) - Slack", "https://app.slack.com/client/T0INVENT1/D0INVENT2",
                           "slack.exe", "Orrin Tal", mine=10, age_days=1)
    w["juno_dc"] = thread(store, "Discord", "@juno.pike - Discord", None, "Discord.exe", "@juno.pike", mine=8,
                          age_days=2)
    w["thistle_wa"] = thread(store, "WhatsApp", "WhatsApp", None, "WhatsApp.Root.exe", "Thistle Hollow", mine=6,
                             age_days=3)
    w["dune_dc"] = thread(store, "Discord", "@mira.dune - Discord", None, "Discord.exe", "@mira.dune", mine=3)
    w["solt_dc"] = thread(store, "Discord", "@mira.solt - Discord", None, "Discord.exe", "@mira.solt", mine=3)
    w["club_dc"] = thread(store, "Discord", "#lantern-club | Quillon - Discord",
                          "https://discord.com/channels/900000000000000001/900000000000000002", "Discord.exe",
                          "Quillon: lantern-club", mine=12, age_days=1)
    w["vesper"] = contact(store, "Vesper Lark", ("vesper.lark",))
    route(store, w["vesper"], "discord.com", "dm", "@vesper.lark", "vesper.lark", [w["vesper_dc"]])
    route(store, w["vesper"], "whatsapp.root", "dm", "Vesper Lark", "+44 7700 900123", [w["vesper_wa"]])
    w["orrin"] = contact(store, "Orrin Tal")
    route(store, w["orrin"], "app.slack.com", "dm", "Orrin Tal", None, [w["orrin_sl"]])
    w["juno"] = contact(store, "Juno Pike", ("juno.pike",))
    route(store, w["juno"], "discord.com", "dm", "@juno.pike", "juno.pike", [w["juno_dc"]])
    w["thistle"] = contact(store, "Thistle Hollow")
    route(store, w["thistle"], "whatsapp.root", "dm", "Thistle Hollow", None, [w["thistle_wa"]])
    w["dune"] = contact(store, "Mira Dune")
    route(store, w["dune"], "discord.com", "dm", "@mira.dune", None, [w["dune_dc"]])
    w["solt"] = contact(store, "Mira Solt")
    route(store, w["solt"], "discord.com", "dm", "@mira.solt", None, [w["solt_dc"]])
    route(store, None, "discord.com", "channel", "lantern-club", None, [w["club_dc"]])
    return w


def board_entry(store: Store, tid: int, *, hwnd: int, read_at: float, process: str) -> dict[str, Any]:
    """The Now board's entry for a window showing the capture thread ``tid`` (its stored app, title, address, scope)."""
    row = store._query("SELECT app, title_ciphertext, url_ciphertext, scope_ciphertext FROM threads WHERE id = ?",
                       (tid,))[0]
    return {"hwnd": hwnd, "foreground": True, "read_at": read_at, "app": row["app"], "process": process,
            "title": store._dec(row["title_ciphertext"]), "url": store._dec(row["url_ciphertext"]),
            "scope": store._dec(row["scope_ciphertext"]) or "", "messages": [{"sender": "x", "text": "y"}]}


class Desk:
    """A stand-in for the voice hands: windows, one dispatcher (recording every action), the judge, a text field with
    the keyboard. ``effects[("keys", "enter")]`` etc. run when that action is dispatched."""

    def __init__(self, windows: dict[int, dict[str, Any]], front: int, memory: Any = None) -> None:
        self.windows, self.front = windows, front
        self.memory = memory
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.judged: list[tuple[str, dict[str, Any]]] = []
        self.hold: dict[str, Any] = {}          # tool -> verdict that holds it
        self.effects: dict[Any, Any] = {}
        self._dispatcher = self
        self.act_off_for: str | None = None

    def overview_text(self) -> tuple[str, dict[str, Any], float]:
        return "", {"windows": [{"hwnd": h, "pid": 0, **w} for h, w in self.windows.items()],
                    "foreground_hwnd": self.front}, 0.0

    def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, dict(args)))
        key = args.get("element") if name == "click" else (name, "+".join(args.get("keys") or []))
        effect = self.effects.get(key)
        if effect is not None:
            effect()
        return SimpleNamespace(ok=True, payload={"details": {"verified": True}}, summary="done", elapsed_ms=1.0)

    def authorize(self, tool: str, tool_input: dict[str, Any], target: Any = None, earlier: Any = None, *,
                  force: bool = False) -> Any:
        self.judged.append((tool, dict(tool_input)))
        return self.hold.get(tool)

    def act_off(self, app: Any) -> str | None:
        return self.act_off_for

    def _now_board(self) -> dict[str, Any]:
        return {}

    def _read_tree(self, hwnd: int) -> Any:
        return {"elements": []}, 1, 1.0

    def _front(self, hwnd: int) -> str:
        self.front = hwnd
        return ""

    def _focus(self, timeout_s: float = 0.4) -> Any:
        return SimpleNamespace(accepts_text=True, is_password=False, window=self.front, role="Edit", name="Message")

    def snapshot(self) -> None:
        return None

    def keys(self) -> list[str]:
        return ["+".join(a.get("keys") or []) for n, a in self.calls if n == "hotkey"]

    def typed(self) -> list[str]:
        return [str(a.get("text")) for n, a in self.calls if n == "type_text"]


class Clock:
    """A stand-in clock: a wait's sleep moves it on."""

    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += float(s)


def open_store(tmp_path: Path) -> Store:
    return Store.open(tmp_path / "memory.db")
