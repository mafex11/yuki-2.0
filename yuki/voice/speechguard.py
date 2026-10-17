"""The last check before Yuki's voice says a line: only what a person would say to the user (docs/VOICE.md §10, v3.10).

Live 2026-09-28 09:06-09:11 the talker spoke its own private reasoning aloud, over and over: "They're on a voice
call on Zen. Those words were for me, so I'll switch to the second YouTube tab.", "Speech recognition was unsure
of 'brochure'. Let me check what they meant.", "Let me hand this to the thinker to close that tab.", "Looking at
the tabs, tab 9 is YouTube...". The user: "it felt like talking with 2 people at the same time". The prompt says
never to write reasoning (it did since v3.2); this is the structural backstop: every line the voice is about to
say passes one cheap, isolated check by the fast model before it is heard.

**What is checked, and how.** One short Haiku call per spoken piece (a sentence or the first clause), with the
user's latest words and the lines said just before it, answering one word: ``SAY`` or ``DROP: <why>`` (thinking
out loud, talking about the user instead of to them, Yuki's inner workings). A model's judgement from worked
examples, never a list of words. Two things need no model: a line of at most :data:`FREE_WORDS` words ("On it.",
"Paused.") cannot carry reasoning, and a line identical to one already said since the user last spoke (the same words, case and punctuation aside) is a repeat ("YouTube's up. YouTube's
up."); a new request of theirs ("again") makes "Done." new again.

**No added wait.** The check starts the moment a piece is written, alongside its speech request: the Gemini voice
holds the piece's *audio* until the verdict (``GeminiTTSService`` in :mod:`yuki.voice.gemini_tts`), and Gemini's
first audio takes ~1.0-1.3 s while the check takes ~0.6-0.9 s, so a kept line is heard as soon as it would have
been. A dropped piece is never played and its words never reach the conversation's context (the TTS releases a
piece's words only after its audio), so the talker does not see itself saying it and copy it. Other voices check
the text before sending it (a short wait, only on the fallback voices).

**Fails closed (v3.13).** Live 2026-09-29 01:26 a stray word after "Bye" was answered aloud with the talker's own
notes ("I was asked to stop listening a moment ago. These words ... are incomplete..."). Now a line longer than
:data:`FREE_WORDS` is heard only on the check's SAY: no verdict within ``timeout_s``, or an error, drops it (a
short plain line still goes out). Once a piece of a reply is caught, the rest of that reply is dropped too (the
voice's player, :mod:`yuki.voice.gemini_tts`). A reply's first piece is checked even when short, and the check
is told it starts a reply: a sentence's tail spoken on its own ("or a tab you had open in Warp before?", the
unsaid rest of a cut question, live 01:30) is a ``fragment``.

Logs are content-free: ``speech_guard`` with ``op`` caught / timeout / error, the reason, the characters and the
milliseconds; a ``summary`` at the end of the conversation.

**Shadow mode (v3.16, ``Settings.voice_speech_channel = "marker"``).** With the marker channel the talker's reply
says itself which of its text is speech (:data:`yuki.voice.mouth.MARKERS`), so this check stops being load-bearing:
it still scores every spoken line with the same model and logs what it would have dropped (``op`` caught with
``shadow`` true), but it never blocks, never holds a line's audio for its verdict and never drops the rest of a
reply. A leak of reasoning into speech is then a measured number (its ``shadow_caught`` count), not a silence.

**The claim gate (kMind, audit 17 Oct; ``Hands.acted_this_turn``).** Live test 1 had 12 false "done" lines, four of
them a reply that ran nothing ("Excalidraw's open in Chrome"). For the lean talker (``code_only``), while the
conversation's ``claim_open()`` says nothing was done for the user's current turn (no hand or skill of the talker's
verified anything, no reflex did it, no call in this turn yet), each line the talker writes gets one isolated check by
the fast model (:data:`CLAIM_PROMPT`, one word: ``CLAIM`` or ``OK``) and its audio waits for it (``blocks``). The check
reads the line against their latest words: only a line that tells them what they asked for is done, or already so, is
a claim; a truthful answer to "is it paused?" or "did it send?" is not (review H1). A claim is dropped in every mode
(``why`` ``claim``), ``on_claim`` is told (the conversation then answers the turn again, told so), and the rest of the
reply goes too. No verdict in time or an error says the line (fail open: a false claim is rarer than every other line).
A model's judgement, never a word list. Lines said by code are not the talker's and never claim-checked: owed outcomes
and results pass the guard (``unguard``), and every other line code says (a hand's or skill's done line, a step line,
an acknowledgement) is told to the guard as it goes to the voice (:meth:`SpeechGuard.code_line`), so its pieces skip the
claim check (the internal-word and repeat checks still apply).

**The ask check (fTalk, live test A of 18 Oct).** A claim needs both halves of :data:`CLAIM_PROMPT`: their words ask for
something to be done, and the line says it is. The first half depends on their words alone, so it is judged once per
user turn, the moment the words are known (:meth:`SpeechGuard.prejudge`, at Flux's eager end of turn and again at the
final words when they differ), in parallel with the talker's own first token (~1.3-1.7 s), by the same fast model
(:data:`ASK_PROMPT`, one word: ``ASK`` or ``NO``, with Yuki's last line for context: "yeah" after "Want it on?" is an
ask). The verdict belongs to one utterance: it is dropped when they start speaking again
(:meth:`SpeechGuard.new_utterance`, which also takes Yuki's last line before their words, so a speculative reply's own
pieces never become its context), and it is reused only for the same words against the same last line (review H1: a
"yeah" to a question and a later "yeah" to an offer are two checks). When it says ``NO`` (plain small talk, a question
about how things are, a reaction) no line of that turn can be a claim, so its lines go out at once, unchecked (``by``
``claim_free``); ``ASK`` (a correction, a complaint or a hint at their moment counts, review L1), no answer yet or an
error keeps the line check as before, and the two race: a ``NO`` that lands first releases the line too. Live test A
measured the line check at p50 887 ms, held after the talker's first words on every plain reply (small talk 3.2-3.4 s
to first audio; "didn't catch that" 1.2 s from text to voice); the ask check is done before those words exist.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

#: A line of at most this many words is said without a model check ("On it.", "Done.", "Yeah, what's up?"): too short
#: to carry reasoning. (Measured offline 2026-09-28: asked to judge repeats too, the model dropped outcomes said after
#: an acknowledgement, "Opening it." then "Playing it now."; repeats are now only exact ones, and two openings of the
#: same thing never happen: the hand runs once.)
FREE_WORDS = 3
#: The lines said in the last this many seconds are shown to the check (a repeat in other words).
REPEAT_S = 20.0
#: What the check may answer when it drops a line.
REASONS = ("reasoning", "about_user", "internals", "fragment", "repeat", "claim")   # "repeat": the exact-repeat check

#: kMind: the claim gate's check (module docstring): one line, judged against the user's latest words, for which nothing
#: was done yet. Review H1 (18 Oct): a truthful answer to "is it paused?" or "did it send?" is no claim.
CLAIM_PROMPT = """\
You check one line that Yuki, a voice assistant on the user's PC, is about to say out loud, against the user's latest \
words. Nothing has been done on their PC for those words yet: no action ran for them.
Answer CLAIM only when their latest words ask Yuki to do or change something (open, play, pause, send, close, switch, \
set, show) and the line tells them it is done or that it is already so: "Done.", "There you go.", "Sent.", "It's up \
in Chrome.", "Already playing."
Answer OK for everything else. An answer to their question about how things are now, or about what was done \
earlier, is OK ("is spotify running?" -> "Yep, it's running."; "did the email go?" -> "Yes, it went out earlier."; \
"what did you change?" -> "I closed the tab and turned it down."). So is a \
question, small talk, sympathy, a joke, an offer, saying Yuki is on it or about to do it, saying it can't, or saying \
it is not done or did not work. When unsure, OK.
One word: CLAIM or OK."""

#: fTalk: the first half of the claim check, judged once per user turn from their words alone (module docstring).
ASK_PROMPT = """\
You read the user's latest words to Yuki, a voice assistant on their PC (speech recognition may have misheard them; \
Hindi and Hinglish are common). Answer ASK when the words ask Yuki to do or change something on the PC now (open, \
play, pause, put on, send, message, close, switch, set, turn up or down, show, type); say yes to Yuki's offer or \
question just before them; correct or complain about what Yuki just did or about something still wrong on the PC \
("not that", "another one", "a bit more", "it's still loud", "that didn't work"); or tell Yuki about their moment in \
a way they may want acted on (eating, bored, starting work, heading out). Answer NO only for plain small talk, a \
greeting, a question about how things are or what happened, a reaction to something that is fine, thanks. When unsure, \
ASK.
One word: ASK or NO."""

GUARD_PROMPT = """\
You check one line that Yuki, a voice assistant on the user's PC, is about to say out loud to the user. The line \
was written by Yuki's fast talker model, which sometimes thinks out loud by mistake. A person helping someone \
speaks TO them: short, natural, about what is being done, what was found, or a question for them.

