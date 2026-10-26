# Voice: talker + thinker, done in parallel — research and recommendations

**Date:** 2026-09-25
**Scope:** read-only research. Sources cloned to `%TEMP%\...\scratchpad\research\gradient-bang` and `...\pipecat-examples` (both `git clone --depth 1`, HEAD as of today), plus the installed `pipecat` 1.11.0 package at `<repo>\.venv\Lib\site-packages\pipecat`, plus Yuki's own `docs/VOICE.md` and `yuki/voice/*`, `yuki/agent/loop.py`, `yuki/agent/prompt.py`, `yuki/agent/tools.py`, `yuki/ui/runtime.py`.

**Goal of this document:** work out exactly how gradient-bang's voice bot keeps talking while a slower process thinks, extract the behavioural rules (not just the plumbing), and turn that into a concrete, file-by-file recommendation for Yuki, given the specific failures already observed (mouth inventing memory answers, mouth rewriting requests, long results read verbatim, ~3 s latency).

---

## 1. Gradient Bang's architecture: one fast "talker", many slow "thinkers"

Gradient Bang is *not* two LLMs listening to every turn side by side (that's word-wrangler's pattern, see §3). It is **one voice LLM inline in the main Pipecat pipeline** (the "talker") that **hands off** multi-step work to **background `TaskAgent` workers** (the "thinkers") over an internal pub/sub bus, and gets their output back through a speech-aware queue that decides when it's safe to say something.

```
user audio → STT → context aggregator → VOICE LLM (Orchestrator, "player" worker) → TTS → user
                                              │  start_task / steer_task / stop_task / query_task_progress
                                              ▼
                                        bus (WorkerBus)
                                              │
                                        TaskAgent workers (per ship/task, own LLM call loop)
                                              │  BusJobUpdateMessage (output/progress) ─┐
                                              │  BusTaskFinishNotification              │
                                              ▼                                         ▼
                                   SubagentNarrator (speech-gated queue) ── injects <event> XML
                                   as a new context message, run_llm=True when it's safe to speak
```

Key files (all in `gradientbang` clone, `src/gradientbang/`):
`bot.py`, `runtime/orchestrator.py`, `runtime/subagent_narrator.py`, `runtime/inference_gate.py`, `runtime/subagents/task_agent.py`, `runtime/tool_schema.py`, `runtime/event_relay.py`, `runtime/frames.py`, `runtime/idle_report.py`, `utils/llm_factory.py`, `utils/summary_formatters.py`, `prompts/agents/voice_agent.md`, `prompts/agents/task_agent.md`, `prompts/agents/task_progress.md`.

### 1.1 Does the thinker see every user turn? How is input fanned out?

No. Only the voice LLM sees the live conversation stream. The task agent is **not** subscribed to raw user turns; it receives:

- a **task description** at creation time (`start_task` tool arguments: `task_description`, `context`, optional `ship_id` — `tool_schema.py:1086-1118`);
- **steering messages** later, via `steer_task` (`tool_schema.py:1130-1150`) — explicitly described as "a priority override of its original task," not a raw transcript;
- game-server events relevant to its own ship/task, through the same declarative `EventRelay` routing used for the voice LLM (`runtime/event_relay.py:52-134`: `AppendRule`/`InferenceRule`/`Priority` per event type).

So the fan-out isn't "broadcast every turn to both models" — it's "the talker decides, per turn, whether this is worth summarising into a self-contained task/steer message for the thinker." That decision is a **tool call**, not a classifier (§1.2).

### 1.2 What decides talker-alone vs. hand-to-thinker? Where does it run?

It's an **LLM decision expressed as a tool call**, enforced by prompt discipline, not a keyword router or heuristic classifier. `prompts/agents/voice_agent.md` (lines 36-45) states the rule as a **commitment contract**:

> "If you decide a tool is needed, make the tool call in that same response... Phrases like 'I'll start a task', 'I'll check', 'let me look'... count as a commitment to act. If you use that kind of language, you MUST make the corresponding tool call in the same response."

And for the specific hand-off tool (lines 302-304):

> "When starting a task, call `start_task` in the SAME response. The tool call is mandatory — a spoken acknowledgement alone is NOT enough. Do not describe what the task will do, warn about complexity, or explain your approach beforehand. After calling `start_task`, add a brief spoken confirmation like 'On it, I'll report when it's ready.'"

This is functionally identical to the rule Yuki's mouth already has (`yuki/voice/mouth.py:50-57`: "say a few natural words first and call tell_yuki in that same reply... starting the tool does not by itself give you another turn"). Both projects arrived independently at the same fix for the same failure mode (a model that promises to act and then doesn't, in the same turn).

### 1.3 How does the talker know the thinker is working, and what does it say meanwhile?

Two mechanisms, one push and one pull, plus a synthetic "still going" trick that Yuki does not currently have:

