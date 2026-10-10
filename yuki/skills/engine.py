"""The skill engine: runs a skill's route ladder, checks the result, keeps the ledger (PLAN.md Phase S; D14, D17-D19).

``run(result_of_resolve, ctx)`` takes a :class:`yuki.skills.resolve.Plan` (or its Ask / Held / Handoff / Refused) and:

1. writes the run's ledger row at ``t0``, before the first step (D18: what changes on screen from then until it is
   done is Yuki's doing, not the user's habit);
2. tries each rung in order. **Right before every rung that takes focus or sends input it reads again whether a game
   is in front** (D17(1)); with one, that rung is skipped, and when no rung that neither takes focus nor sends input is
   left the run is held ("after the round?"), never handed to the thinker (D17(2));
3. runs the rung's verb (the closed list of :data:`yuki.skills.connection.VERBS`: UI Automation patterns by name
   inside containers, the media session, Core Audio, launch arguments and URIs through ``launch.open_url``'s guarded
   ``os.startfile``, a recorded path, a driver), then its done-check (:mod:`yuki.skills.donecheck`): **"done" only on
   True** (D19). A rung that could not run (its control missing, a cold window) passes to the next one; a contradicted
   check counts against the rung and passes on; a check that couldn't read after the rung acted stops the ladder with
   "couldn't confirm" said plainly (another rung would do it twice) and never counts against it;
4. after a "done", D19's second probe runs a few seconds later (on a short-lived thread while it waits, at most 6 s;
   ``recheck="inline"`` in tests) and marks a false done in the ledger when it disagrees;
5. when every rung failed or could not run, rung C: the request goes to the thinker with the skill pinned
   (``handoff`` dict: goal, slots with their sources, the done-check, what was tried), unless a game is in front.

Logs (D12, content-free through ``yuki.log.events.content_free``): ``skill_resolve``, ``route_try``, ``done_check``,
``done_recheck``, ``skill_run``, ``skill_handoff``. ``open_uri`` lives here (L14): an app URI opened by
``yuki.actions.launch.open_url`` with no app, i.e. its guarded ``os.startfile``.
"""

from __future__ import annotations

import importlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from yuki.skills.connection import Check, Route, Skill, fill, fill_value
from yuki.skills.resolve import HELD_LINE, Ask, Handoff, Held, Live, Plan, Refused, canonical, done_line

#: The time a request may take from the words to the done line (PLAN.md §5: p90 <= 5 s).
BUDGET_S = 5.0
#: The pinned skill block's size cap (``agent/frame.py`` renders it, never cut).
PINNED_CHARS = 900
LINE_UNVERIFIED = "I did it, but I couldn't confirm it worked."
LINE_FAILED = "That didn't work."
LINE_OTHER_DEVICE = "{app} is playing on another device, so I left it."
LINE_ALREADY = "That's already how it is."


# ---------------------------------------------------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class RouteResult:
    """What one rung's verb did. ``ran``: True it acted (a pattern called, a command sent, a level set), False it did
    not (nothing found, refused, not built), None it may have (a timeout after sending). ``verified``: its own
    read-back. ``already``: the end state was there before (a true no-op). ``outcome``: a code word. ``facts`` are
    logged (content-free); ``facts_ram`` never leave RAM (a session's title before a command)."""

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
    ``on_focus``: called right before a rung that takes focus (the acting marker; sound-only rungs publish none)."""

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

    def game_now(self) -> str:
        """D9, read now (fails closed: a check that raised counts as a game)."""
        try:
            if self.game is not None:
                return str(self.game() or "")
            from yuki.memory.gamegate import game_in_front

            return str(game_in_front() or "")
        except Exception:  # noqa: BLE001
            return "unknown"


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

    @property
    def done(self) -> bool:
        """Whether "done" may be said: only on a seen result (or a true no-op the check saw already there)."""
        return self.outcome in ("done", "already") and self.verified is True

    def report(self) -> str:
        """For the model that called it (the talker or the thinker): what happened, in plain words."""
        if self.done:
            return f"Done and checked: {self.done_line or self.say}"
        if self.outcome == "asked":
            return f"Ask them: {self.ask}"
        if self.outcome == "held_game":
            return f"Held: a game is in front, nothing was sent. Say: {self.say}"
        if self.outcome == "handed_over":
            return "Its routes didn't get there; the request goes to the thinker with the skill pinned."
        if self.outcome == "unverified":
            return f"It ran, but the PC couldn't confirm the result. Say so plainly: {self.say}"
        if self.outcome == "other_device":
            return f"Not changed: {self.say}"
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
        ctx.log(event, **rec)
    except Exception:  # noqa: BLE001 - a log never breaks a run
        pass


def _numbers(values: Mapping[str, Any]) -> dict[str, float]:
    """The numeric slot values (content-free; the ledger keeps them for "again")."""
    return {k: float(v) for k, v in values.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


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
    if hands is None:
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


def _done_spec(skill: Skill, values: Mapping[str, Any]) -> dict[str, Any] | None:
    """The check the thinker's finish gate reads for a pinned skill: the first route done-check (else re-check) that
    is not the route's own read-back, its templates filled."""
    for r in skill.routes:
        for c in (r.done, r.recheck):
            if c is not None and c.probe != "read_back":
                try:
                    return fill_value(c.as_dict(), values)
                except KeyError:
                    continue
    return None