DROP the line when it is any of these:
- reasoning: thinking out loud, its own deliberation, plan or judgement about what to do or whom the words were \
for ("Those words were for me, so I'll switch tabs.", "Let me check what they meant.", "I need to see what's on \
the screen to find the button.", "I'll stay quiet.", "That sounds like dictation, not for me."), commentary on \
the user's words or on what it was told instead of an answer ("I was asked to stop listening a moment ago.", \
"These words are incomplete, just the start of a sentence in Hindi.", "There's no clear request here, so I'm \
staying quiet.", "That was probably not meant for me."), or a note to itself: their request echoed back as a \
command, an order said to no one, neither a question to them nor what Yuki is doing (they said "close the second \
YouTube tab" -> "Close the second YouTube tab."; "in warp window open the llm os chat" -> "In Warp, open the LLMOS \
chat."; but "Open Warp?" asks them and "Opening it." says what Yuki does: both SAY)
- about_user: talking ABOUT the user instead of to them ("They're on a voice call on Zen.", "The user wants the \
second tab.", "He asked for YouTube.")
- internals: Yuki's inner workings: the thinker, handing over, speech recognition, transcripts, tools, call mode, \
ids, numbered tabs or windows ("Let me hand this to the thinker.", "Speech recognition was unsure of that word.", \
"Was that Hindi, or did the speech recognition mix it up?", "Tab 9 is YouTube and tab 10 is the one showing.", \
"The tab tool refused.")
- fragment: only when the line STARTS a reply (you are told so): not a whole thing to say on its own but the \
tail of a sentence never said ("or a tab you had open in Warp before?", "or is the sidebar open right now?", \
"or something else?", "and the second one too."). A short whole reply is no fragment ("On it.", "Yes.", \
"Which one?", "Done, it's open.", "Bye!").

