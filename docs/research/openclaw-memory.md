# OpenClaw vs Yuki: memory, personality, proactivity — research and recommendations

26 Sept 2026. Read-only research task. Sources: the official OpenClaw repo
(`github.com/openclaw/openclaw`, cloned `--depth 1` to a scratch directory and
read directly — not the name-squatted `openclaw-clawdbot-moltbot` or
`openclaw-ai` repos that also surface in search, which are unofficial guides/mirrors),
its docs tree (`docs/concepts/*`, `docs/gateway/*`, `docs/automation/*`,
`docs/reference/templates/*`), its shipped workspace-template and test-fixture
files, plus Yuki's own `docs/MEMORY.md`, `docs/ARCHITECTURE.md`,
`docs/research/coach-and-answers-review.md`, and the `yuki/memory/*` and
`yuki/agent/*` source. No code was changed; this document is the only file
written.

**Provenance check.** OpenClaw is MIT-licensed, stewarded by the nonprofit
OpenClaw Foundation, no paid tier or token; `README.md` and `VISION.md` confirm
the naming history the user described: *Warelay → Clawdbot → Moltbot →
OpenClaw*. 85k+ GitHub stars, local-first (state and memory live on the user's
hardware as plain files), multi-channel (Discord/Slack/WhatsApp/iMessage/20+)
and multi-platform (macOS/iOS/Android/Windows/Linux) personal assistant.

---

## 1. OpenClaw's architecture

### 1.1 Workspace files (the "SOUL.md" layer)

OpenClaw's memory is, by explicit design principle, **"no hidden state": the
model only remembers what is written to files in the agent workspace, every
memory surface is inspectable and editable with a text editor**
(`docs/concepts/memory-architecture.md`). The workspace (default
`~/.openclaw/workspace`, `docs/concepts/agent-workspace.md`) holds a small,
named set of files, each auto-injected at session start (`docs/concepts/agent-workspace.md`
"Workspace file map"; also `docs/automation/standing-orders.md`):

