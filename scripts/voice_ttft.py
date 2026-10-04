"""The talker's time to first token, A/B, with real Bedrock calls (docs/VOICE.md v3.14, item "win back the talker").

Builds the talker's request the way a conversation sends it (its system layers with a real profile and rules from a
COPY of memory, its tools, a few turns of conversation, the volatile block with a realistic desktop summary) for two
or more variants of the talker's code, and sends them interleaved (A, B, A, B, ...) so a busy PC or a slow Bedrock
minute hits every variant alike. Reports time to the first content block (what Pipecat's TTFB measures) and the
prompt's size (cached, written, uncached tokens) per variant.

Usage::

    .venv\\Scripts\\python.exe scripts/voice_ttft.py --db <COPY of memory.db> --variant now=. --variant v313=<worktree>
        [--rounds 10] [--desk items|plain]

A variant is ``name=<repo root>``: that tree's ``yuki/voice/mouth.py`` and ``yuki/voice/hands.py`` are loaded as
they are there; ``name=<repo root>@<file>`` also puts that file's text in the turn as its memory lookup block (the
talker's memory A/B: ``relevant`` facts against the whole-memory ``related`` block). Memory is read from the copy only; nothing touches the desktop.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHROME = 12291
DESK = ('screen space 3840x1080 @0,0 (2 screens) | cursor (960, 540)\n'
        f'[{CHROME}] chrome.exe "(1558) YouTube - Google Chrome" @0,0 1920x1040 FOREGROUND MAXIMIZED (not full screen)\n'
        '[2164770] warp.exe "LLM OS agent harness revamp" @1920,0 1920x1040\n'
        '[66830] Discord.exe "#general - Discord" @1920,0 1920x1040 (minimised)\n'
        'media: Chrome "Make Up My Mind" by Stellar Blade OST paused (media keys act on this one)\n'
        'app volumes: chrome.exe 100%\n'
        f'tabs of [{CHROME}] (3, * = the one showing): 1 "Inbox (12) - Corvana Mail"; 2 "Claude"; 3* "(1558) YouTube"\n'
        'names on the screen in Google Chrome (items, chats, links, buttons you can see, or for an app that shows '
        'none, its recent tab titles; their words may be a mishearing of one): Home; Shorts; Subscriptions; You; '
        'History; Mix - Make Up My Mind; Joji - Glimpse of Us; lofi hip hop radio; Stellar Blade OST')
ITEMS = [("Mix - Make Up My Mind - Stellar Blade: BLOOD RAIN Official Trailer OST", "link"),
         ("Joji - Glimpse of Us (Official Video)", "link"), ("lofi hip hop radio - beats to relax/study to", "link"),
         ("Stellar Blade OST - Full Soundtrack", "link"), ("Joji - SLOW DANCING IN THE DARK (Official Video)", "link"),
         ("Search", "button"), ("Voice search", "button"), ("Create", "button"), ("Notifications", "button"),
         ("Home", "link"), ("Shorts", "link"), ("Subscriptions", "link")]
TURNS = [("user", "Open YouTube."), ("assistant", "YouTube's up."), ("user", "Scroll down."),
         ("assistant", "Mm-hm."), ("user", "What time is it?"), ("assistant", "It's twenty past nine.")]
WORDS = "Play the make up my mind mix."


def load(root: Path, name: str) -> tuple[Any, Any]:
    """That tree's mouth and hands modules, loaded under their own names."""
    mods = []
    for part in ("mouth", "hands"):
        spec = importlib.util.spec_from_file_location(f"{name}_{part}", root / "yuki" / "voice" / f"{part}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        mods.append(mod)
    return mods[0], mods[1]


def _supported(fn: Any, **kwargs: Any) -> dict[str, Any]:
    """The keyword arguments ``fn`` takes (an older tree's mouth lacks the newer ones)."""
    import inspect

    params = inspect.signature(fn).parameters
    return {k: v for k, v in kwargs.items() if k in params and v is not None}


def request(mouth: Any, hands: Any, memory: Any, with_items: bool, lookup: str = "", history: int = 1, *,
            channel: str = "marker", call: str | None = None, state: dict[str, Any] | None = None) -> dict[str, Any]:
    from yuki.voice.pipeline import async_tool_instructions

    system = mouth.mouth_system_blocks(memory, tail=[async_tool_instructions()],
                                       **_supported(mouth.mouth_system_blocks, channel=channel, call=call,
                                                    state=state))
    tools = []
    extra = []
    if hasattr(mouth, "fetch_recent"):   # vfix4: that tree's talker has its quick memory tool (and the Earlier block)
        from yuki.voice.recall import QUICK_MEMORY

        extra.append(QUICK_MEMORY)
    for t in (mouth.TELL_YUKI, mouth.CHECK_PROGRESS, mouth.MORE_OF_RESULT, mouth.STAY_QUIET, mouth.END_CONVERSATION,
              mouth.GO_QUIET, *extra, *hands.HAND_TOOLS):
        tools.append({"name": t["name"], "description": t["description"],
                      "input_schema": {"type": "object", "properties": t["properties"], "required": t["required"]}})
    desk = DESK
    if with_items and hasattr(hands, "items_line"):
        items = hands.ScreenItems(hwnd=CHROME, title="", read=4, at=0.0, version=0,
                                  items=[{"id": i, "role": "Hyperlink" if kind == "link" else "Button", "name": n}
                                         for i, (n, kind) in enumerate(ITEMS)])
        desk += "\n" + hands.items_line(items)
        desk += f"\nfocus: the text field Edit \"Search\" in [{CHROME}] has the keyboard (your type hand types there)"
    # ``history``: the short conversation said that many times over (a conversation some turns in, v3.14b)
    messages = [{"role": r, "content": [{"type": "text", "text": t}]} for _ in range(max(1, history)) for r, t in TURNS]
    # Pipecat's breakpoints on the last two user messages (the moving conversation cache)
    for m in [m for m in messages if m["role"] == "user"][-1:]:
        m["content"][-1]["cache_control"] = {"type": "ephemeral"}
    if channel == "marker" and hasattr(mouth, "with_markers"):
        messages = mouth.with_markers(messages)   # its earlier replies as the marker channel writes them (v3.16)
    messages.append({"role": "user", "content": [{"type": "text", "text": WORDS},
                                                 mouth.volatile_block(**_supported(
                                                     mouth.volatile_block, desk=desk, lookup=lookup, call=call,
                                                     channel=channel, state=state))]})
    return {"system": system, "tools": tools, "messages": messages}


async def one(client: Any, model: str, req: dict[str, Any]) -> dict[str, Any]:
    t0 = time.monotonic()
    first = None
    async with client.messages.stream(model=model, max_tokens=300, thinking={"type": "disabled"}, **req) as stream:
        async for ev in stream:
            if first is None and ev.type in ("content_block_start", "content_block_delta"):
                first = time.monotonic() - t0
        final = await stream.get_final_message()
    u = final.usage
    return {"ttft_ms": round((first or 0) * 1000), "total_ms": round((time.monotonic() - t0) * 1000),
            "uncached": u.input_tokens, "cache_read": u.cache_read_input_tokens,
            "cache_write": u.cache_creation_input_tokens,
            "tools": [b.name for b in final.content if b.type == "tool_use"]}


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True, help="a COPY of memory.db")
    ap.add_argument("--variant", action="append", required=True,
                    help="name=<repo root>[@<text file>]: the file is the turn's memory lookup block (v3.14)")
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--desk", choices=("items", "plain"), default="items")
    ap.add_argument("--dump", type=Path, default=None, help="every call as a JSON line (variant, ttft, tokens)")
    ap.add_argument("--history", type=int, default=1, help="the six-message conversation repeated this many times")
    ap.add_argument("--channel", choices=("marker", "guard"), default="marker", help="the speech channel (v3.16)")
    ap.add_argument("--call", default="", help="an app name: the request is built in call mode")
    ap.add_argument("--tokens-only", dest="tokens_only", action="store_true",
                    help="one call per variant (max_tokens 1): the prompt's size only")
    args = ap.parse_args(argv)

    from yuki.agent.memory import MemoryAccess
    from yuki.config import Settings
    from yuki.voice.pipeline import bedrock_client

    s = Settings()
    client = bedrock_client(s.aws_region, getattr(s, "model_backend", None))
    variants = []
    for spec in args.variant:
        name, root = spec.split("=", 1)
        root, _, lookup_file = root.partition("@")
        lookup = Path(lookup_file).read_text(encoding="utf-8") if lookup_file else ""
        mouth, hands = load(Path(root).resolve(), name)
        memory = mouth.fetch_memory(MemoryAccess(path=args.db))
        variants.append((name, request(mouth, hands, memory, args.desk == "items", lookup, args.history,
                                       channel=args.channel, call=args.call or None)))
    if args.tokens_only:
        for name, req in variants:
            response = await client.messages.create(model=s.voice_mouth_model, max_tokens=1, **req)
            u = response.usage
            total = u.input_tokens + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
            print(json.dumps({"variant": name, "total_input": total, "cache_read": u.cache_read_input_tokens,
                              "cache_write": u.cache_creation_input_tokens, "uncached": u.input_tokens,
                              "system_chars": sum(len(b.get("text", "")) for b in req["system"]),
                              "tools_chars": len(json.dumps(req["tools"]))}))
        return 0
    for name, req in variants:   # write each variant's cache first (a conversation's warm-up does the same)
        await one(client, s.voice_mouth_model, req)
    rows: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in variants}
    for _ in range(args.rounds):
        for name, req in variants:
            row = await one(client, s.voice_mouth_model, req)
            rows[name].append(row)
            if args.dump is not None:
                with open(args.dump, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"variant": name, "at": time.time(), **row}) + "\n")
    for name, rs in rows.items():
        t = sorted(r["ttft_ms"] for r in rs)
        last = rs[-1]
        print(json.dumps({"variant": name, "ttft_p50": t[len(t) // 2], "ttft_p90": t[int(len(t) * 0.9)],
                          "ttft_min": t[0], "cache_read": last["cache_read"], "uncached": last["uncached"],
                          "cache_write": last["cache_write"], "tools": [r["tools"] for r in rs][:3]}))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
