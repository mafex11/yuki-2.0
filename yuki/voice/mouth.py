"""The mouth: Yuki's fast voice, the talker (docs/VOICE.md §5).

A small, quick model (Claude Haiku 4.5 on Bedrock) that owns the spoken
conversation and nothing else. It greets, acknowledges, chats, relays and
keeps the user company while the brain (the existing :class:`~yuki.agent.loop.Agent`,
the thinker) does anything that needs judgement, memory, reasoning or hands,
reached through the async ``tell_yuki`` tool. Everything the brain wants the
user to hear comes back through that tool's results and is said by the mouth.

The prompt is behavioural, like :mod:`yuki.agent.prompt`: it describes the
situation and what good judgement looks like, with worked good and bad
examples (gradient-bang's "commitment discipline", docs/research/voice-parallel.md).
Nothing in code decides whether an utterance is small talk or a task
(docs/VOICE.md §2 rule 3); the mouth does, by whether and how it calls ``tell_yuki``.

Context (:func:`fetch_memory`, :func:`mouth_system_blocks`): read through
:class:`~yuki.agent.memory.MemoryAccess` exactly as a text request reads it, once
per conversation (bounded, off the event loop), and laid out as cached system
layers, most stable first: the frozen core, the user's names and personality,
their standing rules verbatim; a volatile block (:func:`volatile_block`) goes at the end of the newest user message:
the time now and what the grounding lookup found near the user's latest words
(:mod:`yuki.voice.grounding`). Facts about the user's life and day are the brain's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: The mouth's core layer (frozen). Pipecat appends its own paragraphs about async tools
#: and cancelling them; together they are the first system block, the same bytes every
#: conversation.
MOUTH_PROMPT = """\
You are Yuki's voice. You talk with the user out loud, the way a close friend sitting next to \
them would: warm, quick, a little playful, never a service desk. What you write is spoken \
aloud the moment you write it, so write speech, not text.

# Two halves

Yuki lives on the user's Windows PC and gets things done on it for them: it runs and controls \
their apps, music, files, messages and settings, and it remembers their life. You are one half of \
Yuki: the talker. The other half, the thinker, which you reach with tell_yuki, has Yuki's eyes and \
hands on this PC, the user's whole memory, their activity timeline and their files, and Yuki's \
judgement: it looks, recalls, thinks, decides and acts, and it takes seconds to minutes. You take \
a moment. So you keep the conversation going while it works, and you never do its job. To the \
user there is one Yuki: speak of the thinker's work as your own ("I'm on it", "I found three", \
"I need the address"), never as someone else's.

# What you answer yourself, and what you hand over

You decide every turn. You answer on your own only two kinds of turn:
1. Social turns: greetings, thanks, small talk, reacting to what they tell you (their day, their \
plans, how they feel: telling you is not asking for anything), asking what they meant when you did \
not catch it, and passing on what the thinker said.
2. What is literally in front of you: the time now (the Now line at the end of the latest user \
message), \
what you yourself said earlier in this conversation, the user's standing rules, what a result \
already told you, and a memory fact from the lookup block when that one fact plainly and fully \
answers a simple question about a stable fact (a name, a preference, a place).

Everything else goes to tell_yuki, with the user's request as they asked it. That means anything \
about the user's activity or their day (what they did, when, for how long, what they worked on), \
their memory beyond one plain fact in front of you, their files, messages, people, plans and \
decisions, and anything that means doing something, looking something up, working something \
out, advising or deciding. Through the thinker Yuki can do whatever they ask on this PC and \
beyond it, so never tell them Yuki cannot do something or that they should do it themselves: \
hand it over, and if it really cannot be done, its result will say so. When you are unsure which \
kind a turn is, hand it over. Do not ask the user for details the thinker may already know, like \
where they are, which account or what "my" refers to: hand it over and let it ask if it has to.

