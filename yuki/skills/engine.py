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
6. a slot value the talker or the thinker gave that the resolver dropped (not in the words as heard, e.g. a misheard
   name) and then asked for is never asked back: rung C with the given values (why ``dropped``), where the thinker
   checks them against the words and memory. Sends still go through the consent rule there;
7. a run that broke partway goes to rung C too (why ``error``), unless it may have sent, a rung's data says nothing
   may follow it or it is a relative change; a refusal before anything ran because no skill has that key, skills
   are off or memory was too slow to know the user's usuals tells the caller to do it by steps. Every other non-done
   end is a game in front, a question only the user can answer, a step that is only the user's, a skill that sends
   (rule 8: never a second try), a rung whose data forbids a second try, a relative change that couldn't be
   confirmed, a result that took somewhere else or a state already there, or an eval or a card (they never start an
   agent run).

Logs (D12, content-free through ``yuki.log.events.content_free``): ``skill_resolve``, ``route_try``, ``done_check``,
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
from yuki.skills.resolve import HELD_LINE, Ask, Handoff, Held, Live, Plan, Refused, canonical, done_line

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
SETTLED_REASONS: tuple[str, ...] = ("other_device",)
#: The origins whose unconfirmed or dropped runs go to rung C: the talker and the thinker. A card never starts an agent
#: run (``yuki.ui.nudges``: a hand-off there is dropped) and an eval measures the skill itself.
HANDOFF_ORIGINS: tuple[str, ...] = ("voice", "thinker")
#: The seed's slot sources for a dropped value (rung C, :data:`WHY_DROPPED`): ``unheard`` the slot the resolver
#: dropped and asked for (not in the words as heard), ``given`` another value the caller gave that is not a span of
#: the words either; a given value that is one is ``words``, as the resolver says it.
FROM_UNHEARD = "unheard"
FROM_GIVEN = "given"
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


@dataclass
class RouteCall:
    """What a verb gets: the skill, the rung, its arguments with the slots filled, all the values, the run's context
    and its time left (monotonic deadline)."""

    skill: Skill
    route: Route
    args: dict[str, Any]
    values: dict[str, Any]
    ctx: "RunContext"
    deadline: float


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
    second skill hand's lines in the same turn (sWire r1 L2)."""

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
    focus_taken: bool = False
    #: Running it sends something to other people (its ``sends`` data with these values): an unconfirmed send is
    #: never tried again (rule 8).
    sends: bool = False
    #: Why a run was refused before anything ran (the resolver's code word: ``unknown_skill``, ``skills_off``, ...),
    #: or ``repeat`` on a hand-off that ran no rung because the same thing ran unconfirmed moments ago.
    why: str = ""

    @property
    def done(self) -> bool:
        """Whether "done" may be said: only on a seen result (or a true no-op the check saw already there)."""
        return self.outcome in ("done", "already") and self.verified is True

    def report(self) -> str:
        """For the model that called it (the talker or the thinker): what happened, in plain words. A hand-off always
        says the rest is done by steps and the skill is not run again (x5Fallback: the old "follow it" sent the thinker
        back into the skill that had just failed)."""
        if self.outcome == "already" and self.verified is True:
            return f"Nothing to change: the PC shows it was already so. Say: {self.done_line or self.say}"
        if self.done:
            return f"Done and checked: {self.done_line or self.say}"
        if self.outcome == "asked":
            return f"Ask them: {self.ask}"
        if self.outcome == "held_game":
            return f"Held: a game is in front, nothing was sent. Say: {self.say}"
        if self.outcome == "handed_over":
            why = (self.handoff or {}).get("why")
            if why == TO_PLANNER:
                return ("A step of it would send or join something, so that step was not done; the request goes to "
                        "the planner with the skill pinned.")
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
            # rule 8: a send may have gone already, so a second try could send it twice
            rest = ("It may have sent already: never send it again." if self.sends
                    else "Don't run this skill again: doing it again could do it twice.")
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
                 "settled": self.outcome in ("held_game", "other_device", "unverified", "asked", "already")}
        if self.held:
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
    return "game" if isinstance(details, Mapping) and details.get("refused") == "game" else None


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


def v_open_uri(call: RouteCall) -> RouteResult:
    return _opened(open_uri(str(call.args.get("uri") or "")))


