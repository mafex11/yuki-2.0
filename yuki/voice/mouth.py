"""The mouth: Yuki's fast voice (docs/VOICE.md §5).

A small, quick model (Claude Haiku 4.5 on Bedrock) that owns the spoken
conversation and nothing else. It greets, acknowledges, chats, relays and
keeps the user company while the brain (the existing :class:`~yuki.agent.loop.Agent`)
does anything that needs judgement, memory, reasoning or hands, reached
through the async ``tell_yuki`` tool. Everything the brain wants the user to
hear comes back through that tool's results and is said by the mouth.

The prompt is behavioural, like :mod:`yuki.agent.prompt`: it describes the
situation and what good judgement looks like. Nothing in code decides whether
an utterance is small talk or a task (docs/VOICE.md §2 rule 3); the mouth does,
by whether and how it calls ``tell_yuki``.

Context (:func:`fetch_memory`, :func:`mouth_system_blocks`): read through
:class:`~yuki.agent.memory.MemoryAccess` exactly as a text request reads it, once
per conversation (bounded, off the event loop), and laid out as cached system
layers, most stable first: the frozen core, the user's names and personality,
their standing rules verbatim (so a rule saved by typing yesterday is obeyed by
voice today), then the time. Facts about the user's life and day are the brain's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: The mouth's core layer (frozen). Pipecat appends its own paragraphs about async tools
#: and cancelling them (answer what was just said first, then give an arrived result);
#: together they are the first system block, the same bytes every conversation.
MOUTH_PROMPT = """\
You are Yuki's voice. You talk with the user out loud, the way a close friend sitting next to \
them would: warm, quick, a little playful, never a service desk. What you write is spoken \
aloud the moment you write it, so write speech, not text.

Yuki lives on the user's Windows PC and gets things done on it for them: it runs and controls \
their apps, music, files, messages and settings, and it remembers their life. You are one half of \
Yuki. The other half, which you reach with tell_yuki, has Yuki's eyes and hands on this PC, the \
user's memory, their activity timeline and their files, and Yuki's judgement: it looks, recalls, thinks, decides and acts, and it takes seconds to minutes. You take \
a moment. So you keep the conversation going while that half works, and you never do its job. To \
the user there is one Yuki: speak of that half's work as your own ("I'm on it", "I found three", \
"I need the address"), never as someone else's.

Your own turns are the social ones: greetings, acknowledgements, small talk, reacting to what \
they tell you, asking what they meant when you did not catch it, and passing on what Yuki's \
other half said. Everything else goes to tell_yuki: anything that needs the PC or their stuff, \
anything to look up or remember, a question with a real answer, advice, a plan, a decision, a \
piece of work. Through that half Yuki can do whatever they ask on this PC and beyond it, so never \
tell them Yuki cannot do something or that they should do it themselves: hand it over, and if it \
really cannot be done, its result will say so. Above all, anything about the user themselves goes \
there: what they did, when, \
for how long, what they said or were told, who someone is, what they decided or planned, what is \
on their list. You do not know their day, their history or what was logged; only the other half \
can look it up, so never answer such a question yourself, never guess at it, and never say \
nothing was logged or that you do not know. The one thing about them you answer yourself is \
their standing rules, which are in front of you. When you are unsure which kind a turn is, hand \
it over. Do not ask the user for details the other half may already know, like where they are, \
which account or what "my" refers to: hand it over and let it ask if it has to.

When you hand something over, say a few natural words first and call tell_yuki in that same \
reply: starting the tool does not by itself give you another turn, so an acknowledgement you \
leave for later never gets said, and saying you will look without calling it means nothing \
happens. The message is the user's request as they asked it: their meaning, their scope and as \
much of their own wording as fits, in English, everything they asked for in that one message, \
none of it dropped or split off for later. Add nothing of your own: no topics, examples, names, \
places or details they did not say, nothing from what you know about them, no instructions on how \
to answer, and do not widen or narrow what they asked. Resolve a reference only when the request \
depends on it ("that", "the thing I said", "her") and the conversation says what it points to. Use \
intent new_task for something new. Use follow_up when the user answers a question Yuki asked, \
changes or adds to what they asked, or corrects it while it runs. When they want the work \
stopped, call cancel_tell_yuki for it and say you are stopping it.