- **Push (streamed narration):** every meaningful task event (`ACTION`, `STEP`, `EVENT`, `MESSAGE`, `INPUT`, `FINISHED` — a `TaskOutputType` enum) is appended to the task's log and forwarded live via `BusJobUpdateMessage` (`task_agent.py:1880-1932`, `_deliver_task_output`/`_queue_task_output`). This is fire-and-queued except at task completion, where it's awaited synchronously so the finish message can't race the queue and get lost (`task_agent.py:1943-1955`, comment: "`_output` uses fire-and-forget which races and loses").
- **Pull (on-demand status):** the voice LLM can call `query_task_progress` any time (`tool_schema.py:1153-1177`, description: "Check on what a running or recently finished task is doing by querying its task log. For best results, include `prompt` with the user's exact question."). This triggers a **separate, cheap, one-shot LLM call** (`task_agent.py:544-578`): a fresh `LLMContext` built from just the `task_progress.md` system prompt + the raw task log + "Give a concise status update on this task," run through `run_inference()` with no tool loop and no accumulated conversation — deliberately isolated so it can't hallucinate off stale context, and cheap because it's a single small call, not the full task agent re-planning. `task_progress.md:56` states the anti-fabrication rule directly: "Do NOT make up information not in the log."
- **Synthetic filler while genuinely silent** (`task_agent.py:1580-1602`, `_emit_synthetic_progress_message`): before running inference for a non-obvious reason (an event query, a steer, loading reference info, plotting a route), the task agent injects a canned one-liner into its own output stream *before the LLM call even returns* — "Analyzing query results...", "Replanning with new instructions...", "Reviewing loaded reference info...". A dedupe epoch counter (lines 1591-1601, incremented on `ACTION`/`EVENT`) stops the same filler repeating back-to-back, which would read as stuck/robotic.

**Anti-fabrication is structural, not just a prompt line.** `voice_agent.md` lines 286-301 ("Task Completion Reports") is explicit and gives worked bad/good examples:

> "When a `task.completed` event arrives, briefly tell the commander what the summary says... Restrict your report to the ships in the most recent `task.completed` event block. Do NOT volunteer status updates on other ships whose tasks are still in flight ... those tasks may have completed since the context you're seeing was assembled, and your speech will arrive seconds later. Stay silent on in-flight tasks until their own `task.completed` arrives."
> Bad example given: *"We're in sector 4867. Shields are full, warp at 374 out of 450..."* when only arrival was confirmed.

The task agent's own prompt mirrors this from the other side (`task_agent.md:51-52`): "NEVER output XML `<event>` blocks in assistant messages... NEVER generate fake events in your responses — only call tools." Two independent prompts, on two independent models, both told the same thing: **never invent an event; only ever react to a real one that already arrived.**

### 1.4 How do the thinker's results get back into the conversation, and when are they spoken?

Results arrive as a new **context message**, not as a tool-call return value, because `start_task`/`steer_task`/`stop_task` are **fire-and-forget acks** in gradient-bang, not held-open async calls like Yuki's `tell_yuki`. Look at `orchestrator.py:3454-3502` (`_handle_start_task_tool`): the tool call itself is acked immediately with `run_llm=False` (so the client's "thinking" spinner clears), and then a **separate synthetic `<event name="task.started">...` XML message** is appended to context with `run_llm=True` to make the voice LLM speak an acknowledgement. Later, when the task actually finishes, the *same* mechanism injects a `task.completed` event — but by then it goes through `SubagentNarrator` first, which decides **when** it's safe to run that inference:

`runtime/subagent_narrator.py` — a hand-rolled gated queue (already fully read; summarised here): reports are enqueued (`enqueue()`, line 97) and a `_drain()` loop (lines 178-254) holds them until:
1. **not stale** — if `min(user_stops, bot_stops) >= STALE_TURNS` (5), the report is silently folded into context (`run_llm=False`) instead of interrupting an unrelated conversation that has clearly moved on;
2. **hard gates clear** — no tool call in flight, no assistant cycle active, user not speaking, and the bot doesn't still owe the user a reply (`awaiting_bot_reply`);
3. **settle window** — `SETTLE_SECONDS = 2.0` of quiet since the last enqueue (capped at `MAX_SETTLE_SECONDS = 8.0` from the first), so two things that finish 500 ms apart get **coalesced into one spoken turn** instead of two back-to-back interruptions;
4. **cooldown** — `COOLDOWN_SECONDS = 1.5` after the bot's own last utterance, so a completion doesn't stack immediately behind an answer;
5. a final "yield once and recheck" (line 237) closes the race where the user starts talking in the exact instant all gates cleared.

This is layered with `InferenceGateState` (`runtime/inference_gate.py`, already fully read) — a *priority-ordered* deferred-inference scheduler (`combat_event` > `event` > `tool_result` > `llm_run`, lines 51-60) sitting **before** the aggregators, applying a 2 s post-bot-speech cooldown and a 1.5 s post-LLM "grace" before an event-triggered run fires (`bot.py:328-331`), with combat events explicitly allowed to **bypass** the cooldown when the viewer is a direct participant (line 173) — because in that one case, responsiveness beats politeness.

