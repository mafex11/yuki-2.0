# Yuki voice — design contract (v1)

Goal: talk to Yuki the way you talk to a person sitting next to you. It answers within about a second, you can cut it off mid-sentence, its voice carries emotion, and it keeps talking to you while it works on your PC. Nothing gets slower on the PC side: the brain keeps its full thinking (3–8 s per step).

This contract was researched on 2026-09-24 against the installed source of Pipecat 1.11.0 (installed with every extra listed below on Python 3.13.1 / Windows 11 without problems), the Yuki code as of `d622dc9`, and vendor documentation. Where a number comes from a vendor it says so, and so does anything not yet verified.

All rules in `docs/ARCHITECTURE.md` apply: no behaviour heuristics, no fixed sleeps, log everything, builders never touch the live desktop. Voice-specific consequences are in §2.

---

## 1. The idea in one picture

Two models, running at the same time, with one job each:

- The **mouth** is a fast model that owns the conversation. It hears you, answers within about a second, and chats. It hands every PC job to the brain, tells you what's happening, and passes on what the brain found. It never touches the PC.
- The **brain** is the existing Yuki `Agent` loop, unchanged in spirit. It owns the task: it sees the desktop, thinks, acts, asks, finishes. It never speaks to you directly; everything it wants you to hear goes through the mouth.

```
                      ┌──────────────────────────── yuki-ui process ───────────────────────────────┐
                      │                                                                             │
 ┌─────────┐  PCM     │  VOICE THREAD (asyncio, Pipecat)                    GUI THREAD (Qt)         │
 │   mic   │──────────┼─► LocalAudioTransport.input                                                 │
 └─────────┘          │      │ (audio_in_filter: echo canceller, §11)       tray · overlay ·        │
                      │      ▼                                              status strip · hotkeys  │
                      │   Deepgram Flux STT ──── transcript + "user done" ─┐        ▲               │
                      │      │  (StartOfTurn = barge-in)                   │        │ Qt signals    │
                      │      ▼                                             │        │               │
                      │   user context aggregator ◄────────────────────────┘  ┌─────┴───────────┐   │
                      │      ▼                                                │  AgentRuntime   │   │
                      │   MOUTH  Haiku 4.5 on Bedrock  ── tell_yuki ─────────►│  (existing)     │   │
                      │      │   (AnthropicLLMService,   yuki_status          │                 │   │
                      │      │    AsyncAnthropicBedrock) cancel_tell_yuki     │ worker lane ────┼─► BRAIN: Agent.run
                      │      │                      ◄── progress/question/   │ front_desk lane │   │  (Sonnet/Opus 5,
                      │      │                          final (async tool)    └─────────────────┘   │   tools, memory)
                      │      ▼                                   ▲                                  │
                      │   Cartesia Sonic TTS                     │  VoiceBridge (§6): job ids,      │
                      │      ▼                                   │  thread-safe hand-over both ways │
                      │   speaker tap (echo reference, §11)      │                                  │
                      │      ▼                                                                      │
 ┌──────────┐  PCM    │   LocalAudioTransport.output                                                │
 │ speakers │◄────────┼──────┘                                                                      │
 └──────────┘         │   assistant context aggregator (records only what was actually spoken)      │
                      └─────────────────────────────────────────────────────────────────────────────┘
```

How the two depend on each other:

| Direction | What travels | Mechanism |
|---|---|---|
| mouth → brain | a new task, a correction to the running task, an answer to the brain's question | `tell_yuki(message, intent)` → `VoiceBridge` → `AgentRuntime.submit` / `Agent.steer` / `Agent.answer` |
| mouth → brain | stop | the built-in `cancel_tell_yuki` tool → `Agent.cancel()` |
| brain → mouth | milestones worth saying ("found it, starting playback") | new brain tool `say(text)` → intermediate result of the running `tell_yuki` call |
| brain → mouth | a question for the user | `AskUser` event → intermediate result |
| brain → mouth | the outcome | `Final` / `ErrorEvent` → final result of `tell_yuki` |
| mouth ← brain state | "what are you doing right now?" | `yuki_status()` reads the bridge's live job table, instantly |

Pipecat 1.11 already provides the hard part of this handshake natively as **async function calls** (`register_function(..., cancel_on_interruption=False)`):

- The mouth keeps talking while the tool runs.
- Partial results are streamed back.
- A result that arrives while you are talking waits until you finish; one that arrives while the mouth is talking waits until it finishes.
- The mouth can cancel the call through an auto-generated `cancel_<name>` tool.
- Pipecat also puts standing instructions into the system prompt telling the model not to invent a result while it waits.

We build on that instead of writing our own event bus.

---

## 2. Rules for the voice layer

1. **Only the mouth speaks.** The brain's words (the `done` message, `say`, questions) reach the user only after the mouth turns them into speech. Speaking style, emotion and brevity live in one prompt.
2. **The mouth never claims a PC result it did not receive.** It may say "on it" and "I've asked Yuki to…"; it may say "done" or report an outcome only from a `tell_yuki` result. Enforced by the prompt (§5.2) and by structure: results arrive only through the tool channel. Not enforced by scanning its words (that would be a keyword heuristic, rule 1).
3. **No keyword routing.** Nothing in code decides whether an utterance is chit-chat, a task, a correction or a stop. The mouth decides by which tool it calls and with what `intent`. The bridge routes on state (is a job running? is the brain waiting for an answer?), which is plumbing.
4. **The brain chooses what is worth saying.** Progress is spoken only when the brain calls `say`. Raw tool events are never narrated, and there is no fixed "update every N seconds" timer (rule 1 forbids timers steering behaviour).
5. **Tools never run on a guess.** Speculative replies (Flux eager end-of-turn) may start the mouth talking early, but never start PC work. Pipecat enforces this: a speculative inference that reaches a tool call is withdrawn and re-run on the confirmed transcript (`LLMService._run_function_calls`, "speculative inference wants a tool call, cancelling it").
6. **Barge-in stops the voice, not the work.** Interrupting the mouth cuts its speech and its current reply. The brain's task keeps running unless the mouth cancels it on purpose.
7. **Log everything** (§13). Every transcript, every mouth request/response, every tool call, every spoken line (and how much of it was actually heard), every interruption, every latency mark.
8. **Keys from environment variables only**: `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY` (or `ELEVENLABS_API_KEY`). Bedrock keeps using `AWS_BEARER_TOKEN_BEDROCK`. Nothing in files.