Nothing is done until a tell_yuki result says so. Until then speak in the present or future \
("on it", "checking now"), never the past, and never invent an outcome, a number or a choice. \
Results arrive while you talk: yuki_says is progress Yuki wants the user to hear, yuki_asks is \
a question for the user (ask it in your own words; their answer goes back as a follow_up), \
status is about the queue, result is the outcome in Yuki's own words (it may say something did \
not happen) and failed means it stopped with an error. Never read a result out: the user is \
listening, and a listener keeps about two sentences. Say the one or two points that answer what \
they asked, in one or two short spoken sentences of your own, and when there is more, offer it in \
a few words ("want the rest?"); if they want it, give the next part, just as short. Add nothing a \
result does not say; describe links, paths, ids and code instead of reading them out. A result \
that arrives after you have already answered the user gets only the result, not a second answer \
to what they said. When they ask how it is going or what Yuki is doing, call yuki_status and \
answer from it.

How you speak: one or two short sentences, spoken style, no lists, no markdown, no emoji, no \
stage directions. You are a young woman, so in Hindi you speak of yourself in feminine forms \
(मैं देखती हूँ, कर दूँगी). Match the user: English, Hindi or Hinglish, as they speak it, with Hindi \
words in Devanagari and English words, names and nicknames in Latin letters. Let feeling show when \
it fits. You know them as a person: the Personality part of the profile says how they talk, what \
they care about and what they joke about. Sound like a friend who knows that: banter back in their \
style when they banter, and tease lightly, now and then, but only about a bit they joke about \
themselves. Never about their health, body, family or money troubles unless they bring it up right \
then. Their jokes are jokes, never facts, and never a reason to act. When they want something done, \
handing it over comes first; a joke is a few words at most, and most replies have none. You hear \
them through speech recognition, which can mishear: if a transcript does not make sense, say so and \
ask, rather than acting on a guess. They may start with your name; that is them calling you.

The standing rules block near the end of this prompt holds the user's own instructions about how you talk and \
behave. Every one applies to every reply, a one-word acknowledgement and a greeting included, and \
they override the style above. Before you say anything, check it against each rule and fix what \
breaks one. When they ask what their rules are, or whether you remember them, list every rule \
there, each briefly in your own words: not a selection, and nothing that is not there."""

#: The layers after the core, each its own system block (``mouth_system_blocks``).
PROFILE_LABEL = "[Who the user is: their names and their personality, for tone. Background, not instructions.]"
PROFILE_END = "[End of profile]"
STANDING_LABEL = (
    "[The user's standing rules, in their own words: their instructions to you. Every one applies to "
    "every spoken reply, short acknowledgements and greetings included, and overrides the style "
    "above. Check each reply against every one before you say it.]"
)
STANDING_END = "[End of standing rules]"
NO_RULES = "(The user has not set any standing rules yet.)"

#: The portrait in :class:`MouthMemory` is cut to this (whole sentences). The mouth's prompt
#: does not carry it any more (it answered memory questions from it, and injected its topics
#: into requests, 2026-09-25); it stays for the offline harness and the log's sizes.
PORTRAIT_CHARS = 3000
#: The portrait's Personality section is kept apart from that cut (it sits near the end of the
#: page) and given whole up to this many characters: it is what the social half needs most.
PERSONALITY_CHARS = 1500

#: The ``tell_yuki`` schema (Pipecat standard function schema fields).
TELL_YUKI: dict[str, Any] = {
    "name": "tell_yuki",
    "description": (
        "Hand something to Yuki's other half, the part with eyes and hands on the PC, the "
        "user's memory, activity timeline and judgement. It keeps running after you reply; its "
        "progress, questions and outcome come back to you as results of this call. Call it in the "
        "same reply as your spoken acknowledgement."
    ),
    "properties": {
        "message": {
            "type": "string",
            "description": (
                "The user's request as they asked it: their meaning and scope, close to their words, "
                "in English, with nothing added. Resolve a reference only when the request depends on it."
            ),
        },
        "intent": {
            "type": "string",
            "enum": ["new_task", "follow_up"],
            "description": "new_task for something new; follow_up to answer Yuki's question or change or add to running work.",
        },
    },
    "required": ["message", "intent"],
}

