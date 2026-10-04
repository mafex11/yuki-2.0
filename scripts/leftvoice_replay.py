"""leftVoice timing replays of the talker (report 71): when the first words of a quick memory read can be heard.

Report 65's open point: "what's on my list" by voice was silent for about 2.5-3 s, because the talker's call that makes
the quick_memory call says nothing (1.46-1.59 s to its end) and only the reply after the report speaks (first token
about 1.0-1.2 s more). These replays run the talker on the real model, the way ``vfix4_replay`` builds each request
(the cached layers, the real tool schemas, the markers, the volatile block), and time each stream from the request:

- ``first_event_ms``: the stream's first event (the model's time to first token);
- ``words_ms``: the first words the voice could start on: the first spoken text after a say marker line, or a call's
  say_first the moment its string is whole (the pipeline's ``early_words``), whichever comes first;
- ``end_ms``: the end of the stream (when the call is written and runs).

The two-turn cases pair the look (``*_look``) with the reply after its report (``*_reply``): the answer's first words
come at about look ``end_ms`` + reply ``words_ms`` (the tool itself takes a few ms on a real memory). Each case keeps
vfix4's checks of what was done; the timing is the point here.

    .venv\\Scripts\\python.exe scripts\\leftvoice_replay.py [--reps 4] [--variant new|head] [--budget 0.6] [--only id,id]

Nothing opens a device, touches the desktop or reads the real memory (invented items).
"""

from __future__ import annotations

import contextvars
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import vfix4_replay as v4  # noqa: E402  (installs its cases and checks into voice_vfix_replay)
import voice_vfix_replay as base  # noqa: E402
from voice_vfix_replay import a, call, result, u  # noqa: E402

#: The events of the stream being read in this task (one replay call per task).
_EVENTS: contextvars.ContextVar[list[tuple[float, Any]] | None] = contextvars.ContextVar("events", default=None)

ASK_LIST = "What's on my list?"
TOTALS = "I told you about the total list, right? What were the totals?"
PLUMBER = "What did I tell you earlier about the plumber?"
TODAY_REPORT = {"found": "Today with you, oldest first:\n- (07:10) they: The plumber said he can only come on Thursday "
                         "after four, so remind me to be home by then. / you: Got it, Thursday after four for the plumber.",
                "how_to_handle_it": "For you, not to read out: answer what they asked from it in a few words. When it "
                                    "does not hold what they mean, say where you looked (today's chat) and offer to "
                                    "dig further, which is a hand-over."}


ADD_ASK = "Add call mom to my list, and what time is it?"


def _line() -> str:
    """A clean add's line, said by code: in the conversation as words before the call (``chronological``)."""
    from yuki.voice import recall

    return recall.ADDED_ONE_LINE


def _added_said_report(item: str) -> dict[str, Any]:
    """A clean add's report as QuickMemory settles it: its line already said by code, and a run after it (third check
    LVV-1: that run answers the rest of the same words, or stays quiet)."""
    from yuki.voice import recall

    return {"added": 1, "where": recall.LIST_PLACE, "items": [item], "said": recall.ADDED_ONE_LINE,
            "how_to_handle_it": recall.ADDED_SAID_NOTE}


def _lead_call(args: dict[str, Any], cid: str, lead: str) -> list[dict[str, Any]]:
    """The look as the conversation holds it: in this tree the call carries its say_first (spoken first, so its words
    go before the call), on main the call alone."""
    if _HAS_LEAD:
        return [a(lead), call("quick_memory", {**({"say_first": lead} if _LEAD_FIELD else {}), **args}, cid)]
    return [call("quick_memory", args, cid)]


