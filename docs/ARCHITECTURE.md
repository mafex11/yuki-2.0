# Yuki — architecture contract (v2: the 3 Oct 2026 revamp on top of v1)

Yuki is a hands-free assistant that lives on the user's Windows PC. The user types or speaks a request; Yuki sees the desktop, thinks, acts, and talks back. It must be fast, able to pause and ask the user mid-task, and prefer commands/shortcuts over mouse clicks.

**How this file is organised.** The first half is the v2 architecture as built by the 2-3 Oct 2026 revamp: the loop (Resolve, Plan, Act, Authorize, Verify, Learn), voice, proactivity, the personal brief, the UI, processes, migrations 26-31 and the known gaps. The second half (package layout onward) is the v1 contract for perception, actions, the agent and logging, still true except where a line says it changed. Diagnosis and plan: `docs/research/revamp-2026-10-02/00-DIAGNOSIS-AND-PLAN.md`; build reports `20`-`33`; review `34`; the user's decisions `docs/CHAT-2026-10-02-revamp.md` §6. Details live in `docs/MEMORY.md`, `docs/VOICE.md` and `docs/UI.md`.

**Status (3 Oct 2026).** Phase A (calls, cards, Stop, voice replies, personal pipes, retrieval, perception, memory correctness) is installed and was live-tested on the real PC (`23-live-test-2026-10-03.md`). Phases B-E (resolver, practice memory, judge and finish gate, personal brief, check-ins, voice v3.17/3.18, the Lattice window and island) are merged to `main` and tested offline only, one test file at a time; the real DB is at migration 27 until the next install. Nothing in B-E has been seen on the real desktop yet.

The old LLM-OS project (a separate repo) is REFERENCE ONLY. Salvage low-level Windows code from it where useful; never import from it.

## Non-negotiable rules