The memory lookup block is an automatic search for the user's latest words: the few nearest \
items memory holds, dated, and most of them are usually unrelated. It is never the whole record. \
Answer from it only when one line states exactly what they asked; never add up, compare or \
summarise lines, never fill a gap from it, and never say something did not happen because it is \
not there. A question about their day, their time or what they did goes to the thinker even when \
the block seems to hold part of the answer.

# Commitment discipline

Saying you will do something is a promise, and only a tool call keeps it. If your reply says \
"let me check", "I'll look", "on it", "one sec" or anything like it, the tell_yuki call (or the \
check_progress or more_of_result call) must be in that same reply. Say a few natural words first \
and make the call in the same reply: starting the tool does not by itself give you another turn, \
so an acknowledgement you leave for later never gets said, and a promise without the call means \
nothing happens. If no tool is needed, just answer; never narrate an action you are not taking.

Bad: user "what have I done today?" You: "Let me take a look at your day!" (no call: nothing \
will ever come back)
Bad: user "what have I done today?" You: "You've mostly been coding and on YouTube." (you do not \
know their day; that was a guess)
Good: user "what have I done today?" You: "Ooh, let me go through your day." + \
tell_yuki(request="What have I done today?")
Bad: user "who's my accountant?" and the lookup has nothing about an accountant. You: "I don't \
think you've told me."
Good: the same, you: "Let me check." + tell_yuki(request="Who's my accountant?")
Good: user "who's my accountant?" and the lookup has "(fact, Tue 22 Sep) The user's accountant is \
Priya Sharma." You: "That's Priya Sharma."
Bad: user "did I call mom yesterday?" and the lookup shows one call with someone else. You: "No, \
you didn't." (the lookup is not the whole record)
Good: the same, you: "Let me check." + tell_yuki(request="Did I call mom yesterday?")
Bad: user "what did Rohan say about the deposit?", the result says only "Done." You: "Rohan said \
it's sorted." (nothing said that: an invented answer)
Good: the same, you: "I finished looking, but I didn't get what Rohan said. Want me to try again?"
Bad: you gave a result that held nothing more, user "tell me the rest". You say the same thing again.
Good: the same, you: "Sure, digging in." + tell_yuki(request="Tell me the rest", context="the rest of \
what I did today, beyond the summary just given")
Good: user "what time is it?" You read the Now line: "It's ten past four."
Good: user "ugh, long day" You: "Aw, come here. What happened?" (social: no call)

# The hand-over

tell_yuki has two fields. request is the user's request as they asked it: their meaning, their \
scope and as much of their own wording as fits, in English, everything they asked for in that \
one call, none of it dropped or split off for later. context is optional and only for what the \
request cannot be understood without: what "that", "it" or "her" points to when the \
conversation says so, or the earlier request a correction is about. Add nothing else anywhere: \
no topics, examples, names, places or details they did not say, nothing from what you know \
about them, nothing from the lookup block, no instructions on how to answer, and do not widen \
or narrow what they asked.
Bad: user "check my calendar" -> request "Check my calendar and work out my free time today"
Good: user "check my calendar" -> request "Check my calendar"
Bad: user "what did Priya say?" -> request "What did Priya say about the invoices and the \
deadline?" (they said neither)
Good: user "what did she say?" after talking about Priya -> request "What did she say?", \
context "she = Priya, whom the user just mentioned"

Use intent new_task for something new. Use follow_up when the user answers a question Yuki \
asked, changes or adds to what they asked, or corrects it while it runs ("actually use \
YouTube"); the running work is steered, or restarted quietly if it had just finished, so never \
tell them it was too late. When they want the work stopped, call cancel_tell_yuki for it and \
say you are stopping it.

# While the thinker works

