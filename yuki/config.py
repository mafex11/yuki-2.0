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

#: Default raised from ``low`` to ``high`` on 2026-09-22 after watching what low
#: effort actually costs on a real request. Asked to "play my Japanese playlist
#: on Spotify", Yuki typed the word "japanese" into Spotify's global search three
#: times, never formed the thought that "my Japanese playlist" means the playlist
#: in the user's own library whose *name* is written in Japanese, and gave up
#: after 1m41s. A human did it in four steps by looking at the library and
#: picking the playlist with the Japanese name. That is not a speed problem, it
#: is a thinking problem: interpreting what a request refers to, and noticing
#: that a repeated action is not working, is exactly the work that gets skipped
#: when there is no budget to do it in.
#:
#: The earlier ``low`` default came from a bench on five short, single-step
#: requests (``scripts/bench.py``, Sonnet 5, 2026-09-22), where ``low`` finished
#: in 53 s against ``medium``'s 70 s with the same 4/5 succeeding. That
#: measurement stands, but it measured the wrong thing: those tasks needed no
#: reasoning, so it only ever showed that thinking costs seconds, never that not
#: thinking costs the whole task. Seconds are cheap next to a failed request that
#: the user then has to do by hand.
#:
#: No new timings have been taken for ``high``; expect it to be slower per turn
#: and to be re-benchmarked on a task set that actually requires reasoning. All
#: five levels stay available and ``/effort`` still switches mid-conversation, so
#: dropping back for a session of trivial requests is one command away.
#:
#: Default lowered from ``high`` to ``medium`` on 2026-09-23, for round latency.
#: 107 real rounds measured each round at about 3.5 s fixed plus 13.8 ms per
#: output token (thinking included), and the fixed part cannot be reduced, so
#: output tokens are the part of a round Yuki controls. Now that perception is
#: reliable (tree waking, coverage, content-ready checks, the window view
#: attached after actions), less of each round goes on working out what the
#: screen shows, and the extra thinking ``high`` buys is paid on every round of
#: every task. The Spotify case above remains the warning sign to watch for.
#: This is a default, not a verdict: the tray menu switches low/medium/high for
#: both lanes at once, and ``logs/requests.csv`` records effort, wall time,
#: model time, calls and tokens for every request, so the levels can be
#: compared on real tasks (``scripts/costs.py`` summarises it) and this changed
#: back if ``medium`` starts losing tasks ``high`` would have finished.
DEFAULT_EFFORT = "medium"


