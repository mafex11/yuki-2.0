# OpenClaw harness features — complete catalogue, mapped to Yuki

26 Sept 2026. Read-only research. Sources: the official OpenClaw repo cloned
`--depth 1` to a scratch dir and its `docs/` tree (concepts, gateway, tools,
automation, channels, security, cli, nodes, web, reference, start — several
hundred files, read directly, not summarized from search results), plus
Yuki's own `docs/ARCHITECTURE.md`, `docs/VOICE.md`, `docs/UI.md`, and
`yuki/agent/{loop,context,tools,prompt,memory}.py`, `yuki/ui/runtime.py`,
`yuki/config.py`, `yuki/memory/*`. Memory, personality-evolution and
proactivity were already researched in depth in
`docs/research/openclaw-memory.md` (26 Sept 2026) — this document does not
redo that comparison, only condenses it and slots it into the fuller
catalogue below. No code was changed; this is the only file written.

**How to read the tables.** *Status*: **HAVE** (Yuki already does this, ~as
well or better), **PARTIAL** (some of it, gap noted), **MISSING** (not
present, worth considering), **N/A** (does not apply to a single-user
Windows desktop agent, reason given). *Effort*: S (hours–a day), M (a few
days), L (a real project). All recommendations respect Yuki's rules: no
keyword/regex behavior heuristics, lightweight on the PC, memory only ever
changed by an audited pipeline, never hand-edited.

---

## 1. Agent loop

| Feature | What it does (OpenClaw) | Lives at | Yuki status | Recommendation |
|---|---|---|---|---|
| Turn structure | `runEmbeddedAgent` resolves model/auth, serializes via queues, streams events, enforces a timeout per run | `docs/concepts/agent-loop.md` | **HAVE** — `Agent._drive()` (`yuki/agent/loop.py:924`), `max_steps` cap, per-request accounting | keep |
| Streaming tool dispatch | Tool calls run as soon as a block is complete during streaming | `docs/concepts/agent-loop.md`, `streaming.md` | **HAVE, arguably ahead** — Yuki dispatches action/perception tools mid-stream (`_can_run_early`, `loop.py:1244`), something OpenClaw's docs describe mainly for chat-preview streaming, not tool execution | keep |
| Parallel tool calls | Not really a feature — OpenClaw runs one call per turn like Yuki | — | **N/A** | — |
| Retries (transient failures) | Same-model recovery: rate limits ≤10 attempts, other transient errors 8 retries/90s window, exponential backoff capped at 30s, cooldowns scale 30s→1min→5min | `docs/concepts/retry.md` | **MISSING** — Yuki's model call has no retry/backoff of its own; only the voice mouth path (Pipecat) retries once on timeout | **adopt (S)** — wrap `Agent.client.messages.create` with one deterministic retry (exponential, capped) on `APIConnectionError`/5xx/429 only — plumbing, not behavior |
| Model failover / fallback chains | Two-stage: rotate auth profile, then fall back to next model in a configured chain | `docs/concepts/model-failover.md` | **MISSING** — one model id, no fallback; a Bedrock region/model outage currently just fails the request | **adopt (S–M)** — add one configured fallback model (e.g. Opus 5 or a second region) tried once after N consecutive `APIConnectionError`/5xx on the primary; log it, never silent for cost-relevant switches |
| Thinking levels / effort | `off|minimal|low|...|adaptive|max`, resolved through several override layers | `docs/tools/thinking.md` | **HAVE** — `Settings.effort`, `resolve_effort`, per-request `run(effort=...)`, voice's `voice_brain_effort="low"` | keep |
| Per-task model choice | Agents/runs can pin a specific model | `docs/concepts/model-providers.md` | **HAVE** — `run(model=...)`; voice mouth (Haiku) vs brain (Sonnet) already exploits this | keep |
| Cost control | Per-provider cost tables, tiered pricing, `/usage cost` | `docs/reference/api-usage-costs.md`, `token-use.md` | **PARTIAL** — Yuki estimates and logs cost per request (`_account_response`, `requests.csv`) but enforces no budget/cap | **adopt (S)** — a daily spend counter with a soft alert (tray note), no throttling; plumbing only |
| Timeouts | Per-run timeout (48h default), per-tool timeout | `docs/concepts/agent-loop.md` | **HAVE** — `tool_timeout_s`, `max_steps` gives up after N rounds | keep |
| Cancellation | Cancel a run; descendants cascade | `docs/concepts/subagent-yield-handoff.md` | **HAVE** — `Agent.cancel()`, checked at 3 points (`loop.py:543`) | keep |
| Steering mid-run | `/steer` adds guidance at the next steering boundary without resetting context | `docs/tools/steer.md` | **HAVE, same shape** — `Agent.steer()` (`loop.py:512`), drained at step boundaries, held-final logic when it arrives right as `done` is about to fire | keep |
| Queue modes for busy runs | Broadcast/queue policies per channel; a session can be busy while new messages arrive | `docs/concepts/queue.md`, `queue-steering.md` | **PARTIAL** — Yuki's front-desk lane (`yuki/ui/runtime.py`) answers while worker is busy and hands off actionable requests to a FIFO queue; no configurable queue *modes* (interrupt/merge/parallel), just the one fixed shape | keep as designed — a second mode is not obviously worth the complexity for one user |
| Loop detection | Hashes stable tool-call outcomes; blocks a batch after a repeat, ends the run after a second | `docs/tools/loop-detection.md` | **MISSING** — the system prompt asks the model to notice repetition (`prompt.py` "read the error before retrying") but nothing enforces it | **adopt (S)** — count identical `(tool, normalized_input)` calls per request; after N (e.g. 3) force an `ask_user` instead of a 4th identical call. Deterministic plumbing, not a behavior heuristic — it counts, doesn't decide *what* to do differently |