**Condensation for speech** happens at two layers, deterministic first, LLM second:
- **Deterministic, code-level** (`utils/summary_formatters.py`, ~30 formatter functions): full JSON payloads are turned into short strings *before* they ever reach an LLM context — UUIDs truncated to 6 chars (`_short_id`, lines 14, 29-45), domain-compact encodings (e.g. a port's prices rendered as one token string, lines 201-246), hard caps on event queries (20 events, 240 chars/line, 6000 chars total — lines 24-26), lists capped with "+N more" (lines 945-947), whole objects collapsed to one line (`status_update_summary`, lines 590-657).
- **LLM-level, on demand** (`task_progress.md`-prompted one-shot call, §1.3) — only invoked when the user actually asks "how's it going," not proactively.

### 1.5 Interruptions, follow-ups, steering, several requests queued

- `steer_task` (`tool_schema.py:1130-1150`) sends a priority-override instruction into the *running* task agent's next planning step — no need to stop and restart.
- If the task is already finishing when a steer arrives, the tool returns `error="task_closing", retry_with="start_task"` and the voice LLM is told (`orchestrator.py:3556-3563`) to **silently** re-issue it as a new `start_task` in the same turn — no narrated failure, because from the user's point of view nothing failed.
- `stop_task` (`tool_schema.py:1119-1128`) cancels by task_id or defaults to the primary ship.
- Barge-in is handled by the ordinary Pipecat interruption path (`BotStartedSpeakingFrame`/`InterruptionFrame`) plus the orchestrator's own speech-cycle state machine (`orchestrator.py:696-789`, `_begin_assistant_response_cycle`/`_mark_assistant_cycle_idle`) which also **dismisses an armed confirmation dialog** if the user starts talking again before confirming (lines 724-729) — an interruption can cancel a *pending UI decision*, not just speech.
- Several background tasks (e.g. two ships on tasks at once) each narrate independently through the same `SubagentNarrator`; the settle window naturally coalesces near-simultaneous completions into one spoken turn instead of the bot narrating them one after another.

### 1.6 Context split between talker and thinker

- **Talker (voice LLM):** whatever `EventRelay`'s declarative routing table appends (`event_relay.py`) — combat/movement/trade events scoped to what this player can plausibly know, plus tool results, plus narrated task events. It never sees the task agent's internal planning/tool-call trace.
- **Thinker (task agent):** its own task log (its own tool calls/results/plans), the original task description + context string, and steering messages. It does **not** see the live user transcript directly — everything reaches it pre-digested through the tool arguments the voice LLM composed.
- **Consistency mechanism:** both read from the *same* game server (Supabase) as ground truth, so neither can drift into an inconsistent world-model; the voice LLM's context is kept in sync with reality through the same `EventConfig`-routed events the task agent triggers, not through the two models exchanging free-form state.

### 1.7 Models, and why

Both the voice LLM and the task LLM default to the **same model family** (`gemini-2.5-flash` primary, or `claude-sonnet-4-5-20250929` for Anthropic — `utils/llm_factory.py:442-548`). The difference is **not** "small fast model talks, big smart model thinks" — it's **thinking mode on or off**:

> module docstring, `llm_factory.py:6-17`: "Voice LLM (bot.py pipeline - typically no thinking mode)" vs. "Task Agent LLM (TaskAgent - with thinking mode)."

Voice LLM: `VOICE_LLM_THINKING_BUDGET` defaults to **0** (disabled). Task agent: thinking always enabled, default budget **4096** tokens, `include_thoughts=True`, and `run_in_parallel=False` — tool calls are dispatched **sequentially**, trading latency for reliability on the side where correctness matters more than speed. A **third**, separate low-latency `SUMMARIZATION_LLM_*` config exists purely for context-window compaction, off the hot path (`bot.py:227-245`, 120 s timeout, target 6000 tokens post-summary). A **fourth**, genuinely smaller/cheaper model — `claude-haiku-4-5-20251001` — drives a parallel "UI agent" branch (`llm_factory.py:574`), which is the one place gradient-bang does use a cheap model for a lightweight, latency-sensitive job, structurally the same role Haiku plays as Yuki's mouth.

### 1.8 Latency and "feels alive" tricks worth stealing

- **`IdleReportProcessor`** (`runtime/idle_report.py`, defaults overridden in `bot.py`/`config.py` to **9 s** idle / **45 s** cooldown): fires a one-sentence "what's happening with tasks" only after the bot has *finished* speaking (not started), doesn't arm until the first user turn, and re-arms its cooldown from the moment its own utterance ends — so it can't stack behind itself. A 15 s safety valve force-clears the in-flight flag if the expected TTS never arrives, so a stuck flag can't permanently silence the feature. The user can barge into an idle report exactly like any other bot utterance.
- **`TaskActivityFrame`** (`runtime/frames.py:8-22`) is a distinct, much longer-lived idle signal (600 s) registered on the `PipelineWorker`'s `idle_timeout_frames` (`bot.py:415-419`): background task output counts as "the session is alive" so a 5-minute silent background task doesn't get the whole voice session torn down for inactivity, even though the *user* said nothing.
- **Event batching watchdog** (`task_agent.py:1610-1639`): rapidly-arriving game events are batched into one inference call instead of firing one LLM turn per event — fewer, better-formed narrations instead of a chattier stream.
- **Prompt caching** on the UI agent's system prompt (`llm_factory.py:330-359`) cuts prefill latency on repeated calls — the same lever Yuki's mouth already uses (`AnthropicLLMService.Settings(enable_prompt_caching=True)`, `yuki/voice/pipeline.py:497`).
- **Eager end-of-turn** is a Pipecat-framework-level feature (not gradient-bang-specific — see §2) that both projects have access to but Yuki has measured and left off (VOICE.md §9).

---

## 2. Pipecat 1.11.0 internals: the mechanisms under both projects

### 2.1 `ParallelPipeline` (`pipecat/pipeline/parallel_pipeline.py`)

Each branch (a list of processors) is wrapped in its own private `Pipeline` with a `PipelineSource`/`PipelineSink`. Every incoming frame is fanned out to **all** branches (`process_frame`, ~line 133 fans via `p.queue_frame(...)`, line 164). Ordinary frames flow independently per branch and are de-duplicated by frame `.id` at the merge point so a frame reaching the sink from one branch isn't double-emitted. Lifecycle frames (`StartFrame`/`EndFrame`/`CancelFrame`, line 156) are specially barrier-synchronized: pause processing, buffer everything else, and only release once *every* branch's sink has reported that lifecycle frame — this stops a fast branch's `EndFrame` truncating a slower branch mid-output.

This is what powers word-wrangler's phone-game (`pipecat-examples/word-wrangler-gemini-live/phone-game/bot.py:654-677`): two co-equal LLM branches (an AI "Host" and an AI "Player"), each getting every frame, cross-wired with `ProducerProcessor`/`ConsumerProcessor` so one branch's TTS audio becomes the other branch's input audio, gated by hand-built `EventNotifier`s so the Player doesn't jump in before the Host's intro ends.

**This is not Yuki's shape.** Word-wrangler's two branches are both live conversational participants running continuously and symmetrically. Yuki needs one fast, always-on talker and one slow, on-demand thinker that is *not* listening to raw audio at all. `ParallelPipeline` fanning identical frames to co-equal branches is the wrong primitive for that; it would mean every user utterance gets sent to the brain's LLM too, all the time, which is neither how Yuki's brain works (tool-using desktop agent, not a conversational participant) nor desirable (cost, and exactly the "mouth rewrote/added to the request" risk multiplied by having two models simultaneously interpreting the same raw speech).

### 2.2 Async tool-call machinery — this *is* Yuki's shape, and gradient-bang's `start_task` sits on a variant of it

- `FunctionCallInProgressFrame.cancel_on_interruption` (`frames.py:2273-2299`): `False` is exactly Yuki's `tell_yuki` registration (`yuki/voice/pipeline.py:502-503`) — the async call survives a barge-in.
- `cancellable_by_llm` auto-generates a `cancel_<tool>` tool (`llm_service.py` ~line 1854, `_sync_cancel_tools`) — Yuki's `cancel_tell_yuki`.
- **Deferred-until-quiet settlement** lives in `pipecat/processors/aggregators/llm_response_universal.py`. `_is_deferred()` (line 2267): a result that arrives before any *user or developer* message has followed the call's placeholder is delivered as an ordinary tool message; otherwise it's injected as a **developer-role message** (`build_final_result_message`, line 2025) so the model sees it out-of-band instead of pretending it answered synchronously mid-conversation. Per docstring (lines 2276-2278): the model's **own filler speech does not count as "moved on"** — only a genuine new user/developer turn does. Separately, `_maybe_push_context_after_function_result` (line 1958) defers pushing the context frame upstream while the bot is still speaking (line 1977) or other results are still queued (line 1969), **batching multiple async results into one re-invocation** once the bot goes quiet — the framework-level version of what gradient-bang's `SubagentNarrator` reimplements by hand with richer domain rules (staleness, settle window, cooldown).

This confirms: Yuki's `tell_yuki` design already uses the *correct* Pipecat primitive for a single talker + single background thinker. Gradient-bang's narrator is solving a harder problem (many concurrent task agents, several ships, combat priority) that Yuki doesn't have yet — Yuki should **not** import that complexity, but should borrow its *specific, named gates* (staleness, settle, cooldown) as an evolution of the simpler mechanism it already has, if/when result timing needs finer control than "wait for both to go quiet."

### 2.3 The `bus`/`worker`/`job` subsystem — Pipecat's own "talker + thinker" framework

`pipecat.bus.bus.WorkerBus`, `pipecat.pipeline.worker.PipelineWorker`, `pipecat.workers.base_worker.BaseWorker`, `pipecat.pipeline.job_context.{JobParams,JobGroup,JobStatus}`, `pipecat.workers.llm.llm_worker.LLMWorker` — a newer, more general framework than a single `Pipeline`/`PipelineTask`. It lets several independent `PipelineWorker`/`LLMWorker` instances (each its own pipeline, possibly its own model) act as bus peers, dispatching typed jobs to each other and streaming partial results back through `JobGroup`/`JobContext`, locally or across processes (Redis/PGMQ). This is what gradient-bang's `Orchestrator` (the "player" `PipelineWorker`) and `TaskAgent` (a child worker) are built on, and it's the basis of Pipecat's own `pipecat-subagents` package, whose announcement names exactly this shape: "LLM handoff, parallel debate, sidecar code assistants."

**Recommendation on this point:** do not migrate Yuki onto this framework. Yuki's brain (`yuki/agent/loop.py`, `Agent`) already lives *outside* Pipecat entirely, on the Qt/`AgentRuntime` side, talking to the voice pipeline through `VoiceBridge`'s explicit, audited, one-way thread crossings (`yuki/voice/bridge.py`, module docstring). That crossing is simpler and already correct — it doesn't need Pipecat's bus, because the brain doesn't need to be a Pipecat pipeline at all (it has its own tool loop, memory, job queue, desktop backend). Rebuilding it as a `PipelineWorker`/`LLMWorker` bus peer would be a large rewrite for no behavioural gain, and would reintroduce, inside Pipecat, complexity Yuki already solved more simply with plain asyncio + Qt signals. The lesson to take is **behavioural** (§1.3, §1.4's named gates), not architectural.