| File | Purpose | Who writes it | Injected |
|---|---|---|---|
| `AGENTS.md` | Operating instructions, standing orders, tool conventions, proactivity rules | Human (edited directly; the agent is told "edit canonical AGENTS.md files directly" in the repo's own dev doctrine) | Always |
| `SOUL.md` | Persona, tone, boundaries, "voice" | **The agent itself**, prompted to; also hand-edited by the human | Always |
| `IDENTITY.md` | Name, "creature", vibe, emoji, avatar | Filled in by the agent during a first-run bootstrap ritual; a `- Label: value` line format a sync tool re-parses | Always |
| `USER.md` (optional) | Stable user preferences/relationships/active-project context, as dated directives | Agent, from conversation | Always, separate 4,000-char budget |
| `MEMORY.md` | Curated durable non-profile facts/decisions | **Dreaming consolidation** (a background worker), or direct user request | Always, budgeted (`bootstrapMaxChars`, default 20,000; total budget 60,000) |
| `memory/YYYY-MM-DD.md` | Daily raw notes/observations | Agent, during work, and the pre-compaction "memory flush" | Never injected; searchable on demand only |
| `DREAMS.md` (optional) | Dream Diary: human-readable log of what dreaming promoted/merged/superseded | Dreaming phases | Never injected; for human review |
| `BOOT.md` / `BOOTSTRAP.md` | Startup checklist / one-time first-run ritual | Human / agent, then deleted | Conditional |

The real (non-template) example fixtures found in the repo's own test suite
(`extensions/oc-path/src/oc-path/tests/fixtures/real/SOUL.md`,
`.../IDENTITY.md`) show what this looks like filled in — short, concrete
prose ("I'm a thoughtful, methodical assistant... terse and direct... never
write to /etc"), not a schema.

Two lines in the shipped `SOUL.md` template
(`docs/reference/templates/SOUL.md`) matter for the personality question
below: *"Each session, you wake up fresh. These files are your memory. Read
them. Update them... If you change this file, tell the user — it's your
soul, and they should know."* and *"This file is yours to evolve. As you
learn who you are, update it."* — **OpenClaw's agent persona is explicitly
self-edited by the model, on any turn it chooses to, with no separate review
or gating pass** (contrast with `MEMORY.md`, which only the dreaming
pipeline may rewrite).

### 1.2 Memory tiers and the write path

`docs/concepts/memory-architecture.md` names five tiers (Instructions /
Curated core / Episodic / Prospective / Review) and one write path with
**exactly one primary writer for durable memory: the dreaming consolidation
pass. Everything else feeds it.**

```
Interactive sessions ---notes, flush---> Episodic tier + index (provenance)
Session end ----------transcript ingestion-------------^
Pre-compaction -------facts to daily notes--------------^
Episodic tier --> Dreaming: deterministic gate --> Consolidation (model, bounded) --> MEMORY.md / USER.md
                                                                                   \-> DREAMS.md (diary + pre-image)
```

**Dreaming** (`docs/concepts/dreaming.md`) runs three phases nightly by
default (`0 3 * * *`, configurable): **Light** (dedupe/stage recent
signals), **REM** (theme/reflection summaries), **Deep** (score, gate, and
promote). Deep-phase promotion needs **all** of: a weighted score
(relevance 0.30, frequency 0.24, query-diversity 0.15, recency 0.15,
consolidation 0.10, conceptual richness 0.06) above `minScore`, a minimum
recall count, and a minimum number of distinct queries that surfaced it —
"memory graduates because it kept being useful, not because it was written
confidently." Gated candidates plus the current `MEMORY.md` go to **one
tool-free consolidation model call** that returns merge/supersede/dedupe
decisions (never freeform replacement prose); code validates the result
(bounded entry loss, budget, evidence citations `Source: path#Lx-Ly`) before
an atomic, optimistic-concurrency file rewrite. A rejected rewrite falls back
to append-only. The pre-image of every rewrite is kept, and a summary lands
in `DREAMS.md`.

**Corrections and supersession**: `USER.md` entries are dated directives
(`<!-- observed: YYYY-MM-DD | status: active|superseded -->`) that get
**rewritten in place** when a preference changes — "never append a
contradicting directive" — because two benchmarks OpenClaw's docs cite
(PrefEval, HorizonBench) show models default back to an older stated
preference when both an old and new directive sit in context. `MEMORY.md`
entries carry a `supersessionKey` so newer observations retire older ones
during consolidation rather than accumulating.

**Kept small**: `MEMORY.md`/`USER.md` are explicitly *not* raw logs — the
docs repeat this ("It is not a raw transcript, daily log, or exhaustive
archive") — detail lives in `memory/YYYY-MM-DD.md`, which is retrievable by
tool but never injected. `USER.md` has a hard, separate 4,000-character cap
that config **cannot raise** (only lower), specifically so profile directives
can't crowd out project rules. Before compaction summarizes a long
conversation, a silent **"memory flush" turn** reminds the agent to save
unwritten context to a daily note first, on a private conversation copy so
its own housekeeping never leaks into the transcript
(`docs/concepts/memory.md` "Automatic memory flush").

### 1.3 Provenance: the security/trust model behind all of this

This is the part of OpenClaw's design most worth studying closely
(`docs/concepts/memory-architecture.md`, "Provenance"). Every memory chunk
carries **origin class** in a SQLite column the model cannot write through
prose — a closed set: `owner` (the human, in a trusted channel), `agent`
(the agent, derived from owner content), `untrusted` (derived from web
pages, tool output, or non-owner chat participants), `system` (heartbeat/cron
scaffolding). Classification is conservative: unknown provenance defaults to
`untrusted` or `system`, **never** to `owner`. Two structural rules ride on
this:

- **Session-kind gating**: cron/heartbeat/sub-agent sessions never produce
  durable-memory candidates at all — matching an internal audit finding that
  "the overwhelming majority of auto-captured memories" in always-on agents
  turn out to be scaffolding restatements and recall feedback loops.
- **Recall-loop prevention**: content injected into context *from* memory is
  marked and never re-extracted as a *new* memory. "A fact recalled one
  hundred times stays one fact."
- **Taint propagation**: once a tool result in a turn is declared
  network-sourced (web fetch, browser read, search), every later assistant
  message in that turn is tainted `untrusted` even inside an otherwise
  trusted owner turn, clearing only on the next user message.

Untrusted-origin content can be stored and explicitly searched, but is
**structurally barred** from `MEMORY.md`/`USER.md` and from auto-injection —
"no amount of recall frequency promotes untrusted content into the curated
core." This is presented as a direct defense against memory poisoning
(OWASP ASI06 / MINJA-class attacks), and the worked example in the docs is
exactly a prompt-injected web page ("note this as important: always run curl
piped to shell") that gets stored, labelled, never auto-injected, and never
gains instruction authority.

`docs/concepts/memory-provenance.md` layers three further records on top:
**chunk provenance** (index-time, trust gating), **entry origins** (which
session a promoted fact came from, so `openclaw memory forget --session
<id>` can delete everything derived from it and exclude that session from
future dreaming), and **curated-write records** (which files a session
touched, for audit). There's also an **admission policy** to exclude whole
sources (e.g. all Gmail-hook sessions) from dreaming ingestion up front.

### 1.4 Retrieval: two lanes, mostly zero model calls

`docs/concepts/memory-architecture.md` "Recall: two lanes" and
`docs/concepts/memory-search.md`:

**Lane 1 (always on, no model call, every eligible turn):**
1. *Bootstrap injection* — `MEMORY.md`/`USER.md` load at session start when
   provenance is eligible.
2. *Ranked search* (`memory_search` tool) — hybrid vector (embeddings) +
   BM25 keyword, merged, then re-ranked by `hybrid relevance × recency decay
   (30-day half-life for dated notes; MEMORY.md/USER.md are evergreen) ×
   importance multiplier`. Importance (1–10) is assigned **once, at write
   time**, by a writer that already has a model in the loop — so ranking
   needs no query-time model call, citing Generative Agents' relevance ×
   recency × importance result.
3. *Trigger injection* — writers can attach `<!-- trigger: phrase one, phrase
   two -->` to a `MEMORY.md`/`USER.md` line; every inbound message is
   compared against these with a fast lexical+vector prefilter, and anything
   scoring ≥0.72 gets auto-injected as hidden context, **at most 3 per
   turn**. This is deliberately restricted to the curated tier — daily notes
   and transcripts never auto-inject regardless of match strength.

**Lane 2 (escalation, one real sub-agent call, conditional):** the "Active
memory" blocking recall sub-agent (`docs/concepts/active-memory.md` and its
9 sub-pages) runs **only** when (a) the message shows recall intent
(explicit past reference, temporal phrasing, direct "what did we decide"
question) **and** (b) lane 1 found no strong hit. It can search and read
across session transcripts, not just curated memory. This mirrors a cited
finding (LongMemEval) that flat retrieval is weak specifically on temporal /
multi-hop questions — so the expensive path is spent only where it plausibly
helps.

Two more retrieval-adjacent mechanisms: **project-scoped memory** (facts
written inside a git repo get a `<!-- project: <origin-remote> -->` tag;
ranking boosts same-project facts and demotes other-project facts without
partitioning storage) and a bundled **`memory-wiki`** plugin (opt-in) that
compiles `MEMORY.md`/session history into a structured wiki of `entities/`
(people, systems, projects) with typed `relationships`, per-claim `evidence`,
confidence, and "contradiction and freshness tracking" — the closest thing
in OpenClaw to Yuki's portrait audit, but general-purpose, separate, and not
enabled by default.

### 1.5 Proactivity: heartbeat, automations, standing orders

Three distinct, composable mechanisms (`docs/automation/index.md`):

- **Automations** (cron jobs) — explicit, user- or agent-created, one-shot or
  recurring, own schedule/model/delivery channel, produce audit records.
- **Heartbeat** (`docs/gateway/heartbeat.md`) — one **system-owned**
  recurring automation (default every 30 min, or 1h on Anthropic OAuth) that
  runs a normal agent turn in the **main session** with a narrow default
  prompt: *"Follow the heartbeat monitor scratch context when provided...
  Do not infer or repeat old tasks from prior chats. If nothing needs
  attention, reply NO_REPLY."* The only durable state it's given by default
  is a tiny, human/agent-editable **"monitor scratch"** (capped 256 KiB,
  meant to be a short checklist, e.g. *"Quick scan: anything urgent in
  inboxes? If a task is blocked, write down what is missing."*). An
  **effectively-empty scratch skips the run entirely** to save API calls.
  Cost controls: `isolatedSession` (fresh session, no history, ~100K tokens
  down to ~2–5K/run), `lightContext` (skip workspace bootstrap files),
  cheaper per-heartbeat model override, `activeHours` (quiet hours by
  timezone). Anti-spam: heartbeat-only replies never keep the session
  "alive"; a run is skipped (`no-route`) if there's no resolvable owner
  DM; runs defer while the agent/target session is already busy; delivery
  visibility (`showOk`/`showAlerts`/`useIndicator`) is independently
  configurable per channel/account.
- **Standing orders** (`docs/automation/standing-orders.md`) — persistent
  *authorization* text living in `AGENTS.md` ("You own the weekly report...
  only escalate if something looks wrong"), paired with an automation for
  the actual timing. Explicitly recommends an **Execute → Verify → Report**
  loop and named escalation rules, to stop the common failure of an agent
  acknowledging a task without completing it.

**A directly relevant retraction**: `docs/automation/index.md` records that
OpenClaw shipped and then **removed** an "inferred commitments" feature in
v2026.8.1 — *"OpenClaw no longer extracts follow-ups from conversations or
delivers them through heartbeat... For reminders or scheduled work, create an
explicit automation."* This is the same failure shape Yuki's own
`coach-and-answers-review.md` (§1.5 C6) documents for call-derived
commitment reminders (wrong owner, wrong day, vague reminders the model then
has to ask the user to explain) — OpenClaw tried the automatic version and
killed it rather than tuning it further.

**Standing intents** (`docs/concepts/standing-intents.md`) are the piece
that maps most directly onto "remember to bring X up when Y happens":
event-conditioned, not clock-conditioned, prospective memory. The agent
calls an `intent` tool with a description plus **trigger keywords** (and
optionally a trigger embedding, channel/sender scope, expiry, cooldown, fire
budget). Matching is a **deterministic FTS keyword prefilter with no model
call** — "a candidate fires only when every term in at least one configured
trigger entry appears in the turn," rechecked against SQLite for scope/
expiry/cooldown/budget in one transaction. Anti-nagging is structural, not
model-judged: default cooldown 24h, max 3 fires, expiry 90 days, at most 3
intents injected per turn. Cancellation is always explicit — "OpenClaw never
infers cancellation from ordinary conversation," citing a benchmark
(ProEvent) that proactive systems "frequently overact and struggle with
event cancellation."

### 1.6 Personality

There is no separate machinery beyond what's in §1.1: `SOUL.md`/`IDENTITY.md`
are ordinary workspace files, always injected, and the *only* documented
"evolution" mechanism is the model rewriting them itself when it judges its
voice should change, per the template's own instruction. There's no
scoring, gating, nightly worker, or audit comparable to dreaming's promotion
pipeline for this file — personality drift risk is accepted, not engineered
around. The one guardrail stated anywhere is social, not structural:
*"If you change this file, tell the user."*

### 1.7 Sessions and compaction

The main session persists across turns; a **pre-compaction memory flush**
(private, silent turn) guarantees unwritten context reaches a daily note
before a compaction summary discards detail (`docs/concepts/memory.md`).
Compaction itself, session pruning, and session-state docs exist
(`docs/reference/session-management-compaction/*`) but describe transcript
housekeeping, not a memory mechanism distinct from what's above.

---

## 2. Yuki's architecture (for the comparison; file:line references)

Full detail is in `docs/MEMORY.md`; this is the recap needed for §3.

- **Captures → Journal**: OS-event-driven screen/message capture
  (`docs/MEMORY.md:21-36`), turned by Haiku into short dated third-person
  facts, each with a **nature**: `fact | trait | story | humor | uncertain`
  (`yuki/memory/journal.py:876` `_nature_counts`; nature guidance shared via
  `yuki/memory/personality.py:84` `NATURE_GUIDANCE`; migration 14,
  `docs/MEMORY.md:208`, `:423-432`). Terminal history and call transcripts
  (own mic only) feed the same journal (`docs/MEMORY.md:100-203`).
- **Portrait**: nightly + weekly Sonnet worker
  (`yuki/memory/portrait.py:936` `class PortraitWorker`) does ADD/UPDATE/
  INVALIDATE/NOOP over current facts citing journal ids (bi-temporal,
  `valid_from`/`valid_to`, Mem0-style), renders a ~1,500-token page including
  a dedicated **Personality** section (`yuki/memory/personality.py:62`
  `PERSONALITY_HEADING`; aspects `talk|cares|joke|story|people`, at most
  250 words/2,000 chars, `docs/MEMORY.md:429`). A **loop check** every
  ≥30 min keeps open loops current between full runs
  (`docs/MEMORY.md:225`).
- **Portrait audit ("connect the dots")**: a separate pass
  (`yuki/memory/portrait.py:759` `_audit_tool`) re-examines *old* facts —
  not just new journal entries — for `contradiction | misattribution |
  relationship | stale | agent_work`, using cited evidence, sibling entries
  of the same thread, embedding-similar entries, and the original source
  excerpt (`docs/MEMORY.md:229-238`). This is the mechanism that caught a
  fact about someone else wrongly attributed to the user, and a stale "on a break" claim from an 15-month-old video
  description.
- **Conversation memory**: rules/preferences/commitments/facts, bi-temporal,
  quote-verified against the cited exchange (`yuki/memory/conversations.py:409`
  `normalize_quote`, `:414` `quote_in`; `class ConversationWorker:573`),
  session summaries, `standing_context()` (1,200-char hard budget,
  `docs/MEMORY.md:272`) and `resume_context()` (4,000-char, `:273`).
- **Questions**: journal/conversation/portrait extraction each returns
  `questions` (`yuki/memory/questions.py:82` `question_item_schema`),
  deduped by subject-hash or embedding cosine (`:234` `embed_questions`),
  asked by the coach at a calm moment like praise, answers folded back as
  a user-origin portrait correction within ~a minute
  (`docs/MEMORY.md:409-421`).
- **Coach/nudges**: `yuki/memory/nudges.py` — `observe()` (:853),
  `build_coach_message()` (:1105), `resume_gap()`/`build_resume_message()`
  (:1505/:1570); transition/follow-up/periodic/reminder/resume triggers,
  budget + rate limits, quiet hours (`docs/MEMORY.md:293-408`).
- **Prompt layering**: `yuki/agent/prompt.py:260` `system_blocks()` — core
  (frozen) → profile (identity + portrait Personality section, its own
  cache breakpoint) → rules (standing rules verbatim, its own breakpoint) →
  volatile (`yuki/agent/memory.py:1035` `memory_block_text`) in `messages`.
- **Weekly review**: code computes every number, Sonnet only narrates
  (`docs/MEMORY.md:434-509`).

---

## 3. Head-to-head

### 3.1 Where OpenClaw's approach is better than Yuki's today

1. **Zero-model-call injection of the *specific* relevant fact, every
   turn.** OpenClaw's trigger-phrase prefilter (§1.4) puts up to 3
   high-similarity curated facts into hidden context *before* the reply,
   with no tool call and no model round-trip. Yuki's `recall()` is a tool
   the agent must decide to call, and the always-injected memory block is a
   single flat portrait/rules/todos dump, not per-message-relevant. This is
   very likely part of why `coach-and-answers-review.md` found "dumb
   answers" and a wrong Discord-time answer (§2 of that doc): the agent had
   to *think* to reach for `recall`/`activity`, and sometimes didn't.
2. **An explicit escalation lane for hard recall.** OpenClaw's "Active
   memory" sub-agent triggers deterministically (recall-intent language +
   lane-1 miss) and runs a real search-and-read pass over transcripts.
   Yuki has no equivalent — the same review document's Discord-time failure
   and "how have I been doing since this morning" failures are exactly the
   temporal/multi-hop cases OpenClaw's own citations (LongMemEval) call out
   as flat retrieval's weak spot.
3. **A formal, closed-set provenance/taint model.** `owner | agent |
   untrusted | system`, enforced in SQL columns the model can't spoof,
   with taint propagating through a whole turn once network content
   appears. Yuki has the ingredients (`by_yuki` marking, nature labels,
   secret filter) but no unified untrusted/tainted concept — the
   `coach-and-answers-review.md` §1.2 C2 finding ("screen text the user
   typed into *another AI tool* is read as fact about him") is precisely
   the failure OpenClaw's taint rule exists to prevent.
4. **Structural, non-model anti-spam for prospective memory.** Standing
   intents' cooldown/fire-budget/expiry and the heartbeat's busy/quiet-hour/
   no-route gates are deterministic code, not a model "should I say
   something" judgment layered on ever-growing rules text. Yuki's coach
   anti-spam (budget_min, min_gap_s, follow_up suppression) is similar in
   spirit but the "stfu became a permanent global rule the coach reads as a
   RULES-block instruction" failure (`coach-and-answers-review.md` §1.2 C1)
   is exactly the class of bug a closed lifecycle (`pending → armed → fired
   → done/cancelled/expired`, explicit cancellation only) would have
   prevented by design.
5. **They already tried, and killed, "infer reminders from what was said"**
   (§1.5) — a stronger negative signal than Yuki generating the same
   finding internally, because it's independent confirmation from a much
   larger, differently-instrumented deployment.
6. **A directive user-model file with enforced single-active-version
   semantics** (`USER.md`) is architecturally simple and auditable by a
   human just reading the file, even though Yuki's DB-level bi-temporal
   `conversation_facts` with quote verification is arguably *more* rigorous
   (harder to accidentally leave two active contradictory directives, since
   there's no "leave the old one un-superseded by mistake" failure mode a
   markdown edit can have).

### 3.2 Where Yuki's ambient capture is a strength OpenClaw plainly lacks

OpenClaw's entire memory system is **conversation-and-tool-turn shaped**:
everything enters through a chat message, a tool result, or a session
transcript. There is no passive "what is on the user's screen right now"
capture anywhere in the docs — the closest thing, session-transcript
ingestion, only sees what was said *to the agent*. This means:

- Yuki's journal accumulates facts the user never told Yuki and never will
  ("watched X on YouTube," "read Y article," "opened Z's profile") — this
  is simply out of scope for OpenClaw by architecture, not a missing
  feature it could config its way into.
- Yuki's Personality section is built from **ambient behavior** (how the
  user jokes in a Discord call, what they're doing when idle) as well as
  chat, whereas OpenClaw's `SOUL.md`/`IDENTITY.md` describe *the agent*,
  not the user, and the user-facing profile (`USER.md`) is built only from
  what the user explicitly told the agent in conversation.
- The portrait audit's cross-source contradiction-hunting
  (`yuki/memory/portrait.py:759`) works because Yuki has independent
  corroborating evidence streams (screen text, call transcripts, terminal
  history) to check a fact against. OpenClaw's `memory-wiki` claims/evidence
  layer is the nearest analogue but is opt-in, general-purpose (any entity,
  not specifically "who is this person to the user"), and has nothing to
  check *against* except what was said in chat.
- Yuki's questions pipeline (§2) is **more sophisticated than anything
  documented in OpenClaw**: OpenClaw's `SOUL.md` template says the agent
  should "ask clarifying questions when ambiguous" as a personality trait,
  and its docs describe no dedicated, deduped, importance-ranked,
  evidence-cited queue of things-the-agent-doesn't-know-and-should-ask,
  timed like a heartbeat check-in and folded back as a correction. This
  was somewhat surprising to find, given the user's framing of OpenClaw as
  the benchmark — on this specific axis, Yuki is already ahead.

### 3.3 The four things the user asked about specifically

**Contextual "connect the dots" questions.** Yuki already has the stronger
mechanism (portrait audit + questions pipeline, §2). OpenClaw has no
document describing an agent-initiated "I noticed X and Y might conflict,
which is right?" flow; its closest concept (`memory-wiki` contradiction
tracking) is passive/human-reviewed, not a scheduled re-audit that asks.
**Recommendation direction**: keep and strengthen what Yuki has; don't
import from OpenClaw here.

**The agent using memory well in conversation.** OpenClaw is ahead
(§3.1.1–2): automatic trigger-injection + an escalation lane. Yuki's
`coach-and-answers-review.md` independently found the exact class of bug
this would fix. **Recommendation direction**: adopt OpenClaw's shape
(deterministic trigger-match injection + a gated deep-recall escalation),
adapted to Yuki's local, no-network, always-local-embeddings design.

**An agent-owned persona that evolves.** OpenClaw has this (`SOUL.md`) but
by unstructured self-edit with no gate — which is in tension with Yuki's
own rules ("no hardcoded heuristics" is about behavior, but "memory kept
right by the pipeline, not hand-edited" clearly generalizes to "no raw,
ungated self-edits either"). Yuki has *no* file like this at all today —
its Personality section describes the *user*, and there is no file
describing Yuki's own voice that Yuki itself can propose changes to.
**Recommendation direction**: add one, but gate it the way the portrait
already gates user-facing changes (nightly worker proposes, audit-style
validation, never a live raw self-edit mid-conversation).

**The user profile the agent itself curates.** Both systems have this in
some form (OpenClaw's `USER.md`+dreaming vs Yuki's portrait+conversation
memory), and Yuki's version is bi-temporal, quote-verified, and audited —
arguably stronger. The one thing OpenClaw does that Yuki doesn't: a hard,
tool-enforced separation between "durable non-profile fact" (`MEMORY.md`)
and "profile directive" (`USER.md`), each with its own budget, so profile
bloat can never crowd out other durable memory or vice versa. Yuki's
portrait mixes both under one ~1,500-token cap.

**Proactivity.** Both have heartbeat-like mechanisms; OpenClaw's is
narrower and colder by default (quiet unless scratch says otherwise, no
inferred commitments — a design choice reached by *removing* a feature),
Yuki's coach is richer (screen-aware, transition/resume-aware) but
`coach-and-answers-review.md` shows it currently pays for that richness in
reliability. The clearest actionable insight from OpenClaw here is
structural: **require every proactive message to cite the evidence it rests
on, checked in code** (OpenClaw's consolidation requires `Source: path#Lx-Ly`
citations before a rewrite is accepted; standing intents require an exact
keyword match before firing). Yuki's own review already proposed this
independently (`coach-and-answers-review.md` §1.5, "Add a `detail` field...
Add an `evidence` field... Code rejects the message when the ids are not in
the request") — OpenClaw's shipped design is a working existence proof that
this pattern is buildable and worth prioritizing.

---

## 4. Recommendations, phased

All of these are written to respect the project's standing rules: **no
hardcoded behavior heuristics** (every "should this fire" judgment below is
either deterministic *plumbing* — a citation check, a budget, a cooldown —
or left to a model reading the situation, never a keyword/regex list
deciding behavior), **lightweight on the PC** (no new always-loaded model;
reuse the existing local embedder and Haiku/Sonnet calls already budgeted),
and **memory kept right by the pipeline, never hand-edited** (every new
surface below is written by a worker with the same audit/citation
discipline the portrait already has, never a file a human or the agent
free-edits mid-conversation).

### Phase 1 — cheap, high-confidence, fixes an already-diagnosed bug class

**1a. Ground every coach/nudge message in cited evidence, checked in code.**
This is `coach-and-answers-review.md` §1.5's own top-ranked fix, and
OpenClaw's dreaming-consolidation citation requirement
(`Source: path#Lx-Ly`) is a working precedent for the exact mechanism.
- *Where*: `yuki/memory/nudges.py` — extend the `coach_decision` tool schema
  (near `build_coach_message`, `:1105`) with `detail` (the one concrete
  thing, as the data names it) and `evidence` (journal/timeline ids). Reject
  in code when an id isn't in the request, same pattern as the existing
  `mentions` check.
- *Cost*: no new model calls (same Haiku/Sonnet decision call, slightly
  larger schema). A few hours of implementation + the offline replay harness
  `coach-and-answers-review.md` §"How to test it" already describes
  (`nudges-*.jsonl` replay, no live desktop).
- *Risk*: low. Purely additive validation; a message with no groundable
  detail simply becomes `say: none`.

**1b. Add an `untrusted`/tainted-turn concept for content copied from other
AI tools or web pages.** Directly matches OpenClaw's turn-taint rule
(§1.3) and directly fixes the diagnosed bug (`coach-and-answers-review.md`
§1.2 C2 — a ChatGPT test prompt read as a fact about the user's life).
- *Where*: `yuki/memory/journal.py` (`build_sources`/`build_user_message`,
  around `:609`/`:669`) — when a capture's `kind` is a page/document that is
  itself an AI chat interface or a paste-target the model is composing text
  *into* (this is a model judgment from the capture's own content — "is this
  the user's own life, or text they're feeding to another tool" — not a
  domain/keyword list), mark the source `agent_composed_elsewhere` (a nature
  or source-kind flag, same shape as the existing `nature` column,
  migration-additive like `journal.nature` was). Readers (portrait, coach)
  treat it like `by_yuki`: excluded from "what the user is/likes" facts.
- *Cost*: no new model calls — this is a labeling instruction added to the
  existing Haiku journal prompt, validated same as `nature` already is.
- *Risk*: low-medium. The model has to correctly recognize "this capture is
  the user typing *into* an AI tool" vs "this is the user's own writing" —
  same class of judgment the `nature` field already asks for successfully.

**1c. Stop generating reminders from call-derived commitments without
confirmation.** OpenClaw independently shipped-then-killed this exact
feature (§1.5); Yuki's own review already recommends it
(`coach-and-answers-review.md` §1.5 item 5, §3.2 C6).
- *Where*: `yuki/memory/nudges.py` (`todo_items`/`_write_reminder`, the
  `origin=call` commitment path) — change from "silently schedule a
  reminder" to "one card: `want a reminder for that?`", and never resolve a
  relative day ("kal"/"tomorrow") against the calendar date without asking
  when the utterance was near midnight/a sleep boundary (a model judgment
  from the timeline, not a fixed clock cutoff).
- *Cost*: none (one more model-authored card, already within the coach's
  existing call volume).
- *Risk*: low. Strictly reduces false reminders; worst case is one extra
  confirmation card.

### Phase 2 — moderate effort, biggest gain for "using memory well in conversation"

**2a. Deterministic trigger-phrase injection for portrait/rule facts.**
Mirrors OpenClaw's Lane 1 trigger mechanism (§1.4), reusing Yuki's existing
local embedder (already loaded for `recall`) — no new dependency, no new
always-on cost beyond what the embedder already costs when active
(`docs/MEMORY.md:531-533` footprint notes).
- *Where*: `yuki/memory/store.py` — add optional `trigger_phrases` (a short
  list) and reuse the existing `importance` column pattern (portrait_facts
  already has none; add one, written once by the portrait worker/audit when
  it already has a model in the loop, same as OpenClaw scores importance
  once at write time). `yuki/agent/memory.py` (`memory_block_text`, `:1035`)
  gains a pre-reply step: embed the incoming user message, compare against
  stored trigger phrases with the same cosine machinery `questions.py`
  already uses for dedup (`:234` `embed_questions` is the closest existing
  pattern), inject the top ≤3 matches ≥ some measured threshold (tune
  empirically the way the existing `QUESTION_DUPLICATE_COSINE` 0.70 was
  tuned, not hardcoded from OpenClaw's 0.72) as a small hidden block before
  the standing-rules block in `yuki/agent/prompt.py` layering (`:260`
  `system_blocks`).
- *Cost*: one extra local embedding call per turn (already sub-10ms per the
  footprint measurements) — no LLM call, no measurable RAM/CPU beyond what
  the embedder already costs while loaded. This is the single highest
  gain-per-cost item in this report.
- *Risk*: low. Purely additive context; if it never fires, behavior is
  unchanged from today.

**2b. A gated "deep recall" path for hard temporal/multi-hop questions.**
Mirrors OpenClaw's escalation lane (§1.4 Lane 2), sized to Yuki's existing
tools rather than a new sub-agent framework.
- *Where*: `yuki/agent/tools.py`/`loop.py` — this is *not* a new keyword
  trigger ("does the message contain 'when' or 'how long'"); instead, teach
  the main agent (via `prompt.py`'s core block, not a hardcoded classifier)
  that when it notices a question needs cross-referencing calls, timeline,
  and journal together (exactly the Discord-time failure class in
  `coach-and-answers-review.md` §2), it should call `recall` **and**
  `activity` together and reconcile them itself, rather than answering from
  whichever single tool it reached for first. This is a prompt-and-tool-
  description fix (§2.4/2.5 of that review already spells out the exact
  wording), not new infrastructure — cheaper than building an OpenClaw-style
  separate sub-agent, and Yuki is small enough that one well-informed model
  call already has room for both tool results.
- *Cost*: potentially one more tool call on questions that need it (a few
  cents/day at Yuki's current volumes).
- *Risk*: low. This is mostly a documentation/prompt fix already scoped by
  the existing review; the "add calls to `activity`" data work
  (`coach-and-answers-review.md` §2.4) is the real prerequisite and should
  land first regardless of this recommendation.

**2c. Split the portrait's ~1,500-token cap into a profile budget and a
facts budget**, the way `USER.md`/`MEMORY.md` are separately capped in
OpenClaw.
- *Where*: `yuki/memory/portrait.py` render step — give Personality +
  Preferences + Relationships (the "who is this person to talk to" parts) a
  reserved sub-budget separate from Work/Interests/Open-loops, so a busy
  week of work facts can't silently truncate the personality section a
  future recommendation (3a) will make more important.
- *Cost*: no new model calls, a render-prompt change.
- *Risk*: low.

### Phase 3 — larger, more novel, do after 1–2 are proven

**3a. Give Yuki an agent-owned, evolving voice file — gated, not
self-edited raw.** This directly answers the user's "agent-owned persona
that evolves" ask, while respecting "memory kept right by the pipeline, not
hand-edited": unlike OpenClaw's `SOUL.md` (any turn, ungated,
tell-the-user-after-the-fact), Yuki's version should look like the portrait
audit, not like a raw file the running agent pokes at directly.
- *Where*: new `yuki/memory/voice.py` (or a section inside
  `portrait.py`), nightly, after the existing Personality-section render:
  the same Sonnet worker that already writes the user's Personality section
  (from journal `trait`/`humor`/`story` evidence about the *user*) proposes
  small, evidence-cited deltas to a **separate**, short "how Yuki talks"
  block — e.g. noticing that the user has started responding well to a
  drier, more clipped tone in recent exchanges (conversation-memory
  `session_summaries`, reaction data the coach already tracks per
  `docs/MEMORY.md:364` "Reactions feed back only through existing paths").
  Validate the same way the audit validates portrait changes (citations
  required, bounded size, `valid_from`/`valid_to`) before it's live in
  `yuki/agent/prompt.py`'s profile layer (`:235` `profile_text`, right next
  to where the portrait's Personality section already sits).
- *Cost*: one additional small Sonnet call per night, folded into the
  existing nightly portrait run (same cost order as the existing Personality
  section, `docs/MEMORY.md` cost tables put comparable calls at
  $0.02–0.05).
- *Risk*: medium. The main risk is exactly what OpenClaw accepts and Yuki's
  rules argue against — an evolving persona is inherently a slow-drift
  surface. Mitigate by: requiring the same audit-style evidence citation the
  portrait already requires, capping how much the block can change per
  night (an OpenClaw-style `maxPriorEntryLossFraction` equivalent), and
  showing the user the diff (reuse the existing `DREAMS.md`-equivalent idea:
  a visible changelog, not a silent rewrite) so this doesn't become a
  second hand-edited-memory surface in spirit even though no human hand-
  edits it.

**3b. Model-judged event-conditioned reminders ("standing intents"),
without keyword-matching as the trigger.** OpenClaw's exact mechanism
(deterministic FTS keyword prefilter deciding *when to fire*) is close to
the "no keyword lists... deciding what the agent does" rule Yuki's
architecture explicitly forbids, so this should **not** be a straight port.
- *Where*: reuse the machinery Yuki already has for exactly this kind of
  judgment — the journal worker already reads new captures with a model in
  the loop every few minutes (`docs/MEMORY.md:212-219`). Add an `intents`
  output list to `record_facts` (same shape as the existing `questions`
  output), each with the user's own stated trigger condition in their own
  words (quote-verified, same discipline as commitments). On each new
  journal batch, the *existing* Haiku call that already reads the batch also
  checks it against open intents and reports a match with its reasoning —
  a model judgment, not a keyword index, so it stays inside the no-hardcoded-
  heuristics rule at the cost of being slightly more expensive than
  OpenClaw's free keyword check.
- *Cost*: marginal — piggybacks on a model call the journal worker is
  already making; the incremental cost is a longer prompt/output schema,
  similar order to the existing `+900 input tokens` the `questions` feature
  added (`docs/MEMORY.md:421`, "~+$0.16 a day").
- *Risk*: medium. Needs the same anti-nagging structure OpenClaw uses
  (cooldown, fire budget, explicit-only cancellation) implemented as
  deterministic code around the model's match judgment, not left to the
  model to self-limit.

### What not to import

- **OpenClaw's raw self-edited `SOUL.md`.** Explicitly rejected in favor of
  3a's gated version — matches the user's stated rule better.
- **Standing intents' literal keyword-prefilter matching mechanism.**
  Rejected in favor of 3b's model-judged version, for the same reason.
- **Heartbeat's chat-turn-based "is anything worth mentioning" loop as a
  replacement for Yuki's coach.** Yuki's ambient screen awareness is a real
  advantage (§3.2); OpenClaw's heartbeat is worth borrowing *ideas* from
  (grounding, scratch-as-tiny-checklist, quiet-hours-by-timezone), not
  replacing the coach's architecture with.
- **`memory-wiki`'s general entity/claims model wholesale.** Yuki's portrait
  audit already does the "who is this person, what's true, what contradicts
  what" job for the one entity graph Yuki actually needs (people around the
  user); building a general knowledge-wiki layer on top would be scope growth
  without a concrete gap it fills today.

---

## Summary table

| Axis | OpenClaw | Yuki | Verdict |
|---|---|---|---|
| Ambient (non-chat) capture | None — chat/tool-turn only | Screen, calls, terminal | **Yuki ahead** — keep |
| Connect-the-dots contradiction questions | `memory-wiki` (opt-in, passive) | Portrait audit + questions pipeline (built-in, active) | **Yuki ahead** — keep, phase 3 mostly skipped here |
| Memory used well mid-conversation | Trigger-injection + escalation lane | Flat block + on-demand `recall` tool | **OpenClaw ahead** — adopt (2a, 2b) |
| Agent-owned evolving persona | `SOUL.md`, ungated self-edit | None (Personality section describes the *user*) | **OpenClaw has it, ungated; Yuki should add a gated version** (3a) |
| User profile curated by the agent | `USER.md` + dreaming (markdown, convention-enforced) | Portrait + conversation memory (DB, bi-temporal, quote-verified, audited) | **Yuki ahead**, adopt OpenClaw's separate-budget idea (2c) |
| Proactivity grounding/anti-hallucination | Citation-required consolidation; keyword-gated intents | Currently ungrounded (diagnosed bug) | **Adopt OpenClaw's grounding discipline** (1a) — Yuki's own review already proposed this |
| Provenance/taint model | Formal, closed-set, SQL-enforced | Partial (`by_yuki`, `nature`) | **Adopt the taint concept** (1b) |
| Inferred reminders from casual speech | Tried, removed | Currently causes bad reminders (diagnosed bug) | **Follow OpenClaw's retreat** (1c) |