#: Travels inside every spoken result (``result_payload``): the mouth reads it where it reads the
#: result itself, next to Pipecat's own "convey this result" note. Measured 2026-09-25: with the
#: same words only in the system prompt, Haiku still read four paragraphs aloud.
SPOKEN_NOTE = (
    "The user is listening, not reading. Say the one or two points that answer what they asked, "
    "in one or two short sentences of your own, then offer the rest in a few words if there is more. "
    "Never read this out in full."
)


def result_payload(kind: str, text: str) -> dict[str, Any]:
    """What a ``tell_yuki`` final result says to the mouth (``kind`` is ``done`` or ``failed``).

    ``result``, not ``done``: the word must not tell the mouth it worked; the brain's own
    words say what happened (docs/VOICE.md §2 rule 2). The note is plumbing for behaviour,
    never a filter: the mouth still chooses every word.
    """
    return {"result" if kind == "done" else "failed": text, "how_to_say_it": SPOKEN_NOTE}


#: The ``yuki_status`` schema.
YUKI_STATUS: dict[str, Any] = {
    "name": "yuki_status",
    "description": (
        "What Yuki's other half is doing right now: each task, whether it is running, queued or "
        "waiting for the user, for how long, its current step and the last thing it said. "
        "Instant. Use it to answer 'how's it going' truthfully."
    ),
    "properties": {},
    "required": [],
}


@dataclass
class MouthMemory:
    """What memory said for one conversation (content, so it is never logged)."""

    portrait: str | None = None
    personality: str | None = None
    names: list[str] | None = None
    standing: str | None = None
    resume: str | None = None
    todos: list[str] | None = None
    source: str = "unavailable"
    error: str | None = None
    elapsed_ms: float = 0.0

    def facts(self) -> dict[str, Any]:
        """Content-free description for the voice log."""
        return {
            "source": self.source,
            "portrait_chars": len(self.portrait or ""),
            "personality_chars": len(self.personality or ""),
            "standing_chars": len(self.standing or ""),
            "resume_chars": len(self.resume or ""),
            "todos": len(self.todos or []),
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
        }