def cases() -> list[dict[str, Any]]:
    out = [
        dict(id="list_look", issue="list", check="looks_list", messages=[u(ASK_LIST)]),
        # as a conversation begins: the name, its answer, then the ask
        dict(id="list_named", issue="list", check="looks_list", messages=[u("Yuki."), a("Yeah?"), u(ASK_LIST)]),
        dict(id="list_reply", issue="list", check="says_items", messages=[
            u(ASK_LIST), *_lead_call({"action": "todos"}, "q1", "Let me look."), result("q1", v4.LIST_REPORT)]),
        dict(id="totals_look", issue="list", check="answers_or_looks", messages=[u(TOTALS)]),
        # the list was made in the last hour, so the Earlier block holds it: answering from it is fastest
        dict(id="view_ask", issue="view", check="answers_or_looks", recent=v4._view(), messages=[u(v4.ASK)]),
        dict(id="view_list", issue="view", check="answers_or_looks", recent=v4._view(), messages=[u(ASK_LIST)]),
        dict(id="plumber_look", issue="today", check="looks", messages=[u(PLUMBER)]),
        dict(id="plumber_reply", issue="today", check="plumber", messages=[
            u(PLUMBER), *_lead_call({"action": "today"}, "q4", "Let me check."), result("q4", TODAY_REPORT)]),
        dict(id="add_look", issue="add", check="adds_one", messages=[u("Put renew the car insurance on my list.")]),
        # an add with more asked in the same breath; the run after the code-said add gives the time (third check
        # LVV-1), and after a plain add it stays quiet
        dict(id="add_ask", issue="add", check="add_and_ask", messages=[u(ADD_ASK)]),
        dict(id="add_ask_reply", issue="add", check="says_time_only", messages=[
            u(ADD_ASK), a(_line()), call("quick_memory", {"action": "add_todos", "items": [{"text": "Call mom"}]}, "q9"),
            result("q9", _added_said_report("Call mom"))]),
        dict(id="add_plain_reply", issue="add", check="quiet_after_add", messages=[
            u("Add the car insurance renewal to my to-do list."), a(_line()),
            call("quick_memory", {"action": "add_todos", "items": [{"text": "Car insurance renewal"}]}, "q8"),
            result("q8", _added_said_report("Car insurance renewal"))]),
        # controls: a plain answer (the floor every reply has), and no lead where no tool is needed
        dict(id="time_plain", issue="control", check="plain", messages=[u("What time is it?")]),
        dict(id="whats_up", issue="control", check="plain", messages=[u("What's up?")]),
        # controls: every other call still alone, with no words before it (a hand, a hand-over)
        dict(id="pause_alone", issue="control", check="alone:media_key", messages=[u("Pause the music.")]),
        dict(id="open_alone", issue="control", check="alone:open_url", messages=[u("Open Gmail in a new tab.")]),
        dict(id="day_alone", issue="control", check="alone:tell_yuki", messages=[u("What have I done today?")]),
    ]
    return out


def _says_time(spoken: str) -> bool:
    """The reply gave a time (digits, or a time said in words: "half ten", "ten twenty-nine")."""
    return any(ch.isdigit() for ch in spoken) or any(w in spoken for w in ("half", "past", "o'clock", "twenty",
                                                                            "thirty", "forty", "fifty"))


def check(case: dict[str, Any], row: dict[str, Any]) -> bool:
    kind = case["check"]
    names = [c["name"] for c in row["calls"]]
    spoken = (row.get("spoken") or "").casefold()
    if kind == "plumber":
        return "thursday" in spoken and not names
    if kind == "adds_one":
        adds = [c["input"] for c in row["calls"] if c["name"] == "quick_memory" and c["input"].get("action") == "add_todos"]
        return bool(adds) and any("insurance" in str(i).casefold() for i in adds[0].get("items") or [])
    if kind == "add_and_ask":   # the add (the time may be said before it, or by the run after the add's line)
        adds = [c["input"] for c in row["calls"] if c["name"] == "quick_memory" and c["input"].get("action") == "add_todos"]
        return bool(adds) and any("mom" in str(i).casefold() for i in adds[0].get("items") or [])
    if kind == "says_time_only":   # the time, and the add (already said by code) not again
        return _says_time(spoken) and "added" not in spoken and "list" not in spoken and not names
    if kind == "quiet_after_add":  # nothing more asked: stay_quiet alone, no words
        return not spoken and names in ([], ["stay_quiet"])
    if kind == "plain":
        return not names and bool(spoken)
    if kind.startswith("alone:"):   # the call they want, and nothing spoken before it
        return kind.split(":", 1)[1] in names and not spoken
    return v4.check(case, row)


class _TimedStream:
    """The SDK stream, with each event's arrival time kept for this task."""

    def __init__(self, stream: Any, events: list[tuple[float, Any]]) -> None:
        self._stream = stream.__aiter__()
        self._events = events

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> Any:
        event = await self._stream.__anext__()
        self._events.append((time.monotonic(), event))
        return event


