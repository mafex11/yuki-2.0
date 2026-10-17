"""The brain's settings, read from :class:`yuki.config.Settings` with ``getattr`` defaults.

Read once per request (:meth:`BrainSettings.read`): effort is pinned per channel and never changes mid-request.

kBrain (18 Oct): the plan-first brain is THE brain. Its shipped mode is ``all`` (typed and spoken); ``loop`` stays only
as the rollback value. The planner runs on :data:`PLANNER_MODEL` (Sonnet 5.5) unless ``brain_model`` names another;
the tray's model switch (``Settings.model``) belongs to the old loop and no longer reaches the planner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from yuki.config import EFFORT_LEVELS

#: ``Settings.brain``: which requests the new brain takes (``loop`` is the rollback value only).
BRAIN_MODES: tuple[str, ...] = ("loop", "typed", "all")
#: The shipped mode: the brain takes typed requests and the voice's hand-overs.
DEFAULT_MODE = "all"
#: The planner's model when ``brain_model`` is unset: Sonnet 5.5 (measured 18 Oct on this PC's Bedrock, ap-south-1:
#: first token 0.77-0.92 s on an 8k-character system prompt, warm). Adaptive thinking at ``low`` effort skips thinking
#: on simple asks and thinks on harder ones by itself.
PLANNER_MODEL = "global.anthropic.claude-sonnet-5-5"


def _get(settings: Any, name: str, default: Any) -> Any:
    value = getattr(settings, name, None)
    return default if value is None or value == "" else value


def _effort(value: Any, default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in EFFORT_LEVELS else default


@dataclass(frozen=True)
class BrainSettings:
    """One request's view of the settings (the design's numbers as defaults).

    Attributes:
        mode: ``Settings.brain`` (``loop`` | ``typed`` | ``all``; anything else reads as ``all``).
        model: The planner's model (``brain_model``, else :data:`PLANNER_MODEL`).
        effort_voice / effort_typed: The planner's effort per channel (adaptive thinking on both; design L9).
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
        mode = str(_get(settings, "brain", DEFAULT_MODE)).strip().lower()
        return cls(
            mode=mode if mode in BRAIN_MODES else DEFAULT_MODE,
            model=str(_get(settings, "brain_model", "") or PLANNER_MODEL),
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
        """The effort pinned for ``channel`` (``voice`` | ``typed``) for a whole request."""
        return self.effort_voice if channel == "voice" else self.effort_typed

    def facts(self) -> dict[str, Any]:
        """Content-free facts for the log."""
        return {"mode": self.mode, "model": self.model, "effort_voice": self.effort_voice,
                "effort_typed": self.effort_typed, "stream": self.stream, "eager": self.eager,
                "max_replans": self.max_replans, "act_calls": self.act_calls, "answer_calls": self.answer_calls}


__all__ = ["BRAIN_MODES", "DEFAULT_MODE", "PLANNER_MODEL", "BrainSettings"]