def pinned(skill: Skill, values: Mapping[str, Any], filled: Mapping[str, str], tried: list[dict[str, Any]],
           *, why: str = "") -> dict[str, Any]:
    """The pinned skill handed to the thinker (rung C; map-skills S3.4): its goal, the slots with their sources, what
    "done" means (words and the machine check), what was already tried. At most :data:`PINNED_CHARS` as rendered."""
    slots = {k: {"value": v, "from": filled.get(k, "")} for k, v in canonical(values).items()}
    out = {"key": skill.key, "app": skill.app, "goal": skill.menu, "slots": slots,
           "done_words": fill(skill.done_line, values) or "", "done": _done_spec(skill, values),
           "tried": [{"rung": t.get("rung"), "via": t.get("via"), "why": t.get("outcome")} for t in tried][:4],
           "why": why}
    while len(json.dumps(out, ensure_ascii=False, default=str)) > PINNED_CHARS and out["tried"]:
        out["tried"] = out["tried"][:-1]
    return out


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
    try:
        return _run(resolved, ctx, started)
    except Exception as exc:  # noqa: BLE001 - a broken run is an error outcome, never a crash
        key = getattr(getattr(resolved, "skill", None), "key", None) or str(getattr(resolved, "skill", "") or "")
        _emit(ctx, "skill_run", skill=key, result="error", error=type(exc).__name__)
        return SkillResult(skill=key, outcome="error", say=LINE_FAILED,
                           ms=round((ctx.clock() - started) * 1000.0, 1))


def _ledger_start(ctx: RunContext, skill: Skill, values: Mapping[str, Any], filled: Mapping[str, str],
                  t0: float) -> str | None:
    from yuki.skills import registry

    key = registry.new_run_key()
    got = _mem(ctx.memory, "skill_run_start",
               lambda c: registry.start_run(_store_of(c), skill=skill, origin=ctx.origin, t0=t0, run_key=key,
                                            request_id=ctx.request_id, session_id=ctx.session_id, filled=filled),
               timeout_s=0.5)
    return got or (key if ctx.memory is not None else None)


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