class _TimedClient:
    """A client whose ``messages.create`` streams are timed (the request's send time is the first mark)."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.messages = self

    async def create(self, **kwargs: Any) -> Any:
        events = _EVENTS.get()
        if events is not None:
            events.append((time.monotonic(), "sent"))
        stream = await self._client.messages.create(**kwargs)
        return _TimedStream(stream, events if events is not None else [])


def timing(events: list[tuple[float, Any]], mouth: Any, say_first_tools: frozenset[str]) -> dict[str, Any]:
    """The marks of one stream (ms from the request), read as the pipeline reads it."""
    from yuki.voice.pipeline import early_words

    if not events or events[0][1] != "sent":
        return {}
    sent = events[0][0]
    reader = mouth.ReplyChannel()
    first_event = words = None
    tool, args, said = None, "", True
    for t, event in events[1:]:
        kind = getattr(event, "type", "")
        first_event = first_event if first_event is not None else t
        if kind == "content_block_start" and getattr(event.content_block, "type", "") == "tool_use":
            tool, args = event.content_block.name, ""
            said = tool not in say_first_tools
        elif kind == "content_block_delta" and hasattr(event.delta, "partial_json") and tool is not None:
            args += event.delta.partial_json or ""
            if not said and early_words(args) is not None:
                said = True
                words = words if words is not None else t
        elif kind == "content_block_delta" and hasattr(event.delta, "text"):
            if reader.feed(event.delta.text or "").strip() and words is None:
                words = t
    end = events[-1][0]

    def ms(x: float | None) -> int | None:
        return None if x is None else round((x - sent) * 1000)

    return {"first_event_ms": ms(first_event), "words_ms": ms(words), "end_ms": ms(end)}


_HAS_LEAD = False      # this tree's talker says a few words before its look (or in the call's say_first)
_LEAD_FIELD = False    # in the call's say_first
_SAY_FIRST: frozenset[str] = frozenset()


def _install(variant: str, ref: str) -> None:
    """This tree's quick memory schema, or ``ref``'s (head: read with git show), and the tools whose say_first is said
    as it streams."""
    global _HAS_LEAD, _LEAD_FIELD, _SAY_FIRST
    import subprocess
    import tempfile

    if variant == "head":
        tmp = Path(tempfile.mkdtemp(prefix="leftvoice-head-"))
        for rel in ("yuki/voice/recall.py", "yuki/voice/pipeline.py"):
            text = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=ROOT, capture_output=True, text=True,
                                  encoding="utf-8", check=True).stdout
            (tmp / Path(rel).name).write_text(text, encoding="utf-8")
        recall = base._load("leftvoice_head_recall", tmp / "recall.py")
        say_first = set()
        for line in (tmp / "pipeline.py").read_text(encoding="utf-8").splitlines():
            if line.startswith("SAY_FIRST_TOOLS = frozenset("):
                say_first = set(json.loads(line.split("(", 1)[1].rsplit(")", 1)[0].replace("{", "[").replace("}", "]")))
        quick = recall.QUICK_MEMORY
    else:
        from yuki.voice.pipeline import SAY_FIRST_TOOLS
        from yuki.voice.recall import QUICK_MEMORY as quick

        say_first = set(SAY_FIRST_TOOLS)
    _SAY_FIRST = frozenset(say_first)
    _LEAD_FIELD = "say_first" in quick["properties"]
    _HAS_LEAD = _LEAD_FIELD or "a few words" in quick["description"]
    v4.QUICK_MEMORY = quick   # vfix4_replay's tools_for inserts this schema


_one = v4.one


async def one(client: Any, settings: Any, case: dict[str, Any], mouth: Any, hands: Any, tools: list[dict[str, Any]],
              memory: Any) -> dict[str, Any]:
    events: list[tuple[float, Any]] = []
    token = _EVENTS.set(events)
    try:
        row = await _one(_TimedClient(client), settings, case, mouth, hands, tools, memory)
    finally:
        _EVENTS.reset(token)
    row.update(timing(events, mouth, _SAY_FIRST))
    row["ok"] = check(case, row)
    return row


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for r in rows:
        if "error" in r:
            continue
        s = out.setdefault(r["id"], {"n": 0, "ok": 0, "first_event": [], "words": [], "end": []})
        s["n"] += 1
        s["ok"] += int(r["ok"])
        for key in ("first_event", "words", "end"):
            if r.get(f"{key}_ms") is not None:
                s[key].append(r[f"{key}_ms"])
    for s in out.values():
        s["with_words"] = len(s["words"])   # replies that said something before their stream ended
        for key in ("first_event", "words", "end"):
            v = s.pop(key)
            s[f"{key}_p50"] = round(statistics.median(v)) if v else None
            s[f"{key}_min"] = min(v) if v else None
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--reps", type=int, default=4)
    ap.add_argument("--variant", choices=("new", "head"), default="new")
    ap.add_argument("--head-ref", default="main")
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--budget", type=float, default=0.6)
    ap.add_argument("--only", default="")
    ap.add_argument("--out", type=Path, default=Path(tempfile_dir()) / "yuki-leftvoice")
    args = ap.parse_args(argv)
    _install(args.variant, args.head_ref)
    args.cases = ""
    run = asyncio.run(base.main_async(args))
    by = summary(run["rows"])
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"leftvoice-replay-{args.variant}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({**run, "timing": by}, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps({k: run[k] for k in ("variant", "ok", "n", "errors", "usd", "seconds")}))
    for cid, s in by.items():
        print(f"{cid:<14} ok {s['ok']}/{s['n']}  words {s['with_words']}/{s['n']}  first_event p50 "
              f"{s['first_event_p50']}  words p50 {s['words_p50']} (min {s['words_min']})  end p50 {s['end_p50']}")
    print("file:", path)
    return 0


def tempfile_dir() -> str:
    import tempfile

    return tempfile.gettempdir()


base.cases = cases
base.check = check
base.one = one

if __name__ == "__main__":
    raise SystemExit(main())
