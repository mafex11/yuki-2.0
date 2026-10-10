"""The one consent rule (brain-design.md §2.5): one table, in code, applied to every act step before it runs.

``check(step, plan, user_words, live, check_verdict) -> allow | ask | refuse | draft``, row by row, the first row that
applies decides:

1. **Game in front** (the Now board's ``game_front``, else the local foreground read, D9; unknown from both counts as a
   game) and the step takes focus, sends input, restores, launches or opens a link: **refuse** ("after the round?").
   The D17 backstop in ``yuki/actions`` still guards underneath. A ``run_skill`` is not refused here: the skill engine
   applies each rung's own game rule (a no-focus rung, the mixer or the media session, may run mid-game).
2. The step's app has its "Act" switch off, or one of its kinds is "just suggest" (``util.PolicyReader``,
   ``util.acts_in_app``, ``util.app_off_line``, ``util.suggest_only``): **refuse**.
3. **Join** (H3): a may-commit step in a window the Now board lists in ``calling_tabs``; a click whose (role, name) is
   a capability's declared join control (catalog data, ``[chat] labels.*.join_voice``: identity, never keywords); or
   the commit check says ``effect = join``: **refuse** (bring the window forward, "you click Join"). The one
   exception: a ``run_skill`` of a capability whose commit word (phase L's D24 table, ``Skill.commit``) is ``words``,
   on the user's plain words (the commit check's ``asked``; decision 7).
4. **Send**: the step sends for certain (``tools.sends_for_certain``: typed with Enter or a line break, or a click on
   a control that is not a button, menu item or split button while Yuki's typed text waits in that window), is a
   ``run_skill`` whose skill sends with these slots, or the commit check says ``effect = send`` (a button, menu item or
   split button click, or a key, in a window where that text waits is may-commit and goes to the check, told the text
   waits, so "type a search, click Search" is not refused as a send; designer's call, review r2 L1): **refuse**
   ("undeclared send") unless the plan declared ``consent.commit = send`` and the step is a sending capability. Then
   allowed only when (a) ``quote`` is a whole-words span of the user's words, (b) ``to`` is named in the quote (memory's
   people index, ``contacts.named_in``, or its words in the quote), (c) the step's text equals ``consent.text`` and is
   itself a span of the user's words (decision 2), (d) the commit check says ``asked``, (e) the user's recent messages
   to that person hold no near-duplicate of the text (``util.similar``, last 3 hours). (c) or (d) failing is **draft**:
   the capability runs with its send slot on a non-sending option, or, for Yuki's own wording, opens the chat with no
   text and the wording is typed without Enter. (e) failing is **ask**. An allowed send runs the countdown first, and
   only with every ``sends_when`` slot on a sending value (a slot left out would be filled with its default, a draft:
   refused so the planner fills it, never counted down or reported as sent).
5. **Lasting**: any other may-commit step (its traits destroy or reach: a committing-role click, a key, a shell, a
   close, a non-web open, a fetch after a file, clipboard or memory read, M9), and a step of a kind the user's policy
   holds below "do it and tell me": the commit check decides (``effect = none`` allow; lasting and asked allow; lasting
   and not asked **ask** its question; the check failed or timed out **ask**, fail closed).
6. Anything else (focus, a web page, scroll, typing without Enter, a list item, tab or link click, reads): **allow**.

Rows 1-4 (a)-(c), (e) are code facts: tool metadata (``yuki.agent.tools.call_traits``), the Now board, capability
data and string spans. Whether the words *ask* for the effect is the commit check's call (``yuki.brain.check``), an
independent model that sees only the user's words and the steps, never the planner's own judgement (H1, H2). No regex,
no keyword list, no app name: strings are only compared with strings.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from yuki.agent.tools import Traits, call_traits, sends_for_certain, tool_traits
from yuki.brain import util

ALLOW, ASK, REFUSE, DRAFT = "allow", "ask", "refuse", "draft"
RESULTS: tuple[str, ...] = (ALLOW, ASK, REFUSE, DRAFT)
#: The plan's declared commit (``consent.commit``, §2.2).
COMMITS: tuple[str, ...] = ("none", "send", "join", "lasting")
#: The commit check's effects (§2.6); every one but ``none`` is lasting.
EFFECTS: tuple[str, ...] = ("none", "send", "join", "delete", "close", "buy", "setting", "other")

#: What the user is told (code lines; voice-shaped).
LINE_GAME = "You're in a game: after the round?"
LINE_JOIN = "{app} is in front: you click Join."
LINE_JOIN_PLAIN = "You click Join."
LINE_UNDECLARED_SEND = ("that step would send something the plan did not declare as a send: send only through the "
                        "message capability with commit send, or leave it as a draft")
#: The same refusal for typing with Enter (``submit`` or a line break): Enter as its own step is checked (row 5), so a
#: search box or an address bar still works; a message still goes through the capability.
LINE_TYPED_ENTER = ("typing with Enter sends what was typed, and the plan declared no send: type it without submit and "
                    "press Enter as its own step (that step is checked), send a message through the message capability "
                    "with commit send, or leave it as a draft")
LINE_NOT_CAPABILITY = "send through the message capability"
LINE_RECIPIENT = "the step sends to someone other than the plan's recipient"
LINE_DRAFTED = "It's typed in; send it when you're ready."
LINE_DUPLICATE = "You sent that to {to} at {at}. Send it again?"
LINE_DUPLICATE_PLAIN = "You sent that recently. Send it again?"
LINE_CHECK_FAILED = "Should I go ahead with that? I couldn't check it was what you asked for."
#: The same question naming the step (``describe_step``'s running words for it: "Closing Slack").
LINE_CHECK_FAILED_STEP = "Should I go ahead with {step}? I couldn't check it was what you asked for."
LINE_SEND_SLOT = ("the send leaves its send slot ({slots}) out: fill it with its sending value, or leave it as a "
                  "draft")

#: A near-duplicate send counts within this many seconds (the user's messages to that person, §2.3).
DUPLICATE_WINDOW_S = 3 * 3600.0

#: The steps whose input takes the screen even with no focus trait of their own (row 1): an app opened, a link
#: opened, a shell (it may start a window), a window shown or restored. Fixed tool names and operations.
_TAKES_SCREEN_TOOLS = frozenset({"launch_app", "open_url", "run_powershell", "open_yuki"})
_TAKES_SCREEN_WINDOW_OPS = frozenset({"restore", "focus", "maximize", "fullscreen"})


# ---------------------------------------------------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class Call:
    """One act step as it will run: the Dispatcher tool (the plan's verb) and its input, and the code facts of its
    target read just before (``role`` and ``name``: the element as the window's read shows it, never the plan's word
    for it; ``app``: the target window's process)."""

    tool: str
    input: dict[str, Any]
    index: int = -1
    hwnd: int | None = None
    app: str = ""
    title: str = ""
    role: str = ""
    name: str = ""


@dataclass
class Live:
    """Code facts at the moment a step runs.

    ``game``: the game in front (``""`` none, its name, ``None`` unknown: counted as a game). ``calling``: the windows
    the Now board lists in ``calling_tabs``. ``typed_pending``: windows holding text Yuki typed and has not sent.
    ``reads``: what this request has read so far (``file``, ``clipboard``, ``memory``: M9). ``join_controls``: the
    (role, name) pairs capabilities declare as their join control, casefolded. ``recent``: the user's own last messages
    to the person the send names (``{"text", "at"}``). ``people_in(text)``: memory's people index, the names of each
    person named in ``text`` (``None``: memory can't say). ``policy``: the autonomy policy reader."""

    game: str | None = ""
    calling: frozenset[int] = frozenset()
    typed_pending: frozenset[int] = frozenset()
    reads: frozenset[str] = frozenset()
    join_controls: frozenset[tuple[str, str]] = frozenset()
    recent: tuple[Mapping[str, Any], ...] = ()
    people_in: Callable[[str], list[list[str]] | None] | None = None
    policy: Any = None
    now: float = field(default_factory=time.time)


@dataclass(frozen=True)
class Verdict:
    """The rule's answer for one step. ``why``: a code word for the logs (content-free). ``line``: what a refusal says;
    ``question``: what an ask asks; ``final``: a refusal that ends the run with its line (a game in front, an app
    switched off, a join) instead of a replan; ``countdown``: an allowed send runs the countdown first;
    ``draft_input``: a draft's capability input (its send slot on a non-sending option, and for Yuki's own wording no
    text); ``type_after``: Yuki's own wording, typed without Enter once the chat is open; ``focus``: a refused join's
    window, brought forward."""

    result: str
    row: int
    why: str
    line: str = ""
    question: str = ""
    final: bool = False
    countdown: bool = False
    draft_input: dict[str, Any] | None = None
    type_after: str = ""
    focus: int | None = None

    @property
    def allowed(self) -> bool:
        return self.result == ALLOW


# ---------------------------------------------------------------------------------------------------------------------
# Plain readers (a plan may be a dict, bCore's dataclass, or a strict-tool JSON object)
# ---------------------------------------------------------------------------------------------------------------------


def field_of(obj: Any, key: str, default: Any = None) -> Any:
    """``obj[key]`` for a mapping, else ``obj.key``; ``default`` when missing or ``None``."""
    if obj is None:
        return default
    value = obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)
    return default if value is None else value


def words(value: Any) -> str:
    return " ".join(str(value or "").split())


def plan_consent(plan: Any) -> dict[str, Any]:
    """The plan's ``consent`` as a plain dict (``commit`` defaulting to ``none``)."""
    c = field_of(plan, "consent", {})
    commit = str(field_of(c, "commit", "none") or "none")
    return {"commit": commit if commit in COMMITS else "none", "quote": words(field_of(c, "quote", "")),
            "to": words(field_of(c, "to", "")), "text": words(field_of(c, "text", ""))}


def in_words(text: str, user_words: Sequence[str]) -> bool:
    """Whether ``text`` stands, whole words in order, in the user's own words: one entry, or all of this request's
    entries read as one (speech finals split a sentence). ``contacts.span_in``: case and punctuation aside."""
    from yuki.memory.contacts import span_in

    text = words(text)
    entries = [words(w) for w in user_words if words(w)]
    if not text or not entries:
        return False
    return any(span_in(text, w) for w in entries) or span_in(text, " ".join(entries))


# ---------------------------------------------------------------------------------------------------------------------
# Skills (catalog data through util's shim)
# ---------------------------------------------------------------------------------------------------------------------


def skill_input(call: Call, plan: Any = None) -> tuple[str, dict[str, Any]]:
    """``(skill key, slots)`` of a ``run_skill`` call: its own ``skill`` and ``slots``, the key falling back to the
    plan's ``route = "skill:<id>"``."""
    key = words(call.input.get("skill"))
    if not key:
        route = words(field_of(plan, "route", ""))
        key = route.split(":", 1)[1].strip() if route.startswith("skill:") else ""
    slots = call.input.get("slots") if isinstance(call.input.get("slots"), Mapping) else {}
    return key, dict(slots)


def _templated_slot(skill: Any, arg: str) -> str:
    """The slot a skill's checks template into a probe argument (``text = "{text}"``, ``person = "{person}"``): the
    capability's own data names which slot carries what the message says and whom it goes to. ``""``: none."""
    from yuki.skills.connection import template_names

    for route in getattr(skill, "routes", ()) or ():
        for check in (getattr(route, "done", None), getattr(route, "recheck", None)):
            value = (getattr(check, "args", None) or {}).get(arg) if check is not None else None
            names = template_names(value) if isinstance(value, str) else []
            if len(names) == 1 and value.strip() == "{" + names[0] + "}":
                return names[0].split(".", 1)[0]
    return ""


def text_slot(skill: Any) -> str:
    """The slot of a sending capability that holds the text it sends (its checks' ``text`` argument)."""
    return _templated_slot(skill, "text")


def person_slot(skill: Any) -> str:
    """The slot of a sending capability that names whom it goes to (its checks' ``person`` argument)."""
    return _templated_slot(skill, "person")


def skill_sends(skill: Any, slots: Mapping[str, Any], *, partial: bool = True) -> bool:
    """Whether running ``skill`` with these slots sends to people (its own ``sends`` / ``sends_when`` data). With
    ``partial`` (the default: is this step a send?) a slot left out counts as sending, the resolver may fill it so;
    without it (does this step send for sure?) every ``sends_when`` slot must hold a sending value. Never raises:
    unreadable data counts as a send when ``partial``, as not sending for sure otherwise."""
    if skill is None:
        return False
    try:
        from yuki.skills.connection import sends_with

        return bool(sends_with(skill, slots, partial=partial))
    except Exception:  # noqa: BLE001 - fail closed both ways
        return bool(getattr(skill, "sends", False)) if partial else False


def send_slots(skill: Any) -> list[str]:
    """The slots a capability's ``sends_when`` data names (``send``): code words for a mismatch line."""
    return [str(c.get("slot")) for c in getattr(skill, "sends_when", ()) or () if isinstance(c, Mapping)
            and c.get("slot")]


def skill_joins(skill: Any) -> bool:
    """Whether a skill's end is joining a call (its declared ``end``, D1's moment vocabulary ``call_join``)."""
    return str(getattr(skill, "end", "") or "") == "call_join"


def skill_commit(skill: Any) -> str:
    """The skill's commit word (phase L's D24 ``commit``: ``none`` < ``words`` < ``user``); ``""`` before L ships it."""
    return str(getattr(skill, "commit", "") or "")


def draft_slots(skill: Any, slots: Mapping[str, Any], *, keep_text: bool) -> dict[str, Any] | None:
    """The capability's slots with every ``sends_when`` slot set to one of its options that does not send (its own
    data: ``send = "draft"``), and without the text when ``keep_text`` is false (Yuki's own wording: the chat opens
    empty). ``None`` when the data offers no non-sending option (nothing to draft with)."""
    out = dict(slots)
    for cond in getattr(skill, "sends_when", ()) or ():
        name = str(cond.get("slot") or "")
        if not name or "in" not in cond:
            return None
        sending = {str(x) for x in cond.get("in") or []}
        slot = skill.slot(name) if callable(getattr(skill, "slot", None)) else None
        options = [str(o) for o in (getattr(slot, "options", ()) or ()) if str(o) not in sending]
        if not options:
            return None
        default = getattr(slot, "default", None)
        out[name] = str(default) if default is not None and str(default) in options else options[0]
    if not getattr(skill, "sends_when", ()):
        return None   # a skill that always sends has no draft form
    if not keep_text:
        name = text_slot(skill)
        if not name:
            return None
        out.pop(name, None)
    return out if not skill_sends(skill, out) else None


# ---------------------------------------------------------------------------------------------------------------------
# Code facts of one step
# ---------------------------------------------------------------------------------------------------------------------


def traits_of(call: Call, live: Live) -> Traits:
    """The step's traits (tool metadata with its target's facts: the clicked role, Yuki's typed text waiting in that
    window, a web fetch after a read in this request, M9). ``call.tool`` is the Dispatcher name that runs
    (``fetch_url`` or ``read_file`` for a ``fetch`` step)."""
    return call_traits(call.tool, call.input, role=call.role or None,
                       typed_pending=bool(call.hwnd) and int(call.hwnd) in live.typed_pending,
                       files_read=bool(live.reads))


def is_join_control(call: Call, live: Live) -> bool:
    """A click on a capability's declared join control, by (role, name) identity."""
    if call.tool != "click" or not call.name:
        return False
    return (call.role.casefold(), words(call.name).casefold()) in live.join_controls


def policy_holds(call: Call, live: Live) -> bool:
    """One of the step's kinds is held below "do it and tell me" by the user's autonomy settings (``ask`` or
    ``show_plan``): the commit check must find their words asked for it, as the judge did for those settings."""
    try:
        levels = (live.policy or util._shared_reader()).levels()
    except Exception:  # noqa: BLE001 - an unreadable policy: the defaults
        levels = dict(util.POLICY_DEFAULTS)
    return any(levels.get(kind, "do_and_tell") in ("ask", "show_plan") for kind in traits_of(call, live).kinds)


def may_commit(call: Call, live: Live, plan: Any = None) -> bool:
    """Whether the step could send, delete, buy, close, join or otherwise last (§2.5 rows 3-5): its traits destroy or
    reach, it clicks a declared join control, it is a ``run_skill`` that sends or joins, or the user's policy holds one
    of its kinds. These are the steps the commit check sees."""
    if call.tool in ("look", "memory", "wait"):
        return False
    if call.tool == "run_skill":
        key, slots = skill_input(call, plan)
        sk = util.skill(key)
        return skill_sends(sk, slots) or skill_joins(sk)
    t = traits_of(call, live)
    return t.destroys or t.reaches or is_join_control(call, live) or policy_holds(call, live)


def takes_screen(call: Call) -> bool:
    """Row 1: the step takes focus, sends input, restores a window, launches an app or opens a link (tool metadata and
    fixed operations). ``run_skill`` is gated rung by rung inside the engine instead."""
    if call.tool in ("run_skill", "look", "memory", "fetch", "fetch_url", "wait"):
        return False
    if call.tool in _TAKES_SCREEN_TOOLS:
        return True
    if call.tool == "window_action" and str(call.input.get("action") or "") in _TAKES_SCREEN_WINDOW_OPS:
        return True
    return tool_traits(call.tool, call.input).needs in ("input", "focus")


def game_now(board: Mapping[str, Any] | None = None) -> str | None:
    """D9: the game in front: the Now board's ``game_front`` when fresh (its name, or ``"game"``), else the local read
    of the foreground program (``gamegate.game_in_front``). ``""``: none; ``None``: neither could tell (counts as a
    game). Never raises."""
    try:
        from yuki.memory.now import game_in_front as board_game

        front = board_game(board) if board else None
        if front is not None:
            return str((board or {}).get("game_front_name") or "game") if front else ""
    except Exception:  # noqa: BLE001 - the local read decides
        pass
    try:
        from yuki.memory.gamegate import game_in_front

        return str(game_in_front() or "")
    except Exception:  # noqa: BLE001 - unknown: counted as a game
        return None


def calling_windows(board: Mapping[str, Any] | None) -> frozenset[int]:
    """The windows the Now board lists in ``calling_tabs`` (a calling site's tab in them)."""
    out: set[int] = set()
    for key in ((board or {}).get("calling_tabs") or {}):
        try:
            out.add(int(key))
        except (TypeError, ValueError):
            continue
    return frozenset(out)


def join_controls() -> frozenset[tuple[str, str]]:
    """The join controls capabilities declare (``[chat] labels.<language>.join_voice = [role, name]`` in each chat
    app's connection file), casefolded. Data, read through ``yuki.skills.chat``; never raises."""
    out: set[tuple[str, str]] = set()
    try:
        from yuki.skills.chat import app_data, chat_apps

        for app in chat_apps():
            for table in ((app_data(app).get("labels") or {}).values()):
                pair = table.get("join_voice") if isinstance(table, Mapping) else None
                if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                    out.add((str(pair[0]).casefold(), words(pair[1]).casefold()))
    except Exception:  # noqa: BLE001 - no data: rows 3a and 3c still hold
        pass
    return frozenset(out)


def _clock(at: float) -> str:
    try:
        return time.strftime("%I:%M %p", time.localtime(float(at))).lstrip("0")
    except (TypeError, ValueError, OverflowError):
        return ""


def near_duplicate(text: str, live: Live) -> Mapping[str, Any] | None:
    """The user's recent message to this person that the text nearly repeats (``util.similar`` at ``util.SIMILAR`` or
    more, within :data:`DUPLICATE_WINDOW_S`), else ``None``. A number, never a reading of the words."""
    for row in live.recent:
        try:
            at = float(row.get("at") or 0.0)
        except (TypeError, ValueError):
            continue
        if live.now - at > DUPLICATE_WINDOW_S:
            continue
        if util.similar(text, str(row.get("text") or "")) >= util.SIMILAR:
            return row
    return None


def _named(to: str, quote: str, live: Live) -> bool:
    """Row 4 (b): the recipient is named in the user's quote: one of the people the index finds in the quote goes by
    ``to``, or ``to``'s own words stand in the quote (a group or channel by its name; memory unavailable)."""
    from yuki.memory.contacts import span_in, tokens

    if not to or not quote:
        return False
    if span_in(to, quote):
        return True
    try:
        people = live.people_in(quote) if live.people_in is not None else None
    except Exception:  # noqa: BLE001
        people = None
    want = tokens(to)
    return any(tokens(n) == want for names in (people or []) for n in names)


# ---------------------------------------------------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------------------------------------------------


def check(step: Call, plan: Any, user_words: Sequence[str], live: Live, check_verdict: Any) -> Verdict:
    """The table, row by row (module docstring). ``check_verdict``: the commit check's verdict for this very step
    (``yuki.brain.check.CheckVerdict``: ``effect``, ``asked``, ``question``, ``failed``), or ``None`` when the step is not
    may-commit (or none was had: a may-commit step without one fails closed). ``plan``: the plan (``None``: a talker
    hand, a one-step plan with nothing declared). Never raises: an error inside is an ask."""
    try:
        return _check(step, plan, list(user_words or []), live, check_verdict)
    except Exception:  # noqa: BLE001 - fail closed
        return Verdict(ASK, 0, "error", question=failed_question(step))


def failed_question(step: Call) -> str:
    """The fail-closed question, naming the step in code words (``describe_step``'s running line: the verb, the
    control's role and name, the app), so a yes is a yes to that step. Never raises."""
    try:
        from yuki.ui.step_words import describe_step

        about = {k: v for k, v in (("element", step.name[:80]), ("role", step.role), ("app", step.app)) if v}
        line = words(describe_step(step.tool, {**(step.input or {}), "_about": about}, done=False))
    except Exception:  # noqa: BLE001 - the plain question
        line = ""
    if not line:
        return LINE_CHECK_FAILED
    return LINE_CHECK_FAILED_STEP.format(step=line[:1].lower() + line[1:])


def screen_gate(step: Call, live: Live) -> Verdict | None:
    """Rows 1-2 alone (a game in front; the app's Act switch off or a kind on "just suggest"): the refusal, or
    ``None``. Re-run after an ask was answered and after the send countdown, since the screen may have changed while
    the user was asked or the countdown ran."""
    return _rows_1_2(step, live, traits_of(step, live))


def _rows_1_2(step: Call, live: Live, traits: Traits) -> Verdict | None:
    # row 1: a game in front
    if takes_screen(step) and (live.game is None or live.game):
        return Verdict(REFUSE, 1, "game", line=LINE_GAME, final=True)

    # row 2: the app's Act switch off, or a kind set to "just suggest"
    if step.tool != "run_skill":
        reader = live.policy or util._shared_reader()
        try:
            off = reader.app_off(step.app) if util.acts_in_app(step.tool, step.input, traits) else None
        except Exception:  # noqa: BLE001 - an unreadable policy never blocks (the old judge's rule)
            off = None
        if off:
            return Verdict(REFUSE, 2, "act_off", line=util.app_off_line(off), final=True)
        try:
            only = util.suggest_only(traits, reader.levels())
        except Exception:  # noqa: BLE001
            only = None
        if only is not None:
            kind = util.KIND_WORDS.get(only, only)
            return Verdict(REFUSE, 2, "suggest", final=True,
                           line=f"Your settings say only suggest for {kind.lower()}, so I'll leave that to you.")
    return None


def _check(step: Call, plan: Any, user_words: list[str], live: Live, cv: Any) -> Verdict:
    consent = plan_consent(plan)
    traits = traits_of(step, live)
    committing = may_commit(step, live, plan)
    effect = str(getattr(cv, "effect", "") or "") if cv is not None else ""
    failed = cv is None or bool(getattr(cv, "failed", False))
    asked = bool(getattr(cv, "asked", False)) and not failed

    # rows 1-2: a game in front; the app's Act switch off or a kind on "just suggest"
    gate = _rows_1_2(step, live, traits)
    if gate is not None:
        return gate

    # row 3: joins are the user's click
    sk = None
    if step.tool == "run_skill":
        key, slots = skill_input(step, plan)
        sk = util.skill(key)
    joining = (committing and bool(step.hwnd) and int(step.hwnd) in live.calling) or is_join_control(step, live) \
        or effect == "join" or (sk is not None and skill_joins(sk))
    if joining:
        if sk is not None and skill_commit(sk) == "words" and asked and effect in ("join", "none", ""):
            return Verdict(ALLOW, 3, "join_words")
        app = util.app_name(step.app) if step.app else ""
        why = ("join_window" if step.hwnd and int(step.hwnd) in live.calling
               else "join_control" if is_join_control(step, live) else "join_effect")
        return Verdict(REFUSE, 3, why, line=LINE_JOIN.format(app=app) if step.app else LINE_JOIN_PLAIN, final=True,
                       focus=int(step.hwnd) if step.hwnd else None)

    # row 4: sends
    sends = sends_for_certain(traits) or effect == "send" or (sk is not None and skill_sends(sk, skill_input(step, plan)[1]))
    if sends:
        return _send(step, plan, consent, user_words, live, sk, asked, failed)

    # row 5: anything else that could last
    if committing:
        if failed:
            return Verdict(ASK, 5, "check_failed", question=failed_question(step))
        if effect in ("none", "") and cv is not None:
            return Verdict(ALLOW, 5, "none")
        if asked:
            return Verdict(ALLOW, 5, "asked")
        question = words(getattr(cv, "question", "")) or failed_question(step)
        return Verdict(ASK, 5, "lasting", question=question)

    # row 6
    return Verdict(ALLOW, 6, "none")


def _send(step: Call, plan: Any, consent: dict[str, Any], user_words: list[str], live: Live, sk: Any, asked: bool,
          failed: bool) -> Verdict:
    """Row 4 for a step that sends."""
    if consent["commit"] != "send":
        return Verdict(REFUSE, 4, "undeclared_send",
                       line=LINE_TYPED_ENTER if step.tool == "type_text" else LINE_UNDECLARED_SEND)
    if step.tool != "run_skill" or sk is None or not getattr(sk, "sends", False):
        return Verdict(REFUSE, 4, "not_capability", line=LINE_NOT_CAPABILITY)
    _key, slots = skill_input(step, plan)
    quote, to, text = consent["quote"], consent["to"], consent["text"]
    # (a) the quote is the user's own words; (b) it names the recipient (code facts: refuse, the planner may fix it)
    if not quote or not in_words(quote, user_words):
        return Verdict(REFUSE, 4, "quote", line="the send's quote is not a span of the user's own words")
    if not _named(to, quote, live):
        return Verdict(REFUSE, 4, "recipient", line="the send's recipient is not named in the user's quote")
    who = words(slots.get(person_slot(sk))) if person_slot(sk) else ""
    if who and not _same_person(who, to, live):
        return Verdict(REFUSE, 4, "recipient_slot", line=LINE_RECIPIENT)
    name = text_slot(sk)
    step_text = words(slots.get(name)) if name else ""
    own = bool(step_text) and step_text == text and in_words(step_text, user_words)       # (c)
    if not own or not asked:                                                                # (c) or (d) failed
        keep = bool(step_text) and in_words(step_text, user_words)
        drafted = draft_slots(sk, slots, keep_text=keep)
        if drafted is None:
            return Verdict(REFUSE, 4, "no_draft", line="this capability has no way to leave a draft")
        return Verdict(DRAFT, 4, "not_asked" if own else "text", line=LINE_DRAFTED,
                       draft_input={**step.input, "slots": drafted},
                       type_after="" if keep else (step_text or text))
    if not skill_sends(sk, slots, partial=False):
        # a send slot left out: the resolver would fill its default (a draft), so this step would not send for sure;
        # it is never counted down or reported as sent (the planner fills the slot, or the plan drafts)
        return Verdict(REFUSE, 4, "send_slot", line=LINE_SEND_SLOT.format(slots=", ".join(send_slots(sk)) or "send"))
    seen = near_duplicate(step_text, live)                                                 # (e)
    if seen is not None:
        at = _clock(float(seen.get("at") or 0.0))
        question = LINE_DUPLICATE.format(to=to, at=at) if to and at else LINE_DUPLICATE_PLAIN
        return Verdict(ASK, 4, "duplicate", question=question)
    return Verdict(ALLOW, 4, "send", countdown=True)


def _same_person(slot_value: str, to: str, live: Live) -> bool:
    """The capability's person slot names the plan's recipient: the same words, or one of the names memory's people
    index gives the one person ``to`` names (a first name for the full name). A part of a name alone ("Iyer" for
    "Hemanth Iyer") is not enough, and with memory unable to say only the same words pass."""
    from yuki.memory.contacts import tokens

    want, slot = tokens(to), tokens(slot_value)
    if not want or not slot:
        return False
    if slot == want:
        return True
    try:
        people = live.people_in(to) if live.people_in is not None else None
    except Exception:  # noqa: BLE001
        people = None
    fits = [names for names in people or [] if any(tokens(n) == want for n in names)]
    return len(fits) == 1 and any(tokens(n) == slot for n in fits[0])


def after_ask(verdict: Verdict) -> Verdict:
    """An ask the user said yes to: allowed this once (a send keeps its countdown)."""
    return replace(verdict, result=ALLOW, countdown=verdict.row == 4)


__all__ = ["ALLOW", "ASK", "COMMITS", "DRAFT", "EFFECTS", "LINE_DRAFTED", "LINE_GAME", "REFUSE", "RESULTS", "Call",
           "Live", "Verdict", "after_ask", "calling_windows", "check", "draft_slots", "failed_question", "field_of",
           "game_now", "in_words", "is_join_control", "join_controls", "may_commit", "near_duplicate", "person_slot",
           "plan_consent", "policy_holds", "screen_gate", "send_slots", "skill_commit", "skill_input", "skill_joins",
           "skill_sends", "takes_screen", "text_slot", "traits_of"]
