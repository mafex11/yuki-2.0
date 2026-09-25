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
    "sonnet": "global.anthropic.claude-sonnet-5",
    "opus": "global.anthropic.claude-opus-5",
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
    # Claude Haiku 4.5: the memory journal worker (yuki.memory.journal).
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": {
        "input": 1.00,
        "output": 5.00,
        "cache_write": 1.25,
        "cache_read": 0.10,
    },
    # The same model through the global inference profile: the voice mouth
    # (yuki.voice), measured faster from India than ``us.`` (docs/VOICE.md §14).
    "global.anthropic.claude-haiku-4-5-20251001-v1:0": {
        "input": 1.00,
        "output": 5.00,
        "cache_write": 1.25,
        "cache_read": 0.10,
    },
}

#: The voice mouth (docs/VOICE.md §5). Claude Haiku 4.5 is Active on Bedrock with
#: an end-of-life "no sooner than 2026-10-16" and no Haiku successor listed
#: (checked 2026-09-25), so this is a setting, not a constant in code.
DEFAULT_VOICE_MOUTH_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


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
        user_names: The user's own names and handles as apps show them. Memory
            capture marks messages from these senders as the user's, and the
            journal is told these names are the user (plus the names the
            watcher learns on screen, e.g. "Kian (you)").

    Voice (``voice_*``, docs/VOICE.md §15). None of it costs anything until
    voice is turned on from the tray or its hotkey, or the wake word is:
        voice_hotkey: Global combo that starts or ends a voice conversation
            (``YUKI_VOICE_HOTKEY`` overrides it).
        voice_wake: Listen for "Yuki" locally and start a conversation on it.
            On by default (the user asked for it, 2026-09-25); the tray's
            "Listen for 'Yuki'" switch changes it and remembers the choice
            (``%LOCALAPPDATA%\\Yuki\\voice.json``).
        voice_wake_during_calls: Keep listening for "Yuki" while a call or a
            meeting is on (off: saying the name to friends does nothing).
        voice_wake_threshold: The keyword spotter's trigger threshold (higher
            = fewer false wakes, more misses); measured in docs/VOICE.md §22.
        voice_wake_boost: The spotter's boosting score for the keyword.
        voice_idle_s: Quiet seconds (nobody speaking, no result owed) that
            end a conversation.
        voice_hold_max_s: Longest a quiet conversation stays open waiting to
            speak a running task's result; past it the result lands as a card.
        voice_input_device: Microphone (name, or part of it). ``None``: the
            Windows default communications input.
        voice_output_device: Output device name. ``None``: the default output.
        voice_require_headphones: Refuse to start a conversation while the
            sound comes out of speakers (no echo canceller yet, §11).
        voice_headphone_devices: Outputs to count as headphones although
            Windows calls them something else ("Speakers" on an interface).
        voice_stt_model / voice_stt_languages: Deepgram Flux model and the
            language hints it is given.
        voice_eot_threshold / voice_eager_eot_threshold / voice_eot_timeout_ms:
            Flux's end-of-turn settings (eager off: ``None``).
        voice_mouth_model: Bedrock model id of the fast mouth.
        voice_mouth_max_tokens: Output ceiling for one spoken reply.
        voice_tts_provider: ``elevenlabs`` or ``deepgram`` (Aura-2, English only).
        voice_tts_model / voice_tts_voice: The provider's model and voice id
            (empty: the provider's default in :mod:`yuki.voice.pipeline`).
        voice_tts_fallback: Provider used when the main one cannot speak (its
            key is missing or lacks permission); ``""`` = no fallback, voice
            then refuses to start and says why. A voice the account may not use
            (a library voice on the free plan) falls back to the default
            ElevenLabs voice for that conversation instead, with a note.
        voice_tts_speed / voice_tts_stability / voice_tts_similarity_boost /
            voice_tts_style / voice_tts_speaker_boost: ElevenLabs voice settings
            (speed 0.7-1.2; the others 0-1 and a switch). ``None`` = unset: the
            voice's own defaults and speed 1.0 (the user's choice, 2026-09-25).
            ``voice.json`` keys ``tts_speed``, ``tts_stability``,
            ``tts_similarity_boost``, ``tts_style``, ``tts_speaker_boost`` win.
        voice_usd_per_stt_min / voice_usd_per_1k_tts_chars: List prices for the
            cost estimate in the voice log (estimates only; nothing decides on them).
        voice_grounding: Look the user's words up in local memory on every turn
            and show the mouth the nearest facts (docs/VOICE.md §5.5).
        voice_grounding_wait_ms: How long the mouth's call may wait for that
            lookup; a slower lookup is attached to the next call instead.
        voice_grounding_facts: How many facts the mouth is shown.
        voice_settle_s / voice_settle_max_s: The narrator's settle window: things
            that arrive this close together are said as one turn, waiting at
            most the second figure from the first (docs/VOICE.md §6.4).
        voice_cooldown_s: Quiet after Yuki's own speech before a result or a
            progress line may start.
        voice_stale_turns: User turns a waiting result may sit through before it
            is folded into the conversation silently instead of spoken.
        voice_progress_lines: Most spoken progress lines per task.
        voice_result_sentences / voice_result_chars: How much of a result is
            spoken at first (the rest waits for "want the rest?").
        voice_brain_effort: The brain's ``output_config.effort`` for requests
            that came by voice (typed ones keep ``effort``). Spoken requests are
            heard live and are mostly one quick action or fact; measured
            2026-09-25 (docs/VOICE.md §7): "minimize Discord" and "what time is
            it in Tokyo" at medium vs low, see there.
        voice_final_gap_s: The narrator's pause after Yuki's own speech before a
            result or a question is said (progress lines keep ``voice_cooldown_s``).
        voice_brain_thinking: Thinking for spoken requests (off: ``thinking`` is
            disabled for them; typed ones keep adaptive thinking).
        voice_brain_portrait: Attach the rest of the portrait and the resume to a
            spoken request's memory block (off: the profile and rules layers only,
            plus to-dos, know-how and commitments).
        voice_brain_model: The thinker's model for spoken requests (alias or id;
            empty: the brain's own ``model``). Measured in docs/VOICE.md §7.
        voice_progress_pause_s: A progress line of a running task is said only
            after Yuki has been quiet this long (a real pause), never after its result.
        voice_hands: Give the mouth its own single-step desktop tools (docs/VOICE.md §5.6).
        voice_desk_wait_ms: How long a mouth call waits for the desktop summary.
    """

    model: str = DEFAULT_MODEL
    aws_region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "ap-south-1"))
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
    ui_cancel_hotkey: str = "alt+shift+x"
    pricing: dict[str, dict[str, float]] = field(
        default_factory=lambda: {model: dict(rates) for model, rates in DEFAULT_PRICING.items()}
    )
    requests_csv: Path = Path("logs/requests.csv")
    user_names: list[str] = field(default_factory=lambda: ["Kian", "Mafex", "mafex11"])
    voice_hotkey: str = "ctrl+alt+y"
    voice_wake: bool = True
    voice_wake_during_calls: bool = False
    voice_wake_threshold: float = 0.2
    voice_wake_boost: float = 2.0
    voice_idle_s: float = 20.0
    voice_hold_max_s: float = 600.0
    voice_input_device: str | None = None
    voice_output_device: str | None = None
    voice_require_headphones: bool = True
    voice_headphone_devices: tuple[str, ...] = ()
    voice_stt_model: str = "flux-general-multi"
    voice_stt_languages: tuple[str, ...] = ("en", "hi")
    voice_eot_threshold: float = 0.7
    voice_eager_eot_threshold: float | None = None
    voice_eot_timeout_ms: int = 5000
    voice_mouth_model: str = DEFAULT_VOICE_MOUTH_MODEL
    voice_mouth_max_tokens: int = 400
    voice_tts_provider: str = "elevenlabs"
    voice_tts_model: str = "eleven_flash_v2_5"
    voice_tts_voice: str = ""
    voice_tts_fallback: str = "deepgram"
    voice_tts_speed: float | None = None
    voice_tts_stability: float | None = None
    voice_tts_similarity_boost: float | None = None
    voice_tts_style: float | None = None
    voice_tts_speaker_boost: bool | None = None
    voice_usd_per_stt_min: float = 0.0078
    voice_usd_per_1k_tts_chars: dict[str, float] = field(
        default_factory=lambda: {"elevenlabs": 0.05, "deepgram": 0.030}
    )
    voice_grounding: bool = True    # on again 2026-09-25: the service's embedder over its pipe (tray ~210 MB in a conversation); off by itself when no service answers
    voice_grounding_wait_ms: float = 60.0
    voice_grounding_facts: int = 4
    voice_settle_s: float = 1.5
    voice_settle_max_s: float = 6.0
    voice_cooldown_s: float = 1.5
    voice_stale_turns: int = 3
    voice_progress_lines: int = 2
    voice_result_sentences: int = 2
    voice_result_chars: int = 280
    voice_brain_effort: str = "low"
    voice_brain_thinking: bool = False
    voice_brain_portrait: bool = False
    voice_brain_model: str = ""
    voice_final_gap_s: float = 0.3
    voice_progress_pause_s: float = 8.0
    voice_hands: bool = True
    voice_desk_wait_ms: float = 250.0

    def __post_init__(self) -> None:
        self.model = resolve_model(self.model)
        self.effort = resolve_effort(self.effort)
        self.voice_brain_effort = resolve_effort(self.voice_brain_effort) if self.voice_brain_effort else ""

    @property
    def sessions_dir(self) -> Path:
        """Absolute directory for session logs."""
        log_dir = Path(self.log_dir)
        return log_dir if log_dir.is_absolute() else self.project_root / log_dir

    @property
    def voice_log_dir(self) -> Path:
        """Absolute directory of the content-free voice logs (``logs/voice``)."""
        return self.sessions_dir.parent / "voice"

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