---

## 3. The stack

| Layer | Choice | Why | Fallback / alternative |
|---|---|---|---|
| Framework | **Pipecat 1.11** (`pipecat-ai`, BSD-2, free; Pipecat Cloud is optional hosting we don't use) | In-process Python, native async tools, barge-in, word-accurate context on interruption, Flux and Cartesia services, observers for logging. Verified to install on Py 3.13 / Win 11 | LiveKit Agents (needs a media server — overkill locally), TEN (heavy), Vocode (stalled), hand-rolled (weeks to redo interruption/aggregation) |
| Audio I/O | `LocalAudioTransport` (PyAudio 0.2.14, installed fine) | Mic and speakers directly, no browser | `SmallWebRTCTransport` + browser page if we ever want the browser's echo canceller |
| Speech-to-text + turn detection | **Deepgram Flux**, `flux-general-multi`, `language_hints=[en, hi]` | One service does transcription *and* "user finished" (~260 ms median end-of-turn, vendor). Multilingual GA 2026-04-29 with in-sentence code-switching, which matters for Hinglish. $0.0065/min streamed | `flux-general-en` if Hinglish accuracy disappoints on English turns; Nova-3 multi + Silero VAD + Smart Turn v3 (adds 200–600 ms) |
| Mouth | **Claude Haiku 4.5** on Bedrock (`us.anthropic.claude-haiku-4-5-20251001-v1:0`, already in `DEFAULT_PRICING`), thinking off | ~0.8 s time to first token on Bedrock (Artificial Analysis median), cheap ($1/$5 per M), same auth and SDK as the brain | Model id is a setting: Bedrock lists Haiku 4.5 end-of-life "no sooner than" Oct 2026, so plan to swap to its successor |
| Brain | **Existing `Agent`** via `AgentRuntime` (Sonnet 5 default, Opus 5 on demand, adaptive thinking) | Already sees, acts, asks, remembers, logs | — |
| Text-to-speech | **Cartesia Sonic 3.6** (`sonic-3.6`, Pipecat default) | Fastest measured (Sonic 3.5: 128 ms median first audio incl. network, Vapi Jun 2026). Hinglish in Latin script, inline `<emotion value="…"/>`, `[laughter]`, speed/volume. ~$0.04–0.05 per 1k chars | ElevenLabs Flash v2.5 (197 ms measured, no emotion tags); ElevenLabs `eleven_v3_conversational` (audio tags like `[laughs]`, but 758 ms measured median) when expressiveness beats speed |
| Echo cancellation | v1: **headphones**. v2: **WebRTC APM** via `livekit` (`livekit.rtc.AudioProcessingModule`) as a Pipecat `audio_in_filter` | Pipecat ships no echo canceller (only noise filters: RNNoise, Koala, AIC, Krisp). Without one, Yuki hears itself and interrupts itself | `pywebrtc-audio` (AEC3, Win wheels 3.10–3.14) |

**Why the mouth talks to Bedrock through `AnthropicLLMService` and not `AWSBedrockLLMService`:**
- `AnthropicLLMService` takes a `client=` argument, so it gets `AsyncAnthropicBedrock(aws_region=...)`: the same SDK, auth (`AWS_BEARER_TOKEN_BEDROCK`) and Messages API format as the brain. Confirmed that `AsyncAnthropicBedrock` exposes `beta.messages`, which Pipecat calls.
- `AWSBedrockLLMService` uses the Converse API through aiobotocore: a different wire format, and its own caching and tool shapes.
- Risk: Pipecat always sends `betas=["interleaved-thinking-2025-05-14"]`. V0 (§17) confirms Bedrock accepts it for Haiku with one real call; if not, the fallback is `AWSBedrockLLMService` with `enable_prompt_caching=True` (botocore 1.43 also reads the bearer token).

---

## 4. Process and thread model

Voice runs **inside the `yuki-ui` process**, so it shares the `AgentRuntime`, tray, status strip and logs, and so Windows' microphone-in-use record names Yuki's own program (see §7.6).

| Thread | Owns | Talks to others through |
|---|---|---|
| GUI thread (Qt) | tray, overlay, status strip, hotkeys, `AgentRuntime` signals | Qt signals (queued connections) |
| `worker` / `front_desk` lanes (QThread each, existing) | one `Agent` each, blocking `Agent.run` generators | Qt signals out; `queue.Queue` in (jobs, answers) |
| **voice thread** (new, `threading.Thread`, runs `asyncio.run(...)`) | the Pipecat pipeline: transport, STT, mouth, TTS | `VoiceBridge`: `loop.call_soon_threadsafe` into asyncio; a Qt signal object out |
| PyAudio callback threads (inside Pipecat's transport) | raw audio | Pipecat internals |

Rules:
- Yuki has no asyncio today. Pipecat's loop lives **only** on the voice thread. Nothing on that loop ever calls a blocking Yuki function (`Agent.run`, `next()`, memory reads): those take seconds.
- Every crossing is explicit and one-way:
  - **Into asyncio:** `loop.call_soon_threadsafe(queue.put_nowait, item)` or `asyncio.run_coroutine_threadsafe(...)`.
  - **Out of asyncio:** emit a signal on a `QObject` that lives on the GUI thread (PySide6 queues it across threads).
- **Voice on:** the tray/hotkey starts the voice thread. **Voice off:** it cancels the pipeline (`PipelineWorker.cancel()`), joins the thread, and closes the audio devices. Brain jobs already running are not cancelled by turning voice off; their results show up as cards/strip as they do today.

---

## 5. The mouth

### 5.1 What it is

- An `AnthropicLLMService` with thinking disabled and prompt caching on.
- Its conversation lives in a Pipecat `LLMContext` managed by an `LLMContextAggregatorPair`.
- The assistant aggregator records **only the words actually spoken** when you interrupt: Cartesia's word timestamps drive it, and Pipecat strips Cartesia's SSML tags from those tokens.
- Long sessions are kept bounded by Pipecat's auto context summarization (`LLMAssistantAggregatorParams(enable_auto_context_summarization=True)`), not by our own trimming.
- One mouth context per voice session. It starts fresh when voice is turned on.

### 5.2 Prompt (behavioural, no keyword lists)

Short, in the same voice as `yuki/agent/prompt.py`. It says, in prose:

- **Who:** you are Yuki's voice. A capable friend sitting next to the user, not a service desk. You speak; you cannot see or touch the PC. Yuki's hands and eyes are a separate part of you that you reach with `tell_yuki`.
- **What you handle yourself:** conversation, opinions, general knowledge, arithmetic, anything that needs no look at this PC or the user's stuff.
- **What you hand over:** anything that needs the PC, its screen, files, apps, the web through the PC, or the user's memory. Call `tell_yuki` **in the same reply** as a short spoken acknowledgement that says what you are doing, before the tool call. This matters mechanically: starting an async tool does not by itself trigger another reply, so the acknowledgement must come with the call.
- **Honesty about work:** until a `tell_yuki` result says so, the work is not done. Talk about it in the present or future ("opening it now"), never the past. When a result arrives, report it plainly; if it failed or was cancelled, say so.
- **Relaying the brain:** progress lines and questions from Yuki arrive as results. Say them in your own words, briefly. Don't read out URLs, paths, ids or code; describe them.
- **Speaking style:**
  - One or two short sentences.
  - No lists, no markdown.
  - Match the user's language: English or Hinglish, as they speak.
  - Let feeling show when it fits: warmth, amusement, sympathy, excitement. Use the TTS emotion tags sparingly (§10), at most one per reply.
- **Corrections and stops:** when the user changes or adds to what they asked while Yuki is working, pass it on with `intent="follow_up"`. When they want the work stopped, call `cancel_tell_yuki`. When they start something unrelated, use `intent="new_task"`.
- **What the user knows about them:** a labelled block with the memory portrait (see §5.4), background, not instructions.

Pipecat appends its own `ASYNC_TOOL_INSTRUCTIONS` to the system prompt automatically: answer what the user just said first, then give the arrived result at the end of that same reply, and say it once.

### 5.3 The mouth's tools

| Tool | Kind | Arguments | What happens |
|---|---|---|---|
| `tell_yuki` | **async** (`cancel_on_interruption=False`, `cancellable_by_llm=True`, `timeout_secs=None`) | `message: str` — what Yuki should do, written for Yuki, with references resolved ("the Lo-fi playlist we talked about", not "that one"). `intent: "new_task" \| "follow_up"` | See §6.2. Stays open for the life of a `new_task` job, streaming intermediate results; settles with the final result |
| `cancel_tell_yuki` | built in, generated by Pipecat because `cancellable_by_llm=True` | the tool call id | Pipecat cancels the handler (throws `CancelledError` into it); the handler calls `bridge.cancel(job)`; Pipecat tells the mouth the call was cancelled |
| `yuki_status` | sync, instant | none | Returns the bridge's job table (§6.3): what's running, current step label, seconds elapsed, last `say`, pending question, queued jobs. Lets the mouth answer "what are you doing?" truthfully without waiting on the brain |

`follow_up` covers both "answer to Yuki's question" and "change of plan", so the mouth doesn't have to tell those apart; the bridge routes by the brain's state.

### 5.4 Context the mouth gets

- **System:** prompt (§5.2), the memory portrait block from `MemoryAccess.context(...)` read once when the voice session starts (off the event loop, bounded like today at 3 s), current date/time.
- **Tools:** the three above.
- **Caching:** Haiku 4.5 on Bedrock caches only a prefix of **at least 4,096 tokens** per checkpoint (Sonnet 5: 1,024; Opus 5: 512). A short prompt silently won't cache. The stable prefix (persona + portrait of ~1,500 tokens + tools + a few worked examples of good spoken replies) is sized to cross 4,096 on purpose, so TTFT and cost drop on every turn after the first. V0 measures whether it does.
- **Warm-up:** like the brain's `prewarm`, one throwaway call (`max_tokens=1`) with the same system and tools when voice turns on, so the first real turn hits a warm cache. Logged as `startup_cost`.

---

## 6. The bridge: how mouth and brain talk

`yuki/voice/bridge.py`. The only code that touches both worlds.

### 6.1 Job ids (runtime change)

Today `AgentRuntime.submit(text)` returns nothing and signals carry only a lane name. To send results back to the right `tell_yuki` call:

- `AgentRuntime.submit(text, *, origin_hwnd=None, source="text") -> str` returns a `job_id`.
- Every runtime signal carries it: `started`, `thought`, `tool_called`, `tool_finished`, `asked`, `said` (new), `finished`, `failed`, `queued`, `lane_done`.
- A front-desk → worker hand-off keeps the same `job_id`.
- `AgentRuntime.cancel(job_id)` cancels a running job, or removes a queued one from its lane's queue. Today `Lane.cancel()` leaves the queue alone, and a cancel issued before `run()` starts is lost because `run()` clears the flag.

### 6.2 `tell_yuki` routing

```
tell_yuki(message, intent) handler, on the voice thread:

  raw = the user's own words for this turn (last TranscriptionFrame text, from the user aggregator)
  text = message + "\n\n[The user's own words: \"" + raw + "\"]"      # plumbing: keeps the brain close to what was said

  if intent == "new_task":
      job = bridge.submit(text, origin_hwnd=foreground at end of turn)   # → GUI thread → AgentRuntime.submit
      loop:  event = await job.events.get()
             Say(text)        → result_callback({"yuki_says": text},       FunctionCallResultProperties(is_final=False))
             AskUser(q)       → result_callback({"yuki_asks": q},          FunctionCallResultProperties(is_final=False))
             Queued           → result_callback({"status": "queued behind the current task"}, is_final=False)
             Final(text)      → result_callback({"done": text});  return
             ErrorEvent(text) → result_callback({"failed": text}); return   # cancellation arrives here too (outcome=cancelled)
      on CancelledError (cancel_tell_yuki or voice off):  bridge.cancel(job); raise

  if intent == "follow_up":
      target = the most recent unfinished voice job (worker first)
      if none:                       treat as new_task
      elif target.awaiting_answer:   bridge.answer(target, text)   → Lane.answer → Agent.answer
      else:                          bridge.steer(target, text)    → Agent.steer (new, §7.1)
      result_callback({"status": "passed on to the running task"})   # settles at once; the original call carries the outcome
```

- Intermediate results carry `run_llm` unset. Pipecat then runs the mouth once the user and the mouth are both quiet, so `say` lines and questions get spoken promptly but never over anyone. Pipecat batches several results that arrive together into one reply.
- A result that arrives while the user is speaking goes into the context and is spoken at the end of the next reply (Pipecat's standing instruction).
- `new_task` while the worker is busy goes through the existing routing unchanged: the front desk (look-only) takes it. If it needs hands, the runtime queues it for the worker and the `tell_yuki` call reports `queued`. One task at a time on the mouse and keyboard stays true.

### 6.3 The job table (for `yuki_status`)

The bridge keeps, per job: `job_id`, `lane`, `state` (queued / running / waiting_for_answer / done / failed / cancelled), `request`, `started_at`, `step_label` (from `tool_called`, built with the existing `tool_label`), `last_say`, `question`, `outcome`. Updated from runtime signals on the GUI thread, read from the voice thread under a lock. It is the single source of truth for "what is Yuki doing": the Talker–Reasoner "shared belief state" pattern (DeepMind 2024), without anything being narrated.

### 6.4 Result payloads (what the mouth sees)

JSON objects, one key naming the kind, so the mouth never has to guess:

```json
{"yuki_says": "Found the playlist, starting it now."}
{"yuki_asks": "There are two Lo-fi playlists, Lo-fi Beats and Lo-fi Study. Which one?"}
{"status": "queued behind the current task"}
{"done": "Playing Lo-fi Beats on Spotify."}
{"failed": "Spotify didn't open: it isn't installed for this user."}
```

A cancelled call is settled by Pipecat itself with its own notice ("this tool call was cancelled before it returned a result").

---

## 7. Brain changes

Small and contained. Text mode benefits too.

### 7.1 Steering inbox: `Agent.steer(text)`

- A thread-safe `queue.SimpleQueue` on `Agent`.
- **Drained at the step boundary:** its items are appended as a text block to the next tool-results message (right before `context.add_tool_results`, `loop.py:703-708`), framed as `[The user added while you were working]: …`.
- This keeps the one-`tool_result`-per-`tool_use` rule intact. Consecutive user content is already routine in the transcript.
- **If the brain is about to finish** (`done` called, or a text-only reply) and the inbox is not empty, it does not finish: the steer goes in and the loop takes another step. Otherwise a correction made during the last model call would be silently dropped.
- Logged as `steer` (new event type).
- **Latency:** a step is 3.5 s fixed + 13.8 ms per output token (`config.py:55-62`), so a correction lands within one step. Aborting an in-flight model call to land it sooner is not needed for v1.

### 7.2 Spoken progress: the `say(text)` tool

**Schema:**

```python
{"name": "say", "label": "Telling you",
 "description": "Tell the user something worth hearing while you keep working: a milestone, a finding, "
                "or why this is taking a while. The user may be listening rather than watching. "
                "Not every step, and never the final answer (that goes in done).",
 "input_schema": _obj({"text": {"type": "string", "description": "One short spoken sentence."}}, ["text"])}
```

**Behaviour:**
- Added to `CONTROL_TOOLS`, so every lane has it. It is placed at the end of the control group, which costs one cache write when rolled out.
- **Not** in `_DEFERRED_TOOLS`, so it runs mid-stream the moment its block completes. The user hears the milestone while the model is still writing the next action.
- It always returns `ok=True` ("said"), instantly. It never waits for the speech: a failure would set `stop_after` and drop `done`.
- New `ToolOutcome.kind = "say"`, a new `Say(text)` event (added to `AgentEvent`, `EVENT_TYPES`, `Lane._drive`), and a new runtime signal `said(lane, job_id, text)`.
- **Text mode:** the status strip shows it.

### 7.3 Cancel that lands

The goal is that "stop" actually stops.

- **Add a cancel checkpoint after `_request` returns**, before yielding `Final`. Today a text-only final reply is yielded even after a cancel.
- **Clear the flag at `run()` start only for jobs that were not cancelled while queued.** The runtime's per-job cancel (§6.1) covers this.
- **v2:** abort an in-flight model stream on cancel by closing the stream from `cancel()`. The stream object is kept on the agent, and iteration then raises and is handled as cancelled. Running tools stay uninterruptible, as documented today, and are bounded by their own timeouts (PowerShell 20 s, tree 6 s, launch 8 s).

### 7.4 Prompt

No change to the shared frozen prompt. The brain already ends with "what you would text back … usually one sentence, no lists or markdown" (`prompt.py:105-114`), which is ideal raw material. The mouth rewrites it for speech anyway (rule §2.1). So voice needs **no brain prompt change and no cache bust** beyond adding `say`.

### 7.5 Origin

- Voice requests have no overlay, so no `origin_hwnd` is captured today.
- The bridge records the foreground window (`GetForegroundWindow`) at the user's end of turn and passes it as `origin_hwnd`. "Put the user back where they were" keeps working.

### 7.6 Microphone-in-use fact

The request origin reports which program is using the mic (`loop.py:1642-1659`), and the prompt reads a live mic as "on a call".

- With voice on, Yuki itself holds the mic. It is tagged "(the program you run as)" only when the mic user's exe basename equals `sys.executable`'s. `yuki-ui` may run under `pythonw.exe` while `sys.executable` says `python.exe`.
- V1 verifies the tag holds for the voice thread's PyAudio stream, and otherwise compares by PID or process image path instead. Otherwise every voice request would look like "user is on a call".

---

## 8. How everything flows (sequences)

Times are targets, not measurements (§14).

**A. Chit-chat — the brain is never involved**
```
user: "how's it going?"      Flux EndOfTurn (~0.26 s) → mouth (Haiku, ~0.8 s TTFT) → Cartesia (~0.13 s)
yuki: "Pretty good! Quiet day on your PC so far."          ≈ 1.1–1.6 s after the user stops
```

**B. A simple task**
```
user: "play my lo-fi playlist"
mouth: text "Sure, putting it on." + tool_use tell_yuki(new_task, "Play the user's Lo-fi playlist on Spotify")
       └ spoken immediately; bridge.submit → worker lane → Agent.run
brain: launch_app spotify → look_at_window → click → done("Playing Lo-fi Beats on Spotify.")   (~8–20 s)
       └ Final → result_callback({"done": ...}) → mouth runs when both are quiet
yuki: "It's playing — Lo-fi Beats."
```

**C. A long task with progress**
```
brain: ... say("Found three invoices from September, adding them up.")
       └ Say → intermediate result → mouth: "Found three September invoices, adding them up now."
brain: ... done("Total is ₹48,200 across three invoices.")
yuki: "All done — ₹48,200 across the three."
```

**D. The brain needs an answer**
```
brain: ask_user("Two Lo-fi playlists — Beats or Study?")  → generator pauses (existing)
       └ AskUser → intermediate {"yuki_asks": ...} → mouth: "Quick one — Lo-fi Beats or Lo-fi Study?"
user: "study"
mouth: tell_yuki(follow_up, "Lo-fi Study") → bridge sees awaiting_answer → Agent.answer → brain resumes
       (follow_up call settles at once; the original new_task call carries the outcome)
```

**E. A correction mid-task**
```
user (while the brain works): "actually use YouTube, not Spotify"
mouth: "Got it, switching to YouTube." + tell_yuki(follow_up, "Use YouTube instead of Spotify")
bridge: job running, not waiting → Agent.steer → lands at the next step boundary (≤ one model call)
```

**F. Stop**
```
user: "stop, never mind"
mouth: "Okay, stopping." + cancel_tell_yuki(<id>)
Pipecat cancels the handler → bridge.cancel(job) → AgentRuntime.cancel(job_id) → Agent.cancel()
brain stops at the next checkpoint (v2: aborts the in-flight model stream at once)
```

**G. Barge-in while Yuki is talking**
```
yuki: "It's playing — Lo-fi Beats, and I also noticed your—"
user: "thanks, that's all"          Flux StartOfTurn → InterruptionFrame → TTS and the mouth's reply cut
context keeps only "It's playing — Lo-fi Beats, and I also noticed your" (word timestamps)
the brain is untouched (tell_yuki is async; interruption does not cancel it)
```

**H. A result lands while the user is talking**
```
brain finishes while the user is mid-sentence about something else
aggregator: user speaking → result written into the context, no reply triggered
user finishes → mouth answers them first, then adds "…and the playlist's on, by the way."
```

**I. A new request while the worker is busy**
```
mouth: tell_yuki(new_task, "What's the weather in Bangalore?") → AgentRuntime.submit → worker busy → front desk
front desk answers (look-only) → Final → mouth speaks it; the worker's task keeps going
if the front desk reaches for hands → runtime queues it for the worker → {"status": "queued…"} → mouth says so
```

---

## 9. Turn-taking and interruptions

- **Turn detection is Flux's.** `DeepgramFluxSTTService` recommends its own turn strategies to the user aggregator: `ExternalUserTurnStrategies`, or `EagerUserTurnStrategies` when constructed with `enable_eager_end_of_turn=True`.
  - We pass no `user_turn_strategies` of our own, so Flux owns start and end of turn.
  - Pipecat's default local Smart Turn v3 does not run alongside; a second detector would trigger a second inference for the same turn.
  - A Silero VAD on the aggregator is optional; Pipecat's docs say it only improves STT metrics. Left out unless the metrics need it.
- **Starting settings** (Deepgram's low-latency preset): `eot_threshold=0.7`, `eager_eot_threshold=0.4`–`0.5`, `eot_timeout_ms=5000`–`6000`.
  - Eager mode starts the mouth ~100–200 ms early.
  - It costs 50–70 % more mouth calls, which is cheap at Haiku prices.
  - A speculative reply is held until the turn is confirmed and dropped if the transcript changes (`SpeculationGate`, `NormalizedMatch`).
- **Barge-in:** Flux `StartOfTurn` while the mouth is speaking → `InterruptionFrame` → Cartesia output and the in-flight mouth reply are cancelled. The assistant aggregator trims the spoken record to the words actually played.
- **Backchannels** ("mm-hmm", "haan") should not cut Yuki off.
  - The mechanism is Pipecat's `MinWordsUserTurnStartStrategy`: a user turn *starts an interruption* only after N words while the bot is speaking.
  - This is a numeric turn-taking threshold like the VAD's, not a keyword list.
  - Start at 2, tune from the logs.
  - Passing it means passing `UserTurnStrategies(start=[...])` ourselves, which overrides Flux's recommendation. Not combined with the eager strategy until V3 proves the combination.
- **Don't use** `FunctionCallUserMuteStrategy`. It mutes the user during function calls, and ours run for the length of a task.
- **Keyterms:** Flux `keyterm` gets "Yuki" and the user's names from `Settings.user_names`. This is data, not logic.

---

## 10. Voice and emotion

- **Cartesia Sonic 3.6** with a voice from Cartesia's emotion-capable set (Leo, Jace, Kyle, Gavin, Maya, Tessa, Dana, Marian); final pick by ear.
- **The mouth writes emotion inline**, using Cartesia's own markup, which Pipecat's `CartesiaTTSService` passes through and strips from word timestamps:
  - `<emotion value="excited"/>`, `<emotion value="sympathetic"/>`, `<speed ratio="1.1"/>`, `[laughter]`, `<break time="300ms"/>`.
  - The prompt lists a small palette and says: at most one per reply, only when it fits.
- **Limits:**
  - Unverified end to end: tags written by the LLM, passing through sentence aggregation into Cartesia. Pipecat's docs prefer inserting tags with a text transformer. V0 checks that a tag inside a streamed reply reaches Cartesia intact; if not, the mouth writes a tag only at the start of a sentence.
  - Cartesia treats these as guidance, not commands.
  - Emotion works in English only; Hinglish replies speak neutrally.
  - Professional voice clones can't change speed per request.
- **Display:** tags are removed before a line is shown as a card or written to `requests.csv` (a plumbing strip of Cartesia's markup). The JSONL log keeps the raw text.
- **If Cartesia disappoints:**
  - ElevenLabs Flash v2.5: fast, no tags, emotion only via voice settings.
  - `eleven_v3_conversational`: audio tags (`[laughs]`, `[whispers]`, `[sighs]`, `[excited]`), GA 2026-08-19, but ~0.5 s slower in independent tests. Use stability "Creative" or "Natural"; "Robust" suppresses tags.
  - Switching providers is a config change (`voice_tts_provider`). The prompt's tag palette follows the provider.

---

## 11. Echo cancellation

Without it, speakers feed Yuki's own voice into the mic; Flux hears speech and the mouth interrupts itself.

- **v1: headphones.** Documented requirement; the tray's voice toggle says so the first time.
- **v2: WebRTC APM.**
  - `livekit` (1.1.20, one wheel for all Pythons) → `livekit.rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=True, high_pass_filter=True)`.
  - **Wiring in Pipecat:**
    - `SpeakerTap`, a `FrameProcessor` placed right before `transport.output()`, copies each outgoing TTS audio frame into `apm.process_reverse_stream(...)`. That is the far-end reference: exactly what goes to the speakers.
    - `ApmFilter`, a `BaseAudioFilter` passed as `LocalAudioTransportParams.audio_in_filter`, runs `apm.process_stream(...)` on the mic audio.
    - Both use 10 ms frames at the same rate (16 kHz in, resampled).
    - `apm.set_stream_delay_ms()` comes from PyAudio's reported output + input latency, not a guessed constant. The value is logged.
  - **Acceptance:** a 60 s recording with Yuki talking over speakers produces no Flux `StartOfTurn`. The user runs this test (rule 7).
- **Fallback:** `pywebrtc-audio` (AEC3, automatic delay estimation).
- Windows' own communications-mode echo cancellation isn't reachable from PyAudio (the stream category can't be set), so we don't rely on it.

---

## 12. UI integration

- **Tray:** a "Voice" on/off item next to Model/Effort, plus the mic and speaker device choice. It shows "headphones recommended" until the echo canceller exists.
- **Hotkey:** a new `voice` action (`YUKI_VOICE_HOTKEY`, default `ctrl+alt+v`) toggles the voice session.
  - `YukiUi._on_hotkey` currently treats every non-`cancel` action as the overlay toggle (`app.py:169-177`), so `voice` gets its own branch.
  - Push-to-talk needs key-down/up events the hotkey layer doesn't have; later.
- **Status strip gets a persistent voice state** while voice is on: `listening` · `hearing you` · `thinking` (mouth generating) · `speaking` · `working` (a brain job runs; the existing step line) · `muted`.
  - Driven by Pipecat frames (`UserStartedSpeakingFrame`, `BotStartedSpeakingFrame`, …) forwarded by the bridge.
  - The strip never takes focus (unchanged).
- **Questions:** in voice mode the brain's `ask_user` is spoken, so `_on_asked` must not pop the overlay and grab focus (`app.py:237-247`). It shows the question as a card without focus, and a typed answer still works.
- **Transcript cards:** optional. The overlay can show the last exchange (user words + Yuki's words, tags stripped).
- **Cancel hotkey** Alt+Shift+X keeps working. It also cuts current speech: the bridge queues an `InterruptionWorkerFrame` on the pipeline worker, which turns it into an `InterruptionFrame`.

---

## 13. Logging

Same contract as ARCHITECTURE.md §Logging: JSONL, one record per event, never truncated.

- **New file** `logs/sessions/<id>-voice.jsonl`, written by a `VoiceLogObserver` (a Pipecat `BaseObserver`) plus the bridge. The `SessionLogger` is wrapped in a lock like the memory service's `_Log`, because observers run on the voice thread.
- **New event types** (added to `EVENT_TYPES`):

| type | fields |
|---|---|
| `voice_session` | start/stop, devices, sample rates, providers, models, voice id, thresholds, echo mode |
| `stt_turn` | transcript, language, confidence, `eager` bool, start/eager/end timestamps, resumed |
| `mouth_request` | full system, messages, tools, settings (as `llm_request`) |
| `mouth_response` | content blocks, stop reason, usage, ttfb_ms, latency_ms, speculative bool, withdrawn bool |
| `mouth_tool_call` / `mouth_tool_result` | name, args, tool_call_id, job_id, payload, is_final |
| `tts` | text (raw, with tags), ttfb_ms, chars, interrupted bool, spoken_text (what was actually heard) |
| `interruption` | at, what was cut, words heard |
| `bridge` | job_id, submit/steer/answer/cancel, lane, state change |
| `steer`, `say` | brain-side, in the lane's own JSONL as well |
| `turn_latency` | per user turn: `user_stopped → eot → mouth_first_token → first_audio`, from Pipecat's `UserBotLatencyObserver` / metrics (`enable_metrics=True, enable_usage_metrics=True`) |

- **`logs/voice_turns.csv`**: one row per user turn: `timestamp, session, transcript_chars, language, eot_ms, mouth_ttft_ms, tts_ttfb_ms, first_audio_ms, speculative, interrupted, mouth_input_tokens, cache_read_tokens, output_tokens, cost_usd, job_id`. This is how thresholds and prompts get tuned.
- **Audio recording is off** (`voice_record_audio=False`). Turning it on writes `<id>-voice/turn-<n>.wav` via Pipecat's audio buffer processor. It is off by default for privacy.

---

## 14. Latency budget

Median, from this PC in India, warm. Estimates from vendor and independent numbers; V0 replaces them with measurements.

| Stage | Target |
|---|---|
| Mic capture + 80 ms chunks (+ echo canceller in v2) | 50–100 ms |
| Flux end-of-turn (transcript arrives with it) | ~260 ms (eager: ~100–200 ms earlier) |
| Haiku first token on Bedrock + round trip to `us.` | 600–900 ms (cached prefix lowers it) |
| First speakable phrase formed | 100–200 ms |
| Cartesia first audio | 130–200 ms |
| **User stops → Yuki's voice starts** | **≈ 1.1–1.6 s**, aim < 1.2 s |

For reference: human turn gaps average ~200 ms (Stivers et al. 2009), and voice agents start to feel sluggish past ~0.8–1.2 s. The levers, in order of payoff:
1. Eager end-of-turn.
2. The 4,096-token cached prefix.
3. The mouth's reply starting with a short clause, so the first phrase is ready sooner.
4. The `global.` inference profile if it measures faster than `us.`.
5. Anthropic API direct (~200 ms faster TTFT than Bedrock, but a second account and auth path; only if 1–4 are not enough).

Brain work is not on this path at all; its time shows up as spoken progress, not silence.

---

## 15. Configuration

**New `Settings` fields** (defaults shown; every one mutable at runtime the way `model`/`effort` are, where it makes sense):

```python
voice_hotkey: str = "ctrl+alt+v"                     # YUKI_VOICE_HOTKEY
voice_input_device: int | None = None                # PyAudio index; None = system default
voice_output_device: int | None = None
voice_echo: str = "headphones"                       # "headphones" | "apm"
voice_stt_model: str = "flux-general-multi"
voice_stt_languages: tuple[str, ...] = ("en", "hi")
voice_eot_threshold: float = 0.7
voice_eager_eot_threshold: float | None = 0.5        # None = eager off
voice_eot_timeout_ms: int = 5000
voice_barge_in_min_words: int = 2
voice_mouth_model: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
voice_tts_provider: str = "cartesia"                 # "cartesia" | "elevenlabs"
voice_tts_model: str = "sonic-3.6"                   # or eleven_flash_v2_5 / eleven_v3_conversational
voice_tts_voice: str = ""                            # chosen by ear in V0
voice_tts_speed: float = 1.0
voice_record_audio: bool = False
```

**Environment:** `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY` (or `ELEVENLABS_API_KEY`), existing `AWS_BEARER_TOKEN_BEDROCK`, `AWS_REGION`.

**Dependencies** (verified installable on Py 3.13 / Win 11): `pipecat-ai[deepgram,cartesia,anthropic,local]==1.11.*` (+ `elevenlabs` for the alternative, `livekit` for v2 echo cancellation). The Smart Turn torch extra (`local-smart-turn`) is not needed: Flux does turn detection.

---

## 16. Failure handling

| Failure | Behaviour |
|---|---|
| Deepgram connect/stream error | Pipecat reconnects. On a fatal error (`FluxFatalError`): voice turns off, strip shows "voice unavailable", text mode untouched, logged |
| Mouth call fails or throttles | Pipecat retries once on timeout (`retry_on_timeout`); then the mouth says nothing, the strip shows the error, and the next turn tries again. Brain jobs unaffected |
| TTS fails | Text of the reply goes to a card instead; logged |
| Brain errors | `ErrorEvent` → `{"failed": …}` → the mouth says it plainly |
| Pipeline idle | Never self-terminates: `idle_timeout_secs=None` (Pipecat's default would cancel after 300 s of no frames) |
| Voice turned off mid-task | Pipeline cancelled, `tell_yuki` handlers get `CancelledError`, but the **brain job is not cancelled** (only `cancel_tell_yuki` or the cancel hotkey cancel work). Results land as cards/strip as in text mode |
| Mic/speaker device vanishes | PyAudio error → voice off with a clear message; device choice in the tray |
| Bedrock credentials missing | Voice refuses to start with the same message the brain uses |

---

## 17. Package layout

```
yuki/voice/
  __init__.py      # VoiceSession: start()/stop() the voice thread; public entry used by ui/app.py
  pipeline.py      # builds transport → STT → aggregator → mouth → TTS → tap → output → aggregator; PipelineWorker + runner
  mouth.py         # mouth system prompt, tool schemas (tell_yuki, yuki_status), handlers, warm-up
  bridge.py        # VoiceBridge: job table, Qt⇄asyncio hand-over, routing (submit / steer / answer / cancel)
  audio.py         # device listing/selection; ApmFilter + SpeakerTap (v2)
  log.py           # VoiceLogObserver → <id>-voice.jsonl, voice_turns.csv
```

Brain-side edits:
- `agent/loop.py`: `steer`, the `say` kind, the cancel checkpoint.
- `agent/tools.py`: the `say` schema and `CONTROL_TOOLS`.
- `log/events.py`: `Say` and new event types.
- `ui/runtime.py`: job ids, per-job cancel, `said`.
- `ui/app.py`, `ui/hotkey.py`, `ui/status.py`: the toggle, voice hotkey, voice state.
- `config.py`: the §15 fields.

**Pipeline sketch** (Pipecat 1.11 API, signatures checked against the installed source; values illustrative):

```python
transport = LocalAudioTransport(LocalAudioTransportParams(
    audio_in_enabled=True, audio_out_enabled=True,
    input_device_index=s.voice_input_device, output_device_index=s.voice_output_device,
    audio_in_filter=apm_filter if s.voice_echo == "apm" else None))

stt = DeepgramFluxSTTService(
    api_key=os.environ["DEEPGRAM_API_KEY"],
    enable_eager_end_of_turn=s.voice_eager_eot_threshold is not None,   # Flux then recommends EagerUserTurnStrategies
    settings=DeepgramFluxSTTService.Settings(
        model=s.voice_stt_model, language_hints=[Language.EN, Language.HI],
        eot_threshold=s.voice_eot_threshold, eager_eot_threshold=s.voice_eager_eot_threshold,
        eot_timeout_ms=s.voice_eot_timeout_ms, keyterm=["Yuki", *s.user_names]))

mouth = AnthropicLLMService(
    api_key="",                                        # unused: the client carries auth
    client=AsyncAnthropicBedrock(aws_region=s.aws_region),
    settings=AnthropicLLMService.Settings(
        model=s.voice_mouth_model, enable_prompt_caching=True,
        thinking=AnthropicLLMService.ThinkingConfig(type="disabled")))
mouth.register_function("tell_yuki", bridge.tell_yuki,
                        cancel_on_interruption=False, cancellable_by_llm=True, timeout_secs=None)
mouth.register_function("yuki_status", bridge.yuki_status)

tts = CartesiaTTSService(api_key=os.environ["CARTESIA_API_KEY"],
                         settings=CartesiaTTSService.Settings(model=s.voice_tts_model, voice=s.voice_tts_voice))

context = LLMContext(messages=[{"role": "system", "content": mouth_system(portrait)}],
                     tools=ToolsSchema(standard_tools=[TELL_YUKI, YUKI_STATUS]))
user_agg, assistant_agg = LLMContextAggregatorPair(          # no user_turn_strategies: Flux supplies them
    context,
    assistant_params=LLMAssistantAggregatorParams(enable_auto_context_summarization=True))

pipeline = Pipeline([transport.input(), stt, user_agg, mouth, tts,
                     speaker_tap, transport.output(), assistant_agg])   # assistant aggregator AFTER output = only heard words
worker = PipelineWorker(pipeline,
                        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
                        idle_timeout_secs=None,     # default is 300 s + cancel: an always-on session would kill itself
                        observers=[VoiceLogObserver(logger), UserBotLatencyObserver()])
runner = WorkerRunner()
await runner.add_workers(worker)
await runner.run()
```

**Pipecat 1.0 (2026-04-14) broke most pre-April-2026 tutorials.** Use the current names:

| Use | Not (removed or deprecated) |
|---|---|
| `PipelineWorker` | `PipelineTask` (deprecated 1.3) |
| `WorkerRunner` | `PipelineRunner` (deprecated 1.3) |
| `LLMContext` + `LLMContextAggregatorPair` | `OpenAILLMContext` (removed 1.0) |
| `Settings` | `InputParams` (deprecated) |
| `InterruptionFrame` | `StartInterruptionFrame` (removed) |
| `MinWordsUserTurnStartStrategy` | `MinWordsInterruptionStrategy` (removed) |
| `pipecat.turns.user_mute` strategies | `STTMuteFilter` (removed) |

Pin `pipecat-ai==1.11.*`: releases ship every 1–3 weeks.

**Closest upstream reference:** `examples/multi-worker/openclaw-agent/` in the Pipecat repo, a voice loop in front of a slow coding agent with `send_to_agent` / `stop_agent` / `agent_status`.
- It speaks brain results by queueing `LLMMessagesAppendFrame(run_llm=True)`, which runs the LLM immediately, even over the user.
- We use async-tool intermediate and final results instead, because those wait until user and bot are both quiet (§6.2).

---

## 18. Build phases

Each phase ends with something the user can try live (rule 7: builders verify by import, compile and stub-driven offline checks only).

- **V0 — talking loop, no brain.**
  - Mic → Flux → Haiku → Cartesia → speakers, headphones on.
  - Prove: Bedrock accepts Pipecat's Anthropic calls for Haiku (the `interleaved-thinking` beta header); the cached prefix crosses 4,096 tokens and cache reads show up; real latency per stage (`voice_turns.csv`); Flux on the user's Hinglish (accuracy, and which script Hindi comes back in); voice choice.
  - Voice log in place from day one.
- **V1 — brain hand-off.**
  - `tell_yuki` (new_task only) + bridge + job ids + tray toggle/hotkey + status states.
  - Results spoken from `Final` / `ErrorEvent`.
- **V2 — two-way.**
  - `say`, `ask_user` over voice, `follow_up` (answer + `Agent.steer`), `cancel_tell_yuki` + per-job cancel + the post-request cancel checkpoint, `yuki_status`, the mic-in-use tag check.
- **V3 — feel.**
  - Eager end-of-turn, the barge-in minimum-words threshold, emotion palette tuning, WebRTC echo cancellation (speakers without headphones), stream abort on cancel.
- **V4 — later.**
  - Push-to-talk, wake phrase (Pipecat has `WakePhraseUserTurnStartStrategy`), ending a session by voice, writing voice conversations to memory (today Yuki's own conversations are never captured), local fallback (faster-whisper large-v3-turbo int8 + Kokoro fit in the RTX 2060's 6 GB).

---

## 19. Costs (rough, per hour of voice session)

- **Flux** bills streamed audio, including silence, while the session is open: 60 min × $0.0065 ≈ **$0.39/h**. Turning voice off when not talking matters more than anything else here.
- **Mouth:** a turn with a cached ~5k-token prefix plus ~1–2k of fresh context and ~60 output tokens is ≈ $0.002–0.003. At 60 turns/h that's **≈ $0.15/h**.
- **Cartesia:** ~750 chars per spoken minute. 10 min of Yuki speech/h ≈ 7.5k chars ≈ **$0.30–0.40/h**.
- **Brain:** unchanged, whatever the tasks cost today (`requests.csv`).

---

## 20. Decisions considered and rejected

- **Speech-to-speech "live" APIs** (OpenAI Realtime, Gemini Live): simplest and most natural, but not available to this project.
- **One model doing both jobs:** the brain's 3–8 s think would be dead air on every turn.
- **Pipecat's own `BackendLLMWorker` / job system for the brain:** Pipecat 1.11 ships a front-end/back-end delegation pattern. Rejected because Yuki's brain already exists, with perception, memory, lanes, logging and safety. Rebuilding it as a Pipecat worker would duplicate all of it. Async tools are the thinnest seam.
- **Streaming the brain's thinking or every tool event to the mouth:** it over-narrates and invites made-up progress. The brain's `say` plus the `yuki_status` pull replace it.
- **A canned "wait a minute, I'm looking into it":** robotic by the third time. The mouth's acknowledgement is written for the request, in the same reply as the hand-off.
- **Code that rewrites or blocks "done"-like words while a task runs:** suggested by some write-ups, but it's a keyword heuristic (rule 1). Structure and the prompt carry this instead.
- **Rate-limited progress timer ("an update every 8–10 s"):** a fixed timer steering behaviour (rule 1). The brain decides via `say`.
- **Separate `stop_yuki` tool:** replaced by Pipecat's built-in `cancel_tell_yuki`, which also settles the call correctly in the mouth's context.
- **LiveKit Agents:** excellent, but built around a media server; nothing here needs one. (Its console mode does apply WebRTC echo cancellation locally, the one thing we'd envy.)
- **TEN Framework:** its licence adds conditions against hosting it on end-user devices, so it's out for a desktop assistant.
- **`LLMMessagesAppendFrame` to speak brain events** (the openclaw example's way): runs the mouth immediately, even while the user or the mouth is speaking. Async-tool results wait for quiet.

---

## 21. Open questions and risks

1. **Hinglish on Flux multi:** vendor claims in-sentence code-switching. No source covers Indian English or Hinglish specifically. V0 decides between `flux-general-multi` and `flux-general-en`.
2. **Haiku 4.5 end-of-life** on Bedrock "no sooner than" Oct 2026. The model id is a setting; watch for the successor and re-measure TTFT.
3. **The Bedrock beta header** from Pipecat's Anthropic service (V0, one call). Fallback: `AWSBedrockLLMService`.
4. **Echo cancellation quality** on this PC's speakers (V3).
5. **The async tool contract under stress:** several results in one breath, a `follow_up` racing a `Final`, cancel during `ask_user`. Stub-driven offline tests in V2; live by the user.
6. **Mouth context vs brain context:** they are separate by design. The brain sees the mouth's `message` plus the user's own words, not the whole spoken conversation. If the brain needs more ("the thing I mentioned five minutes ago"), the mouth must resolve it in `message`. Watch the logs for tasks that fail from missing context.
7. **Session length:** Pipecat's auto summarization keeps the mouth context bounded. Its summaries are logged and checked for dropped commitments.
