"""vfix text replays of the talker (report 40, V1-V12): the turns that went wrong on 3 Oct, re-told with invented names.

Nothing opens a device or touches the desktop: text in, text out, on the real talker model on Bedrock, each request built
the way a conversation builds it (the cached layers, the real tool schemas, the conversation with its markers, the
volatile block with a stand-in desktop), read by the same speech channel the pipeline uses. Every case has a check on
what the talker did (which tool, which arguments, what script it spoke in); the spoken words are printed for review.

    .venv\\Scripts\\python.exe scripts\\voice_vfix_replay.py [--reps 3] [--variant new|head] [--budget 1.0]
        [--cases FILE] [--only id,id] [--out DIR]

``--variant head`` runs the same cases on the talker of the commit this branch started from (``git show HEAD~N``: the
prompt, the tool schemas and the speech channel of ``--head-ref``, default ``main``). ``--cases FILE``: extra cases in
the same JSON shape (kept outside the repo; the real turns stay private). Writes ``<out>/vfix-replay-<variant>-<time>.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yuki.config import Settings  # noqa: E402

DESK = """screen space 1920x1080 @0,0 (one screen) | cursor (1135, 20)
[12291] chrome.exe "Home - VideoSite - Google Chrome" @0,0 1920x1040 FOREGROUND
[8194] Slack.exe "general | Team - Slack" @0,0 1920x1040
[40112] Telegram.exe "Telegram" @0,0 1920x1040
media: Chrome VideoSite "Lo-fi mix" paused (media keys act on this one)
app volumes (Windows volume mixer, each app's own level): chrome.exe 100%; Slack.exe 70%
tabs of [12291] (2, * = the one showing): 1 "Inbox (3) - Mail"; 2* "Home - VideoSite"
items on the screen in [12291], numbered for your click hand (page content first): 1 "Ocean drive - 4 minutes" link; \
2 "Night walk - 7 minutes" link; 3 "Home" link"""

DESK_NO_ARC = """screen space 1920x1080 @0,0 (one screen) | cursor (1135, 20)
[8194] Slack.exe "general | Team - Slack" @0,0 1920x1040 FOREGROUND
media: none"""

DESK_ARC_VIDEO = """screen space 1920x1080 @0,0 (one screen) | cursor (1135, 20)
[264022] Arc.exe "Arc" @0,0 1920x1040 FOREGROUND
media: Arc VideoSite "Guess the creator - VideoSite" playing (media keys act on this one)
items on the screen in [264022], numbered for your click hand (page content first): 1 "Guess the creator" link; \
2 "VideoSite Home" link; 3 "Subscribe" button"""

PERSONALITY = ("Personality: casual and quick, swears and banters, mixes Hindi and English (Hinglish), often on calls "
               "with friends; likes short, plain answers with no fluff.")
PEOPLE = ("Kabir (friend): Slack DM 'kabir.r' (usual, most used lately); Telegram 'Kabir'.\n"
          "Meera (cousin): Telegram 'Meera' (usual).")


def u(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def a(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def call(name: str, args: dict[str, Any], cid: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"type": "tool_use", "id": cid, "name": name, "input": args}]}


def result(cid: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid, "content": json.dumps(payload)}]}


def cases() -> list[dict[str, Any]]:
    """id, issue, check, messages, and optional desk / call / work / cut / state / heard / after_goodbye."""
    chat = [u("what's up?"), a("Not much, you?"), u("just fixing some bugs"), a("Nice, good luck with them.")]
    c: list[dict[str, Any]] = [
        # V1 - shut up / go away end the conversation, with no words
        dict(id="shut_up", issue="V1", check="end_quiet", messages=[*chat, u("Shut up.")]),
        dict(id="shut_up_after_done", issue="V1", check="end_quiet", messages=[
            u("click the night walk video"), call("click", {"window": 12291, "item": 2, "named": "the night walk video",
                                                            "say_when_done": "Done."}, "k1"),
            result("k1", {"done": "clicked", "said": "Done."}), a("Done."), u("Ugh, enough already. Shut up.")]),
        dict(id="go_away_after_bye", issue="V1", check="end_quiet", messages=[
            u("never mind, go back to sleep"), a("No worries, I'm around."), u("bye bye"), a("Bye, take care!"),
            u("Go away.")]),
        dict(id="go_away_dude", issue="V2", check="end_quiet", messages=[*chat, u("Leave me alone for now, man.")]),
        dict(id="said_go_away", issue="V2", check="end_quiet", messages=[
            *chat, u("go away"), a("I'm still here, what do you need?"), u("Man, I told you to go away.")]),
        # V3 - an accidental wake, a goodbye
        dict(id="accidental_wake", issue="V3", check="end_any", messages=[
            u("Oh, it's nothing, sorry."), a("No worries. You good?"),
            u("Yep, wrong name at the wrong time. All fine, you can sleep again.")]),
        dict(id="bye_bye", issue="V3", check="end_any", messages=[*chat, u("Bye bye.")]),
        dict(id="goodbye", issue="V3", check="end_any", messages=[*chat, u("Yeah. Goodbye.")]),
        # controls: still quiet for dictation and for the call; go_quiet only for stop listening for a while
        dict(id="dictation", issue="V2 control", check="quiet", messages=[u(
            "so like make it as a document so that after the compaction this will stay in your memory")]),
        dict(id="call_banter", issue="V2 control", check="quiet", call="Discord", messages=[u("bro did you see that clutch")]),
        dict(id="call_shut_up", issue="V1", check="end_quiet", call="Discord", messages=[u("Yuki, shut up.")]),
        dict(id="stop_listening_2h", issue="V1 control", check="go_quiet_120", messages=[*chat, u("stop listening for two hours")]),
        # V2: with the named refusal gone from code, words right after the name are never left in silence
        dict(id="named_pause", issue="V2 named", check="not_quiet", named=True, messages=[u("Pause the music.")]),
        dict(id="named_call_open", issue="V2 named", check="not_quiet", named=True, call="Discord",
             messages=[u("Open Slack.")]),
        dict(id="named_call_time", issue="V2 named", check="not_quiet", named=True, call="Discord",
             messages=[u("What time is it?")]),
        dict(id="leave_alone_for_now", issue="V1", check="end_any", messages=[*chat, u("Leave me alone for now.")]),
        dict(id="thanks", issue="V2 control", check="not_quiet", messages=[
            u("pause the music"), call("media_key", {"key": "playpause", "want": "paused", "say_when_done": "Paused."},
                                       "m1"), result("m1", {"done": "media: Chrome now paused", "said": "Paused."}),
            a("Paused."), u("cool, thanks")]),
        # V4 - the app they named wins over the usual one, even when the message is about the usual one
        dict(id="named_app", issue="V4", check="app_named_telegram", messages=[
            u("can you check my video home page and get me some recommendations?"),
            u("then text Kabir on Telegram, ask him when he's getting on Slack today"),
            u("since we have to sort out the plan."),
            u("Nah, just ask when he'll be on Slack, we have to sort out the plan. That's it.")]),
        # V6(a) - never claim a step the thinker has not reported
        dict(id="no_claim", issue="V6", check="no_tool_short", work='task 5 "Text Kabir on Telegram asking when he '
             'will be on Slack, then scroll my video recommendations until I say stop" running for 22 s, now: '
             'typing a message', messages=[
                 u("text Kabir on Telegram and then keep scrolling my recommendations"),
                 call("tell_yuki", {"screen": False, "say_first": "On it.", "app_named": "Telegram",
                                    "request": "Text Kabir on Telegram, then scroll my recommendations until I say "
                                               "stop", "acts": True, "intent": "new_task"}, "t9"),
                 result("t9", {"status": "started"}), u("Nothing is moving on the page, man.")]),
        # V7 - keep scrolling until I say stop, and stop
        dict(id="keep_scrolling", issue="V7", check="keep_start", messages=[u("keep scrolling down until I say stop")]),
        dict(id="stop_scrolling", issue="V7", check="keep_stop",
             state={"scrolling": 'Chrome "Home - VideoSite", down, normal pace, 7 steps in 12 s'},
             messages=[u("keep scrolling down until I say stop"),
                       call("scroll", {"window": 12291, "direction": "down", "keep": "start", "say_when_done": ""},
                            "s1"),
                       result("s1", {"done": "scrolling down by itself now", "said": ""}), u("okay stop")]),
        dict(id="scrolling_nice", issue="V7", check="no_stop",
             state={"scrolling": 'Chrome "Home - VideoSite", down, normal pace, 4 steps in 7 s'},
             messages=[u("keep scrolling until I say stop"),
                       call("scroll", {"window": 12291, "direction": "down", "keep": "start", "say_when_done": ""},
                            "s2"),
                       result("s2", {"done": "scrolling down by itself now", "said": ""}), u("nice, nice")]),
        # V9 - after a cut, never the same line again
        dict(id="repeat_after_cut", issue="V9", check="no_repeat",
             cut={"heard": "Thanks. So you're sorting out how the settings page", "unsaid":
                  "handles each mode, piece by piece. It's coming along well."},
             previous="Thanks. So you're sorting out how the settings page handles each mode, piece by piece. It's "
                      "coming along well.",
             messages=[u("good job"), a("Thanks. So you're sorting out how the settings page handles each mode, piece "
                                        "by piece. It's coming along well."), u("Mm-hm.")]),
        dict(id="repeat_hello", issue="V9", check="no_repeat", cut={"heard": "", "unsaid": "Hey, how's it going?"},
             previous="Hey, how's it going?", messages=[u("Hello."), a("Hey, how's it going?"), u("Yeah.")]),
        # V10 - Roman script whatever the transcript's script
        dict(id="devanagari_name", issue="V10", check="roman", named=True, messages=[u("यूकी")]),
        dict(id="devanagari_question", issue="V10", check="roman", messages=[*chat, u("और बताओ, क्या चल रहा है?")]),
        # V11 - open the browser and the site in it: both parts
        dict(id="open_arc_youtube", issue="V11", check="open_both", desk=DESK_NO_ARC,
             messages=[u("Start Arc and put YouTube on in it.")]),
        dict(id="then_after_open", issue="V11", check="then_done", desk=DESK_ARC_VIDEO, messages=[
            u("Start Arc and put YouTube on in it."),
            call("open_app", {"say_first": "Opening Arc.", "name": "Arc", "then": "open YouTube in it",
                              "say_when_done": ""}, "o1"),
            result("o1", {"done": "launched 'Arc': hwnd 264022 (Arc.exe) \"Arc\"; in the foreground",
                          "then": "open YouTube in it",
                          "how_to_handle_it": "__THEN_NOTE__"})]),
        # V12 - short, no commentary on their work, no praise
        dict(id="whats_up", issue="V12", check="short", messages=[u("Yuki."), a("Yeah, what's up?"), u("What's up?")]),
        dict(id="good_job", issue="V12", check="short", messages=[
            u("what do you see on my screen?"), a("A design mockup of an app's state screens, and your code editor "
                                                  "on the other monitor."), u("Good job.")]),
    ]
    return c


# ---------------------------------------------------------------------------------------------------------------
# the talker variant
# ---------------------------------------------------------------------------------------------------------------


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def talker(variant: str, ref: str) -> tuple[Any, Any]:
    """``(mouth module, hands module)``: this tree's, or ``ref``'s (read with git show, loaded under other names)."""
    if variant == "new":
        import yuki.voice.hands as hands
        import yuki.voice.mouth as mouth

        return mouth, hands
    tmp = Path(tempfile.mkdtemp(prefix="vfix-head-"))
    for rel in ("yuki/voice/mouth.py", "yuki/voice/hands.py"):
        text = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", check=True).stdout
        (tmp / Path(rel).name).write_text(text, encoding="utf-8")
    return _load("vfix_head_mouth", tmp / "mouth.py"), _load("vfix_head_hands", tmp / "hands.py")


def tools_for(mouth: Any, hands: Any) -> list[dict[str, Any]]:
    out = []
    for t in (mouth.TELL_YUKI, mouth.CHECK_PROGRESS, mouth.MORE_OF_RESULT, mouth.STAY_QUIET, mouth.END_CONVERSATION,
              mouth.GO_QUIET, *hands.HAND_TOOLS):
        out.append({"name": t["name"], "description": t["description"],
                    "input_schema": {"type": "object", "properties": t["properties"], "required": t["required"]}})
    out.append({"name": "cancel_tell_yuki", "description": mouth.CANCEL_TELL_YUKI_DESCRIPTION,
                "input_schema": {"type": "object", "properties": {"tool_call_id": {"type": "string"}}, "required": []}})
    return out


# ---------------------------------------------------------------------------------------------------------------
# one reply and its check
# ---------------------------------------------------------------------------------------------------------------


def _routed_first(request: str, want: str, usual: str) -> bool:
    """The request sends on ``want`` ("... on Telegram ...") before any mention of ``usual`` (which may be what the
    message is about: "... on Telegram asking when he's getting on Slack")."""
    at = request.find(f"on {want}")
    if at < 0:
        return False
    other = request.find(f"on {usual}") if usual else -1
    return other < 0 or at < other


def _devanagari(text: str) -> bool:
    return any("ऀ" <= ch <= "ॿ" for ch in text or "")


def check(case: dict[str, Any], row: dict[str, Any]) -> bool:
    """Whether the talker did what the case wants (tool names and arguments, the script it spoke in). An eval's own
    scoring of the model's output, never a rule inside Yuki."""
    calls = row["calls"]
    names = [c["name"] for c in calls]
    first = calls[0]["input"] if calls else {}
    spoken = row["spoken"]
    kind = case["check"]
    if kind == "end_quiet":
        return names == ["end_conversation"] and not str(first.get("say_first") or "").strip() and not spoken
    if kind == "end_any":
        return "end_conversation" in names and "go_quiet" not in names
    if kind == "quiet":   # the stay_quiet call, or a quiet marker alone (the pipeline takes both as silence)
        return (names == ["stay_quiet"] or (not names and row.get("marker") == "quiet")) and not spoken
    if kind == "not_quiet":
        return "stay_quiet" not in names and bool(spoken or names)
    if kind == "go_quiet_120":
        return names == ["go_quiet"] and int(first.get("minutes") or 0) == 120
    if kind == "app_named_telegram" or kind.startswith("app_named:"):
        want = "telegram" if kind == "app_named_telegram" else kind.split(":", 1)[1].casefold()
        usual = "slack" if kind == "app_named_telegram" else str(case.get("usual") or "").casefold()
        tells = [c["input"] for c in calls if c["name"] == "tell_yuki"]
        if not tells:
            return False
        # the hand-off that carries the message names the app they said (another for the rest of the request may name
        # none), and no hand-off routes the message through the usual app
        for tell in tells:
            request = str(tell.get("request") or "").casefold()
            if want in request and str(tell.get("app_named") or "").casefold() != want:
                return False
            if usual and f"message {usual}" in request:
                return False
        return any(str(t.get("app_named") or "").casefold() == want
                   and _routed_first(str(t.get("request") or "").casefold(), want, usual) for t in tells)
    if kind == "no_tool_short":
        return not names and len(spoken.split()) <= 25
    if kind == "keep_start":
        return names == ["scroll"] and first.get("keep") == "start"
    if kind == "keep_stop":
        return names == ["scroll"] and first.get("keep") == "stop"
    if kind == "no_stop":
        return all(c["input"].get("keep") != "stop" for c in calls)
    if kind == "no_repeat":
        return bool(spoken or names) and " ".join(spoken.split()) != " ".join(str(case.get("previous") or "").split())
    if kind == "roman":
        return bool(spoken or names) and not _devanagari(spoken)
    if kind == "open_both":
        for c in calls:
            i = c["input"]
            if c["name"] == "open_app" and "arc" in str(i.get("name") or "").casefold() and str(i.get("then") or "").strip():
                return True
            if c["name"] == "open_url" and "youtube" in str(i.get("url") or "") and "arc" in str(i.get("app") or "").casefold():
                return True
            if c["name"] == "tell_yuki" and "youtube" in str(i.get("request") or "").casefold():
                return True
        return False
    if kind == "then_done":
        for c in calls:
            i = c["input"]
            if c["name"] == "open_url" and "youtube" in str(i.get("url") or ""):
                return True
            if c["name"] == "click" and int(i.get("item") or 0) == 2:
                return True
            if c["name"] == "tell_yuki" and "youtube" in str(i.get("request") or "").casefold():
                return True
        return False
    if kind == "short":
        return not names and 0 < len(spoken.split()) <= 8
    return False


async def one(client: Any, settings: Settings, case: dict[str, Any], mouth: Any, hands: Any,
              tools: list[dict[str, Any]], memory: Any) -> dict[str, Any]:
    from yuki.voice.pipeline import async_tool_instructions, with_volatile

    app = case.get("call")
    if case.get("people"):   # a case of its own people lines (a private case file's)
        memory = mouth.MouthMemory(source="memory", names=list(memory.names or []), personality=memory.personality,
                                   people=case["people"], standing=memory.standing)
    system = mouth.mouth_system_blocks(memory, tail=[async_tool_instructions()], call=app,
                                       call_evidence="fresh" if app else None, channel="marker")
    messages = []
    for m in case["messages"]:
        if m["role"] == "user" and isinstance(m["content"], list):
            blocks = []
            for b in m["content"]:
                if b.get("type") == "tool_result" and "__THEN_NOTE__" in str(b.get("content")):
                    payload = json.loads(b["content"])
                    payload["how_to_handle_it"] = str(getattr(hands, "_THEN_NOTE", "")).format(
                        rest=payload.get("then") or "")
                    b = {**b, "content": json.dumps(payload)}
                blocks.append(b)
            m = {**m, "content": blocks}
        messages.append(m)
    messages = mouth.with_markers(messages)
    desk = {"__NO_ARC__": DESK_NO_ARC, "__ARC_VIDEO__": DESK_ARC_VIDEO}.get(case.get("desk"), case.get("desk"))
    kw: dict[str, Any] = dict(desk=desk or DESK, call=app, heard=case.get("heard"), channel="marker",
                              work=case.get("work"), named=bool(case.get("named")),
                              after_goodbye=bool(case.get("after_goodbye")), cut=case.get("cut"))
    try:
        block = mouth.volatile_block(state=case.get("state"), **kw)
    except TypeError:
        block = mouth.volatile_block(**kw)
    messages = with_volatile(messages, block)
    reader_kw: dict[str, Any] = {}
    if case.get("previous") and "repeat_of" in mouth.ReplyChannel.__init__.__code__.co_varnames:
        reader_kw = {"repeat_of": case["previous"], "heard": (case.get("cut") or {}).get("heard") or case["previous"]}
    reader = mouth.ReplyChannel(cap_words=int(getattr(settings, "voice_reply_words_cap", 0) or 0), **reader_kw)
    started = time.monotonic()
    text, spoken = "", ""
    calls: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    stream = await client.messages.create(model=settings.voice_mouth_model, max_tokens=settings.voice_mouth_max_tokens,
                                          system=system, tools=tools, messages=messages, stream=True)
    async for event in stream:
        kind = getattr(event, "type", "")
        if kind == "message_start":
            u_ = event.message.usage
            usage["input_tokens"] += int(getattr(u_, "input_tokens", 0) or 0)
            usage["cache_read_tokens"] += int(getattr(u_, "cache_read_input_tokens", 0) or 0)
            usage["cache_write_tokens"] += int(getattr(u_, "cache_creation_input_tokens", 0) or 0)
        elif kind == "message_delta":
            usage["output_tokens"] += int(getattr(getattr(event, "usage", None), "output_tokens", 0) or 0)
        elif kind == "content_block_start" and getattr(event.content_block, "type", "") == "tool_use":
            current = {"name": event.content_block.name, "json": ""}
            calls.append(current)
        elif kind == "content_block_delta" and hasattr(event.delta, "partial_json") and current is not None:
            current["json"] += event.delta.partial_json or ""
        elif kind == "content_block_delta" and hasattr(event.delta, "text"):
            text += event.delta.text
            spoken += reader.feed(event.delta.text)
    spoken += reader.finish() or ""
    for c in calls:
        try:
            c["input"] = json.loads(c.pop("json") or "{}")
        except ValueError:
            c["input"] = {}
    cost = settings.estimate_cost(settings.voice_mouth_model, usage) or 0.0
    row = {"id": case["id"], "issue": case["issue"], "check": case["check"], "text": text, "spoken": spoken.strip(),
           "calls": calls, "repeat_dropped": bool(getattr(reader, "repeat", False)), "usage": usage,
           "marker": getattr(reader, "first", None),
           "cost_usd": round(cost, 6), "ms": round((time.monotonic() - started) * 1000)}
    row["ok"] = check(case, row)
    return row


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    from yuki.voice.pipeline import bedrock_client

    settings = Settings()
    mouth, hands = talker(args.variant, args.head_ref)
    client = bedrock_client(settings.aws_region, getattr(settings, "model_backend", None))
    memory = mouth.MouthMemory(source="memory", names=["Dev"], personality=PERSONALITY, people=PEOPLE,
                               standing="- Keep replies short and plain.")
    tools = tools_for(mouth, hands)
    plan = cases()
    if args.cases:
        plan += json.loads(Path(args.cases).read_text(encoding="utf-8"))
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        plan = [c for c in plan if c["id"] in wanted]
    runs = [c for c in plan for _ in range(args.reps)]
    spent = {"usd": 0.0}
    rows: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(args.parallel)

    async def run(case: dict[str, Any]) -> None:
        async with sem:
            if spent["usd"] >= args.budget:
                return
            for attempt in range(3):
                try:
                    row = await one(client, settings, case, mouth, hands, tools, memory)
                    break
                except Exception as exc:  # noqa: BLE001 - throttled: back off
                    if attempt == 2:
                        rows.append({"id": case["id"], "issue": case["issue"], "error": f"{type(exc).__name__}: {exc}"})
                        return
                    await asyncio.sleep(2.0 * (attempt + 1))
            spent["usd"] += row["cost_usd"]
            rows.append(row)
            names = ",".join(c["name"] for c in row["calls"]) or "-"
            args_ = row["calls"][0]["input"] if row["calls"] else {}
            short = {k: args_.get(k) for k in ("say_first", "app_named", "request", "action", "keep", "minutes", "then",
                                               "url", "name") if k in args_}
            print(f"{'OK ' if row['ok'] else 'BAD'} {row['id']:<22} tools={names:<20} {json.dumps(short)[:150]} "
                  f"spoken={row['spoken'][:90]!r}", flush=True)

    started = time.monotonic()
    if runs:
        await run(runs[0])   # the first call writes the cache
    await asyncio.gather(*(run(c) for c in runs[1:]))
    by_case: dict[str, dict[str, Any]] = {}
    for r in rows:
        if "error" in r:
            continue
        s = by_case.setdefault(r["id"], {"issue": r["issue"], "n": 0, "ok": 0})
        s["n"] += 1
        s["ok"] += int(r["ok"])
    by_issue: dict[str, dict[str, int]] = {}
    for s in by_case.values():
        t = by_issue.setdefault(s["issue"], {"n": 0, "ok": 0})
        t["n"] += s["n"]
        t["ok"] += s["ok"]
    reads = sorted(r["usage"]["cache_read_tokens"] + r["usage"]["cache_write_tokens"] for r in rows if "usage" in r)
    return {"variant": args.variant, "rows": rows, "by_case": by_case, "by_issue": by_issue,
            "ok": sum(int(r.get("ok", False)) for r in rows), "n": len(rows),
            "cached_prefix_tokens_p50": reads[len(reads) // 2] if reads else None,
            "errors": sum(1 for r in rows if "error" in r), "usd": round(spent["usd"], 4),
            "seconds": round(time.monotonic() - started, 1)}


def rescore(path: Path, extra: str = "") -> int:
    """Score an earlier run again with today's checks (the replies as they were; no model call)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    plan = cases() + (json.loads(Path(extra).read_text(encoding="utf-8")) if extra else [])
    by_id = {c["id"]: c for c in plan}
    by_case: dict[str, dict[str, Any]] = {}
    for row in data["rows"]:
        if "error" in row or row["id"] not in by_id:
            continue
        if row.get("marker") is None:
            head = (row.get("text") or "").lstrip().split("\n", 1)[0].strip().lower().rstrip(":")
            row["marker"] = head if head in ("say", "act", "quiet") else None
        row["ok"] = check(by_id[row["id"]], row)
        s = by_case.setdefault(row["id"], {"issue": row["issue"], "n": 0, "ok": 0})
        s["n"] += 1
        s["ok"] += int(row["ok"])
    by_issue: dict[str, dict[str, int]] = {}
    for s in by_case.values():
        t = by_issue.setdefault(s["issue"], {"n": 0, "ok": 0})
        t["n"] += s["n"]
        t["ok"] += s["ok"]
    print(json.dumps({"file": str(path), "variant": data.get("variant"), "ok": sum(s["ok"] for s in by_case.values()),
                      "n": sum(s["n"] for s in by_case.values()), "by_issue": by_issue, "by_case": by_case}, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--variant", choices=("new", "head"), default="new")
    ap.add_argument("--head-ref", default="main")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--budget", type=float, default=1.0)
    ap.add_argument("--cases", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "yuki-vfix")
    ap.add_argument("--rescore", default="", help="score an earlier run's file again with the checks here (no calls)")
    args = ap.parse_args(argv)
    if args.rescore:
        return rescore(Path(args.rescore), args.cases)
    run = asyncio.run(main_async(args))
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"vfix-replay-{args.variant}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(run, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: run[k] for k in ("variant", "ok", "n", "errors", "usd", "seconds", "cached_prefix_tokens_p50",
                                          "by_issue")}, indent=1))
    print(json.dumps(run["by_case"], indent=0))
    print("file:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