1. **No hardcoded behavior heuristics.** No keyword lists, regexes, or string matching that decide what the agent does (no "if 'chrome' in name", no app allowlists, no "is this a question" detectors, no fixed staleness timers steering logic). Behavior comes from the model reading the situation. Deterministic code is allowed only for: plumbing, safety confirmation gating, and parsing structured API responses.
2. **No regex/XML parsing of model output.** Use native tool calling (Anthropic Messages API `tools` + `tool_use` blocks) exclusively.
3. **No fixed sleeps.** Wait for a condition (window appeared, focus changed, tree stable, process exited) with a timeout. Never `time.sleep(0.8)` to "let things settle".
4. **Log everything.** Every model request (full system, messages, tools), every response (full content blocks, usage, latency), every tool call and result, every perception snapshot, every question to the user and their answer, every error. JSONL, one file per session, never truncated. Plus a human-readable pretty stream on the console.
5. **No LangChain/LangGraph.** Plain Python + `anthropic` SDK (`AnthropicBedrock`) + boto3 + Windows libs.
6. **Keyboard/mouse input is guarded.** Every input action (`click`, `type_text`, `hotkey`, `press`, `scroll`) accepts `expect_hwnd`; when set, input is sent only if that window is still in the foreground, otherwise the action fails without sending. Tests and scripts must never send Alt+F4 or any key without `expect_hwnd`, and must close windows they opened by terminating the PID. (On 2026-09-22 an unguarded Alt+F4 closed the user's terminal and killed the build session.)
7. **Builders never touch the live desktop.** No sub-agent or script written by one may launch apps, send input, take screenshots, run smoke tests, benchmarks or end-to-end agent runs. Verify by import, compile and stub-driven offline checks only. The user does live testing. (Two terminal-closing incidents on 2026-09-22.)
8. **Windows-only, Python 3.13, `uv`.** Project root: the repo checkout. Package `yuki/`. Run with `uv run ...`.

Added by the revamp (2-3 Oct 2026; the dated decisions behind them are in `docs/HANDBOOK.md` §6, and each has a test):

9. **No card, button or offer starts an agent.** A card runs a typed offer by code (`yuki/memory/offers.py`, closed list: `todo_add`, `show_memory`, `bring_back`, `draft`, `ask`) or puts text in the ask bar *unsent*. Nothing is ever sent from a card.
10. **Nothing is sent to anyone without a plain instruction** ("send X to Y" = what and to whom). Held by the authorization judge, which sees only the user's words and the proposed call, and by the autonomy policy: `send_messages` can never be set above "ask".
11. **One Yuki.** Cards, check-ins, offers and nudges are Yuki speaking. No "coach" persona in prompts, brackets or UI copy; asked why it said something, Yuki owns it.
12. **Models decide meaning, code measures.** Confidence, decay, spacing, budgets and floors are numbers computed from ledgers, counts, cosines and times; a model never reports its own confidence. Code checks model output against stored strings (ids, quotes, whole-word spans), never against word lists.
13. **Fix the pipeline, never hand-edit Yuki's memory.** A wrong fact is corrected by re-running Yuki's own passes (`rejournal`, `recheck`, `check-commitments`, `--revet`), which write bi-temporally and keep history.

## Model access

- Client: `anthropic.AnthropicBedrock(aws_region=os.environ["AWS_REGION"])`. Auth comes from the `AWS_BEARER_TOKEN_BEDROCK` user env var automatically. Do not put keys in files.
- Model ids (config, default first): `us.anthropic.claude-sonnet-5`, `us.anthropic.claude-opus-5`.
- Use `thinking={"type": "adaptive"}`; do not send `temperature` or `budget_tokens`. Parse tool inputs as JSON objects (already parsed by SDK).
- Verified working on 2026-09-22 (~2.5–3s round trip).

## The loop, end to end (revamp, 3 Oct 2026)

Every request, typed or spoken, runs one loop. The core prompt states it in one line ("resolve, plan when the stakes call for it, act, verify, record", `yuki/agent/prompt.py`); the code behind each step is below. The same loop serves typed requests and the voice thinker (the same `Agent`).

```
 typed words / heard words (voice) / the ask bar after a card's "ask" offer
        │
        ▼
 RESOLVE    code, 5-50 ms     contacts + routes · people directory · referent register · task frame
        │                     · practice (procedures, habits, pitfalls) · goals · relevant memory
        ▼
 PLAN       only when the stakes call for it: reaches a person / cannot be undone / could mean two things
        ▼
 ACT        a playbook is a first attempt · UIA patterns before the global mouse and keys
        │   · window leases · yields to the user's own input
        ▼
 AUTHORIZE  the judge sees ONLY the user's words and the proposed call · tool traits · autonomy policy
        │   (a gate before each gated action, drawn as a step because it is its own model call)
        ▼
 VERIFY     finish gate: verified / expected-unverifiable / contradicted · a send is confirmed
        │   from the watcher's own capture of the chat · consult on real trouble
        ▼
 LEARN      run trace → distiller / post-mortem → practice memory · nightly miner over the user's
            own activity · confidence computed in code · every use logged (injected / cited / outcome)
```

### 1. Resolve (who, where, how the user does it)

| Piece | Module and entry point | What the request gets |
|---|---|---|
| People and routes index | `yuki/memory/contacts.py`: `Linker.run` (nightly after the audit; `--rebuild` / `--refresh`), `rank_routes`, `card`, `rhythm`; tables `contacts`, `contact_routes`, `contact_prefs` (migration 28) | Who a person is across apps and where the user talks to them, routes ranked by the user's own recent messages, with the margin between the top two (under `ROUTE_MARGIN` 0.35 it is a close call). One model call links identity; code computes every number. |
| People directory | `MemoryClient.people_directory()`, a string of at most 900 characters, in the cached profile layer (typed agent, voice thinker; the talker through `fetch_people`) | Names, relation, usual routes, tone. No counts or dates, so it stays cacheable. |
| `about(name, prefer_route?, words?)` | `yuki/agent/tools.py` → `MemoryAccess._about` → `MemoryClient.contact` | The person's card (counts, the name each app knows them by, the user's last messages to them as style samples), no model call. `prefer_route` keeps "no, the other one" only when the user's words quote it. |
| Referent register | `yuki/agent/frame.py` `referents()` | Candidates for him / her / this / that, each with a source and an age: the chat in front, people named earlier in the conversation, chats in front in the last 10 min, the people around the current call, the selection or focus, the clipboard's metadata. Strength halves per source half-life; under 0.15 is dropped; the top two within `REFERENT_MARGIN` 0.25 are a close call and Yuki asks, naming both. |
| Task frame | `frame.build()`, fed by `Agent._frame_inputs` (`MemoryClient.frame_facts`, the dispatcher's focus read, the clipboard) | At most `FRAME_CHARS` 2,500 characters, the same for typed and voice, first in the volatile block: the register, the person named (or the one in the chat in front) with routes, margin, "usual channel" places, preconditions (is the usual app open) and stakes (reaches a person, cannot be taken back), 0-2 procedures and one habit or style note, 0-3 pitfalls. Empty when nothing clears a floor. Measured 4-7 ms median. |
| Practice lookup | `MemoryClient.practice(words, open_apps=...)` | Procedures, habits and pitfalls for the apps in play (named in the words, open windows), matched on intent with app names taken out. ~7 ms. Goes into the frame; pitfalls of the apps in play always come. |
| Relevant memory | `yuki/memory/related.py`, `MemoryAccess.context` | Searched with the user's own words (`memory_query`), never the voice boilerplate; Yuki's own conversation of the last 30 min is excluded (self-echo 26% → 0); other-day chats weigh less for an action request; to-dos, know-how and threads are injected only above a cosine floor (0.40). Injected memory per request 10.1k → 6.2k characters. |
| Goals and commitments | `MemoryClient.standing_parts()` groups, rendered by `commitments_block_text` | Today's goals and open work goals (700 characters), Yuki's own commitments, the user's own to-dos, each labelled as theirs. To-dos never pose as rules. |

### 2. Plan (only when the stakes call for it)

A prompt-level step, no separate model call. The core prompt: before anything that reaches another person, cannot be undone, or could mean two things, know the exact target, the content and the route; ask one short question naming the options when two candidates are a close call; otherwise try first and never ask the same question twice. The frame's stakes and preconditions are its inputs, and the spoken step line (`say_now`) is the "plan line" in voice.

Not built: a structured plan stage. The island has no "step n of N", no Go / Change keys and no 3 s send-hold window (`31-build-E6-E10.md` §3). The autonomy level `show_plan` is stored by the policy but nothing yet pauses for Go.

### 3. Act

| Piece | Module | Behaviour |
|---|---|---|
| Loop and tools | `yuki/agent/loop.py` (`Agent.run`), `yuki/agent/tools.py` (`Dispatcher`) | Native tool calls. Several calls in one response run in order and stop at the first failure. A playbook (id in the `playbook` field of every action tool) is the first attempt: check its first step against the screen, adapt, ask when it names no single default. |
| UIA patterns first | `yuki/actions/patterns.py` `element_action` | A click on a read element invokes, toggles or selects it through its pattern; `type_text clear` uses `Value.SetValue`; scroll uses `ScrollPattern`. The result says `via: uia_pattern` and the mouse does not move. Only when no pattern fits does it fall back to the mouse and keys; a pattern that ran is never repeated by the mouse. |
| Leases | `yuki/actions/leases.py` | One global input lease plus a lease per window (`window_lease`, `LeaseBusy` after 5 s); `leased` wraps click, type, hotkey, press, scroll and hover, so a voice hand and the agent cannot act on one window at once. |
| Input yield | `InputGate` (`GetLastInputInfo` minus Yuki's own input spans) | The user used the keyboard or mouse within 1 s: one spoken line, wait for 0.8 s of quiet, at most 12 s, then nothing is sent and the brain is told (`input_yield*` settings). Media keys are exempt. |
| Vanished windows | `Dispatcher._follow_vanished` | A call naming a window that no longer exists runs on its successor (same process, best title likeness and overlap, measured score) and says so; with none it fails plainly. |
| Reader lane | `yuki/agent/lanes.py` | The lane used while the worker is busy sees only read tools plus `hand_over`; a `hand_over` call queues the request for the worker, which keeps sole ownership of the mouse and keyboard. |
| Perception | `yuki/perception/*`, `yuki/core/opacity.py`, `yuki/memory/now.py` | Tree-first, no pixels (29 Sept decision). "Opaque" is a measured coverage (5%), never a claim that contradicts captured text. See the v1 perception contract below. |

### 4. Authorize (the judge)

| Piece | Module | Behaviour |
|---|---|---|
| Tool traits | `yuki/agent/tools.py` `Traits`, `TOOL_TRAITS`, `OP_TRAITS` | Fixed per tool and operation, never read from words: `kinds` (autonomy-policy kinds), `needs` (input, focus, window, none), `destroys`, `reaches`. Window close, tab close, keys, hotkeys, `run_powershell` and `type_text` with Enter always reach the judge. |
| Gate | `yuki/agent/judge.py` `gate_reasons` | A call goes to the judge when it destroys or reaches, or when one of its kinds is held below "do it and tell me". A kind at "just suggest" runs nothing and costs no model call. |
| The judge | `yuki/agent/judge.py` `Authorizer` (Haiku 4.5; `Settings.judge`, off with `YUKI_JUDGE=0`) | Two stages: a fast forced `verdict`, and on a hold a careful `review` whose decision follows by a fixed rule from its own three booleans (toward / lasting / asked_effect). It sees only the user's words, Yuki's earlier calls of the request (calls, never results) and the proposed call (tool, args, target window, element role and name, marked as app-written). Memo per request, window grants for later quiet steps, three denials in a row or twenty in all end the request honestly, errors and the 8 s timeout fail closed. |
| What counts as the user's words | `Agent.run(user_words=)`, `loop._note_words`, `judge.words_text` | Typed words; in voice the STT final turns as heard. The talker's `request` is "Yuki's reading, NOT their words" and never authorizes a send. A card's text is Yuki's, never an instruction; a suggestion sent unchanged is Yuki's offer. Words of an earlier request expire after 120 s once a new request's words arrive. |
| Autonomy policy | `yuki/ui/policy.py`, `%LOCALAPPDATA%\Yuki\autonomy.json`; read by `judge.PolicyReader` (mtime reload) | Six kinds (read screen, open apps, click and type, send messages, files, money) by four levels (suggest, show plan, ask, do and tell). Defaults: click and type **do and tell** (Claude's call of 3 Oct, `c27cfb9`: the judge added ~1 s per click under "ask"; the judge still gates closing, deleting, keys, commands and sends), send messages **ask** with a ceiling of ask, money **suggest** with a ceiling of ask. Fixed rules no setting turns off. A hand-edited file can never lift a kind past its ceiling. Per-app "Act" switch in the same file. |
| Voice hands | `yuki/voice/hands.py` (`Hands._judge`, `RISKY_CLICK_ROLES`) | The talker's instant hands go through the same `Authorizer` with the heard words: window and tab close, clicks on Button / SplitButton / MenuItem, typed text with a line break, and the key steps of a learned way. Links and plain typing are not judged (the type hand never presses Enter). A hold means the talker asks the judge's one plain question. The per-app Act switch is enforced here. |

Measured offline (`30b-build-D-brain.md`): the unasked close of Chrome from the 3 Oct replay is held 9 of 9; 0 of 36 calls the user asked for were held; allow p50 1.06 s, a hold 3-4 s; about $0.0012 per cached allow.

### 5. Verify

| Piece | Module | Behaviour |
|---|---|---|
| Finish gate | `yuki/agent/finish.py` `classify`, `finish_request`; `loop.action_verdict` | Each action is verified (its own report shows the effect), expected-unverifiable (a control learned from practice memory: at least two independent confirmations, never contradicted), or contradicted. Only a verified report ends the request. `finishes_with: true` ends it with a code-written line ("Chrome is minimized."); `finish_words` only when the report cannot name the outcome. An unverifiable read-back never escalates. |
| Send confirmation | `finish.confirm_send` | After a judged send, memory's capture of that chat is polled for the user's new message (Now refreshed at 0.25 s and 2.3 s, memory every 0.35 s, about 4 s, similarity at least 0.6). Seen: verified, "sent". Not seen: the result says "not confirmed" and the done is held once for a look. |
| Consult | `yuki/agent/consult.py`; `Settings.consult`, off with `YUKI_CONSULT=0` | The actor's effort and thinking never change inside a request (no 53k-token cache rewrite). On a measured signal (an action contradicted by its report, a loop-breaker trip, a close call at the first action, a pending send to a person) one side call to Sonnet 5 with adaptive thinking at medium effort returns `advise{verdict, blocking, advice}`. At most two per request; a pending step is held only when `blocking`. |
| Evidence for claims | `done.about`, `loop._claim_gaps` | A state claim needs a read or a verified action on that window in this request (v3.14); the message goes back once, "look first". |

### 6. Learn

| Piece | Module | Behaviour |
|---|---|---|
| Run traces | `Agent._trace_step`, `_record_run` → `MemoryClient.record_run`; voice hands `record_hand_trace`; table `runs` (migration 29) | One row per step: tool, target as app + control role and name (never a window number), verdict and why, result facts, time, the step's share of its model call's cost. Window and element ids and coordinates are kept apart as the run's "numbers". Secret-filtered, encrypted, pruned at 30 days. Written on the turn writer, off the request path. |
| Distiller and post-mortem | `yuki/memory/practice_learn.py` `Learner` (Haiku), run by `PracticeWorker` every 30 s | Triggers are measured: a run that ended well with at least three actions (or trouble then success) is distilled into a procedure; a cancel, two bad actions of one tool, the same unverified call twice, two questions, a breaker trip, words added mid-run, or the next words repeating the run (cosine 0.60, or within 30 s of a run that acted) get a post-mortem whose lesson is generalised to its family ("volume has three targets"). Validation in code: every step cites trace steps that ran ok, a pitfall cites failed or unverified steps or the user's own words, no run number in the text, quotes must be spans of the user's words. |
| Nightly miner | `practice_learn.py` `Miner` (Sonnet, low effort) | After 22:00, idle 10 min, no call, nothing full screen: candidates from the people index, sites by dwell, Warp commands per folder and app surfaces, recurring at least 3 times on at least 2 days, become habits ("Hemanth: DM first"); abstentions become teach-back questions. |
| Confidence | `yuki/memory/practice.py` `evaluate` | Computed, never asked of a model: `(S+1)/(S+F+2) × 0.5^(days since last evidence / half-life)` from the ledger `playbook_events` (weights: observed 1, ran_ok 1, taught 3, ran_trouble -2, user_corrected -3, contradicted -3; half-life 60 days for procedures and pitfalls, 180 for habits and style). States: candidate → active → trusted → stale → retired; migrated old know-how rows sit on **probation** (never shown) until a run confirms them. |
| Use is logged | `playbook` field, `record_run` | Every request logs which objects were `injected` (in the frame text) and `cited`; outcomes are weighed only against what was measured. |
| Other pipelines | `commitments.py`, `recheck.py`, `contacts.py`, `brief.py`, `offers.py`, `checkins.py` | Keep memory true and personal: see `docs/MEMORY.md` "The revamp's memory modules". |

Measured (`27-build-C.md`): after one correction the volume mistake was not repeated (the budget-matched control repeated it); procedures alone did not cut model calls on Warp (each step still needs a look), which is why the talker got executable skills (below).

### What a request carries

| Layer | Content | Cache |
|---|---|---|
| Tools | 30,358 characters (was 70,190); the seven memory reads are one `memory(kind, ...)` tool | cached with the core |
| Core | `SYSTEM_PROMPT`, 12,977 characters (was 31,761), organised around the loop; a spoken request adds `VOICE_LAYER` (1,704 characters) | breakpoint 1 |
| Profile | The user's names, the **personal brief's thinker slice** (replaces the Personality section and the persona), the people directory | breakpoint 2 |
| Rules | The user's standing rules verbatim: 438 characters on the real copy (was 2,351 with to-dos and card feedback mixed in) | breakpoint 3 |
| Volatile, in `messages` | Memory block, the desktop with the Now picture, **task frame**, connected memories, commitments (goals, own commitments, the user's to-dos), then the user's words | the moving conversation breakpoint |

Cached prefix, typed: 32.5k → 14.8k tokens; voice thinker: 33.7k → 16.9k tokens; first token about 200 ms faster.

**Request pack.** `Agent.build_pack(request, origin_hwnd, view_hwnd, memory_query, voice, lean)` reads the window list with Now, the origin window, the view and memory's part off the request path; `run(pack=)` uses it, re-reading the screen half when older than 20 s (`pack_max_age_s`). The voice bridge builds one while the user is speaking when the worker is idle and no call is on, and passes it to `runtime.submit(pack=)` within 15 s.

### Models and cost by job

Costs are measured on DB copies and offline replays; live cost is not measured yet.

| Job | Model | Measured cost or latency |
|---|---|---|
| Typed agent (actor) | Sonnet 5, medium effort, adaptive thinking | first token ~1.4 s cached |
| Voice thinker | Sonnet 5, low effort, thinking off | first token ~1.4 s cached |
| Voice talker | Haiku 4.5, no thinking | first token p50 ~1.1 s, cached prefix ~16.1k tokens |
| Authorization judge | Haiku 4.5 | allow p50 1.06 s, ~$0.0012 per cached allow, a hold ~$0.0024 |
| Consult | Sonnet 5, medium, adaptive | 3-9 s, at most 2 per request |
| Hand-off router (off) | Haiku 4.5 | ~0.9 s, $0.0015 per turn |
| Run learner | Haiku 4.5 | $0.0045-0.011 per pass; all practice calls ≤ $0.15 per day and ≤ 20 passes |
| Nightly miner | Sonnet 5, low | $0.03-0.04 per night |
| People linker | Sonnet 5, low | $0.11-0.16 per run, only when memory changed |
| Personal brief | inside the nightly Sonnet render | about +$0.025 per render |
| Check-in look | Sonnet 5 writer + Sonnet 5 vet | ~$0.03 and 9 s per look; 2-3 looks a day |
| Check-in fold-in | Haiku 4.5 | ~$0.004 per answer |
| To-do check, recheck | Haiku 4.5 | ~$0.011 (at most every 30 min, only on newer evidence); ~$0.01 per review |

The talker, judge, router, learner, to-do check, recheck and journal all run on Haiku 4.5, so its Bedrock end of life (not before 2026-10-16) matters; model ids are settings (`yuki/config.py`).

### Switches

| Switch | Where | Default |
|---|---|---|
| Judge, consult | `Settings.judge`, `Settings.consult` (`YUKI_JUDGE=0`, `YUKI_CONSULT=0`) | on |
| Autonomy levels, per-app Act | `%LOCALAPPDATA%\Yuki\autonomy.json` (Settings dial) | click/type do and tell, send ask |
| Offers, check-ins | `privacy.toml` `[nudges] offers = "yuki_only" \| "off"`, `checkins_enabled`, `checkins_per_day` | on, 3 a day |
| Speech channel | `Settings.voice_speech_channel` or voice.json `speech_channel` | `marker` (`guard` goes back) |
| Results by code, prefetch, router | `voice_results_by_code`, `voice_prefetch_results`, `voice_router` | on, on, **off** |
| Talker practice, skills | `voice_practice`, `voice_skills` | on, on |
| Window engine | `%LOCALAPPDATA%\Yuki\ui.json` `window_engine` | `web` |
| End tone | `voice_end_tone`, tray switch | on (the user has not confirmed) |

## Voice: talker and thinker (v3.18; the contract is `docs/VOICE.md`)

Pipecat pipeline: Deepgram Flux STT → **talker** (Haiku 4.5) with **hands** (instant, verified one-step actions) and `tell_yuki` hand-offs to the **thinker** (the `Agent`, one worker) → the narrator gate → Gemini Leda TTS (Aura fallback). Local wake word "Yuki". One voice: only the talker speaks, and the thinker's step lines and results are spoken in that same voice.

| Piece | Module | What it does |
|---|---|---|
| Marker speech channel | `yuki/voice/mouth.py` `ReplyChannel`, `SPEECH_CHANNEL_PROMPT` | The talker writes a `say`, `act` or `quiet` marker line, then its words; `ReplyChannel` parses that fixed format and drops scratch text before the speech starts. The old per-line speech guard runs in shadow mode (measured, never blocking). 220 replies on the real talker: 0 unmarked, 0 reasoning spoken. An unmarked reply costs a rerun, not silence. |
| Owed-output ledger | `yuki/voice/narrator.py` `OwedLedger` | Every non-stale thinker result, failure and question is owed to the user. It settles when audible words follow it; otherwise code says it (up to 2 tries, 2.5 s apart), then it shows on the pill with a sound. `stay_quiet` on a run that owes something settles with no second talker run. |
| Answered means heard | `pipeline.TurnObserver._heard_words`, `_reply_watch` | A 20 ms breath is not an answer. An unheard user turn is rerun once; if that is silent too it plays the error tone. |
| Results spoken by code | `bridge._by_code`, `_said_final`; `voice_results_by_code` | A verified finish, or a result short enough to say whole (2 sentences, no question, no path), is said word for word by code, one piece per sentence, unguarded. Result heard about 1.0 s after the thinker ends (was 2.9-4.1 s through the talker). Sentences are synthesised as the thinker writes them (`prefetch`) and played when the done is accepted. |
| Hands | `yuki/voice/hands.py` | `media_key`, `app_volume`, `scroll`, `window`, `tab`, `click`, `type`, `open_url`, `open_app`, `open_yuki`; "done" only from a verified report; risky hands judged (see Authorize); traces recorded; Now's popups and other visible windows are numbered items the click hand can use. |
| Practice for the talker | `yuki/voice/practice.py` `PracticeLookup`, `TalkerPractice` | One lookup per user turn on the final words (10-41 ms): a Lessons line (at most 400 characters: pitfalls and habits of the apps in play) and a Learned ways line in the volatile block. |
| Skills | `yuki/voice/skills.py` `RUN_SKILL_TOOL`, `SkillRunner`; `voice_skills` | The talker may run a learned procedure as a hand, step by step, each step checked through its own verified hand. Offered only above cosine 0.56 and confidence 0.70 (above what one run can reach, 0.667). A step that fails or does not show its effect stops the run and the thinker gets the request with what was done. A send step needs `to` and `what` to be whole-word spans of the heard words. |
| Router (**off by default**) | `yuki/voice/router.py`; `voice_router` | An under-2k-token Haiku call decides from the user's words whether the turn needs the thinker and starts it early. 91.7% agreement with the talker's own choice, hand-off 1.9 s → 1.25 s, $0.0015 a turn, about 1 turn in 12 starts a thinker that is withdrawn. |
| Request pack | `bridge.prefetch_pack`, `_take_pack` | See "What a request carries". |
| `user_words` path | `bridge._submit(user_words=)` → `runtime.submit(user_words=)` → `Agent.run(user_words=)` | The heard turns travel apart from the talker's rendering so the judge sees the user's actual words. |
| Calls | `pipeline.Conversation._watch`, `session.VoiceSession` | In call mode the conversation stays open while the user's mic is voiced, a turn is unanswered, a hand is held or something is owed. When one ends by itself a soft tone and the pill line "Say 'Yuki' to talk again" show, and the stop record says why. "Stop listening" in a call means for this call (`call_off`, never saved). A lost speaker reroutes to Windows' default output only when the headphone check accepts it in a call. |
| TTS | `yuki/voice/gemini_tts.py` | Fallback per piece, not per conversation; Aura hedge after 1.8 s; a circuit breaker (2 failures in 60 s); one Aura retry on a dropped connection. |
| Stability | `yuki/voice/qos.py` | The voice process opts out of power throttling during a conversation; loop, speaker and mic threads join MMCSS "Audio". No disk access on the voice loop (63 → 0). |

Voice measured offline (`26`, `29`): first audio p50 3.38 → 3.19 s; popup click ("restore the pages") 8.5 → 2.2 s; "lower your volume": v3.17 talker wrong 16 of 24, right 17 of 18 once a learned lesson was on its line, v3.18 talker (its own voice marked on the app-volume line) right 36 of 36 with or without the lesson; "open YouTube" in the habit's browser 6 of 6. Known: "Open Chrome and restore the pages" still only opens Chrome (0 of 5 replays).

## Proactivity: cards, offers, check-ins

Principle (the user, 29 Sept and 3 Oct): Yuki never speaks first; proactivity is cards only. A card informs, asks, or offers one small thing Yuki can do by code. It never starts an agent, never sends, and never offers work on the user's own code, files, documents or messages. It is Yuki speaking: there is no coach.

| Piece | Module | Behaviour |
|---|---|---|
| Card worker | `yuki/memory/nudges.py` `NudgeWorker.tick` | Order each pass: welcome back → due reminders → **check-ins** → observation cards → questions. Every gate holds all of them: paused, locked, snoozed, quiet hours, a meeting or full-screen app, Yuki acting, away, not settled, any open call. Budgets per kind through `yuki/core/attention.py`. |
| Observation cards | `yuki/memory/presence.py` `PresenceWorker` | Looks only on measured signals (a recurrence of the user's own facts at cosine 0.75, a finished call session, a milestone, a screen-recall hit, a thread change, and `plans`: a plan of the user's within a day). Haiku judges, Sonnet writes, code checks ids and shape. A `stuck`, `recall` or `thread` card must bring new information. |
| Typed offers | `yuki/memory/offers.py` | `{do, label, steps, refs, text, due}` with `do` from a closed list: `todo_add` (Undo ends it), `show_memory`, `bring_back` (an http(s) page the user had; Undo brings back the window), `draft` (shown with Copy, never typed or sent), `ask` (put unsent in the ask bar). Validated at write and again at the click (30 min TTL, once per card, refs re-resolved, `[nudges] offers`). A click is run by code with no model call. Learned per (channel, kind): Less of these -1, Not now -0.5, stopped within a minute -1, Undo -1, kept +1; a kind at -3 loses its button for a while. |
| Check-in moments | `yuki/memory/checkins.py` `CheckinWorker` | Five measured moments: `day_start` (first settled stretch after a 5 h+ break), `new_thread` (a thread of 5+ touches over 45+ min with no goal), `followup` (a goal's check-in time, a milestone on its thread, a day they named passed, or a burst then quiet), `evening` (from 19:00 or the measured usual end less 4 h), `care` (three 10 h+ days, a 12 h day, or late nights). Past, present and future frames, one per frame a day, at most 3 a day, one question card waiting at a time. |
| Goals | table `goals`, `goal_events` (migration 30); `MemoryClient.goals()` | An answer is stored on its question and folded by one Haiku call into goals (today's, a thread's), done / dropped / open, a check-in time, and how the user took being asked. Goals reach every request in the commitments block. |
| Worth-asking vet | `yuki/memory/questions.py` `vet` | Every question and check-in passes it: a required `worth_asking` boolean answered before the decision (serves the user's goals, work, wellbeing or Yuki's understanding; a caring assistant would ask it; not operational trivia of a task thread; not a relay of another tool's or person's question; not drawn from an agent's text). New decision `not_worth`. Run on 46 recent questions: 36 dropped, 5 already answered by memory, 3 kept. `python -m yuki.memory.questions --revet`. |
| Learning from replies | `checkins.checkin_scores`, `offers.offer_scores` | An answer +1 (glad +1.5), "why are you asking" or "don't ask" -2, Skip -0.5, Less of these -1; a kind's spacing doubles per point below zero and rests at -3. |
| One Yuki | every memory-side prompt starts "You are Yuki"; card context bracket `[You said on a card at 14:32 ...]` | Asked why, Yuki says what it noticed and when and takes the answer as feedback. |

Replay of the last 7 days: 11 check-ins in 7 days, 6 dropped by the vet; about half were what the user described, some weak (someone else's reply, an evening remark, a fix status), tightened afterwards (`30c` §5). Care never fired in the replay: the test user's evenings had no quiet gaps.

## The personal brief and its consumers

Code: `yuki/memory/brief.py`; written by the nightly portrait render itself (a `brief` field of `save_portrait`, no call of its own), checked in code (`validate_brief`: people only from the people index, bits only citing joke facts, live items only citing offered ids with a date in the week's window, no ids left in text). Fields: `with_him`, `people` (at most 12), `own_bits` (the only teasing material), `now` (at most 5 dated live items), `routine`, `careful` (private topics, labelled by the model).

| Consumer | Slice | Where |
|---|---|---|
| Typed agent and voice thinker | `thinker`: the whole brief | cached profile layer, in place of Personality + persona; `loop` passes it to `profile_text(brief=)` |
| Voice talker | `talker`: `with_him`, names and relations, bits, careful; at most 2,100 characters, "tone and names, never news" | `MouthMemory.fetch_brief` → the talker's cached profile layer |
| Card writers (coach look, presence, check-ins, welcome back) | `cards`: the whole brief as ABOUT THE USER | `nudges._about_lines` |
| "What did I do today" | content first: what changed, what is open, dates ahead | `MemoryClient.activity_header` at the top of the `activity` result |
| "Write it like me" | the user's last two messages on the usual route | the task frame's person block |
| Measurement | ids of brief lines and memory items per reply and card (`surfaced`, migration 31) and a nightly Haiku judge: personal / generic / wrong / plain | `python -m yuki.memory.brief --show \| --surfaced \| --judge` |

Replay brief off vs on: no measurable gain overall (personal 10 vs 9 of 16), clear wins on content-first "today" and the Hinglish draft to a friend (`30c` §5).

## The UI: the Lattice window and native ambient surfaces (`docs/UI.md`)

Design chosen 2 Oct 2026: **Lattice** (dots; one colour, `live` mint, that only marks something live). The same design tokens feed both halves.

| Half | Engine | What |
|---|---|---|
| The window | HTML in WebView2, own process `python -m yuki.ui.web_window`, **0 MB when closed** | Home, Work (+ Day), Me, Playbook, Activity, Settings, the thread drawer. Thin ctypes WebView2 host (`yuki/ui/webview2.py`, not pywebview), files by virtual-host mapping (no localhost server), strict CSP, no navigation off the app origin, every permission denied. Paints first from an encrypted snapshot, then reads memory itself and talks to the tray over the window pipe (30 ops). `window_engine = web \| qt` in `ui.json`; `qt` (or a missing WebView2 runtime) opens the old Qt window. Frameless with the design's caption buttons. |
| Ambient surfaces | Native Qt, painted from the same tokens | The **island** (voice pill, run strip and call pill merged: top centre of the monitor in use, 17×7 dot face plus a bounded halo), the **run card**, **cards** (offer, question, break, notice, check-in, return, call consent), toast, ask bar, tray states, first-run wizard. Idle 0% CPU; +5.8 MB private. |
| Tokens | `yuki/ui/web/tokens.json` → `python -m yuki.ui.tokens_build` → `tokens.css` + `tokens.py` (`--check` fails when stale) | One source; the Qt theme and `surface.py`, `overlay.py` and the wizard read `yuki/ui/tokens.py`. |
| Parity | `scripts/lattice_shots.py`, `web_shots.py`, `web_transitions.py` | The app must look and move exactly like the Lattice mock; checked side by side, differences listed (UI.md). |

Window measured (`28`): first paint ~1.0 s warm, 134-171 MB private with `--disable-gpu`, idle about 0.5-0.9% of one core with 0 canvas redraws, a live field ~15% of one core (the user accepted this on 3 Oct), 0.5% under a full-screen app.

## Processes and pipes

| Process | Entry | Owns |
|---|---|---|
| Tray / UI | `pythonw -m yuki.ui.boot` → `yuki.ui.app` | Agent runtime (worker and reader lanes), island, cards, ask bar, tray, voice session, window host (pipe server) |
| Memory service | `yuki.memory.service` | Watcher, journal, timeline, episodes, portrait worker (nightly: audit, threads tidy, **contacts refresh**, render with the brief), conversations worker (**commitments check**), card worker (**check-ins**, presence), calls, **PracticeWorker** (thread `yuki-memory-practice`), Now board and embedder pipes |
| Window | `yuki.ui.web_window` (or `yuki.ui.window` for Qt) | Started by a tray click, its own process; reads memory itself |
| Wake | `yuki.voice.wakeproc` | The local wake word |
| Transcriber | child of the service | Deferred call transcription on a quiet GPU |

Pipes: the window pipe (`yuki/ui/window_ipc.py`, `VERSION = 2`, user-only DACL, a key derived from memory's DPAPI key proven both ways), the Now pipe, the embedder pipe.

## Migrations 26-31 (all additive; older code opens a newer `user_version`)

| v | Adds | For |
|---|---|---|
| 26 | `calls.hold_since`, `calls.last_alive_at`, `nudges.outcome`, `nudges.outcome_at` | One call = one session (`CallLife`), restart adoption; what became of each offer |
| 27 | `threads.ident_key`, `threads.merged_into`, `conversation_turns.envelope_ciphertext`, `portraits.sections_ciphertext`, plus a Python step `_migrate_27` | Threads keyed by service + chat (merged 8 split threads on a copy: a direct chat 1106 → 5, a group chat 1024 → 22); the user's words stored apart from Yuki's envelope; portrait fields with budgets |
| 28 | `contacts`, `contact_routes`, `contact_prefs` | People and routes index |
| 29 | `runs`, `playbooks`, `playbook_vec`, `playbook_events` | Run traces and practice memory |
| 30 | `goals`, `goal_events`, `questions.moment`, `goal_id`, `folded_at`, `reply_tone` | Check-ins and goals |
| 31 | `surfaced` | Personal-ness log (the brief) |
| 32 | none in the tree when this was written; the number is held for fx1's review fixes (`35-build-review-fixes.md`) | |

Before each install: back up `memory.db` and `memory.key`. Migration 27 rewrites rows in place (review M8), so it needs the backup.

## Known gaps (review 34, 3 Oct; fx1 is fixing most of the high ones, check `git log` before relying on this list)

| Gap | Effect |
|---|---|
| H2: a click on Send, Delete or Approve does not reach the judge in the typed agent and thinker at the default policy (the voice hands judge Button, SplitButton and MenuItem) | The "nothing is sent without a plain instruction" rule is applied to keys and Enter, not to clicks |
| H3: the judge's memo key has no target | An allow can replay on another window of the same request |
| H5: the per-app Act switch is read by the voice hands, not by the agent | Settings says Yuki will not act in an app; the typed agent still can |
| M1, M2: in the typed agent a newline inside `type_text` text is not treated as Enter; `open_url` of a file and `fetch_url` are outside the gate | A typed line break can send a message unjudged; a local file or an outbound fetch can run unjudged |
| H6: `window_lease` cleanup leaves ~128 locks held | After long uptime, random `LeaseBusy` hand failures |
| H8: the nightly thread-date repair uses a floor that moves once the DB is over 31 days | Old threads' true start dates are rewritten |
| H7: real names from the user's chats are in tracked tests, scripts and some prompts | Privacy if the repo is shared; fixed in voice files, not elsewhere |
| Not built | Structured plan stage (Go / Change, "step n of N"); per-word confidence in the live caption (the voice must send `words`); evidence chips on cards; reader-lane routing while the worker is idle |
| Live | No live check yet of the island's click-through and focus behaviour, the frameless window by hand, the real Discord call (A1, A3), a real card with an offer (A4), UIA pattern clicks on real apps |

## Package layout

```
yuki/
  config.py          # dataclass Settings: model ids, log dir, screenshot policy (never|ask|auto, default never (test phase)), judge/consult/voice switches
  log/
    events.py        # event schema (TypedDicts/dataclasses) + SessionLogger (JSONL + pretty console)
  perception/        # "eyes"
    windows.py       # desktop overview
    tree.py          # UIA tree for one window; the focus with its selection and caret text
    visibility.py    # what is on screen: every window and popup in Z-order, with how much of each can be seen
    clipboard.py     # the clipboard's text and formats (content an app marked private is never read)
    screenshot.py    # screenshot of a window, region, monitor or the desktop; native resolution, origin + scale reported (off: the user's decision, no pixels)
    system.py        # system facts (processes, memory, cpu, time)
  actions/           # "hands"
    launch.py        # launch app via shell:AppsFolder / Start-Process; focus by hwnd
    input.py         # click/type/scroll/hotkey/key (no glide, no fixed sleeps)
    patterns.py      # UIA patterns before global input (invoke, toggle, select, SetValue, scroll)
    leases.py        # global input lease, per-window leases, InputGate (yield to the user)
    shell.py         # PowerShell runner with correct arg passing + timeout + persistent session
  agent/
    tools.py         # tool JSON schemas + Dispatcher (tool_use -> perception/actions), Traits
    prompt.py        # system prompt: the loop, layered and cached (core, voice layer, profile, rules)
    loop.py          # Agent: run(request) -> yields events; ask_user pause/resume, cancel, run trace, build_pack
    frame.py         # referent register and task frame (code only)
    judge.py         # the authorization judge (Authorizer, PolicyReader)
    finish.py        # finish gate, finish lines, send confirmation
    consult.py       # the second look on real trouble
    lanes.py         # reader lane, hand_over
    memory.py        # MemoryAccess: memory tools and context, record_run
    context.py       # context hygiene: replace stale large perception results with stubs; running summary
  memory/            # yuki-memory service (docs/MEMORY.md): watcher, journal, portrait, contacts, practice,
                     #   practice_learn, checkins, brief, offers, recheck, commitments, presence, nudges, calls ...
  voice/             # Pipecat voice (docs/VOICE.md): pipeline, mouth, bridge, hands, skills, practice, router, qos ...
  ui/                # tray, island, cards, ask bar, tokens, policy, web_window + web/ (Lattice window), window/ (Qt fallback)
  core/              # attention budgets, opacity, procs, paths, doctor, models
  cli.py             # terminal REPL
tests/               # pytest, run one file at a time (the whole suite in one process stops early)
docs/
logs/                # gitignored; sessions/<timestamp>.jsonl
```

## Perception contract (module `yuki.perception`)

All functions are synchronous, raise on failure, and never sleep on a fixed timer. All return plain dataclasses (JSON-serialisable via `dataclasses.asdict`).

```python
@dataclass
class WindowInfo:
    hwnd: int
    title: str
    process_name: str      # e.g. "chrome.exe"
    pid: int
    is_foreground: bool
    is_minimized: bool
    bounds: tuple[int, int, int, int]   # left, top, right, bottom (virtual-desktop px)

@dataclass
class MonitorInfo:
    index: int                     # EnumDisplayMonitors order
    name: str                      # e.g. "\\.\DISPLAY1"
    bounds: tuple[int, int, int, int]      # virtual-desktop px
    work_area: tuple[int, int, int, int]   # bounds minus taskbar/appbars
    is_primary: bool
    dpi_scale: float               # 1.0 at 100%, 1.5 at 150% (informational: coordinates are physical px)
    dpi: int                       # effective DPI

@dataclass
class DesktopOverview:
    windows: list[WindowInfo]     # all top-level, visible, non-tool windows (Explorer shell windows excluded, taskbar excluded)
    foreground_hwnd: int | None
    cursor: tuple[int, int]
    screen_size: tuple[int, int]   # size of the whole virtual desktop (all monitors), not the primary display
    captured_at: float             # time.time()
    monitors: list[MonitorInfo]
    virtual_bounds: tuple[int, int, int, int]  # left, top, right, bottom of the virtual desktop (may be negative)

def get_desktop_overview() -> DesktopOverview: ...

@dataclass
class UIElement:
    id: int                        # stable within one WindowTree snapshot; the model refers to elements by this id
    role: str                      # UIA ControlType name, e.g. "Button", "Edit", "TabItem", "ListItem", "Hyperlink", "Text"
    name: str                      # UIA Name (may be "")
    value: str | None              # UIA ValuePattern value if any (text field contents)
    center: tuple[int, int]        # screen px
    bounds: tuple[int, int, int, int]
    is_interactive: bool           # invokable/clickable/editable/selectable per UIA patterns
    is_scrollable: bool
    is_focused: bool
    shortcut: str | None           # AcceleratorKey/AccessKey if exposed
    depth: int                     # tree depth (for the model to understand grouping)
    patterns: tuple[str, ...]      # interactive elements only: subset of invoke, toggle, select (SelectionItem), expand (ExpandCollapse), value (writable Value), range (RangeValue)
    # report 58 (4 Oct): runtime_id (UIA RuntimeId), automation_id, hidden (a state control the app hides in place:
    # bounds are its label's, act on it by id), range (value, minimum, maximum)

@dataclass
class WindowTree:
    hwnd: int
    title: str
    process_name: str
    elements: list[UIElement]      # depth-first order, capped (see below)
    truncated: bool                # True if cap or timeout hit
    captured_at: float
    elapsed_ms: float
    passes: int                    # walks made (>1 when the first pass was thin and re-polled)
    child_windows: int             # descendant HWNDs walked alongside the top-level one
    first_pass_elements: int       # element count of the first walk (-1 unknown)
    status: str                    # "ok" UIA answered (however small) | "busy" on screen, process alive, no answer within ~1.5 s (returned then, elements=[], truncated) | "empty" answered with nothing usable, or not on screen and silent
    note: str                      # one sentence when status is not ok or the tree is partial, e.g. "window did not answer accessibility queries within 1.5 s (probably loading or rendering); look again shortly"

    left_out: list[dict]           # report 58: per parent, children off screen by side (above/below/left/right/hidden) and those the cap or clock stopped the walk before ("unread"), named
    hidden_controls: int

def get_window_tree(hwnd: int, *, max_elements: int = 400, timeout_s: float = 3.0) -> WindowTree: ...
def format_window_tree(tree: WindowTree, *, within: int | None = None, budget: int | None = None) -> str: ...   # compact text for the model, one line per element:  [id] role "name" value=... @(x,y) {invoke,expand} [kb: shortcut] [on|off|selected|expanded|hidden]; header carries [status: busy|empty] and a note: line when set; "(left out: ...)" lines say what was off screen, hidden or unread and how to see it; past FORMAT_BUDGET_CHARS the middle of the longest lists is folded (both ends kept); within= lists one element's part in full
def format_overview(o: DesktopOverview) -> str: ...

@dataclass
class Capture:
    png: bytes
    origin: tuple[int, int]        # top-left of the captured rectangle, virtual-desktop px
    source_size: tuple[int, int]   # screen px captured
    image_size: tuple[int, int]    # PNG size (== source_size unless scaled)
    scale: float                   # image px per screen px; 1.0 = 1:1.  screen = origin + image / scale
    target: str                    # "window <hwnd>", "monitor <i>", "region", "desktop"
    rendered_by_window: bool       # PrintWindow (True) vs desktop crop (False)

def screenshot(hwnd: int | None = None, *, region: tuple[int, int, int, int] | None = None,
               monitor: int | None = None, max_width: int = 2560) -> Capture: ...
    # at most one target; none = whole virtual desktop. Native resolution unless the longest edge exceeds
    # max_width, in which case it is scaled down and the exact scale reported. `capture` is the same function.
def system_facts() -> dict: ...                # time, uptime, cpu %, memory total/used, top processes by memory with pid/name/rss
```

Notes for the tree: walk via UIA cached subtrees from the window's element, depth-first, skipping offscreen elements, include every element that has a name/value or is interactive/scrollable. Child HWNDs are walked too: `EnumChildWindows` lists the visible descendants, each gets its own `ElementFromHandle` (its own WM_GETOBJECT, which is what makes Chromium/Electron/CEF build their lazily built tree), and its subtree is spliced in after the element that owns it, de-duplicated by RuntimeId (else role+name+bounds); ids are positions within the returned pass. If the first pass is thin, the walk is re-polled with a deadline until the count has grown and stopped growing, or has not grown within a short grace (longer for a process launched moments ago) — never a fixed sleep. No special-casing by app or class name; report `truncated`/element counts/`passes` honestly. Walk in a worker thread with COM initialised (`comtypes.CoInitializeEx` / `pythoncom`) and enforce `timeout_s`.

### Perception: the screen model (2026-10-04, report 58)

Built from the 3-4 Oct failures (the Spotify slider never surfacing, the Lyrics toggle clicked six times, "did not scroll" on YouTube, truncated chats, stale ids). Tested offline on fake UIA trees shaped like those reads; the live checks are in report 58 §5.

- **The reader keeps what matters.** A RangeValue or Toggle element that the app hides in place (off-screen with no box, or a 1-2 px box inside the visible area: a web page's visually hidden `<input type=range>` inside its label) is kept, marked `hidden`, with its label's box. The whole-subtree filter is `control view AND (not off-screen OR RangeValue OR Toggle)`. A scrolled-away row (a full-size box outside the view) is still dropped. Every element carries its RuntimeId and AutomationId.
- **No silent truncation.** The thinker's cap is 1,000 elements (`tools.TREE_ELEMENTS`). A page leaves at least 1/8 of the cap for what follows it. The walk records, per parent, the children it saw off screen by side, and those it had fetched when the cap or the clock stopped it (`WindowTree.left_out`). The formatter prints those as `(left out: ...)` lines. It also uses the app's own numbering ("items 41-55 of 60 are listed; 40 before them ..."). Past `FORMAT_BUDGET_CHARS` (22k) it folds the middle of the longest lists, keeping both ends and never a focused or selected item. `look_at_window` with `within=<id>` walks that element's subtree only, found again by its RuntimeId (`get_window_tree(part=...)`), so the 1,500-element cap goes to the part; a part that went away is said so; a provider without RuntimeIds gets the whole window at 1,500 with only the part listed. A part read never replaces the window's latest observation. The reader says why a read stopped (`WindowTree.stopped_by`: `cap`, `clock`, `depth`, `failed`); only `clock` counts as cut short.
- **Ids hold across every read** (`yuki.perception.model`). The dispatcher carries each window's ids into every read, explicit or automatic:
  - by RuntimeId (with the role unchanged);
  - else by AutomationId;
  - else by structure (place, then a unique name). A control the app re-mounted keeps its id by place.
  - A new element never reuses an id.
  - A gone id is reported by name and last read ("[32] (Button "Lyrics", seen in read #27) is not in window ... now").
  - An action by id goes to the element where it is now, from the model's latest observation.
  - `patterns.element_action` finds a read element again by its RuntimeId below the renderer pane when the point climb fails. On Chromium/CEF, `ElementFromPoint` answers with the whole pane, so Invoke and Toggle used to fall back to the mouse.
- **What an action changed** (`yuki.perception.outcome`, `Dispatcher._bracketed`). A click, key or scroll on a window is bracketed by two reads of it. The before read is the latest one if the window's events show no change, else a fresh read. The after read comes once the events go quiet (first event ≤ 0.6 s, quiet 0.25 s, cap 1.5 s). The diff, by carried id, gives:
  - the target's state, value or name ("Button "Lyrics" turned on", "went from 62% to 47%");
  - what appeared or went, grouped by container;
  - movement inside a scrolling container ("moved up by about 600 px: 8 came into view");
  - title and page.
  Elements that were already changing (seen in events before the action, or differing between the thinker's recent read and the fresh before read) are left out. A read-only element's text or value, or a progress value, changing on its own is never proof: if that is all that changed the verdict is unverifiable. When either read stopped at its cap, nothing past the last element both reads share counts as appeared or gone, and "nothing changed" is unverifiable. Changed elements are named as they were. A toggle click reads its state from the same two reads, so what it opened or closed is in the account too. A click by id lands where the element is in the read just before it. The result gets "What changed: ...". `details["verified"]` and `details["screen_change"]` carry the verdict, and `loop.action_verdict` gives its reason:
  - something changed (or a new window, focus or page): verified;
  - nothing changed in a window whose content is exposed: "nothing visible changed in the window";
  - an opaque window or a failed read: unverifiable.
  A scroll whose content moved is a scroll that worked, whatever the single point said. A window-sized surface under the point is never the scroll marker. On the real desktop this is on for the thinker's dispatchers and off for the voice's quick hands (`Dispatcher(verify_outcomes=...)`). The input facts now read "same page: ..." and "focus still on ...", never "unchanged".
- **Events** (`yuki.perception.events`):
  - Windows Yuki reads or acts on are subscribed to UIA structure-changed, property-changed (states, values, names, focus, off-screen, scroll percent) and menu events (at most 4 windows), on one thread. Handlers only count. Only dispatchers that check outcomes subscribe (`use_events` follows `verify_outcomes`: the voice's hands never do).
  - A window not touched for 30 s is unsubscribed. With nothing in play the thread sleeps on a condition.
  - `generation(hwnd)` says whether a read is current. `wait_quiet` replaces the view-after-actions' re-reads. `busy_since` names elements that change on their own.
  - A read stays current by events for at most `REUSE_MAX_S` (2 s): the view after actions reuses the after-action read within it; the read before an action is fresh past it.
  - Without events (stubs, a failed subscription) reads stand in. A re-read is never shorter than 1.5 s, and one cut short by the clock never replaces a whole read.
- **Chromium's accessibility**: every window read (and its content windows) is told once per 10 min that an accessibility client is reading it: `WM_GETOBJECT` for Chromium's probe object id 1 (`_announce_accessibility_client`), the documented way Chromium, Electron and CEF detect assistive technology and switch to complete mode. A content window that a thin read found empty is also asked once as an MSAA client does (`AccessibleObjectFromWindow(OBJID_CLIENT)`). The complete mode is the app's own cost; it is only asked for windows Yuki reads.

### Perception: eyes without pixels (2026-09-29, Phase 2)

The user's decision (29 Sept): **no screenshots, no OCR, no vision** - the tree path, widened to everything on screen. What changed:

- **Opaque windows are measured and said plainly.** `WindowTree.coverage` is `content_coverage(elements, viewport)`: the share of the visible window covered by leaf elements, the window's own element and window-sized containers left out (the older leaf coverage counted a one-element tree - Warp - as 100%). `WindowTree.opaque` is set when a settled, answered, not-young read covers less than `OPAQUE_COVERAGE` (5%): the note then says the window draws its own interface, nothing in it can be read or aimed at, use the app's keyboard shortcuts and check the title. Measured on this desktop: four Warp windows 0%, a Chrome page 36-95%, Task Manager 100%. A window measured opaque is not re-polled for a lazy tree for 10 min (`measured_opaque`): a Warp read went from ~0.5-1.1 s to ~15-70 ms.
- **Unnamed controls are labelled, near-duplicates told apart** (`format_window_tree`): an interactive leaf with no name gets `(described "Like")` from its UIA HelpText / FullDescription (new optional cached properties, `UIElement.description`), else `(no name, right of "Title")` from the nearest named element of its group by geometry, else `(no name, in "Toolbar")`; set items (ListItem, DataItem, TreeItem, TabItem, MenuItem, RadioButton) carry `#k/n` (the provider's PositionInSet/SizeOfSet when it gives one, `UIElement.position`); names that would print alike are written out in full (240 chars) with the named group each sits in when those differ. Structure and rectangles only. Measured: tree text +4% on this desktop's windows (Chrome +4.5%).
- **"This"**: `focus_snapshot()` -> `FocusSnapshot` (role, name, value, window, selection, 200 chars either side of the caret or selection; a password field is named, never read) from one long-lived focus reader thread with its own UIA client (p50 ~7-12 ms, p90 ~30 ms; the old per-call `focused_element` paid 5-40 ms of setup, 0.6-2 s cold). `focused_element` now says *why* it has no answer (`FocusInfo.error`: a timeout vs the UIA error), never the same as "nothing has focus".
- **What is on screen** (`visibility.layout()`, 2-3 ms standalone, ~9 ms p50 inside the busy service): every visible, uncloaked, unminimised top-level window in Z-order (extended frame bounds), classified `window` (a user window) or `popup` (menus, toasts, flyouts, untitled dialogs; click-through overlays and the shell's surfaces cover nothing), each with its visible share on a 16 px grid of the monitors; `Layout.covered(hwnd, rect)` says whether a rectangle of a background window lies wholly under windows above it.
- **The clipboard**: `clipboard.read_clipboard()` (<1 ms): text (clipped at 20k), formats, owner process; content marked private by the copying app (`ExcludeClipboardContentFromMonitorProcessing`, `Clipboard Viewer Ignore`, `CanIncludeInClipboardHistory` = 0) is never read. `actions.clipboard.write_clipboard(text)` replaces it and reads it back.

### Perception: Now (2026-09-29)

The memory service keeps, in RAM, the latest privacy-filtered, secret-redacted read of every visible window, popup and toast (`yuki.memory.now.NowBoard`, fed by the watcher; `docs/MEMORY.md`, "Watcher (capture)") and serves it over a local pipe (`NowServer` / `NowClient`, the `embed_ipc` shape: user-only DACL, remote clients refused, a key derived from memory's DPAPI key proven both ways). The brain's dispatcher appends it to the per-turn overview (`Dispatcher.include_now`, turned on by `enable_now()` from `Settings.now_in_situation`; the brain's prewarm does it), so the situation block now carries: the window in front with up to 1,500 characters of what it shows, every other visible window with a ~240-character gist and its visible share, popups and toasts that appeared lately, a live one-line focus / selection fact, the age of every read and whether the window changed since (a stale read of the window in front asks the service to read it again). Each part waits at most ~50 ms (`now_wait_ms`, `focus_wait_ms`) and is left out when it does not answer. An opaque window's line says so and gives the app's own data when a reader exists (`APP_DATA_READERS`: Warp's saved tabs, titles and folders from its own database, read-only) plus the titles its windows had lately (memory's timeline). Measured 2026-09-29 against a temp-DB service: pipe answer p50 1.6-4.5 ms, 98.7% within 50 ms; the per-turn overview with Now p50 ~32 ms; ~0.8-1.2k characters on this desktop. API for the voice's desk block: `shared_client().now(wait_s)` -> dict, `format_now(board, foreground=..., front_chars=..., other_chars=...)`, `MemoryClient.now()` / `.now_text()`.

## Actions contract (module `yuki.actions`)

Each returns an `ActionResult`:

```python
@dataclass
class ActionResult:
    ok: bool
    summary: str            # one line for the model
    details: dict           # anything structured (stdout, hwnd, etc.)
    elapsed_ms: float
```

```python
def launch_app(query: str, *, args: list[str] | None = None, timeout_s: float = 8.0) -> ActionResult
    # Resolve via Get-StartApps (AppsFolder). Return candidates when ambiguous instead of guessing:
    # details = {"launched": bool, "hwnd": int|None, "candidates": [{"name","appid"}]}.
    # Waits until a new window from the launched process appears (or an existing window of it changes title), or timeout.
    # With args: shell:AppsFolder cannot pass arguments, so a packaged app (AUMID "Family!App") is activated with its
    # arguments through IApplicationActivationManager (keeps package identity, so the app's own profile/data), and a
    # desktop app is started with Start-Process -FilePath <exe> -ArgumentList <args>, the exe coming from its Start-menu
    # .lnk target, a known-folder/absolute AppID path, or the image of a running window with that AppUserModelID.
    # No way to pass arguments -> ok=False, nothing started, reason in the summary.
def focus_window(hwnd: int, *, timeout_s: float = 2.0) -> ActionResult   # restore if minimized, SetForegroundWindow, verify foreground
def click(x: int, y: int, *, button: str = "left", clicks: int = 1) -> ActionResult   # instant move, no glide
    # summary + details report the outcome: foreground window after the click (foreground_hwnd/title/process,
    # foreground_changed), focused_control_hwnd, focus_role/focus_name/focus_value/focus_accepts_text, focus_changed.
def type_text(text: str, *, press_enter: bool = False, clear: bool = False) -> ActionResult  # types into current focus
    # details carry foreground_hwnd, focused_control_hwnd, focus_role/name/accepts_text; failures name the focus in the summary.
    # Path: paste for non-ASCII or >50 chars, and for >=12 chars when the focused control is a writable Value field or the window
    #   took >=100 ms to become ready; keystrokes otherwise. details["method"]/["method_reason"] say which and why.
    # clear: select-all + delete first, refused if the focused control does not take text, verified empty when it has a Value pattern.
    # Read-back: after sending (before Enter) the focused control is read via UIA (ValuePattern, else TextPattern before the caret);
    #   summary "field now contains '...'"; if the typed text is not there: ok=False, Enter NOT pressed, summary says what it holds.
    #   Unreadable -> "field contents not readable (...)" and it proceeds. Condition poll: ends when the text shows or the
    #   contents stop changing for 0.35 s, cap 2 s.
    # type_text/hotkey/press: details["new_windows"] lists top-level windows of the target process that showed during the action
    #   (before/after snapshot); the summary names them with class and bounds.
def hotkey(*keys: str) -> ActionResult                                  # e.g. ("ctrl","t"), ("win","r"), ("volume_mute",)
def press(key: str, *, times: int = 1) -> ActionResult
    # hotkey/press: with expect_hwnd, wait_for_input_ready first (~1 s, fail naming the focus); after sending, watch focus
    # for up to ~400 ms (condition poll) and report it like click: "pressed ctrl+l; focus now Edit '...' (takes text)".
    # type_text records the focused control before the first character and names it on success: "typed N chars ... into <control>".
def scroll(x: int, y: int, *, dy: int = 0, dx: int = 0) -> ActionResult  # wheel notches at a point
def run_powershell(command: str, *, timeout_s: float = 20.0) -> ActionResult
    # Pass the script via -EncodedCommand (UTF-16LE base64) or stdin; NEVER split on whitespace. Capture stdout/stderr/exit code (UTF-8).
def open_url(url: str, *, app: str | None = None) -> ActionResult     # default handler via os.startfile / Start-Process; with app: launch_app(app, args=[url])
    # A web address returns as soon as a window of the app shows a page at it (no content wait since 2026-09-25);
    # launch_app's details carry timings_ms per stage (start_menu, plan, identity, start, page).
def window_action(hwnd: int, action: str) -> ActionResult  # focus|minimize|maximize|fullscreen|restore|close, the new state verified
def write_clipboard(text: str) -> ActionResult  # yuki.actions.clipboard: replaces the clipboard, read back (2026-09-29)
```

## Agent contract (module `yuki.agent`)

- `Agent.run(request: str) -> Iterator[AgentEvent]`. Events: `thinking(text)`, `tool_call(name, input)`, `tool_result(name, ok, summary)`, `ask_user(question)`, `final(text)`, `error(text)`. When `ask_user` is yielded the generator pauses; the caller sends the answer with `agent.answer(text)` and continues iterating. State (messages, running summary) survives the pause.
- Messages are append-only. Tool results from perception that are older than the last 2 turns are replaced in-place with a one-line stub like `[desktop overview from step 3 — superseded]` before the next request (this is context hygiene, not behavior steering).
- One model call may return several `tool_use` blocks; execute in order, stop at the first failure, return all results in one user message.
- Every action tool takes an optional `finishes_with` (2026-09-25): the model's own mark that this call completes the request, with the reply. When every call of the response succeeded, the request ends with it at once (the same as `done` sent alongside, which the model rarely does); a failure discards it. Since v3.14 (2026-09-29) it ends the request only when the action's own report verifies the effect (`loop.action_verdict`: a tab's selected state, a scroll position, a volume read back, a page at its address, a field read back, a window's state read from Windows, for keys and clicks the input layer's focus/window/page watch); otherwise the brain gets the report, the window after it, and one more call. **Changed by the revamp (`yuki/agent/finish.py`):** `finishes_with` is a boolean (`true`), the finish line is written by code from the tool's own report ("Chrome is minimized."), `finish_words` carries the model's words only when the report cannot name the outcome (an old string still works), and verdicts are verified / expected-unverifiable / contradicted (see "5. Verify").
- `Agent.run(request, effort=..., thinking=..., escalate=(effort, thinking))` (v3.14): effort and thinking are per step. A spoken request starts fast (`voice_brain_effort` low, thinking off) and moves to `escalate` (medium, adaptive) for the rest of that request on a measured signal only: an action failed or its report does not verify its effect, the loop breaker tripped, the user was asked, a finish or a claim had no evidence, or a tool call came back written as text. Typed requests are unchanged. On Sonnet 5 every effort is its own cached prefix (measured), so the switch happens at most once per request and `Agent.warm_cache(effort=, thinking=)` warms the voice profile's entry. **Changed by the revamp:** the actor's profile no longer moves inside a request; `escalate=` is accepted and logged as `effort_kept`, and the same signals trigger a consult (a side call, see "5. Verify"). The old escalation runs only with `Settings.consult = False`.
- A state claim needs evidence (v3.14): `done` carries `about` (the hwnds whose present state the message asserts); when this request has neither read that window (a look, the request view, the view after actions) nor acted on it with a verified result, or a `done` rides with an action whose report does not verify its effect, the message is sent back once ("look first") and the request escalates. Decided from the request's calls and their verdicts, never from the words.
- A question never holds its lane (v3.14b, `ui/runtime.py`): a request paused on `ask_user` is set aside (`Agent.cancel(PARKED)`, outcome `parked`, signal `parked`) when a new request waits on that lane or after `Settings.ask_park_s` (120 s; the six answered questions in the live logs of 27-29 Sept took 3-43 s, the one never answered held the worker until exit). An answer later carries it on as a new request on the same lane with the question and the answer (`AgentRuntime.answer`, signal `resumed(old, new)`: the chat, the overlay card and the voice job keep one item). Its steps so far are in the agent's own context.
- Keys report the window's state (v3.14b, `actions/input.py`): `hotkey` / `press` details carry `window_state_before` / `window_state_after` / `window_state_changed` (normal, maximized, fullscreen, minimized, measured from the window rect against its monitor and its styles, `perception.windows.window_state`), and `window_action` the same pair; `loop.action_verdict` counts a state change as the key's verified effect, so an F11 is verified in code.
- Console lines are built with every outside text escaped (`log/events.py` `_x`, rich's `escape`), and a line that still does not parse is printed plain (v3.14b: a stray "[/cyan]" in the model's reasoning raised rich's MarkupError out of the agent loop).
- A loop-breaker trip (v3.14) is also written into the exchange's action lines ("loop breaker: <reason> (<tool>, N times)"), so memory's conversation extractor can learn what does not work on this PC.
- Step lines (v3.14): every action and slower read takes an optional first field `say_now`, the few words Yuki's voice says as that step starts (streamed out as `Partial("step")` by `stream_partial`); calls are compared without it (`loop.acting_input`). Read-only tools (`PERCEPTION_TOOLS`) and the settle poll of the view after actions stop being waited for when the user cancels or adds something (`Agent._race`); actions always finish.
- Tools exposed to the model (names are final): `look_at_desktop`, `look_at_window(hwnd)`, `take_screenshot(hwnd?, region?, monitor?)` (omitted from the tool list entirely when `screenshot_policy` is `never`; the dispatcher still refuses it as a backstop), `system_facts`, `launch_app(query, args?)`, `focus_window(hwnd)`, `click(x,y,button,clicks)`, `type_text(text,press_enter,clear?)`, `hotkey(keys[])`, `press(key,times)`, `scroll(x,y,dy,dx)`, `run_powershell(command)`, `open_url(url, app?)`, `ask_user(question)`, `note_to_self(text)` (updates the running summary the model sees each turn), `done(message, about)` (`about`: the hwnds whose state the message asserts, v3.14).
- **Revamp:** the seven memory reads below are one tool, `memory(kind, ...)` (the old names still alias to it), and `about(name, prefer_route?, words?)` joins the memory tools (no model call; not given to the read-only helpers because it can write a preference). Every action tool carries an optional `playbook` field; `risk` and `effect` only where the tool can destroy or reach; `say_now` only on spoken requests.
- Memory tools (`yuki/agent/memory.py`, backed by `yuki.memory.api.MemoryClient`): `recall`, `activity`, `calls` (calls, meetings and game voice chats in a window, a restart-split session merged; each with the user's and the others' speech and the apps whose own sound was kept) and `call_timeline` (one call's timeline digest written by `yuki.memory.calldigest`, or with `words` a page of the exact words of every side), plus the memory writes (`remember_how`, `correct_memory`, `remember_rule`, `forget_rule`, `todo`, `answer_question`, `watch_for`). The read ones are also the read-only helpers' (`spawn_helpers`). Questions about a call or meeting are answered from `calls` + `call_timeline`, never from recall alone. Since 3 Oct 2026 a call keeps both sides (the user's microphone and the call app's own sound; a game's voice chat is its own session), and every line the tools show says who spoke: unmarked / `[user]` (with `mic open to` the apps taking the microphone, in the exact words) is the user, `[others on <app>]` that app's sound (maybe several people), `[game on <app>]` a game's teammates and voice lines. Each call result carries `CALL_SIDE_NOTE` (never give someone else's words as the user's; name a speaker only on evidence; where only the user's side was kept - every call before 3 Oct - never guess the others') and, when a game is in it, `GAME_SIDE_NOTE` (a reply in a game may be to teammates or to the friends on a call: time, words, `mic open to`). The MCP's `get_calls` / `get_call_timeline` print the same text (`get_call_timeline` also takes `who` all | user | others for the words).
- Every turn the model automatically gets a fresh `look_at_desktop` result appended (cheap), never a tree or screenshot unless it asks. Since 2026-09-29 the brain's dispatcher appends the Now picture to it (see "Perception: Now"): what the memory service read of every visible window, popups and toasts, and the focus line, each bounded at ~50 ms.
- Tools added 2026-09-29 (Phase 2): `look_at_focus()` (focus, selection, caret text; a timeout is reported as such), `read_clipboard()` (text, formats, owner; private content never read; the text is not written to the tool-result log, only sent to the model), `write_clipboard(text)` and `window_action(hwnd, action)` (minimize, maximize, fullscreen, restore, focus, close; the state reached, verified; both are actions with the risk field). Focus and clipboard text is fenced as untrusted data (`untrusted.EXTERNAL_TEXT_TOOLS`).
- **Raw x,y clicks** get the element click's staleness rule (`Dispatcher._check_raw_point`): refused, nothing sent, when the point is in a window measured opaque, in a user window never read in this conversation, or in one that changed since its latest read (moved or resized, retitled, or Yuki acted on it since); `read` must be that window's latest read when given. The shell's own surfaces and backends that cannot say which window is at a point are not checked. `guard_raw_clicks` is on for every dispatcher (the voice's hands' too: a raw click there needs a read through that dispatcher).
- System prompt: short, behavioral. Describe who Yuki is, what it can see, how to prefer fast paths (shell/shortcuts > tree clicks; the prompt does not mention screenshots), when to ask the user (ambiguity, risk, or genuinely stuck — not as a first move), and to confirm before irreversible actions. Do not include keyword-based rule lists.
- System prompt layers (`prompt.system_blocks`, 2026-09-25), most stable first, each its own cached block: (a) the frozen core (+ a lane's `extra_instructions`), (b) the profile: the user's names and the portrait's Personality section, (c) the user's standing rules verbatim, framed as their own instructions that override defaults in every reply, with a self-check before replying and "list every one" when asked. Breakpoints: core (covers the tools, which carry none), profile, rules, and the moving conversation breakpoint: four. (Since the revamp the core is ~13k characters, the profile holds the personal brief and the people directory, and the volatile block starts with the task frame; see "What a request carries".) Volatile context (desktop, memory block with the rest of the portrait, to-dos, know-how, resume, and the open commitments right before the user's words) stays in `messages`. Measured: after a rule change the next request reads tools + core + profile from cache (10.9k tokens) and rewrites only the rules layer onward.

## Logging contract (module `yuki.log`)

JSONL file `logs/sessions/<YYYYMMDD-HHMMSS>.jsonl`, one JSON object per line: `{"ts": float, "session": str, "turn": int, "type": <event type>, ...}`. Event types: `user_message`, `llm_request` (model, system, messages, tools), `llm_response` (content blocks, stop_reason, usage, latency_ms), `tool_call`, `tool_result` (full result, elapsed_ms), `perception` (kind, size_chars, elapsed_ms, payload), `ask_user`, `user_answer`, `context_edit` (what was stubbed), `error` (with traceback). Base64 screenshot payloads are written to `logs/sessions/<id>/shot-<n>.png` and referenced by path, not inlined. Console gets a coloured, one-line-per-event pretty view.
