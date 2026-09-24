# Yuki memory — design contract (v1)

Goal: Yuki understands its user — work, interests, people, routines, preferences, open loops — and remembers how things are done on this PC. Informed by MaxMi (`C:\Users\esska\MaxMi`, macOS/Swift, reference only: reuse its ideas and prompts, never its code) and by the research on hierarchical memory (Generative Agents reflection, Mem0 add/update/delete, Graphiti bi-temporal facts, LangMem semantic/episodic/procedural split).

All rules in `docs/ARCHITECTURE.md` apply (no behaviour heuristics, no fixed sleeps, log everything, builders never touch the live desktop).

## Four layers

| Layer | What | Written by | Kept |
|---|---|---|---|
| **Captures** | What newly appeared on screen: conversation messages (fingerprinted, new or history) or main-text deltas of pages/documents | watcher | 30 days (TTL); message fingerprints forever |
| **Journal** | Short dated third-person facts ("Watched 'X' by MrBeast on YouTube in Arc"), each dated by the message or capture it comes from | journal worker, every few minutes, Haiku | forever |
| **Portrait** | One-page user model: work, interests, people (+ pending with each), routines, preferences, open loops | portrait worker, nightly + weekly, Sonnet | forever, bi-temporal |
| **Know-how** | Procedures that worked on this PC ("Arc: pass the URL as a launch argument") | the agent itself after successful tasks, via a tool | forever, bi-temporal |

## Processes