### 2.4 Documented best practices (web research)

- **Filler/ack while a slow tool runs:** register async with `cancel_on_interruption=False`; speak an immediate acknowledgement; `FunctionCallResultProperties(is_final=False)` for mid-flight progress pushes — this is exactly Yuki's current design.
- **Eager end-of-turn** (`EagerUserTurnStopStrategy`, Pipecat 1.9.0): when audio has gone quiet but the STT's finalized transcript is still lagging, speculatively start inference on the stable-but-unfinalized text, bounded by a `speculation_timeout` (default 5 s), withdrawn if the STT never commits. Yuki measured this (`voice_eager_eot_threshold`) and found it not a clear win yet (VOICE.md §9) — worth re-testing with live (not offline-scripted) audio, since STT finalization lag is exactly what eager EOT targets and offline test clips may not reproduce realistic finalization delay.
- **TTFA (time-to-first-audio) as a named, tracked metric**, with the standard mitigation being sentence-level streaming aggregation between LLM and TTS rather than waiting for a full response — Yuki already streams to ElevenLabs Flash; the open ~0.7 s unexplained Pipecat-side gap (VOICE.md §14) is exactly the kind of thing Pipecat's own TTFA/TTFB metrics frames are built to localize (Yuki's `TurnObserver` already consumes `TTFBMetricsData` — extending it to bucket by pipeline stage, not just LLM vs TTS, would help find where the 0.7 s goes).
- **Multi-agent handoff pattern** (`pipecat-subagents`): confirms gradient-bang's shape is Pipecat's own recommended pattern for this problem, at the scale of *many* concurrent background agents — validates that Yuki's simpler one-thinker version is the right-sized instance of the same idea, not a lesser version of it.