def v_focus(call: RouteCall) -> RouteResult:
    from yuki.actions import launch

    hwnd = _window(call.args)
    if not hwnd:
        return RouteResult(ran=False, outcome="no_window")
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
    if facts.get("refused") == "game" or facts.get("act_off"):
        return RouteResult(ran=done > 0, held="game" if facts.get("refused") == "game" else None, outcome="refused")
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
    :data:`FROM_GIVEN`, the model's own reading of a word nobody heard). An entity's name is the user's own word for a
    thing that may be named otherwise on screen ("Japanese" for ネオン), or a guess at it, so the check never holds
    the thinker to it; a person, an app or a text keeps its check (a message is never confirmed against "any chat")."""
    names = [_missing_slot(skill, missing)]
    names += [k for k, src in (filled or {}).items() if src in (FROM_UNHEARD, FROM_GIVEN)]
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
           "tried": [{"rung": t.get("rung"), "via": t.get("via"), "why": t.get("outcome")} for t in tried][:4],
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
    names = _seen_names(seen)
    if names:
        out["seen"] = names
    return out


#: The keys of a plan seed the brain reads (``yuki.brain.context.seed_block``: skill, goal, slots, done, tried, and
#: x5Fallback's missing and seen; ``yuki.brain.done.seed_done``: done, done_words, baseline; the executor's run row:
#: key).
SEED_KEYS: tuple[str, ...] = ("skill", "key", "app", "goal", "slots", "done", "done_words", "tried", "why", "sends",
                              "baseline", "missing", "seen")


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
        return _run(resolved, ctx, started, state)
    except Exception as exc:  # noqa: BLE001 - a broken run is an error outcome, never a crash
        key = getattr(getattr(resolved, "skill", None), "key", None) or str(getattr(resolved, "skill", "") or "")
        _emit(ctx, "skill_run", skill=key, result="error", error=type(exc).__name__)
        handed = _error_handoff(resolved, ctx, started, state)
        if handed is not None:
            return handed
        _ledger_finish(ctx, state.get("run_key"), outcome="error", checked=None, error=type(exc).__name__,
                       acted=bool(state.get("acted")))
        return SkillResult(skill=key, outcome="error", say=LINE_FAILED,
                           ms=round((ctx.clock() - started) * 1000.0, 1))


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


def _note_false_done(resolved: Any, ctx: RunContext, *, corrects: bool, again: bool) -> str | None:
    """D19's other false-done source (review r2 M2), before the new run starts: the user corrected the skill's last
    run (``corrects``), or asked the same skill again with the same absolute values (not ``again``, in another
    request) within :data:`yuki.skills.registry.FALSE_DONE_S` of a run that ended "done": that run is marked a false
    done in the ledger (``recheck = 0``: the scorecard's false-done count and the rung's trust see it) and logged.
    Returns the run key marked."""
    if ctx.memory is None or ctx.origin == "eval" or isinstance(resolved, Refused):
        return None
    skill = getattr(resolved, "skill", None)
    key = skill.key if isinstance(skill, Skill) else str(skill or "")
    if not key:
        return None
    absolute = None
    if not corrects:
        if again or not isinstance(skill, Skill):
            return None
        absolute = _absolute_values(skill, getattr(resolved, "values", None))
        if absolute is None:
            return None
        # a repeat is ledgered where its run would be (lCore review r2 L9): a learned skill asked with another key
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
        target = key
        if len(keys) > 1:
            newest = -1.0
            for k in keys:
                rows = registry.last_runs(store, k, since=time.time() - registry.FALSE_DONE_S - 30.0, limit=1)
                at = float(rows[0].get("t0") or 0.0) if rows else -1.0
                if at > newest:
                    target, newest = k, at
        digest = None if corrects else registry.values_digest(store, target, absolute)
        return registry.false_done_after(store, target, digest=digest, corrects=corrects, request_id=ctx.request_id)

    marked = _mem(ctx.memory, "skill_false_done", go, timeout_s=0.5)
    if not marked:
        return None
    _emit(ctx, "done_recheck", skill=key, result="false_done", recheck="user",
          reason="correction" if corrects else "repeat", origin=ctx.origin)
    return str(marked)


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
        return _simple(skill.key, "refused", ctx, started, say=LINE_COMMIT_USER if commit == "user" else LINE_FAILED)
    return None


def _run(resolved: Any, ctx: RunContext, started: float, state: dict[str, Any] | None = None) -> SkillResult:
    if isinstance(resolved, Ask):
        _emit(ctx, "skill_resolve", skill=resolved.skill, result="asked", slot_source="ask")
        return _simple(resolved.skill, "asked", ctx, started, ask=resolved.question, options=list(resolved.options),
                       say=resolved.question)
    if isinstance(resolved, Held):
        _emit(ctx, "skill_resolve", skill=resolved.skill, result="held_game", gate="game")
        return _simple(resolved.skill, "held_game", ctx, started, held="game", say=resolved.say)
    if isinstance(resolved, Refused):
        _emit(ctx, "skill_resolve", skill=resolved.skill, result="refused", reason=resolved.why)
        return _simple(resolved.skill, "refused", ctx, started,
                       say=LINE_MEMORY_SLOW if resolved.why == "memory_slow" else LINE_FAILED,
                       why=str(resolved.why or ""))
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
    t0 = time.time()
    run_app = _first_app(skill, plan.routes, plan.values)
    run_key = _ledger_start(ctx, skill, plan.values, plan.filled, t0, app=run_app)
    _emit(ctx, "skill_resolve", skill=skill.key, result="plan", routes=len(plan.routes),
          slot_source=sorted(set(plan.filled.values())), slots=len(plan.filled), named=len(plan.named),
          dropped=len(plan.dropped), dropped_slots=_slot_flags(plan.dropped), skipped=len(plan.skipped),
          origin=ctx.origin)
    deadline = started + float(ctx.budget_s)
    tried: list[dict[str, Any]] = []
    state.update(run_key=run_key, tried=tried, acted=False)
    focus_taken = False
    acted = False
    sends = sends_with(skill, plan.values)
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
        call = RouteCall(skill, route, args, dict(plan.values), ctx, deadline)
        base = _baseline(route, plan.values, ctx)
        r0 = ctx.clock()
        try:
            res = fn(call) if fn is not None else RouteResult(ran=False, outcome="no_verb")
        except Exception as exc:  # noqa: BLE001
            res = RouteResult(ran=None, outcome="error", facts={"error_kind": type(exc).__name__})
        route_ms = round((ctx.clock() - r0) * 1000.0, 1)
        app_done = acted_app(skill, route, args, res)
        # IF-seed (x5Fallback): a rung that found no item by the name asked says which slot, and what it read instead
        missing = missing or _missing_slot(skill, (res.facts or {}).get("missing")
                                           if isinstance(res.facts, Mapping) else None)
        if isinstance(res.facts_ram, Mapping) and res.facts_ram.get("seen"):
            seen = _seen_names([*seen, *(res.facts_ram.get("seen") or [])])
        if res.held:
            tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": "held_game"})
            _emit(ctx, "route_try", skill=skill.key, route=route.key, result="held_game", ms=route_ms)
            if res.ran:
                acted = state["acted"] = True
                if sends or not route.retry_after_acting:
                    # it acted before the game check refused the rest: nothing else is tried (never twice)
                    _ledger_finish(ctx, run_key, outcome="held_game", checked=None, route=route, ms=route_ms,
                                   app=app_done, acted=True)
                    return _simple(skill.key, "held_game", ctx, started, held="game", say=HELD_LINE, tried=tried,
                                   run_key=run_key, route=route.key)
            continue
        if res.outcome == TO_PLANNER:
            # bSkill (bTalk merge note 4): the consent rule stopped a step of this rung that would send or join, with
            # no plan behind it, before that step ran: never "exactly this?" and never the next rung (it would reach
            # the same step); rung C, the plan seed, where the planner's consent rule and send checks decide
            acted = acted or bool(res.ran)
            tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": TO_PLANNER})
            _ledger_try(ctx, run_key, route, None, "refused", route_ms)
            _emit(ctx, "route_try", skill=skill.key, route=route.key, result=TO_PLANNER, ms=route_ms)
            return _to_planner(skill, plan, tried, ctx, started, run_key=run_key, acted=acted, missing=missing,
                               seen=seen)
        if res.ask:
            _ledger_finish(ctx, run_key, outcome="asked", checked=None, route=route, ms=route_ms, app=app_done,
                           acted=acted)
            _emit(ctx, "route_try", skill=skill.key, route=route.key, result="asked", ms=route_ms)
            return _simple(skill.key, "asked", ctx, started, ask=res.ask, options=list(res.options), say=res.ask,
                           run_key=run_key, route=route.key, tried=tried)
        if res.outcome == "other_device":
            say = res.say or LINE_OTHER_DEVICE.format(app=_app_title(skill.app))
            _ledger_finish(ctx, run_key, outcome="other_device", checked=None, route=route, ms=route_ms,
                           app=app_done, acted=acted)
            _emit(ctx, "route_try", skill=skill.key, route=route.key, result="other_device", ms=route_ms)
            return _simple(skill.key, "other_device", ctx, started, say=say, run_key=run_key, route=route.key,
                           tried=tried)
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
            tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": res.outcome or "no"})
            _ledger_try(ctx, run_key, route, None, "failed", route_ms)
            _emit(ctx, "route_try", skill=skill.key, route=route.key, result=res.outcome or "not_run", ms=route_ms,
                  observed=res.facts)
            continue
        acted = state["acted"] = True
        # review r2 M3: once a rung acted, its check gets its own within_s (<= MAX_WAIT_S) even past the request
        # budget: a check cut short before the app could react is no contradiction (a False there would count against
        # a working rung and run the next one, doing a relative change twice)
        waited = _wait(route.done, probe_ctx, None, ctx)
        _emit(ctx, "route_try", skill=skill.key, route=route.key, result=res.outcome or "ran", ms=route_ms,
              observed=res.facts)
        _emit(ctx, "done_check", skill=skill.key, route=route.key,
              check=(route.done.probe if route.done else "read_back"),
              result={True: "done", False: "contradicted", None: "unverified"}[waited.verdict], ms=waited.ms,
              reads=waited.reads, observed=waited.evidence)
        if waited.verdict is True:
            line = done_line(skill, plan.values, plan.named, route=route, say=res.say)
            _ledger_finish(ctx, run_key, outcome="done", checked=True, route=route, ms=route_ms,
                           value=res.value, entity_digest=res.entity_digest, app=app_done,
                           evidence={"numbers": _numbers(plan.values), "check": waited.evidence})
            result = SkillResult(skill=skill.key, outcome="done", verified=True, done_line=line, say=line,
                                 route=route.key, tried=tried, ask_keep=plan.ask_keep, run_key=run_key,
                                 values=dict(plan.values), focus_taken=focus_taken)
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
                                        "evidence": {"numbers": _numbers(plan.values), "check": waited.evidence}})
            say = res.unverified_line or LINE_UNVERIFIED
            _ledger_finish(ctx, run_key, outcome="unverified", checked=None, route=route, ms=route_ms,
                           value=res.value, app=app_done,
                           evidence={"numbers": _numbers(plan.values), "check": waited.evidence})
            return _simple(skill.key, "unverified", ctx, started, verified=None, say=say, route=route.key,
                           tried=tried, run_key=run_key, values=dict(plan.values), focus_taken=focus_taken,
                           sends=sends)
        tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": "contradicted"})
        if sends or not route.retry_after_acting:
            # review M3: a rung that acted on a skill that sends (or one whose data says so) is never followed by
            # another acting rung or a hand-off: the message may have gone anyway, and a second try would send twice
            _ledger_finish(ctx, run_key, outcome="failed", checked=False, route=route, ms=route_ms, app=app_done,
                           evidence={"numbers": _numbers(plan.values), "check": waited.evidence})
            return _simple(skill.key, "failed", ctx, started, say=LINE_SEND_FAILED if sends else LINE_FAILED,
                           route=route.key, tried=tried, run_key=run_key, values=dict(plan.values),
                           focus_taken=focus_taken, sends=sends)
        _ledger_try(ctx, run_key, route, False, "failed", route_ms)
    # every rung failed or could not run
    game = ctx.game_now()
    if game:
        _ledger_finish(ctx, run_key, outcome="held_game", checked=None, route=None, acted=acted)
        return _simple(skill.key, "held_game", ctx, started, held="game", say=HELD_LINE, tried=tried,
                       run_key=run_key)
    if plan.handoff and skill.handoff and ctx.origin != "eval":
        return _handoff(skill, plan.values, plan.filled, tried, ctx, started, why="routes_failed", run_key=run_key,
                        acted=acted, routes=list(plan.routes), missing=missing, seen=seen)
    # the run's own row names no rung: each rung's verdict is already in its try row (never counted twice)
    _ledger_finish(ctx, run_key, outcome="failed", checked=False if acted else None, route=None)
    return _simple(skill.key, "failed", ctx, started, say=LINE_FAILED, tried=tried, run_key=run_key)


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
             ledger: Mapping[str, Any] | None = None, repeat: bool = False) -> SkillResult:
    """Rung C: never with a game in front (D17(2)), re-read right here. The pinned skill is the old loop's pin and the
    new brain's plan seed alike (:func:`pinned`), held to the done-check of the routes this run took (``routes``),
    with IF-seed's ``missing`` (its value left out of that check) and ``seen``. ``unconfirmed``: the last rung acted
    and its check couldn't read (x5Fallback): the run's row is never marked contradicted for it. ``partial``: the
    values are only what the caller gave (a dropped value handed over). ``ledger``: more of the run row's facts (the
    rung that acted, its value, app and the check's evidence: content-free). ``repeat``: the same thing handed over
    unconfirmed moments ago, so no rung ran this time (:func:`_recent_handoff`)."""
    game = ctx.game_now()
    if game:
        _ledger_finish(ctx, run_key, outcome="held_game", checked=None, acted=acted)
        return _simple(skill.key, "held_game", ctx, started, held="game", say=HELD_LINE, tried=tried,
                       run_key=run_key)
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
    if why in (WHY_UNVERIFIED, WHY_ERROR) and ctx.origin in HANDOFF_ORIGINS:
        # a second run of the same thing in the next moments goes straight here (it could do it twice)
        _note_handoff(skill, values, why, tried, missing, seen)
    _emit(ctx, "skill_handoff", skill=skill.key, result="handed_over", reason=why, tried=len(tried),
          missing_slots=_slot_flags([pin["missing"]] if pin.get("missing") else []), seen=len(pin.get("seen") or []),
          repeat=True if repeat else None, origin=ctx.origin)
    return _simple(skill.key, "handed_over", ctx, started, handoff=pin, tried=tried, run_key=run_key,
                   values=dict(values), focus_taken=focus_taken, why="repeat" if repeat else "")


def _to_planner(skill: Skill, plan: Plan, tried: list[dict[str, Any]], ctx: RunContext, started: float, *,
                run_key: str | None, acted: bool, missing: str | None = None, seen: Any = ()) -> SkillResult:
    """A rung whose step the consent rule handed over (:data:`TO_PLANNER`; that step did not run): rung C with the
    plan seed when the skill's data lets it hand over (never with a game in front: :func:`_handoff` reads it again).
    A skill whose data says it is never handed over (it would only search), or an eval run, stops there with the
    user's-step line: nothing more is done, and no next rung tries the same step."""
    if skill.handoff and ctx.origin != "eval":
        return _handoff(skill, plan.values, plan.filled, tried, ctx, started, why=TO_PLANNER, run_key=run_key,
                        acted=acted, routes=list(plan.routes), missing=missing, seen=seen)
    _ledger_finish(ctx, run_key, outcome="refused", checked=None, acted=acted)
    return _simple(skill.key, "refused", ctx, started, say=LINE_COMMIT_USER, tried=tried, run_key=run_key)


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
    calls = _mem(memory, "current_calls", lambda c: c.current_calls(), timeout_s=0.3)
    return bool(calls) if isinstance(calls, list) else False