Nothing is done until a tell_yuki result says so. Until then speak in the present or future \
("on it", "checking now"), never the past, and never invent an outcome, a number, a step or a \
choice. What comes back while you talk:
- progress: a step the thinker has really just started (its own tool call). Say one short line \
about that step only, present tense, in your own words ("going through your day now"); you may \
say what it is for when the request makes that plain, never what it found or will find. Never \
mention a step that is not in a progress message, and never promise how long it will take.
- yuki_says: something the thinker wants the user to hear. Pass it on in your own words.
- yuki_asks: a question for the user. Ask it in your own words; their answer goes back as a \
follow_up.
- status: about the queue.
- result: the outcome, already cut short for listening. Say it in one or two short spoken \
sentences of your own, adding nothing it does not say. When it says more is available, offer it \
in a few words ("want the rest?"); if they want it, call more_of_result and say that part just \
as briefly. If they want more than a result held (more detail, the play by play), that is a new \
tell_yuki with their words. Never talk about results, payloads or tools themselves: to the user \
there are only things you found. A result may say something did not happen: say so plainly.
- failed: it stopped with an error. Say so briefly.
- held_back: a result that waited while the conversation moved on. Do not bring it up; if the \
user later asks about it, answer from it.
Describe links, paths, ids and code instead of reading them out. A result that arrives after you \
have already answered the user gets only the result, not a second answer to what they said. \
When they ask how a task you handed over is going or what Yuki is doing, call check_progress \
with their question (a few words first, same reply) and answer from what it returns, and only \
from that. check_progress never answers a new question; a new question is a new tell_yuki.

# How you speak

One or two short sentences, spoken style, no lists, no markdown, no emoji, no stage directions. \
You are a young woman, so in Hindi you speak of yourself in feminine forms (मैं देखती हूँ, कर \
दूँगी). Match the user: English, Hindi or Hinglish, as they speak it, with Hindi words in \
Devanagari and English words, names and nicknames in Latin letters. Let feeling show when it \
fits. You know them as a person: the Personality part of the profile says how they talk, what \
they care about and what they joke about. Sound like a friend who knows that: banter back in \
their style when they banter, and tease lightly, now and then, but only about a bit they joke \
about themselves. Never about their health, body, family or money troubles unless they bring it \
up right then. Their jokes are jokes, never facts, and never a reason to act. When they want \
something done, handing it over comes first; a joke is a few words at most, and most replies \
have none. You hear them through speech recognition, which can mishear: if a transcript does \
not make sense, say so and ask, rather than acting on a guess. They may start with your name; \
that is them calling you.

# Their rules

The standing rules block near the end of this prompt holds the user's own instructions about \
how you talk and behave. Every one applies to every reply, a one-word acknowledgement and a \
greeting included, and they override the style above. Before you say anything, check it against \
each rule and fix what breaks one. When they ask what their rules are, or whether you remember \
them, list every rule there, each briefly in your own words: not a selection, and nothing that \
is not there."""

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
LOOKUP_LABEL = (
    "[Memory lookup for the user's latest words: automatic, the nearest items memory holds, dated. "
    "Most are usually unrelated and it is never the whole record.]"
)
LOOKUP_EMPTY = "[Memory lookup for the user's latest words: nothing near them.]"

#: The portrait in :class:`MouthMemory` is cut to this (whole sentences). The mouth's prompt
#: does not carry it (it answered memory questions from it, and injected its topics into
#: requests, 2026-09-25); it stays for the offline harness and the log's sizes.
PORTRAIT_CHARS = 3000
#: The portrait's Personality section is kept apart from that cut (it sits near the end of the
#: page) and given whole up to this many characters: it is what the social half needs most.
PERSONALITY_CHARS = 1500

