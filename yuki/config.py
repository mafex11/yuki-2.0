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

    def __post_init__(self) -> None:
        self.model = resolve_model(self.model)

    @property
    def sessions_dir(self) -> Path:
        """Absolute directory for session logs."""
        log_dir = Path(self.log_dir)
        return log_dir if log_dir.is_absolute() else self.project_root / log_dir

    def with_model(self, name: str) -> "Settings":
        """Return a copy pointing at ``name`` (alias or full model id)."""
        return replace(self, model=resolve_model(name))


def resolve_model(name: str) -> str:
    """Expand a short alias such as ``opus`` to a full Bedrock model id.

    Unknown values pass through untouched so a brand-new model id can be used
    without a code change.
    """
    return MODEL_ALIASES.get(name.strip().lower(), name.strip())