#: Anthropic list prices in US dollars per million tokens, keyed by the Bedrock
#: model id. ``cache_write`` is the 5-minute cache write (1.25x input) and
#: ``cache_read`` a cache hit (0.1x input). Bedrock bills separately and its
#: rates may differ from these (regional or cross-region inference profiles,
#: negotiated pricing); override ``Settings.pricing`` to match your bill. The
#: numbers only feed the cost *estimate* in the logs -- nothing decides anything
#: on them.
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    MODEL_ALIASES["sonnet"]: {
        "input": 2.00,
        "output": 10.00,
        "cache_write": 2.50,
        "cache_read": 0.20,
    },
    MODEL_ALIASES["opus"]: {
        "input": 5.00,
        "output": 25.00,
        "cache_write": 6.25,
        "cache_read": 0.50,
    },
}


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
            This is a safety/consent gate, not behaviour steering. Defaults to
            ``never`` for now, deliberately and temporarily: with the tree-waking
            fix in place the UIA tree is usable even on the Chromium/Electron
            apps that used to come back empty, and the point of the current
            testing phase is to find out where the tree really is not enough
            rather than let a screenshot paper over it. Screenshots are not going
            away -- expect this back at ``auto`` once the tree path has been
            exercised properly.
        max_tokens: ``max_tokens`` for every request.
        thinking_display: ``summarized`` returns readable reasoning; ``omitted``
            (the API default on Sonnet 5 / Opus 5) returns empty thinking text.
        stream: Use the streaming endpoint (on by default). Tools then start
            while the response is still being generated, as soon as their
            ``tool_use`` block is complete, and the stored turn is
            ``get_final_message()``. ``False`` falls back to ``messages.create``
            with every tool run after the response.
        max_steps: Hard ceiling on model round-trips per user request, so a
            confused loop cannot burn tokens forever.
        keep_perception_turns: How many recent assistant turns keep their full
            perception payloads before older ones are stubbed out.
        tool_timeout_s: Ceiling handed to PowerShell and other slow actions.
        effort: ``output_config.effort`` sent on every request, one of
            :data:`EFFORT_LEVELS`. Lower effort means less thinking and fewer,
            more-consolidated tool calls; it also means less of the
            interpretation and self-correction that hard requests live on. The
            default is ``medium``, chosen for round latency now that perception
            is reliable (see :data:`DEFAULT_EFFORT`); the tray's effort switch
            and ``logs/requests.csv`` exist to compare levels on real tasks.
            Mutable so the CLI's ``/effort`` command (and
            :meth:`yuki.agent.loop.Agent.set_effort`) can change it
            mid-conversation.
        ui_hotkey: Global combo that opens and closes the overlay.
        ui_cancel_hotkey: Global combo that cancels whatever the worker is doing.
        pricing: US dollars per million tokens per model id, each entry with
            ``input``, ``output``, ``cache_write`` and ``cache_read``. Defaults
            to Anthropic's list prices (:data:`DEFAULT_PRICING`). Bedrock rates
            may differ -- override this dict (or single entries) to match what
            AWS actually bills. A model with no entry is logged with
            ``cost_usd`` of ``None`` rather than a guess.
        requests_csv: Append-only CSV with one line per finished request,
            resolved against ``project_root`` like ``log_dir``.
    """

    model: str = DEFAULT_MODEL
    aws_region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "us-east-1"))
    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    log_dir: Path = Path("logs/sessions")
    screenshot_policy: ScreenshotPolicy = "never"
    max_tokens: int = 8000
    thinking_display: Literal["summarized", "omitted"] = "summarized"
    stream: bool = True
    max_steps: int = 40
    keep_perception_turns: int = 2
    tool_timeout_s: float = 20.0
    effort: str = DEFAULT_EFFORT
    ui_hotkey: str = "alt+shift"
    ui_cancel_hotkey: str = "ctrl+alt+space"
    pricing: dict[str, dict[str, float]] = field(
        default_factory=lambda: {model: dict(rates) for model, rates in DEFAULT_PRICING.items()}
    )
    requests_csv: Path = Path("logs/requests.csv")

    def __post_init__(self) -> None:
        self.model = resolve_model(self.model)
        self.effort = resolve_effort(self.effort)

    @property
    def sessions_dir(self) -> Path:
        """Absolute directory for session logs."""
        log_dir = Path(self.log_dir)
        return log_dir if log_dir.is_absolute() else self.project_root / log_dir

    @property
    def requests_csv_path(self) -> Path:
        """Absolute path of the per-request CSV."""
        path = Path(self.requests_csv)
        return path if path.is_absolute() else self.project_root / path

    def price_for(self, model: str) -> dict[str, float] | None:
        """The ``$/1M tokens`` rates for ``model`` (alias or id), or ``None``."""
        return self.pricing.get(resolve_model(model))

    def estimate_cost(self, model: str, usage: dict[str, int]) -> float | None:
        """Estimated US dollars for ``usage`` on ``model``.

        Args:
            model: Alias or model id.
            usage: Token counts keyed ``input_tokens`` (uncached input),
                ``cache_write_tokens``, ``cache_read_tokens`` and
                ``output_tokens`` (thinking included, as the API bills it).

        Returns:
            The estimate, or ``None`` when there is no price for the model.
        """
        rates = self.price_for(model)
        if rates is None:
            return None
        return (
            int(usage.get("input_tokens") or 0) * float(rates.get("input", 0.0))
            + int(usage.get("cache_write_tokens") or 0) * float(rates.get("cache_write", 0.0))
            + int(usage.get("cache_read_tokens") or 0) * float(rates.get("cache_read", 0.0))
            + int(usage.get("output_tokens") or 0) * float(rates.get("output", 0.0))
        ) / 1_000_000

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
