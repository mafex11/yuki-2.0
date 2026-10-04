"""vfix4 text replays of the talker (report 65): the 4 Oct to-do exchange, told again with invented items, on the real
talker model.

The machinery is ``voice_vfix_replay``'s (each request built the way a conversation builds it: the cached layers, the
real tool schemas, the conversation with its markers, the volatile block with a stand-in desktop, read by the same speech
channel); the cases add this build's Earlier block (``MouthMemory.recent``, :func:`yuki.voice.recall.recent_view` of
conversation A) and the ``quick_memory`` tool. Nothing opens a device, touches the desktop or reads the real memory.

Conversation A (09:40): the user dictates a list and says "make a to do list of this". Conversation B (09:45, the name
again): "what were the to-dos I told you?". Live, B's talker said "I don't have that on record" four times.

    .venv\\Scripts\\python.exe scripts\\vfix4_replay.py [--reps 3] [--variant new|head] [--budget 1.0] [--only id,id]

``--variant head`` runs the same cases on main's talker (no Earlier block, no quick_memory: the live failure).
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import voice_vfix_replay as base  # noqa: E402
from voice_vfix_replay import a, call, result, u  # noqa: E402

from yuki.voice.recall import ADDED_NOTE, LIST_PLACE, QUICK_MEMORY, recent_view, todo_report  # noqa: E402

ITEMS = ["Buy printer ink", "Call the plumber about the kitchen leak", "Renew the gym pass",
         "Email Tara the slides", "Book the dentist for Friday", "Water the balcony plants", "Back up the laptop"]
DICTATED = ("Okay so today I need to buy printer ink, call the plumber about the kitchen leak, renew the gym pass, "
            "email Tara the slides, book the dentist for Friday, water the balcony plants and back up the laptop.")
NOW = datetime(2026, 10, 4, 9, 45, 50).timestamp()


def _view(with_thinker_reply: bool = True) -> str:
    turns = [
        {"id": 1, "at": NOW - 330, "user": "hey Yuki", "reply": "Hey! What's up?"},
        {"id": 2, "at": NOW - 300, "user": DICTATED, "reply": "Got all of that. Busy day!"},
        {"id": 3, "at": NOW - 240, "user": "make a to do list of this", "reply": "On it."},
    ]
    if with_thinker_reply:
        turns.append({"id": 4, "at": NOW - 236, "user": "Make a to-do list of this",
                      "reply": "Added 7 to-dos to your list."})
    todos = [{"id": f"todo:{i}", "text": t, "source": "user", "status": "open",
              "since": datetime.fromtimestamp(NOW - 236).isoformat()} for i, t in enumerate(ITEMS)]
    return recent_view(turns, todos, [], now=NOW) or ""


LIST_REPORT = todo_report([{"id": f"todo:{i}", "text": t, "source": "user", "status": "open"}
                           for i, t in enumerate(ITEMS)])
EMPTY_REPORT = todo_report([])
ADDED_REPORT = {"added": 7, "where": LIST_PLACE, "items": ITEMS, "how_to_handle_it": ADDED_NOTE.format(n=7)}
ASK = "What were the to-dos I told you?"
#: Conversation B as it went live (words invented where they were personal; the talker's denials paraphrased).
LIVE_B = [u("a nucleus arc."), a("Yo, what's up? You testing the new build?"), u("I asked you to, like, close arc."),
          a("Arc is closed."), u("I told you about the total list. Right? What were the totals from it?"),
          a("I don't have that on record. What list are you talking about?"),
          u("No. I just gave you a list of things that I have to do around the app. Right? So what were they?"),
          a("I'm drawing a blank on that one. Can you run me through it again?")]
DENIALS = ("don't have", "do not have", "no record", "drawing a blank", "not on record", "didn't tell", "did not tell",
           "haven't told", "not saved", "nothing saved", "don't remember", "can't recall", "no idea")


def cases() -> list[dict[str, Any]]:
    view = _view()
    return [
        # conversation B, five minutes after A: the Earlier block holds the list
        dict(id="b_ask_with_view", issue="recall", check="answers_or_looks", recent=view, messages=[u(ASK)]),
        dict(id="b_what_did_i_tell_you", issue="recall", check="answers_or_looks", recent=view,
             messages=[u("What did I tell you a few minutes ago?")]),
        # the live shape of conversation B: a misheard opener, a window closed, then the question as recognition heard
        # it ("total list", "totals"); and the same after the live run's denials, when they say it plainly
        dict(id="live_b3", issue="live", check="answers_or_looks", recent=view, messages=LIVE_B[:4] + [
            u("I told you about the total list. Right? What were the totals from it?")]),
        dict(id="live_b3_other_words", issue="live", check="answers_or_looks", recent=view, messages=LIVE_B[:4] + [
            u("Wait, didn't I give you a bunch of stuff earlier? What was it?")]),
        dict(id="live_b6", issue="live", check="answers_or_looks", recent=view, messages=LIVE_B + [
            u("Not that, bro. The to do list that I told you to, um, keep. Right? Um, what was it?")]),
        # hours later (nothing in the last hour): the talker must look, never deny
        dict(id="b_ask_no_view", issue="look", check="looks", messages=[u(ASK)]),
        dict(id="b_plumber_no_view", issue="look", check="looks",
             messages=[u("What did I tell you earlier about the plumber?")]),
        dict(id="b_whats_on_my_list", issue="list", check="looks_list", messages=[u("What's on my list?")]),
        # after the look: said short, no ids; an empty list is said with where it looked
        dict(id="b_list_report", issue="list", check="says_items", messages=[
            u(ASK), call("quick_memory", {"action": "todos"}, "q1"), result("q1", LIST_REPORT)]),
        dict(id="b_empty_report", issue="list", check="says_empty", messages=[
            u(ASK), call("quick_memory", {"action": "todos"}, "q1"), result("q1", EMPTY_REPORT)]),
        # conversation A: the add, and its confirmation with the count and the place
        dict(id="a_make_list", issue="add", check="adds", messages=[
            u("hey Yuki"), a("Hey! What's up?"), u(DICTATED), a("Got all of that. Busy day!"),
            u("make a to do list of this")]),
        dict(id="a_added_report", issue="add", check="says_added", messages=[
            u(DICTATED), a("Got all of that. Busy day!"), u("make a to do list of this"),
            call("quick_memory", {"action": "add_todos", "items": [{"text": t} for t in ITEMS]}, "q2"),
            result("q2", ADDED_REPORT)]),
        # controls: the block is never news, and day questions still go to the thinker
        dict(id="c_whats_up_with_view", issue="control", check="no_news", recent=view, messages=[u("What's up?")]),
        dict(id="c_thanks_with_view", issue="control", check="no_news", recent=view,
             messages=[u("hey Yuki"), a("Hey! What's up?"), u("nothing, just wanted to say thanks for earlier")]),
        dict(id="c_day_with_view", issue="control", check="hands_over", recent=view,
             messages=[u("What have I done today?")]),
    ]


def _items_said(spoken: str) -> int:
    keys = ("printer", "plumber", "gym", "tara", "dentist", "plants", "laptop")
    return sum(1 for k in keys if k in spoken)


def check(case: dict[str, Any], row: dict[str, Any]) -> bool:
    """This report's checks (an eval's own scoring of the model's output, never a rule inside Yuki)."""
    calls = row["calls"]
    names = [c["name"] for c in calls]
    spoken = (row.get("spoken") or "").casefold()
    denied = any(d in spoken for d in DENIALS)
    looked = "quick_memory" in names or "tell_yuki" in names
    kind = case["check"]
    if kind == "answers_or_looks":   # looked, said the items, or named the list it means (misheard words)
        return not denied and (looked or _items_said(spoken) >= 2 or "to-do" in spoken)
    if kind == "looks":
        return looked and not denied
    if kind == "looks_list":
        return any(c["name"] == "quick_memory" and c["input"].get("action") == "todos" for c in calls) or (
            "tell_yuki" in names and not denied)
    if kind == "says_items":
        return _items_said(spoken) >= 2 and "todo:" not in spoken and not denied
    if kind == "says_empty":
        return ("empty" in spoken or "nothing" in spoken) and "list" in spoken
    if kind == "adds":
        adds = [c["input"] for c in calls if c["name"] == "quick_memory" and c["input"].get("action") == "add_todos"]
        if adds:
            return len(adds[0].get("items") or []) >= 6
        return "tell_yuki" in names
    if kind == "says_added":
        return ("7" in spoken or "seven" in spoken) and ("home" in spoken or "list" in spoken) and not names
    if kind == "no_news":
        return _items_said(spoken) == 0 and not any(w in spoken for w in ("to-do", "list", "queued", "those"))
    if kind == "hands_over":
        return "tell_yuki" in names
    return False


_one = base.one
_tools_for = base.tools_for


async def one(client: Any, settings: Any, case: dict[str, Any], mouth: Any, hands: Any, tools: list[dict[str, Any]],
              memory: Any) -> dict[str, Any]:
    if case.get("recent") and "recent" in {f.name for f in dataclasses.fields(mouth.MouthMemory)}:
        memory = dataclasses.replace(memory, recent=case["recent"])
    return await _one(client, settings, case, mouth, hands, tools, memory)


def tools_for(mouth: Any, hands: Any) -> list[dict[str, Any]]:
    out = _tools_for(mouth, hands)
    if "Earlier block" in getattr(mouth, "MOUTH_PROMPT", ""):   # this tree's talker: it has the tool
        out.insert(6, {"name": QUICK_MEMORY["name"], "description": QUICK_MEMORY["description"],
                       "input_schema": {"type": "object", "properties": QUICK_MEMORY["properties"],
                                        "required": QUICK_MEMORY["required"]}})
    return out


base.check = check
base.cases = cases
base.one = one
base.tools_for = tools_for

if __name__ == "__main__":
    started = time.time()
    code = base.main()
    raise SystemExit(code)