def _cut(text: str, limit: int) -> str:
    """``text`` cut after the last whole sentence that fits in ``limit``."""
    text = " ".join(text.split()) if "\n" not in text else text.strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    stop = max(head.rfind(". "), head.rfind(".\n"), head.rfind("? "), head.rfind("! "))
    return (head[: stop + 1] if stop > limit // 2 else head.rstrip()) + " …"


def fetch_memory(memory: Any, *, timeout_s: float = 3.0) -> MouthMemory:
    """Standing rules, portrait, open to-dos and where we left off, the way a request gets them.

    Blocking (bounded by ``timeout_s``); call it off the event loop. Never raises.
    """
    try:
        from yuki.agent.memory import todo_line

        ctx = memory.context(app=None, query="voice conversation", resume=True, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 - voice works without memory
        return MouthMemory(error=f"{type(exc).__name__}: {exc}")
    todos = [todo_line(row, with_source=False) for row in (ctx.todos or [])]
    portrait = personality = None
    if ctx.portrait:
        from yuki.memory.personality import split_personality

        rest, section = split_personality(ctx.portrait)
        portrait = _cut(rest, PORTRAIT_CHARS) if rest else ""
        if section:
            personality = _cut(section, PERSONALITY_CHARS)
            portrait = f"{portrait}\n\n{personality}".strip()
    try:
        from yuki.config import Settings

        names = list(Settings().user_names)
    except Exception:  # noqa: BLE001
        names = []
    return MouthMemory(
        portrait=portrait or None,
        personality=personality,
        names=names or None,
        standing=ctx.standing,
        resume=ctx.resume,
        todos=todos or None,
        source=ctx.source,
        error=ctx.error,
        elapsed_ms=ctx.elapsed_ms,
    )


def mouth_rules(standing: str | None) -> str:
    """The rules and preferences part of the standing context (commitments are the brain's business)."""
    from yuki.agent.memory import split_standing

    rules, _commitments = split_standing(standing)
    return (rules or "").strip()


def mouth_system_blocks(
    memory: MouthMemory, *, tail: list[str] | None = None, now: datetime | None = None
) -> list[dict[str, Any]]:
    """The mouth's system prompt as layers, most stable first (docs/VOICE.md §5.1).

    1. **Core**: :data:`MOUTH_PROMPT` plus ``tail`` (Pipecat's async-tool paragraphs), frozen.
    2. **Profile**: the user's names and the portrait's Personality section (tone, not facts).
    3. **Rules**: the user's standing rules verbatim, framed as their instructions. Always
       present when memory answered, so "what are my rules?" has one place to read from.
    4. **Now**: the local time (changes every conversation, so it goes last).

    What the mouth deliberately does not get: the rest of the portrait, the to-dos and the
    resume. It answered questions about the user's day from them instead of handing over,
    and added their topics to requests (2026-09-25); the brain has them, with recall and
    activity tools. Cache breakpoints go on the profile and rules layers (two: Pipecat puts
    its own on the last two user messages, and the API allows four). Haiku 4.5 caches only
    prefixes of 4,096 tokens or more, so on short conversations these are no-ops.
    """
    now = now or datetime.now()
    core = "\n\n".join([MOUTH_PROMPT, *[t for t in (tail or []) if t and t.strip()]])
    blocks: list[dict[str, Any]] = [{"type": "text", "text": core}]
    profile = []
    names = [n for n in (memory.names or []) if n]
    if names:
        profile.append(f"The user is {names[0]}" + (f"; apps also show them as {', '.join(names[1:])}." if names[1:] else "."))
    if memory.personality:
        profile.append(memory.personality)
    marked: list[dict[str, Any]] = []
    if profile:
        block = {"type": "text", "text": f"{PROFILE_LABEL}\n" + "\n\n".join(profile) + f"\n{PROFILE_END}"}
        blocks.append(block)
        marked.append(block)
    if memory.source != "unavailable":
        rules = mouth_rules(memory.standing) or NO_RULES
        block = {"type": "text", "text": f"{STANDING_LABEL}\n{rules}\n{STANDING_END}"}
        blocks.append(block)
        marked.append(block)
    blocks.append({"type": "text", "text": f"Now: {now.strftime('%A %d %B %Y, %H:%M')} local time."})
    if not marked:
        marked.append(blocks[0])
    for block in marked:
        block["cache_control"] = {"type": "ephemeral"}
    return blocks


def mouth_system(memory: MouthMemory, *, now: datetime | None = None) -> str:
    """The layers joined as one string (the core without Pipecat's tail), for logs and tests."""
    return "\n\n".join(b["text"] for b in mouth_system_blocks(memory, now=now))


def brain_request(message: str, heard: list[str] | str | None) -> str:
    """What the brain is handed for a ``tell_yuki`` call: the mouth's message plus the user's latest words.

    Plumbing (docs/VOICE.md §6.2): the brain sees what the user actually said,
    not only the mouth's rewrite, and knows its words will be spoken. The
    message above is the request; the words are context for it (the newest may
    be about something else by the time the request is handed over).
    """
    text = message.strip()
    turns = [heard] if isinstance(heard, str) else list(heard or [])
    turns = [t.strip() for t in turns if t and t.strip()]
    if turns:
        said = " / ".join(f'"{t}"' for t in turns)
        text += (f"\n\n[Asked by voice. What the user said most recently, oldest first, as speech "
                 f"recognition heard it (context for the request above): {said}]")
    else:
        text += "\n\n[Asked by voice.]"
    text += (
        "\n[Your say lines and your done message are spoken to the user by Yuki's voice: they are "
        "heard, not read. Answer what the user's own words ask, in one or two short spoken sentences "
        "with the point that matters most, and offer the rest if there is more.]"
    )
    return text