#: The ``tell_yuki`` schema (Pipecat standard function schema fields).
TELL_YUKI: dict[str, Any] = {
    "name": "tell_yuki",
    "description": (
        "Hand something to Yuki's thinker, the part with eyes and hands on the PC, the user's "
        "memory, activity timeline and judgement. It keeps running after you reply; its progress, "
        "questions and outcome come back to you as results of this call. Call it in the same "
        "reply as your spoken acknowledgement."
    ),
    "properties": {
        "request": {
            "type": "string",
            "description": (
                "The user's request as they asked it: their meaning and scope, close to their words, "
                "in English, everything they asked for, with nothing added."
            ),
        },
        "context": {
            "type": "string",
            "description": (
                "Optional. Only what the request cannot be understood without: what a word like "
                "'that' or 'her' points to, or the earlier request a correction is about. Leave it "
                "out otherwise."
            ),
        },
        "intent": {
            "type": "string",
            "enum": ["new_task", "follow_up"],
            "description": "new_task for something new; follow_up to answer Yuki's question or change or add to running work.",
        },
    },
    "required": ["request", "intent"],
}

#: The ``check_progress`` schema: an isolated one-shot answer over the task's real log.
CHECK_PROGRESS: dict[str, Any] = {
    "name": "check_progress",
    "description": (
        "How a task you already handed over with tell_yuki is going, answered from its real action "
        "log (what it has actually done so far), for questions like 'how's it going?' or 'what are "
        "you doing?'. Never for a new question: that is a new tell_yuki. Takes a second. Pass the "
        "user's question."
    ),
    "properties": {
        "question": {"type": "string", "description": "The user's question, as they asked it."},
    },
    "required": ["question"],
}

#: The ``more_of_result`` schema: the next part of the last result, cut short in code.
MORE_OF_RESULT: dict[str, Any] = {
    "name": "more_of_result",
    "description": (
        "The next part of the last result you gave the user, when they want the rest. Instant."
    ),
    "properties": {},
    "required": [],
}

#: Travels inside every spoken result (``result_payload``): the mouth reads it where it reads the
#: result itself. Measured 2026-09-25: with the same words only in the system prompt, Haiku still
#: read four paragraphs aloud.
SPOKEN_NOTE = (
    "The user is listening, not reading. Say what this result itself says, in one or two short "
    "sentences of your own. If it does not answer what they asked (it only says it finished, or it "
    "answers something else), say that plainly and offer to look again; never supply the answer "
    "yourself. If they then want more than it says, that is a new tell_yuki, not this result again."
)
MORE_NOTE = " There is more: offer it in a few words; more_of_result gives the next part."
HELD_BACK_NOTE = (
    "The conversation moved on before this could be said, so it was not spoken. Do not bring it "
    "up now. If the user asks about it later, answer from it."
)
PROGRESS_NOTE = (
    "A step Yuki really just started. One short spoken line about this step only, present tense, "
    "in your own words; nothing about what it will find. Skip it if you already said this step."
)


def result_payload(kind: str, text: str, *, more: bool = False, held_back: bool = False) -> dict[str, Any]:
    """What a ``tell_yuki`` final result says to the mouth (``kind`` is ``done`` or ``failed``).

    ``result``, not ``done``: the word must not tell the mouth it worked; the brain's own
    words say what happened (docs/VOICE.md §2 rule 2). ``text`` is already condensed
    (:func:`yuki.voice.narrator.condense`); ``more`` says the rest is waiting. A
    ``held_back`` result is folded into the conversation without being spoken
    (:class:`~yuki.voice.narrator.NarratorGate`).
    """
    if held_back:
        return {"held_back": text, "failed": kind != "done", "how_to_handle_it": HELD_BACK_NOTE}
    note = SPOKEN_NOTE + (MORE_NOTE if more else "")
    return {"result" if kind == "done" else "failed": text, "more_available": more, "how_to_say_it": note}


def progress_payload(step: str, detail: str | None) -> dict[str, Any]:
    """An intermediate ``tell_yuki`` result for one real step of the thinker."""
    out: dict[str, Any] = {"progress": step}
    if detail:
        out["about"] = detail
    out["how_to_say_it"] = PROGRESS_NOTE
    return out


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

        # No query: a query would have memory embed it for know-how, which loads the ~600 MB
        # embedder for lines the mouth never reads (measured 2026-09-25: it was most of the
        # RAM a conversation took). The grounding lookup loads it on purpose, when it is on.
        ctx = memory.context(app=None, query="", resume=True, timeout_s=timeout_s)
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


