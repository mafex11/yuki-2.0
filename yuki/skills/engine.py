"""The skill engine: runs a skill's route ladder, checks the result, keeps the ledger (PLAN.md Phase S; D14, D17-D19).

``run(result_of_resolve, ctx)`` takes a :class:`yuki.skills.resolve.Plan` (or its Ask / Held / Handoff / Refused) and:

1. writes the run's ledger row at ``t0``, before the first step (D18: what changes on screen from then until it is
   done is Yuki's doing, not the user's habit);
2. tries each rung in order. **Right before every rung it applies the rung's game rule again with a fresh read**
   (D17(1)): one that takes focus or sends input, or whose data says ``never``, is skipped with a game in front; a
   ``states`` one runs only while the app's window is in a proven state (the skill driver's ``window_state`` hook);
   when no rung is left the run is held ("after the round?"), never handed to the thinker (D17(2)). The run's row
   names the app each rung acts on before it acts (D18: a mixer level set on Spotify is Spotify's moment);
3. runs the rung's verb (the closed list of :data:`yuki.skills.connection.VERBS`: UI Automation patterns by name
   inside containers, the media session, Core Audio, launch arguments and URIs through ``launch.open_url``'s guarded
   ``os.startfile``, a recorded path, a driver), then its done-check (:mod:`yuki.skills.donecheck`): **"done" only on
   True** (D19). A rung that could not run (its control missing, a cold window) passes to the next one; a contradicted
   check counts against the rung and passes on, except on a skill that sends (or a rung with ``retry_after_acting =
   false``), where it stops: a second rung could send twice. A check that couldn't read after the rung acted never
   counts against it and no second rung runs (it would do it twice): the run goes to rung C with the seed (why
   ``unverified``: the thinker looks at the screen and finishes it by steps), except on a skill that sends, a rung
   with ``retry_after_acting = false``, a relative change (a ``points`` slot: done again it changes twice), a rung
   that changed nothing and was only checking a state already there (``already``) or whose check says it took
   somewhere else (``other_device``), or a run from a card or an eval, where it stops with "couldn't confirm" said
   plainly (x5Fallback, failure audit 10 Oct C2: "couldn't confirm" ended every Arc request with nobody trying the
   normal way). The same skill with the same values asked again within :data:`REPEAT_HANDOFF_S` of such a hand-off
   runs no rung (it could do it twice): rung C at once. Onset probes (``mic_held``) compare with the state read right
   before the rung acted;
4. after a "done", D19's second probe runs a few seconds later (on a short-lived thread while it waits, at most 6 s;
   ``recheck="inline"`` in tests) and marks a false done in the ledger when it disagrees;
5. when every rung failed or could not run, rung C: the request goes to the thinker with the skill pinned
   (``handoff`` dict: goal, slots with their sources, the done-check, what was tried), unless a game is in front.
   On the new brain the same dict is the **plan seed** (bSkill, brain-design 2.1): the planner gets it in its request
   turn and its done-check is fixed (the check of the routes this run took). A rung whose step the consent rule handed
   over (``brain = "all"``: a step that would send or join with no plan behind it, ``TO_PLANNER``) goes to rung C at
   once, never to the next rung and never to "exactly this?". A rung that found no item by the name asked says so
   (``RouteResult.facts["missing"]``: the slot; ``facts_ram["seen"]``: the names it read there, RAM only): the seed
   carries both (``missing``, ``seen``), and the missing value is left out of its done-check, so the user's own word
   for a thing ("Japanese") is never required as its name on screen;
6. a slot value the talker or the thinker gave that the resolver dropped (Phase 2.1: only a person, or a slot of a
   lasting run, is ever dropped as not in the words as heard; an undoable slot's reading is taken as given and its done
   line names the guess) and then asked for is never asked back: rung C with the given values (why ``dropped``), where
   the thinker checks them against the words and memory. Sends still go through the consent rule there; "not that"
   to a guess runs the next of the user's own candidates, never the same guess again (:data:`LINE_WHICH` when none);
7. a run that broke partway goes to rung C too (why ``error``), unless it may have sent, a rung's data says nothing
   may follow it or it is a relative change; a refusal before anything ran because no skill has that key, skills
   are off or memory was too slow to know the user's usuals tells the caller to do it by steps. Every other non-done
   end is a game in front, a question only the user can answer, a step that is only the user's, a skill that sends
   (rule 8: never a second try), a rung whose data forbids a second try, a relative change that couldn't be
   confirmed, a result that took somewhere else or a state already there, or an eval or a card (they never start an
   agent run). Phase 2.3: every such end carries its evidence (:attr:`SkillResult.evidence`: what was tried, what the
   check saw, what is missing), so the talker words it in one short line; the code's line is only the fallback.

Logs (D12, content-free through ``yuki.log.events.content_free``): ``skill_resolve`` (Phase 2.1: ``source``, where the
given values came from, and ``guessed_slots``, as code words and schema names), ``route_try``, ``done_check``,
``done_recheck``, ``skill_run``, ``skill_handoff`` (the slots the resolver dropped, or a route found missing, by their
schema names: ``dropped_slots`` / ``missing_slots``, never their values). ``open_uri`` lives here (L14): an app URI
opened by ``yuki.actions.launch.open_url`` with no app, i.e. its guarded ``os.startfile``.
"""

from __future__ import annotations

import importlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from yuki.skills.connection import (Check, Route, Skill, fill, fill_value, sends_with, strictest, template_names,
                                    window_state_reader)
from yuki.skills.resolve import (FROM_CARD, FROM_CHOSEN, FROM_MENU, GUESS_SOURCES, HELD_LINE, Ask, Handoff, Held,
                                 Live, Plan, Refused, canonical, done_line, shown_value)

#: The time a request may take from the words to the done line (PLAN.md §5: p90 <= 5 s).
BUDGET_S = 5.0
#: The pinned skill block's size cap (``agent/frame.py`` renders it, never cut).
PINNED_CHARS = 900
LINE_UNVERIFIED = "I did it, but I couldn't confirm it worked."
LINE_FAILED = "That didn't work."
#: A sending skill's rung acted and its check read "not so": said plainly, and nothing else is tried (a second rung
#: could send it twice).
LINE_SEND_FAILED = "I tried to send it, but the PC shows it didn't go through, so I stopped there."
LINE_OTHER_DEVICE = "{app} is playing on another device, so I left it."
LINE_ALREADY = "That's already how it is."
#: Memory didn't answer the resolve in time (the voice's ``hands.MEMORY_SLOW_LINE``, the same words): nothing ran.
LINE_MEMORY_SLOW = "I couldn't reach my memory just now, try again?"
#: D24: a run that would itself do a step that is only ever the user's (a join on a calling site: they click Join).
LINE_COMMIT_USER = "That step is yours to do, so I left it."
#: A rung's outcome when the consent rule handed one of its steps over (bSkill; bTalk merge note 4): the step would
#: send or join with no plan behind it, so it did not run and the run goes to the planner (rung C, the plan seed).
TO_PLANNER = "to_planner"
#: x5Fallback (failure audit 10 Oct, C2): why a run went to rung C when a rung acted and its check couldn't read
#: (:data:`WHY_UNVERIFIED`), or when a value the caller gave was dropped by the resolver and then asked for
#: (:data:`WHY_DROPPED`).
WHY_UNVERIFIED = "unverified"
WHY_DROPPED = "dropped"
#: Why a run went to rung C when the run itself broke partway (an exception outside any rung's own guard): the thinker
#: looks first and finishes only what isn't done (never on a skill that sends, or one with a rung whose data says
#: nothing may follow it).
WHY_ERROR = "error"
#: skill20 (final-D B2): the wall-clock time a rung's done-check said "done", stamped on that rung's RAM facts
#: (``RouteResult.facts_ram``) right before its re-check, so a second source read before the verdict (a board read
#: taken while the browser came forward, before the new tab drew) is never counted as a contradiction.
DONE_AT = "done_at"
#: skill20 (final-D P1): the fill sources that are Yuki's guess, never the user's own word (``RouteCall.fill_sources``):
#: the taste fill, Yuki's own pick (``chosen``) and a given value the words don't hold (``resolve.GUESS_SOURCES``).
GUESS_FILLS: frozenset[str] = frozenset({*GUESS_SOURCES, "taste", FROM_CHOSEN})
#: skill20 (contract C10): a rung's code fact that it never read the state it needed (a cold window, a read that timed
#: out or wasn't built): ``RouteResult.facts[UNREAD] = True``. The run's evidence and voice facts list such rungs
#: (``unread``), so a caller may run the skill once more (nothing was pressed there).
UNREAD = "unread"
#: fix-r31 (A2, final-I R13, a message row): a rung's code fact that it put nothing anywhere: no typing
#: step ran and no key was pressed (the chat driver's: the chat it opened was never confirmed open, so nothing was
#: typed): ``RouteResult.facts[NOTHING_OUT] = True``. A run handed over unconfirmed on such a rung may run once more
#: (nothing in it can happen twice), so its pin says so (:data:`RERUN_KEY`) and the caller is told to bring the app's
#: window up and run it again, never to type it by steps (A8 and ONE's ``chat_type`` refuse that typing).
NOTHING_OUT = "nothing_out"
#: fix-r31 (A2): the pin's (and the plan seed's) code fact that this hand-off's skill may run once more with the same
#: values (``yuki.brain.plan.SEED_RERUN_KEY`` reads the same key): ``True``, else left out. Given once per skill and
#: values within :data:`REPEAT_HANDOFF_S` (:func:`_rerun_offer`): a second such hand-off is an ordinary one.
RERUN_KEY = "rerun"
#: fix-r31 (A2): the hand-off's first line when :data:`RERUN_KEY` is set (the planner and ONE read it; under 200
#: characters, the run_skill summary's cut).
LINE_RERUN = ("Nothing was typed or pressed (the PC couldn't confirm it open): bring the app's window up, then run this "
              "skill once more with the same values; never type it by steps.")
#: The resolver's refusals before anything ran whose report sends the caller to the normal way (by steps), each in
#: plain words: a key no skill has (the model's own slip), skills switched off, memory too slow to know the user's
#: usuals (the skill can't run blind, but steps need none of them; the thinker's origin only: the voice says its own
#: line). Every other refusal keeps its own line (a step that is only the user's, a card's commit).
REFUSED_BY_STEPS: dict[str, str] = {"unknown_skill": "no skill has that key", "skills_off": "skills are switched off",
                                    "memory_slow": "Yuki's memory didn't answer in time, so the skill wasn't run"}
#: A run of the same skill with the same values within this long of a hand-off whose rung acted unconfirmed (or broke
#: partway) goes to rung C at once, never through its rungs again: a second run could do it twice (open a second tab,
#: press play on a playing row), exactly as no second rung runs after an unconfirmed one. About one exchange (the
#: current exchange is the user's turns within about 120 s, fix-plan IF-words).
REPEAT_HANDOFF_S = 120.0
#: The check's own code word for a result that took but somewhere else (Spotify started it on another device): said
#: plainly as the driver words it, never handed to the thinker to "finish" (it would press play again: a pause).
SETTLED_REASONS: tuple[str, ...] = ("other_device", "row_stale")
#: skill19 (live 19 Oct RC1): ``row_stale`` is Spotify's check after a press on a window that isn't drawing: the
#: session plays now (it did not before the press) with sound here, and only the row's label never redrew. Said plainly
#: as "couldn't confirm it's that one", never handed to the thinker (it would raise the window to read the row) and
#: never followed by a second rung (a second press would pause it). Review M3: also after a switch while one played,
#: when the session plays another track than before the press.
#: Review M3 (skill19): a rung that did not run by its own reads, but whose reads couldn't tell (its act may have been
#: taken: a press on an app whose state showed no change it could read), says ``facts["not_run"] =`` this word: the
#: next rung still tries, and its try is "couldn't confirm" (``unverified``), never a "couldn't" counted against it
#: (``registry.by_record``: a rung's place by its own record).
NOT_RUN_UNSURE = "unsure"
#: The origins whose unconfirmed or dropped runs go to rung C: the talker and the thinker. A card never starts an agent
#: run (``yuki.ui.nudges``: a hand-off there is dropped) and an eval measures the skill itself.
HANDOFF_ORIGINS: tuple[str, ...] = ("voice", "thinker")
#: The seed's slot sources for a dropped value (rung C, :data:`WHY_DROPPED`): ``unheard`` the slot the resolver
#: dropped and asked for (not in the words as heard), ``given`` another value the caller gave that is not a span of
#: the words either; a given value that is one is ``words``, as the resolver says it.
FROM_UNHEARD = "unheard"
FROM_GIVEN = "given"
#: kTaste (contract C2; audit 17 Oct decision 2): the slots a decider adds when the user asks Yuki to choose ("a video I
#: like", "something chill", "put something on"): ``choose`` (true) and ``context`` (their words or the understanding's,
#: free text, for the judgement only). Never a skill's own slots: taken out before the resolver.
CHOICE_SLOTS: tuple[str, ...] = ("choose", "context")
#: Phase 2.1/2.2: the run's own notes a caller may add (at the top of ``run_skill``'s arguments, or inside its slots
#: when the skill has no slot of that name; :func:`note_args`): ``why``, a pick's code-made why that the caller passes
#: back from the candidates it chose from (said in the done line, ``resolve.WHY_LINE``), and ``from``, the caller's
#: word for where a value it gave came from (``resolve.CALLER_SOURCES``: card / screen / menu / reading; gd D1, contract
#: K-H: ``heard``, read from a misheard word of theirs, ``resolve.CALLER_HEARD``; one word, or a slot -> word map).
#: Never a skill's own slots: taken out before the resolver.
NOTE_SLOTS: tuple[str, ...] = ("why", "from")
#: Phase 2.1: "not that" right after a guess (the caller's correction giving that same guessed value again) never runs
#: it again: the next of the user's own candidates runs, else this question (rule 149: one line, one other way).
LINE_WHICH = "Not that one, then. Which did you mean?"
#: How long Yuki's guesses are kept for "not that" (RAM only; about one exchange, as :data:`REPEAT_HANDOFF_S`).
GUESS_KEEP_S = 120.0
#: Phase 2.3: at most this many names read on screen ride on a result's evidence for the talker.
EVIDENCE_SEEN = 8
#: fd (final-F D3): a rung's own word for the item it acted on or found already so, by slot (``facts_ram[ITEM_KEY]``:
#: ``{"playlist": "ネオン"}``, RAM only): the run's trace, so a correction never plays it again.
ITEM_KEY = "item"
#: fd (D3, D4): the runs of the last this long are kept (RAM, this process): what each asked for and acted on, how it
#: ended, the request it ran in, the slot it asked about.
TRACE_KEEP_S = 120.0
TRACE_RUNS = 8
#: fd (final-F D2, one-brain M1): an unconfirmed result that acted with sound up gets its skill's second source read
#: once, this long after its own check gave up, before it goes back (and before any agent round).
UNVERIFIED_RECHECK_S = 1.0
#: How long the pick may take (the feeds within ``youtube_feed.REQUEST_BUDGET_S``, then one small model call bounded by
#: ``taste.CHOOSE_WAIT_S``); a late pick leaves the request to the normal way.
CHOOSE_TIMEOUT_S = 4.5
#: The outcome of a pick held because a call is live (decision 2: never during a call, said so instead).
HELD_CALL = "held_call"
#: fHands (live 18 Oct, k_t3_draft): a rung's step stopped by something other than a game (the gate could not check
#: and failed closed, or another guard refused it: ``yuki.actions.gate.BLOCKED``). A ``RouteResult.held`` of this word
#: and the run's outcome of the same name: a settled stop said with :data:`LINE_BLOCKED` (or
#: :data:`LINE_BLOCKED_PARTWAY` once a rung acted), never the game line, never the next rung, never handed over (the
#: thinker would be stopped the same way).
HELD_BLOCKED = "blocked"
#: morning21 (A2): the run's outcome after a rung's code-word stop (``RouteResult.stop``: a precondition met, said
#: plainly): not done, nothing done, never a question to the user ("Ask them: Zen isn't open" on 21 Oct made the
#: planner drop the meeting part). The report names the code word and the app it needed; the ledger keeps a refusal
#: (never counted against the rung).
NOT_DONE = "not_done"
LINE_BLOCKED = "Something on the PC stopped me there, so nothing was done."
LINE_BLOCKED_PARTWAY = "Something on the PC stopped me partway, so I left it there."
#: At most this many names a route read on screen ride on a seed (``seen``, RAM only), each at most this long.
SEEN_MAX = 40
SEEN_NAME_CHARS = 80
#: A slot's schema name as the connection files write it (what a content-free log line may carry, never a value).
_SLOT_NAME = re.compile(r"[a-z_][a-z0-9_]{0,40}")


# ---------------------------------------------------------------------------------------------------------------------
# D24: commit is engine code, keyed on the end kind (phase L design section 4.3; critique H4)
# ---------------------------------------------------------------------------------------------------------------------

#: Who may reach each end kind (the moments' D1 vocabulary): ``user`` never Yuki's step (the skill stops before it);
#: ``words`` only on the user's own plain words, through the consent check (rule 8); ``none`` Yuki may reach it. An end
#: kind not in the table is ``user`` (:func:`commit_for`). A capability's own ``commit`` may only make it stricter.
COMMIT_BY_END: dict[str, str] = {
    "call_join": "user", "game_voice": "user", "message_sent": "words", "volume_change": "none",
    "media_context": "none", "page": "none", "site_front": "none", "media_play": "none",
    # morning21 (mb note 1): a monitor's brightness or power, read back; its own off question (only screen, a game or
    # a call shown) is the display driver's, so the run itself commits nothing
    "display_change": "none",
}
#: The end kinds whose commit depends on what serves them (PLAN D24, lSplit review r1 L2): a join of a chat app's voice
#: channel (the serve needs ``chat_voice``) is the user's own "join X" (rule 8, Q3), so ``words``, where a calling
#: site's join stays ``user``. When a serve's needs map to different words, the strictest holds (lCore review r2 L7:
#: a calling site's join beside a chat's voice need stays ``user``).
COMMIT_BY_NEED: dict[tuple[str, str], str] = {("call_join", "chat_voice"): "words", ("call_join", "calling_site"): "user"}


def commit_for(end: str, needs: tuple[str, ...] | list[str] = (), declared: str = "") -> str:
    """D24: the commit word of reaching ``end`` (:data:`COMMIT_BY_END`, or the strictest of :data:`COMMIT_BY_NEED`
    for the serve's needs when any is listed); an unknown end kind is ``user``; a capability's ``declared`` commit only
    ever makes it stricter."""
    end = str(end or "")
    base = COMMIT_BY_END.get(end, "user")
    by_need = [COMMIT_BY_NEED[(end, str(n))] for n in needs or () if (end, str(n)) in COMMIT_BY_NEED]
    if by_need:
        base = strictest(*by_need) or base
    return strictest(base, str(declared or "")) or "user"


def run_commit(skill: Skill) -> str:
    """The commit of the end a run of ``skill`` reaches. A capability that serves a commit end and stops at its
    ``pre_commit_end`` (``browser.site_tab``: the tab in front, the user clicks Join) reaches only that end: its own
    ``commit`` governs the commit end it serves, never the run (PLAN D24, lSplit review r2 L4), but only when that
    end is itself no commit (lCore review r2 L7: a ``pre_commit_end`` that is a commit kind keeps the declared commit).
    Anything else reaches its own end, whose commit its serve's needs and its own ``commit`` (stricter only) decide."""
    needs = tuple(n for serve in skill.serves if serve.end == skill.end for n in serve.needs)
    if skill.pre_commit_end and skill.pre_commit_end == skill.end and commit_for(skill.end, needs) == "none":
        return "none"
    return commit_for(skill.end, needs, skill.commit)


def served_commit(skill: Skill, end: str) -> str:
    """The commit of an end ``skill`` serves (the learner's "is the habit's own end a commit", design section 4.3)."""
    needs = tuple(n for serve in skill.serves if serve.end == end for n in serve.needs)
    return commit_for(end, needs, skill.commit)


def ledger_skill(skill: Skill, values: Mapping[str, Any]) -> Skill:
    """The catalog entry a run is counted on (D14; lSplit review r2 L1): a learned skill's run whose words named
    another value for one of its bound key slots is its capability's run, never the learned skill's (its trust and
    offers count only its own habit). Compared as one identity (:func:`yuki.skills.connection.scope_value`)."""
    from yuki.skills.menu import names_other_key

    if getattr(skill, "origin", "") != "learned" or not names_other_key(skill, values):
        return skill
    # one rule with ``menu.request_key`` (lCore review L11): the capability's run
    try:
        from yuki.skills.catalog import load

        cap = load().get(skill.capability)
    except Exception:  # noqa: BLE001
        cap = None
    return cap if cap is not None else skill


# ---------------------------------------------------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class RouteResult:
    """What one rung's verb did. ``ran``: True it acted (a pattern called, a command sent, a level set), False it did
    not (nothing found, refused, not built), None it may have (a timeout after sending). ``verified``: its own
    read-back. ``already``: the end state was there before (a true no-op). ``outcome``: a code word. ``facts`` are
    logged (content-free); ``facts_ram`` never leave RAM (a session's title before a command). A rung that found no
    item by the name asked names that slot in ``facts["missing"]`` and the names it read there in
    ``facts_ram["seen"]``: they ride on the hand-off's seed (:func:`pinned`)."""

    ran: bool | None = None
    verified: bool | None = None
    outcome: str = ""
    held: str | None = None
    ask: str | None = None
    options: list[str] = field(default_factory=list)
    say: str | None = None
    unverified_line: str | None = None
    already: bool = False
    facts: dict[str, Any] = field(default_factory=dict)
    facts_ram: dict[str, Any] = field(default_factory=dict)
    value: float | None = None
    entity_digest: str | None = None
    #: The app the verb acted on (a driver says it: Spotify, Discord, the mixer's ``master``), when the route's own
    #: arguments don't name it; D18 matches a moment's app with it (``skill_runs.app``).
    app: str | None = None
    #: morning21 (A2): a code-word stop said plainly, never a question: nothing was done and nothing more is tried,
    #: for a precondition the rung met (``outcome``: its code word; ``say``: its plain line; ``app``: the app it
    #: needed, when one). The run ends :data:`NOT_DONE` (never "Ask them:"); ``ask`` stays for real questions (options
    #: or a slot).
    stop: bool = False


@dataclass
class RouteCall:
    """What a verb gets: the skill, the rung, its arguments with the slots filled, all the values, the run's context
    and its time left (monotonic deadline). skill20 (final-D P1): ``fill_sources``, each slot's fill source as the
    resolver gave it (``Plan.filled``: ``words``, ``again``, ``told``, ``live``, ``pref``, ``taste``, ``default``, or a
    given value's ``chosen`` / ``menu`` / ``card`` / ``screen`` / ``reading`` / ``granted`` / ``worded``), so a driver
    that learns from a run (Spotify's pairing) can tell the user's own value from Yuki's guess (:data:`GUESS_FILLS`)."""

    skill: Skill
    route: Route
    args: dict[str, Any]
    values: dict[str, Any]
    ctx: "RunContext"
    deadline: float
    fill_sources: dict[str, str] = field(default_factory=dict)


@dataclass
class RunContext:
    """Where a run comes from and what it may use. ``memory``: a ``MemoryAccess`` (``.call``) or a ``MemoryClient``;
    ``hands``: the voice hands (rung B's step runner); ``game``: D9's "which game is in front" (default the local read,
    ``yuki.memory.gamegate.game_in_front``); ``verbs`` / ``probes``: replacements by name (tests, replays);
    ``baselines``: replacements of the onset probes' before-reads (:data:`yuki.skills.donecheck.BASELINES`);
    ``window_state``: the app's window state now for the ``game = "states"`` rule (default the skill's driver hook,
    :func:`window_state_reader`); ``on_focus``: called right before a rung that takes focus (the acting marker;
    sound-only rungs publish none); ``hand_id``: the voice hand that called the run (``voice-<turn>-h<n>``), written
    on each engine line as ``hand_id`` beside ``request_id`` (the turn, D19), so following one hand never picks up a
    second skill hand's lines in the same turn (sWire r1 L2). ``grant``: the brain's consent grant for this very
    ``run_skill`` step, only as :func:`yuki.skills.uia_route.send_grant` validated it (:func:`run_for_thinker`): a send
    the consent rule allowed and counted down, held by the request's desk hands now. The resolver keeps its exact
    text and person, and the chat driver may send exactly that text to exactly that person (failure fix-plan Item 11's
    L2). ``None`` on the voice, the old loop, a card or an eval: their runs check the user's words as before."""

    origin: str = "voice"
    words: list[str] = field(default_factory=list)
    memory: Any = None
    hands: Any = None
    log: Callable[..., Any] | None = None
    game: Callable[[], str] | None = None
    verbs: Mapping[str, Callable[[RouteCall], RouteResult]] = field(default_factory=dict)
    probes: Mapping[str, Callable[..., Any]] = field(default_factory=dict)
    request_id: str | None = None
    session_id: str | None = None
    budget_s: float = BUDGET_S
    recheck: str = "thread"
    on_focus: Callable[[], Any] | None = None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    baselines: Mapping[str, Callable[[Mapping[str, Any]], Any]] = field(default_factory=dict)
    window_state: Callable[[str], str | None] | None = None
    hand_id: str | None = None
    grant: Mapping[str, Any] | None = None
    #: P1.4 (18 Oct free-thinker plan): where the run's app window goes as soon as it is up, before anything is typed
    #: (``(monitor, how)``: :func:`place_hint`), so an app opens on the screen the user isn't watching; ``None``: where
    #: the app puts itself.
    place: tuple[str, str] | None = None
    #: Phase 2.2: a pick's code-made why the caller passed back (:data:`NOTE_SLOTS`), said in the done line.
    why: str = ""
    #: Phase 2.1: the caller's word for where the values it gave came from (:data:`NOTE_SLOTS` ``from``), for the
    #: resolver's ``given_from``.
    given_from: Any = None
    #: skill19 (contract C1): the desk's front read before and after each rung (:func:`front_now`: the foreground
    #: window and every window's visible share), replaced by name in tests and replays; ``None``: the live read (an eval
    #: run reads nothing: its focus is the route's declaration).
    front: Callable[[], Any] | None = None
    #: skill22 (A19): whether the caller calls a run again once when a slot came in another form than it takes (the
    #: report says which form; :func:`_first_form_slip`): ``True`` / ``False``, or ``None`` by origin (the thinker reads
    #: its tool result and calls again: on; the voice hands until they give such a result back as a call's own shape:
    #: off, the question as before).
    form_recall: bool | None = None
    #: fd (final-F D3): on a correction, the items it leaves out, by slot (``{"playlist": ["ネオン"]}``): what the
    #: corrected run asked for and acted on, from the engine's own trace (:func:`_corrected`), verified or not. A driver
    #: never measures or judges the words to mean one of them. RAM only; set by :func:`resolve_and_run`.
    turned_down: dict[str, list[str]] = field(default_factory=dict)
    #: gd D2 (final-G2 k_spotify_louder): the caller's ``corrects`` flag for this run (the user corrected the skill's
    #: last run), so a driver that learns from a run never keeps a correction's words as the name of the row it played
    #: (those words name what the user just rejected). Set by :func:`resolve_and_run`. RAM only.
    corrects: bool = False
    #: lb B1 (contract K-F): the chat that was in front when the user spoke, as the resolver took its person
    #: (:func:`yuki.skills.chat.front_chat`: ``{"id", "name", "thread", "app", "chat", "handle", "hwnd"}``), so the chat
    #: driver keeps the run in that very chat (its thread) and names it. Set by :func:`resolve_and_run` only when the
    #: person came from it (``resolve.FROM_FRONT``); ``None`` otherwise. RAM only, never logged.
    front_chat: dict[str, Any] | None = None

    def game_now(self) -> str:
        """D9, read now (fails closed: a check that raised counts as a game)."""
        try:
            if self.game is not None:
                return str(self.game() or "")
            from yuki.memory.gamegate import game_in_front

            return str(game_in_front() or "")
        except Exception:  # noqa: BLE001
            return "unknown"

    def window_state_now(self, skill: Skill) -> str | None:
        """The skill's app window state now (one of ``WINDOW_STATES``), or ``None`` (no reader, or it couldn't
        read: a ``game = "states"`` rung then stays off with a game in front)."""
        from yuki.skills.resolve import _window_state

        return _window_state(skill, Live(window_state=self.window_state))