def _run(resolved: Any, ctx: RunContext, started: float) -> SkillResult:
    if isinstance(resolved, Ask):
        _emit(ctx, "skill_resolve", skill=resolved.skill, result="asked", slot_source="ask")
        return _simple(resolved.skill, "asked", ctx, started, ask=resolved.question, options=list(resolved.options),
                       say=resolved.question)
    if isinstance(resolved, Held):
        _emit(ctx, "skill_resolve", skill=resolved.skill, result="held_game", gate="game")
        return _simple(resolved.skill, "held_game", ctx, started, held="game", say=resolved.say)
    if isinstance(resolved, Refused):
        _emit(ctx, "skill_resolve", skill=resolved.skill, result="refused", reason=resolved.why)
        return _simple(resolved.skill, "refused", ctx, started, say=LINE_FAILED)
    if isinstance(resolved, Handoff):
        return _handoff(resolved.skill, resolved.values, resolved.filled, [], ctx, started, why=resolved.why,
                        run_key=None)
    if not isinstance(resolved, Plan):
        return _simple(str(resolved or ""), "refused", ctx, started, say=LINE_FAILED)
    plan = resolved
    skill = plan.skill
    t0 = time.time()
    run_key = _ledger_start(ctx, skill, plan.values, plan.filled, t0)
    _emit(ctx, "skill_resolve", skill=skill.key, result="plan", routes=len(plan.routes),
          slot_source=sorted(set(plan.filled.values())), slots=len(plan.filled), named=len(plan.named),
          dropped=len(plan.dropped), skipped=len(plan.skipped), origin=ctx.origin)
    deadline = started + float(ctx.budget_s)
    tried: list[dict[str, Any]] = []
    focus_taken = False
    acted = False
    for route in plan.routes:
        if route.via == "agent":
            break
        if route.needs_focus:
            game = ctx.game_now()
            if game:
                # D17(1): read again right before a step that takes focus or sends input
                tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": "game"})
                _emit(ctx, "route_try", skill=skill.key, route=route.key, result="held_game", gate="game")
                continue
            if ctx.on_focus is not None:
                try:
                    ctx.on_focus()
                except Exception:  # noqa: BLE001
                    pass
            focus_taken = True
        try:
            args = fill_value(dict(route.args), plan.values)
        except KeyError:
            tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": "no_value"})
            continue
        fn = ctx.verbs.get(route.via) or VERB_FUNCS.get(route.via)
        call = RouteCall(skill, route, args, dict(plan.values), ctx, deadline)
        r0 = ctx.clock()
        try:
            res = fn(call) if fn is not None else RouteResult(ran=False, outcome="no_verb")
        except Exception as exc:  # noqa: BLE001
            res = RouteResult(ran=None, outcome="error", facts={"error_kind": type(exc).__name__})
        route_ms = round((ctx.clock() - r0) * 1000.0, 1)
        if res.held:
            tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": "held_game"})
            _emit(ctx, "route_try", skill=skill.key, route=route.key, result="held_game", ms=route_ms)
            if res.ran:
                acted = True
            continue
        if res.ask:
            _ledger_finish(ctx, run_key, outcome="asked", checked=None, route=route, ms=route_ms)
            _emit(ctx, "route_try", skill=skill.key, route=route.key, result="asked", ms=route_ms)
            return _simple(skill.key, "asked", ctx, started, ask=res.ask, options=list(res.options), say=res.ask,
                           run_key=run_key, route=route.key, tried=tried)
        if res.outcome == "other_device":
            say = res.say or LINE_OTHER_DEVICE.format(app=_app_title(skill.app))
            _ledger_finish(ctx, run_key, outcome="other_device", checked=None, route=route, ms=route_ms)
            _emit(ctx, "route_try", skill=skill.key, route=route.key, result="other_device", ms=route_ms)
            return _simple(skill.key, "other_device", ctx, started, say=say, run_key=run_key, route=route.key,
                           tried=tried)
        probe_ctx = _probe_ctx(ctx, t0, plan.values, res)
        if res.already:
            # a true no-op: the end state was already there; still checked before it is said (never a fresh "done")
            waited = _wait(route.done, probe_ctx, deadline - ctx.clock(), ctx)
            if waited.verdict is True:
                line = res.say or LINE_ALREADY
                _ledger_finish(ctx, run_key, outcome="already", checked=None, route=route, ms=route_ms,
                               evidence={"numbers": _numbers(plan.values)})
                _emit(ctx, "done_check", skill=skill.key, route=route.key, result="already", ms=waited.ms)
                return _simple(skill.key, "already", ctx, started, verified=True, done_line=line, say=line,
                               run_key=run_key, route=route.key, tried=tried, values=dict(plan.values))
        if res.ran is False:
            tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": res.outcome or "no"})
            _ledger_try(ctx, run_key, route, None, "failed", route_ms)
            _emit(ctx, "route_try", skill=skill.key, route=route.key, result=res.outcome or "not_run", ms=route_ms,
                  observed=res.facts)
            continue
        acted = True
        waited = _wait(route.done, probe_ctx, deadline - ctx.clock(), ctx)
        _emit(ctx, "route_try", skill=skill.key, route=route.key, result=res.outcome or "ran", ms=route_ms,
              observed=res.facts)
        _emit(ctx, "done_check", skill=skill.key, route=route.key,
              check=(route.done.probe if route.done else "read_back"),
              result={True: "done", False: "contradicted", None: "unverified"}[waited.verdict], ms=waited.ms,
              reads=waited.reads, observed=waited.evidence)
        if waited.verdict is True:
            line = done_line(skill, plan.values, plan.named, route=route, say=res.say)
            _ledger_finish(ctx, run_key, outcome="done", checked=True, route=route, ms=route_ms,
                           value=res.value, entity_digest=res.entity_digest,
                           evidence={"numbers": _numbers(plan.values), "check": waited.evidence})
            result = SkillResult(skill=skill.key, outcome="done", verified=True, done_line=line, say=line,
                                 route=route.key, tried=tried, ask_keep=plan.ask_keep, run_key=run_key,
                                 values=dict(plan.values), focus_taken=focus_taken)
            _after_done(ctx, skill, route, plan, probe_ctx, result)
            result.ms = round((ctx.clock() - started) * 1000.0, 1)
            _emit(ctx, "skill_run", skill=skill.key, route=route.key, result="done", ms=result.ms,
                  origin=ctx.origin, rung=route.rung)
            return result
        if waited.verdict is None:
            # it acted and the PC couldn't confirm: said plainly, never counted against it; no second rung (twice)
            say = res.unverified_line or LINE_UNVERIFIED
            _ledger_finish(ctx, run_key, outcome="unverified", checked=None, route=route, ms=route_ms,
                           value=res.value, evidence={"numbers": _numbers(plan.values), "check": waited.evidence})
            return _simple(skill.key, "unverified", ctx, started, verified=None, say=say, route=route.key,
                           tried=tried, run_key=run_key, values=dict(plan.values), focus_taken=focus_taken)
        tried.append({"route": route.key, "rung": route.rung, "via": route.via, "outcome": "contradicted"})
        _ledger_try(ctx, run_key, route, False, "failed", route_ms)
    # every rung failed or could not run
    game = ctx.game_now()
    if game:
        _ledger_finish(ctx, run_key, outcome="held_game", checked=None, route=None)
        return _simple(skill.key, "held_game", ctx, started, held="game", say=HELD_LINE, tried=tried,
                       run_key=run_key)
    if plan.handoff and skill.handoff and ctx.origin != "eval":
        return _handoff(skill, plan.values, plan.filled, tried, ctx, started, why="routes_failed", run_key=run_key,
                        acted=acted)
    # the run's own row names no rung: each rung's verdict is already in its try row (never counted twice)
    _ledger_finish(ctx, run_key, outcome="failed", checked=False if acted else None, route=None)
    return _simple(skill.key, "failed", ctx, started, say=LINE_FAILED, tried=tried, run_key=run_key)


