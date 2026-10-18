"""The brain's settings, read from :class:`yuki.config.Settings` with ``getattr`` defaults.

Read once per request (:meth:`BrainSettings.read`): effort is pinned per channel and never changes mid-request.

kBrain (18 Oct): the plan-first brain is THE brain. Its shipped mode is ``all`` (typed and spoken); ``loop`` stays only
as the rollback value. The planner runs on :data:`PLANNER_MODEL` (Sonnet 5.5) unless ``brain_model`` names another;
the tray's model switch (``Settings.model``) belongs to the old loop and no longer reaches the planner.

Phase 4 (oTyped): the switch's values come from :mod:`yuki.one.modes` (``one`` beside ``all``); :class:`OneSettings`
is the ONE agent's typed view (its model, typed thinking, who settles a clean act); its effort is this module's rule
(:meth:`BrainSettings.effort_of`), and its budgets are :mod:`yuki.one.budget`'s (``one_typed_*`` overrides).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Collection, Mapping

from yuki.config import EFFORT_LEVELS
# phase 4: the one brain-mode helper is the only copy of the switch's values (``one`` among them; an unknown value
# reads as ``all``, never the old thinker)
from yuki.one.modes import BRAIN_MODES, DEFAULT_MODE
from yuki.one.modes import normalize as _brain_mode
#: The planner's model when ``brain_model`` is unset: Sonnet 5.5 (measured 18 Oct on this PC's Bedrock, ap-south-1:
#: first token 0.77-0.92 s on an 8k-character system prompt, warm). Adaptive thinking at ``low`` effort skips thinking
#: on simple asks and thinks on harder ones by itself.
PLANNER_MODEL = "global.anthropic.claude-sonnet-5-5"


#: aBrain (phase 2.5, 18 Oct): how the planner's effort is chosen per request. ``auto``: from the request's structure
#: (:func:`effort_for_request`): typed requests medium; a spoken hand-over medium when the talker's understanding (C3)
#: has two or more parts or no part names a skill of the live catalog (or no understanding came, or a skill handed the
#: request over), else low. A level (``low`` | ``medium`` | ``high`` ...) pins both channels to it, and ``fixed`` uses
#: ``brain_effort_voice`` / ``brain_effort_typed`` as before, so the final test can compare. Read from
#: ``brain_effort_rule`` on the settings, else the ``YUKI_BRAIN_EFFORT`` environment variable, else ``auto``.
EFFORT_RULES: tuple[str, ...] = ("auto", "fixed")
DEFAULT_EFFORT_RULE = "auto"
#: The effort a request gets under ``auto`` when its structure asks for thinking, and when it doesn't.
EFFORT_THINK, EFFORT_QUICK = "medium", "low"


def _get(settings: Any, name: str, default: Any) -> Any:
    value = getattr(settings, name, None)
    return default if value is None or value == "" else value


def _effort(value: Any, default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in EFFORT_LEVELS else default


def _effort_rule(settings: Any) -> str:
    """``auto``, ``fixed`` or an effort level (:data:`EFFORT_RULES`); anything else is ``auto``."""
    raw = getattr(settings, "brain_effort_rule", None) or os.environ.get("YUKI_BRAIN_EFFORT") or DEFAULT_EFFORT_RULE
    text = str(raw).strip().lower()
    return text if text in EFFORT_RULES or text in EFFORT_LEVELS else DEFAULT_EFFORT_RULE


def effort_for_request(channel: str, understanding: Mapping[str, Any] | None, *, seeded: bool = False,
                       skills: Collection[str] | None = None) -> tuple[str, str]:
    """``(effort, why)`` under the ``auto`` rule, from the request's structure only (never its words).

    Typed: :data:`EFFORT_THINK` (``typed``). Spoken: :data:`EFFORT_THINK` when a skill handed the request over
    (``seeded``: its usual way didn't get there), when no understanding came (``no_reading``), when the talker's
    understanding (contract C3) holds two or more parts (``parts``), or when no part names a skill (``no_skill``: a part
    whose ``skill`` is empty, or not one of the live catalog's ``skills`` ids when those are known); else
    :data:`EFFORT_QUICK` (``one_skill``). ``why`` is a code word for the log."""
    if channel != "voice":
        return EFFORT_THINK, "typed"
    if seeded:
        return EFFORT_THINK, "seeded"
    if not isinstance(understanding, Mapping):
        return EFFORT_THINK, "no_reading"
    parts = [p for p in (understanding.get("parts") or []) if isinstance(p, Mapping)]
    if len(parts) >= 2:
        return EFFORT_THINK, "parts"
    known = frozenset(str(k) for k in skills) if skills is not None else None

    def fits(part: Mapping[str, Any]) -> bool:
        skill = " ".join(str(part.get("skill") or "").split())
        return bool(skill) and (known is None or skill in known or skill.split("#", 1)[0] in known)

    if not parts or not any(fits(p) for p in parts):
        return EFFORT_THINK, "no_skill"
    return EFFORT_QUICK, "one_skill"


@dataclass(frozen=True)
class BrainSettings:
    """One request's view of the settings (the design's numbers as defaults).

    Attributes:
        mode: ``Settings.brain`` (``all`` | ``one`` | ``loop`` | ``typed``; anything else reads as ``all``).
        model: The planner's model (``brain_model``, else :data:`PLANNER_MODEL`).
        effort_rule: How the effort is chosen (:data:`DEFAULT_EFFORT_RULE`, aBrain phase 2.5): ``auto`` (from the
            request's structure: :meth:`effort_of`), ``fixed`` (the two values below), or a level pinned for both.
        effort_voice / effort_typed: The planner's effort per channel under ``fixed`` (adaptive thinking on both).
        max_tokens: The planner's output ceiling (a plan is ~100-600 tokens plus thinking; streamed).
        thinking_display: ``summarized`` (the island's thinking line) or ``omitted``.
        stream: Stream the planner (the default; steps then run as they close).
        eager: Send ``eager_input_streaming`` on streamed calls (``brain_eager_streaming``).
        max_replans: Act plans: replans after the first plan (design 2.3: 3).
        act_calls: Act plans: model calls in all (5).
        answer_calls / answer_s: Answer plans: model calls (8) or seconds of work (30).
        check_model: The small model for a yes/no read of the user's answer to Yuki's question (``check_model``, else
            ``judge_model``).
        ask_judge_s: How long that read may take before it counts as "no" (fail closed).
        recent_chars: Each of the last two requests' words and answer in the request turn (1,500).
        you_chars: The "you right now" block (1,800).
        overview_chars: The window list and the Now picture (2,500).
        people_chars: People named and their last messages (900).
        context_chars: Card or coach text, labelled, never the user's words (1,200).
        seed_chars: A skill's plan seed (800).
        profile_chars: Profile and standing rules, breakpoint 2 (3,000).
        menu_chars: The menu, breakpoint 3 (``skills_menu_chars``, 2,400).
        result_chars: A step report's screen diff (1,500).
        desk_fresh_s: An overview read ahead (``begin_early``) is used when at most this old (2 s).
        pack_fresh_s: The rest of a context read ahead is used when at most this old (``pack_max_age_s``, 20 s).
        takeover_stop_s: A pause nobody resumes ends the request after this long (``takeover_idle_stop_s``).
    """

    mode: str = DEFAULT_MODE
    model: str = PLANNER_MODEL
    effort_rule: str = DEFAULT_EFFORT_RULE
    effort_voice: str = "low"
    effort_typed: str = "low"
    max_tokens: int = 16000
    thinking_display: str = "summarized"
    stream: bool = True
    eager: bool = True
    max_replans: int = 3
    act_calls: int = 5
    answer_calls: int = 8
    answer_s: float = 30.0
    check_model: str = ""
    ask_judge_s: float = 4.0
    recent_chars: int = 1500
    you_chars: int = 1800
    overview_chars: int = 2500
    people_chars: int = 900
    context_chars: int = 1200
    seed_chars: int = 800
    profile_chars: int = 3000
    menu_chars: int = 2400
    result_chars: int = 1500
    desk_fresh_s: float = 2.0
    pack_fresh_s: float = 20.0
    takeover_stop_s: float = 600.0

    @classmethod
    def read(cls, settings: Any) -> "BrainSettings":
        """The values in force now (``settings``: a :class:`yuki.config.Settings`, or anything with its fields)."""
        return cls(
            mode=_brain_mode(_get(settings, "brain", DEFAULT_MODE)),
            model=str(_get(settings, "brain_model", "") or PLANNER_MODEL),
            effort_rule=_effort_rule(settings),
            effort_voice=_effort(_get(settings, "brain_effort_voice", "low"), "low"),
            effort_typed=_effort(_get(settings, "brain_effort_typed", "low"), "low"),
            max_tokens=int(_get(settings, "brain_max_tokens", 16000)),
            thinking_display=("omitted" if str(_get(settings, "thinking_display", "summarized")) == "omitted"
                              else "summarized"),
            stream=bool(_get(settings, "stream", True)),
            eager=bool(_get(settings, "brain_eager_streaming", True)),
            max_replans=max(0, int(_get(settings, "brain_max_replans", 3))),
            act_calls=max(1, int(_get(settings, "brain_act_calls", 5))),
            answer_calls=max(1, int(_get(settings, "brain_answer_calls", 8))),
            answer_s=float(_get(settings, "brain_answer_s", 30.0)),
            check_model=str(_get(settings, "check_model", "") or _get(settings, "judge_model", "")),
            ask_judge_s=float(_get(settings, "brain_ask_judge_s", 4.0)),
            recent_chars=int(_get(settings, "brain_recent_chars", 1500)),
            you_chars=int(_get(settings, "brain_you_chars", 1800)),
            overview_chars=int(_get(settings, "brain_overview_chars", 2500)),
            people_chars=int(_get(settings, "brain_people_chars", 900)),
            context_chars=int(_get(settings, "brain_context_chars", 1200)),
            seed_chars=int(_get(settings, "brain_seed_chars", 800)),
            profile_chars=int(_get(settings, "brain_profile_chars", 3000)),
            menu_chars=int(_get(settings, "skills_menu_chars", 2400)),
            result_chars=int(_get(settings, "brain_result_chars", 1500)),
            desk_fresh_s=float(_get(settings, "brain_desk_fresh_s", 2.0)),
            pack_fresh_s=float(_get(settings, "pack_max_age_s", 20.0)),
            takeover_stop_s=float(_get(settings, "takeover_idle_stop_s", 600.0)),
        )

    def effort_for(self, channel: str) -> str:
        """The ``fixed`` rule's effort for ``channel`` (``voice`` | ``typed``)."""
        return self.effort_voice if channel == "voice" else self.effort_typed

    def effort_of(self, channel: str, understanding: Mapping[str, Any] | None = None, *, seeded: bool = False,
                  skills: Collection[str] | None = None) -> tuple[str, str]:
        """The effort pinned for a whole request, and why (a code word for the log): the rule's level when it names
        one (``pinned``), the channel's setting under ``fixed``, else :func:`effort_for_request`."""
        if self.effort_rule in EFFORT_LEVELS:
            return self.effort_rule, "pinned"
        if self.effort_rule == "fixed":
            return self.effort_for(channel), "fixed"
        return effort_for_request(channel, understanding, seeded=seeded, skills=skills)

    def facts(self) -> dict[str, Any]:
        """Content-free facts for the log."""
        return {"mode": self.mode, "model": self.model, "effort_rule": self.effort_rule,
                "effort_voice": self.effort_voice, "effort_typed": self.effort_typed, "stream": self.stream,
                "eager": self.eager, "max_replans": self.max_replans, "act_calls": self.act_calls,
                "answer_calls": self.answer_calls}


# ---------------------------------------------------------------------------------------------------------------------
# Phase 4: the ONE agent (``brain = "one"``)
# ---------------------------------------------------------------------------------------------------------------------

#: The ONE agent's model when ``one_model`` is unset: Sonnet 5.5 (the lean talker's and the planner's model today).
ONE_MODEL = "global.anthropic.claude-sonnet-5-5"
#: The models a ONE agent's failover chain ends with (Sonnet 5, either profile): the chain stops after the first of
#: them, and no Haiku is ever in it (addendum must-fix 9: no Haiku driving acts). Model ids: data, not words.
ONE_CHAIN_LAST: frozenset[str] = frozenset({"global.anthropic.claude-sonnet-5", "us.anthropic.claude-sonnet-5"})
#: Typed thinking (``one_typed_thinking``): ``adaptive`` on every round at the typed effort (design 2.5 and 2.6:
#: typed is adaptive medium on every round, today's typed effort under the ``auto`` rule), or ``fast`` (off).
ONE_TYPED_THINKING: tuple[str, ...] = ("adaptive", "fast")
#: Who ends a clean simple act (``one_settle``, addendum decision 2): ``code`` (shipped) or ``model``.
ONE_SETTLE: tuple[str, ...] = ("code", "model")
#: What one tool result may carry back to the typed agent (characters). The runner caps its own reads (design 3.1:
#: memory, fetch and a shell's output up to 12,000 typed) before their fence; this leaves room for the fence and the
#: result's other fields around a capped read, so a fence is never cut. The driver cuts anything longer at this.
ONE_RESULT_CHARS = 14000


def _choice(value: Any, choices: tuple[str, ...]) -> str:
    text = str(value or "").strip().lower()
    return text if text in choices else choices[0]


def _whole(value: Any, default: int, least: int) -> int:
    """A whole number setting: ``value`` when it reads as one (at least ``least``), else ``default`` (a bad value never
    fails a request; ``one.budget.limits_for``'s rule)."""
    try:
        return max(least, int(value))
    except (TypeError, ValueError, OverflowError):
        return max(least, int(default))


def one_chain(chain: Any, *, last: frozenset[str] = ONE_CHAIN_LAST) -> list[dict[str, Any]]:
    """The ONE agent's failover chain from a model's configured one (``Settings.model_fallbacks[model]``): its
    entries in order up to and including the first Sonnet 5 (:data:`ONE_CHAIN_LAST`), with any Haiku entry left out
    (model ids only: data). Never raises; a chain it can't read is ``[]`` (the primary alone)."""
    out: list[dict[str, Any]] = []
    for entry in chain if isinstance(chain, (list, tuple)) else ():
        model = str(entry.get("model") or "") if isinstance(entry, Mapping) else ""
        if not model or "haiku" in model.casefold():
            continue
        out.append(dict(entry))
        if model in last:
            break
    return out


@dataclass(frozen=True)
class OneSettings:
    """One typed request's view of the ONE agent's settings (read once per request).

    Attributes:
        model: ``one_model`` (else :data:`ONE_MODEL`).
        typed_thinking: :data:`ONE_TYPED_THINKING` (``one_typed_thinking``).
        settle: :data:`ONE_SETTLE` (``one_settle``).
        max_tokens: The output ceiling of one round (``one_max_tokens``, else the planner's ``brain_max_tokens``,
            16,000: thinking included; streamed).
        result_chars: :data:`ONE_RESULT_CHARS` (``one_result_chars``).
    """

    model: str = ONE_MODEL
    typed_thinking: str = "adaptive"
    settle: str = "code"
    max_tokens: int = 16000
    result_chars: int = ONE_RESULT_CHARS

    @classmethod
    def read(cls, settings: Any) -> "OneSettings":
        """The values in force now; a bad number reads as its default (never a failed request)."""
        planner = _whole(_get(settings, "brain_max_tokens", 16000), 16000, 1024)
        return cls(
            model=str(_get(settings, "one_model", "") or ONE_MODEL),
            typed_thinking=_choice(_get(settings, "one_typed_thinking", ""), ONE_TYPED_THINKING),
            settle=_choice(_get(settings, "one_settle", ""), ONE_SETTLE),
            max_tokens=_whole(_get(settings, "one_max_tokens", planner), planner, 1024),
            result_chars=_whole(_get(settings, "one_result_chars", ONE_RESULT_CHARS), ONE_RESULT_CHARS, 1000),
        )

    def facts(self) -> dict[str, Any]:
        """Content-free facts for the log."""
        return {"model": self.model, "typed_thinking": self.typed_thinking, "settle": self.settle,
                "max_tokens": self.max_tokens}


__all__ = ["BRAIN_MODES", "DEFAULT_MODE", "PLANNER_MODEL", "BrainSettings", "EFFORT_RULES", "DEFAULT_EFFORT_RULE",
           "EFFORT_THINK", "EFFORT_QUICK", "effort_for_request", "ONE_CHAIN_LAST", "ONE_MODEL", "ONE_RESULT_CHARS",
           "ONE_SETTLE", "ONE_TYPED_THINKING", "OneSettings", "one_chain"]