@dataclass
class SkillResult:
    """One run's outcome, for the voice, the thinker and the ledger."""

    skill: str
    outcome: str
    verified: bool | None = None
    done_line: str = ""
    say: str = ""
    route: str | None = None
    tried: list[dict[str, Any]] = field(default_factory=list)
    ask: str | None = None
    options: list[str] = field(default_factory=list)
    held: str | None = None
    handoff: dict[str, Any] | None = None
    ask_keep: dict[str, Any] | None = None
    run_key: str | None = None
    ms: float = 0.0
    recheck_thread: threading.Thread | None = None
    recheck_verdict: bool | None = None
    values: dict[str, Any] = field(default_factory=dict)
    #: skill19 (contract C1): measured per rung (:func:`front_change`): a rung brought another window to the front (the
    #: foreground before it is not the one after it). Only a rung whose front couldn't be read (or an eval run) falls
    #: back to its route's declaration (``needs_focus``). Set on every return path of a run that reached its rungs.
    focus_taken: bool = False
    #: Integration review F1: a rung that ran declared it takes the front (``needs_focus``), whatever the measure saw:
    #: a page opened as a new tab in the browser window already in front changes no foreground, yet the voice's tab
    #: record (fHands' "close it") is placed on it. Only that consumer reads it; the desk scope reads the measure.
    focus_declared: bool = False
    #: skill19 (contract C1): something on the PC was touched by this run: a rung acted by its own word (``ran``), or
    #: the measured front changed under a rung (a window came to the front, or the window in front lost most of its
    #: visible share), even when the rung itself said it couldn't (Spotify's link rung raised Spotify, then "page not
    #: shown"). Every return path.
    acted: bool = False
    #: skill19 (C1): the apps the rungs acted on (:func:`acted_app` per rung that acted or changed the front, and the
    #: app of a window a rung brought to the front), app keys in order, each once; and those windows' handles.
    acted_apps: list[str] = field(default_factory=list)
    acted_windows: list[int] = field(default_factory=list)
    #: Running it sends something to other people (its ``sends`` data with these values): an unconfirmed send is
    #: never tried again (rule 8).
    sends: bool = False
    #: skill20 (final-D messages §6): on a sending run that ended unconfirmed, whether the rung's send key went out by
    #: its own code fact (``facts["pressed"]``: the chat driver's dispatch): ``False`` it never did (nothing can have
    #: been sent), ``True`` it did, ``None`` unknown. "It may have sent already" is said only when it is not ``False``.
    sent_key: bool | None = None
    #: skill20 (contract C10): each slot's fill source as the resolver gave it (``Plan.filled``: ``words``, ``live``,
    #: ``chosen``, ``reading`` ...), and the rungs that never read what they needed (``RouteResult.facts[UNREAD]``: a
    #: cold window, a read that timed out). Code words only; set on every return path of a resolved run.
    fill_sources: dict[str, str] = field(default_factory=dict)
    unread: list[str] = field(default_factory=list)
    #: Why a run was refused before anything ran (the resolver's code word: ``unknown_skill``, ``skills_off``, ...),
    #: or ``repeat`` on a hand-off that ran no rung because the same thing ran unconfirmed moments ago; on a
    #: :data:`NOT_DONE` end, the rung's own code word (``not_opened``, ``no_tab``, ...).
    why: str = ""
    #: morning21 (A2): on a :data:`NOT_DONE` end, the app key the stopped rung needed (``RouteResult.app``), when one.
    needs_app: str = ""
    #: Phase 2.1: the values Yuki guessed (the caller's reading, a listed name, one of their known things), each
    #: ``{"slot", "value" (as said), "from" (resolve.GUESS_SOURCES)}``: the done line names them; RAM only.
    guessed: list[dict[str, Any]] = field(default_factory=list)
    #: Phase 2.3: what a run that didn't end in a checked done tried, saw and is missing (:func:`_evidence`), so the
    #: talker words that outcome itself in one short line; ``say`` stays the code's fallback line. RAM only (the
    #: values and the names read on screen are the user's).
    evidence: dict[str, Any] = field(default_factory=dict)
    #: fd (final-F D3): the items the rungs acted on or found already so, by slot (their own word,
    #: ``RouteResult.facts_ram[ITEM_KEY]``). RAM only.
    items: dict[str, str] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        """Whether "done" may be said: only on a seen result (or a true no-op the check saw already there)."""
        return self.outcome in ("done", "already") and self.verified is True

    def report(self) -> str:
        """For the model that called it (the talker or the thinker): what happened, in plain words, and on any other
        end than a checked done a last line with the evidence (:func:`evidence_words`), the first line's shape kept
        (``brain.done.engine_line`` reads it)."""
        text = self._report()
        if not self.done and self.evidence and self.outcome != "asked":    # a question needs only its words
            words = evidence_words(self.evidence)
            if words:
                text = f"{text}\n{words}"
            recall = _recall_line(self.evidence) if self.evidence.get(FORM_RECALL) else ""
            if recall:
                # skill22 (A19, fix L1): nothing was done, and the value it went without came in another form than it
                # takes: the re-call is the FIRST line (the planner reads only the first line, cut at 200 characters);
                # the code's own line stays behind its "Say: " mark there (``brain.done.engine_line``), the fallback
                # said when the request ends without the call
                say = " ".join(str(self.say or "").split())
                text = f"{recall} Else Say: {say}\n{text}" if say else f"{recall}\n{text}"
        return text

    def _report(self) -> str:
        """:meth:`report`'s first lines. A hand-off always says the rest is done by steps and the skill is not run
        again (x5Fallback: the old "follow it" sent the thinker back into the skill that had just failed)."""
        if self.outcome == "already" and self.verified is True:
            return f"Nothing to change: the PC shows it was already so. Say: {self.done_line or self.say}"
        if self.done:
            return f"Done and checked: {self.done_line or self.say}"
        if self.outcome == "asked":
            # skill19 (RC5a / C5): a value given for a code-typed slot that isn't a form it takes is never dropped
            # silently: the caller reads which slot and the form it takes (the question stays the user's)
            forms = (self.evidence or {}).get("form") if isinstance(self.evidence, Mapping) else None
            if isinstance(forms, Mapping) and forms:
                if (self.evidence or {}).get(FORM_RECALL):
                    # skill22 (A19): the first such slip of a request is the caller's to mend, never a question
                    return _recall_line(self.evidence)
                given = _forms_given(forms)
                return f"Ask them: {self.ask}\n{given}: pass it in that form when their words give it."
            return f"Ask them: {self.ask}"
        if self.outcome == NOT_DONE:
            # morning21 (A2): a precondition the rung met, said plainly; the code word and the app it needed (a code
            # fact the caller may repair: open that app, then run it again), never "Ask them:"
            needs = f"; needs {self.needs_app}" if self.needs_app else ""
            done = "it acted partway (see the line)" if self.acted else "nothing was done"
            return f"Not done ({self.why or 'stopped'}{needs}): {done}. Say: {self.say}"
        if self.outcome == "held_game":
            return f"Held: a game is in front, nothing was sent. Say: {self.say}"
        if self.outcome == HELD_CALL:
            return f"Held: a call is live, so nothing was put on. Say: {self.say}"
        if self.outcome == HELD_BLOCKED:
            return (f"Stopped: something on the PC (not a game) stopped a step, so nothing more was tried; don't do it "
                    f"by steps (the same stop applies). Say: {self.say}")
        if self.outcome == "handed_over":
            why = (self.handoff or {}).get("why")
            if why == TO_PLANNER:
                return ("A step of it would send or join something, so that step was not done; the request goes to "
                        "the planner with the skill pinned.")
            if why == WHY_UNVERIFIED and (self.handoff or {}).get(RERUN_KEY) is True and self.why != "repeat":
                # fix-r31 (A2): the rung's own code fact says nothing went out, so a second run can't do it twice; the
                # old "finish it by steps (type)... don't run this skill again" met A8's refusal of that typing
                return LINE_RERUN
            if why in (WHY_UNVERIFIED, WHY_ERROR):
                first = ("It acted, but the PC couldn't confirm the result" if why == WHY_UNVERIFIED
                         else "It broke partway, so what it did is unknown")
                if self.why == "repeat":
                    # x5Fallback: the same thing ran moments ago unconfirmed; a second run could do it twice
                    first = ("It already ran moments ago with these values and the PC couldn't confirm the result, "
                             "so it was not run again (it could do it twice)")
                return (f"{first}: look at the screen and finish it by steps (look, click, type), redoing nothing "
                        f"that already worked; don't run this skill again.")
            if why == WHY_DROPPED:
                unheard = [k for k, s in ((self.handoff or {}).get("slots") or {}).items()
                           if isinstance(s, dict) and s.get("from") == FROM_UNHEARD]
                return (f"It didn't run: {', '.join(unheard) or 'a value'} as given isn't in the user's words as "
                        f"heard (misheard, or reworded). Do it by steps (look, click, type) with what the user "
                        f"meant, checked against the user's words and memory; don't run this skill again.")
            return ("Its own routes didn't get there: do it by steps (look, click, type) with the skill pinned; don't "
                    "run this skill again.")
        if self.outcome == "unverified":
            # review r2 M1: the first line keeps main's shape, the driver's own line right after "Say so plainly: "
            # (``brain.done.engine_line`` reads it off the run_skill summary: the first line, cut at 200 characters;
            # 64 + the longest driver line, 117, fits), so the brain speaks "playing on another device..." or "already
            # playing ... so I left it" and never a generic "I did it" for a press that never happened; the guidance
            # rides on the second line, which only the model reads
            head = ("It ran, but the PC couldn't confirm it." if self.sends
                    else "It ran, but the PC couldn't confirm the result.")
            say = " ".join(str(self.say or "").split())
            # rule 8: a send may have gone already, so a second try could send it twice. skill20 (final-D, one, the
            # misheard-name draft row: Enter was refused, the driver's own fact said no send key went out, and "it may
            # have sent" was still said): only when the send key went out, or that is unknown
            if self.sends and self.sent_key is False:
                rest = "The send key never went out, so it wasn't sent; don't run this skill again."
            elif self.sends:
                rest = "It may have sent already: never send it again."
            else:
                rest = "Don't run this skill again: doing it again could do it twice."
            return f"{head} Say so plainly: {say}\n{rest}" if say else f"{head}\n{rest}"
        if self.outcome == "other_device":
            return f"Not changed: {self.say}"
        if self.outcome == "refused" and self.why in REFUSED_BY_STEPS:
            # x5Fallback: no skill ran and nothing moved, so the normal way is open (never a stop at "that didn't work")
            return ("Not run: " + REFUSED_BY_STEPS[self.why] + ", and nothing was done. Do it by steps (look, click, "
                    "type).")
        return f"Not done ({self.outcome}). {self.say}".strip()

    def as_voice(self) -> tuple[bool, str, dict[str, Any]]:
        """``(ok, report, facts)`` for the voice hands (``Hands._skill``'s replacement, sVoice): ``facts`` carries
        ``verified`` (the done-check), ``done_line`` (said by code only when verified), ``say``, ``ask``,
        ``options``, ``held``, ``ask_keep``, ``handoff_skill`` (the pinned dict for ``bridge.escalate``), ``skill``,
        ``outcome``, ``settled`` (nothing more may be tried: held, other device, unverified after acting)."""
        facts = {"verified": self.done, "done_line": self.done_line if self.done else "", "say": self.say,
                 "ask": self.ask, "options": list(self.options), "held": self.held, "ask_keep": self.ask_keep,
                 "handoff_skill": self.handoff, "skill": self.skill, "outcome": self.outcome, "route": self.route,
                 "run_key": self.run_key,
                 "settled": self.outcome in ("held_game", HELD_CALL, HELD_BLOCKED, "other_device", "unverified",
                                             "asked", "already", NOT_DONE)}
        if self.outcome == NOT_DONE:
            facts["stop_why"] = self.why          # morning21 (A2): the rung's code word (content-free)
            if self.needs_app:
                facts["needs_app"] = self.needs_app
        # Phase 2.1 / 2.3 (RAM only): the guesses the done line names, and on any other end the evidence the talker
        # words it from (``say`` is the code's fallback line)
        if self.guessed:
            facts["guessed"] = [dict(g) for g in self.guessed]
        if self.evidence and not self.done:
            facts["evidence"] = dict(self.evidence)
        # skill19 (C1): what the run measurably touched (the hands' look after it reads the result itself too)
        facts["acted_apps"] = list(self.acted_apps)
        facts["acted_windows"] = list(self.acted_windows)
        # skill20 (C10): code words only; ``unread`` only when a rung never read (a caller may run it once more)
        facts["fill_sources"] = dict(self.fill_sources)
        if self.unread:
            facts["unread"] = list(self.unread)
        if self.sends and self.sent_key is not None:
            facts["sent_key"] = bool(self.sent_key)
        if isinstance(self.evidence, Mapping) and self.evidence.get(FORM_RECALL):
            # skill22 (A19): refused for the call's own shape (a slot in another form than it takes), nothing done: the
            # voice hands give such a call back once to be made again (their ``shape`` retry), never a question
            facts["shape"] = True
        if self.outcome == HELD_BLOCKED:
            facts["refused"] = HELD_BLOCKED      # fHands: never said as a game (``held`` stays empty)
        elif self.held:
            facts["refused"] = "game"
        return self.done, self.report(), facts


# ---------------------------------------------------------------------------------------------------------------------
# Plumbing: memory, logs
# ---------------------------------------------------------------------------------------------------------------------


def _mem(memory: Any, what: str, fn: Callable[[Any], Any], timeout_s: float = 1.0) -> Any:
    """``fn(client)`` through a ``MemoryAccess`` (bounded wait) or directly on a ``MemoryClient`` / store holder.
    ``None`` when there is no memory or it failed (the run goes on)."""
    if memory is None:
        return None
    try:
        call = getattr(memory, "call", None)
        if callable(call) and not hasattr(memory, "store"):
            return call(what, fn, timeout_s=timeout_s)
        return fn(memory)
    except Exception:  # noqa: BLE001
        return None


def _store_of(client: Any) -> Any:
    return getattr(client, "store", client)


def _emit(ctx: RunContext, event: str, **fields: Any) -> None:
    if ctx.log is None:
        return
    try:
        from yuki.log.events import content_free

        fields = {k: v for k, v in fields.items() if v is not None}
        # the log vocabulary's code words are lower case (yuki.log.events): a rung letter and a route key too
        for key in ("route", "rung"):
            if isinstance(fields.get(key), str):
                fields[key] = fields[key].lower()
        if "origin" in fields:
            fields["lane"] = fields.pop("origin")           # an id field: voice / thinker / card / eval
        rec = content_free({"type": event, **fields})
        rec.pop("type", None)
        if ctx.request_id and "request_id" not in rec:
            rec["request_id"] = ctx.request_id
        if ctx.hand_id and "hand_id" not in rec:
            rec["hand_id"] = ctx.hand_id
        ctx.log(event, **rec)
    except Exception:  # noqa: BLE001 - a log never breaks a run
        pass


