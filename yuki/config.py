"""Runtime settings for Yuki.

Everything tunable lives here so no module reaches for an environment variable
or a magic number on its own. Auth is deliberately absent: the Bedrock client
picks up ``AWS_BEARER_TOKEN_BEDROCK`` from the environment itself and no key is
ever written to a file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

ScreenshotPolicy = Literal["never", "ask", "auto"]

#: Short aliases the user (and the CLI's ``/model`` command) can type.
MODEL_ALIASES: dict[str, str] = {
    "sonnet": "us.anthropic.claude-sonnet-5",
    "opus": "us.anthropic.claude-opus-5",
}

DEFAULT_MODEL = MODEL_ALIASES["sonnet"]

#: Every effort level ``output_config.effort`` accepts, cheapest first. The loop
#: passes the value through untouched, so this tuple is only here to catch a typo
#: before the API does.
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

#: Measured with ``scripts/bench.py`` on this machine on 2026-09-22 (Sonnet 5,
#: the same five requests, one run each): ``low`` took 16 model calls and 53 s of
#: wall time against ``medium``'s 15 calls and 70 s, at 3.2 s versus 4.4 s per
#: call and 75 versus 419 thinking tokens, with the same 4/5 requests succeeding.
#: Low effort is also the level that reliably finishes a one-step request in a
#: single round trip. Raise it per request with ``/effort`` when a task genuinely
#: needs more thinking; a harder task set may well want ``medium`` back.
DEFAULT_EFFORT = "low"


@dataclass
class Settings:
    """All knobs for one Yuki process.

    Attributes:
        model: Bedrock model id used for the next request. Mutable so the CLI's
            ``/model`` command can switch models mid-conversation.
        aws_region: Region passed to :class:`anthropic.AnthropicBedrock`.
        project_root: Base directory that ``log_dir`` is resolved against.
        log_dir: Directory holding one JSONL file (and one screenshot folder)
            per session.
        screenshot_policy: ``never`` refuses screenshots, ``ask`` requires the
            user to approve one, ``auto`` takes them whenever the model asks.
            This is a safety/consent gate, not behaviour steering.
        max_tokens: ``max_tokens`` for every request.
        thinking_display: ``summarized`` returns readable reasoning; ``omitted``
            (the API default on Sonnet 5 / Opus 5) returns empty thinking text.
        stream: Use the streaming endpoint and ``get_final_message()``.
        max_steps: Hard ceiling on model round-trips per user request, so a
            confused loop cannot burn tokens forever.
        keep_perception_turns: How many recent assistant turns keep their full
            perception payloads before older ones are stubbed out.
        tool_timeout_s: Ceiling handed to PowerShell and other slow actions.
        effort: ``output_config.effort`` sent on every request, one of
            :data:`EFFORT_LEVELS`. Lower effort means less thinking and fewer,
            more-consolidated tool calls. Mutable so the CLI's ``/effort``
            command (and :meth:`yuki.agent.loop.Agent.set_effort`) can change it
            mid-conversation.
        ui_hotkey: Global combo that opens and closes the overlay.
        ui_cancel_hotkey: Global combo that cancels whatever the worker is doing.
    """

    model: str = DEFAULT_MODEL
    aws_region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "us-east-1"))
    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    log_dir: Path = Path("logs/sessions")
    screenshot_policy: ScreenshotPolicy = "auto"
    max_tokens: int = 8000
    thinking_display: Literal["summarized", "omitted"] = "summarized"
    stream: bool = False
    max_steps: int = 40
    keep_perception_turns: int = 2
    tool_timeout_s: float = 20.0
    effort: str = DEFAULT_EFFORT
    ui_hotkey: str = "alt+shift"
    ui_cancel_hotkey: str = "ctrl+alt+space"

    def __post_init__(self) -> None:
        self.model = resolve_model(self.model)
        self.effort = resolve_effort(self.effort)

    @property
    def sessions_dir(self) -> Path:
        """Absolute directory for session logs."""
        log_dir = Path(self.log_dir)
        return log_dir if log_dir.is_absolute() else self.project_root / log_dir

    def with_model(self, name: str) -> "Settings":
        """Return a copy pointing at ``name`` (alias or full model id)."""
        return replace(self, model=resolve_model(name))

    def with_effort(self, level: str) -> "Settings":
        """Return a copy running at ``level``. Raises on an unknown level."""
        return replace(self, effort=resolve_effort(level))


def resolve_model(name: str) -> str:
    """Expand a short alias such as ``opus`` to a full Bedrock model id.

    Unknown values pass through untouched so a brand-new model id can be used
    without a code change.
    """
    return MODEL_ALIASES.get(name.strip().lower(), name.strip())


def resolve_effort(level: str) -> str:
    """Normalise an effort level, raising :class:`ValueError` on an unknown one.

    Unlike :func:`resolve_model`, an unrecognised value is rejected rather than
    passed through: the API refuses the request outright, and finding that out
    from a traceback here beats finding it out from a 400 mid-task.
    """
    if not isinstance(level, str):
        raise ValueError(f"effort must be a string, got {level!r}")
    candidate = level.strip().lower()
    if candidate not in EFFORT_LEVELS:
        raise ValueError(
            f"unknown effort {level!r}; choose one of {', '.join(EFFORT_LEVELS)}"
        )
    return candidate