def _probe_ctx(ctx: RunContext, t0: float, values: Mapping[str, Any], res: RouteResult) -> Any:
    from yuki.skills.donecheck import ProbeContext

    def memory_call(what: str, fn: Callable[[Any], Any]) -> Any:
        return _mem(ctx.memory, what, fn, timeout_s=0.8)

    return ProbeContext(since=t0, values=dict(values), result=res,
                        memory_call=memory_call if ctx.memory is not None else None, probes=dict(ctx.probes),
                        clock=ctx.clock, sleep=ctx.sleep)


def _wait(check: Check | None, probe_ctx: Any, left_s: float, ctx: RunContext) -> Any:
    from yuki.skills.donecheck import wait

    return wait(check or Check(probe="read_back", within_s=0.0), probe_ctx, deadline_s=max(0.0, left_s))


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


def _handoff(skill: Skill, values: Mapping[str, Any], filled: Mapping[str, str], tried: list[dict[str, Any]],
             ctx: RunContext, started: float, *, why: str, run_key: str | None, acted: bool = False) -> SkillResult:
    """Rung C: never with a game in front (D17(2)), re-read right here."""
    game = ctx.game_now()
    if game:
        _ledger_finish(ctx, run_key, outcome="held_game", checked=None)
        return _simple(skill.key, "held_game", ctx, started, held="game", say=HELD_LINE, tried=tried,
                       run_key=run_key)
    pin = pinned(skill, values, filled, tried, why=why)
    _ledger_finish(ctx, run_key, outcome="handed_over", checked=False if acted else None)
    _emit(ctx, "skill_handoff", skill=skill.key, result="handed_over", reason=why, tried=len(tried))
    return _simple(skill.key, "handed_over", ctx, started, handoff=pin, tried=tried, run_key=run_key,
                   values=dict(values))


# ---------------------------------------------------------------------------------------------------------------------
# Entry points: the voice hands and the thinker's tool
# ---------------------------------------------------------------------------------------------------------------------


def _live_now(game: str, hands: Any = None, *, media: bool = True) -> Live:
    """The live facts at request time, read once on request (no polling): the game in front, the media sessions
    playing (only when a slot of the skill fills from them), whether the voice knows of a live call."""
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
    in_call = bool(getattr(hands, "in_call", False)) if hands is not None else False
    return Live(game=game, playing=playing, in_call=in_call)