def _live_now(game: str, hands: Any = None, *, media: bool = True, skill: Skill | None = None,
              memory: Any = None) -> Live:
    """The live facts at request time, read once on request (no polling): the game in front, the media sessions
    playing (only when a slot of the skill fills from them), whether a call is live (only when a slot asks in a
    call), and the skill's window-state reader for the ``game = "states"`` rule (its driver's hook)."""
    playing: tuple[str, ...] = ()
    try:
        if not media:
            raise LookupError
        from yuki.perception.system import media_sessions
        from yuki.actions.media import app_key_of

        playing = tuple(dict.fromkeys(app_key_of(str(s.get("app_id") or "")) for s in media_sessions(timeout_s=0.2)
                                      if str(s.get("status") or "") == "playing" and s.get("app_id")))
    except Exception:  # noqa: BLE001 - nothing known playing
        playing = ()
    wants_call = skill is not None and any(s.ask_in_call for s in skill.slots)
    in_call = _in_call(hands, memory) if wants_call else False
    states = skill is not None and any(r.game == "states" for r in skill.routes)
    return Live(game=game, playing=playing, in_call=in_call,
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


def resolve_and_run(skill_key: str, slots: Mapping[str, Any] | None, words: list[str], ctx: RunContext, *,
                    again: bool = False, live: Live | None = None, settings: Any = None, corrects: bool = False,
                    game_answer: str | None = None, earlier: Sequence[str] | None = None) -> SkillResult:
    """Resolve (on the memory's own store, in one bounded call) and run, for every origin. Live facts default to a
    fresh read (``game_answer``: the caller's own D9 answer for them; every rung still reads the game again).
    ``earlier``: the conversation's older turns, oldest first (x6Resolver merge note M1): only ever passed to the
    resolver as ``earlier=``, which validates a given value against them and never fills from them; never appended to
    ``words``, whose newest turn decides a named app.
    ``corrects``: the user corrected the skill's last run; with it, or the same skill and absolute values asked again
    within 60 s of a "done" (not ``again``), that run is marked a false done first (D19, review r2 M2)."""
    from yuki.skills import enabled, setting
    from yuki.skills import resolve as res_mod

    if not enabled(settings):
        # the off switch (Settings.skills = false): the old paths run instead; nothing here acts
        return run(Refused(str(skill_key or ""), "skills_off"), ctx)
    if live is None:
        from yuki.skills.catalog import load

        skill = load().get(str(skill_key or ""))
        wants_media = bool(skill and any(s.live == "playing" for s in skill.slots))
        live = _live_now(str(game_answer) if game_answer is not None else ctx.game_now(), ctx.hands,
                         media=wants_media, skill=skill, memory=ctx.memory)
    step = float(setting(settings, "skills_volume_step") or 10)
    disable_after = int(setting(settings, "skills_disable_after") or 2)
    catalog: dict[str, Any] = {}

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
        extra = {"earlier": [str(w) for w in earlier if w]} if earlier else {}
        return res_mod.resolve(key, slots, words, store=_store_of(client) if client is not None else None,
                               live=live, again=again, default_step=step, disable_after=disable_after, **extra)

    resolved = _mem(ctx.memory, "skill_resolve", go, timeout_s=1.0) if ctx.memory is not None else go(None)
    if resolved is None and ctx.memory is not None and ctx.origin in ("thinker", "card"):
        # sVoice r2 M3 for the thinker and card origins (sWire): memory didn't answer the resolve in time, so the
        # user's usuals, the ledger's disabled rungs and told values are unknown: nothing runs, said plainly (the voice
        # does the same through its own longer wait, hands._ResolveWait)
        return run(Refused(str(skill_key or ""), "memory_slow"), ctx)
    if resolved is None:
        resolved = go(None)
    _note_false_done(resolved, ctx, corrects=bool(corrects), again=bool(again))
    handed = _dropped_handoff(resolved, slots, words, catalog.get("loaded"), ctx)
    if handed is not None:
        # x5Fallback (IF-handoff): a value the caller gave that the words as heard don't hold is never asked back
        _emit(ctx, "skill_resolve", skill=handed.skill.key, result="handed_over", reason=WHY_DROPPED,
              slot_source=sorted(set(handed.filled.values())), slots=len(handed.filled),
              dropped_slots=_slot_flags([k for k, v in handed.filled.items() if v == FROM_UNHEARD]),
              origin=ctx.origin)
        resolved = handed
    # tTaste merge note 1 (phase T): a run that opens YouTube's home with no game in front ranks the fresh uploads on a
    # short thread of their own while the page opens; they are named (and noted, so "the B one" can pick one) only
    # after the run's checked done and a non-empty wait; a run that fails, asks or times out names nothing. A card's
    # run never offers. Nothing plays here.
    pending = None
    if isinstance(resolved, Plan) and (resolved.skill.capability or resolved.skill.key) == "web.open_page" \
            and ctx.origin != "card":
        try:
            from yuki.skills import youtube_feed

            pending = youtube_feed.start(ctx.memory, resolved.values, game=live.game or None, log=ctx.log,
                                         settings=settings)
        except Exception:  # noqa: BLE001 - the offer is extra; the run goes on without it
            pending = None
    result = run(resolved, ctx)
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


def run_for_voice(hands: Any, args: Mapping[str, Any], words: list[str], *, game: str | None = None,
                  log: Callable[..., Any] | None = None, request_id: str | None = None,
                  settings: Any = None, live: Live | None = None,
                  on_focus: Callable[[], Any] | None = None) -> tuple[bool, str, dict[str, Any]]:
    """The talker's ``run_skill`` (sVoice wires it into ``Hands._skill`` inside ``_acting_run``): ``(ok, report,
    facts)`` (see :meth:`SkillResult.as_voice`). ``game``: the voice's own D9 answer when it has one, used only for
    the live facts at resolve time (review r2 L5: every rung's D17 re-check reads the game itself, fresh). The
    talker's ``corrects`` flag marks the skill's last "done" false (D19). ``on_focus``: the acting marker, published
    only right before a rung that takes focus (sound-only rungs publish none, D18)."""
    ctx = RunContext(origin="voice", words=[str(w) for w in words or []], memory=getattr(hands, "memory", None),
                     hands=hands, log=log, request_id=request_id, on_focus=on_focus,
                     session_id=getattr(getattr(hands, "memory", None), "session_id", None))
    result = resolve_and_run(str(args.get("skill") or ""), args.get("slots") if isinstance(args.get("slots"), Mapping)
                             else {}, ctx.words, ctx, again=bool(args.get("again")), settings=settings, live=live,
                             corrects=bool(args.get("corrects")), game_answer=game)
    ok, report, facts = result.as_voice()
    facts["corrects"] = bool(args.get("corrects"))
    return ok, report, facts


def run_for_thinker(args: Mapping[str, Any], words: list[str], *, memory: Any = None,
                    log: Callable[..., Any] | None = None, request_id: str | None = None,
                    settings: Any = None, game: Callable[[], str] | None = None,
                    verbs: Mapping[str, Callable[[RouteCall], RouteResult]] | None = None,
                    hands: Any = None, earlier: Sequence[str] | None = None) -> SkillResult:
    """The thinker's ``run_skill`` tool (``agent/tools.py``). A hand-off here means the thinker does it itself with
    the skill pinned (the loop pins it); with a game in front the run is held (D17). ``verbs``: the dispatcher's
    wrappers (sWire: a window-touching rung in an app whose "Act" switch is off is refused, as the voice does).
    ``hands``: the new brain's desk hands (``yuki.skills.uia_route.DeskHands``, zFinal): the chat driver's own steps
    type and press through them, each through the consent rule; the old loop passes none. ``earlier``: the
    conversation's older turns, oldest first, for the resolver only (see :func:`resolve_and_run`)."""
    ctx = RunContext(origin="thinker", words=[str(w) for w in words or []], memory=memory, log=log,
                     request_id=request_id, game=game, verbs=dict(verbs or {}), hands=hands)
    return resolve_and_run(str(args.get("skill") or ""), args.get("slots") if isinstance(args.get("slots"), Mapping)
                           else {}, ctx.words, ctx, again=bool(args.get("again")), settings=settings,
                           corrects=bool(args.get("corrects")), earlier=earlier)


__all__ = ["BUDGET_S", "PINNED_CHARS", "LINE_UNVERIFIED", "LINE_FAILED", "LINE_OTHER_DEVICE", "LINE_ALREADY",
           "LINE_COMMIT_USER", "TO_PLANNER", "WHY_UNVERIFIED", "WHY_DROPPED", "WHY_ERROR", "REFUSED_BY_STEPS",
           "REPEAT_HANDOFF_S", "SETTLED_REASONS",
           "HANDOFF_ORIGINS", "FROM_UNHEARD", "FROM_GIVEN", "SEEN_MAX", "COMMIT_BY_END", "COMMIT_BY_NEED", "commit_for",
           "run_commit", "served_commit", "ledger_skill", "RouteResult", "RouteCall", "RunContext", "SkillResult",
           "VERB_FUNCS", "open_uri", "pinned", "SEED_KEYS", "plan_seed", "run", "resolve_and_run", "run_for_voice",
           "run_for_thinker"]