## 2. Context management

| Feature | What it does | Lives at | Yuki status | Recommendation |
|---|---|---|---|---|
| System prompt layering | Fixed sections, cache boundary between stable prefix and volatile suffix | `docs/concepts/system-prompt.md` | **HAVE, closely matched** — core/profile/rules layers, ≤4 cache breakpoints (`prompt.system_blocks`, `context.py`) | keep |
| Bootstrap/workspace file injection with size caps | `AGENTS.md`/`SOUL.md`/etc auto-injected, per-file 20k char cap, 60k total | `docs/concepts/agent-workspace.md` | **N/A-ish** — Yuki has no workspace-file convention (identity/rules live in the DB, not files); the equivalent budgets already exist (`USER.md`-style 1,200/4,000-char standing/resume budgets, portrait ~1,500 tokens) | see personality section below for the one real gap (no agent-owned voice file) |
| Session transcripts | Durable, queryable transcript per session | `docs/reference/session-management-compaction/*` | **HAVE** — JSONL per session (`SessionLogger`), full request/response bodies (richer than OpenClaw's metadata-first transcripts) | keep |
| Compaction (how/when) | Summarizes old turns on overflow or usage threshold, keeps tool-call/result pairs together | `docs/concepts/compaction.md` | **HAVE, arguably ahead** — `ContextManager.compact()` collapses finished requests to one line each, cache-aware (rewrites only what's already past the previous breakpoint), no model call needed at all (OpenClaw's default is model-summarized) | keep |
| Memory flush before compaction | A silent turn writes unwritten context to a daily note before compaction discards detail | `docs/concepts/memory.md` "Automatic memory flush" | **HAVE via a different, arguably stronger mechanism** — Yuki's conversation/journal workers ingest every turn in the background regardless of compaction timing (`memory.log_turn`), not gated on a compaction event | keep |
| Context pruning of old tool results | TTL-based clearing of old tool-use blocks | `docs/concepts/session-pruning.md` | **HAVE, arguably ahead** — supersession-based stubbing (`context.py` module doc): a perception result is stubbed only once something newer of the same kind exists, never by a blind TTL, and cache-aware | keep |
| Caching | Ephemeral/long-TTL prompt caching per provider | `docs/reference/prompt-caching.md` | **HAVE** — same idea, tuned to Bedrock/Sonnet's 1024-token minimum, measured (`ARCHITECTURE.md` "Measured: ... 10.9k tokens") | keep |

## 3. Memory

Full catalogue already in `docs/research/openclaw-memory.md`. One-line status per tier, condensed:

| Tier/mechanism | Yuki status |
|---|---|
| Curated core / profile file (`MEMORY.md`+`USER.md` vs portrait) | **HAVE, arguably ahead** — bi-temporal, quote-verified, audited |
| Episodic tier + write path | **HAVE** — journal, conversation memory, episodes |
| Dreaming/consolidation | **HAVE, different shape** — nightly portrait worker + audit pass, gated by evidence citation, not a scored promotion pipeline |
| Corrections/supersession | **HAVE** — `correct_memory`, portrait ADD/UPDATE/INVALIDATE/NOOP |
| Provenance/taint (owner/agent/untrusted/system) | **PARTIAL** — has `by_yuki`, `nature`, secret filter; no closed-set taint that propagates through a turn |
| Zero-model-call trigger injection (Lane 1) | **MISSING** — this is OpenClaw's single biggest edge; see top-15 §1 |
| Escalation lane for hard recall (Lane 2) | **MISSING** — see top-15 §3 |
| Retention/forgetting | **PARTIAL** — no `memory forget --session` equivalent; portrait audit invalidates but doesn't purge |
| Per-user vs global | **N/A** — single user |

## 4. Personality and identity

| Feature | What it does | Lives at | Yuki status | Recommendation |
|---|---|---|---|---|
| `SOUL.md` (agent's own voice/persona, self-edited) | Model rewrites its own persona file any turn it judges its voice should change, socially gated only ("tell the user") | `docs/reference/templates/SOUL.md`, `docs/concepts/soul.md` | **MISSING** — Yuki's Personality section describes the *user*, not Yuki; nothing describes or evolves Yuki's own voice | **adopt, gated (M–L)** — see prior report §3a: a nightly worker proposes evidence-cited deltas to a small "how Yuki talks" block, validated like the portrait audit, never a live raw self-edit |
| `IDENTITY.md` (name/avatar/vibe, first-run ritual) | Filled in once at bootstrap | `docs/concepts/agent-workspace.md` | **N/A** — Yuki already has a fixed name/identity; no bootstrap ritual needed |
| `USER.md` (agent-curated user profile) | Dated directives, single-active-version | `docs/concepts/user-model.md` | **HAVE, ahead** — portrait + conversation memory, DB-enforced bi-temporal |
| `AGENTS.md` conventions | Operating instructions/standing orders a human edits directly | `docs/concepts/agent-workspace.md` | **HAVE, different form** — `ARCHITECTURE.md` plays this role (human-edited contract); standing rules play the "instructions" role, machine-enforced rather than file-read |

## 5. Proactivity

| Feature | What it does | Lives at | Yuki status | Recommendation |
|---|---|---|---|---|
| Heartbeat (system-owned periodic check) | Runs a narrow, scratch-driven turn every 30–60 min, `NO_REPLY` default, cost-controlled (isolated session, light context, quiet hours) | `docs/gateway/heartbeat.md` | **PARTIAL** — coach (`yuki/memory/nudges.py`) is richer (screen-aware) but ungrounded (diagnosed bug, see memory report §1a); no isolated/light-context cost discipline since it shares the main memory pipeline already | adopt OpenClaw's **grounding discipline**, not its architecture (memory report §1a, top-15 §2) |
| Cron / scheduled jobs | User- or agent-created recurring automations, own schedule/model/channel | `docs/automation/cron-jobs.md` | **MISSING** — Yuki has reminders (memory-derived) but no general-purpose "run this agent turn on a schedule" primitive | low priority (M if wanted) — a single-user desktop assistant has less need for this than a multi-channel bot; the todo/reminder system already covers "remind me at X" |
| Standing orders (authorization text + automation) | "You own the weekly report..." pattern, Execute→Verify→Report loop | `docs/automation/standing-orders.md` | **PARTIAL** — standing rules capture the authorization half; no Execute→Verify→Report loop enforcement | low priority |
| Standing intents (event-conditioned reminders) | Fires on keyword match in a future turn, not clock time; deterministic FTS prefilter, cooldown/budget/expiry | `docs/concepts/standing-intents.md` | **MISSING** — see memory report §3b: adopt the *shape* (cooldown/budget/expiry, explicit-cancel-only) but keep matching model-judged, not keyword, per Yuki's rules | top-15 §14 |
| Quiet hours / anti-spam budgets | `activeHours`, no-route gating, per-channel visibility | `docs/gateway/heartbeat.md` | **HAVE** — tray "Quiet for 1 hour"/"until tomorrow", coach budget_min/min_gap_s, voice narrator gate cooldown/settle/stale | keep; top-15 §15 suggests unifying the two anti-spam frameworks |
| Retracted "inferred commitments" | OpenClaw shipped and killed auto-extracting follow-ups from conversation | `docs/automation/index.md` | Directly matches a diagnosed Yuki bug (call-derived reminders) | **fix (S)** — memory report §1c, top-15 §7 |

## 6. Tools and skills

| Feature | What it does | Lives at | Yuki status | Recommendation |
|---|---|---|---|---|
| Tool registry + policy filter chain | Tools filtered through profile→allow/deny→sandbox→sub-agent policy before the model ever sees them | `docs/tools/index.md` | **PARTIAL** — Yuki has a static per-instance `tool_names` allowlist (worker vs front-desk) and one policy-gated tool (`screenshot_policy`), but no general multi-layer policy system | N/A to build further — two lanes is the right size for one user; a general policy engine would be over-engineering |
| Skills system (SKILL.md, lazy-loaded, gated by env/os/binaries) | A folder with frontmatter; only name+description sit in the prompt; full body loaded on reference | `docs/tools/skills.md`, `creating-skills.md` | **MISSING** — Yuki has `remember_how` (procedural know-how in memory, injected in the memory block) but no discoverable, lazily-loaded, versioned skill-file convention | **consider (M)** — `remember_how` already gets most of the value (know-how attached only when relevant); a real skills directory would mainly help if Yuki grows *many* long procedures that shouldn't all sit in the memory block. Not urgent; know-how's memory-driven relevance beats a static file list for a single user |
| Exec/shell tool + approval flow | `exec` with `mode: deny/allowlist/ask/auto/full`, executable-identity-bound approvals, YOLO mode | `docs/tools/exec.md`, `exec-approvals.md` | **PARTIAL** — `run_powershell` exists with no allowlist or approval gate; safety is entirely prompt-level ("confirm before irreversible actions") | **adopt selectively (S–M)** — not full policy engine, but a deterministic guard for a short list of *structurally* destructive shell verbs (`Remove-Item -Recurse`, `Format-Volume`, registry deletes) that routes through `ask_user` regardless of what the model intended — plumbing (pattern match on the *action's* destructiveness, not on app/keyword semantics), consistent with rule 1's "safety confirmation gating" carve-out |
| File tools (read/write/edit/apply_patch) | Structured multi-file patch tool, workspace-scoped | `docs/tools/apply-patch.md` | **PARTIAL** — Yuki does file edits via PowerShell scripting, not a structured patch tool | low priority — PowerShell already gives full file access; a dedicated patch tool mainly pays off for code-editing agents, which Yuki mostly isn't |
| Browser tool / computer-use | Ref-based browser control, snapshot+act computer-use tool | `docs/tools/browser.md`, `docs/nodes/computer-use.md` | **HAVE, arguably the deepest implementation of this concept anywhere** — Yuki's entire perception/action stack (UIA tree, verified scroll/tab/window actions) *is* computer-use, natively, not bolted on | keep; this is Yuki's core strength |
| MCP integration (consuming external MCP servers) | Declare MCP servers, tools appear filtered/gated | `docs/tools/mcp.md` | **MISSING as a consumer** — Yuki *exposes* an MCP server (`yuki/mcp/server.py`, read-only memory tools for Claude Code) but doesn't consume external MCP tools itself | low priority — Yuki's tool set is deliberately small and desktop-native; consuming arbitrary MCP servers cuts against "lightweight" and "no hardcoded" less than it seems, but there's no concrete need yet |
| Tool Search (dynamic discovery) | BM25 search over a tool catalog instead of sending every schema | `docs/tools/tool-search.md` | **N/A** — Yuki's tool count (~20) is nowhere near where this pays for itself | — |
| Loop detection | See agent-loop table | — | **MISSING** | top-15 §9 |
| Self-learning / skill workshop | Background review proposes or auto-applies new skill files from a session's own successful work | `docs/tools/self-learning.md` | **HAVE, different substrate** — `remember_how` is Yuki's self-learning: the agent saves what worked after a task with discovery, attached to memory rather than a file | keep |

## 7. Sub-agents and multi-agent

| Feature | What it does | Lives at | Yuki status | Recommendation |
|---|---|---|---|---|
| Sub-agent spawning (`sessions_spawn`) | Background run in its own session, tracked as a task, isolated or forked context | `docs/tools/subagents.md` | **MISSING** in general form | see below |
| Isolated one-shot escalation call | A cheap, tool-free model call over a bounded log to answer a narrow question | (Yuki independently built this) | **HAVE** — `check_progress` in voice (`yuki/voice/progress.py`): one isolated Haiku call over a task's action log, "do not make up information not in the log" | keep — this is exactly the *useful part* of sub-agent spawning for a single-user assistant; generalize it rather than building full multi-agent (top-15 §3's deep-recall escalation is the same pattern applied to memory) |
| Context isolation / handoff / announce | Isolated child transcript, completion pushed up, results treated as evidence not instructions | `docs/tools/subagents/*` | **N/A** — no multi-agent need on one PC with one user; the two-lane worker/front-desk split (`yuki/ui/runtime.py`) already covers "something else answers while the main agent is busy" | keep the two-lane design; don't build general spawning |
| Agent-to-agent messaging | `sessions_send` between agents/sessions | `docs/concepts/multi-agent.md` | **N/A** | — |
| Managed worktrees (isolated git checkouts per task) | — | `docs/concepts/managed-worktrees.md` | **N/A** — Yuki is not a coding agent | — |

## 8. Sessions

| Feature | Yuki status | Note |
|---|---|---|
| Per-channel/per-peer sessions, session keys | **N/A** | one user, one PC |
| Resets (daily/idle) | **PARTIAL** | portrait rebuilds nightly; conversation session spans one app run, no daily reset of the *agent* session itself |
| History limits | **HAVE** | `keep_perception_turns`, `KEEP_FULL_REQUESTS=3` compaction |
| Idle expiry | **PARTIAL** | voice conversations expire (`voice_idle_s`); the desktop agent's own conversation has no idle-based reset, only app-run boundaries |

## 9. Channels and I/O

| Feature | Yuki status | Note |
|---|---|---|
| Multi-channel messaging (Discord/Slack/WhatsApp/20+) | **N/A** | desktop assistant, by design single-surface |
| Voice/talk mode, wake word | **HAVE, arguably ahead** | `docs/VOICE.md` — two-model mouth/brain split, narrator gate, grounding lookup; more nuanced than OpenClaw's talk-mode docs describe |
| Text | **HAVE** | overlay (`docs/UI.md`) |
| Media handling | **PARTIAL** | screenshots yes; no image/video/audio generation tools (not obviously needed) |
| Group messages, broadcast, bot-loop protection | **N/A** | no group/multi-agent messaging surface |

## 10. Safety and trust

| Feature | What it does | Lives at | Yuki status | Recommendation |
|---|---|---|---|---|
| Prompt-injection wrapping of untrusted content | External content wrapped in random per-request boundary markers + notice; strips fake chat-template tokens and leaked internal tags | `docs/gateway/security/prompt-injection.md` | **MISSING** — Yuki's `look_at_window`/screen text can carry arbitrary web-page or chat content straight into context with no boundary marking | **adopt (S)** — wrap perception text that comes from a browser/web-content window in a clearly-labeled untrusted block, same pattern as `ATTACHED_LABEL`/`ATTACHED_END` already used for auto-attached context (`context.py:135`); this is plumbing (a label), not a keyword-based content filter |
| Taint propagation through a turn | Once one tool result is network-sourced, later assistant output in that turn is tainted | `docs/concepts/memory-architecture.md` | **MISSING**, ties to memory report §1b | top-15 §4 |
| Elevated/dangerous action gating | Structured approval before exec on host, executable-identity binding | `docs/tools/exec-approvals.md`, `elevated.md` | **PARTIAL** — see tools table; Yuki relies on the model's own judgement plus `ask_user`, no deterministic gate for destructive commands | top-15 (folded into §9's exec guard) |
| Secrets handling | SecretRef abstraction, agent never sees plaintext, egress proxy | `docs/gateway/secrets.md` | **N/A-ish, appropriate for scope** — Yuki reads API keys from env/`HKCU\Environment` only, never stores or lets the model see secrets; memory is DPAPI-encrypted at rest. A vault abstraction is overkill for one machine, one user | keep as is |
| Threat-model atlas / incident response process | MITRE-ATLAS-mapped threat catalogue, disclosure process | `docs/security/THREAT-MODEL-ATLAS.md` | **N/A** — project-management artifact for an open-source multi-tenant project, not a harness feature to clone | — |

## 11. Observability

| Feature | What it does | Lives at | Yuki status | Recommendation |
|---|---|---|---|---|
| Structured logs | JSONL + console, redaction, rotation | `docs/gateway/logging.md` | **HAVE, arguably ahead** — Yuki logs full request/response bodies (OpenClaw logs metadata-first); rule 4 already mandates this | keep |
| Usage/cost tracking | `/usage cost`, provider cost tables | `docs/concepts/usage-tracking.md` | **HAVE** — per-request cost, `requests.csv`, comparable in rigor | keep |
| Doctor command (config lint, repair, health) | ~20 numbered checks: config, auth, sandbox images, workspace tips; `--fix`/`--lint` | `docs/gateway/doctor.md` | **MISSING** — no unified `yuki doctor`; checks are scattered (voice preflight, memory `warm()`, prewarm retries) | **adopt (S–M)** — top-15 §11: one CLI command that checks Bedrock reachability, AWS env var, memory service status, mic/speaker/headphone state, disk space for logs; read-only, no auto-fix needed at Yuki's scale |
| Health/readiness endpoints | `/health`, `/ready` HTTP probes | `docs/gateway/health.md` | **N/A** — no remote clients to probe a single desktop process | — |
| OpenTelemetry / Prometheus | Metrics export | `docs/gateway/opentelemetry.md` | **N/A** — no fleet to monitor | — |
| Audit trail | Metadata ledger of agent runs/tool actions | `docs/gateway/audit.md` | **HAVE, different form** — the full JSONL log already is the audit trail; `requests.csv` gives the summarized ledger | keep |

## 12. Configuration and UX

| Feature | What it does | Lives at | Yuki status | Recommendation |
|---|---|---|---|---|
| Config system (layered JSON5, schema-validated, hot reload) | `~/.openclaw/openclaw.json` with `$include`, env substitution, SecretRef | `docs/gateway/configuration.md` | **PARTIAL** — flat `Settings` dataclass + env vars, no file-based layering, no hot reload, no schema validation | low priority — for one developer-operated machine, a dataclass with sane defaults is simpler and matches "lightweight"; would only be worth it if config needs to change without a restart |
| Onboarding wizard | Guided first-run flow establishing verified inference before anything else | `docs/start/onboarding.md` | **N/A** — Yuki has one installation, one operator (the user), who already wrote the architecture contract | — |
| Control UI / web dashboard | Full SPA served by the Gateway | `docs/web/control-ui.md` | **N/A by design** — Yuki's status views are native (tray, overlay, pill), which is the right choice for a desktop-only single-user tool, not a gap | — |
| Status views | Today's list, quiet toggles, model switch | `docs/cli/status.md` | **HAVE** — tray memory section (`docs/UI.md`), voice pill | keep |
| Presence indicators | Live roster of connected clients / active-computer heuristic | `docs/nodes/presence.md` | **N/A** — one PC | — |

## 13. Anything else notable

- **Instant/verified "hands" pattern (voice v3.3).** Not from OpenClaw — Yuki independently built a pattern OpenClaw's docs don't have at all: a "done" line is spoken only once the tool's own report says `verified` (window state, scroll position, tab selection, page title). This is a genuinely novel harness feature worth documenting as a Yuki strength, not a gap.
- **`finishes_with` (single-call task completion).** Also not in OpenClaw's docs: an action tool can carry the model's own claim that this call completes the request, checked against the call's actual success before being honored. Saves a full round trip on one-step voice tasks. Keep and consider generalizing to text-mode single-step requests too (partially already shared code).
- **Ambient, non-conversational capture.** Covered at length in the memory report — Yuki's screen/call/terminal capture has no OpenClaw analogue at all; OpenClaw's entire memory system is conversation-and-tool-turn shaped. This remains Yuki's structural advantage and should not be diluted by importing OpenClaw's conversation-only assumptions.

---

## Top 15 to clone into Yuki next

Ordered by impact on how smart, personal and quick Yuki feels. All respect the four rules (no hardcoded/keyword behavior, lightweight, memory only changed by an audited pipeline, safety gating is plumbing not judgement).

1. **Deterministic trigger-phrase memory injection** (M). Reuse the local embedder already loaded for `recall`; portrait/rule facts get an `importance` + optional trigger phrases written once at portrait-worker time; `yuki/agent/memory.py` embeds the incoming message and injects top ≤3 matches before the reply, no model call. *Files:* `yuki/memory/store.py`, `yuki/agent/memory.py:memory_block_text`, `yuki/agent/prompt.py`.
2. **Ground every coach/nudge message in cited evidence, checked in code** (S). Extend `coach_decision`/`build_coach_message` (`yuki/memory/nudges.py:1105`) with `detail`+`evidence` fields; reject in code when an id isn't in the request.
3. **Gated deep-recall escalation**: teach the agent to call `recall` **and** `activity` together and reconcile them for temporal/cross-source questions (M). *Files:* `yuki/agent/prompt.py`, `yuki/agent/tools.py`.
4. **Untrusted/tainted-content marking** for screen text that is itself an AI chat interface or a paste-target (S–M). *File:* `yuki/memory/journal.py` (`build_sources`/`build_user_message`).
5. **A configured model-failover fallback** for Bedrock outages: one alternate model/region tried after N consecutive `APIConnectionError`s (S–M). *File:* `yuki/agent/loop.py` (`client` property, `_warm_model`/request path).
6. **Prompt-injection boundary wrapping** for web/browser content read via `look_at_window` (S). *File:* `yuki/agent/context.py` (reuse the `ATTACHED_LABEL` pattern for an `[Untrusted content from a web page]` frame).
7. **Stop silently scheduling reminders from call-derived commitments** — one confirmation card instead (S). *File:* `yuki/memory/nudges.py` (`todo_items`, `origin=call` path).
8. **An agent-owned, evolving voice file**, gated like the portrait audit (M–L). *File:* new `yuki/memory/voice.py` or a `portrait.py` section, surfaced in `yuki/agent/prompt.py`'s profile layer.
9. **Loop-detection breaker**: after N identical `(tool, input)` calls in one request, force `ask_user` instead of a repeat (S). *File:* `yuki/agent/loop.py` (`_run_one`, alongside the existing `_record_call` repetition tracking).
10. **Daily cost-spend alert** (soft cap, no throttling) (S). *File:* `yuki/agent/loop.py` (`_summarize`), a small running total surfaced in the tray.
11. **A `yuki doctor` check** (Bedrock reachability, AWS env, memory service status, mic/speaker, disk space for logs) (S–M). *New file:* `scripts/doctor.py` or a `yuki.cli` subcommand.
12. **Split the portrait's cap into a profile budget and a facts budget** (S). *File:* `yuki/memory/portrait.py` render step.
13. **A deterministic guard for structurally destructive shell verbs** (recursive delete, format, registry-delete patterns) that always routes through `ask_user` regardless of model intent (S–M). *File:* `yuki/actions/shell.py` (`run_powershell`).
14. **Model-judged, event-conditioned reminders ("standing intents")**, matching by model judgement not keywords, with OpenClaw's anti-nagging *shape* (cooldown/budget/expiry, explicit-cancel-only) (M–L). *File:* `yuki/memory/journal.py` (extend `record_facts`'s output schema, same pattern as `questions`).
15. **Unify the coach's and voice's anti-spam frameworks** (budget/cooldown/settle/stale) into one shared module so a rule tuned in one place doesn't silently diverge from the other (S). *Files:* `yuki/memory/nudges.py`, `yuki/voice/narrator.py`.