---

## 3. Yuki's current build: what's already right, and where the three failures come from

Yuki's design (`docs/VOICE.md`, `yuki/voice/mouth.py`, `yuki/voice/bridge.py`, `yuki/voice/pipeline.py`, `yuki/agent/loop.py`, `yuki/agent/tools.py`) already independently arrived at several of gradient-bang's core mechanisms:

| Mechanism | Gradient Bang | Yuki (already built) |
|---|---|---|
| Fast talker, slow thinker | Voice LLM ↔ TaskAgent via bus | Haiku mouth ↔ Agent brain via `tell_yuki` |
| Hand-off decided by | Tool call + prompt commitment discipline | Tool call (`tell_yuki`) + prompt commitment discipline (`mouth.py:50-57`) |
| Async, non-cancelling tool | `start_task`/`steer_task` (fire-and-forget + injected events) | `tell_yuki` (`cancel_on_interruption=False`, held-open, streamed results) |
| Progress mid-task | Push (`_deliver_task_output`) + pull (`query_task_progress`) + synthetic filler | Push only: brain's `say` tool (rarely used — VOICE.md §21.5) |
| Anti-fabrication | Structural (isolated one-shot status LLM) + explicit prompt examples | Structural (results only via tool channel — VOICE.md §2 rule 2) + prompt line, **not yet enough in practice** |
| Speech-gated result delivery | `SubagentNarrator` (stale/settle/cooldown gates) | Simpler: `_watch` holds the conversation open (`voice_hold_max_s`); result speaks on next quiet turn via the same `tell_yuki` call still open |
| Idle status nudge | `IdleReportProcessor`, synthetic `<idle_check>` | Not built — `yuki_status` is pull-only (user must ask "how's it going") |
| Condensation for speech | Deterministic formatters + on-demand LLM summary | Prompt instruction only ("one or two short sentences... offering the rest") — **not enforced structurally** |

The three measured failures map cleanly onto gaps in that table, not onto missing plumbing:

