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

from yuki.core.models import resolve_backend as _resolve_backend
from yuki.core.paths import state_root as _state_root

ScreenshotPolicy = Literal["never", "ask", "auto"]

#: Which requests the new brain (``yuki.brain``: one plan call, the executor, one done-check) takes: ``loop`` none
#: (today's ``yuki.agent.loop`` for everything), ``typed`` the typed ones (the ask bar, the chat page: release 17a),
#: ``all`` typed and spoken (release 17b, with the voice wiring). The one rollback switch (brain-design.md 10.1, M10).
BRAIN_MODES: tuple[str, ...] = ("loop", "typed", "all")


def _brain_default() -> str:
    """``YUKI_BRAIN``, else ``brain`` in ``%LOCALAPPDATA%/Yuki/settings.json``, else ``loop`` (read once, at start)."""
    forced = (os.environ.get("YUKI_BRAIN") or "").strip().lower()
    if forced in BRAIN_MODES:
        return forced
    try:
        import json

        from yuki.core.models import prefs_path

        data = json.loads(prefs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return "loop"
    value = str(data.get("brain") or "").strip().lower() if isinstance(data, dict) else ""
    return value if value in BRAIN_MODES else "loop"


#: The skills switch (failure fix-plan Item 1, 10 Oct): ``on`` everything (the shipped capabilities and the skills Yuki
#: learned from the user), ``capabilities`` the shipped capabilities only (the learned rows are not loaded, so no menu,
#: tool enum, planner or card sees them), ``off`` no skills anywhere (the old hands and tools: ``Settings.skills``).
SKILLS_MODES: tuple[str, ...] = ("on", "capabilities", "off")
#: The values the switch accepts (settings.json also takes JSON ``true`` / ``false``; ``YUKI_SKILLS`` is text). Config
#: words, not the user's speech. Anything else is not a switch value and leaves the default (``on``).
_SKILLS_WORDS: dict[str, str] = {
    "on": "on", "true": "on", "yes": "on", "1": "on",
    "capabilities": "capabilities", "capability": "capabilities",
    "off": "off", "false": "off", "no": "off", "0": "off",
}


def skills_mode_of(value: object) -> str | None:
    """The switch mode a settings value names (:data:`SKILLS_MODES`), ``None`` when it names none: a bool, ``0`` /
    ``1``, or one of the switch words in any case."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, int) and value in (0, 1):
        return "on" if value else "off"
    if isinstance(value, str):
        return _SKILLS_WORDS.get(value.strip().lower())
    return None


def _skills_default() -> str:
    """``YUKI_SKILLS``, else ``skills`` in ``%LOCALAPPDATA%/Yuki/settings.json``, else ``on`` (:data:`SKILLS_MODES`),
    the same way :func:`_brain_default` reads ``brain``."""
    forced = skills_mode_of(os.environ.get("YUKI_SKILLS") or "")
    if forced is not None:
        return forced
    try:
        import json

        from yuki.core.models import prefs_path

        data = json.loads(prefs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return "on"
    value = skills_mode_of(data.get("skills")) if isinstance(data, dict) else None
    return value if value is not None else "on"


#: :func:`skills_mode`'s last read: ``((YUKI_SKILLS, settings.json's mtime and size), mode)``.
_skills_seen: tuple[tuple[str, tuple[int, int] | None], str] | None = None


def skills_mode() -> str:
    """The skills switch as a fresh ``Settings()`` would read it now (:func:`_skills_default`), read again only when
    ``YUKI_SKILLS`` or settings.json changed (one stat a call). For code that holds no ``Settings``: the catalog (a
    learned row is loaded only with ``on``) and a run with no settings handed in (a routine card's), which before this
    always counted as on."""
    global _skills_seen
    try:
        from yuki.core.models import prefs_path

        st = prefs_path().stat()
        stamp: tuple[int, int] | None = (int(st.st_mtime_ns), int(st.st_size))
    except (OSError, ImportError):
        stamp = None
    key = (os.environ.get("YUKI_SKILLS") or "", stamp)
    seen = _skills_seen
    if seen is not None and seen[0] == key:
        return seen[1]
    mode = _skills_default()
    _skills_seen = (key, mode)
    return mode


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

#: The same models through the US inference profiles (a failover target, yuki.agent.failover).
for _alias, _rates in (("sonnet", DEFAULT_PRICING[MODEL_ALIASES["sonnet"]]),
                       ("opus", DEFAULT_PRICING[MODEL_ALIASES["opus"]])):
    DEFAULT_PRICING[f"us.anthropic.claude-{_alias}-5"] = dict(_rates)

#: Failover chains (``Settings.model_fallbacks``): the same model in another region first (the
#: global profile from us-east-1, then the US profile), then a sibling model. Thinking blocks of
#: the primary are ignored by a sibling (the API drops another model's blocks), so a switch
#: mid-task is safe; Haiku takes no effort or adaptive thinking, so no chain switches *to* Haiku.
DEFAULT_FALLBACKS: dict[str, list[dict[str, str | None]]] = {
    MODEL_ALIASES["sonnet"]: [
        {"model": MODEL_ALIASES["sonnet"], "region": "us-east-1"},
        {"model": "us.anthropic.claude-sonnet-5", "region": "us-east-1"},
        {"model": MODEL_ALIASES["opus"], "region": None},
    ],
    MODEL_ALIASES["opus"]: [
        {"model": MODEL_ALIASES["opus"], "region": "us-east-1"},
        {"model": MODEL_ALIASES["sonnet"], "region": None},
    ],
    DEFAULT_VOICE_MOUTH_MODEL: [
        {"model": DEFAULT_VOICE_MOUTH_MODEL, "region": "us-east-1"},
        {"model": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "region": "us-east-1"},
        {"model": MODEL_ALIASES["sonnet"], "region": None},
    ],
}


@dataclass
class Settings:
    """All knobs for one Yuki process.

    Attributes:
        model: Bedrock model id used for the next request. Mutable so the CLI's
            ``/model`` command can switch models mid-conversation.
        aws_region: Region passed to :class:`anthropic.AnthropicBedrock`.
        model_backend: ``bedrock`` (AnthropicBedrock, ``AWS_BEARER_TOKEN_BEDROCK``) or ``anthropic``
            (the Anthropic API, ``ANTHROPIC_API_KEY``); model ids stay Bedrock ids either way and
            are translated per request (:mod:`yuki.core.models`). Default: ``YUKI_MODEL_BACKEND``,
            then ``%LOCALAPPDATA%/Yuki/settings.json``, then whichever key is set.
        project_root: Base directory that ``log_dir`` is resolved against.
        log_dir: Directory holding one JSONL file (and one screenshot folder)
            per session.
        screenshot_policy: Always ``never``: screenshots are refused (the user's rule: tree capture only, no
            screenshots). Any other value given is clamped to ``never`` at construction (brain-design.md 5, L1);
            the setting itself goes with the old brain (bPrune).
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
        ask_park_s: A question the user has not answered within this many seconds sets its request aside, so
            the lane runs what comes next; a later answer carries it on (v3.14b; a newer request on the lane sets
            it aside at once). The six answered questions in the live logs of 27-29 Sept took 3-43 s.
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
            meeting is on (on since v3.4, the user's wish, 2026-09-26). A
            conversation started or running during a call is in call mode: the
            mouth decides whether each utterance was for Yuki or for the people on
            the call and stays quiet on the latter (docs/VOICE.md §25).
        voice_wake_threshold: The keyword spotter's trigger threshold (higher
            = fewer false wakes, more misses); measured in docs/VOICE.md §22.
        voice_wake_boost: The spotter's boosting score for the keyword.
        voice_idle_s: Quiet seconds (nobody speaking, no result owed) that
            end a conversation after its first exchange.
        voice_idle_per_exchange_s / voice_idle_max_s: Each further exchange with
            Yuki (a user turn she answered) adds this much to that quiet time, up
            to the maximum: a back-and-forth is not cut off mid-thought.
        voice_call_idle_s: In call mode, the conversation ends this long after the
            last exchange with Yuki (talk to the others does not count), so the
            rest of the call is not streamed or judged.
        voice_hold_max_s: Longest a quiet conversation stays open waiting to
            speak a running task's result; past it the result lands as a card.
        voice_input_device: Microphone (name, or part of it). ``None``: the
            Windows default communications input.
        voice_output_device: Output device name. ``None``: the default output.
        voice_require_headphones: Refuse to start a conversation while the
            sound comes out of speakers (the v3.3 rule; off since the echo guard).
        voice_headphone_devices: Outputs to count as headphones although
            Windows calls them something else ("Speakers" on an interface).
        voice_echo: How a conversation on speakers keeps Yuki from hearing
            herself (docs/VOICE.md §11): ``auto`` (WebRTC echo cancellation, else
            half duplex), ``half`` (the mic is silenced while Yuki is audible; no
            barge-in) or ``full`` (treat the output as headphones). Headphones are
            always full duplex.
        voice_echo_tail_ms: Half duplex: how long after Yuki's audio ends the mic
            stays silenced (the room's echo and the devices' latency).
        voice_wake_chime: A soft two-note chime the moment the wake word is
            heard, so the user knows Yuki is listening before the pill is seen.
        voice_notice_repeat_s: A failure to start or a conversation ending in an
            error plays a short tone and shows the reason in the pill; the same
            reason again within this many seconds only shows the pill.
        voice_stt_model / voice_stt_languages: Deepgram Flux model and the
            language hints it is given.
        voice_eot_threshold / voice_eager_eot_threshold / voice_eot_timeout_ms:
            Flux's end-of-turn settings (eager off: ``None``).
        voice_mouth_model: Bedrock model id of the fast mouth.
        voice_mouth_max_tokens: Output ceiling for one spoken reply.
        voice_tts_provider: ``gemini`` (Gemini TTS, the default since 2026-09-27: voice Leda, sound
            tags and a per-turn style the mouth chooses), ``elevenlabs`` or ``deepgram`` (Aura-2,
            English only). ``voice.json`` ``tts_provider`` wins.
        voice_gemini_model / voice_gemini_voice: The Gemini TTS model and prebuilt voice
            (``voice.json`` ``gemini_model`` / ``gemini_voice`` win).
        voice_gemini_style: A baseline delivery style sent with every reply the mouth gives no style
            of its own (``""``: none; ``voice.json`` ``gemini_style`` wins).
        voice_gemini_first_audio_s: A Gemini request with no audio by then has failed (it counts towards the
            circuit breaker below); measured first audio ~1.0-1.3 s.
        voice_tts_hedge_s: A Gemini piece with no first byte this long after its request also starts on the
            fallback voice (Aura); whichever plays first wins and the other is cancelled (v3.16; ``0``: off).
        voice_tts_breaker_failures / voice_tts_breaker_window_s / voice_tts_breaker_open_s: Gemini failures
            (no audio in time, an error, a silent stream) this many within the window send the following pieces
            straight to the fallback voice for the open time, then Gemini is tried again (v3.16: per piece, never
            for the rest of a conversation; a quota or key refusal opens it for ``DOWN_FOR_S``).
        voice_tts_model / voice_tts_voice: The ElevenLabs (or Aura) model and voice id
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
        voice_progress_lines: Most spoken "still on it" lines per task (v3.9: said only into silence).
        voice_wait_line_s / voice_wait_again_s: Seconds of silence (nobody speaking) before a running
            task's one "still on it" line, and before a second one (v3.9; intermediate chatter is
            never spoken otherwise, and the outcome is said once).
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
            disabled for them; typed ones keep adaptive thinking). v3.13: on (adaptive) with
            effort medium; v3.14: off with effort low for the fast first pass, and
            the escalation below when a measured signal says it needs judgement.
        voice_brain_escalate_effort / voice_brain_escalate_thinking: The profile a spoken
            request moves to for its next model calls once an action fails or its report
            does not verify its effect, the loop breaker trips, the user is asked, or a claim
            has no evidence (``Agent.run(escalate=)``, docs/VOICE.md v3.14). Empty / ``None``:
            no escalation.
        voice_brain_portrait: Attach the rest of the portrait and the resume to a
            spoken request's memory block (off: the profile and rules layers only,
            plus to-dos, know-how and commitments).
        voice_brain_model: The thinker's model for spoken requests (alias or id;
            empty: the brain's own ``model``). Measured in docs/VOICE.md §7.
        voice_progress_pause_s: A progress line of a running task is said only
            after Yuki has been quiet this long (a real pause), never after its result.
        voice_step_lines / voice_step_silence_s / voice_step_after_s / voice_step_long_silence_s: The
            thinker's own step lines (``say_now``) said by code (v3.14, :mod:`yuki.voice.steplines`), only while
            the step runs: after this much silence and this long into the step, or at its start once the silence
            is already the long one; never once the step has returned (v3.14b).
        voice_hands: Give the mouth its own single-step desktop tools (docs/VOICE.md §5.6).
        voice_desk_wait_ms: How long a mouth call waits for the desktop summary.
        voice_keyterms_max: At most this many names and terms from memory are given to speech
            recognition as keyterms per conversation (docs/VOICE.md §9, v3.5).
        voice_wake_process: Run the wake-word listener in its own process (v3.12), so the app's other
            threads never hold it back; in-process when that cannot start.
        voice_standby: Keep the next conversation built and connected, dormant, while listening for the name
            (v3.11): speech recognition's socket open with no audio sent, the caches warm; the name wakes it at once.
        voice_stt_provider: ``deepgram`` (Flux, the default) or ``local`` (Parakeet on this PC with Silero VAD and
            Smart Turn: private and offline, English only, ~1 s slower to first audio here; report 67b and 86).
            ``voice.json`` ``stt_provider`` wins. ``voice_tts_provider`` may be ``local`` too (``voice.json``
            ``tts_voice`` names the voice: ``supertonic:F1``, ``kokoro:af_heart``; yuki.voice.local_models).
        voice_local_keep_loaded: Keep the speech models on this PC loaded all the time (no load at each wake, ~1 GB
            held); off: loaded at the wake, unloaded ``voice_local_unload_s`` after the last conversation.
            ``voice.json`` ``local_keep_loaded`` wins.
        voice_local_threads / voice_local_hotword_score: Threads per local model (67b: 2; more did not help) and how
            strongly memory's names are boosted in local recognition (67b: 2.0; capped at 3.0, where names repeat).
        voice_local_models_dir: Where the local models are (``""``: ``%LOCALAPPDATA%\\Yuki\\voice\\models``).
        voice_cache_refresh_s: While dormant (and the user is at the PC), the talker's and thinker's prompt caches
            (their stable layers are kept an hour) are read again this often.
        voice_screen_terms: At most this many names on the screen in front (its list items, chats, tabs,
            links, buttons) are added to them, refreshed as the screen changes (v3.10); 0 = off.
        voice_speech_guard: Check every line with the fast model before the voice says it: never its
            reasoning, talk about the user, its inner workings or a repeat (yuki.voice.speechguard, v3.10).
        voice_speech_guard_timeout_s: The check's verdict is waited for at most this long (fails open).
        voice_speech_channel: What decides which of the talker's words are spoken (v3.16). ``"guard"``: the
            spoken-line check above blocks lines (as since v3.10). ``"marker"``: every reply starts with a fixed
            marker line (``say`` / ``act`` / ``quiet``, :data:`yuki.voice.mouth.MARKERS`) and only text after
            ``say`` is spoken; unmarked text never is (the turn is answered again), and the check runs in shadow
            mode: it scores and logs every spoken line but never blocks.
        voice_end_tone: When a conversation ends by itself (quiet in a call, or quiet for its idle time) after
            at least one exchange, a soft falling tone says so (v3.16, non-speech; the pill line "Say 'Yuki' to
            talk again" is shown either way). ``voice.json`` ``end_tone`` (the tray) wins.
        voice_prewarm: With the wake word on, load the conversation's code (Pipecat, the services)
            and do the one-off checks in the background after start-up, so the first "Yuki" is
            heard at once (docs/VOICE.md §26, v3.5). Costs RAM while idle.
        voice_prewarm_delay_s: How long after start-up the prewarm begins.
        voice_results_by_code: A verified finish, or a thinker result short enough to say whole (2 sentences /
            280 characters, ``voice_result_*``), is said by code word for word in Yuki's one voice, with no talker run
            (v3.17, C1); longer ones, questions, failures and late ones still go to the talker.
        voice_prefetch_results: The sentences of the thinker's done message are fetched by the voice as each one is
            written (v3.17, C2; Gemini only), so a result said by code plays the moment it is accepted.
        voice_router: A small model call decides, from the user's words alone, whether the turn needs the thinker,
            and starts it ~1 s after they stop instead of when the talker begins its hand-off (v3.17, E3a). On since
            vGame (5 Oct: the user asked for the thinker to start at once); in a game or a call only on words said to
            Yuki by name (docs/research/revamp-2026-10-02/26-build-voice-speed.md, 79-build-vGame.md).
        voice_fast_dismiss / voice_fast_ack: vGame (79-build-vGame.md). A turn that is only the user's words for "go
            away" (``yuki.voice.fastpath.DISMISS_PHRASES``) ends the conversation on its final transcript, with no
            model call; a turn said to Yuki by name gets an instant "Mm-hm" in her voice ("Yeah?" to the name alone).
        voice_focus_mode: vGame. While a full-screen game is in front or a call is on: replies short and quieter, no
            follow-ups or small talk, no step lines, a rescued sound-alike never wakes it, and
            ``voice_focus_close_on_quiet``: a turn the talker judged not for Yuki ends the conversation at once,
            silently. ``voice_focus_reply_words`` caps a spoken reply there.
        voice_name_while_talking: vGame. The wake word keeps listening during a conversation; a turn it hears the
            name in is said to Yuki (measured evidence for the acknowledgement, the router and the talker).
        voice_stt_warm: vGame. Half a second of silence goes to speech recognition when a standby connects, so the
            first words after the wake come back ~0.5 s sooner (measured).
        voice_router_model: The router's model (empty: the talker's model).
        voice_router_adopt_s: A thinker the router started is withdrawn, unseen, if the talker has not handed the
            turn over within this many seconds of its start (or chose another way).
        voice_talker: bTalk (brain rebuild rev 2, design 3 and 10.1): the talker's prompt and hand-off tool, ``"full"``
            (today's, ``tell_yuki``) or ``"lean"`` (``hand_over``: the exact words go by code). Empty (the default):
            derived from ``brain``: ``loop`` / ``typed`` -> full, ``all`` -> lean. Set it only for debugging. The router
            (``voice_router``) is off under ``brain = "all"`` whatever it says (``yuki.voice.mouth.router_on``); the
            only supported pairs are loop / typed -> (full, router as set) and all -> (lean, off).
        voice_reply_words_cap: The talker's spoken reply ends at its next sentence end once it has said this many
            words; the rest is held for "want the rest?" (v3.17, H2). 0 = off.
        voice_qos: While a conversation is live, the process is opted out of Windows' power throttling and the voice
            loop, the speaker's writer and the microphone's callback threads too (v3.17, D7).
        voice_mmcss: Those threads also join the MMCSS "Audio" task (Windows' scheduler for audio threads).
        voice_practice: The talker gets practice memory's lines for the user's words (v3.18): the Lessons line (the
            top pitfalls and habits of the apps in play, at most ``voice_lessons_chars``) and the Learned ways line.
        voice_skills: The talker may run a learned way as its hand (``run_skill``, v3.18), step by step, each step
            checked, handed to the thinker at the first deviation; only ways above both floors are offered.
        voice_skill_min_cosine: A way is offered as a skill only this near the words (measured, 29-build §2.1).
        voice_skill_min_confidence: ... and only this sure (memory's computed confidence; above one source's 0.667).
        voice_lessons_chars: The Lessons line's size in the talker's volatile block.
        voice_hand_traces: Every talker hand (and learned-way run) is kept as a run trace in practice memory so the
            learners see the hands too (v3.18; written off the voice loop).
        voice_reflex: lSpeed (live test 17 Oct 2026, problem 1; ``yuki.voice.reflex``). The user's own words for which
            one talker hand (a media key, a relative volume change or mute, a window brought up, restored or
            minimised, a scroll, an app opened) was verified done, and nothing else was done for that turn, are kept
            (encrypted, in memory); the next time exactly those words are heard, that hand runs at once at the end of
            their turn, with no model in front, and the talker only speaks. Never for sends, closes, tabs, messages,
            joins or with a game in front; words that mapped to two different calls are dropped; a correction in the
            next words (a small model call, within ``voice_reflex_correct_s``) drops the entry.
        voice_reflex_at: When a reflex acts: ``"eager"`` (Flux's eager end of turn, measured 17 Oct: median 0.48-0.67 s
            after speech; the final transcript is re-checked and nothing acts twice) or ``"final"`` (the final
            transcript, 0.32-0.48 s later).
        voice_reflex_wait_s: The talker's call for those words waits at most this long for the reflex's outcome (a
            hand takes 4-400 ms), so it is told it is done and only speaks.
        voice_reflex_seed_days / voice_reflex_seed_skip: The first seeding reads the user's own repeated talker hands
            of this many days (practice memory's hand traces), leaving out sessions whose id starts with one of these
            (the live test harness's own).
        voice_practice, voice_skills (Phase S, D15): the old talker learning path (the Lessons and Learned ways lines,
            the step-list ``run_skill``) is off by default. It never runs beside the skill path: with ``skills`` on it
            stays off whatever these say; with ``skills = false`` they work as before S (``voice_practice`` the lines,
            ``voice_skills`` also the step-list hand). Deleted in phase P.
        skills: The skill path (PLAN.md Phase S, docs/research/audit-2026-10-09): the talker's Skills menu and its
            ``run_skill`` / ``keep_usual`` hands, the thinker's ``run_skill`` tool. With a ``volume`` skill loaded the
            talker's own ``app_volume`` hand, its volume keys and the thinker's ``app_volume`` tool are off; ``false``
            brings the old behaviour back everywhere (those hands and tools, no menu, no pinned skill). Read like
            ``brain`` (failure fix-plan Item 1): ``YUKI_SKILLS``, else ``"skills"`` in
            ``%LOCALAPPDATA%/Yuki/settings.json``, else on (:func:`_skills_default`). The values: ``true`` / ``"on"``
            everything; ``"capabilities"`` the shipped capabilities only (``skills_learned`` false); ``false`` /
            ``"off"`` no skills anywhere (``skills`` false). Code with no ``Settings`` reads the same switch
            (:func:`skills_mode`).
        skills_learned: The skills Yuki learned from the user are loaded (``yuki.skills.catalog``): true only with the
            switch ``on``; ``"capabilities"`` and ``"off"`` leave the learned rows out of the catalog, so no menu (the
            talker's, the thinker's, the planner's), tool enum or routine card has them. The rows stay in the database
            and the Playbook; switching back brings them back.
        skills_max: At most this many skills (one per app and end state).
        skills_menu_chars: The menu's size cap (characters), in the talker's cached hands block and the thinker's
            ``run_skill`` description.
        skills_disable_after: Contradicted done-checks in a row before a rung is switched off (retried after 24 h or
            when its connection file changes); "couldn't confirm" never counts.
        skills_volume_step: The volume step for "a bit louder" until the user's own usual step is known.
        youtube_feed_budget_s: Phase T: how long the fresh-uploads strip may take (public feeds, no key).
        youtube_fresh_days: Phase T: an upload counts as fresh this many days (14: map-routines T1.4; picks 7 and 9
            days old were seen).
    """

    model: str = DEFAULT_MODEL
    aws_region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "ap-south-1"))
    model_backend: str = field(default_factory=lambda: _resolve_backend())
    #: Base of ``logs/``: the project in a dev checkout, ``%LOCALAPPDATA%\Yuki`` in the installed app.
    project_root: Path = field(default_factory=lambda: _state_root())
    log_dir: Path = Path("logs/sessions")
    screenshot_policy: ScreenshotPolicy = "never"
    max_tokens: int = 8000
    thinking_display: Literal["summarized", "omitted"] = "summarized"
    stream: bool = True
    max_steps: int = 40
    keep_perception_turns: int = 2
    tool_timeout_s: float = 20.0
    ask_park_s: float = 120.0
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
    voice_wake_during_calls: bool = True
    voice_wake_threshold: float = 0.2
    voice_wake_boost: float = 2.0
    voice_idle_s: float = 20.0
    voice_idle_per_exchange_s: float = 8.0
    voice_idle_max_s: float = 45.0
    voice_call_idle_s: float = 10.0
    voice_hold_max_s: float = 600.0
    voice_input_device: str | None = None
    voice_output_device: str | None = None
    voice_require_headphones: bool = False
    voice_headphone_devices: tuple[str, ...] = ()
    voice_echo: str = "auto"
    voice_echo_tail_ms: float = 400.0
    voice_wake_chime: bool = True
    voice_notice_repeat_s: float = 60.0
    voice_stt_model: str = "flux-general-multi"
    voice_stt_languages: tuple[str, ...] = ("en", "hi")
    voice_eot_threshold: float = 0.7
    voice_eager_eot_threshold: float | None = 0.4
    voice_eot_timeout_ms: int = 5000
    voice_mouth_model: str = DEFAULT_VOICE_MOUTH_MODEL
    voice_mouth_max_tokens: int = 400
    voice_tts_provider: str = "gemini"
    voice_gemini_model: str = "gemini-3.8-flash-lite-tts"
    voice_gemini_voice: str = "Leda"
    voice_gemini_style: str = ""
    voice_gemini_first_audio_s: float = 3.0
    voice_tts_hedge_s: float = 1.8
    voice_tts_breaker_failures: int = 2
    voice_tts_breaker_window_s: float = 60.0
    voice_tts_breaker_open_s: float = 60.0
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
        default_factory=lambda: {"elevenlabs": 0.05, "deepgram": 0.030, "gemini": 0.010}
    )
    voice_grounding: bool = True    # on again 2026-09-25: the service's embedder over its pipe (tray ~210 MB in a conversation); off by itself when no service answers
    voice_grounding_wait_ms: float = 60.0
    voice_grounding_facts: int = 4
    # v3.14: where each turn's memory for the talker comes from: "related" (the memory builder's whole-memory
    # related_text(voice=True): words, screen, threads of work; measured +25 ms on the talker's first token and
    # lookups p50 49 ms vs 80 ms, docs/VOICE.md v3.14) or "relevant" (fast memory + know-how by the words, v3.5-v3.13;
    # also what "related" falls back to where memory has no related_text)
    voice_grounding_source: str = "related"
    voice_settle_s: float = 1.5
    voice_settle_max_s: float = 6.0
    voice_cooldown_s: float = 1.5
    voice_stale_turns: int = 3
    voice_progress_lines: int = 2
    voice_wait_line_s: float = 6.0
    voice_wait_again_s: float = 15.0
    voice_result_sentences: int = 2
    voice_result_chars: int = 280
    # v3.13 (the user's call, measured on the Warp stub: full screen 4/4 vs 1/4 at low with thinking off); v3.14: a
    # fast first pass (low, thinking off) that escalates the same request to medium + adaptive on a measured signal,
    # with outcomes said only on a verified report (measured in docs/VOICE.md v3.14).
    voice_brain_effort: str = "low"
    voice_brain_thinking: bool = False
    voice_brain_escalate_effort: str = "medium"
    voice_brain_escalate_thinking: bool | None = True
    voice_brain_portrait: bool = False
    voice_brain_model: str = ""
    voice_final_gap_s: float = 0.3
    voice_progress_pause_s: float = 8.0
    # v3.14 "one voice narrates live steps": the thinker's say_now line is said by code only into a noticeable
    # silence while its step runs (nobody spoke for voice_step_silence_s and the step has run voice_step_after_s, or
    # the silence is already voice_step_long_silence_s); never after the step returned (v3.14b); docs/VOICE.md.
    voice_step_lines: bool = True
    voice_step_silence_s: float = 2.5
    voice_step_after_s: float = 0.8
    voice_step_long_silence_s: float = 5.0
    # v3.14: her own voice for short lines she says often, kept on disk (yuki.voice.ttscache; "": the user's voice
    # folder, LOCALAPPDATA/Yuki/voice/tts-cache).
    # v3.14b: the process runs at above-normal priority while a conversation is live (yuki.voice.priority; measured on
    # a fully loaded PC: loop wake-up p99 8-11 ms vs 67-87 ms at normal).
    voice_priority_boost: bool = True
    voice_tts_cache: bool = True
    voice_tts_cache_dir: str = ""
    voice_hands: bool = True
    voice_desk_wait_ms: float = 250.0
    voice_keyterms_max: int = 50
    voice_screen_terms: int = 30
    voice_standby: bool = True
    voice_wake_process: bool = True
    voice_cache_refresh_s: float = 3000.0
    voice_stt_provider: str = "deepgram"
    voice_local_keep_loaded: bool = False
    voice_local_unload_s: float = 300.0
    voice_local_threads: int = 2
    voice_local_hotword_score: float = 2.0
    voice_local_models_dir: str = ""
    voice_speech_guard: bool = True
    voice_speech_guard_timeout_s: float = 2.5
    # v3.16: "marker" by the offline eval (docs/research/revamp-2026-10-02/21-build-A1-A2.md): 220 replies, 0 unmarked,
    # 0 reasoning in speech; "guard" (voice.json speech_channel) brings back the blocking line check
    voice_speech_channel: str = "marker"
    voice_end_tone: bool = True
    voice_prewarm: bool = True
    voice_prewarm_delay_s: float = 20.0
    # v3.17 voice speed (docs/research/revamp-2026-10-02/26-build-voice-speed.md)
    voice_results_by_code: bool = True
    voice_prefetch_results: bool = True
    voice_router: bool = True
    voice_router_model: str = ""
    voice_router_adopt_s: float = 2.5
    voice_talker: str = ""
    voice_reply_words_cap: int = 45
    # vGame (docs/research/revamp-2026-10-02/79-build-vGame.md)
    voice_fast_dismiss: bool = True
    voice_fast_ack: bool = True
    voice_focus_mode: bool = True
    voice_focus_close_on_quiet: bool = True
    voice_focus_reply_words: int = 18
    voice_name_while_talking: bool = True
    voice_stt_warm: bool = True
    voice_qos: bool = True
    voice_mmcss: bool = True
    # v3.18 practice memory for the talker (docs/research/revamp-2026-10-02/29-build-voice-procedures.md); off since
    # phase S (D15): never beside the skill path, back only with skills = false (and these on)
    voice_practice: bool = False
    voice_skills: bool = False
    voice_skill_min_cosine: float = 0.56
    voice_skill_min_confidence: float = 0.70
    voice_lessons_chars: int = 400
    voice_hand_traces: bool = True
    # lSpeed (live test 17 Oct 2026, problem 1): the learned reflex path (yuki.voice.reflex)
    voice_reflex: bool = True
    voice_reflex_at: str = "eager"
    voice_reflex_wait_s: float = 0.45
    voice_reflex_correct_s: float = 20.0
    voice_reflex_seed_days: float = 30.0
    voice_reflex_seed_skip: tuple[str, ...] = ("livetest-",)
    # Phase S skills (docs/research/audit-2026-10-09/PLAN.md; yuki.skills.DEFAULTS mirrors these). The switch (failure
    # fix-plan Item 1): YUKI_SKILLS, else "skills" in settings.json, else on; "capabilities" hides the learned rows
    skills: bool = field(default_factory=lambda: skills_mode() != "off")
    skills_learned: bool = field(default_factory=lambda: skills_mode() == "on")
    skills_max: int = 20
    skills_menu_chars: int = 2400
    skills_disable_after: int = 2
    skills_volume_step: int = 10
    youtube_feed_budget_s: float = 1.5
    youtube_fresh_days: int = 14
    # -- reliability (docs/research/openclaw-harness-features.md, the reliability group) --
    #: Model failover (yuki.agent.failover): per model id, the targets tried after it keeps failing
    #: (throttled, 5xx, overloaded, timed out, unreachable, model or profile not found), in order:
    #: the same model through another region or inference profile first, then a sibling model.
    #: Each is ``{"model": id, "region": region or None (the primary's)}``.
    model_fallbacks: dict[str, list[dict[str, str | None]]] = field(
        default_factory=lambda: {k: [dict(t) for t in v] for k, v in DEFAULT_FALLBACKS.items()})
    #: Retries on one target before moving on, and the backoff (seconds, doubled each time, capped;
    #: a ``retry-after`` the service sends is honoured up to the cap).
    model_retries: int = 2
    model_retry_base_s: float = 0.6
    model_retry_max_s: float = 4.0
    #: Seconds without a byte before a model call counts as timed out, and to connect.
    model_timeout_s: float = 90.0
    model_connect_timeout_s: float = 10.0
    #: The same, for the memory service's background model calls (``yuki.agent.failover.make_client``, role
    #: ``memory``: portrait, audit, journal, episodes, cards, call digests). They stream with adaptive thinking, and
    #: Sonnet's thinking streams no text by default (display "omitted"), so a long think is a silent stretch: the
    #: portrait audit's chunks went quiet for over 90 s and failed 10 of 10 nightly runs on ``model_timeout_s``
    #: (measured 28 Sep-5 Oct: every portrait ReadTimeout at 92-95 s; the calls that got through took up to 152 s).
    #: Nobody waits on these, so a hung call may take longer to give up.
    memory_model_timeout_s: float = 300.0
    #: After a failover worked, stay on that target this long before trying the primary again.
    failover_sticky_s: float = 300.0
    #: Loop breaker (yuki.agent.loopguard): the same failing result this many times trips it; this many
    #: steps in a row with every call failed trips it; after this many trips only ask_user/done may run.
    loop_breaker_repeats: int = 3
    loop_breaker_failed_steps: int = 4
    loop_breaker_max_trips: int = 2
    #: Daily model spend (agent + voice + memory) above which one notice is shown and logged: the floor of the line.
    #: Once a few days are known the line is measured instead (yuki.core.cost.CostAlert: 1.5 times a usual day of the
    #: last week, never under this), so it speaks only on an unusual day (report 76: the fixed line fired 7 of 7 days).
    daily_cost_alert_usd: float = 3.0
    #: Read-only helpers (yuki.agent.subagents): model, effort ("" = none sent), output cap per call,
    #: model calls per helper, wall seconds per helper, input tokens per helper, most at once.
    helpers_model: str = DEFAULT_VOICE_MOUTH_MODEL
    helpers_effort: str = ""
    helpers_max_tokens: int = 1200
    helpers_max_turns: int = 6
    helpers_timeout_s: float = 45.0
    helpers_max_input_tokens: int = 60000
    helpers_max: int = 4
    # -- eyes (2026-09-29, Phase 2; docs/ARCHITECTURE.md "Perception: Now") --
    #: Attach the live picture of the screen to the brain's per-turn situation: what the memory service read
    #: of every visible window (the one in front first), popups and toasts seen lately, and a live focus line
    #: (yuki.memory.now; ``Dispatcher.include_now``, set by the agent loop from this).
    now_in_situation: bool = True
    #: How long a turn waits for that picture from the memory service, and for the focus line (ms); each is
    #: left out when it does not answer in time (measured 2026-09-29: pipe p50 1.4 ms, focus p50 ~10 ms).
    now_wait_ms: float = 50.0
    focus_wait_ms: float = 50.0
    # -- revamp D, part 1: the brain's safety and speed (docs/research/revamp-2026-10-02/30b-build-D-brain.md) --
    #: The authorization judge (yuki.agent.judge): a separate Haiku call that sees only the user's words and the
    #: proposed call, for calls whose tool metadata can destroy or reach someone and kinds the autonomy policy holds
    #: below "do it and tell me". Its model, and how long a verdict may take before the call is held (fail closed).
    #: (``YUKI_JUDGE=0`` / ``YUKI_CONSULT=0`` turn the defaults off: the test suite does, so a scripted fake model
    #: client is never asked an extra question; the judge's and the consult's own tests turn them on.)
    judge: bool = field(default_factory=lambda: os.environ.get("YUKI_JUDGE", "1") != "0")
    judge_model: str = DEFAULT_VOICE_MOUTH_MODEL
    judge_timeout_s: float = 8.0
    #: Consult instead of escalating (yuki.agent.consult): the actor's cached profile stays fixed for the whole
    #: request; on a measured signal one side call (this model, effort, thinking on) over a short summary returns
    #: at most ~120 words of advice. Most consults per request; a pending send to a person is a signal too.
    consult: bool = field(default_factory=lambda: os.environ.get("YUKI_CONSULT", "1") != "0")
    consult_model: str = DEFAULT_MODEL
    consult_effort: str = "medium"
    consult_max: int = 2
    consult_before_send: bool = True
    consult_timeout_s: float = 30.0
    #: A send is confirmed from memory's capture of the chat: a new message of the user's in that app since the
    #: Enter, similar to the text, within this many seconds (yuki.agent.finish).
    send_confirm_s: float = 4.0
    #: Quieter hands (yuki.actions.leases): before global mouse or keys, wait while the user typed or moved the mouse
    #: within ``recent`` seconds, until ``quiet`` seconds pass with no input, at most ``max`` seconds (then nothing is
    #: sent and the brain is told).
    input_yield: bool = True
    input_yield_recent_s: float = 1.0
    input_yield_quiet_s: float = 0.8
    input_yield_max_s: float = 12.0
    #: V8 (3 Oct): how long recent input is watched to tell a key or click from the mouse moving (mouse moves alone
    #: never hold Yuki back), and how long a wait lasts before its one line is said (silent before).
    input_yield_probe_s: float = 0.25
    input_yield_notify_s: float = 3.0
    #: appTakeover (4 Oct): while the worker's request acts, the user's real keys, clicks or a big mouse movement pause
    #: it ("You took over"); Resume or Stop on the island, their words, or typing to Yuki goes on. A pause nobody
    #: resumes ends the request quietly after ``takeover_idle_stop_s``. The movement threshold is the user's own
    #: setting (``ui.json`` ``takeover_move_px``).
    takeover_pause: bool = True
    takeover_idle_stop_s: float = 600.0
    #: A request pack (Agent.build_pack) older than this is rebuilt for its screen half (the memory half is kept).
    pack_max_age_s: float = 20.0
    # -- the brain rebuild (docs/research/audit-2026-10-09/next/brain-design.md rev 2, release 17a) --
    #: Which requests the new brain takes (:data:`BRAIN_MODES`): ``loop`` (the default: nothing changes), ``typed``,
    #: ``all``. Read at each request's start, so flipping it back is the rollback (``YUKI_BRAIN`` or ``brain`` in
    #: ``settings.json`` at start).
    brain: str = field(default_factory=_brain_default)
    #: The planner's effort per channel (adaptive thinking on both; pinned for a whole request, tuned from the
    #: request logs).
    brain_effort_voice: str = "low"
    brain_effort_typed: str = "low"
    #: Act plans: replans after a step did not match (the call budget bounds the rest).
    brain_max_replans: int = 3
    #: The commit check's model (one small yes/no call per plan, only on steps that could send, delete, buy, close or
    #: join; also the read of the user's reply to Yuki's yes/no question): ``judge_model`` renamed.
    check_model: str = DEFAULT_VOICE_MOUTH_MODEL

    def __post_init__(self) -> None:
        if self.model_backend not in ("bedrock", "anthropic"):
            raise ValueError(f"model_backend must be 'bedrock' or 'anthropic', not {self.model_backend!r}")
        # screenshots never (brain-design.md 5, L1): the ask and auto values are gone; bPrune removes the setting
        self.screenshot_policy = "never"
        mode = str(self.brain or "").strip().lower()
        self.brain = mode if mode in BRAIN_MODES else "loop"
        # the skills switch (Item 1): a bool, or the switch's own word given as ``skills`` ("capabilities"); the
        # learned skills are never on with the skill path off
        switch = skills_mode_of(self.skills)
        if switch == "capabilities":
            self.skills, self.skills_learned = True, False
        else:
            self.skills = (switch != "off") if switch is not None else bool(self.skills)
        self.skills_learned = bool(self.skills and self.skills_learned)
        self.brain_effort_voice = _soft_effort(self.brain_effort_voice)
        self.brain_effort_typed = _soft_effort(self.brain_effort_typed)
        self.brain_max_replans = max(0, int(self.brain_max_replans or 0))
        self.model = resolve_model(self.model)
        self.effort = resolve_effort(self.effort)
        self.voice_brain_effort = resolve_effort(self.voice_brain_effort) if self.voice_brain_effort else ""
        self.voice_brain_escalate_effort = (resolve_effort(self.voice_brain_escalate_effort)
                                            if self.voice_brain_escalate_effort else "")

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
        """The ``$/1M tokens`` rates for ``model`` (alias or id), or ``None``.

        A model on another provider (``openai:gpt-5-mini``, Settings → AI) is priced by
        :mod:`yuki.agent.providers.pricing`: known list prices, OpenRouter's own, free and local at 0, else
        ``None`` (never another model's rate).
        """
        if ":" in model:
            from yuki.agent.providers.catalog import split_qualified

            if split_qualified(model) is not None:
                from yuki.agent.providers.pricing import rates

                return rates(model, self.pricing)
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

        After a call that Settings → AI sent to another provider, the callers still name the model they asked
        for (the memory workers price with their Claude id): the model that actually answered on this thread
        is priced instead (:func:`yuki.agent.providers.pricing.routed_name`), with OpenRouter's returned amount
        when it is that call's.
        """
        if model:
            from yuki.agent.providers.catalog import split_qualified
            from yuki.agent.providers.pricing import cost, routed_name

            name = model if split_qualified(model) is not None else routed_name(model)
            if name is not None:
                return cost(name, usage, self.pricing)
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


def _soft_effort(level: object) -> str:
    """A brain effort level, normalised; an unknown or empty one is ``low`` (the brain's default, never a crash)."""
    text = str(level or "").strip().lower()
    return text if text in EFFORT_LEVELS else "low"


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