- `yuki-memory`: a separate background process (started by the tray app; its own entry point) that runs the watcher, the journal worker, the conversation worker (Yuki's own exchanges, see "Conversation memory") and the portrait worker. A crash here must never take Yuki down.
- Yuki (the agent) reads memory directly from the store (same machine) through tools; no network hop. An MCP server for other agents comes later.

## Watcher (capture)

- **Triggers are OS events**, not polling: `SetWinEventHook` for `EVENT_SYSTEM_FOREGROUND`, `EVENT_OBJECT_NAMECHANGE` (title changes), `EVENT_OBJECT_FOCUS`; plus a slow adaptive backstop re-read of the foreground window (e.g. every 45–90 s while the user is active). Debounce so a burst of events yields one capture after the window settles (condition: title/tree stable), never a fixed sleep.
- **Page changes without a title change** (reels, feeds): the timeline re-reads the page's address within `PAGE_ACTIVE_REPROBE_S` (2.5 s) of a content name or value change of the process in front (the address bar's value is one), and when the address changed it asks the watcher for a capture (`Watcher.request_capture(hwnd, "page")`, a message to its hook thread), rate-limited per window by `min_gap_s["page"]` (2 s). Measured 2026-09-24 night 2: 5 reels scrolled, 4 captures with distinct captions (before: 1, the backstop).
- **Empty first read retried once**: a read after a `foreground`/`start`/`resume`/`page` trigger that found nothing (or under `retry_below_chars`, 24), or a window that did not answer (`window_busy`, also after `title`), is read again once the window settles: on its next hooked event, when the process behind a UWP frame appears (the hooks are then re-registered for it), or at the latest `retry_wait_s` (5 s) later. Windows Settings: empty at launch, then captured 1 s later (before: nothing at all). Known limit: WinUI apps (Settings) raise no WinEvents for in-app navigation (UIA events only), so later page switches there are seen by the backstop only.
- **A browser tab still loading is not another tab**: when the window's selected tab and its title name none of the pages found (they are hidden tabs' Documents, which Chromium keeps in the tree), nothing is read (`window_busy`); reading the whole window then would read the hidden tabs as if they were on screen.
- **What is read**: the foreground window only, through the extraction layer `yuki/memory/extract` (`extract(hwnd, now=, user_names=, app=, timeout_s=1.3)`, never raises): one structured, visible-only UIA read, turned by per-app profiles (`extract/profiles_default.toml`, data) or generic structure into an `Extraction`: `kind` (conversation | email | page | document | list | terminal | generic), `thread_scope` (the Slack channel/DM, the Gmail subject, the page URL), and either `messages` (chats and mail: sender, `is_me`, time label as shown, absolute `at`, a stable `fingerprint`, a `content_key`) or the main `body` with UI chrome removed (`dropped_chars`). Sidebars, toolbars, conversation lists and the composer are never content. The read budget is `capture_budget_s` = 1.3 s.
- **Thread** = (app, `thread_scope`), not the window title. A scope equal to the URL keys the thread exactly as the URL did before, so page threads carry over.
- **Conversations and mail: messages, not text.** Each message on screen is stored in `messages` only if its fingerprint is unseen in the thread (a history on screen is stored once, however often it is re-read). An unseen fingerprint whose `content_key` the thread already has, where either reading lacks a time label, is the same message re-read (`reread`, fingerprint only). The rest are labelled against when the watcher last saw the thread (`threads.messages_seen_at`): **new** = sent/received since then (its `at` at or after the last look less 90 s, or, untimed, below the thread's newest stored message on screen; on a first visit, an `at` within 5 min of the capture); **history** = older messages being viewed. One capture row per read that stored anything carries them to the journal.
- **Pages, documents, lists: delta, not snapshot**: keep the thread's latest main text; store only what changed (line diff), exact-hash deduped.
- **List rows carry their own date**: a list row (inbox row, folder entry) that shows a date - its rightmost time label, its tooltip, or a labelled cell "Date modified: 9/23/2026 5:35 PM" - is written `[dated YYYY-MM-DD[ HH:MM]] <row>` (resolved against the capture time). The journal makes each row of a `list` capture its own source, dated by that date.
- **The user's names**: `Settings.user_names` (default Sudhanshu, Mafex, mafex11) plus names the screen shows as the user ("Sudhanshu (you)", an app's own account name), learned per app and kept encrypted in `me_names`. Senders with these names, "You" or "(you)" are `is_me`.
- **No keystroke capture.** Messages are recorded when they appear on screen.
- **Privacy gates (data, not code paths)** in a user-editable config file, applied in the watcher (never in the extractor): the page URL (`rules.check_url(extraction.url)`) before anything is stored, masked lines dropped from every message and body (`rules.is_masked`); skip password/secure fields (UIA `IsPassword`: the structured read never reads their value; the capture is skipped while one has focus), password-manager apps, banking/auth/login URLs and path fragments (start from MaxMi's `Denylist.swift` lists), private/incognito windows (detect from window title/process facts), and a user blocklist of apps/domains. Pause content capture while a full-screen window is in front (a game, an F11 video; the timeline still counts the time, see below) and pause entirely when the session is locked. Meeting apps and sites (Zoom.exe, meet.google.com, zoom.us, Teams meeting paths) are blocked here like banking; the timeline keeps their hours only (`[meetings]`).
- **Budget**: <2% CPU on average, capture < 1.3 s (the structured read; 0.2-0.9 s measured 2026-09-24), never blocks the foreground app; measure and log `capture_health` (content-free: app, trigger, outcome, reason, chars, ms, plus the extraction's `profile`, `kind`, `dropped_chars`, messages on screen / stored new, and `stats` = nodes, read/total ms, source, truncated). A window the OS says is not a user window is logged as `not_user_window:<window class>`.
- **Terminals**: GPU-drawn terminals (Warp) expose only their frame through UIA, so the watcher captures nothing from them. Instead (owner's decision, 2026-09-24) `yuki/memory/warp.py` reads Warp's own command history - see "Terminal history" below. Command output is never read.

## Yuki's own actions

While the agent carries out a request with its hands (from its first action tool - launch, click, type, keys, scroll, PowerShell, open URL - until the request ends; look-only requests never count), it publishes a marker (`yuki/memory/acting.py`): `acting.json` next to the database (request text, start, lane, pid; removed at the end; entries of a dead process are ignored) and sets the named event `Local\YukiMemoryActing-<db digest>` that the service creates. The service's `yuki-memory-acting` thread (woken by the event, or every 2 s) tells the watcher and the timeline.

- Captures triggered during the window, or within `acting_tail_s` (3 s) after it, are stored `by_yuki = 1` with the request (encrypted). Timeline stretches are cut where acting starts and stops; the ones in between are `by_yuki` (migration 7, additive: `captures.by_yuki`, `captures.yuki_request_ciphertext`, `timeline.by_yuki`, `timeline.yuki_request_ciphertext`, `journal.by_yuki`).
- The journal shows such sources as `BY YUKI (at the user's request "...")` and records them as "Yuki, at the user's request '...', opened ..."; facts from them are `by_yuki`.
- The portrait leaves `by_yuki` facts and stretches out entirely (the user's request itself reaches it through conversation memory); episodes list the stretches apart as YUKI ACTING, outside the time use. Conversation memory records a request as a task, never as a taste ("asked for the soba article" is not "interested in Japanese cuisine").

## Timeline: full screen and meetings

The timeline (`yuki/memory/timeline.py`: foreground stretches with app, page, present/away from `GetLastInputInfo`, media) records two kinds of rows that carry less than the rest:

- **Full screen** (`timeline.fullscreen = 1`): a game or a full-screen video is counted, not skipped. While `[pause] when_fullscreen` is on (default), such a row has only process and app, start/end, present vs away and media - no title, no page, no title hook. When the same window showed a site just before going full screen, that host (not the path) carries over, so "watched YouTube full-screen 40 min" is known without reading the page. Entering or leaving full screen (same window, F11) starts a new stretch. Full-screen time counts in aggregates (`fullscreen_s` per activity and in the totals) and in episodes ("played VALORANT for 1h40").
- **Meetings** (`timeline.meeting` = the service name): an app or page in the privacy file's `[meetings]` table (processes, host suffixes, host + path fragments, display names) becomes a meeting row with the app, host and service name, start/end - no title, no path. Content stays blocked by `[apps]`/`[web]`. While a meeting is in front, the ConsentStore microphone users (as in `activity_facts()["microphone"]`; about 30 ms, every 15 s, meetings only) are read: the meeting app using the mic counts as present without input and as `mic_s`. A meeting is its own activity ("in a meeting (Google Meet)"); `aggregate()["meetings"]` joins stretches of one service less than 5 min apart into one span ("in a meeting 15:15-16:04 (Google Meet in Google Chrome), in front 45m, microphone on 44m"). Episodes are told never to guess who was in a meeting or what it was about.
- **Watching**: time while the app in front plays media is `passive_s` (watching or listening) whatever the input says; for a web page, only when its title names the playing media (a background tab playing is not the page in front). `GetLastInputInfo` alone cannot tell watching from doing on this PC: the Logitech G402 mouse (`HID VID_046D PID_C07E`) reports one-count moves every 4-40 s with nobody there (raw-input probe, 2026-09-24 night 2), so "away" is rarely reached until the mouse stops drifting (a matte pad, lower DPI or lift-off distance in G HUB, or unplugging it at night).
- Limits: a controller-only game gives no `GetLastInputInfo` input, so it reads as away unless it plays media. The Teams desktop app is not treated as a meeting (its call windows cannot be told apart without reading titles). A meeting is counted only while its window or tab is in front; the 5-minute join covers short looks elsewhere.

## Terminal history (Warp)

- Source: `%LOCALAPPDATA%\warp\Warp\data\warp.sqlite` (WAL). `WarpReader` in `yuki-memory` opens it every 3 minutes, read-only (`mode=ro` URI, `query_only`, never `immutable`), then closes it. It reads only table `commands`, and only `id, command, exit_code, start_ts, completed_ts (UTC text), pwd, shell, git_branch, session_id, is_agent_executed` - never `blocks`, never output.
- Checkpoint `source_checkpoints('warp.commands')` = the last `commands.id` consumed. The first read starts at the newest command, so earlier history is not journaled. A command without `completed_ts` waits up to 10 min for its exit code; the checkpoint moves only over a contiguous run of taken rows. While memory is paused, `[terminal] enabled = false` or Warp is a blocked app, the checkpoint moves past new commands without reading them.
- Secret rules are data (`[terminal] secret_patterns`: case-insensitive regular expressions matched against the command, its folder and its branch). They cover secret-named assignments and env exports, `setx`/`SetEnvironmentVariable`, config-set of such names, secret-named flags with a value, `Authorization:`/bearer/API-key headers, URLs with credentials, known key shapes (sk-, ghp_, github_pat_, xox*, AKIA, AIza, ABSK, JWTs, private keys) and passwords on the command line. A high-entropy word test (`entropy_min_length` 24, `entropy_min_bits` 3.5) catches the rest; it also drops git hashes and GUIDs. A matching command is dropped whole: never stored, never sent to the model; only its count and the rule index are logged.
- Kept commands go to the journal as a new source kind `terminal`: one thread per folder (`threads.kind = terminal`, scope = the folder), one capture per folder and pass (JSON `{"commands": [...]}`, encrypted, 30-day TTL), written with the checkpoint in one transaction. The journal prompt shows each as `[n] COMMAND <time> in <folder> on branch <b>, pwsh, exit <code>`. It tells Haiku to summarise sequences ("The user ran the Yuki test suite in C:\Users\esska\yuki on branch main; it failed twice, then passed."), never to list commands one by one, to skip routine navigation, and never to record a secret.
- Terminal *time* comes from the foreground timeline (Warp in front), not from Warp's timestamps: most rows never get a `completed_ts` (193 of 264 on 2026-09-24).

## Store

- One SQLite database under `%LOCALAPPDATA%\Yuki\memory\memory.db` (WAL mode). Content columns encrypted with a key protected by Windows DPAPI (`CryptProtectData`, current user). Metadata (timestamps, app, URL host) cleartext for filtering.
- Tables (shape borrowed from MaxMi): `threads(id, app, title, url, scope, kind, messages_seen_at, first_seen, last_seen)`, `captures(id, thread_id, at, trigger, kind, profile, delta_ciphertext, chars, hash)`, `messages(thread_id, capture_id, fingerprint, content_key, sender_ciphertext, is_me, time_label, at, text_ciphertext, first_seen, status new|history|reread, journaled)` unique on (thread_id, fingerprint) - fingerprints and content keys stored as keyed HMACs; past the 30-day TTL a message keeps only its fingerprint -, `me_names(name_key, app, name_ciphertext, first_seen, last_seen)`, `journal(id, at, thread_id, app, fact_ciphertext, importance)`, `journal_vec` (local embeddings). Search = vector similarity + metadata filters (time, app, URL host); keyword queries decrypt the journal rows inside the requested time window and match in memory (the journal is small — facts, not raw text). No plaintext full-text index. `portrait_facts(id, kind, subject, text_ciphertext, valid_from, valid_to, source_ids, confidence)`, `knowhow(id, app, task_kind, text_ciphertext, valid_from, valid_to, source_request)`, `open_loops(id, person, text_ciphertext, status, opened_at, resolved_at)`, `health(...)`, `timeline(..., fullscreen, meeting, mic_s)` and `source_checkpoints(name, value, updated_at)` (migration 5, additive). Migration 6 (additive) adds conversation memory: `conversation_turns`, `conversation_facts`, `session_summaries`, `conversation_batches` and the vector tables `conversation_turn_vec`, `session_summary_vec`, `conversation_fact_vec` (see "Conversation memory").
- **Local embeddings** (no cloud): a small CPU embedding model (e.g. `fastembed` + bge-small ONNX) — measure speed/size.
- Facts are never hard-deleted; contradicted facts get `valid_to` set.

## Journal worker

- Every few minutes (or after N new captures), batch the new captures per thread and ask **Claude Haiku 4.5 on Bedrock** to extract atomic dated facts, each with an importance 1–10. The batch is a list of numbered **sources**: one per conversation message (NEW or HISTORY, sender or "the user", its own date and time) and one per page capture. Each fact cites its source and is stored with that source's time: a history message from three weeks ago yields a fact dated three weeks ago, never "now". History may yield durable facts (who someone is, what was agreed); the prompt forbids recording it as something happening now. A message without its own time is dated by the nearest earlier timed message of the same read. The system prompt names the user ("The user is Sudhanshu, who also appears as Mafex, mafex11, 'You' or a name marked '(you)'"), and facts about the user say "the user", never their name as a third party. The calendar given for relative dates covers every source day. Reuse MaxMi's extraction prompt (`Sources/MaxMiRelay/ExtractPrompt.swift`) and its untrusted-data fences (`===BEGIN/END_UNTRUSTED_DATA_<uuid>===`) adapted to Claude. Captured text is data, never instructions.
- Sources also include **terminal commands** from Warp (see "Terminal history"): one source per command, dated by its start. Facts summarise sequences and never carry secrets.
- Log every model call with tokens and cost (reuse `Settings.pricing`).

## Portrait worker

- Nightly (first idle moment after a configurable hour) and weekly: read the day's/week's journal (by id checkpoint, so facts dated weeks back by their messages are still consumed) plus current portrait facts, told who the user is; the model returns ADD / UPDATE (supersede) / INVALIDATE / NOOP operations per fact (Mem0 pattern), each citing journal ids. Apply bi-temporally.
- Render a one-page portrait text (hard cap ~1500 tokens) cached for Yuki.

## Conversation memory

Memory of Yuki's own conversations with the user: rules the user sets mid-chat ("call me babe always"), preferences about how Yuki talks, what the user handed Yuki for later or Yuki promised, facts the user reveals, and continuity across restarts. Design basis: `docs/research/conversation-memory.md` (Mem0 diff operations, Graphiti bi-temporal invalidation, end-of-session summaries, a small always-attached block and just-in-time recall). Code: `yuki/memory/conversations.py` (worker, in `yuki-memory`), `yuki/memory/api.py` (Yuki side), `yuki/memory/store.py` (migration 6).

**Tables (migration 6, additive).**
- `conversation_turns(id, session_id, request_id, at, user_ciphertext, reply_ciphertext, actions_ciphertext, outcome, extracted, extract_attempts, extract_batch_id, summary_id, summary_attempts, created_at)`: one row per exchange. The id is allocated by the writer from a microsecond clock, so it is known before the write lands. Kept forever: this is the raw conversation log (no capture TTL).
- `conversation_facts(id, kind rule|preference|commitment|fact, subject_ciphertext, text_ciphertext, quote_ciphertext, status active|done|revoked|superseded, valid_from, valid_to, due_at, source_turn_ids, origin model|user, created_at, expired_at, superseded_by, end_note_ciphertext, batch_id)`: bi-temporal. Nothing is deleted: an UPDATE supersedes the old version, and a revoke or done ends it, keeping `valid_to` and the words or reason that ended it. `quote` holds the user's own words for rules and preferences. `fact` is reserved: durable facts go to the journal (below).
- `session_summaries(id, session_id, started_at, ended_at, turn_count, text_ciphertext, batch_id, created_at)`. The spec's start/end columns are named `started_at`/`ended_at` because `end` is an SQL keyword.
- `conversation_batches`: content-free accounting per model call (purpose extract|summary, tokens, cost, latency, ops applied/noop/rejected, journal facts, outcome).
- Vector tables `conversation_turn_vec`, `session_summary_vec`, `conversation_fact_vec`: local 384-d embeddings, encrypted like the rest.

**Writing a turn (Yuki's process).** `MemoryClient.log_turn(session_id, request_id, at, user_text, reply_text, actions, outcome)` encrypts the exchange and inserts it through a dedicated SQLite connection that waits at most 8 ms for the write lock. If another writer holds the lock longer, the row goes to a background writer: turns queue behind it in order and the id is returned at once. It then sets the named auto-reset event `Local\YukiMemoryTurns-<db digest>`, which wakes the worker across processes. It never raises; it returns 0 if the turn could not be stored. Measured 2026-09-24: 0.5 ms per turn (2.3 ms for the first, which opens the connection); with the database locked, 10.5 ms, then 0.3 ms for each queued turn.

**Extraction (worker, Haiku 4.5).**
- *When it runs.* When at least 3 turns are pending, or the oldest pending turn was queued 60 s ago. A new turn wakes the worker at once; otherwise it waits on a condition, with a timeout set to the next due time.
- *Batches.* Pending turns are batched per session, at most 12 turns and 16,000 characters each.
- *What the model sees.* The current active items (C<n>), items that ended in the last 14 days (never to be re-added), the last 12 journal facts from conversations (so none is repeated), up to 4 earlier exchanges of the session as context only, the new exchanges numbered [n], and a calendar for relative dates.
- *What it returns.* One forced, strict `record_conversation_memory` call. `operations` are ADD / UPDATE / INVALIDATE (`end_as` done|revoked) / NOOP, each citing exchanges. `journal_facts` are atomic third-person facts, each with an importance and an exchange.
- *Validation, in code.*
  - Rules and preferences need a quote that is a span of the cited exchange's user text, after normalising case, whitespace, quote marks and dashes. A paraphrase or an inferred rule is rejected.
  - Revoking a rule or preference needs the user's words too.
  - Only a commitment can be `done`.
  - The target must be active. Evidence older than the item is rejected. Each item gets one operation per call.
  - A commitment's quote is dropped when Yuki, not the user, said it.
  - `due` ("YYYY-MM-DD[ HH:MM]") becomes `due_at`.
- *Durable facts go to the journal,* dated by their exchange (app "Yuki", thread `yuki:conversation`, kind `yuki_chat`). The portrait worker consumes them like any other fact. There is no parallel fact store.
- *One transaction* writes the batch row, the item changes, the journal facts, the turns marked extracted and all vectors.
- *Failures.* A failed call bumps `extract_attempts`; after 3 the turn is given up. After a failure the worker waits 5 min.

**Session summaries.** A session's exchanges split where 30 minutes passed without one. A run has ended when a later run of the same session exists, when 30 minutes passed since its last exchange, or when a later exchange belongs to another session id. Each ended run gets a 2-4 sentence, past-tense, third-person summary (forced strict `save_session_summary`). The model sees the exchanges (the latest 24,000 characters) and, as context, the session's previous summary. The summary is embedded and stored. Two known limits: two Yuki front ends running at once with different session ids would split each other's runs, and the summary of the last session appears only once it has ended, so `resume_context` right after a restart shows its exchanges without a summary.

**What Yuki attaches (hard budgets in code).**
- `standing_context()`: the active rules and preferences, each in the user's own words with the date it was said, and the open commitments with due and asked dates. Hard cap 1,200 characters (~300 tokens). Over the cap, the oldest items are dropped whole. An item is cut only after a whole sentence, and dropped if even its first sentence does not fit. It returns `""` when there is nothing. It is meant to go after the cached portrait prefix on every request.
- `resume_context(within_hours=6)`: when the newest exchange is at most 6 h old, a header (the session span, the number of exchanges, how long ago it ended), that session's latest summary, and its last 6 exchanges, each with the user's words, Yuki's reply and one line of actions and outcome. Messages are cut after whole sentences, or shown as "a long message of N characters" when the first sentence alone is too long. Hard cap 4,000 characters (~1,000 tokens), dropping the oldest exchange first. It returns `None` otherwise.
- `remember_rule(text, source_turn=None)`: stores the rule at once in the user's words (origin `user`). It supersedes an active rule or preference with identical words or embedding cosine of at least 0.85.
- `revoke_rule(text)`: finds the active rule or preference containing every word of `text` (the newest if several). Otherwise it takes the nearest by embedding, at a cosine of at least 0.45. Measured 2026-09-24: a withdrawal in other words scored 0.50-0.57, and unrelated text 0.03-0.29. It marks the rule revoked and returns its id, or 0.
- `recall(...)` now also returns `kind: "chat"` hits, one exchange each (`The user said: "..." Yuki replied: "..." Yuki did: ...`), and `kind: "session"` hits (summaries, with `until`), each dated. Both use vector plus keyword search with RRF, as for facts and episodes, and are left out when `app` is given.

**Portrait.** The render gets a RELATIONSHIP DATA block: the active rules and preferences in the user's words, and up to 8,000 characters of the last 14 days of session summaries, newest kept. It writes a short Relationship section on how the user likes Yuki to talk, and on running themes that span more than one session. That block feeds this section only: every other section still comes from the facts. A new or ended rule, or a new summary, since the last render re-renders the portrait on the next run.

**Pause.** The `paused` flag does not stop conversation memory. It stops screen capture. Turns the user types to Yuki are still stored and extracted, just as the journal worker finishes what was captured before a pause. Revisit this if the user wants pause to mean "remember nothing".

**Costs** (measured 2026-09-24, synthetic two-session history, list prices):

| Call | Tokens (in / out) | Cost |
|---|---|---|
| Extraction, 5 exchanges | 3.7k / 0.3k | $0.0052 |
| Extraction, 3 exchanges | 3.6k / 0.2k | $0.0046 |
| Summary | 1.7-1.8k / 0.1-0.16k | $0.0022-0.0026 |
| Portrait render with Relationship (Sonnet) | 2.1k / 0.24k | $0.0066 |

Logs: `logs/memory/conversations-YYYYMMDD.jsonl`, with usage, cost, latency and stop reason in the clear and the request, response and operations encrypted. Status cost (`memory_cost_today_usd`) includes these calls. `yuki-memory --no-conversations` turns the worker off; turns are still stored.

## Yuki integration

- The current portrait text + relevant know-how are attached to every request as labelled context ("[What Yuki knows about the user, from memory]"); after it (outside the cached prefix) `standing_context()`, and at the start of a new session `resume_context()`. Every exchange goes to `log_turn(...)`.
- Tools: `recall(query, since?, until?, app?, person?)` → journal facts, episodes, past chats and sessions; `remember_how(app, text)` → know-how write after a success; `update_portrait(text)` → a user-confirmed correction; `remember_rule(text)` / `revoke_rule(text)` → a standing rule the user just gave or withdrew, applied at once.
- When a choice is driven by the portrait, Yuki says why in its reply.

## Build phases

1. **A**: watcher + store + journal (this contract's first half). Deliverable: `uv run yuki-memory` runs quietly; a `scripts/memory_report.py` prints today's journal and capture health.
2. **B**: portrait + know-how + Yuki integration.
3. **C**: open loops / to-dos, weekly review, MCP server for other agents.