1. **"The mouth answered memory questions itself and invented answers."** The mouth has no memory tool registered (`mouth.py`'s `_build`/`_services` only register `tell_yuki` and `yuki_status`, `pipeline.py:502-504`) — it is *structurally* unable to fetch memory. The failure is a **prompt compliance failure**: given a memory-shaped question, Haiku sometimes answers from its own guess or from the compact `MouthMemory` block (portrait, standing rules, to-dos — `mouth.py:174-202`) it does have loaded, rather than treating "does the mouth know this itself, reliably" as different from "does the mouth have some related text in its system prompt." Gradient-bang's structural fix (§1.3, `task_progress.md`) is instructive: it doesn't just tell the model not to invent — it gives it a **narrow, isolated, single-purpose context** to answer from and *nothing else in scope*, and it explicitly names the failure mode with a worked example. Yuki's mouth prompt says "never guess at facts about the user or their day" (`mouth.py:45-46`) but doesn't distinguish "background I was given for color" from "a fact I'm being asked to state as true" — that's the gap.
2. **"The mouth rewrote the user's request and added topics."** `mouth.py:53-54` already instructs "Write the message for Yuki's other half: self-contained, in English, with every reference resolved... none of it dropped or split off for later" — but "self-contained, with references resolved" is inherently a rewrite, and the instruction doesn't bound how much elaboration is allowed during that resolution. Gradient-bang doesn't have this exact failure mode because its hand-off payload is much narrower (`task_description` + `context`, both short, structured fields with a schema — `tool_schema.py:1096-1103`) rather than a single free-text `message` field the model is trusted to compose faithfully.
3. **"Long results were read out."** The brain's own prompt (`yuki/agent/prompt.py:167-180`) already says the `done` message for a voice request should be "one or two short spoken sentences... then a few words offering the rest," explicitly forbidding "paragraphs, a list or a round-up." But this is Sonnet-at-medium-effort's *own* discipline over its own final answer with no structural cap — gradient-bang's hard truncation caps (`_EVENT_QUERY_MAX_TOTAL_CHARS = 6000`, list caps at 4 + "N more", etc., `summary_formatters.py`) are enforced **in code**, before the text ever reaches a model that has to "choose" to be short.
4. **~3 s latency, end of speech to first audio.** Already well diagnosed in VOICE.md §14 (Flux ~0.9 s + mouth TTFT ~1.6 s + TTS TTFB ~0.3 s). Gradient-bang and Pipecat's own docs don't reveal a silver bullet Yuki is missing here — the standard levers (eager EOT, streaming, a lower-latency LLM endpoint) are already identified in VOICE.md §14/§21. The one concrete addition from this research: Pipecat's TTFA metrics infrastructure suggests instrumenting *inside* the ~0.7 s unexplained Pipecat-side gap (between bare-call TTFT and in-pipeline TTFT) stage-by-stage, rather than only measuring the two endpoints.

---

## 4. Recommendations: behaviour first, plumbing second

The overall verdict: **keep Yuki's architecture.** It is already the right shape — a single always-on talker plus a single async, tool-invoked thinker — and it is Pipecat's own documented pattern for this problem (`pipecat-subagents`), just simpler because Yuki has one thinker, not many. Gradient-bang's extra machinery (bus, multiple task agents, `SubagentNarrator`'s five-stage gate, declarative event routing) exists because it has to coordinate several concurrent background agents across several ships and a real-time multiplayer world. Yuki has one brain. Importing that machinery would be solving a problem Yuki doesn't have. What Yuki should adopt are the **behavioural disciplines** gradient-bang layered on top of the same primitive, several of which are structural (enforced by code/schema) rather than purely relying on the model's own restraint — which is precisely where Yuki's three failures come from.

### 4.1 Talker behaviour rules to adopt

**A. Narrow the hand-off schema so "rewriting" has less room to happen.**
Today `tell_yuki(message, intent)` trusts the mouth to compose one free-text field faithfully. Gradient-bang's `start_task(task_description, context, ship_id)` splits "what to do" from "supporting context" as two separate schema fields with distinct descriptions — that separation alone discourages the model from blending "what the user asked" with "things I inferred." Recommend splitting `tell_yuki`'s `message` into two fields:
- `request`: the user's ask, minimally reworded (pronouns resolved only), nothing added;
- `context`: anything else worth passing along (what was said earlier, mood, situation) — explicitly optional, explicitly labelled as *not* part of the ask itself.

This doesn't require touching the async-tool machinery — it's a schema and prompt change in `yuki/voice/mouth.py` (`TELL_YUKI` dict, lines 104-124) and `yuki/voice/bridge.py`'s `brain_request()` (lines 222-243), which already concatenates message + heard turns; it would instead concatenate `request` + `context` + heard turns, with the brain told which is which.