def resolve_and_run(skill_key: str, slots: Mapping[str, Any] | None, words: list[str], ctx: RunContext, *,
                    again: bool = False, live: Live | None = None, settings: Any = None) -> SkillResult:
    """Resolve (on the memory's own store, in one bounded call) and run. Live facts default to a fresh read."""
    from yuki.skills import enabled, setting
    from yuki.skills import resolve as res_mod

    if not enabled(settings):
        # the off switch (Settings.skills = false): the old paths run instead; nothing here acts
        return run(Refused(str(skill_key or ""), "skills_off"), ctx)
    if live is None:
        from yuki.skills.connection import load

        skill = load().get(str(skill_key or ""))
        wants_media = bool(skill and any(s.live == "playing" for s in skill.slots))
        live = _live_now(ctx.game_now(), ctx.hands, media=wants_media)
    step = float(setting(settings, "skills_volume_step") or 10)
    disable_after = int(setting(settings, "skills_disable_after") or 2)

    def go(client: Any) -> Any:
        seeded = getattr(client, "_skills_seeded", None)
        if callable(seeded):
            seeded()   # the tray's MemoryClient seeds the registry once per connection version
        return res_mod.resolve(skill_key, slots, words, store=_store_of(client) if client is not None else None,
                               live=live, again=again, default_step=step, disable_after=disable_after)

    resolved = _mem(ctx.memory, "skill_resolve", go, timeout_s=1.0) if ctx.memory is not None else go(None)
    if resolved is None:
        resolved = go(None)
    return run(resolved, ctx)


def run_for_voice(hands: Any, args: Mapping[str, Any], words: list[str], *, game: str | None = None,
                  log: Callable[..., Any] | None = None, request_id: str | None = None,
                  settings: Any = None, live: Live | None = None,
                  on_focus: Callable[[], Any] | None = None) -> tuple[bool, str, dict[str, Any]]:
    """The talker's ``run_skill`` (sVoice wires it into ``Hands._skill`` inside ``_acting_run``): ``(ok, report,
    facts)`` (see :meth:`SkillResult.as_voice`). ``game``: the voice's own D9 answer when it has one (else read
    here); the engine still re-reads it before every focus step. ``on_focus``: the acting marker, published only
    right before a rung that takes focus (sound-only rungs publish none, D18)."""
    ctx = RunContext(origin="voice", words=[str(w) for w in words or []], memory=getattr(hands, "memory", None),
                     hands=hands, log=log, request_id=request_id, on_focus=on_focus,
                     session_id=getattr(getattr(hands, "memory", None), "session_id", None))
    if game is not None:
        first = {"used": False}

        def game_fn() -> str:
            if not first["used"]:
                first["used"] = True
                return str(game or "")
            from yuki.memory.gamegate import game_in_front

            return str(game_in_front() or "")

        ctx.game = game_fn
    result = resolve_and_run(str(args.get("skill") or ""), args.get("slots") if isinstance(args.get("slots"), Mapping)
                             else {}, ctx.words, ctx, again=bool(args.get("again")), settings=settings, live=live)
    ok, report, facts = result.as_voice()
    facts["corrects"] = bool(args.get("corrects"))
    return ok, report, facts


def run_for_thinker(args: Mapping[str, Any], words: list[str], *, memory: Any = None,
                    log: Callable[..., Any] | None = None, request_id: str | None = None,
                    settings: Any = None, game: Callable[[], str] | None = None) -> SkillResult:
    """The thinker's ``run_skill`` tool (``agent/tools.py``). A hand-off here means the thinker does it itself with
    the skill pinned (the loop pins it); with a game in front the run is held (D17)."""
    ctx = RunContext(origin="thinker", words=[str(w) for w in words or []], memory=memory, log=log,
                     request_id=request_id, game=game)
    return resolve_and_run(str(args.get("skill") or ""), args.get("slots") if isinstance(args.get("slots"), Mapping)
                           else {}, ctx.words, ctx, again=bool(args.get("again")), settings=settings)


__all__ = ["BUDGET_S", "PINNED_CHARS", "LINE_UNVERIFIED", "LINE_FAILED", "LINE_OTHER_DEVICE", "LINE_ALREADY",
           "RouteResult", "RouteCall", "RunContext", "SkillResult", "VERB_FUNCS", "open_uri", "pinned", "run",
           "resolve_and_run", "run_for_voice", "run_for_thinker"]