SAY everything else: what Yuki is doing right now while it works, said as it does it, like a helper who talks \
while their hands are busy ("Checking the other tab.", "Looking through the page.", "Switching to the next tab.", \
"Opening your Warp tabs.", "Searching for it now."; the user asked for this), acknowledgements ("Sure, opening \
it."), outcomes, failures too ("Closed it.", "Got it, the \
Bro chat's open.", "That click opened a different tab.", "Couldn't find it on the screen."), questions to the user ("Sorry, which chat?", "The Claude tabs?"), what was found, small talk, jokes, \
sympathy, a goodbye, in any language ("Kal subah standup hai."). Asking the user what they meant is fine; \
explaining your own doubts is not. Speech recognition mishears the user, so a name in the line may differ from \
their words ("open the rest chat" -> "Opened Res's chat."): that is still an outcome. Saying something again is \
not your concern here. Drop only what is clearly one of the four; when unsure, SAY.

Answer with exactly one word, SAY, or DROP followed by a colon and one reason word: reasoning, about_user, \
internals or fragment. Nothing else."""


#: Short first lines the model said SAY to in this process (v3.15), by their words: said again, they need no model.
#: The Gemini voice keeps the same knowledge with its cached audio (v3.14); the voices without an audio gate
#: (Aura, ElevenLabs) check a line BEFORE sending it, so a model call there is the whole first-audio wait
#: (~1 s on a ~0.2 s voice). Bounded, oldest first out.
_APPROVED_FIRST: "OrderedDict[str, None]" = OrderedDict()
APPROVED_FIRST_MAX = 512


def approve_first(text: str) -> None:
    norm = _norm(text)
    if norm:
        _APPROVED_FIRST[norm] = None
        _APPROVED_FIRST.move_to_end(norm)
        while len(_APPROVED_FIRST) > APPROVED_FIRST_MAX:
            _APPROVED_FIRST.popitem(last=False)


@dataclass
class Verdict:
    ok: bool
    why: str = ""
    ms: float = 0.0
    by: str = "model"   # model | free | repeat | timeout | error | off | shadow | claim | claim_ok (kMind)


def _norm(text: str) -> str:
    """The words of a line, case and punctuation aside (Yuki's own words: plumbing for the repeat check)."""
    return " ".join("".join(ch if ch.isalnum() else " " for ch in (text or "").casefold()).split())


def parse_verdict(answer: str) -> tuple[bool, str]:
    """``(say, why)`` from the check's one-word answer; anything unreadable says the line (fail open)."""
    head = (answer or "").strip().split("\n", 1)[0].strip()
    word = head.split(":", 1)[0].strip().upper().strip(".!* ")
    if word == "DROP":
        why = head.split(":", 1)[1].strip().strip(".!* ").casefold() if ":" in head else ""
        return False, why if why in REASONS else (why[:20] or "reasoning")
    return True, ""


class SpeechGuard:
    """Checks each spoken line before it is heard (see the module docstring). One per conversation.

    Args:
        client: The conversation's Anthropic client (Bedrock), used with ``await client.messages.create``.
        model: The fast model's id (the talker's own).
        log: The voice log (content-free ``event``) or ``None``.
        context: ``() -> {"user": latest user words}`` for the check (``None``: none).
        timeout_s: How long a verdict is waited for once the audio is ready, at most, before the line goes out.
        judge: A stand-in for the model call in tests: ``async (text, said, user) -> answer``.
        shadow: Score and log only, never block (v3.16, the marker channel; module docstring).
        code_only: bTalk (brain rebuild rev 2, design 3; ``brain = "all"``): no model call at all; only the two code
            rules stay (Yuki's own internal words, an exact repeat since the user last spoke), and every other line
            goes out at once.
    """

    def __init__(self, client: Any, model: str, *, log: Any = None,
                 context: Callable[[], dict[str, Any]] | None = None, timeout_s: float = 2.5,
                 judge: Callable[[str, list[str], str], Any] | None = None, session: str = "",
                 shadow: bool = False, code_only: bool = False) -> None:
        self.client = client
        self.model = model
        self.log = log
        self.context = context
        self.timeout_s = float(timeout_s)
        self.judge = judge
        self.session = session
        self.shadow = bool(shadow)
        self.code_only = bool(code_only)
        #: ``()``: a line was dropped (the turn log keeps only what was heard); set by the conversation.
        self.on_drop: Callable[[], None] | None = None
        #: kMind (the claim gate): ``() -> bool``: nothing was done for the user's current turn yet, so the talker's
        #: lines are checked for a claim of something done (set by the conversation for the lean talker; ``None``: off).
        self.claim_open: Callable[[], bool] | None = None
        #: ``()``: a line was dropped as a claim of something not done (set by the conversation).
        self.on_claim: Callable[[], None] | None = None
        self._waits: set[Any] = set()   # futures whose audio waits for the verdict in every mode (the claim gate)
        #: kMind (review L6): lines code sent to the voice lately, ``(at, normalised)``: never claim-checked
        self._code_lines: deque[tuple[float, str]] = deque(maxlen=16)
        self._said: deque[tuple[float, str, str]] = deque(maxlen=12)   # (at, words, normalised) of lines let through
        self._turn: set[str] = set()   # the lines said since the user last spoke (normalised): exact repeats
        #: The same lines in words, for the model's repeat judgement: only what answers the user's latest words (a
        #: new request's "Let me find it." is no repeat of the last one's "Done."; offline 2026-09-28 it was).
        self._turn_lines: list[str] = []
        self._pending: set[str] = set()   # lines being checked now (a second copy of one is a repeat too)
        #: The pieces written in the last moments, kept or not, in order: a reply's first clause and its next
        #: sentence are checked apart ("Those words were for me," / "so I'll switch tabs."), so each check sees
        #: the pieces before it, a dropped one marked as such.
        self._written: deque[tuple[float, str]] = deque(maxlen=8)
        self._tasks: set[asyncio.Task] = set()
        #: fTalk (the ask check): ``(their words normalised, Yuki's last line before them, future of True ask /
        #: False no / None unknown)`` for the utterance being answered (:meth:`prejudge`); a line is matched to it by
        #: the words the claim check reads. Dropped when they start speaking again (:meth:`new_utterance`).
        self._ask: tuple[str, str, "asyncio.Future[bool | None]"] | None = None
        #: Yuki's last line as it stood when they began this utterance (``None``: not taken, read it at the check).
        self._ask_last: str | None = None
        self.stats: dict[str, Any] = {"checked": 0, "free": 0, "caught": 0, "repeats": 0, "timeouts": 0, "errors": 0,
                                      "shadow_caught": 0, "input_tokens": 0, "output_tokens": 0, "ms": []}

    # -- the check -------------------------------------------------------------------------------------

    def _recent(self, now: float) -> list[str]:
        return list(self._turn_lines[-4:])

    def start(self, text: str, *, first: bool = False, known_ok: bool = False) -> "asyncio.Future[Verdict]":
        """Begin checking ``text`` now; returns a future verdict (on the running loop).

        ``first``: the piece starts a reply (checked even when short: a fragment often is). ``known_ok``: these exact
        short words were said SAY to as a reply's first piece before (the voice cache, v3.14): a short line then needs
        no model, as it needs none anywhere else in a reply (the exact-repeat check still applies).
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Verdict] = loop.create_future()
        now = time.monotonic()
        norm = _norm(text)
        if not norm:
            fut.set_result(Verdict(True, by="free"))
            return fut
        from yuki.core.ownwords import internal_word

        if internal_word(text) is not None:
            # Yuki's own internal vocabulary (the thinker, a tool name, a stray marker word: report 76): never heard,
            # in shadow mode too. A fixed list of Yuki's own words, no model (yuki.core.ownwords).
            self.stats["vocab"] = self.stats.get("vocab", 0) + 1
            fut.set_result(Verdict(False, "internals", by="vocab"))
            return fut
        if norm in self._turn or norm in self._pending:
            self.stats["repeats"] += 1
            fut.set_result(Verdict(False, "repeat", by="repeat"))
            return fut
        said = self._recent(now)
        written = [t for at, t in self._written if now - at < REPEAT_S]
        self._written.append((now, text.strip()))
        if self.code_only:
            if self._claim_open() and not self._by_code(norm, now):
                # kMind: nothing done for their words yet: the line waits for the claim check (module docstring)
                ask = self._ask_for(self._user_words())
                if ask is not None and ask.done() and not ask.cancelled() and ask.result() is False:
                    # fTalk: their words asked for nothing to be done, so no line of this turn can claim it was
                    self.stats["claim_free"] = self.stats.get("claim_free", 0) + 1
                    fut.set_result(Verdict(True, by="claim_free"))
                    return fut
                self._pending.add(norm)
                self._waits.add(fut)
                fut.add_done_callback(self._waits.discard)
                task = loop.create_task(self._check(text, said, fut, written, first, claim=True, ask=ask))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                return fut
            # bTalk: the model call is off (design 3); the two code rules above decided, and the line goes out
            self.stats["free"] += 1
            self.heard(text)
            fut.set_result(Verdict(True, by="free"))
            return fut
        if len(norm.split()) <= FREE_WORDS and (not first or known_ok or norm in _APPROVED_FIRST):
            self.stats["free"] += 1
            self.heard(text)   # short lines go straight out: remember them for the repeat check now
            fut.set_result(Verdict(True, by="free"))
            return fut
        self._pending.add(norm)
        task = loop.create_task(self._check(text, said, fut, written, first))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return fut

    #: A line code sent to the voice is matched to its spoken pieces for this long (s).
    CODE_LINE_S = 30.0

    def code_line(self, text: str) -> None:
        """kMind (review L6): ``text`` goes to the voice from code (a ``TTSSpeakFrame``: a done line, a step line, an
        acknowledgement), not from the talker's reply: its pieces are never claim-checked."""
        norm = _norm(text)
        if norm:
            self._code_lines.append((time.monotonic(), norm))

    def _by_code(self, norm: str, now: float) -> bool:
        """Whether a piece is (part of) a line code sent to the voice in the last :data:`CODE_LINE_S` seconds."""
        piece = f" {norm} "
        return any(now - at < self.CODE_LINE_S and piece in f" {line} " for at, line in self._code_lines)

    def _claim_open(self) -> bool:
        fn = self.claim_open
        if fn is None:
            return False
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001 - no gate rather than a broken line
            return False

    def _user_words(self) -> str:
        """The user's latest words, as the claim check reads them (``context``); ``""`` when unknown."""
        if self.context is None:
            return ""
        try:
            return str((self.context() or {}).get("user") or "")
        except Exception:  # noqa: BLE001
            return ""

    # -- fTalk: the ask check (module docstring) ---------------------------------------------------------------

    #: Yuki's last line is shown to the ask check when it was said within this many seconds ("yeah" after an offer).
    ASK_CONTEXT_S = 120.0

    def _last_said(self) -> str:
        """Yuki's last line let through in the last :data:`ASK_CONTEXT_S` seconds, ``""`` when none."""
        now = time.monotonic()
        return next((t for at, t, _n in reversed(self._said) if now - at < self.ASK_CONTEXT_S), "")

    def new_utterance(self) -> None:
        """The user began speaking (a new utterance): the last ask verdict is not theirs any more, and Yuki's last line
        is taken now, before any piece of a speculative reply to these words can be let through."""
        self._ask = None
        self._ask_last = self._last_said()

    def prejudge(self, words: str) -> None:
        """Judge now, in the background, whether the user's latest ``words`` ask for something to be done (the first
        half of the claim check, :data:`ASK_PROMPT`). Only while the claim gate is on (the lean talker). The verdict
        depends on the words and on Yuki's last line before them, so it is reused only when both are the same (the
        eager and the final words of one utterance), else judged again. Call it on the voice loop when their words are
        known (an eager end of turn, the final words). Never raises."""
        if not self.code_only or self.claim_open is None or self.judge is not None:
            return   # no claim gate, or a stand-in judge (tests keep the one-call check)
        norm = _norm(words)
        if not norm:
            return
        last = self._ask_last if self._ask_last is not None else self._last_said()
        if self._ask is not None and self._ask[0] == norm and self._ask[1] == last:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        fut: asyncio.Future[bool | None] = loop.create_future()
        self._ask = (norm, last, fut)
        task = loop.create_task(self._run_ask(" ".join(str(words).split()), last, fut))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _ask_for(self, user: str) -> "asyncio.Future[bool | None] | None":
        """The ask check's future for these words in the utterance being answered (``None``: none was started)."""
        got = self._ask
        return got[2] if got is not None and got[0] == _norm(user) else None

    async def _run_ask(self, words: str, last: str, fut: "asyncio.Future[bool | None]") -> None:
        started = time.monotonic()
        result: bool | None = None
        try:
            content = (f"Yuki's last line before their words: \"{last or '(nothing yet)'}\"\n"
                       f"Their latest words: \"{words}\"")
            response = await self.client.messages.create(model=self.model, max_tokens=3, system=ASK_PROMPT,
                                                         messages=[{"role": "user", "content": content}])
            usage = getattr(response, "usage", None)
            self.stats["input_tokens"] += int(getattr(usage, "input_tokens", 0) or 0)
            self.stats["output_tokens"] += int(getattr(usage, "output_tokens", 0) or 0)
            answer = "".join(getattr(b, "text", "") for b in getattr(response, "content", []) or [])
            word = (answer or "").strip().split("\n", 1)[0].strip().upper().strip(".!* ")
            result = False if word.startswith("NO") else True if word.startswith("ASK") else None
        except asyncio.CancelledError:
            if not fut.done():
                fut.set_result(None)
            raise
        except Exception as exc:  # noqa: BLE001 - unknown: the line check decides, as before
            self._log(op="ask_error", error=type(exc).__name__)
        ms = round((time.monotonic() - started) * 1000)
        self.stats["asks_checked"] = self.stats.get("asks_checked", 0) + 1
        if result is False:
            self.stats["asks_no"] = self.stats.get("asks_no", 0) + 1
        self.stats.setdefault("ask_ms", []).append(ms)
        if not fut.done():
            fut.set_result(result)

    async def _claim_verdict(self, text: str, user: str, ask: "asyncio.Future[bool | None] | None") -> Verdict:
        """The claim check for one line, raced with the turn's ask check: a ``NO`` that lands first frees the line
        (``claim_free``), else the line's own verdict (``claim`` / ``claim_ok``)."""
        line = asyncio.ensure_future(self._ask_claim(text, user))
        try:
            if ask is not None and not ask.done():
                await asyncio.wait({line, ask}, return_when=asyncio.FIRST_COMPLETED)
            if ask is not None and ask.done() and not ask.cancelled() and ask.result() is False:
                if not line.done():
                    line.cancel()
                self.stats["claim_free"] = self.stats.get("claim_free", 0) + 1
                return Verdict(True, by="claim_free")
            answer = await line
        except asyncio.CancelledError:
            line.cancel()
            raise
        is_claim = (answer or "").strip().split("\n", 1)[0].strip().upper().strip(".!* ").startswith("CLAIM")
        self.stats["claims_checked"] = self.stats.get("claims_checked", 0) + 1
        return Verdict(False, "claim", by="claim") if is_claim else Verdict(True, by="claim_ok")

    async def _check(self, text: str, said: list[str], fut: "asyncio.Future[Verdict]",
                     written: list[str] | None = None, first: bool = False, claim: bool = False,
                     ask: "asyncio.Future[bool | None] | None" = None) -> None:
        started = time.monotonic()
        user = self._user_words()
        verdict: Verdict
        try:
            if claim:
                verdict = await self._claim_verdict(text, user, ask)
            else:
                answer = await self._ask(text, said, user, written or [], first)
                ok, why = parse_verdict(answer)
                verdict = Verdict(ok, why, by="model")
        except asyncio.CancelledError:
            if not fut.done():
                fut.set_result(Verdict(self.short(text), "unchecked", by="error"))
            raise
        except Exception as exc:  # noqa: BLE001 - closed for a long line, open for a short one (v3.13)
            self.stats["errors"] += 1
            verdict = Verdict(self.short(text), "unchecked", by="error")
            self._log(op="error", error=type(exc).__name__)
        verdict.ms = round((time.monotonic() - started) * 1000)
        if first and verdict.ok and verdict.by == "model" and len(_norm(text).split()) <= FREE_WORDS:
            approve_first(text)
        self.stats["checked"] += 1
        self.stats["ms"].append(verdict.ms)
        if not fut.done():
            fut.set_result(verdict)

    async def _ask(self, text: str, said: list[str], user: str, written: list[str] | None = None,
                   first: bool = False) -> str:
        if self.judge is not None:
            result = self.judge(text, said, user)
            return await result if asyncio.iscoroutine(result) else str(result)
        before = " / ".join(f'"{x}"' for x in said[-4:]) or "(nothing yet)"
        just = [x for x in (written or []) if x not in said[-4:]][-3:]
        pieces = (f"\nWritten just before it (not heard, or dropped): " + " / ".join(f'"{x}"' for x in just)) if just else ""
        content = (f"The user's latest words: \"{user or '(none)'}\"\nLines Yuki said just before: {before}{pieces}\n"
                   + ("This line STARTS a new reply: nothing of it was written before it.\n"
                      f"The line to check: \"{text}\"" if first else
                      f"The line to check (it may be the rest of a sentence begun just before): \"{text}\""))
        response = await self.client.messages.create(model=self.model, max_tokens=8, system=GUARD_PROMPT,
                                                     messages=[{"role": "user", "content": content}])
        usage = getattr(response, "usage", None)
        self.stats["input_tokens"] += int(getattr(usage, "input_tokens", 0) or 0)
        self.stats["output_tokens"] += int(getattr(usage, "output_tokens", 0) or 0)
        return "".join(getattr(b, "text", "") for b in getattr(response, "content", []) or [])

    async def _ask_claim(self, text: str, user: str) -> str:
        if self.judge is not None:
            result = self.judge(text, [], user)
            return await result if asyncio.iscoroutine(result) else str(result)
        content = f"The user's latest words: \"{user or '(none)'}\"\nThe line to check: \"{text}\""
        response = await self.client.messages.create(model=self.model, max_tokens=4, system=CLAIM_PROMPT,
                                                     messages=[{"role": "user", "content": content}])
        usage = getattr(response, "usage", None)
        self.stats["input_tokens"] += int(getattr(usage, "input_tokens", 0) or 0)
        self.stats["output_tokens"] += int(getattr(usage, "output_tokens", 0) or 0)
        return "".join(getattr(b, "text", "") for b in getattr(response, "content", []) or [])

    async def decide(self, fut: "asyncio.Future[Verdict]", text: str, *, wait_s: float | None = None) -> Verdict:
        """The verdict for a line whose audio (or text) is ready now: waits at most ``wait_s`` (default the timeout).

        A kept line is remembered for the repeat check; a dropped one is logged and reported (``on_drop``).
        """
        wait = self.timeout_s if wait_s is None else max(0.0, wait_s)
        try:
            verdict = await asyncio.wait_for(asyncio.shield(fut), timeout=wait) if not fut.done() else fut.result()
        except asyncio.TimeoutError:
            # Fails closed for anything but a short plain line (v3.13): an unchecked long line is never heard.
            self.stats["timeouts"] += 1
            self._log(op="timeout", chars=len(text), waited_ms=round(wait * 1000), said=self.short(text) or self.shadow,
                      **({"shadow": True} if self.shadow else {}))
            verdict = Verdict(self.short(text) or self.shadow, "unchecked", by="timeout")
        self._pending.discard(_norm(text))
        if verdict.ok:
            if verdict.by != "free":
                self.heard(text)
            return verdict
        if self.shadow and verdict.by not in ("vocab", "claim"):
            # v3.16: the marker channel decides what is spoken; this verdict is only measured (a leak, if any, is
            # logged with its reason) and the line is said. A check that failed has no verdict: not a catch. Yuki's
            # own internal words (``vocab``) are dropped in every mode.
            self.heard(text)
            if verdict.by in ("error", "timeout"):
                return Verdict(True, verdict.why, verdict.ms, by=verdict.by)
            self.stats["shadow_caught"] += 1
            self._log(op="caught", why=verdict.why, by=verdict.by, chars=len(text), ms=verdict.ms, shadow=True)
            return Verdict(True, verdict.why, verdict.ms, by="shadow")
        self.stats["caught"] += 1
        self._log(op="caught", why=verdict.why, by=verdict.by, chars=len(text), ms=verdict.ms)
        if verdict.by == "claim" and self.on_claim is not None:
            try:
                self.on_claim()
            except Exception:  # noqa: BLE001
                pass
        if self.on_drop is not None:
            try:
                self.on_drop()
            except Exception:  # noqa: BLE001
                pass
        return verdict

    @staticmethod
    def short(text: str) -> bool:
        """A line of at most :data:`FREE_WORDS` words (said even unchecked: too short to carry reasoning)."""
        return len(_norm(text).split()) <= FREE_WORDS

    def drop_rest(self, text: str) -> Verdict:
        """A later piece of a reply one of whose pieces was caught: dropped unheard (v3.13), logged and reported."""
        self._pending.discard(_norm(text))
        self.stats["caught"] += 1
        self._log(op="caught", why="rest_of_reply", by="reply", chars=len(text), ms=0)
        if self.on_drop is not None:
            try:
                self.on_drop()
            except Exception:  # noqa: BLE001
                pass
        return Verdict(False, "rest_of_reply", by="reply")

    async def allows(self, text: str) -> bool:
        """Check ``text`` and wait for the verdict (the fallback voices, which check before sending).

        In shadow mode the line goes out at once and its verdict is only logged when it comes (no wait)."""
        fut = self.start(text)
        if self.shadow and not self.blocks(fut):
            self.observe(fut, text)
            return True
        return (await self.decide(fut, text)).ok

    def blocks(self, fut: Any) -> bool:
        """The line waits for its verdict in every mode, shadow too: Yuki's own internal words (``vocab``, decided at
        once) and a line under the claim gate (kMind: its check is running, or it found a claim)."""
        try:
            if fut is None:
                return False
            if fut in self._waits:
                return True
            return bool(fut.done() and fut.result().by in ("vocab", "claim"))
        except Exception:  # noqa: BLE001
            return False

    def observe(self, fut: "asyncio.Future[Verdict]", text: str) -> None:
        """Shadow mode: log this line's verdict when it arrives, in the background; the line is never held for it."""
        try:
            task = asyncio.get_running_loop().create_task(self.decide(fut, text))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def heard(self, text: str) -> None:
        """A line that is being said (for the repeat check)."""
        norm = _norm(text)
        if norm:
            self._said.append((time.monotonic(), text.strip(), norm))
            self._turn.add(norm)
            self._turn_lines.append(text.strip())

    def new_turn(self) -> None:
        """The user spoke: a line said before their words is not a repeat of what answers them."""
        self._turn.clear()
        self._turn_lines = []
        self._written.clear()

    # -- bookkeeping -------------------------------------------------------------------------------------

    def _log(self, **fields: Any) -> None:
        if self.log is not None:
            try:
                self.log.event("speech_guard", **({"session": self.session} if self.session else {}), **fields)
            except Exception:  # noqa: BLE001 - logging never breaks the voice
                pass

    def summary(self) -> dict[str, Any]:
        ms = sorted(self.stats["ms"])
        ask = sorted(self.stats.get("ask_ms") or [])
        return {k: v for k, v in self.stats.items() if k not in ("ms", "ask_ms")} | {
            "p50_ms": ms[len(ms) // 2] if ms else None, "max_ms": ms[-1] if ms else None} | (
            {"ask_p50_ms": ask[len(ask) // 2], "ask_max_ms": ask[-1]} if ask else {})

    def cancel(self) -> None:
        for task in list(self._tasks):
            task.cancel()


__all__ = ["SpeechGuard", "Verdict", "GUARD_PROMPT", "ASK_PROMPT", "parse_verdict", "FREE_WORDS", "REPEAT_S"]