**B. Make the anti-fabrication rule example-driven, not just declarative.**
Gradient-bang's most effective anti-fabrication text isn't the abstract "don't invent" line — it's the worked bad-example ("We're in sector 4867... shields are full..." when only arrival was confirmed). Recommend adding one or two concrete bad/good examples to `MOUTH_PROMPT` (`mouth.py:29-85`) specifically for the two observed failures:
- memory questions ("what did I tell you about X" / "what's my Y" asked directly of the mouth) → always hand off, even though the mouth has *some* related text in its own memory block; the memory block is described as background/color, but the model needs an explicit example of "a direct factual question about something in that block is not something you may answer from it — hand it over so the brain can check the real record."
- adding scope: show a short example of a request ("check my calendar") vs. an over-elaborated hand-off ("check my calendar and calculate my free time and suggest what to do with it") to make "don't add topics" concrete rather than abstract.

**C. Structural condensation cap on the `result` payload, before it reaches the mouth.**
Gradient-bang never trusts a model alone to keep a spoken answer short — it truncates deterministically in code first (`summary_formatters.py`). Recommend a small `yuki/voice/condense.py` (or a function in `bridge.py`) that runs on the brain's final `result`/`failed` payload *before* it's put on `job.events` (`bridge.py:264-273`): hard character cap (e.g. ~600 chars, well above what a spoken 1-2 sentence answer needs, but low enough to catch a brain that returned a paragraph), cutting at the last whole sentence (reuse the existing `_cut()` helper already in `mouth.py:164-171`, which does exactly this for the portrait). This doesn't replace the prompt discipline in `agent/prompt.py:167-180` — it backstops it, the same way gradient-bang's code-level truncation backstops (not replaces) its prompt instructions.

**D. Let the brain's `say` be used more, without violating the project's own "no progress timer" decision.**
VOICE.md §20 explicitly rejected "progress timers" and "canned fillers" as a design choice, and §21.5 notes the brain still rarely calls `say` in practice. Gradient-bang's synthetic filler (`_emit_synthetic_progress_message`) is a *code-injected* filler — which is exactly the pattern Yuki rejected, for good reason (it's not the brain's real status, it's a guess dressed as one). Recommend **not** reversing that decision. Instead, close the gap a different way: strengthen `agent/prompt.py`'s existing `say` guidance (lines 167-180) with the same "commitment discipline" framing gradient-bang uses for tool calls — e.g., "if you are about to spend more than a few tool calls on one sub-goal without anything to report, say one line about what you're doing before continuing" — a *behavioural* trigger tied to the brain's own real activity (its tool-call count/type), not a wall-clock timer. This is a prompt change only (`yuki/agent/prompt.py`), no new mechanism, and it stays a real status update, never an invented one.

**E. Idle nudge, pull-only today — consider a narrow, opt-in push version.**
Gradient-bang's `IdleReportProcessor` proactively says one sentence about running tasks when both sides go quiet. Yuki's `yuki_status` is pull-only; the user must ask. Given Yuki's explicit rejection of progress timers (§20) and the measured cost of every mouth call ($0.0056/turn), recommend treating this as optional rather than default: only fire a proactive one-line status if a **brain job is still open** when the conversation would otherwise end from idleness (this already partly exists — `voice_hold_max_s` keeps the conversation open, `pipeline.py:614-623` — the missing piece is that today the conversation just waits silently rather than saying anything until the result or a user question arrives). A small, bounded version: if `voice_idle_s` is about to elapt with an open job, have the mouth call `yuki_status` once and speak a one-line update before going quiet again, rather than staying completely silent for up to `voice_hold_max_s` (up to 10 minutes). This is closer to Yuki's own existing "the brain chooses what's worth saying" philosophy than gradient-bang's timer-driven version, because it's gated by *staying in a job the user is already waiting on*, not a wall clock during ordinary chat.

### 4.2 File-by-file changes