def _numbers(values: Mapping[str, Any]) -> dict[str, float]:
    """The numeric slot values (content-free; the ledger keeps them for "again")."""
    return {k: float(v) for k, v in values.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


#: The app key the volume watcher writes for the whole PC's level (``yuki.memory.volumewatch.MASTER``).
MASTER_APP = "master"


def acted_app(skill: Skill, route: Route, args: Mapping[str, Any] | None, res: "RouteResult | None" = None) -> str:
    """D18: the app a rung acts on, for ``skill_runs.app`` (a moment of that app and kind inside the run's window is
    Yuki's): what the verb itself said (a driver's ``RouteResult.app``), the whole PC's level for
    ``endpoint_volume``, the route's own ``app`` / ``process`` / ``window`` argument (filled: a mixer level set on
    the app the user named), a recorded path's first app, else the connection's app. The registry keeps it as an
    app key."""
    if res is not None and res.app:
        return str(res.app)
    if route.via == "endpoint_volume":
        return MASTER_APP
    a = args if isinstance(args, Mapping) else {}
    for k in ("app", "process", "window"):
        v = a.get(k)
        if isinstance(v, str) and v.strip():
            return v
    apps = a.get("apps")
    if isinstance(apps, (list, tuple)) and apps and isinstance(apps[0], str) and apps[0].strip():
        return apps[0]
    return skill.app


# ---------------------------------------------------------------------------------------------------------------------
# skill19 (contract C1; live 19 Oct all-desk RC1(b), one-desk D5): the front, measured before and after each rung
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class FrontRead:
    """The desk's front at one instant (read-only): the foreground window, every window's visible share (the
    occlusion grid's, 0..1) and each window's process id. Content-free (no titles)."""

    fg: int = 0
    shares: dict[int, float] = field(default_factory=dict)
    pids: dict[int, int] = field(default_factory=dict)


def front_now() -> FrontRead | None:
    """The front now (:func:`yuki.perception.visibility.layout`: one EnumWindows pass and the 16 px grid, about 3-4 ms;
    no focus, no input). ``None`` when the desk couldn't be read. Never raises."""
    try:
        from yuki.perception.visibility import layout

        lay = layout()
        if not lay.surfaces:
            return None
        return FrontRead(fg=int(lay.foreground or 0),
                         shares={int(s.hwnd): float(s.visible_share) for s in lay.surfaces},
                         pids={int(s.hwnd): int(s.pid) for s in lay.surfaces})
    except Exception:  # noqa: BLE001 - unread: the route's declaration stands
        return None


def front_change(before: Any, after: Any, own: Callable[[int, int], bool] | None = None) -> dict[str, Any]:
    """What a rung did to the front, from two :class:`FrontRead` (code facts): ``read`` (both read), ``taken`` (the
    foreground after is another window than before), ``covered`` (the window in front before lost most of its visible
    share: perception.diff's own cover rule, ``COVER_KEEP`` / ``COVER_MIN_LOSS``; a window that left the screen counts
    as 0), ``hwnd`` / ``pid`` (the window in front after, when it is another).

    ``own(hwnd, pid)``: whether a window that came up is the rung's (it acts in that window's app); a window that was
    not on screen before the rung is always its. A change no window of the rung's made (the user switched windows
    while the rung waited) is ``other`` and neither ``taken`` nor ``covered``. Without ``own`` every change counts.
    Pure but for ``own``."""
    if not isinstance(before, FrontRead) or not isinstance(after, FrontRead):
        return {"read": False}
    try:
        from yuki.perception.diff import COVER_KEEP, COVER_MIN_LOSS
    except Exception:  # noqa: BLE001 - the same rule's numbers
        COVER_KEEP, COVER_MIN_LOSS = 0.50, 0.20

    def mine(hwnd: int) -> bool:
        if own is None or hwnd not in before.shares:
            return True
        try:
            return bool(own(int(hwnd), int(after.pids.get(hwnd) or 0)))
        except Exception:  # noqa: BLE001 - unknown: counted (a rung's change is never missed by an error)
            return True

    taken = bool(after.fg) and after.fg != before.fg
    covered = False
    s0 = before.shares.get(before.fg) if before.fg else None
    if s0 is not None and s0 > 0.0:
        s1 = float(after.shares.get(before.fg, 0.0))
        covered = (s0 - s1) >= COVER_MIN_LOSS and s1 < COVER_KEEP * s0
    out: dict[str, Any] = {"read": True, "taken": False, "covered": False, "other": False}
    if taken:
        if mine(int(after.fg)):
            out.update(taken=True, hwnd=int(after.fg), pid=int(after.pids.get(after.fg) or 0))
        else:
            out["other"] = True
    if covered:
        # the windows that came up over it: newly on screen, or a share gained by at least the cover's own loss
        gainers = [h for h, s in after.shares.items() if h != before.fg
                   and (h not in before.shares or float(s) - float(before.shares.get(h, 0.0)) >= COVER_MIN_LOSS)]
        if not gainers or any(mine(h) for h in gainers):
            out["covered"] = True
        else:
            out["other"] = True
    return out


def _pid_app(pid: int) -> str:
    """The app key of a process (its image name as the timeline keys it), ``""`` unknown. Never raises."""
    if not pid:
        return ""
    try:
        import psutil

        from yuki.memory.store import app_key

        return str(app_key(psutil.Process(int(pid)).name()) or "")
    except Exception:  # noqa: BLE001
        return ""


def _app_key_of(app: Any) -> str:
    """An app as the timeline keys it (``"Spotify"`` -> ``"spotify"``), ``""`` none. Never raises."""
    if not app or not isinstance(app, str):
        return ""
    try:
        from yuki.memory.store import app_key

        return str(app_key(app) or "")
    except Exception:  # noqa: BLE001
        return app.casefold()


class _Measure:
    """One run's measured front (C1): what every rung did to it, folded (:func:`front_change`). A rung whose front
    couldn't be read counts by its route's declaration, as before (``needs_focus``). A window that came up counts as
    the rung's when it is new on screen or belongs to an app the run acts in (the skill's app, its app-typed slots'
    values, the app each rung names: :func:`acted_app`), so the user's own switch while a rung waited is no act."""

    def __init__(self, ctx: "RunContext", skill: Skill | None = None, values: Mapping[str, Any] | None = None) -> None:
        self.ctx = ctx
        self.on = ctx.origin != "eval"
        self.focus = False
        self.touched = False
        self.apps: list[str] = []
        self.hwnds: list[int] = []
        self.own: set[str] = set()
        self.first: Any = None     # review L4: the first rung's "before" read (the run's end is compared with it)
        if skill is not None:
            self.own.add(_app_key_of(skill.app))
            for slot in getattr(skill, "slots", ()) or ():
                if getattr(slot, "type", "") == "app":
                    v = (values or {}).get(slot.name)
                    self.own.add(_app_key_of(v.get("key") or v.get("name") if isinstance(v, Mapping) else v))
        self.own.discard("")

    def read(self) -> Any:
        if not self.on:
            return None
        try:
            return self.ctx.front() if self.ctx.front is not None else front_now()
        except Exception:  # noqa: BLE001
            return None

    def fold(self, route: Route, before: Any, after: Any, *, acted: bool, app: str | None) -> dict[str, Any]:
        """Fold one rung's change in; ``acted``: the rung acted by its own word; ``app``: :func:`acted_app`'s app for
        it. Returns the change (for its ``route_try`` line)."""
        if self.first is None and isinstance(before, FrontRead):
            self.first = before
        own = set(self.own) | {_app_key_of(app)}
        own.discard("")
        ch = front_change(before, after, own=(lambda _h, pid: _pid_app(pid) in own) if own else None)
        if ch.get("read"):
            self._take(ch)
            moved = bool(ch.get("taken") or ch.get("covered"))
        else:
            self.focus = self.focus or bool(route.needs_focus)
            moved = False
        if (acted or moved) and app and str(app) not in self.apps:
            self.apps.append(str(app))
        return ch

    def _take(self, ch: Mapping[str, Any]) -> None:
        """A measured change (:func:`front_change`, read) folded into the run's focus / touched / windows / apps."""
        self.focus = self.focus or bool(ch.get("taken"))
        self.touched = self.touched or bool(ch.get("taken") or ch.get("covered"))
        if ch.get("taken"):
            hwnd = int(ch.get("hwnd") or 0)
            if hwnd and hwnd not in self.hwnds:
                self.hwnds.append(hwnd)
            front_app = _pid_app(int(ch.get("pid") or 0))
            if front_app and front_app not in self.apps:
                self.apps.append(front_app)

    def settle(self) -> None:
        """Review L4: one more read when the run ends, against the first rung's "before": a window a rung raised while
        its check waited (an async raise after an open or a press) counts too. Only a window of an app the run acts
        in (or one new on screen) counts, as for each rung. A front that couldn't be read changes nothing."""
        if not self.on or not isinstance(self.first, FrontRead):
            return
        own = set(self.own) | {_app_key_of(a) for a in self.apps}
        own.discard("")
        ch = front_change(self.first, self.read(), own=(lambda _h, pid: _pid_app(pid) in own) if own else None)
        if ch.get("read"):
            self._take(ch)

    def stamp(self, result: "SkillResult", acted: bool) -> None:
        try:
            self.settle()
        except Exception:  # noqa: BLE001 - the per-rung measure stands
            pass
        # integration review F1: the run loop's declared value (every constructor passes it) is kept before the
        # measure takes its place
        result.focus_declared = bool(result.focus_taken)
        result.focus_taken = bool(self.focus)
        result.acted = bool(acted or self.touched)
        result.acted_apps = list(self.apps)
        result.acted_windows = list(self.hwnds)
        if isinstance(result.evidence, dict) and result.evidence:
            # Phase 2.3's "nothing on the PC was touched" only when nothing measurably was (one-desk D5)
            result.evidence["acted"] = result.acted


def _front_word(ch: Mapping[str, Any]) -> str:
    """A rung's front change as one code word for its log line: ``taken`` / ``covered`` / ``other`` (a change no window
    of the rung's made) / ``kept`` / ``unread``."""
    if not ch.get("read"):
        return "unread"
    return ("taken" if ch.get("taken") else "covered" if ch.get("covered") else "other" if ch.get("other")
            else "kept")


# ---------------------------------------------------------------------------------------------------------------------
# The verbs
# ---------------------------------------------------------------------------------------------------------------------


def _window(args: Mapping[str, Any]) -> int:
    if args.get("hwnd"):
        try:
            return int(args["hwnd"])
        except (TypeError, ValueError):
            return 0
    from yuki.skills.uia_route import app_window

    return int(app_window(str(args.get("window") or args.get("process") or "")) or 0)


def _held_if_refused(details: Mapping[str, Any] | None) -> str | None:
    """The refusal kind an action's ``details`` carry (``yuki.actions.gate.carried``): ``"game"`` (held for a game) or
    :data:`HELD_BLOCKED` (a settled stop, fHands), else ``None``."""
    value = details.get("refused") if isinstance(details, Mapping) else None
    return value if value in ("game", HELD_BLOCKED) else None


def v_uia_invoke(call: RouteCall) -> RouteResult:
    from yuki.skills.uia_route import press

    a = call.args
    hwnd = _window(a)
    if not hwnd:
        return RouteResult(ran=False, outcome="no_window")
    got = press(hwnd, role=str(a.get("role") or ""), name=str(a.get("name") or ""),
                pattern=str(a.get("pattern") or "invoke"), value=a.get("value"), container=a.get("container") or (),
                budget_s=max(0.2, min(float(a.get("budget_s") or 0.6), call.deadline - call.ctx.clock())))
    facts = {"status": got.outcome, "n": got.n, "ms": got.ms}
    if got.outcome == "refused" and getattr(got, "kind", "") in ("game", HELD_BLOCKED):
        # fHands: the gate refused the pattern call (a game's own window, or it failed closed): by its kind
        return RouteResult(ran=False, held=got.kind, outcome="refused", facts=facts)
    if got.outcome == "pressed":
        return RouteResult(ran=True, outcome="pressed", facts=facts)
    if got.outcome == "ambiguous":
        return RouteResult(ran=False, outcome="ambiguous", ask=str(a.get("ask_ambiguous") or
                                                                   "I found two of those. Which one?"), facts=facts)
    if got.outcome == "timeout":
        return RouteResult(ran=None, outcome="timeout", facts=facts)
    return RouteResult(ran=False, outcome=got.outcome, facts=facts)


def v_media_command(call: RouteCall) -> RouteResult:
    from yuki.actions.media import command

    a = call.args
    mc = command(str(a.get("verb") or ""), a.get("app"), timeout_s=1.0)
    facts = mc.facts()
    ram = {"title_before": mc.title, "app_id": mc.app_id}
    if mc.outcome == "already":
        return RouteResult(ran=False, already=True, outcome="already", facts=facts, facts_ram=ram)
    if mc.outcome == "sent":
        return RouteResult(ran=True, outcome="sent", facts=facts, facts_ram=ram)
    if mc.outcome == "pending":
        return RouteResult(ran=None, outcome="pending", facts=facts, facts_ram=ram)
    return RouteResult(ran=False, outcome=mc.outcome, facts=facts, facts_ram=ram)


def _opened(result: Any) -> RouteResult:
    details = getattr(result, "details", None) or {}
    held = _held_if_refused(details)
    if held:
        return RouteResult(ran=False, held=held, outcome="refused")
    ok = bool(getattr(result, "ok", False))
    page = details.get("page_check") if isinstance(details.get("page_check"), dict) else {}
    verified = True if ok and page.get("ok") is True else None
    return RouteResult(ran=ok, verified=verified, outcome="opened" if ok else "failed",
                       facts={"page": bool(page), "via": str(details.get("via") or "")[:20]})


def v_open_url(call: RouteCall) -> RouteResult:
    from yuki.actions import launch

    a = call.args
    app = a.get("app")
    return _opened(launch.open_url(str(a.get("url") or ""), app=str(app) if app else None))


def open_uri(uri: str) -> Any:
    """An app URI (``spotify:``, ``discord://``, ``whatsapp://``, ``slack://``) opened with its default handler:
    ``launch.open_url(uri)`` with no app, i.e. its D17-guarded ``os.startfile`` (refused with a game in front). It
    brings the app forward, so it counts as taking focus."""
    from yuki.actions import launch

    return launch.open_url(str(uri or ""))


def place_hint(value: Any) -> tuple[str, str] | None:
    """A run's place hint as ``(monitor, how)``: ``"other"``, a monitor index, ``"primary"`` or ``"with:<hwnd>"``,
    each with an optional ``:<how>`` (keep | maximize | left | right), with or without the ``place:`` the plan's
    window verb writes, or ``{"monitor", "how"}``. ``None`` for anything else (the run goes as before). The plan's own
    parser (``yuki.brain.plan.place_spec``): one machine format."""
    if isinstance(value, Mapping):
        value = f"place:{value.get('monitor') or 'other'}:{value.get('how') or 'keep'}"
    text = " ".join(str(value or "").split())
    if not text:
        return None
    if not text.casefold().startswith("place"):
        text = f"place:{text}"
    try:
        from yuki.brain.plan import place_spec

        return place_spec(text)
    except Exception:  # noqa: BLE001 - no hint
        return None


def place_arg(skill_key: str, args: Mapping[str, Any], slots: Mapping[str, Any]
              ) -> tuple[tuple[str, str] | None, Mapping[str, Any]]:
    """A run's place hint and its slots without it: ``args["place"]`` (the planner's run_skill), else ``slots["place"]``
    (the talker's run_skill has free-form slots and no place field) when the skill has no slot of that name; that
    entry then leaves the slots, so the resolver never reads it as a value. ``(None, slots)`` when there is none."""
    value = args.get("place")
    if value in (None, "") and "place" in slots:
        try:
            from yuki.skills.catalog import load

            skill = load().get(str(skill_key or ""))
            own = bool(skill and any(getattr(sl, "name", "") == "place" for sl in skill.slots))
        except Exception:  # noqa: BLE001 - unknown: the slot stays a slot
            own = True
        if not own:
            value = slots.get("place")
            slots = {k: v for k, v in slots.items() if k != "place"}
    return place_hint(value), slots


def place_for(ctx: RunContext | None, hwnd: int, *, activate: bool) -> bool:
    """Put the run's app window where its place hint says (:attr:`RunContext.place`; ``yuki.actions.place``: gated,
    verified by its monitor, journaled in the request's group so "put it back" undoes it). Called as soon as the
    window is known: before it is brought forward when it already exists (it comes forward there), right after it
    opens otherwise (``activate``: it keeps the front, for the typing after). ``True`` when placed; a refusal or a miss
    leaves the window where it is and the run goes on. Logged content-free.

    With no hint from the caller, the user's own desk habit for the app may give one (phase 3.3,
    :func:`habit_place`)."""
    hint = getattr(ctx, "place", None) if ctx is not None else None
    if not hint and ctx is not None and hwnd:
        hint = habit_place(ctx, int(hwnd))
    if not hint or not hwnd:
        return False
    try:
        from yuki.actions import place

        result = place.place_window(int(hwnd), monitor=hint[0], how=hint[1], activate=activate,
                                    group=getattr(ctx, "request_id", None))
        ok = bool(getattr(result, "ok", False))
    except Exception:  # noqa: BLE001 - not placed: the run goes on
        ok = False
    _emit(ctx, "skill_place", ok=ok, how=hint[1], activate=activate)
    return ok


#: How long the place default waits for memory's answer (s; it reads the desk only when the app has a habit).
HABIT_PLACE_S = 0.6


def _desk_situation() -> Any:
    """The desk now for the habit lookup: windows, monitors and what plays (no sound, no master, no holds)."""
    from yuki.perception.situation import snapshot

    return snapshot(audio=[], master=False, holds="")


def habit_place(ctx: RunContext, hwnd: int) -> tuple[str, str] | None:
    """Phase 3.3: the place hint the user's own desk habit gives a run whose caller gave none
    (:func:`yuki.memory.deskhabits.place_for`, learned nightly from where they keep the app), or ``None``.

    Only to keep their screen theirs: a hint comes only when the window shows on a monitor where another app plays or
    shows full screen now, and their habit for this app, on this desk, is another monitor (e.g. while a video plays on
    monitor 0 they keep Discord on monitor 1). A window they placed elsewhere stays where it is; a minimised one gets
    no hint (the place verb refuses it: "restore it first"); an app they keep full screen (a game) has no habit; the
    place gate still refuses with a game in front; a habit's monitor not measured lit now (final-I S1: standby, off,
    dark, no reply; :func:`yuki.actions.place.monitor_lit`, blocking) gives no hint (``skill_place_habit_held``).
    Content-free log line ``skill_place_habit``."""
    if getattr(ctx, "memory", None) is None or not hwnd:
        return None
    try:
        from yuki.actions import place as _place
        from yuki.memory import deskhabits

        app = _place._app(hwnd)
        if not app or _place._state(hwnd) == "minimized":
            return None   # place_window refuses a minimised window: no hint, no refused move logged
        got = _mem(ctx.memory, "desk_place",
                   lambda c: deskhabits.place_for(_store_of(c), app, situation=_desk_situation),
                   timeout_s=HABIT_PLACE_S)
        if not isinstance(got, Mapping) or got.get("monitor") is None:
            return None
        where = _place.monitor_index(hwnd)
    except Exception:  # noqa: BLE001 - no habit: the window goes where its app puts it
        return None
    target = int(got["monitor"])
    if where is None or target == where or where not in set(got.get("busy") or ()):
        return None
    if not _place.monitor_lit(target):
        # final-I S1 (P0-5): their habit's monitor is not measured lit now (standby, off, dark, no reply): a window
        # moved there would be out of their sight, so no hint; the window stays where it is
        _emit(ctx, "skill_place_habit_held", why="unlit", monitor=target, was=where)
        return None
    _emit(ctx, "skill_place_habit", situational=got.get("when") == "while", n=got.get("n"), m=got.get("m"),
          monitor=target, was=where)
    return str(target), str(got.get("how") or "keep")


def v_open_uri(call: RouteCall) -> RouteResult:
    result = open_uri(str(call.args.get("uri") or ""))
    details = getattr(result, "details", None) or {}
    hwnd = details.get("hwnd") if isinstance(details, Mapping) else None
    if getattr(result, "ok", False) and isinstance(hwnd, int) and not isinstance(hwnd, bool):
        place_for(call.ctx, hwnd, activate=True)
    return _opened(result)


def v_focus(call: RouteCall) -> RouteResult:
    from yuki.actions import launch

    hwnd = _window(call.args)
    if not hwnd:
        return RouteResult(ran=False, outcome="no_window")
    place_for(call.ctx, hwnd, activate=False)   # P1.4: it comes forward on the screen the hint names
    result = launch.focus_window(hwnd)
    held = _held_if_refused(getattr(result, "details", None))
    if held:
        return RouteResult(ran=False, held=held, outcome="refused")
    ok = bool(getattr(result, "ok", False))
    return RouteResult(ran=ok, verified=True if ok else None, outcome="focused" if ok else "failed")


def v_tab_select(call: RouteCall) -> RouteResult:
    from yuki.actions import tabs

    a = call.args
    hwnd = _window(a)
    if not hwnd:
        return RouteResult(ran=False, outcome="no_window")
    if a.get("tab_id"):
        result = tabs.tab_action(hwnd, str(a["tab_id"]), "select")
    else:
        try:
            index = int(a.get("index"))
        except (TypeError, ValueError):
            return RouteResult(ran=False, outcome="no_tab")
        result = tabs.select_tab(hwnd, index, expect_name=a.get("name"))
    ok = bool(getattr(result, "ok", False))
    return RouteResult(ran=ok, verified=True if ok else None, outcome="selected" if ok else "failed")


def v_uia_steps(call: RouteCall) -> RouteResult:
    from yuki.skills.uia_route import SkillRefused, route_steps, run_route

    hands = call.ctx.hands
    if hands is None or getattr(hands, "rung_b", True) is False:
        # the new brain's desk hands carry a capability's own driver steps only: a learned recorded path stays the
        # planner's there (rung C, the skill as its plan seed), as before (zFinal)
        return RouteResult(ran=False, outcome="no_hands")
    a = call.args
    try:
        steps = route_steps(a.get("steps"), call.values)
        sends = a.get("sends") if isinstance(a.get("sends"), Mapping) else None
        ok, _report, facts = run_route(hands, steps, words=list(call.ctx.words), apps=list(a.get("apps") or []),
                                       key=call.skill.key, sends=sends)
    except SkillRefused:
        return RouteResult(ran=False, outcome="refused_steps")
    done = int(facts.get("steps_done") or 0)
    kind = _held_if_refused(facts)
    if kind or facts.get("act_off"):
        return RouteResult(ran=done > 0, held=kind, outcome="refused")
    if facts.get("to_planner"):
        # bSkill (bTalk merge note 4): the consent rule (brain = "all": hands.authorize -> brain.check.one) handed a
        # step over that would send or join; it did not run. Rung C, never the talker's "exactly this?"
        return RouteResult(ran=done > 0, outcome=TO_PLANNER, facts={"steps": int(facts.get("steps") or 0),
                                                                   "done": done})
    if facts.get("held"):
        return RouteResult(ran=done > 0, ask=str(facts.get("question") or ""), outcome="held_by_check")
    verified = True if (ok and facts.get("verified") is True) else (False if facts.get("deviation") else None)
    return RouteResult(ran=done > 0 if done or not ok else None, verified=verified,
                       outcome="steps_ok" if ok else str(facts.get("deviation") or "failed"),
                       facts={"steps": int(facts.get("steps") or 0), "done": done})


def v_slider(call: RouteCall) -> RouteResult:
    """A slider by RangeValue only: ``press``, ``hover`` and ``hotkey`` are never passed, so ``set_slider`` can't
    reach ``SetFocus``, a key or the pointer (critic H1)."""
    from yuki.actions.slider import set_slider

    a = call.args
    hwnd = _window(a)
    if not hwnd:
        return RouteResult(ran=False, outcome="no_window")
    to, by = a.get("to"), a.get("by")
    result = set_slider(hwnd, {"role": str(a.get("role") or "Slider"), "name": str(a.get("name") or "")},
                        to=float(to) if to is not None else None, by=float(by) if by is not None else None,
                        press=None, hover=None, hotkey=None)
    details = getattr(result, "details", None) or {}
    verified = details.get("verified") if isinstance(details.get("verified"), bool) else None
    ran = True if verified else (False if details.get("missing") else None)
    value = details.get("after") if isinstance(details.get("after"), (int, float)) else None
    return RouteResult(ran=ran, verified=verified, outcome="set" if verified else "not_verified",
                       value=float(value) if value is not None else None)


def v_app_volume(call: RouteCall) -> RouteResult:
    from yuki.actions.app_volume import set_app_volume

    a = call.args

    def num(k: str) -> float | None:
        v = a.get(k)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    mute = a.get("mute")
    result = set_app_volume(str(a.get("app") or ""), level=num("level"), change=num("change"),
                            mute=(mute == "on" or mute is True) if mute is not None else None)
    details = getattr(result, "details", None) or {}
    verified = details.get("verified") if isinstance(details.get("verified"), bool) else None
    ran = "before" in details or bool(verified)
    after = details.get("after") if isinstance(details.get("after"), (int, float)) else None
    return RouteResult(ran=ran, verified=verified if ran else None, outcome="set" if verified else
                       ("not_verified" if ran else "no_app"), value=float(after) if after is not None else None)


def v_endpoint_volume(call: RouteCall) -> RouteResult:
    from yuki.perception.audio import set_master_volume

    a = call.args
    level = a.get("level") if isinstance(a.get("level"), (int, float)) and not isinstance(a.get("level"), bool) \
        else None
    change = a.get("change") if isinstance(a.get("change"), (int, float)) and not isinstance(a.get("change"), bool) \
        else None
    mute = a.get("mute")
    want_mute = (mute == "on" or mute is True) if mute is not None else None
    if level is None and change is None and want_mute is None:
        # review r2 L3: nothing to set is no action, so never a done (set_app_volume refuses the same case)
        return RouteResult(ran=False, outcome="no_value")
    got = set_master_volume(level=level, change=change, mute=want_mute)
    if not got:
        return RouteResult(ran=False, outcome="unavailable")
    ok = True
    if level is not None or change is not None:
        target = level if level is not None else max(0.0, min(100.0, float(got["before"]) + float(change)))
        ok = abs(float(got["level"]) - float(target)) <= 1.0
    if want_mute is not None:
        ok = ok and bool(got.get("muted")) == want_mute
    return RouteResult(ran=True, verified=ok, outcome="set" if ok else "not_verified", value=float(got["level"]))


def v_driver(call: RouteCall) -> RouteResult:
    route = call.route
    try:
        module = importlib.import_module(f"yuki.skills.{route.driver}")
        fn = (getattr(module, "ROUTES", {}) or {}).get(route.call)
    except Exception:  # noqa: BLE001
        fn = None
    if not callable(fn):
        return RouteResult(ran=False, outcome="no_driver")
    got = fn(call)
    return got if isinstance(got, RouteResult) else RouteResult(ran=None, outcome="bad_driver")


VERB_FUNCS: dict[str, Callable[[RouteCall], RouteResult]] = {
    "uia_invoke": v_uia_invoke, "media_command": v_media_command, "open_url": v_open_url, "open_uri": v_open_uri,
    "focus": v_focus, "tab_select": v_tab_select, "uia_steps": v_uia_steps, "slider": v_slider,
    "app_volume": v_app_volume, "endpoint_volume": v_endpoint_volume, "driver": v_driver,
}


# ---------------------------------------------------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------------------------------------------------


def _names_slot(value: Any, drop: frozenset[str]) -> bool:
    """Whether a check argument is a string whose templates name one of the ``drop`` slots (``"{playlist.name}"``
    names ``playlist``)."""
    return isinstance(value, str) and any(n.split(".", 1)[0] in drop for n in template_names(value))


def _without_slots(value: Any, drop: frozenset[str]) -> Any:
    """A check's arguments without every argument that names a ``drop`` slot, at any depth (an ``all`` check's parts
    keep their other arguments). Plumbing on the connection's own templates."""
    if not drop:
        return value
    if isinstance(value, Mapping):
        return {k: _without_slots(v, drop) for k, v in value.items() if not _names_slot(v, drop)}
    if isinstance(value, (list, tuple)):
        return [_without_slots(v, drop) for v in value if not _names_slot(v, drop)]
    return value


def _done_spec(skill: Skill, values: Mapping[str, Any], routes: list[Route] | None = None,
               drop: Any = ()) -> dict[str, Any] | None:
    """The check a pinned skill is held to (the thinker's finish gate; the new brain's plan seed, where it is fixed):
    the first route done-check (else re-check) that is not the route's own read-back, its templates filled. The routes
    these values take come first (bSkill: ``routes``, the resolver's ladder for this run, else every route whose
    ``when`` holds for them), so a hand-off of the PC's own level is held to that level's check and never to the first
    route's (an app slider's); then any route, as before. ``drop``: slots whose value a route found missing on screen
    (x5Fallback): the arguments naming them are left out of the check, so "Japanese" (the user's word for a playlist
    named otherwise) is never required as the name on screen."""
    from yuki.skills.resolve import _when_ok

    gone = frozenset(str(d) for d in drop or () if d)
    try:
        first = [r for r in routes if r.via != "agent"] if routes else [r for r in skill.routes if _when_ok(r, values)]
    except Exception:  # noqa: BLE001 - the routes as listed
        first = []
    for group in (first, skill.routes):
        for r in group:
            for c in (r.done, r.recheck):
                if c is not None and c.probe != "read_back":
                    try:
                        return fill_value(_without_slots(c.as_dict(), gone), values)
                    except KeyError:
                        continue
    return None


def _missing_slot(skill: Skill, name: Any) -> str | None:
    """The slot a route reported it found no item for (``facts["missing"]``), when it is one of the skill's own slots
    (code data; anything else is ignored)."""
    name = str(name or "").strip()
    return name if name and skill.slot(name) is not None else None


def _drop_for(skill: Skill, missing: Any, filled: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The slots whose arguments leave the seed's done-check, **entities** only: one a route found missing, and one
    whose value the caller gave but the words as heard don't hold (a dropped hand-off: :data:`FROM_UNHEARD` /
    :data:`FROM_GIVEN`, the model's own reading of a word nobody heard; Phase 2.1: a reading taken as given,
    ``resolve.FROM_READING``). An entity's name is the user's own word for a thing that may be named otherwise on
    screen ("Japanese" for ネオン), or a guess at it, so the check never holds the thinker to it; a person, an app or a
    text keeps its check (a message is never confirmed against "any chat")."""
    from yuki.skills.resolve import FROM_READING

    names = [_missing_slot(skill, missing)]
    names += [k for k, src in (filled or {}).items() if src in (FROM_UNHEARD, FROM_GIVEN, FROM_READING)]
    out: list[str] = []
    for name in names:
        slot = skill.slot(name) if name else None
        if slot is not None and slot.type == "entity" and name not in out:
            out.append(name)
    return tuple(out)


def _seen_names(names: Any) -> list[str]:
    """The names a route read on screen (``facts_ram["seen"]``), cleaned: strings, squashed, each at most
    :data:`SEEN_NAME_CHARS`, no repeats, at most :data:`SEEN_MAX`. RAM only (they are the user's own things)."""
    out: list[str] = []
    for n in names if isinstance(names, (list, tuple)) else ():
        text = " ".join(str(n).split())[:SEEN_NAME_CHARS] if isinstance(n, str) else ""
        if text and text not in out:
            out.append(text)
        if len(out) >= SEEN_MAX:
            break
    return out


def _check_status(evidence: Any, depth: int = 0) -> str:
    """The code word a done-check's evidence gives for what it read (its ``status``, else its ``reason``: "no_window",
    "other_page", "unread"), at any depth of an ``all`` check's parts; ``""`` when none."""
    if not isinstance(evidence, Mapping) or depth > 3:
        return ""
    for key in ("status", "reason"):
        word = evidence.get(key)
        if isinstance(word, str) and _SLOT_NAME.fullmatch(word):
            return word
    for value in evidence.values():
        got = _check_status(value, depth + 1)
        if got:
            return got
    return ""


def _evidence(skill: Skill, values: Mapping[str, Any] | None, tried: Sequence[Mapping[str, Any]], *, acted: bool,
              route: Route | None = None, waited: Any = None, missing: str | None = None, seen: Any = (),
              sources: Mapping[str, str] | None = None, reason: str = "", line: str = "") -> dict[str, Any]:
    """Phase 2.3: what a run that didn't end in a checked done tried, saw and is missing, for the talker to word in one
    short line (never the code's template): ``asked`` (the values as said), ``from`` (where each given value came
    from), ``tried`` (each rung: its key, verb and outcome code), ``acted`` (whether anything on the PC was touched),
    ``check`` (the last done-check: its probe, ``done`` / ``not_so`` / ``could_not_read``, and the status it read),
    ``missing`` (the slot no item was found for), ``seen`` (up to :data:`EVIDENCE_SEEN` names read there instead),
    ``reason`` (a code word: why it stopped) and ``fallback_line`` (the code's own line, said only if the talker can't).
    RAM only. Never raises."""
    ev: dict[str, Any] = {"acted": bool(acted)}
    try:
        asked = {k: shown_value(skill, k, v) for k, v in dict(values or {}).items()}
        asked = {k: v for k, v in asked.items() if v}
        if asked:
            ev["asked"] = asked
        if sources:
            ev["from"] = {str(k): str(v) for k, v in sources.items()}
        steps = [{"route": t.get("route"), "via": t.get("via"), "outcome": t.get("outcome"),
                  # integ28 (gb merge note 6, K-S): the switcher it opened still holds the keyboard (measured)
                  **({"switcher_held": True} if t.get("switcher_held") is True else {})}
                 for t in tried or () if isinstance(t, Mapping)]
        if route is not None and not any(s.get("route") == route.key for s in steps):
            steps.append({"route": route.key, "via": route.via, "outcome": "acted"})
        if steps:
            ev["tried"] = steps[-6:]
        if waited is not None:
            check = {"probe": (route.done.probe if route is not None and route.done is not None else "read_back"),
                     "saw": {True: "done", False: "not_so", None: "could_not_read"}.get(waited.verdict,
                                                                                     "could_not_read")}
            status = _check_status(getattr(waited, "evidence", None))
            if status:
                check["status"] = status
            ev["check"] = check
        if missing:
            ev["missing"] = str(missing)
        names = _seen_names(seen)[:EVIDENCE_SEEN]
        if names:
            ev["seen"] = names
        if reason:
            ev["reason"] = str(reason)
        if line:
            ev["fallback_line"] = " ".join(str(line).split())
    except Exception:  # noqa: BLE001 - what was gathered so far
        pass
    return ev


def _person_value(skill: Skill, values: Mapping[str, Any] | None) -> str:
    """fd (D5): the person a run is for, as said (its person slot's value: a ``person`` slot or a text slot the
    capability binds to a contact), ``""`` with none. RAM only."""
    try:
        from yuki.skills.resolve import _person_bound

        for slot in skill.slots:
            if _person_bound(skill, slot) and (values or {}).get(slot.name):
                return shown_value(skill, slot.name, (values or {}).get(slot.name))
    except Exception:  # noqa: BLE001
        pass
    return ""


def evidence_words(ev: Mapping[str, Any] | None) -> str:
    """The evidence (:func:`_evidence`) as one line for the model that words the outcome: "Evidence: asked site
    excalidraw.com; tried open_url (open_url: opened); the PC was touched; the check tab_url could_not_read
    (no_window); ...". ``""`` for none."""
    if not isinstance(ev, Mapping) or not ev:
        return ""
    parts: list[str] = []
    asked = ev.get("asked") if isinstance(ev.get("asked"), Mapping) else {}
    if asked:
        src = ev.get("from") if isinstance(ev.get("from"), Mapping) else {}
        parts.append("asked " + ", ".join(
            f"{k} {v}" + (f" (from {src[k]})" if src.get(k) and src[k] != "words" else "") for k, v in asked.items()))
    tried = [t for t in ev.get("tried") or [] if isinstance(t, Mapping)]
    if tried:
        parts.append("tried " + ", ".join(f"{t.get('route')} ({t.get('via')}: {t.get('outcome')})" for t in tried))
    if any(t.get("switcher_held") is True for t in tried):
        # integ28 (gb merge note 6, K-S): measured at the rung's end, never inferred from its outcome word
        parts.append("the switcher it opened still holds the keyboard (measured)")
    parts.append("the PC was touched" if ev.get("acted") else "nothing on the PC was touched")
    check = ev.get("check") if isinstance(ev.get("check"), Mapping) else None
    if check:
        status = f" ({check['status']})" if check.get("status") else ""
        parts.append(f"the check {check.get('probe')} {check.get('saw')}{status}")
    if ev.get("missing"):
        parts.append(f"no item found for {ev['missing']}")
    if ev.get("seen"):
        parts.append("seen there instead: " + " | ".join(str(n) for n in ev["seen"]))
    if ev.get("reason"):
        parts.append(f"stopped: {ev['reason']}")
    if ev.get(UNREAD):
        # skill20 (C10): a rung that never read the app pressed nothing there
        parts.append("never read the app, so nothing was pressed there: " + ", ".join(str(r) for r in ev[UNREAD]))
    # skill19: the chat opened and nothing was typed (C3), what holds the keyboard; the control's level read (O5); a
    # code-typed value given in another form than the slot takes (RC5a)
    if ev.get("not_typed") or ev.get("reason") == "not_typed":
        # fd (final-F D5; live 20 Oct k_clarify_who_draft under one: "Discord is still on the Quasar40404 chat, not
        # Hemanth's" for Hemanth's own chat, since the evidence said neither whose chat opened nor why nothing was
        # typed): the driver's facts, as they are
        who = ev.get("chat_open")
        head = (f"{who}'s chat opened (the PC shows it open)" if isinstance(who, str) and who
                else "the chat opened (the PC shows it open)" if who else "the chat opened")
        held = (": the typing was held by Yuki's own step check, as its text is not in this request's words"
                if ev.get("typing_held") else "")
        parts.append(f"{head} but nothing was typed{held}" + (f" ({ev['holder']} has the keyboard)"
                                                             if ev.get("holder") else ""))
    if isinstance(ev.get("current"), (int, float)) and not isinstance(ev.get("current"), bool):
        parts.append(f"the control reads {float(ev['current']):g}% now")
    forms = ev.get("form") if isinstance(ev.get("form"), Mapping) else {}
    for k, v in forms.items():
        parts.append(f"{k} as given isn't a form it takes ({v})")
    if ev.get("fallback_line"):
        parts.append(f"code's fallback line: \"{ev['fallback_line']}\"")
    return "Evidence: " + "; ".join(parts) + "."


def pinned(skill: Skill, values: Mapping[str, Any], filled: Mapping[str, str], tried: list[dict[str, Any]],
           *, why: str = "", baseline: Mapping[str, Any] | None = None, routes: list[Route] | None = None,
           missing: str | None = None, seen: Any = (), partial: bool = False) -> dict[str, Any]:
    """The pinned skill handed over at rung C (map-skills S3.4): its goal, the slots with their sources, what "done"
    means (words and the machine check: :func:`_done_spec`, from ``routes`` when given), what was already tried,
    ``baseline``: the onset probes' states read right before the hand-off (the finish gate compares with them;
    content-free booleans), and ``sends`` when running it sends something to other people (``partial``: ``values``
    are only what the caller gave, so a send condition on a slot it left out counts as met: rule 8 errs toward "may
    send"). At most :data:`PINNED_CHARS` as rendered.

    x5Fallback (IF-seed): ``missing``, the slot whose value no route found on screen (an entity slot's value is left
    out of the done-check and the done words: the user's word for it may not be its name there), and ``seen``, up to
    :data:`SEEN_MAX` names the routes read there (RAM only, never logged): the thinker picks the one meant from them
    and memory.

    The same dict is the new brain's **plan seed** (brain-design 2.1, bSkill): ``skill`` names the skill for the
    planner's seed block (``key`` stays for the old loop's frame and finish gate), and its ``done`` is the plan's fixed
    done-check (``yuki.brain.plan`` takes it when the planner writes ``probe none``; ``yuki.brain.done.seed_done``
    reads it). :func:`plan_seed` makes one from a pin kept elsewhere (a voice escalation's ``skill=``)."""
    missing = _missing_slot(skill, missing)
    drop = _drop_for(skill, missing, filled)
    shown = {k: v for k, v in values.items() if k not in drop}
    slots = {k: {"value": v, "from": filled.get(k, "")} for k, v in canonical(values).items()}
    out = {"key": skill.key, "app": skill.app, "goal": skill.menu, "slots": slots,
           "done_words": fill(skill.done_line, shown) or "", "done": _done_spec(skill, values, routes, drop=drop),
           "tried": [{"rung": t.get("rung"), "via": t.get("via"), "why": t.get("outcome"),
                      # integ28 (gb merge note 6, K-S): the switcher's measured hold, within PINNED_CHARS
                      **({"switcher_held": t["switcher_held"]} if isinstance(t.get("switcher_held"), bool) else {})}
                     for t in tried][:4],
           "why": why}
    # ``partial``: a value only the slot's default gave is no answer to the send condition (left out: it counts as met)
    basis = {k: v for k, v in values.items() if filled.get(k) != "default"} if partial else values
    if sends_with(skill, basis, partial=partial):
        # review r2 M1: the finish gate never sends a pressed send back to "finish it" (rule 8: a second send)
        out["sends"] = True
    if baseline:
        out["baseline"] = dict(baseline)
    if missing:
        out["missing"] = missing
    while len(json.dumps(out, ensure_ascii=False, default=str)) > PINNED_CHARS and out["tried"]:
        out["tried"] = out["tried"][:-1]
    # bSkill: the planner's seed block names the skill by ``skill`` (added after the size fit, so the old loop's
    # rendered block, which never shows it, keeps its size); x5Fallback: ``seen`` likewise (the frame gives it its own
    # room, ``yuki.agent.frame.SEEN_CHARS``)
    out["skill"] = skill.key
    if any(isinstance(t, Mapping) and t.get("switcher_held") is True for t in tried):
        # integ28 (gb merge note 6, K-S): the seed's own fact too, added after the size fit as ``skill`` is, so the fit
        # dropping the last tried item (the rung that left it) never loses it; ``executor.switcher_held`` reads it
        out["switcher_held"] = True
    names = _seen_names(seen)
    if names:
        out["seen"] = names
    return out


#: The keys of a plan seed the brain reads (``yuki.brain.context.seed_block``: skill, goal, slots, done, tried, and
#: x5Fallback's missing and seen; ``yuki.brain.done.seed_done``: done, done_words, baseline; the executor's run row:
#: key). integ28 (gb merge note 6, K-S): ``switcher_held``, read by ``executor.switcher_held`` and ``seed_block``.
#: fix-r31 (A2): :data:`RERUN_KEY`, read by ``yuki.brain.plan.seed_again`` and ``seed_block``.
SEED_KEYS: tuple[str, ...] = ("skill", "key", "app", "goal", "slots", "done", "done_words", "tried", "why", "sends",
                              "baseline", "missing", "seen", "switcher_held", RERUN_KEY)


def plan_seed(pin: Any) -> dict[str, Any] | None:
    """The new brain's plan seed (``Brain.run(plan_seed=)``) from a pinned skill (:func:`pinned`, or one kept across a
    hand-off: a voice escalation's ``skill=``, the runtime job's ``skill``): the same keys, with ``skill`` set from
    ``key`` when an older pin lacks it. ``None`` for anything that is not a pin (no skill named). Code data only: the
    done-check is copied as the engine wrote it, never rewritten."""
    if not isinstance(pin, Mapping):
        return None
    key = str(pin.get("skill") or pin.get("key") or "").strip()
    if not key:
        return None
    seed = {k: pin[k] for k in SEED_KEYS if k in pin and pin[k] is not None}
    seed["skill"], seed["key"] = key, str(pin.get("key") or key)
    return seed


def _app_title(app: str) -> str:
    try:
        from yuki.skills.uia_route import app_words

        return app_words(app)
    except Exception:  # noqa: BLE001
        return app


def run(resolved: Any, ctx: RunContext | None = None) -> SkillResult:
    """Run what :func:`yuki.skills.resolve.resolve` gave (see the module docstring). Never raises."""
    ctx = ctx or RunContext()
    started = ctx.clock()
    # the run's ledger key and what it tried, while it runs (a run that breaks partway still finishes its row)
    state: dict[str, Any] = {}
    try:
        result = _run(resolved, ctx, started, state)
    except Exception as exc:  # noqa: BLE001 - a broken run is an error outcome, never a crash
        key = getattr(getattr(resolved, "skill", None), "key", None) or str(getattr(resolved, "skill", "") or "")
        _emit(ctx, "skill_run", skill=key, result="error", error=type(exc).__name__)
        handed = _error_handoff(resolved, ctx, started, state)
        if handed is not None:
            result = handed
        else:
            _ledger_finish(ctx, state.get("run_key"), outcome="error", checked=None, error=type(exc).__name__,
                           acted=bool(state.get("acted")))
            tried = [dict(t) for t in state.get("tried") or [] if isinstance(t, Mapping)]
            ev: dict[str, Any] = {"acted": bool(state.get("acted")), "reason": "error", "fallback_line": LINE_FAILED}
            if tried:
                ev["tried"] = [{"route": t.get("route"), "via": t.get("via"), "outcome": t.get("outcome")}
                               for t in tried][-6:]
            result = SkillResult(skill=key, outcome="error", say=LINE_FAILED,
                                 ms=round((ctx.clock() - started) * 1000.0, 1), evidence=ev)
    measure = state.get("measure")
    if isinstance(measure, _Measure):
        # skill19 (contract C1): every return path of a run that reached its rungs (a done, a hand-off, routes_failed,
        # a rung's question, a hold, an error) carries what the rungs measurably did to the front
        try:
            measure.stamp(result, bool(state.get("acted")))
        except Exception:  # noqa: BLE001 - the declared values stand
            pass
    _stamp_sources(result, resolved, state)
    _stamp_form_recall(result, ctx)
    if isinstance(state.get("items"), dict) and not result.items:
        result.items = dict(state["items"])
    _note_trace(resolved, result, ctx, state)
    return result


@dataclass
class _Trace:
    """fd (final-F D3, D4): one run as the engine saw it (RAM only, this process, never logged): when (monotonic), its
    capability, the request it ran in and the user's words then (a digest), how it ended, what it asked for and what
    its rungs acted on (entity slots, by name), what it itself left out (a correction's ``turned_down``), and the slot
    a question of it was about."""

    at: float
    cap: str
    request: str
    heard: str
    outcome: str
    asked: dict[str, str]
    items: dict[str, str]
    down: dict[str, list[str]]
    asked_slot: str = ""
    #: integ27 fix D3: every slot value of the run as its identity (:func:`_run_ids`; ``{}`` for a question): a
    #: correction swaps a value only in a request that is otherwise this run's own
    ids: dict[str, str] = field(default_factory=dict)


_TRACES: list[_Trace] = []
_trace_lock = threading.Lock()


def _heard_digest(words: Any) -> str:
    """The user's words of a run as a digest (RAM only, never logged): whether they have spoken since. ``""`` none."""
    import hashlib

    seq = [str(w) for w in words or () if str(w or "").strip()]
    if not seq:
        return ""
    return hashlib.sha256("\x1e".join(" ".join(w.split()) for w in seq).encode("utf-8", "replace")).hexdigest()[:16]


def _cap_key(skill: Any) -> str:
    return str(getattr(skill, "capability", "") or getattr(skill, "key", "") or "")


def _note_trace(resolved: Any, result: SkillResult, ctx: RunContext, state: Mapping[str, Any]) -> None:
    """Keep this run's trace (:class:`_Trace`) for the next run of its capability in the same exchange. Never
    raises."""
    try:
        ids: dict[str, str] = {}
        if isinstance(resolved, Ask):
            from yuki.skills.catalog import load

            skill = load().get(str(resolved.skill or ""))
            asked: dict[str, str] = {}
            asked_slot = str(resolved.slot or "")
        elif isinstance(resolved, (Plan, Handoff)):
            skill = resolved.skill
            asked = {s.name: " ".join(str(_entity_name(resolved.values.get(s.name))).split())
                     for s in skill.slots if s.type == "entity" and _entity_name(resolved.values.get(s.name))}
            asked_slot = str(state.get("asked_slot") or "")
            try:
                ids = _run_ids(resolved)   # integ27 fix D3
            except Exception:  # noqa: BLE001
                ids = {}
        else:
            return
        cap = _cap_key(skill)
        if not cap:
            return
        down = {k: list(v) for k, v in (ctx.turned_down or {}).items() if isinstance(v, (list, tuple))}
        entry = _Trace(time.monotonic(), cap, str(ctx.request_id or ""), _heard_digest(ctx.words),
                       str(result.outcome or ""), asked, dict(result.items or {}), down, asked_slot, ids)
        with _trace_lock:
            now = entry.at
            _TRACES[:] = [t for t in _TRACES if now - t.at <= TRACE_KEEP_S][-(TRACE_RUNS - 1):] + [entry]
    except Exception:  # noqa: BLE001 - nothing kept: the next run goes as given
        pass


def _entity_name(value: Any) -> str:
    """An entity value's name (``{"name": ...}`` or a plain string), ``""`` none."""
    if isinstance(value, Mapping):
        value = value.get("name")
    return str(value or "") if isinstance(value, str) else ""


def _last_trace(cap: str, *, ran: bool = False) -> _Trace | None:
    """The newest kept run of ``cap`` within :data:`TRACE_KEEP_S` (``ran``: only one that reached its rungs, never a
    question, a hold or a refusal before anything ran), else None."""
    now = time.monotonic()
    with _trace_lock:
        for t in reversed(_TRACES):
            if t.cap == cap and now - t.at <= TRACE_KEEP_S and not (ran and t.outcome in NOT_RAN_OUTCOMES):
                return t
    return None


#: fd (D3): the ends of a run that put nothing on (a question, a hold, a refusal before anything ran).
NOT_RAN_OUTCOMES: frozenset[str] = frozenset({"asked", "held_game", "held_call", "blocked", "refused", NOT_DONE})


def _same_request(t: _Trace, ctx: RunContext) -> bool:
    """Whether a kept run ran in this very request: the same request id, else (no id) the same words."""
    if t.request and ctx.request_id:
        return t.request == str(ctx.request_id)
    heard = _heard_digest(ctx.words)
    return bool(heard) and t.heard == heard


#: fd (D4): the ends after which a run of the same capability in the same request is a fallback (another way to the
#: same ask), never the user's own choice of means.
MISSED_OUTCOMES: frozenset[str] = frozenset({"failed", "unverified", "handed_over", "error", "refused"})


def _fell_back(cap: str, ctx: RunContext) -> bool:
    """fd (final-F D4; live 20 Oct spotify_inapp_volume_down: the in-app rung's "failed", then the talker's own
    ``control: mixer`` and "Keep this as your usual?"): whether the newest run of ``cap`` ran in this same request
    and ended without the thing done (:data:`MISSED_OUTCOMES`): this run is its fallback. Code facts only."""
    t = _last_trace(cap)
    return t is not None and t.outcome in MISSED_OUTCOMES and _same_request(t, ctx)


def _answered_slots(cap: str, ctx: RunContext) -> set[str]:
    """fd (D4): the slot the newest run of ``cap`` asked about (a question of the resolver or a rung's, the means
    ask), when the user has spoken since (their words changed): this run's value for it is their answer."""
    t = _last_trace(cap)
    if t is None or t.outcome != "asked" or not t.asked_slot:
        return set()
    heard = _heard_digest(ctx.words)
    return {t.asked_slot} if heard and heard != t.heard else set()


#: integ27 fix D3: the ends of a run that put nothing on by its own rungs (its hand-off, a miss).
NOTHING_ON_OUTCOMES: frozenset[str] = frozenset({"handed_over", "failed", "error", "refused"})


def _corrected_trace(cap: str, ctx: RunContext) -> _Trace | None:
    """integ27 fix D3: the run a correction corrects: the newest run of ``cap`` within :data:`TRACE_KEEP_S` that
    reached its rungs (never a question, a hold or a refusal), passing over the correction's own earlier tries: a run
    in this very request (:func:`_same_request`: ONE's second way re-runs the same input), and a correcting run that
    put nothing on by its rungs and recorded no row (its ``down`` set, :data:`NOTHING_ON_OUTCOMES`: the voice's try
    that the all brain's thinker now finishes in a request of its own). Such a try's ``down`` was copied from the
    older run this returns, so a chain of corrections still adds up, and its asked name is the correction's own
    restated value, never a row. ``None`` none."""
    now = time.monotonic()
    with _trace_lock:
        traces = list(_TRACES)
    for t in reversed(traces):
        if t.cap != cap or now - t.at > TRACE_KEEP_S or t.outcome in NOT_RAN_OUTCOMES or _same_request(t, ctx):
            continue
        if t.down and not t.items and t.outcome in NOTHING_ON_OUTCOMES:
            continue
        return t
    return None


def _corrected(cap: str, skill: Skill, ctx: RunContext) -> tuple[dict[str, list[str]], _Trace | None]:
    """fd (final-F D3; live 20 Oct k_playlist_not_that, both brains: "No, not this one. The other playlist." ran ネオン
    again): what a correction leaves out, by taste slot: the run it corrects (:func:`_corrected_trace`), the row its
    rungs acted on or found playing (verified or not), and what it left out itself (a chain of corrections never comes
    back round). Review M1: the value it asked for counts only when no rung recorded a row (then that name is all that
    is known of what ran); a name asked that a rung resolved to another row is the user's words, never a row, so "No, I
    said chill" after ネオン is resolved again with ネオン left out, never swapped away. integ27 fix D3: never one of
    the correction's own tries (:func:`_corrected_trace`). Names in RAM only; ``({}, None)`` with none; the trace goes
    back with them (the swap's other-slot check)."""
    t = _corrected_trace(cap, ctx)
    if t is None:
        return {}, None
    out: dict[str, list[str]] = {}
    for slot in skill.slots:
        if not slot.taste:
            continue
        names = [t.items.get(slot.name) or t.asked.get(slot.name, ""), *(t.down.get(slot.name) or [])]
        kept = list(dict.fromkeys(" ".join(str(n).split()) for n in names if str(n or "").strip()))
        if kept:
            out[slot.name] = kept
    return out, t


def _otherwise_same(resolved: Plan, given: Mapping[str, Any] | None, slot: str, t: _Trace | None) -> bool:
    """integ27 fix D3 (Phase 2.1's ``_rejected_slot`` rule for D3's swap): whether a correcting request is otherwise
    the corrected run's own: every slot the caller gave in it, but ``slot`` and the note and choice slots, resolved to
    the same identity as in that run (``_Trace.ids``). A correction that changed another value ("No, in Arc"; "no,
    lower") runs as given. Identities only; never raises."""
    if t is None:
        return False
    try:
        now_ids = _run_ids(resolved)
        keys = [str(k) for k in (given or {}) if str(k) != slot and str(k) not in NOTE_SLOTS
                and str(k) not in CHOICE_SLOTS]
        return all(now_ids.get(k, "") == t.ids.get(k, "") for k in keys)
    except Exception:  # noqa: BLE001 - unsure: as given
        return False


def _stamp_form_recall(result: SkillResult, ctx: RunContext) -> None:
    """skill22 (A19, the plan's side of :func:`_first_form_slip`): a run that went on without a value given in another
    form than its slot takes (``evidence["form"]``), touched nothing and ended refused or failed is handed back to a
    caller that calls again (:attr:`RunContext.form_recall`), once per
    request: ``evidence["recall"]``, which its report and ``as_voice`` (``shape``) carry. Never raises."""
    try:
        ev = result.evidence if isinstance(result.evidence, dict) else None
        forms = ev.get("form") if ev else None
        if (not ev or not isinstance(forms, Mapping) or not forms or ev.get(FORM_RECALL) or result.done
                or result.acted or ev.get("acted") or result.outcome not in ("failed", "refused")):
            return
        if _first_form_slip(ctx, str(result.skill or ""), forms):
            ev[FORM_RECALL] = True
    except Exception:  # noqa: BLE001
        pass


def _stamp_sources(result: SkillResult, resolved: Any, state: Mapping[str, Any]) -> None:
    """skill20 (contract C10): the run's ``fill_sources`` (the resolver's ``filled``: a plan's or a hand-off's) and
    ``unread`` (the rungs whose own fact says they never read: :data:`UNREAD`), on the result and, when it has any, in
    its evidence (the caller reads them with ``.get``). Code words only. Never raises."""
    try:
        filled = getattr(resolved, "filled", None) if isinstance(resolved, (Plan, Handoff)) else None
        if isinstance(filled, Mapping) and not result.fill_sources:
            result.fill_sources = {str(k): str(v) for k, v in filled.items() if v}
        tried = result.tried or state.get("tried") or []
        unread = [str(t.get("route")) for t in tried
                  if isinstance(t, Mapping) and t.get(UNREAD) is True and t.get("route")]
        result.unread = list(dict.fromkeys([*result.unread, *unread]))
        if isinstance(result.evidence, dict) and result.evidence:
            if result.fill_sources:
                result.evidence["fill_sources"] = dict(result.fill_sources)
            if result.unread:
                result.evidence[UNREAD] = list(result.unread)
    except Exception:  # noqa: BLE001 - the result stands without them
        pass


def _error_handoff(resolved: Any, ctx: RunContext, started: float,
                   state: Mapping[str, Any] | None = None) -> SkillResult | None:
    """x5Fallback: a run that broke partway goes to rung C (why :data:`WHY_ERROR`: look first, finish only what isn't
    done) like any other failure, from the talker or the thinker, when nothing about it forbids a second try: never a
    skill that sends with these values (rule 8: it may have gone), never one with a rung whose data says nothing may
    follow it, never a relative change (done twice), never a step that is only the user's, never a skill whose data
    never hands over (``_handoff`` itself holds it with a game in front). ``state``: the run's ledger key and what it
    tried before it broke (its row is finished as handed over). ``None``: the error stands. Never raises."""
    state = state or {}
    try:
        if not isinstance(resolved, Plan) or ctx.origin not in HANDOFF_ORIGINS:
            return None
        skill = resolved.skill
        if (not resolved.handoff or not skill.handoff or sends_with(skill, resolved.values)
                or any(not r.retry_after_acting for r in resolved.routes) or run_commit(skill) == "user"
                or _absolute_values(skill, resolved.values) is None):
            return None
        filled = resolved.filled if isinstance(resolved.filled, Mapping) else {}
        tried = [dict(t) for t in state.get("tried") or [] if isinstance(t, Mapping)]
        return _handoff(skill, dict(resolved.values), filled, tried, ctx, started, why=WHY_ERROR,
                        run_key=state.get("run_key"), acted=bool(state.get("acted")), routes=list(resolved.routes),
                        unconfirmed=True)
    except Exception:  # noqa: BLE001 - the error stands
        return None


def _settled(res: RouteResult, evidence: Any) -> bool:
    """Whether a rung's unconfirmed end is settled by the driver's own data, so it is said plainly and never handed to
    the thinker to "finish": the rung changed nothing and was only checking a state that was already there
    (``already``: Spotify's row read "Pause {name}"; the driver stops there by design, and the thinker pressing that
    row would pause it), or the check says the result took somewhere else (:data:`SETTLED_REASONS`, at any depth of an
    ``all`` check's evidence: playing on another device). Code words only."""
    if res.already:
        return True

    def reason_in(ev: Any, depth: int = 0) -> bool:
        if not isinstance(ev, Mapping) or depth > 3:
            return False
        if str(ev.get("reason") or "") in SETTLED_REASONS:
            return True
        return any(reason_in(v, depth + 1) for v in ev.values() if isinstance(v, Mapping))

    return reason_in(evidence)


def _sent_key(res: RouteResult) -> bool | None:
    """skill20 (final-D messages §6): whether a sending rung's send key went out, by its own code fact
    (``facts["pressed"]``, a boolean: the chat driver's dispatch of the send key), else ``None`` (unknown: "it may
    have sent" stands)."""
    for facts in (res.facts, res.facts_ram):
        pressed = facts.get("pressed") if isinstance(facts, Mapping) else None
        if isinstance(pressed, bool):
            return pressed
    return None


def _not_typed(res: RouteResult) -> bool:
    """skill19 (contract C3): whether a rung's result is the chat driver's "opened, nothing typed": its code fact
    ``facts["why"] == "not_typed"`` (contract C3), or, from a chat driver before C3, the outcome word ``not_typed`` on a
    rung that acted (it opened the chat) and asked its line. No typing went out, so nothing was sent and nothing would
    be done twice. A rung that did not run and says ``not_typed`` (the address bar's: nothing typed, nothing opened)
    is an ordinary "couldn't". Code words only."""
    facts = res.facts if isinstance(res.facts, Mapping) else {}
    if str(facts.get("why") or "") == "not_typed":
        return True
    return str(res.outcome or "") == "not_typed" and res.ran is not False and bool(res.ask)


def _nothing_out(res: RouteResult) -> bool:
    """fix-r31 (A2): whether a rung's own code fact says it put nothing anywhere (:data:`NOTHING_OUT`: no typing step
    ran, no key was pressed), and no send key went out by its ``pressed`` fact. Code words only."""
    facts = res.facts if isinstance(res.facts, Mapping) else {}
    return facts.get(NOTHING_OUT) is True and _sent_key(res) is not True


#: integ28 (gb merge note 6, contract K-S): the most characters a rung's ``stopped`` code word keeps on its tried entry.
STOPPED_CHARS = 24


def _switch_tried(res: RouteResult) -> dict[str, Any]:
    """integ28 (gb merge note 6, contract K-S, reader side; final-G2 R13): a chat switcher rung's measured
    ``switcher_held`` (a bool: what its own shortcut opened still holds the keyboard as it ends; absent = unknown) and
    why it stopped before its last step (``stopped``, a code word), copied from its facts onto its ``tried`` entry. The
    switcher writes them on its facts only on the to_planner, held, taken-over, refused and not_typed ends (its outcome
    word stays the one the engine acts on), so without this the hand-off's seed and the talker's evidence never carry
    them there. Code facts only; ``{}`` for none."""
    facts = res.facts if isinstance(res.facts, Mapping) else {}
    out: dict[str, Any] = {}
    if isinstance(facts.get("switcher_held"), bool):
        out["switcher_held"] = facts["switcher_held"]
    word = facts.get("stopped")
    if isinstance(word, str) and word.strip():
        out["stopped"] = word.strip()[:STOPPED_CHARS]
    return out


#: The slot types whose given value code checks with no words (an option, a number, a signed change, a switch).
_CODE_TYPES = ("enum", "number", "points", "bool")


def _code_slot_forms(skill: Skill | None, dropped: Sequence[str] | None) -> dict[str, str]:
    """skill19 (RC5a / C5): ``{slot: the form it takes}`` for each slot the resolver dropped that code checks by its
    type alone (:func:`yuki.skills.menu.slot_form`: "up | down | signed points"), so a value given in another form is
    never dropped silently: the caller reads what it takes. ``{}`` for none. Never raises."""
    if skill is None or not dropped:
        return {}
    try:
        from yuki.skills.menu import slot_form
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    for name in dropped:
        slot = skill.slot(str(name)) if callable(getattr(skill, "slot", None)) else None
        if slot is not None and slot.type in _CODE_TYPES and _SLOT_NAME.fullmatch(str(name)):
            form = slot_form(slot)
            if form:
                out[str(name)] = form
    return out


#: skill22 (A19): the evidence flag of a slot given in another form than it takes, handed back to the caller to call
#: once more (:func:`_first_form_slip`), and how long one request's slip is remembered (RAM only).
FORM_RECALL = "recall"
FORM_RECALL_S = 120.0
_form_slips: dict[tuple[str, str, tuple[str, ...]], float] = {}
_form_lock = threading.Lock()


def _forms_given(forms: Mapping[str, Any]) -> str:
    """The slots given in another form than they take, each with the form it takes (code's words: the slot's name and
    its form). Pure."""
    return "; ".join(f"{k} as given isn't a form it takes ({v})" for k, v in forms.items())


def _recall_line(evidence: Mapping[str, Any]) -> str:
    """skill22 (A19, fix L1): the one line that hands a run back to its caller to call once more, the slot in the form
    it takes. It is a report's first line on both sides (asked, and a refused or failed run that touched nothing),
    and short enough to stand whole in the planner's summary (the first line, cut at 200 characters) with one slot's
    form. ``""`` with no form. Pure."""
    forms = evidence.get("form") if isinstance(evidence, Mapping) else None
    if not isinstance(forms, Mapping) or not forms:
        return ""
    return (f"Nothing was done yet: {_forms_given(forms)}. Call run_skill again now, once: same skill, that slot "
            f"in that form from their words; say nothing first.")


def _first_form_slip(ctx: RunContext, skill_key: str, forms: Mapping[str, str]) -> bool:
    """skill22 (A19): whether this is the first time in this request (its id, else its session) that ``skill_key`` got
    these slots in another form than they take: then the caller calls once more in the stated form; a second slip
    of the same slots is the user's question, as before. Only for a caller that calls again
    (:attr:`RunContext.form_recall`); a run with neither id (an eval, a test) or from a card never re-calls. Noted
    as it is read. RAM only; never raises."""
    try:
        who = str(getattr(ctx, "request_id", None) or getattr(ctx, "session_id", None) or "")
        wanted = getattr(ctx, "form_recall", None)
        if wanted is None:
            wanted = getattr(ctx, "origin", "") == "thinker"
        if not wanted or not who or getattr(ctx, "origin", "") in ("eval", "card"):
            return False
        key = (who, str(skill_key), tuple(sorted(str(k) for k in forms)))
        now = time.monotonic()
        with _form_lock:
            for k in [k for k, at in _form_slips.items() if now - at > FORM_RECALL_S]:
                _form_slips.pop(k, None)
            if key in _form_slips:
                return False
            _form_slips[key] = now
        return True
    except Exception:  # noqa: BLE001 - unknown: the question, as before
        return False


def _absolute_values(skill: Skill, values: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The run's slot values when they name an end state outright (D19's "the same skill asked again with the same
    values"); ``None`` when a ``points`` slot carries a relative change ("a bit quieter" asked twice is a further
    change, never a correction). Slot types only, never the words."""
    if not isinstance(values, Mapping):
        return None
    for slot in skill.slots:
        if slot.type == "points" and values.get(slot.name) not in (None, ""):
            return None
    return dict(values)


def _ledger_start(ctx: RunContext, skill: Skill, values: Mapping[str, Any], filled: Mapping[str, str],
                  t0: float, app: str | None = None) -> str | None:
    from yuki.skills import registry

    key = registry.new_run_key()
    absolute = _absolute_values(skill, values)
    counted = ledger_skill(skill, values)   # a learned skill run with another key value is its capability's run
    got = _mem(ctx.memory, "skill_run_start",
               lambda c: registry.start_run(_store_of(c), skill=counted, origin=ctx.origin, t0=t0, run_key=key,
                                            request_id=ctx.request_id, session_id=ctx.session_id, filled=filled,
                                            app=app,
                                            slots_digest=registry.values_digest(_store_of(c), counted.key, absolute)),
               timeout_s=0.5)
    return got or (key if ctx.memory is not None else None)


def _note_false_done(resolved: Any, ctx: RunContext, *, corrects: bool,
                     again: bool) -> tuple[str, int | None] | None:
    """D19's other false-done source (review r2 M2), before the new run starts: the user asked the same skill again
    with the same absolute values, as a correction of its last run (``corrects``) or a repeat (not ``again``, in
    another request), within :data:`yuki.skills.registry.FALSE_DONE_S` of a run that ended "done": that run is marked
    a false done in the ledger (``recheck = 0``: the scorecard's false-done count and the rung's trust see it) and
    logged.

    fSkills review M1 (live 18 Oct: web.open_page's protocol rung disabled for 24 h by two marks on runs that had done
    what they were asked): only the same ask redone charges the run it follows, a correction too. A correction with
    other values ("the other playlist") says the value was wrong, never the rung that put it on (when the value was
    Yuki's own pick, :mod:`yuki.skills.taste` counts the quick change as a skip of that pick, its own feedback); and a
    resolve that dropped a value it was given (``dropped``: "open MKBHD's channel" with its page dropped reads as the
    "open YouTube" before it) can't tell a repeat from another ask, so neither marks anything. A question or a hold
    (no values) marks nothing either.
    Returns ``(the run key marked, its recheck before)`` (:func:`_withdraw_false_done`), else ``None``."""
    if ctx.memory is None or ctx.origin == "eval" or not isinstance(resolved, (Plan, Handoff)) or resolved.dropped:
        return None
    if again and not corrects:
        return None
    skill = resolved.skill
    if not isinstance(skill, Skill):
        return None
    absolute = _absolute_values(skill, resolved.values)
    if absolute is None:
        return None
    # the same ask is ledgered where its run would be (lCore review r2 L9): a learned skill asked with another key
    # value counts on its capability (:func:`ledger_skill`)
    key = ledger_skill(skill, absolute).key
    from yuki.skills import registry

    # a correction may follow a run of the learned skill or one its capability took (another key value): the newest
    # of the two is the run corrected (lCore review r2 L9)
    keys = [key]
    if corrects and isinstance(skill, Skill) and getattr(skill, "origin", "") == "learned" and skill.capability \
            and skill.capability != key:
        keys.append(str(skill.capability))

    def go(client: Any) -> Any:
        store = _store_of(client)
        target, before = key, None
        newest = -1.0
        for k in keys:
            rows = registry.last_runs(store, k, since=time.time() - registry.FALSE_DONE_S - 30.0, limit=1)
            at = float(rows[0].get("t0") or 0.0) if rows else -1.0
            if at > newest:
                target, newest, before = k, at, rows[0].get("recheck")
        digest = registry.values_digest(store, target, absolute)
        if not digest:
            return None
        # the same values as the run it follows, a correction or a repeat alike (the digest's own rule)
        marked = registry.false_done_after(store, target, digest=digest, request_id=ctx.request_id)
        return (str(marked), None if before is None else int(before)) if marked else None

    marked = _mem(ctx.memory, "skill_false_done", go, timeout_s=0.5)
    if not marked:
        return None
    _emit(ctx, "done_recheck", skill=key, result="false_done", recheck="user",
          reason="correction" if corrects else "repeat", origin=ctx.origin)
    return marked


def _withdraw_false_done(ctx: RunContext, noted: tuple[str, int | None] | None, result: SkillResult,
                         skill_key: str) -> None:
    """fSkills review M1: the run that followed a false-done mark (:func:`_note_false_done`) found the PC already
    showing what was asked (``already``, checked): the run marked had done it after all (the user asked again for
    another reason: "the other one", "from the start"), so its mark is taken back (its recheck as it was) and logged."""
    if not noted or result.outcome != "already" or result.verified is not True:
        return
    from yuki.skills import registry

    run_key, before = noted
    took = _mem(ctx.memory, "skill_false_done", lambda c: registry.set_recheck(
        _store_of(c), run_key, None if before is None else bool(before)), timeout_s=0.5)
    if took:
        _emit(ctx, "done_recheck", skill=skill_key, result="withdrawn", recheck="user", reason="already",
              origin=ctx.origin)


def _ledger_app(ctx: RunContext, run_key: str | None, app: str) -> None:
    if not run_key:
        return
    from yuki.skills import registry

    _mem(ctx.memory, "skill_run_app", lambda c: registry.set_run_app(_store_of(c), run_key, app), timeout_s=0.5)


def _first_app(skill: Skill, routes: list[Route], values: Mapping[str, Any]) -> str:
    """The app the plan's first rung acts on (D18: the run's row names it from t0)."""
    for route in routes:
        if route.via == "agent":
            break
        try:
            return acted_app(skill, route, fill_value(dict(route.args), values))
        except KeyError:
            continue
    return skill.app


def _game_blocks(route: Route, skill: Skill, ctx: RunContext) -> bool:
    """D17, per rung and read now (review M5): whether a game in front keeps this rung from running. A rung that takes
    focus or sends input, or whose data says ``never``, never runs with one; a ``states`` rung only while the app's
    window is in one of its proven states (read now; no reader: it doesn't run). An ``always`` rung that takes no
    focus reads nothing."""
    if not route.needs_focus and route.game == "always":
        return False
    if not ctx.game_now():
        return False
    if route.needs_focus or route.game != "states":
        return True
    return ctx.window_state_now(skill) not in route.game_states


def _ledger_finish(ctx: RunContext, run_key: str | None, **kw: Any) -> None:
    if not run_key:
        return
    from yuki.skills import registry

    _mem(ctx.memory, "skill_run_finish", lambda c: registry.finish_run(_store_of(c), run_key, **kw), timeout_s=0.5)


def _ledger_try(ctx: RunContext, run_key: str | None, route: Route, checked: bool | None, outcome: str,
                ms: float) -> None:
    if not run_key:
        return
    from yuki.skills import registry

    _mem(ctx.memory, "skill_route_try",
         lambda c: registry.record_route_try(_store_of(c), run_key, route, checked, outcome=outcome, ms=ms),
         timeout_s=0.5)


def _simple(skill_key: str, outcome: str, ctx: RunContext, started: float, **kw: Any) -> SkillResult:
    res = SkillResult(skill=skill_key, outcome=outcome, ms=round((ctx.clock() - started) * 1000.0, 1), **kw)
    _emit(ctx, "skill_run", skill=skill_key, result=outcome, ms=res.ms, origin=ctx.origin)
    return res


def _commit_gate(skill: Skill, ctx: RunContext, started: float) -> SkillResult | None:
    """D24 before anything runs, a plan or a hand-over to the general agent alike (review r1 L6): a step that is only
    the user's is never Yuki's; an offer (a card) never commits (sends and joins stay the user's, rule 8). Refused:
    nothing runs, nothing is written to the ledger. ``None``: it may run."""
    commit = run_commit(skill)
    if commit == "user" or (commit != "none" and ctx.origin == "card"):
        _emit(ctx, "skill_resolve", skill=skill.key, result="refused", reason=f"commit_{commit}")
        line = LINE_COMMIT_USER if commit == "user" else LINE_FAILED
        return _simple(skill.key, "refused", ctx, started, say=line,
                       evidence={"acted": False, "reason": f"commit_{commit}", "fallback_line": line})
    return None


def _run(resolved: Any, ctx: RunContext, started: float, state: dict[str, Any] | None = None) -> SkillResult:
    if isinstance(resolved, Ask):
        try:
            from yuki.skills.catalog import load

            forms = _code_slot_forms(load().get(str(resolved.skill or "")), getattr(resolved, "dropped", None))
        except Exception:  # noqa: BLE001
            forms = {}
        if forms and _first_form_slip(ctx, str(resolved.skill or ""), forms):
            # skill22 (A19; final-D k_spotify_louder: "louder" given for the volume's signed change was dropped and the
            # user was asked "Louder, quieter, or to what level?" after saying "louder"): the first such slip of a
            # request goes back to the caller to call once more in the stated form; nothing is asked or said
            _emit(ctx, "skill_resolve", skill=resolved.skill, result="asked", slot_source="ask", reason="form_recall",
                  dropped_slots=_slot_flags(list(forms)))
            return _simple(resolved.skill, "asked", ctx, started, ask="", options=[], say="",
                           evidence={"acted": False, "reason": "form", "form": forms, FORM_RECALL: True})
        _emit(ctx, "skill_resolve", skill=resolved.skill, result="asked", slot_source="ask",
              **_person_fields(getattr(resolved, "person", None)))
        asked_ev: dict[str, Any] = {"acted": False, "reason": "asked", "fallback_line": resolved.question}
        if forms:
            asked_ev["form"] = forms      # skill19 (RC5a): a dropped code-typed value is never dropped silently
        return _simple(resolved.skill, "asked", ctx, started, ask=resolved.question, options=list(resolved.options),
                       say=resolved.question, evidence=asked_ev)
    if isinstance(resolved, Held):
        _emit(ctx, "skill_resolve", skill=resolved.skill, result="held_game", gate="game")
        return _simple(resolved.skill, "held_game", ctx, started, held="game", say=resolved.say,
                       evidence={"acted": False, "reason": "game", "fallback_line": resolved.say})
    if isinstance(resolved, Refused):
        _emit(ctx, "skill_resolve", skill=resolved.skill, result="refused", reason=resolved.why)
        line = LINE_MEMORY_SLOW if resolved.why == "memory_slow" else LINE_FAILED
        return _simple(resolved.skill, "refused", ctx, started, say=line, why=str(resolved.why or ""),
                       evidence={"acted": False, "reason": str(resolved.why or "refused"), "fallback_line": line})
    state = state if state is not None else {}
    if isinstance(resolved, (Handoff, Plan)):
        refused = _commit_gate(resolved.skill, ctx, started)
        if refused is not None:
            return refused
    if isinstance(resolved, Handoff):
        # a dropped value handed over (x5Fallback) carries only what the caller gave: its send condition errs to "sends"
        return _handoff(resolved.skill, resolved.values, resolved.filled, [], ctx, started, why=resolved.why,
                        run_key=None, partial=resolved.why == WHY_DROPPED)
    if not isinstance(resolved, Plan):
        return _simple(str(resolved or ""), "refused", ctx, started, say=LINE_FAILED)
    plan = resolved
    skill = plan.skill
    if plan.handoff and skill.handoff:
        earlier = _recent_handoff(skill, plan.values, ctx)
        if earlier is not None:
            # x5Fallback: the same thing ran moments ago, acted and couldn't be confirmed (or broke partway): no rung
            # runs again (it could do it twice: a second tab, play pressed on a playing row); rung C at once, with
            # what was tried then, and the thinker looks first
            why, before, missing, seen = earlier
            _emit(ctx, "skill_resolve", skill=skill.key, result="handed_over", reason="repeat", origin=ctx.origin)
            return _handoff(skill, plan.values, plan.filled, before, ctx, started, why=why, run_key=None,
                            routes=list(plan.routes), unconfirmed=True, missing=missing, seen=seen, repeat=True)
    # integ29 (LD1, live row m21_twinkle_means): a means given in another form than it takes (display's "Twinkle Tray"
    # for windows | app) was dropped by the resolver, and a route for "no means chosen" would take that for none and
    # swap their told means for another silently. Before any rung: the first slip of a request goes back to the caller
    # to call once more in the stated form (A19), else the slot's own question. Code types and the slot's role only.
    means_forms = {k: v for k, v in _code_slot_forms(skill, plan.dropped).items()
                   if getattr(skill.slot(k), "role", "") == "means"}
    if means_forms:
        if _first_form_slip(ctx, skill.key, means_forms):
            _emit(ctx, "skill_resolve", skill=skill.key, result="asked", slot_source="ask", reason="form_recall",
                  dropped_slots=_slot_flags(list(means_forms)))
            return _simple(skill.key, "asked", ctx, started, ask="", options=[], say="",
                           evidence={"acted": False, "reason": "form", "form": means_forms, FORM_RECALL: True})
        means_slot = skill.slot(next(iter(means_forms)))
        question = str(getattr(means_slot, "ask", "") or "") or f"Which {means_slot.name}?"
        state["asked_slot"] = means_slot.name      # fd (D4): the means ask, as a rung's
        _emit(ctx, "skill_resolve", skill=skill.key, result="asked", slot_source="ask", reason="form",
              dropped_slots=_slot_flags(list(means_forms)))
        return _simple(skill.key, "asked", ctx, started, ask=question, options=list(means_slot.options or ()),
                       say=question, evidence={"acted": False, "reason": "asked", "fallback_line": question,
                                               "form": means_forms})
    t0 = time.time()
    run_app = _first_app(skill, plan.routes, plan.values)
    run_key = _ledger_start(ctx, skill, plan.values, plan.filled, t0, app=run_app)
    sources = dict(plan.sources)
    guessed = list(plan.guessed)
    # Phase 2.1: where each given value came from (code words: words / heard / menu / card / screen / reading ...) and
    # which slots are Yuki's guess, by their schema names only
    # skill19 (RC2): a rung its own record put last (``reordered``) or rests for now (``rested``: skipped "couldnt")
    planned = [r.key for r in plan.routes]
    in_file = [r.key for r in skill.routes if r.key in set(planned)]
    rested = sum(1 for why in (plan.skipped or {}).values() if why == "couldnt")
    _emit(ctx, "skill_resolve", skill=skill.key, result="plan", routes=len(plan.routes),
          slot_source=sorted(set(plan.filled.values())), slots=len(plan.filled), named=len(plan.named),
          dropped=len(plan.dropped), dropped_slots=_slot_flags(plan.dropped), skipped=len(plan.skipped),
          source=sorted(set(sources.values())) or None, guessed_slots=_slot_flags(guessed), origin=ctx.origin,
          reordered=True if planned != in_file else None, rested=rested or None,
          **_measured_fields(getattr(plan, "measured", None)),
          **_names_fields(getattr(plan, "names_how", None), getattr(plan, "names_miss", None)),
          **_person_fields(getattr(plan, "person", None)))
    deadline = started + float(ctx.budget_s)
    tried: list[dict[str, Any]] = []
    measure = _Measure(ctx, skill, plan.values)
    state.update(run_key=run_key, tried=tried, acted=False, measure=measure)
    focus_taken = False
    acted = False
    sends = sends_with(skill, plan.values)
    guesses = [{"slot": k, "value": shown_value(skill, k, plan.values.get(k)), "from": sources.get(k, "")}
               for k in guessed]
    last_wait: Any = None     # the last done-check read (Phase 2.3's evidence when every rung failed)
    last_route: Route | None = None
    # skill19 (contract C3): a rung that opened the chat but typed nothing (the chat driver's code fact): what holds
    # the keyboard (content-free) and the driver's own line, for the caller who takes it on
    not_typed: dict[str, Any] | None = None
    # skill19 (RC5a / C5): a value given for a code-typed slot that wasn't a form it takes, and that form
    forms = _code_slot_forms(skill, plan.dropped)

    def evidence(*, route: Route | None = None, waited: Any = None, reason: str = "", line: str = "") -> dict[str, Any]:
        ev = _evidence(skill, plan.values, tried, acted=acted or measure.touched, route=route, waited=waited,
                       missing=missing, seen=seen, sources=sources, reason=reason, line=line)
        if forms:
            ev["form"] = dict(forms)
        if not_typed is not None:
            ev["not_typed"] = True
            if not_typed.get("holder"):
                ev["holder"] = not_typed["holder"]
            if not_typed.get("chat_open"):
                who = _person_value(skill, plan.values)
                ev["chat_open"] = who or True      # fd (D5): the person's own chat, confirmed open
            if not_typed.get("held"):
                ev["typing_held"] = True           # fd (D5): the step check held the typing (not the user's words)
        return ev

    # IF-seed: the slot a rung found no item for on screen, and the names it read there (RAM only)
    missing: str | None = None
    seen: list[str] = []
    for route in plan.routes:
        if route.via == "agent":
            break
        if _game_blocks(route, skill, ctx):
            # D17(1): the route's game rule applied again with a fresh read right before every rung (a game may have
            # come to the front during an earlier rung's wait): never focus, input or an unproven state with one
            tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": "game"})
            _emit(ctx, "route_try", skill=skill.key, route=route.key, result="held_game", gate="game")
            continue
        try:
            args = fill_value(dict(route.args), plan.values)
        except KeyError:
            tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": "no_value"})
            continue
        if not_typed is not None and _app_key_of(acted_app(skill, route, args)) == not_typed.get("app", ""):
            # review M2 (skill19): after "opened, nothing typed" something of that app that is no text field holds its
            # keyboard (a dialog left open), so no later rung acts in the same app (its shortcut's effect there is
            # unknowable: Ctrl+K could close the dialog and type the chat's name into the open chat's composer); the
            # caller looks first (rung C below)
            tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": "after_not_typed"})
            _emit(ctx, "route_try", skill=skill.key, route=route.key, result="skipped", reason="after_not_typed")
            continue
        if route.needs_focus:
            if ctx.on_focus is not None:
                try:
                    ctx.on_focus()
                except Exception:  # noqa: BLE001
                    pass
            focus_taken = True
        app_now = acted_app(skill, route, args)
        if app_now != run_app:
            # D18: the row names the app this rung acts on before it acts (a moment it causes is matched live)
            _ledger_app(ctx, run_key, app_now)
            run_app = app_now
        fn = ctx.verbs.get(route.via) or VERB_FUNCS.get(route.via)
        call = RouteCall(skill, route, args, dict(plan.values), ctx, deadline, dict(plan.filled))
        base = _baseline(route, plan.values, ctx)
        front0 = measure.read()        # skill19 (C1): the front right before this rung
        r0 = ctx.clock()
        try:
            res = fn(call) if fn is not None else RouteResult(ran=False, outcome="no_verb")
        except Exception as exc:  # noqa: BLE001
            res = RouteResult(ran=None, outcome="error", facts={"error_kind": type(exc).__name__})
        route_ms = round((ctx.clock() - r0) * 1000.0, 1)
        app_done = acted_app(skill, route, args, res)
        item = res.facts_ram.get(ITEM_KEY) if isinstance(res.facts_ram, Mapping) else None
        if isinstance(item, Mapping) and (res.ran is not False or res.already):
            # fd (D3): what this rung acted on or found already so, by its own word (RAM): the run's trace
            state.setdefault("items", {}).update({str(k): " ".join(str(v).split()) for k, v in item.items()
                                                  if skill.slot(str(k)) is not None and str(v or "").strip()})
        # skill19 (C1; all-desk RC1(b), one-desk D5): what this rung measurably did to the front, whatever it declared
        # (``needs_focus``) and whatever it said it did (a ``ran=False`` that raised a window still touched the PC)
        change = measure.fold(route, front0, measure.read(), acted=res.ran is not False, app=app_done)
        front_w = _front_word(change)
        # IF-seed (x5Fallback): a rung that found no item by the name asked says which slot, and what it read instead
        missing = missing or _missing_slot(skill, (res.facts or {}).get("missing")
                                           if isinstance(res.facts, Mapping) else None)
        if isinstance(res.facts_ram, Mapping) and res.facts_ram.get("seen"):
            seen = _seen_names([*seen, *(res.facts_ram.get("seen") or [])])
        if res.held == HELD_BLOCKED:
            # fHands: something other than a game stopped this rung's step (the gate failed closed, another guard):
            # a settled stop said plainly, never the game line, never the next rung (it would meet the same stop),
            # never handed over (the thinker too). The ledger keeps it as a refusal (never counted against the rung)
            acted = state["acted"] = acted or bool(res.ran)
            tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": HELD_BLOCKED})
            _emit(ctx, "route_try", front=front_w, skill=skill.key, route=route.key, result=HELD_BLOCKED,
                  ms=route_ms)
            _ledger_finish(ctx, run_key, outcome="refused", checked=None, route=route, ms=route_ms, app=app_done,
                           acted=acted)
            line = LINE_BLOCKED_PARTWAY if acted else LINE_BLOCKED
            return _simple(skill.key, HELD_BLOCKED, ctx, started, say=line, tried=tried, run_key=run_key,
                           route=route.key, focus_taken=focus_taken, sends=sends,
                           evidence=evidence(reason=HELD_BLOCKED, line=line))
        if res.held:
            tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": "held_game"})
            _emit(ctx, "route_try", front=front_w, skill=skill.key, route=route.key, result="held_game",
                  ms=route_ms, observed=res.facts)   # integ28 fix F2: the stop end's own facts (content-free)
            if res.ran:
                acted = state["acted"] = True
                if sends or not route.retry_after_acting:
                    # it acted before the game check refused the rest: nothing else is tried (never twice)
                    _ledger_finish(ctx, run_key, outcome="held_game", checked=None, route=route, ms=route_ms,
                                   app=app_done, acted=True)
                    return _simple(skill.key, "held_game", ctx, started, held="game", say=HELD_LINE, tried=tried,
                                   run_key=run_key, route=route.key, evidence=evidence(reason="game", line=HELD_LINE))
            continue
        if res.outcome == TO_PLANNER:
            # bSkill (bTalk merge note 4): the consent rule stopped a step of this rung that would send or join, with
            # no plan behind it, before that step ran: never "exactly this?" and never the next rung (it would reach
            # the same step); rung C, the plan seed, where the planner's consent rule and send checks decide
            acted = acted or bool(res.ran)
            tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": TO_PLANNER,
                          **_switch_tried(res)})   # integ28 (gb merge note 6, K-S)
            _ledger_try(ctx, run_key, route, None, "refused", route_ms)
            _emit(ctx, "route_try", front=front_w, skill=skill.key, route=route.key, result=TO_PLANNER,
                  ms=route_ms, observed=res.facts)   # integ28 fix F2: K-S switcher_held / B3 stopped reach the log
            return _to_planner(skill, plan, tried, ctx, started, run_key=run_key, acted=acted, missing=missing,
                               seen=seen, evidence=evidence(route=route, reason=TO_PLANNER))
        if _not_typed(res):
            # skill19 (contract C3; live 19 Oct all-messaging RC1: "I opened your chat with Hemanth, but couldn't type
            # it." asked as a final question on every Discord draft, so the talker's own hands never typed it as on 18
            # Oct): the chat driver's code fact says no typing went out, so nothing can have been sent and nothing
            # would be done twice. Never a turn-ending question: a later rung in another app may try (none that acts in
            # this app: review M2, the loop's top), and with none left the run is handed to the caller unconfirmed
            # (rung C: the thinker or the agent looks, then types it in the chat now open). Its try is "couldn't
            # confirm" (never counted against the rung); the chat it opened is the PC touched
            acted = state["acted"] = acted or bool(res.ran)
            facts_nt = res.facts if isinstance(res.facts, Mapping) else {}
            holder = facts_nt.get("holder")
            # fd (final-F D5): the driver's own facts on why: the chat it opened is confirmed open (``chat``), and the
            # step check held the typing before it ran (``refused_steps``: its text was not in the request's words)
            not_typed = {"holder": str(holder)[:60] if isinstance(holder, str) and holder.strip() else None,
                         "line": res.unverified_line or res.say or res.ask or "",
                         "app": _app_key_of(app_done), "chat_open": facts_nt.get("chat") == "open",
                         "held": facts_nt.get("refused_steps") is True}
            tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": "not_typed",
                          **_switch_tried(res)})   # integ28 (gb merge note 6, K-S)
            last_route = route
            _ledger_try(ctx, run_key, route, None, "unverified", route_ms)
            _emit(ctx, "route_try", front=front_w, skill=skill.key, route=route.key, result="not_typed",
                  ms=route_ms, observed=res.facts)
            if not route.retry_after_acting:
                break        # its data says nothing may follow it once it acted: the caller takes it on (rung C)
            continue
        if res.stop:
            # morning21 (A2): a code-word stop (a precondition the rung met: a browser that wouldn't open, a strip
            # it couldn't read, no tab of the site), said plainly: not done, never "Ask them:" (21 Oct: "Ask them: Zen
            # isn't open" made the planner drop the meeting part and the talker offer another browser), never the
            # next rung, never counted against this one
            acted = state["acted"] = acted or bool(res.ran)
            line = " ".join(str(res.say or res.ask or "").split()) or LINE_FAILED
            needs = _app_key_of(res.app) if res.app else ""
            tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": res.outcome or "stop"})
            _ledger_finish(ctx, run_key, outcome="refused", checked=None, route=route, ms=route_ms, app=app_done,
                           acted=acted)
            _emit(ctx, "route_try", front=front_w, skill=skill.key, route=route.key, result=res.outcome or "stop",
                  ms=route_ms, observed=res.facts)
            return _simple(skill.key, NOT_DONE, ctx, started, say=line, run_key=run_key, route=route.key, tried=tried,
                           why=str(res.outcome or "stop")[:40], needs_app=needs, focus_taken=focus_taken,
                           evidence=evidence(route=route, reason=res.outcome or "stop", line=line))
        if res.ask:
            asked_slot = (res.facts or {}).get("slot") if isinstance(res.facts, Mapping) else None
            if isinstance(asked_slot, str) and skill.slot(asked_slot) is not None:
                state["asked_slot"] = asked_slot      # fd (D4): the slot this rung's question is about (the means ask)
            # integ29 (mb merge note 12; review M2, m21_bright_both_up_back): the rung's question may carry, in RAM, the
            # forms of the slots it asks about (``facts_ram["form"]``: the display driver's "which monitor" when a
            # target names none). The first such slip of a request that touched nothing goes back to a caller that
            # calls again, as the resolver's A19 recall does; else the question's report names the forms
            ram = res.facts_ram if isinstance(res.facts_ram, Mapping) else {}
            forms_ram = ram.get("form")
            # integ29 (LD2): its own name, so the plan's dropped forms the evidence() closure reads stay bound
            ram_forms: dict[str, str] = {}
            if isinstance(forms_ram, Mapping) and forms_ram and all(skill.slot(str(k)) is not None for k in forms_ram):
                ram_forms = {str(k): str(v) for k, v in forms_ram.items()}
            _ledger_finish(ctx, run_key, outcome="asked", checked=None, route=route, ms=route_ms, app=app_done,
                           acted=acted)
            if ram_forms and not acted and _first_form_slip(ctx, skill.key, ram_forms):
                _emit(ctx, "route_try", front=front_w, skill=skill.key, route=route.key, result="asked",
                      reason="form_recall", ms=route_ms, observed=res.facts)
                return _simple(skill.key, "asked", ctx, started, ask="", options=[], say="", run_key=run_key,
                               route=route.key, tried=tried,
                               evidence={"acted": False, "reason": "form", "form": ram_forms, FORM_RECALL: True})
            _emit(ctx, "route_try", front=front_w, skill=skill.key, route=route.key, result="asked", ms=route_ms,
                  observed=res.facts)   # integ28 fix F2: held_by_check / taken_over / act_off ends' facts
            asked_ev = evidence(route=route, reason=res.outcome or "asked", line=res.ask)   # the plan's dropped forms
            if ram_forms:
                asked_ev["form"] = {**(asked_ev.get("form") or {}), **ram_forms}
            current = (res.facts or {}).get("current") if isinstance(res.facts, Mapping) else None
            if isinstance(current, (int, float)) and not isinstance(current, bool):
                asked_ev["current"] = float(current)    # skill19 (O5): the control's level as the rung read it
            return _simple(skill.key, "asked", ctx, started, ask=res.ask, options=list(res.options), say=res.ask,
                           run_key=run_key, route=route.key, tried=tried, evidence=asked_ev)
        if res.outcome == "other_device":
            say = res.say or LINE_OTHER_DEVICE.format(app=_app_title(skill.app))
            _ledger_finish(ctx, run_key, outcome="other_device", checked=None, route=route, ms=route_ms,
                           app=app_done, acted=acted)
            _emit(ctx, "route_try", front=front_w, skill=skill.key, route=route.key, result="other_device",
                  ms=route_ms)
            return _simple(skill.key, "other_device", ctx, started, say=say, run_key=run_key, route=route.key,
                           tried=tried, evidence=evidence(route=route, reason="other_device", line=say))
        probe_ctx = _probe_ctx(ctx, t0, plan.values, res, base)
        if res.already:
            # a true no-op: the end state was already there; still checked before it is said (never a fresh "done")
            waited = _wait(route.done, probe_ctx, deadline - ctx.clock(), ctx)
            if waited.verdict is True:
                line = res.say or LINE_ALREADY
                _ledger_finish(ctx, run_key, outcome="already", checked=None, route=route, ms=route_ms,
                               evidence={"numbers": _numbers(plan.values)}, app=app_done, acted=acted)
                _emit(ctx, "done_check", skill=skill.key, route=route.key, result="already", ms=waited.ms)
                return _simple(skill.key, "already", ctx, started, verified=True, done_line=line, say=line,
                               run_key=run_key, route=route.key, tried=tried, values=dict(plan.values))
        if res.ran is False:
            tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": res.outcome or "no",
                          **_switch_tried(res)})   # integ28 (gb merge note 6, K-S)
            if isinstance(res.facts, Mapping) and res.facts.get(UNREAD) is True:
                tried[-1][UNREAD] = True      # skill20 (C10): it never read what it needed; nothing was pressed there
            unsure = isinstance(res.facts, Mapping) and res.facts.get("not_run") == NOT_RUN_UNSURE
            _ledger_try(ctx, run_key, route, None, "unverified" if unsure else "failed", route_ms)
            _emit(ctx, "route_try", front=front_w, skill=skill.key, route=route.key,
                  result=res.outcome or "not_run", ms=route_ms, observed=res.facts)
            continue
        acted = state["acted"] = True
        # review r2 M3: once a rung acted, its check gets its own within_s (<= MAX_WAIT_S) even past the request
        # budget: a check cut short before the app could react is no contradiction (a False there would count against
        # a working rung and run the next one, doing a relative change twice)
        waited = _wait(route.done, probe_ctx, None, ctx)
        _emit(ctx, "route_try", front=front_w, skill=skill.key, route=route.key, result=res.outcome or "ran",
              ms=route_ms, observed=res.facts)
        _emit(ctx, "done_check", skill=skill.key, route=route.key,
              check=(route.done.probe if route.done else "read_back"),
              result={True: "done", False: "contradicted", None: "unverified"}[waited.verdict], ms=waited.ms,
              reads=waited.reads, observed=waited.evidence)
        if waited.verdict is None and res.ran is True and not sends:
            second = _second_source(route, probe_ctx, waited, ctx, skill)
            if second is not None:
                waited = second
        if waited.verdict is True:
            # Phase 2.1/2.2: a guessed value is named (one word corrects it); a pick the caller made from the
            # candidates says its why instead
            line = done_line(skill, plan.values, plan.named, route=route, say=res.say, guessed=guessed, why=ctx.why,
                             picked=[k for k in guessed if sources.get(k) in (FROM_MENU, FROM_CARD)])
            # kHands brief item 6: every rung tried is in skill_route_tries, the last one too (the counters leave a
            # run's last rung out of that table: registry.TRY_NOT_LAST)
            _ledger_try(ctx, run_key, route, True, "done", route_ms)
            _ledger_finish(ctx, run_key, outcome="done", checked=True, route=route, ms=route_ms,
                           value=res.value, entity_digest=res.entity_digest, app=app_done,
                           evidence={"numbers": _numbers(plan.values), "check": waited.evidence})
            result = SkillResult(skill=skill.key, outcome="done", verified=True, done_line=line, say=line,
                                 route=route.key, tried=tried, ask_keep=plan.ask_keep, run_key=run_key,
                                 values=dict(plan.values), focus_taken=focus_taken, guessed=guesses)
            try:
                _after_done(ctx, skill, route, plan, probe_ctx, result)
            except Exception:  # noqa: BLE001 - the usual's question and the second probe are extras: done stays done
                pass
            result.ms = round((ctx.clock() - started) * 1000.0, 1)
            _emit(ctx, "skill_run", skill=skill.key, route=route.key, result="done", ms=result.ms,
                  origin=ctx.origin, rung=route.rung)
            return result
        if waited.verdict is None:
            # it acted and the PC couldn't confirm: never counted against it, and no second rung (it would do it twice)
            if (not sends and route.retry_after_acting and plan.handoff and skill.handoff
                    and ctx.origin in HANDOFF_ORIGINS and _absolute_values(skill, plan.values) is not None
                    and not _settled(res, waited.evidence)):
                # x5Fallback (failure audit 10 Oct, C2): rung C with the seed, never a stop at "couldn't confirm": the
                # thinker looks at the screen and finishes it by steps. A sending skill (rule 8), a rung whose data
                # says nothing may follow it, a relative change (done again it would change twice: "a bit quieter"),
                # a no-op or a result that took elsewhere (:func:`_settled`), a card and an eval still stop below
                tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": "unverified"})
                # review r1 L1: no try row for this rung: the run's own row names it (``ledger`` below), never counted
                # twice
                return _handoff(skill, plan.values, plan.filled, tried, ctx, started, why=WHY_UNVERIFIED,
                                run_key=run_key, acted=True, routes=list(plan.routes), unconfirmed=True,
                                missing=missing, seen=seen, focus_taken=focus_taken,
                                ledger={"route": route, "ms": route_ms, "value": res.value, "app": app_done,
                                        "evidence": {"numbers": _numbers(plan.values), "check": waited.evidence}},
                                evidence=evidence(route=route, waited=waited, reason=WHY_UNVERIFIED),
                                again=_nothing_out(res))   # fix-r31 (A2): nothing went out: it may run once more
            say = res.unverified_line or LINE_UNVERIFIED
            _ledger_try(ctx, run_key, route, None, "unverified", route_ms)
            _ledger_finish(ctx, run_key, outcome="unverified", checked=None, route=route, ms=route_ms,
                           value=res.value, app=app_done,
                           evidence={"numbers": _numbers(plan.values), "check": waited.evidence})
            return _simple(skill.key, "unverified", ctx, started, verified=None, say=say, route=route.key,
                           tried=tried, run_key=run_key, values=dict(plan.values), focus_taken=focus_taken,
                           sends=sends, sent_key=_sent_key(res) if sends else None, guessed=guesses,
                           evidence=evidence(route=route, waited=waited,
                                             reason="settled" if _settled(res, waited.evidence) else "", line=say))
        tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": "contradicted"})
        last_wait, last_route = waited, route
        if sends or not route.retry_after_acting:
            # review M3: a rung that acted on a skill that sends (or one whose data says so) is never followed by
            # another acting rung or a hand-off: the message may have gone anyway, and a second try would send twice
            _ledger_try(ctx, run_key, route, False, "failed", route_ms)
            _ledger_finish(ctx, run_key, outcome="failed", checked=False, route=route, ms=route_ms, app=app_done,
                           evidence={"numbers": _numbers(plan.values), "check": waited.evidence})
            line = LINE_SEND_FAILED if sends else LINE_FAILED
            return _simple(skill.key, "failed", ctx, started, say=line, route=route.key, tried=tried,
                           run_key=run_key, values=dict(plan.values), focus_taken=focus_taken, sends=sends,
                           guessed=guesses, evidence=evidence(route=route, waited=waited, reason="contradicted",
                                                              line=line))
        _ledger_try(ctx, run_key, route, False, "failed", route_ms)
    # every rung failed or could not run
    game = ctx.game_now()
    if game:
        _ledger_finish(ctx, run_key, outcome="held_game", checked=None, route=None, acted=acted)
        return _simple(skill.key, "held_game", ctx, started, held="game", say=HELD_LINE, tried=tried,
                       run_key=run_key, evidence=evidence(reason="game", line=HELD_LINE))
    if not_typed is not None:
        # skill19 (contract C3): a chat opened and nothing typed, and no later rung got there: an unconfirmed result the
        # caller takes on (the talker's or the thinker's rung C, the agent's own hands), never a question that ends
        # the turn. Nothing went out, so a send's consent is still the caller's to ask (the seed says it sends)
        line = " ".join(str(not_typed.get("line") or "").split()) or LINE_UNVERIFIED
        if plan.handoff and skill.handoff and ctx.origin in HANDOFF_ORIGINS:
            return _handoff(skill, plan.values, plan.filled, tried, ctx, started, why=WHY_UNVERIFIED,
                            run_key=run_key, acted=acted, routes=list(plan.routes), unconfirmed=True,
                            missing=missing, seen=seen,
                            evidence=evidence(route=last_route, reason="not_typed", line=line))
        _ledger_finish(ctx, run_key, outcome="unverified", checked=None, route=last_route, acted=acted)
        # skill20 review: the driver's code fact says nothing was typed, and any later rung that acted on a send
        # returned above, so no send key went out (never "it may have sent already")
        return _simple(skill.key, "unverified", ctx, started, verified=None, say=line, tried=tried, run_key=run_key,
                       route=last_route.key if last_route is not None else None, values=dict(plan.values),
                       sends=sends, sent_key=False if sends else None, guessed=guesses,
                       evidence=evidence(route=last_route, reason="not_typed", line=line))
    if plan.handoff and skill.handoff and ctx.origin != "eval":
        return _handoff(skill, plan.values, plan.filled, tried, ctx, started, why="routes_failed", run_key=run_key,
                        acted=acted, routes=list(plan.routes), missing=missing, seen=seen,
                        evidence=evidence(route=last_route, waited=last_wait, reason="routes_failed"))
    # the run's own row names no rung: each rung's verdict is already in its try row (never counted twice)
    _ledger_finish(ctx, run_key, outcome="failed", checked=False if acted else None, route=None)
    return _simple(skill.key, "failed", ctx, started, say=LINE_FAILED, tried=tried, run_key=run_key,
                   guessed=guesses, evidence=evidence(route=last_route, waited=last_wait, reason="routes_failed",
                                                      line=LINE_FAILED))


def _baseline(route: Route, values: Mapping[str, Any], ctx: RunContext) -> dict[str, Any]:
    """The onset probes' states right before this rung acts (review M4: a microphone already held is not this run's
    doing). Reads nothing unless the rung's checks include such a probe."""
    from yuki.skills.donecheck import take_baseline

    try:
        return take_baseline([route.done, route.recheck], values, readers=ctx.baselines)
    except Exception:  # noqa: BLE001 - no baseline: an onset can't be shown (None, never a false done)
        return {}


def _probe_ctx(ctx: RunContext, t0: float, values: Mapping[str, Any], res: RouteResult,
               baseline: Mapping[str, Any] | None = None) -> Any:
    from yuki.skills.donecheck import ProbeContext

    def memory_call(what: str, fn: Callable[[Any], Any]) -> Any:
        return _mem(ctx.memory, what, fn, timeout_s=0.8)

    return ProbeContext(since=t0, values=dict(values), result=res,
                        memory_call=memory_call if ctx.memory is not None else None, probes=dict(ctx.probes),
                        clock=ctx.clock, sleep=ctx.sleep, baseline=dict(baseline or {}))


def _wait(check: Check | None, probe_ctx: Any, left_s: float | None, ctx: RunContext) -> Any:
    """A rung's done-check, read until True or its own ``within_s`` (<= ``MAX_WAIT_S``) is up; ``left_s``: also no
    longer than the request's time left (``None``: the check's own time only, for a rung that acted)."""
    from yuki.skills.donecheck import wait

    return wait(check or Check(probe="read_back", within_s=0.0), probe_ctx,
                deadline_s=None if left_s is None else max(0.0, left_s))


def _sound_up(evidence: Any, depth: int = 0) -> bool:
    """Whether a check's evidence read the app sounding on this PC (``audio`` true, at any depth of an ``all`` check's
    parts). Code facts only."""
    if not isinstance(evidence, Mapping) or depth > 3:
        return False
    if evidence.get("audio") is True:
        return True
    return any(_sound_up(v, depth + 1) for v in evidence.values() if isinstance(v, Mapping))


def _second_source(route: Route, probe_ctx: Any, waited: Any, ctx: RunContext, skill: Skill) -> Any:
    """fd (final-F D2, one-brain M1; live 20 Oct: 7 of ONE's 9 closing holds followed a playlist press whose own check
    couldn't read the row while the playlist played, and two of those rounds took the front from the video the user
    watched): a rung that acted, whose check ended unconfirmed while the app sounded here (:func:`_sound_up`), gets its
    skill's own second source read once (its ``recheck`` when that is no ``same_source`` read), after
    :data:`UNVERIFIED_RECHECK_S`, before the result goes back to anyone. Its True is the run's done (code's done line,
    code's tidy step, never a hand-off or an agent round); anything else leaves the result as it was (never a
    contradiction from it). Logged ``done_recheck`` (``reason`` unverified). ``None``: nothing to read, or not done."""
    recheck = route.recheck
    if recheck is None or recheck.same_source or ctx.recheck == "off" or not _sound_up(waited.evidence):
        return None
    try:
        from yuki.skills.donecheck import Waited, recheck as do_recheck

        got = do_recheck(recheck, probe_ctx, after_s=min(float(recheck.after_s), UNVERIFIED_RECHECK_S))
    except Exception:  # noqa: BLE001 - the first verdict stands
        return None
    _emit(ctx, "done_recheck", skill=skill.key, route=route.key, check=recheck.probe,
          result={True: "agrees", False: "not_so", None: "unverified"}.get(got.verdict, "unverified"), ms=got.ms,
          recheck="second_source", reason="unverified")
    if got.verdict is not True:
        return None
    ev = dict(got.evidence) if isinstance(got.evidence, Mapping) else {}
    ev["second_source"] = True
    return Waited(True, ev, int(waited.reads or 0) + int(got.reads or 0), round(float(waited.ms or 0.0)
                                                                               + float(got.ms or 0.0), 1))


def _after_done(ctx: RunContext, skill: Skill, route: Route, plan: Plan, probe_ctx: Any, result: SkillResult) -> None:
    """After a "done": the user's words as a proposed usual (S4.2: one "Keep this as your usual?"), then D19's second
    probe."""
    if plan.ask_keep and ctx.memory is not None:
        from yuki.skills import prefs

        keep = dict(plan.ask_keep)
        quote = " ".join(ctx.words)[:300] or None
        _mem(ctx.memory, "skill_pref_said",
             lambda c: prefs.record(_store_of(c), str(keep["pref"]), str(keep["option"]), "said", quote=quote,
                                    source=f"skill_run:{result.run_key or ''}"), timeout_s=0.5)
    if route.recheck is None or ctx.recheck == "off":
        return
    recheck = route.recheck
    # skill20 (final-D B2): when the verdict was given, for a second source that must have read after it
    ram = getattr(getattr(probe_ctx, "result", None), "facts_ram", None)
    if isinstance(ram, dict):
        ram[DONE_AT] = time.time()

    def job() -> None:
        from yuki.skills import registry
        from yuki.skills.donecheck import recheck as do_recheck

        got = do_recheck(recheck, probe_ctx)
        result.recheck_verdict = got.verdict
        if result.run_key:
            _mem(ctx.memory, "skill_recheck", lambda c: registry.set_recheck(_store_of(c), result.run_key,
                                                                              got.verdict), timeout_s=0.5)
        _emit(ctx, "done_recheck", skill=skill.key, route=route.key, check=recheck.probe,
              result={True: "agrees", False: "false_done", None: "unverified"}[got.verdict], ms=got.ms,
              recheck=("same_source" if recheck.same_source else "second_source"))

    if ctx.recheck == "inline":
        job()
        return
    thread = threading.Thread(target=job, name="yuki-skill-recheck", daemon=True)
    thread.start()
    result.recheck_thread = thread


def _slot_flags(names: Any) -> dict[str, bool] | None:
    """Slot names for a content-free log line (``{"person": true}``): schema names from the connection files only
    (plain lower-case identifiers; anything else is left out), never their values. ``None`` when there are none."""
    flags = {str(n): True for n in names or () if isinstance(n, str) and _SLOT_NAME.fullmatch(n)}
    return flags or None


def _measured_fields(measured: Any) -> dict[str, Any]:
    """names21 (merge note 1): the plan's names measure (:attr:`yuki.skills.resolve.Plan.measured`) for the content-free
    ``skill_resolve`` line: the slots it took (``names_slots``, schema names only), how many by the value given
    (``names_by_value``; kw3 C5: the only way left, the whole-turn and agreement readings are gone), how many are sites
    (``names_sites``), the lowest score and margin (``names_score``, ``names_margin``). Never a value or a name. ``{}``
    when nothing was measured; never raises."""
    try:
        from yuki.skills.names import SITE
        from yuki.skills.resolve import HOW_VALUE

        got = {k: v for k, v in dict(measured or {}).items() if isinstance(v, Mapping)}
        if not got:
            return {}
        hows = [str(v.get("how") or "") for v in got.values()]
        scores = [float(v["score"]) for v in got.values() if isinstance(v.get("score"), (int, float))]
        margins = [float(v["margin"]) for v in got.values() if isinstance(v.get("margin"), (int, float))]
        return {"names_slots": _slot_flags(list(got)), "names_by_value": hows.count(HOW_VALUE) or None,
                "names_sites": sum(1 for v in got.values() if v.get("kind") == SITE) or None,
                "names_score": round(min(scores), 3) if scores else None,
                "names_margin": round(min(margins), 3) if margins else None}
    except Exception:  # noqa: BLE001 - a log field never breaks a run
        return {}


def _names_fields(how: Any, miss: Any) -> dict[str, Any]:
    """gd D5 (final-G2 youtube_channel_deji_arc: why "Deezy" never became their Deji was unmeasured): the names rules'
    code words for the content-free ``skill_resolve`` line (:attr:`yuki.skills.resolve.Plan.names_how` /
    ``names_miss``), as flags the log keeps (``{"caller": true}``, as ``guessed_slots``): ``names_how`` how a given name
    was resolved (``value``, ``caller``, ``listed``, ``heard``, ``kind``), ``names_miss`` why one stayed as given
    (``resolve.NAMES_MISS_WORDS``) and ``names_miss_slots`` which slots (schema names only). Never a value or a name;
    ``{}`` for none; never raises."""
    try:
        out: dict[str, Any] = {}
        hows = _slot_flags(sorted({str(v) for v in dict(how or {}).values()}))
        if hows:
            out["names_how"] = hows
        misses = {str(k): str(v) for k, v in dict(miss or {}).items()}
        words = _slot_flags(sorted(set(misses.values())))
        if words:
            out["names_miss"] = words
            out["names_miss_slots"] = _slot_flags(sorted(misses))
        return out
    except Exception:  # noqa: BLE001 - a log field never breaks a run
        return {}


def _person_fields(person: Any) -> dict[str, Any]:
    """gd D6 (the R13 trigger lead: the right person resolved, yet the chat driver read the window's thread as outside
    their threads): a person slot's resolution for the content-free ``skill_resolve`` line
    (:attr:`yuki.skills.resolve.Plan.person`), one nested record the log keeps as is: ``source`` and ``how`` (code
    words, the log's enum fields), ``people`` (the people the name names), ``dms`` (their direct-message routes) and
    ``threads`` (those routes' thread ids), counts only. Never a name; ``{}`` for none; never raises."""
    try:
        got = dict(person or {})
        rec: dict[str, Any] = {}
        for key in ("source", "how"):
            word = str(got.get(key) or "")
            if word and _SLOT_NAME.fullmatch(word):
                rec[key] = word
        for key in ("people", "dms", "threads"):
            n = got.get(key)
            if isinstance(n, int) and not isinstance(n, bool):
                rec[key] = n
        return {"person": rec} if rec else {}
    except Exception:  # noqa: BLE001 - a log field never breaks a run
        return {}


#: x5Fallback: the hand-offs whose rung acted unconfirmed (or whose run broke partway) in the last
#: :data:`REPEAT_HANDOFF_S`, by skill and values (RAM only, this process): ``{key: (monotonic time, why, tried,
#: missing, seen)}``. A second run of the same thing goes to rung C at once (:func:`_recent_handoff`).
_RECENT_HANDOFFS: dict[str, tuple[float, str, list[dict[str, Any]], str | None, list[str]]] = {}
_recent_lock = threading.Lock()
#: At most this many kept (the oldest go first).
RECENT_MAX = 16


def _repeat_key(skill: Skill, values: Mapping[str, Any]) -> str | None:
    """The same thing asked again: the capability (a learned skill counts as its capability, the values carry what it
    binds) and the values as compared (:func:`yuki.skills.resolve.canonical`). ``None`` for a relative change (done
    twice it is a further change, and it never hands over unconfirmed anyway)."""
    absolute = _absolute_values(skill, values)
    if absolute is None:
        return None
    try:
        return json.dumps([skill.capability or skill.key, canonical(absolute)], sort_keys=True, ensure_ascii=False,
                          default=str)
    except Exception:  # noqa: BLE001
        return None


def _note_handoff(skill: Skill, values: Mapping[str, Any], why: str, tried: list[dict[str, Any]],
                  missing: str | None, seen: Any) -> None:
    """Keep an unconfirmed (or broken) run's hand-off for :data:`REPEAT_HANDOFF_S` (RAM only). Never raises."""
    try:
        key = _repeat_key(skill, values)
        if key is None:
            return
        now = time.monotonic()
        with _recent_lock:
            for k in [k for k, v in _RECENT_HANDOFFS.items() if now - v[0] > REPEAT_HANDOFF_S]:
                _RECENT_HANDOFFS.pop(k, None)
            _RECENT_HANDOFFS.pop(key, None)
            _RECENT_HANDOFFS[key] = (now, why, [dict(t) for t in tried if isinstance(t, Mapping)][:8], missing,
                                     _seen_names(seen))
            while len(_RECENT_HANDOFFS) > RECENT_MAX:
                _RECENT_HANDOFFS.pop(next(iter(_RECENT_HANDOFFS)), None)
    except Exception:  # noqa: BLE001 - nothing kept: a second run then runs its rungs, as before
        pass


#: fix-r31 (A2): the skills and values whose hand-off already said "run it once more" (:data:`RERUN_KEY`) in the last
#: :data:`REPEAT_HANDOFF_S` (RAM only, this process): ``{key: monotonic time}``. One such offer each, so a run that
#: again puts nothing out is an ordinary hand-off (by steps, the skill barred), never a loop.
_RERUN_OFFERED: dict[str, float] = {}


def _rerun_offer(skill: Skill, values: Mapping[str, Any]) -> bool:
    """fix-r31 (A2): whether this hand-off may say "run it once more" (the first for these skill and values within
    :data:`REPEAT_HANDOFF_S`; noted here when it may). A relative change never (it has no repeat key). Never raises."""
    try:
        key = _repeat_key(skill, values)
        if key is None:
            return False
        now = time.monotonic()
        with _recent_lock:
            for k in [k for k, at in _RERUN_OFFERED.items() if now - at > REPEAT_HANDOFF_S]:
                _RERUN_OFFERED.pop(k, None)
            if key in _RERUN_OFFERED:
                return False
            _RERUN_OFFERED[key] = now
            while len(_RERUN_OFFERED) > RECENT_MAX:
                _RERUN_OFFERED.pop(next(iter(_RERUN_OFFERED)), None)
        return True
    except Exception:  # noqa: BLE001 - no offer: the ordinary hand-off
        return False


def _recent_handoff(skill: Skill, values: Mapping[str, Any],
                    ctx: RunContext) -> tuple[str, list[dict[str, Any]], str | None, list[str]] | None:
    """The hand-off this same skill and values made within :data:`REPEAT_HANDOFF_S` after a rung acted unconfirmed (or
    the run broke partway): ``(why, tried, missing, seen)``, or ``None``. The talker's and the thinker's runs only
    (a card's or an eval's run never hands over unconfirmed), never a skill that sends (it never hands over
    unconfirmed: rule 8)."""
    try:
        if ctx.origin not in HANDOFF_ORIGINS or sends_with(skill, values):
            return None
        key = _repeat_key(skill, values)
        if key is None:
            return None
        with _recent_lock:
            got = _RECENT_HANDOFFS.get(key)
        if got is None or time.monotonic() - got[0] > REPEAT_HANDOFF_S:
            return None
        return got[1], [dict(t) for t in got[2]], got[3], list(got[4])
    except Exception:  # noqa: BLE001 - none known: the run runs its rungs
        return None


def _handoff(skill: Skill, values: Mapping[str, Any], filled: Mapping[str, str], tried: list[dict[str, Any]],
             ctx: RunContext, started: float, *, why: str, run_key: str | None, acted: bool = False,
             routes: list[Route] | None = None, unconfirmed: bool = False, missing: str | None = None,
             seen: Any = (), partial: bool = False, focus_taken: bool = False,
             ledger: Mapping[str, Any] | None = None, repeat: bool = False,
             evidence: Mapping[str, Any] | None = None, again: bool = False) -> SkillResult:
    """Rung C: never with a game in front (D17(2)), re-read right here. The pinned skill is the old loop's pin and the
    new brain's plan seed alike (:func:`pinned`), held to the done-check of the routes this run took (``routes``),
    with IF-seed's ``missing`` (its value left out of that check) and ``seen``. ``unconfirmed``: the last rung acted
    and its check couldn't read (x5Fallback): the run's row is never marked contradicted for it. ``partial``: the
    values are only what the caller gave (a dropped value handed over). ``ledger``: more of the run row's facts (the
    rung that acted, its value, app and the check's evidence: content-free). ``repeat``: the same thing handed over
    unconfirmed moments ago, so no rung ran this time (:func:`_recent_handoff`). ``evidence``: what the run tried
    and saw (Phase 2.3, :func:`_evidence`; built here from ``tried`` when the caller has none). ``again`` (fix-r31, A2):
    the rung that acted put nothing out (:func:`_nothing_out`), so an unconfirmed hand-off may say "run it once more"
    (:data:`RERUN_KEY` on the pin, once per skill and values: :func:`_rerun_offer`) and is not kept as a repeat."""
    ev = dict(evidence) if evidence else _evidence(skill, values, tried, acted=acted, missing=missing, seen=seen,
                                                   sources=None, reason=why)
    game = ctx.game_now()
    if game:
        _ledger_finish(ctx, run_key, outcome="held_game", checked=None, acted=acted)
        return _simple(skill.key, "held_game", ctx, started, held="game", say=HELD_LINE, tried=tried,
                       run_key=run_key, evidence={**ev, "reason": "game", "fallback_line": HELD_LINE})
    # the onset probes' states right before the thinker acts (its finish gate compares with them, review M4), on the
    # very check the seed carries (a missing entity's argument left out)
    spec = _done_spec(skill, values, routes, drop=_drop_for(skill, missing, filled))
    try:
        from yuki.skills.donecheck import take_baseline

        base = take_baseline(spec, {}, readers=ctx.baselines) if spec else {}
    except Exception:  # noqa: BLE001 - no baseline: the gate can't show an onset (never a false done)
        base = {}
    pin = pinned(skill, values, filled, tried, why=why, baseline=base, routes=routes, missing=missing, seen=seen,
                 partial=partial)
    _ledger_finish(ctx, run_key, outcome="handed_over", checked=False if acted and not unconfirmed else None,
                   acted=acted, **dict(ledger or {}))
    rerun = (bool(again) and not repeat and why == WHY_UNVERIFIED and ctx.origin in HANDOFF_ORIGINS
             and _rerun_offer(skill, values))
    if rerun:
        # fix-r31 (A2): nothing went out, so the caller brings the window up and runs it once more (never by steps)
        pin[RERUN_KEY] = True
    elif why in (WHY_UNVERIFIED, WHY_ERROR) and ctx.origin in HANDOFF_ORIGINS:
        # a second run of the same thing in the next moments goes straight here (it could do it twice)
        _note_handoff(skill, values, why, tried, missing, seen)
    _emit(ctx, "skill_handoff", skill=skill.key, result="handed_over", reason=why, tried=len(tried),
          missing_slots=_slot_flags([pin["missing"]] if pin.get("missing") else []), seen=len(pin.get("seen") or []),
          repeat=True if repeat else None, rerun=True if rerun else None, origin=ctx.origin)
    return _simple(skill.key, "handed_over", ctx, started, handoff=pin, tried=tried, run_key=run_key,
                   values=dict(values), focus_taken=focus_taken, why="repeat" if repeat else "", evidence=ev)


def _to_planner(skill: Skill, plan: Plan, tried: list[dict[str, Any]], ctx: RunContext, started: float, *,
                run_key: str | None, acted: bool, missing: str | None = None, seen: Any = (),
                evidence: Mapping[str, Any] | None = None) -> SkillResult:
    """A rung whose step the consent rule handed over (:data:`TO_PLANNER`; that step did not run): rung C with the
    plan seed when the skill's data lets it hand over (never with a game in front: :func:`_handoff` reads it again).
    A skill whose data says it is never handed over (it would only search), or an eval run, stops there with the
    user's-step line: nothing more is done, and no next rung tries the same step."""
    if skill.handoff and ctx.origin != "eval":
        return _handoff(skill, plan.values, plan.filled, tried, ctx, started, why=TO_PLANNER, run_key=run_key,
                        acted=acted, routes=list(plan.routes), missing=missing, seen=seen, evidence=evidence)
    _ledger_finish(ctx, run_key, outcome="refused", checked=None, acted=acted)
    return _simple(skill.key, "refused", ctx, started, say=LINE_COMMIT_USER, tried=tried, run_key=run_key,
                   evidence={**dict(evidence or {}), "reason": TO_PLANNER, "fallback_line": LINE_COMMIT_USER})


# ---------------------------------------------------------------------------------------------------------------------
# Entry points: the voice hands and the thinker's tool
# ---------------------------------------------------------------------------------------------------------------------


def _in_call(hands: Any, memory: Any) -> bool:
    """Whether a call is live now, for any origin (review L7): the voice's own picture when its hands carry one, else
    memory's open calls (an app holding the microphone as a call: ``MemoryClient.current_calls``)."""
    if hands is not None and hasattr(hands, "in_call"):
        return bool(getattr(hands, "in_call", False))
    if memory is None:
        return False
    # lMsg merge note 5: memory's open calls checked against the live microphone (a call memory still showed open
    # hours after it ended made the volume ask "whose volume?")
    try:
        from yuki.skills.chat import _current_calls

        calls = _current_calls(memory)
    except Exception:  # noqa: BLE001 - memory's word as before
        calls = _mem(memory, "current_calls", lambda c: c.current_calls(), timeout_s=0.3)
    return bool(calls) if isinstance(calls, list) else False


def _live_now(game: str, hands: Any = None, *, media: bool = True, skill: Skill | None = None,
              memory: Any = None) -> Live:
    """The live facts at request time, read once on request (no polling): the game in front, the media sessions
    playing (only when a slot of the skill fills from them), whether a call is live (only when a slot asks in a
    call), and the skill's window-state reader for the ``game = "states"`` rule (its driver's hook)."""
    playing: tuple[str, ...] = ()
    sessions: tuple[str, ...] = ()
    try:
        if not media:
            raise LookupError
        from yuki.perception.system import media_sessions
        from yuki.actions.media import app_key_of

        # one read: the sessions playing, and every session whatever its status (skill20 review: the apps the playing
        # measure can see, so a reading of one of them yields to the one app playing and a game's never does)
        got = [s for s in media_sessions(timeout_s=0.2) if s.get("app_id")]
        playing = tuple(dict.fromkeys(app_key_of(str(s.get("app_id") or "")) for s in got
                                      if str(s.get("status") or "") == "playing"))
        sessions = tuple(dict.fromkeys(app_key_of(str(s.get("app_id") or "")) for s in got))
    except Exception:  # noqa: BLE001 - nothing known playing
        playing, sessions = (), ()
    wants_call = skill is not None and any(s.ask_in_call for s in skill.slots)
    in_call = _in_call(hands, memory) if wants_call else False
    states = skill is not None and any(r.game == "states" for r in skill.routes)
    return Live(game=game, playing=playing, in_call=in_call, sessions=sessions,
                window_state=window_state_reader(skill) if states else None)


#: The slot types whose value must stand in the user's words (a span, :func:`yuki.skills.resolve.check_value`); an
#: option or a number is checked by code alone, so dropping one is never a mishearing.
WORD_TYPES: tuple[str, ...] = ("text", "app", "entity", "person")


def _given_text(value: Any) -> str:
    """A given slot value as its words (an entity or a person given as ``{"name": ...}``)."""
    if isinstance(value, Mapping):
        value = value.get("name")
    return " ".join(str(value or "").split()) if isinstance(value, (str, int, float)) else ""


def _dropped_handoff(resolved: Any, slots: Mapping[str, Any] | None, words: list[str], loaded: Any,
                     ctx: RunContext) -> Handoff | None:
    """x5Fallback (failure audit 10 Oct, C7; IF-handoff): the resolver's question for a slot the caller **gave** and
    the resolver dropped because the value isn't a span of the words as heard (the talker's or the thinker's own
    reading: "Hemanth" for the heard "hey meant", "Discord" for "this card") becomes rung C with the given values (why
    :data:`WHY_DROPPED`), never "Who to?" asked back. In the seed the asked slot is :data:`FROM_UNHEARD`, another given
    value that stands in the words (or that the resolver checked and kept, review r2 L1) is ``words`` (the resolver's
    word for it) and one that doesn't is :data:`FROM_GIVEN`; an option or a number that isn't valid is left out (code
    checks those without the words).

    ``None`` keeps the question: a slot nobody gave (a real question), a given value that **is** in the words and was
    still asked about (two of the user's people answer to it: a real question too), a question about a given value
    the resolver kept (its ``Ask.dropped`` doesn't name the slot: "Did you mean Hemanth?"), a card or an eval
    (:data:`HANDOFF_ORIGINS`), a skill whose data never hands over, a run that commits without sending (a call join:
    redteam D10, review r1 M1). Sends still go through the consent rule where the
    thinker acts (rule 8); the seed's ``sends`` errs toward "may send" (only the given values are known)."""
    if not isinstance(resolved, Ask) or ctx.origin not in HANDOFF_ORIGINS:
        return None
    try:
        return _dropped(resolved, slots, words, loaded)
    except Exception:  # noqa: BLE001 - the resolver's question stands
        return None


def _asks_whom(resolved: Ask, slots: Mapping[str, Any] | None, loaded: Any) -> bool:
    """Whether the resolver's question is for a person slot nobody gave (fMsg merge note E1): the asked slot is
    person-bound by the capability's own data (``resolve._person_bound``: a ``person`` slot, or a text slot bound to a
    contact, ``message.person``), the caller gave no value for it, and the resolver dropped none there. lb B2: or its
    given person was dropped (the words don't hold it) on a capability that types in a chat (:func:`_whom_in_chat`):
    that question is "Who to?" too, kept, never handed over. Data only; never raises (``False``)."""
    try:
        from yuki.skills.resolve import _person_bound

        if loaded is None or not hasattr(loaded, "get"):
            from yuki.skills.catalog import load

            loaded = load()
        skill = loaded.get(str(resolved.skill or ""))
        slot = skill.slot(resolved.slot) if isinstance(skill, Skill) else None
        if slot is None or not _person_bound(skill, slot):
            return False
        if resolved.slot in set(getattr(resolved, "dropped", None) or ()):
            return _whom_in_chat(skill, slot)
        return not _given_text((slots or {}).get(resolved.slot))
    except Exception:  # noqa: BLE001
        return False


def _front_spoken(hands: Any) -> tuple[int | None, frozenset[int]]:
    """lb B1 (K-F): what the run's hands measured about the window in front since the user spoke, so a chat Yuki itself
    brought to the front is never taken for the one they spoke over: ``(the window in front when they spoke, if an act
    of Yuki's moved the focus in this turn; else None, windows the desk's own steps saw come to the front)``. Read from
    the hands' own records (the voice hands' front before their last act that moved the focus, kept with its turn; the
    brain's desk hands' window their steps brought forward), each by ``getattr``: hands that keep none give
    ``(None, ())``. Never raises."""
    spoke: int | None = None
    brought: set[int] = set()
    try:
        kept = getattr(hands, "_front_before", None)
        turn_of = getattr(hands, "user_turn", None)
        if isinstance(kept, tuple) and len(kept) >= 3 and callable(turn_of):
            turn = turn_of()
            if turn is not None and kept[2] == turn and isinstance(kept[0], int) and kept[0]:
                spoke = int(kept[0])
        for name in ("_brought", "_prev_brought"):
            hwnd = getattr(hands, name, None)
            if isinstance(hwnd, int) and not isinstance(hwnd, bool) and hwnd:
                brought.add(int(hwnd))
    except Exception:  # noqa: BLE001 - what was read
        pass
    return spoke, frozenset(brought)


def _front_reader(skill_key: str, slots: Mapping[str, Any] | None, ctx: RunContext) -> Callable[[Any], Any] | None:
    """lb B1 (final-H2 kw_name_word_in_text; contract K-F): for a capability that types in a chat whose person the caller
    gave and the user's words don't hold as written, on a request with spoken words (voice or thinker), ``store ->``
    the chat that was in front when they spoke (:func:`yuki.skills.chat.front_chat` on memory's Now board, read here
    once, outside memory's call; minus a window Yuki brought forward this turn, :func:`_front_spoken`), for the
    resolver's ``front``; ``None`` when not needed (no board read then). Never raises."""
    try:
        if ctx.origin not in HANDOFF_ORIGINS:
            return None
        words = [str(w) for w in ctx.words or () if str(w or "").strip()]
        if not words:
            return None
        from yuki.skills.catalog import load
        from yuki.skills.resolve import _person_bound

        skill = load().get(str(skill_key or ""))
        if skill is None or not getattr(skill, "sends", False):
            return None
        slot = next((s for s in skill.slots if _person_bound(skill, s)), None)
        text = _given_text((slots or {}).get(slot.name)) if slot is not None else ""
        if not text:
            return None
        from yuki.skills.uia_route import said

        if said(text, words):
            return None              # their words hold it as written: the resolver needs no chat in front
        from yuki.skills import chat

        board = chat._board_now()
        if not isinstance(board, Mapping):
            return None
        spoke, brought = _front_spoken(ctx.hands)
    except Exception:  # noqa: BLE001 - not measured: nobody in front
        return None

    def read(store: Any) -> Any:
        return chat.front_chat(store, board, spoke=spoke, brought=brought)

    return read


def _whom_in_chat(skill: Any, slot: Any) -> bool:
    """lb B2: whether ``slot`` is whom a capability that types in a chat writes to: person-bound by its own data
    (``resolve._person_bound``) on a capability that may send (``sends``: ``message``). Data only; never raises."""
    try:
        from yuki.skills.resolve import _person_bound

        return bool(getattr(skill, "sends", False)) and bool(_person_bound(skill, slot))
    except Exception:  # noqa: BLE001
        return False


def _dropped(resolved: Ask, slots: Mapping[str, Any] | None, words: list[str], loaded: Any) -> Handoff | None:
    """:func:`_dropped_handoff`'s work (it may raise; the caller keeps the question then)."""
    from yuki.skills.resolve import app_name, check_value
    from yuki.skills.uia_route import said

    given = {str(k): v for k, v in (slots or {}).items() if _given_text(v) or isinstance(v, (int, float, bool))}
    if resolved.slot not in given:
        return None
    try:
        from yuki.skills.catalog import load

        skill = (loaded if loaded is not None and hasattr(loaded, "get") else load()).get(str(resolved.skill or ""))
    except Exception:  # noqa: BLE001 - no catalog: the question stands
        skill = None
    asked = skill.slot(resolved.slot) if isinstance(skill, Skill) else None
    if asked is None or not skill.handoff or asked.type not in WORD_TYPES:
        return None
    if _whom_in_chat(skill, asked):
        # lb B2 (final-H2 kw_name_word_in_text, one: "Do it by steps (look, click, type)" after a dropped person, and
        # both planners refuse typing into a chat by steps, one20 / integ28 A8): a person the capability would type to
        # in a chat that the words don't hold is never handed over to be done by steps; the resolver's own question
        # stands ("Who to?", once: an asked run settles the turn, and their answer is noted for it below)
        return None
    if run_commit(skill) != "none" and not skill.sends:
        # review r1 M1: a run that commits without sending (``discord.voice``: a call join reaches other people) keeps
        # the question: a value nobody said is never joined (redteam D10), and the thinker has no consent step for a
        # join. A skill that sends keeps its hand-off: the consent rule asks before any send where the thinker acts
        return None
    # review r2 L1: the resolver's own verdict when it gives one (x6Resolver's ``Ask.dropped``, the slots whose given
    # value failed its checks). A question about a given value it kept (x6's "Did you mean Hemanth?" for a name said
    # as written that sounds like one of the user's people) is a real question and stands. A given slot it checked
    # (its slot loop stops at the question, so only the slots before the asked one) and kept passed every check it
    # has (the user's people, heard forms, transliterations: "Hemanth" from "हेमंत"), so it is the user's word, never
    # "given". With no verdict (an older resolver) today's literal checks stand
    verdict = getattr(resolved, "dropped", None)
    kept: set[str] = set()
    if isinstance(verdict, (list, tuple)):
        failed = {str(v) for v in verdict}
        if asked.name not in failed:
            return None
        order = [s.name for s in skill.slots]
        kept = set(order[:order.index(asked.name)]) - failed if asked.name in order else set()
    heard = [str(w) for w in words or [] if str(w or "").strip()]
    asked_text = _given_text(given[asked.name])
    if not asked_text or said(asked_text, heard):
        return None
    values: dict[str, Any] = {}
    filled: dict[str, str] = {}
    for slot in skill.slots:
        if slot.name not in given:
            if slot.default is not None:
                # as the resolver fills it (the seed's done-check reads every slot it names); ``pinned`` leaves a
                # default out of the send condition (only what the caller gave is known: rule 8 errs to "may send")
                values[slot.name], filled[slot.name] = slot.default, "default"
            continue
        raw, text = given[slot.name], _given_text(given[slot.name])
        if slot.name != asked.name:
            if slot.type == "person":
                ok, value = (said(text, heard), {"name": text}) if text else (False, None)
            else:
                ok, value = check_value(slot, raw, heard)
            if ok:
                values[slot.name], filled[slot.name] = value, "words"
                continue
        if slot.type not in WORD_TYPES or not text:
            if slot.default is not None:
                values[slot.name], filled[slot.name] = slot.default, "default"
            continue
        values[slot.name] = app_name(text) if slot.type == "app" else (
            {"name": text} if slot.type in ("entity", "person") else text)
        # review r2 L1: a value the resolver kept is the user's word even when the literal check here can't see it
        filled[slot.name] = FROM_UNHEARD if slot.name == asked.name else ("words" if slot.name in kept
                                                                           else FROM_GIVEN)
    return Handoff(skill, values, filled, why=WHY_DROPPED)


def granted_values(grant: Mapping[str, Any] | None, slots: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The slot values a validated send grant (:attr:`RunContext.grant`) covers, ``{slot: value}`` for the resolver's
    ``granted=``: its text slot's and its person slot's, when the run gives exactly those values (case and spaces
    aside). ``None`` when there is no grant or this run's values are not the granted ones: a grant for another text or
    another person covers nothing, not even the half that matches (failure fix-plan Item 11's L2)."""
    if not isinstance(grant, Mapping):
        return None
    given = dict(slots or {})
    out: dict[str, str] = {}
    for slot_key, value_key in (("text_slot", "text"), ("person_slot", "person")):
        name, want = _given_text(grant.get(slot_key)), _given_text(grant.get(value_key))
        if not name or not want or name not in given or _given_text(given[name]).casefold() != want.casefold():
            return None
        out[name] = want
    return out


def _thinker_grant(grant: Any, ctx: RunContext, skill_key: str, slots: Mapping[str, Any] | None
                   ) -> Mapping[str, Any] | None:
    """The brain's consent grant for this run when it is one (:func:`yuki.skills.uia_route.send_grant`: a send the
    consent rule allowed for this very step, held by the request's desk hands now) and this run gives exactly its
    values (:func:`granted_values`), else ``None``. Logged content-free (``skill_grant``: whether it holds and a code
    word why not) whenever the caller passed one."""
    if not grant:
        return None
    try:
        from yuki.skills.uia_route import send_grant

        held, why = send_grant(grant, hands=ctx.hands, skill=str(skill_key or ""), request_id=ctx.request_id)
    except Exception:  # noqa: BLE001 - a grant that can't be checked is none
        held, why = None, "error"
    if held is not None and granted_values(held, slots) is None:
        held, why = None, "other_values"
    _emit(ctx, "skill_grant", skill=str(skill_key or ""), held=held is not None, why=why, origin=ctx.origin)
    return held


@dataclass
class _Pick:
    """A request the engine fills with Yuki's pick (:func:`_choice_request`): the skill to run, the slots left, the
    chosen values for the resolver and where they came from, the pick itself and the taste kind it was read from."""

    skill_key: str
    slots: dict[str, Any]
    chosen: dict[str, Any]
    chosen_from: str
    choice: Any
    taste_kind: str


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in ("true", "yes", "1")
    return bool(value)


def asks_choice(slots: Mapping[str, Any] | None) -> bool:
    """Whether a ``run_skill``'s slots ask Yuki to choose (contract C2: ``choose`` true): the engine fills the empty
    slot with its pick, so a caller's "a video nobody picked never plays" guard (the voice hands' ``_needs_offer``)
    lets it through. Never raises."""
    try:
        return isinstance(slots, Mapping) and _truthy(slots.get("choose"))
    except Exception:  # noqa: BLE001
        return False


def _choice_request(skill_key: str, slots: Mapping[str, Any] | None, ctx: RunContext, live: Live,
                    settings: Any = None) -> "_Pick | SkillResult | None":
    """kTaste (contract C2; audit 17 Oct decision 2): when the user asked Yuki to choose (``slots.choose``), or named
    only a channel for a video ("put on Deji's new video"), Yuki picks like a recommender
    (:func:`yuki.skills.taste.choose`) and the run plays its pick: a video (``web.open_video``; ``web.open_page`` on
    YouTube's front page asked to choose becomes it: "put something on YouTube" plays), a channel's page
    (``web.open_page`` on a channel page kind), a playlist of the user's own (``spotify.playlist``). A video or playlist
    text given with it counts when it IS one of their channels or playlists (``taste._same_name``): kw3 (C7, 20 Oct:
    "Put on the new Marques Brownlee video" with the caller's ``MKBHD`` was dropped) the caller's name goes on whether
    or not the user's words hold it as written (the model read their words; the user's own names resolve it), and,
    asked to choose, a name they never said is tagged the card's pick for now (Yuki's own, ``taste.FROM_CARD``, a
    guess, so it never feeds their taste; phase 2.2); asked to choose, any other text given is their words for the
    judgement and the pick is free (review M1); not asked, a text that is none of their channels goes the normal way
    (review H2: "the Sidemen video about the island" is a search, never Sidemen's newest). With a game in front
    nothing is put on; with a call live nothing Yuki chose is put on (a channel they named plays, review L4): the
    result says so and keeps the pick for later (:func:`taste.held_line`,
    :func:`taste.keep_for_later`). ``None``: not such a request, no pick (nothing of theirs to pick from, memory too
    slow), or a concrete value they gave (an offered option, a link: theirs wins); the request then runs the normal way
    with the choice slots taken out."""
    from yuki.skills import taste
    from yuki.skills.catalog import load
    from yuki.skills.connection import scope_value

    given = dict(slots or {})
    asked = asks_choice(given)
    context = " ".join(str(given.get("context") or "").split())
    rest = {k: v for k, v in given.items() if k not in CHOICE_SLOTS}
    skill = load().get(str(skill_key or ""))
    if skill is None:
        return None
    cap = skill.capability or skill.key
    kind = taste.CHOOSE_KINDS.get(cap)
    if kind is None:
        return None
    bound = dict(getattr(skill, "bound", None) or {})
    only = ""
    source = ""                    # phase 2.2: taste.FROM_CARD for a name under choose they never said (Yuki's pick)
    key = skill.key
    if cap == "web.open_page":
        from yuki.skills.youtube_feed import CHANNEL_PAGE, HANDLE_PAGE, HOME_PAGE, SITE

        page = " ".join(str(rest.get("page") or bound.get("page") or "").split()).casefold()
        if not asked or scope_value(rest.get("site") or bound.get("site")) != SITE or rest.get("item"):
            return None
        if page in ("", HOME_PAGE):
            kind, key = "video", "web.open_video"
            rest = {k: v for k, v in rest.items() if k == "browser"}
        elif page in (HANDLE_PAGE, CHANNEL_PAGE):
            kind = "channel"
            rest.pop("page", None)
        else:
            return None
    else:      # web.open_video, spotify.playlist: a text given for the slot
        slot_name = taste.CHOOSE_SLOTS[kind]
        given_v = rest.pop(slot_name, None)
        if isinstance(given_v, Mapping) and given_v.get("ref"):
            return None            # a concrete one (an offered option's link, a learned link): theirs, the normal way
        name = _given_text(given_v)
        if name:
            from yuki.skills.uia_route import said

            if asked and not said(name, ctx.words):
                # choose + a name they never said: the card's pick for now, Yuki's own (a provenance tag only: the
                # name still goes on, and a guess never feeds their taste)
                source = taste.FROM_CARD
            elif cap == "web.open_video" and _offered_pick(ctx, name):
                return None        # an offered upload the caller named ("the Deji one"): the offer's own path
        if name and (asked or cap == "web.open_video"):
            only = name            # a channel or playlist of theirs if it IS one (asked: else a hint; not: else normal)
        if not asked and not only:
            return None
    slot = (load().get(key) or skill).slot(taste.CHOOSE_SLOTS[kind])
    taste_kind = str(getattr(slot, "taste", "") or "")
    hold = "game" if live.game else ("call" if asked and _in_call(ctx.hands, ctx.memory) else "")

    def pick(client: Any) -> Any:
        return taste.choose(_store_of(client), kind, words=list(ctx.words), context=context,
                            session=ctx.session_id, only=only, asked=asked, taste_kind=taste_kind, settings=settings,
                            log=ctx.log, network=not hold, judge_wait_s=0.0 if hold else taste.CHOOSE_WAIT_S,
                            source=source)

    choice = _mem(ctx.memory, "taste_choose", pick, timeout_s=CHOOSE_TIMEOUT_S)
    if not asked and choice is None:
        return None                # the text named no channel of theirs: the normal way with it, as before
    if hold:
        if choice is not None:
            # named for later and kept: the next ask of this conversation puts it on (review L3)
            taste.keep_for_later(choice, session=ctx.session_id)
        outcome = "held_game" if hold == "game" else HELD_CALL
        _emit(ctx, "skill_run", skill=key, result=outcome, choose=True, origin=ctx.origin)
        line = taste.held_line(choice, hold)
        return SkillResult(skill=key, outcome=outcome, say=line, held=live.game or None,
                           evidence={"acted": False, "reason": hold, "fallback_line": line})
    if choice is None:
        return None
    chosen = {choice.slot: choice.value, **choice.extra}
    return _Pick(key, {k: v for k, v in rest.items() if k not in chosen}, chosen,
                 "words" if choice.named else FROM_CHOSEN, choice, taste_kind)


def _title_request(skill_key: str, slots: Mapping[str, Any] | None, ctx: RunContext,
                   live: Live) -> tuple[str, dict[str, Any], str] | None:
    """skill20 (final-D B6): a title handed for a slot every rung of which runs only with a link
    (``taste.takes_offers``: web.open_video's ``video``), with no link and none of the uploads just offered: that title
    as one video's own link
    by code (:func:`yuki.skills.web_route.link_for_title`: the uploads cached from the user's channels' feeds, then the
    links of the page shown in front or in the browser asked for; a string measure, one clear fit only). ``(slot, value,
    where it came from)`` for the resolver's ``chosen``: ``words`` when the title stands in the user's words, else
    ``card`` (one of their channels' uploads) or ``screen`` (a link on the page): a guess the done line names. ``None``:
    no such slot, a link already given, a game in front (nothing is read then), or no clear fit (the run goes on as
    before: no route, the caller does it by steps). Called only when no pick of Yuki's fills the run
    (:func:`_choice_request`). Read-only; content stays in RAM, the log line holds code words."""
    from yuki.skills import taste
    from yuki.skills.catalog import load

    if live.game:
        return None
    skill = load().get(str(skill_key or ""))
    given = dict(slots or {})
    if skill is None:
        return None
    for slot in skill.slots:
        value = given.get(slot.name)
        if not taste.takes_offers(skill, slot) or (isinstance(value, Mapping) and value.get("ref")):
            continue
        title = _given_text(value)
        if not title or _offered_pick(ctx, title):
            continue
        from yuki.skills import web_route

        # the feed's uploads on memory's own side (one bounded call), then the page's links (a read-only UIA job)
        got = _mem(ctx.memory, "skill_title_link",
                   lambda c, t=title: web_route.link_for_title(t, store=_store_of(c)), timeout_s=0.8) \
            if ctx.memory is not None else None
        if got is None:
            got = web_route.link_for_title(title, windows=web_route.title_windows(given.get("browser")))
        if got is None:
            # skill22 (A8): no clear fit: the video site's own search for the title (``youtube.toml [urls] search``),
            # opened and named as a search, never as "playing" (web_route._searched); the caller takes it on from the
            # results (kw3 C11: any title the caller gave, however few its words)
            search = web_route.search_link(title)
            _emit(ctx, "skill_resolve", skill=skill.key, result="title_link",
                  reason="search" if search is not None else "no_fit", origin=ctx.origin)
            if search is None:
                return None
            from yuki.skills.uia_route import said

            where = "words" if said(title, ctx.words) else "screen"
            return slot.name, search, where
        from yuki.skills.uia_route import said

        where = "words" if said(title, ctx.words) else (FROM_CARD if got.get("via") == "feed" else "screen")
        _emit(ctx, "skill_resolve", skill=skill.key, result="title_link", reason=str(got.get("via") or ""),
              fit=got.get("fit"), slot_source=[where], origin=ctx.origin)
        value = {k: got[k] for k in ("name", "ref", "key") if got.get(k)}
        return slot.name, value, where
    return None


def _offered_pick(ctx: RunContext, name: str) -> bool:
    """Whether ``name`` picks one of the uploads just offered aloud ("the Deji one": the offer's own path)."""
    from yuki.skills import taste

    got = _mem(ctx.memory, "taste_offered", lambda c: taste.find_offered(_store_of(c), name), timeout_s=0.5)
    return got is not None


def note_args(skill_key: str, args: Mapping[str, Any], slots: Mapping[str, Any] | None
              ) -> tuple[str, Any, dict[str, Any]]:
    """Phase 2.1/2.2: a run's own notes (:data:`NOTE_SLOTS`) and its slots without them: ``args["why"]`` /
    ``args["from"]`` (the planner's run_skill), else ``slots["why"]`` / ``slots["from"]`` (the talker's free-form
    slots) when the skill has no slot of that name; those entries then leave the slots, so the resolver never reads one
    as a value. ``(why, from, slots)``: the why as one line (``""``: none), the from as given (one word or a slot ->
    word map; ``None``: none). Never raises."""
    rest = dict(slots or {})
    own: set[str] = set()
    try:
        if any(name in rest for name in NOTE_SLOTS):
            from yuki.skills.catalog import load

            skill = load().get(str(skill_key or ""))
            own = {getattr(sl, "name", "") for sl in skill.slots} if skill is not None else set()
    except Exception:  # noqa: BLE001 - unknown: the slots stay slots
        own = set(NOTE_SLOTS)
    got: dict[str, Any] = {name: args.get(name) if isinstance(args, Mapping) else None for name in NOTE_SLOTS}
    for name in NOTE_SLOTS:
        if name in rest and name not in own:
            value = rest.pop(name)
            if got.get(name) in (None, ""):
                got[name] = value
    why = " ".join(got["why"].split()) if isinstance(got.get("why"), str) else ""
    src = got.get("from")
    return why, (src if isinstance(src, (str, Mapping)) and src else None), rest


#: Phase 2.1: the runs of the last :data:`GUESS_KEEP_S` that held a guess of Yuki's, oldest first: (monotonic time, the
#: capability, every slot's value identity in that run, the guessed slots). RAM only, this process; never logged.
_GUESSES: list[tuple[float, str, dict[str, str], frozenset[str]]] = []
_guess_lock = threading.Lock()
#: At most this many such runs are kept.
GUESS_RUNS = 8


def _value_id(value: Any) -> str:
    """A slot value as one identity to compare guesses by (its name for an entity, :func:`scope_value`)."""
    from yuki.skills.connection import scope_value

    try:
        return str(scope_value(value.get("name") if isinstance(value, Mapping) else value) or "")
    except Exception:  # noqa: BLE001
        return ""


def _run_ids(plan: Plan) -> dict[str, str]:
    """Every slot value of a resolved run as its identity (:func:`_value_id`), empty ones left out."""
    ids = {str(k): _value_id(v) for k, v in plan.values.items()}
    return {k: v for k, v in ids.items() if v}


def _guess_runs(cap: str) -> list[tuple[dict[str, str], frozenset[str]]]:
    """``cap``'s runs that held a guess within :data:`GUESS_KEEP_S` (oldest first): (their value identities, their
    guessed slots)."""
    now = time.monotonic()
    with _guess_lock:
        return [(ids, guessed) for t, c, ids, guessed in _GUESSES if c == cap and now - t <= GUESS_KEEP_S]


def _guessed_before(cap: str, slot: str) -> list[str]:
    """The identities guessed for ``cap``'s ``slot`` within :data:`GUESS_KEEP_S` (oldest first)."""
    got = [ids.get(slot, "") for ids, guessed in _guess_runs(cap) if slot in guessed]
    return [i for i in dict.fromkeys(got) if i]


def _note_guesses(plan: Plan) -> None:
    """Keep a run that held a guess (:attr:`Plan.guessed`), with every value it ran, for "not that". Never raises."""
    try:
        cap = plan.skill.capability or plan.skill.key
        now = time.monotonic()
        entry = (now, cap, _run_ids(plan), frozenset(plan.guessed))
        with _guess_lock:
            _GUESSES[:] = [g for g in _GUESSES if now - g[0] <= GUESS_KEEP_S][-(GUESS_RUNS - 1):] + [entry]
    except Exception:  # noqa: BLE001 - nothing kept: "not that" then runs what the caller gives
        pass


def _rejected_slot(resolved: Any) -> str | None:
    """Phase 2.1: on a correction, the slot whose value is again Yuki's guess (``resolve.GUESS_SOURCES``) in a request
    that is otherwise the very run of moments ago that guessed it (every other slot's value the same, review H1): the
    caller re-sent the same request, so the only thing the user can have said "not that" to is that guess. ``None``
    otherwise: a correction that changed any other value ("no, lower, like 10"; "no, in Chrome") runs as given, and a
    value now in their words ("no, I said Excalidraw") is theirs."""
    if not isinstance(resolved, Plan):
        return None
    cap = resolved.skill.capability or resolved.skill.key
    now_ids = _run_ids(resolved)
    runs = _guess_runs(cap)
    for slot in resolved.guessed:
        ident = now_ids.get(slot)
        if not ident:
            continue
        rest = {k: v for k, v in now_ids.items() if k != slot}
        for ids, guessed in runs:
            if slot in guessed and ids.get(slot) == ident and {k: v for k, v in ids.items() if k != slot} == rest:
                return slot
    return None


def _next_own(ctx: RunContext, skill: Skill, slot_name: str, skip: Sequence[str]) -> str | None:
    """The user's next own candidate for a taste slot (:func:`yuki.skills.taste.top` at this hour, then
    :func:`yuki.skills.taste.candidates`), none of ``skip`` (identities already guessed or rejected). ``None`` for any
    other slot or none left."""
    slot = skill.slot(slot_name)
    if slot is None or not slot.taste or ctx.memory is None:
        return None
    from yuki.skills import taste

    gone = set(skip)

    def pick(c: Any) -> str | None:
        store = _store_of(c)
        for t in taste.top(store, slot.taste, n=8):
            if t.name and _value_id(t.name) not in gone:
                return t.name
        for n in taste.candidates(store, slot.taste):
            if n and _value_id(n) not in gone:
                return str(n)
        return None

    got = _mem(ctx.memory, "taste_other", pick, timeout_s=0.5)
    return str(got) if got else None


def _pick_where(line: str) -> str:
    """The route's own browser words at the end of its done line (" in Arc"), to keep in the pick's line. lb B4: the
    last " in " of Yuki's own line (a page line now names its item, which may hold one)."""
    m = re.search(r".* in ([^.]+)\.$", str(line or "").strip())
    return f" in {m.group(1)}" if m else ""


def resolve_and_run(skill_key: str, slots: Mapping[str, Any] | None, words: list[str], ctx: RunContext, *,
                    again: bool = False, live: Live | None = None, settings: Any = None, corrects: bool = False,
                    game_answer: str | None = None, earlier: Sequence[str] | None = None) -> SkillResult:
    """Resolve (on the memory's own store, in one bounded call) and run, for every origin. Live facts default to a
    fresh read (``game_answer``: the caller's own D9 answer for them; every rung still reads the game again).
    ``earlier``: the conversation's older turns, oldest first (x6Resolver merge note M1): only ever passed to the
    resolver as ``earlier=``, which validates a given value against them and never fills from them; never appended to
    ``words``, whose newest turn decides a named app.
    ``corrects``: the user corrected the skill's last run; the same skill and absolute values asked again within 60 s
    of a "done" (as a correction, or not ``again``) mark that run a false done first (D19, review r2 M2), taken back
    when the new run finds the PC already so (fSkills review M1: :func:`_note_false_done`).
    ``ctx.grant`` (the brain's validated consent grant, :func:`run_for_thinker`): the resolver keeps the granted text
    and person as given (``granted=``) instead of dropping them; with none, nothing changes.
    Phase 2.1: the caller's notes (:data:`NOTE_SLOTS`) leave ``slots`` here for every origin (:func:`note_args`; the
    context's own, read from a caller's arguments, first): ``ctx.given_from`` (the caller's word for where its values
    came from) goes to the resolver's ``given_from``, and ``ctx.why`` (a pick's why) makes a listed name it gave its own
    pick (``own_pick``) and is said in the done line. A correction that sends again the very request of moments ago
    whose guess the user turned down (:func:`_rejected_slot`: every other value the same) runs the user's next own
    candidate instead (:func:`_next_own`), else asks (:data:`LINE_WHICH`), never the same guess twice; each run that
    held a guess is kept for that (:func:`_note_guesses`)."""
    from yuki.skills import enabled, setting
    from yuki.skills import resolve as res_mod

    if not enabled(settings):
        # the off switch (Settings.skills = false): the old paths run instead; nothing here acts
        return run(Refused(str(skill_key or ""), "skills_off"), ctx)
    # Phase 2.1/2.2 (review M1): the caller's notes (``why``, ``from``) leave the slots on every path (the voice's
    # hands, a card, the thinker), the context's own (a caller that read them from its arguments) first
    ctx.why, ctx.given_from, slots = note_args(str(skill_key or ""), {"why": ctx.why, "from": ctx.given_from}, slots)
    # gd D2: the drivers read whether this run is a correction (a correction's words never teach a row's name)
    ctx.corrects = bool(corrects)
    # lb B5: whether this very run left the pick to Yuki (its own ``choose`` slot, contract C2: a measured fact of the
    # call, read before the choice slots leave it)
    choose_asked = asks_choice(slots)
    if live is None:
        from yuki.skills.catalog import load

        skill = load().get(str(skill_key or ""))
        wants_media = bool(skill and any(s.live == "playing" for s in skill.slots))
        live = _live_now(str(game_answer) if game_answer is not None else ctx.game_now(), ctx.hands,
                         media=wants_media, skill=skill, memory=ctx.memory)
    # kTaste (C2): the user asked Yuki to choose, or named only a channel: Yuki's pick fills the slot (or a game or a
    # call holds it, said so); the choice slots never reach the resolver
    picked = _choice_request(str(skill_key or ""), slots, ctx, live, settings)
    if isinstance(picked, SkillResult):
        return picked
    titled = None
    if picked is not None:
        skill_key, slots = picked.skill_key, picked.slots
    else:
        if slots and any(k in slots for k in CHOICE_SLOTS):
            slots = {k: v for k, v in slots.items() if k not in CHOICE_SLOTS}
        try:
            # skill20 (final-D B6): a title handed with no link, as its own link by code (or None: as before)
            titled = _title_request(str(skill_key or ""), slots, ctx, live)
        except Exception:  # noqa: BLE001 - the run goes on as before
            titled = None
    step = float(setting(settings, "skills_volume_step") or 10)
    disable_after = int(setting(settings, "skills_disable_after") or 2)
    catalog: dict[str, Any] = {}
    # Item 11's L2: only a grant the thinker origin's caller validated for this very step (never the voice's or a
    # card's), and only for exactly these values
    granted = granted_values(ctx.grant, slots) if ctx.origin == "thinker" else None
    # fd (final-F D4): the slot this capability asked about moments ago, answered now (the engine's own trace)
    try:
        from yuki.skills.catalog import load as _load_catalog

        answered = _answered_slots(_cap_key(_load_catalog().get(str(skill_key or ""))), ctx)
    except Exception:  # noqa: BLE001 - none known: every enum value as given
        answered = set()
    # lb B1 (K-F): the chat in front when they spoke, read only for a person the words don't hold as written
    front_read = _front_reader(str(skill_key or ""), slots, ctx)
    front_seen: dict[str, Any] = {}

    def go(client: Any) -> Any:
        from yuki.skills.catalog import load
        from yuki.skills.menu import request_key

        seeded = getattr(client, "_skills_seeded", None)
        # the tray's MemoryClient seeds the registry once per connection version and returns the store-aware catalog
        loaded = seeded() if callable(seeded) else None
        if loaded is None or not hasattr(loaded, "get"):
            loaded = load()
        catalog["loaded"] = loaded
        # lPage merge note 5 (lSplit review r2 L1): words naming another value for a learned skill's bound key slot
        # run its capability (the learned routes carry the bound value in their arguments), never the learned skill
        key = request_key(loaded, str(skill_key or ""), slots)
        extra: dict[str, Any] = {"earlier": [str(w) for w in earlier if w]} if earlier else {}
        if granted:
            extra["granted"] = dict(granted)
        if picked is not None:
            extra.update(chosen=dict(picked.chosen), chosen_from=picked.chosen_from)
        elif titled is not None:
            extra.update(chosen={titled[0]: dict(titled[1])}, chosen_from=titled[2])
        if ctx.given_from:
            extra["given_from"] = ctx.given_from
        if ctx.why:
            extra["own_pick"] = True        # a listed name it gave is its own pick from the candidates (a guess)
        if answered:
            extra["answered"] = set(answered)
        store = _store_of(client) if client is not None else None
        if front_read is not None and store is not None:
            def front() -> Any:
                # lb B1 (K-F): called by the resolver at most once, only for a person their words don't hold
                front_seen["v"] = front_read(store)
                return front_seen["v"]

            extra["front"] = front
        return res_mod.resolve(key, slots, words, store=store,
                               live=live, again=again, default_step=step, disable_after=disable_after, **extra)

    resolved = _mem(ctx.memory, "skill_resolve", go, timeout_s=1.0) if ctx.memory is not None else go(None)
    if resolved is None and ctx.memory is not None and ctx.origin in ("thinker", "card"):
        # sVoice r2 M3 for the thinker and card origins (sWire): memory didn't answer the resolve in time, so the
        # user's usuals, the ledger's disabled rungs and told values are unknown: nothing runs, said plainly (the voice
        # does the same through its own longer wait, hands._ResolveWait)
        return run(Refused(str(skill_key or ""), "memory_slow"), ctx)
    if resolved is None:
        resolved = go(None)
    if isinstance(resolved, Plan) and res_mod.FROM_FRONT in set(resolved.sources.values()) \
            and isinstance(front_seen.get("v"), Mapping):
        # lb B1 (K-F): the person came from the chat in front: the chat driver keeps the run in that very chat
        ctx.front_chat = dict(front_seen["v"])
    rejected = _rejected_slot(resolved) if corrects and picked is None else None
    replaced = rejected is not None     # review L3: this request's value was already swapped (no second swap below)
    if rejected is not None:
        # Phase 2.1: "not that" to Yuki's own guess, and the caller gave that same guess again: the user's next own
        # candidate for the slot (never one guessed already), else one short question; never the same guess twice
        cap = resolved.skill.capability or resolved.skill.key
        skip = [*_guessed_before(cap, rejected), _value_id(resolved.values.get(rejected))]
        nxt = _next_own(ctx, resolved.skill, rejected, skip)
        _emit(ctx, "skill_resolve", skill=resolved.skill.key, result="not_that", reason="next" if nxt else "asked",
              guessed_slots=_slot_flags([rejected]), origin=ctx.origin)
        redo = None
        if nxt:
            ctx.why = ""            # the caller's why was for the value the user just turned down
            again_slots = {**dict(slots or {}), rejected: nxt}
            src = {**(dict(ctx.given_from) if isinstance(ctx.given_from, Mapping) else {}), rejected: FROM_MENU}
            redo = _mem(ctx.memory, "skill_resolve", lambda c: res_mod.resolve(
                resolved.skill.key, again_slots, words, store=_store_of(c) if c is not None else None, live=live,
                again=again, default_step=step, disable_after=disable_after, given_from=src,
                **({"earlier": [str(w) for w in earlier if w]} if earlier else {})), timeout_s=1.0) \
                if ctx.memory is not None else None
        resolved = redo if redo is not None else Ask(resolved.skill.key, rejected, LINE_WHICH, [])
    if isinstance(resolved, Plan):
        cap = _cap_key(resolved.skill)
        if resolved.ask_keep and _fell_back(cap, ctx):
            # fd (final-F D4): this run is another way to what the same request just missed (the engine's own trace):
            # the means it takes is the caller's fallback, never proposed as the user's usual
            resolved.ask_keep = None
            _emit(ctx, "skill_resolve", skill=resolved.skill.key, result="keep_held", reason="fallback",
                  origin=ctx.origin)
        down, corrected = _corrected(cap, resolved.skill, ctx) if corrects and picked is None else ({}, None)
        if down:
            # fd (final-F D3): a correction never plays the row the corrected run acted on (its own trace, verified or
            # not) or one turned down before it: the drivers leave those out of what the words may mean
            # (``ctx.turned_down``), and a value that IS one of them takes the user's next own candidate (their taste
            # order), else one question. A name the user restates ("No, I said chill") is no such row (review M1).
            # integ27 fix D3: only in a request that is otherwise the corrected run's own (another value changed, "No,
            # in Arc": run as given, the old row still left out of what the words may mean)
            ctx.turned_down = down
            swap = next(((s, names) for s, names in down.items()
                         if _value_id(resolved.values.get(s)) in {_value_id(n) for n in names}
                         and _otherwise_same(resolved, slots, s, corrected)), None)
            if swap is not None:
                replaced = True
                slot_name, names = swap
                skip = [*dict.fromkeys(_value_id(n) for n in names), *_guessed_before(cap, slot_name)]
                nxt = _next_own(ctx, resolved.skill, slot_name, skip)
                _emit(ctx, "skill_resolve", skill=resolved.skill.key, result="not_that",
                      reason="corrected_next" if nxt else "corrected_asked", guessed_slots=_slot_flags([slot_name]),
                      origin=ctx.origin)
                redo = None
                if nxt:
                    ctx.why = ""            # the caller's why was for the value the user just turned down
                    again_slots = {**dict(slots or {}), slot_name: nxt}
                    src = {**(dict(ctx.given_from) if isinstance(ctx.given_from, Mapping) else {}),
                           slot_name: FROM_MENU}
                    skill_now = resolved.skill
                    redo = _mem(ctx.memory, "skill_resolve", lambda c: res_mod.resolve(
                        skill_now.key, again_slots, words, store=_store_of(c) if c is not None else None, live=live,
                        again=again, default_step=step, disable_after=disable_after, given_from=src,
                        **({"earlier": [str(w) for w in earlier if w]} if earlier else {})), timeout_s=1.0) \
                        if ctx.memory is not None else None
                resolved = redo if isinstance(redo, Plan) else Ask(resolved.skill.key, slot_name, LINE_WHICH, [])
    noted = _note_false_done(resolved, ctx, corrects=bool(corrects), again=bool(again))
    handed = _dropped_handoff(resolved, slots, words, catalog.get("loaded"), ctx)
    if handed is not None:
        # x5Fallback (IF-handoff): a value the caller gave that the words as heard don't hold is never asked back
        _emit(ctx, "skill_resolve", skill=handed.skill.key, result="handed_over", reason=WHY_DROPPED,
              slot_source=sorted(set(handed.filled.values())), slots=len(handed.filled),
              dropped_slots=_slot_flags([k for k, v in handed.filled.items() if v == FROM_UNHEARD]),
              origin=ctx.origin)
        resolved = handed
    elif isinstance(resolved, Ask) and _asks_whom(resolved, slots, catalog.get("loaded")):
        # fMsg merge note E1 (live 18 Oct, k_clarify_who_draft): the resolver asks whom a message is for ("Who to?") and
        # nobody gave the person: noted with the user's turns as they stand, so their very next turn ("to hardish")
        # answers it and a draft or an open runs as the one person that answer sounds like, never asked twice
        try:
            from yuki.skills.chat import note_who_asked

            note_who_asked(list(ctx.words))
        except Exception:  # noqa: BLE001 - the question stands either way
            pass
    # tTaste merge note 1 (phase T): a run that opens YouTube's home with no game in front ranks the fresh uploads on a
    # short thread of their own while the page opens; they are named (and noted, so "the B one" can pick one) only
    # after the run's checked done and a non-empty wait; a run that fails, asks or times out names nothing. A card's
    # run never offers. The offer plays nothing; a request that asked Yuki to choose plays its pick instead (above).
    # lb B5 (final-H2 kw_chrome_crashing_youtube, one: "Youtube is up. ... New from SidemenReacts, angryginge13 and
    # Sidemen: want one?" after a plain "open YouTube"): the offer is spoken only when this run left the pick to Yuki
    # (its own ``choose`` slot, a measured fact of the call, and Yuki had nothing to play: the pick above took none);
    # a plain open says nothing more and reads no feed (the user's rule: proactive = cards only; the cards keep their
    # own offers, yuki.memory.candidates)
    pending = None
    if isinstance(resolved, Plan) and (resolved.skill.capability or resolved.skill.key) == "web.open_page" \
            and ctx.origin != "card" and choose_asked:
        try:
            from yuki.skills import youtube_feed

            pending = youtube_feed.start(ctx.memory, resolved.values, game=live.game or None, log=ctx.log,
                                         settings=settings)
        except Exception:  # noqa: BLE001 - the offer is extra; the run goes on without it
            pending = None
    result = run(resolved, ctx)
    if isinstance(resolved, Plan) and getattr(resolved, "page_swap", None) \
            and result.outcome not in NOT_RAN_OUTCOMES | NOTHING_ON_OUTCOMES:
        # integ30 (F2): a site page item swapped for one of the user's own names is noted here, in the engine's own
        # thread, once its run put something on (they may have seen it): the same value given again is their
        # correction. A resolve abandoned past its wait, or a run that put nothing on, never marks one
        try:
            from yuki.skills.web_route import note_page_swap

            note_page_swap(resolved.page_swap)
        except Exception:  # noqa: BLE001
            pass
    if isinstance(resolved, Plan) and resolved.guessed and result.outcome not in (
            "asked", "held_game", HELD_CALL, HELD_BLOCKED, "refused"):
        _note_guesses(resolved)      # Phase 2.1: "not that" next takes another, never this one again
    _withdraw_false_done(ctx, noted, result, getattr(getattr(resolved, "skill", None), "key", str(skill_key or "")))
    # review L3: a value already swapped above (the user's next own candidate) is never swapped a second time here
    other = None if picked is not None or replaced else _other_than_corrected(resolved, result, ctx,
                                                                               corrects=bool(corrects))
    if other is not None:
        # fSkills (live 18 Oct: "No, not this one. The other playlist" ran ネオン again, the very one just put on):
        # a backstop for a talker that re-sends the value the user just rejected. A correction whose run found the PC
        # already so changed nothing, so the slot's next own candidate of theirs runs instead (their taste order, the
        # corrected value left out), resolved with the same extras as the first (the grant, the older turns). Never
        # with Yuki's own pick (``picked``: its done line and taste note name that pick)
        again_slots = {**dict(slots or {}), other[0]: other[1]}
        extra: dict[str, Any] = {"earlier": [str(w) for w in earlier if w]} if earlier else {}
        extra["given_from"] = {**(dict(ctx.given_from) if isinstance(ctx.given_from, Mapping) else {}),
                               other[0]: FROM_MENU}
        regranted = granted_values(ctx.grant, again_slots) if ctx.origin == "thinker" else None
        if regranted:
            extra["granted"] = dict(regranted)
        redo = _mem(ctx.memory, "skill_resolve", lambda c: res_mod.resolve(
            resolved.skill.key, again_slots, words, store=_store_of(c) if c is not None else None, live=live,
            again=again, default_step=step, disable_after=disable_after, **extra), timeout_s=1.0) \
            if ctx.memory is not None else None
        if isinstance(redo, Plan):
            ctx.why = ""            # the caller's why was for the value the PC already showed
            result = run(redo, ctx)
    if picked is not None and result.done:
        # the pick played and its done-check saw it: the line names it and why (code-written), and the conversation
        # keeps it ("another one" takes the next; a quick change counts as a skip)
        from yuki.skills import taste

        result.done_line = taste.choice_line(picked.choice, where=_pick_where(result.done_line))
        _mem(ctx.memory, "taste_chosen", lambda c: taste.chosen(_store_of(c), picked.choice, session=ctx.session_id,
                                                                taste_kind=picked.taste_kind), timeout_s=0.5)
    if pending is not None and result.done:
        try:
            offers = pending.wait()          # at most Settings.youtube_feed_budget_s
            if offers and pending.confirm(offers):
                from yuki.skills import youtube_feed

                result.options = [o.name for o in offers]
                result.done_line = f"{result.done_line} {youtube_feed.offer_line(offers)}".strip()
        except Exception:  # noqa: BLE001 - no offer named
            pass
    return result


def _other_than_corrected(resolved: Any, result: SkillResult, ctx: RunContext, *,
                          corrects: bool) -> tuple[str, str] | None:
    """fSkills: ``(slot, the next own candidate's name)`` when a correction (``corrects``) resolved to the very state
    the PC already shows (its run ``already``): the user said "not this one", so the taste slot that named it takes
    the user's next own candidate of that kind (:func:`yuki.skills.taste.top` at this hour, the current one left out;
    else the next name :func:`yuki.skills.taste.candidates` lists). ``None`` otherwise, or with no other candidate.
    fd (final-F D3): left out too, by name: the row the run found already so (its rung's own word,
    :attr:`SkillResult.items`: "Japanese" given, ネオン playing) and what the correction turned down
    (``ctx.turned_down``)."""
    if not corrects or result.outcome != "already" or not isinstance(resolved, Plan) or ctx.memory is None:
        return None
    from yuki.skills import taste

    for slot in resolved.skill.slots:
        value = resolved.values.get(slot.name)
        if not slot.taste or not isinstance(value, Mapping):
            continue
        now_key = str(value.get("key") or "")
        gone = {" ".join(str(n).split()).casefold() for n in
                [value.get("name"), (result.items or {}).get(slot.name), *((ctx.turned_down or {}).get(slot.name)
                                                                          or [])] if str(n or "").strip()}

        def pick(c: Any, kind: str = slot.taste, gone: set[str] = gone, now_key: str = now_key) -> str | None:
            store = _store_of(c)
            for t in taste.top(store, kind, n=6 + len(gone)):
                if t.name and t.entity_key != now_key and " ".join(t.name.split()).casefold() not in gone:
                    return t.name
            for n in taste.candidates(store, kind):
                if n and " ".join(str(n).split()).casefold() not in gone:
                    return str(n)
            return None

        name = _mem(ctx.memory, "taste_other", pick, timeout_s=0.5)
        if name:
            return slot.name, str(name)
    return None


def run_for_voice(hands: Any, args: Mapping[str, Any], words: list[str], *, game: str | None = None,
                  log: Callable[..., Any] | None = None, request_id: str | None = None,
                  settings: Any = None, live: Live | None = None,
                  on_focus: Callable[[], Any] | None = None) -> tuple[bool, str, dict[str, Any]]:
    """The talker's ``run_skill`` (sVoice wires it into ``Hands._skill`` inside ``_acting_run``): ``(ok, report,
    facts)`` (see :meth:`SkillResult.as_voice`). ``game``: the voice's own D9 answer when it has one, used only for
    the live facts at resolve time (review r2 L5: every rung's D17 re-check reads the game itself, fresh). The
    talker's ``corrects`` flag, with the same values, marks the skill's last "done" false (D19). ``on_focus``: the
    acting marker, published only right before a rung that takes focus (sound-only rungs publish none, D18)."""
    key = str(args.get("skill") or "")
    hint, slots = place_arg(key, args, args.get("slots") if isinstance(args.get("slots"), Mapping) else {})
    why, given_from, slots = note_args(key, args, slots)
    ctx = RunContext(origin="voice", words=[str(w) for w in words or []], memory=getattr(hands, "memory", None),
                     hands=hands, log=log, request_id=request_id, on_focus=on_focus,
                     session_id=getattr(getattr(hands, "memory", None), "session_id", None), place=hint, why=why,
                     given_from=given_from)
    result = resolve_and_run(key, slots, ctx.words, ctx, again=bool(args.get("again")), settings=settings, live=live,
                             corrects=bool(args.get("corrects")), game_answer=game)
    ok, report, facts = result.as_voice()
    facts["corrects"] = bool(args.get("corrects"))
    return ok, report, facts


def run_for_thinker(args: Mapping[str, Any], words: list[str], *, memory: Any = None,
                    log: Callable[..., Any] | None = None, request_id: str | None = None,
                    settings: Any = None, game: Callable[[], str] | None = None,
                    verbs: Mapping[str, Callable[[RouteCall], RouteResult]] | None = None,
                    hands: Any = None, earlier: Sequence[str] | None = None,
                    grant: Mapping[str, Any] | None = None) -> SkillResult:
    """The thinker's ``run_skill`` tool (``agent/tools.py``). A hand-off here means the thinker does it itself with
    the skill pinned (the loop pins it); with a game in front the run is held (D17). ``verbs``: the dispatcher's
    wrappers (sWire: a window-touching rung in an app whose "Act" switch is off is refused, as the voice does).
    ``hands``: the new brain's desk hands (``yuki.skills.uia_route.DeskHands``, zFinal): the chat driver's own steps
    type and press through them, each through the consent rule; the old loop passes none. ``earlier``: the
    conversation's older turns, oldest first, for the resolver only (see :func:`resolve_and_run`). ``grant``: the
    brain executor's consent grant for this very step (``Dispatcher.skill_context["grant"]``; the old loop has none):
    used only when :func:`yuki.skills.uia_route.send_grant` finds it is the one these desk hands hold now, for this
    request and skill, a send with its text and person id, and these slots give exactly its text and person
    (:attr:`RunContext.grant`, failure fix-plan Item 11's L2)."""
    hint, slots = place_arg(str(args.get("skill") or ""), args,
                            args.get("slots") if isinstance(args.get("slots"), Mapping) else {})
    why, given_from, slots = note_args(str(args.get("skill") or ""), args, slots)
    ctx = RunContext(origin="thinker", words=[str(w) for w in words or []], memory=memory, log=log,
                     request_id=request_id, game=game, verbs=dict(verbs or {}), hands=hands, place=hint, why=why,
                     given_from=given_from)
    ctx.grant = _thinker_grant(grant, ctx, str(args.get("skill") or ""), slots)
    return resolve_and_run(str(args.get("skill") or ""), slots, ctx.words, ctx, again=bool(args.get("again")),
                           settings=settings, corrects=bool(args.get("corrects")), earlier=earlier)


__all__ = ["BUDGET_S", "PINNED_CHARS", "LINE_UNVERIFIED", "LINE_FAILED", "LINE_OTHER_DEVICE", "LINE_ALREADY",
           "LINE_COMMIT_USER", "TO_PLANNER", "WHY_UNVERIFIED", "WHY_DROPPED", "WHY_ERROR", "REFUSED_BY_STEPS",
           "REPEAT_HANDOFF_S", "SETTLED_REASONS", "FORM_RECALL", "ITEM_KEY", "TRACE_KEEP_S", "UNVERIFIED_RECHECK_S",
           "HANDOFF_ORIGINS", "FROM_UNHEARD", "FROM_GIVEN", "SEEN_MAX", "COMMIT_BY_END", "COMMIT_BY_NEED", "commit_for",
           "run_commit", "served_commit", "ledger_skill", "RouteResult", "RouteCall", "RunContext", "SkillResult",
           "VERB_FUNCS", "open_uri", "pinned", "SEED_KEYS", "plan_seed", "run", "resolve_and_run", "run_for_voice",
           "run_for_thinker", "granted_values", "place_arg", "place_hint", "place_for", "habit_place",
           "HABIT_PLACE_S",
           "CHOICE_SLOTS", "CHOOSE_TIMEOUT_S", "HELD_CALL", "asks_choice", "NOTE_SLOTS", "LINE_WHICH",
           "GUESS_KEEP_S", "note_args", "evidence_words", "FrontRead", "front_now", "front_change", "NOTHING_OUT",
           "RERUN_KEY", "LINE_RERUN"]