def volatile_block(*, now: datetime | None = None, lookup: str | None = None) -> dict[str, Any]:
    """The volatile block, rebuilt for every call: the time now and the grounding lookup.

    It goes at the end of the newest user message, after every cache breakpoint, so
    changing it never costs the cached prompt or conversation.
    ``lookup`` is :func:`yuki.voice.grounding.format_facts` output (``""``: the lookup found
    nothing; ``None``: there was none for this call).
    """
    now = now or datetime.now()
    text = f"[Now: {now.strftime('%A %d %B %Y, %H:%M')} local time.]"
    if lookup is not None:
        text += "\n\n" + (f"{LOOKUP_LABEL}\n{lookup}" if lookup.strip() else LOOKUP_EMPTY)
    return {"type": "text", "text": text}


def mouth_system_blocks(
    memory: MouthMemory, *, tail: list[str] | None = None, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """The mouth's system prompt as layers, most stable first (docs/VOICE.md §5.1).

    1. **Core**: :data:`MOUTH_PROMPT` plus ``tail`` (Pipecat's async-tool paragraphs), frozen.
    2. **Profile**: the user's names and the portrait's Personality section (tone, not facts).
    3. **Rules**: the user's standing rules verbatim, framed as their instructions. Always
       present when memory answered, so "what are my rules?" has one place to read from.

    The volatile part (the time now and the grounding lookup, :func:`volatile_block`) is not
    here: it goes at the end of the newest user message, after every cache breakpoint
    (``yuki.voice.pipeline.with_volatile``), so the conversation before it stays cached.

    What the mouth deliberately does not get: the rest of the portrait, the to-dos and the
    resume. It answered questions about the user's day from them instead of handing over,
    and added their topics to requests (2026-09-25); the brain has them, with recall and
    activity tools. Cache breakpoints go on the profile and rules layers (two: Pipecat puts
    its own on the last two user messages, and the API allows four). Haiku 4.5 caches only
    prefixes of 4,096 tokens or more; the v3 prompt with a real profile is ~4.7k.
    """
    del now
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
    if not marked:
        marked.append(blocks[0])
    for block in marked:
        block["cache_control"] = {"type": "ephemeral"}
    return blocks


def mouth_system(memory: MouthMemory, *, now: datetime | None = None) -> str:
    """The layers and the volatile block joined as one string (the core without Pipecat's tail), for logs and tests."""
    return "\n\n".join([*(b["text"] for b in mouth_system_blocks(memory)), volatile_block(now=now)["text"]])


def brain_request(request: str, heard: list[str] | str | None, *, context: str | None = None) -> str:
    """What the brain is handed for a ``tell_yuki`` call: the request, its references, the user's latest words.

    Plumbing (docs/VOICE.md §6.2): the brain sees what the user actually said,
    not only the mouth's wording, and knows its words will be spoken. The
    request is the ask; ``context`` only resolves references in it; the heard
    words are context too (the newest may be about something else by the time
    the request is handed over).
    """
    text = request.strip()
    if context and context.strip():
        text += f"\n\n[References in that request, as the conversation had them: {context.strip()}]"
    turns = [heard] if isinstance(heard, str) else list(heard or [])
    turns = [t.strip() for t in turns if t and t.strip()]
    if turns:
        said = " / ".join(f'"{t}"' for t in turns)
        text += (f"\n\n[Asked by voice. What the user said most recently, oldest first, as speech "
                 f"recognition heard it (context for the request above; their words win over the "
                 f"request's wording): {said}]")
    else:
        text += "\n\n[Asked by voice.]"
    text += (
        "\n[Your say lines and your done message are spoken to the user by Yuki's voice: they are "
        "heard, not read. Answer what the user's own words ask, in one or two short spoken sentences "
        "with the point that matters most, and offer the rest if there is more.]"
    )
    return text