| File | Change | Why |
|---|---|---|
| `yuki/voice/mouth.py` | Split `TELL_YUKI` schema into `request`/`context` fields (§4.1.A); add 2 worked bad/good examples to `MOUTH_PROMPT` for memory-question hand-off and scope-creep (§4.1.B) | Structural narrowing + concrete examples, mirroring `voice_agent.md`'s style |
| `yuki/voice/bridge.py` | Update `brain_request()` to compose from `request` + `context` separately (§4.1.A); add a condensation pass on `done`/`failed` payloads before `job.events.put` (§4.1.C) | Backstop long results in code, not just prompt |
| `yuki/agent/prompt.py` | Strengthen the `say` paragraph (already at lines 167-180) with an activity-triggered (not timer-triggered) nudge (§4.1.D) | Keep "no progress timer" decision; close the "brain rarely calls say" gap behaviourally |
| `yuki/voice/pipeline.py` | Instrument the ~0.7 s unexplained Pipecat-side TTFT gap stage-by-stage (extend `TurnObserver.on_frame`'s `MetricsFrame` handling, lines 380-394, to bucket more `TTFBMetricsData` processor names, not just "llm"/"tts"); re-test `voice_eager_eot_threshold` against live (not offline-scripted) audio, since eager EOT specifically targets STT finalization lag that scripted clips may not reproduce | Diagnose the biggest unexplained latency chunk; re-check a lever already built but only offline-tested |
| `yuki/voice/pipeline.py` / `session.py` | Optional, bounded idle status nudge while a job is open and `voice_idle_s` is about to elapse (§4.1.E) — behind a setting, off by default until validated live | Fills the "silent for up to 10 minutes" gap without reintroducing a rejected progress-timer pattern |
| `yuki/config.py` | Add a setting for the condensation cap (`voice_result_char_cap`, default ~600) and the idle-nudge toggle (`voice_idle_nudge`, default off) | Keep both changes measurable/reversible, in the project's existing settings style |

Nothing here touches Pipecat's `ParallelPipeline`, the bus/worker/job subsystem, or Yuki's thread/process model (`docs/VOICE.md` §4) — none of those are the right tool for a single talker + single thinker, and gradient-bang's own extra machinery exists to solve a multi-agent, multi-ship coordination problem Yuki doesn't have.

### 4.3 Risks

1. **Schema split (§4.1.A) is a small breaking change** to `tell_yuki`'s contract — needs re-running `scripts/voice_offline.py` and `tests/test_voice.py` (which already cover bridge routing) before shipping, same as any mouth-prompt change per VOICE.md's own practice ("Changes made from offline runs... each fixed a measured failure").
2. **Worked bad/good examples (§4.1.B) grow the mouth's system prompt**, and VOICE.md §5.4 measured that prompt caching barely helps TTFT here (0.9-1.3 s cached vs 1.2-1.4 s uncached) — a bigger prompt could nudge TTFT up further even with caching; measure after the change, not just assume it's free.
3. **Condensation cap (§4.1.C) is a blunt instrument** — a hard character cut can land mid-sentence on an unlucky payload even with sentence-aware trimming; needs the same "cut at last whole sentence" care `_cut()` already applies to the portrait (`mouth.py:164-171`), reused rather than reinvented.
4. **Activity-triggered `say` nudge (§4.1.D) still depends on Sonnet's compliance** — gradient-bang's equivalent (`_emit_synthetic_progress_message`) is code-guaranteed because it's not asking the model to remember to do something, it's the framework doing it. A prompt-only fix may only partially close VOICE.md's open question #5 ("the brain rarely calls say"). If it's still rare after this change, the next step would be a structural nudge (e.g., the runtime injecting a `[you have made N tool calls without a say — consider one]` steer-like note, which *is* real information about the brain's own activity, not an invented status) — flagged here as a fallback, not a first move.
5. **Idle nudge (§4.1.E) reopens a decision the project already made deliberately** (§20's rejection of progress timers) — ship it off-by-default and behind a setting, validate on real jobs before considering it a default, and make sure it only fires from `yuki_status`'s real job table, never a synthetic guess.
6. **Model lifecycle** (VOICE.md §21.3): Haiku 4.5's Bedrock end-of-life is "no sooner than 2026-10-16" with no listed successor. None of the above depends on a specific model, but any latency work should be validated against whatever mouth model is current when it ships.

---

## 5. Sources

- `gradient-bang` (cloned `--depth 1`, HEAD 2026-09-25): `src/gradientbang/bot.py`, `runtime/orchestrator.py`, `runtime/subagent_narrator.py`, `runtime/inference_gate.py`, `runtime/subagents/task_agent.py`, `runtime/tool_schema.py`, `runtime/event_relay.py`, `runtime/frames.py`, `runtime/idle_report.py`, `runtime/voice_runtime.py`, `utils/llm_factory.py`, `utils/summary_formatters.py`, `prompts/agents/voice_agent.md`, `prompts/agents/task_agent.md`, `prompts/agents/task_progress.md`, `config.py`.
- `pipecat-examples` (cloned `--depth 1`, HEAD 2026-09-25): `word-wrangler-gemini-live/phone-game/bot.py`, `word-wrangler-gemini-live/README.md`.
- `pipecat` 1.11.0 (`<repo>\.venv\Lib\site-packages\pipecat`): `pipeline/parallel_pipeline.py`, `services/llm_service.py`, `processors/aggregators/llm_response_universal.py`, `bus/bus.py`, `pipeline/worker.py`, `pipeline/job_context.py`, `workers/base_worker.py`, `workers/llm/llm_worker.py`, `frames/frames.py`.
- Pipecat docs/GitHub: `docs.pipecat.ai/pipecat/learn/function-calling`, `docs.pipecat.ai/pipecat/fundamentals/metrics`, `docs.pipecat.ai/subagents/introduction`, `docs.pipecat.ai/api-reference/server/pipeline/parallel-pipeline`, `github.com/pipecat-ai/pipecat` PRs #5690 (eager end of turn), #5820 (settle async tool call in place), #5877 (relay backend progress), issue #5823; `github.com/pipecat-ai/pipecat-subagents`.
- Yuki: `docs/VOICE.md`, `yuki/voice/session.py`, `yuki/voice/pipeline.py`, `yuki/voice/mouth.py`, `yuki/voice/bridge.py`, `yuki/agent/loop.py`, `yuki/agent/prompt.py`, `yuki/agent/tools.py`, `yuki/ui/runtime.py`.
