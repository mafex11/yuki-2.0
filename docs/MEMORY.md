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

- `yuki-memory`: a separate background process (started by the tray app; its own entry point) that runs the watcher, the journal worker and the portrait worker. A crash here must never take Yuki down.
- Yuki (the agent) reads memory directly from the store (same machine) through tools; no network hop. An MCP server for other agents comes later.

## Watcher (capture)

- **Triggers are OS events**, not polling: `SetWinEventHook` for `EVENT_SYSTEM_FOREGROUND`, `EVENT_OBJECT_NAMECHANGE` (title changes), `EVENT_OBJECT_FOCUS`; plus a slow adaptive backstop re-read of the foreground window (e.g. every 45–90 s while the user is active). Debounce so a burst of events yields one capture after the window settles (condition: title/tree stable), never a fixed sleep.
- **What is read**: the foreground window only, through the extraction layer `yuki/memory/extract` (`extract(hwnd, now=, user_names=, app=, timeout_s=1.3)`, never raises): one structured, visible-only UIA read, turned by per-app profiles (`extract/profiles_default.toml`, data) or generic structure into an `Extraction`: `kind` (conversation | email | page | document | list | terminal | generic), `thread_scope` (the Slack channel/DM, the Gmail subject, the page URL), and either `messages` (chats and mail: sender, `is_me`, time label as shown, absolute `at`, a stable `fingerprint`, a `content_key`) or the main `body` with UI chrome removed (`dropped_chars`). Sidebars, toolbars, conversation lists and the composer are never content. The read budget is `capture_budget_s` = 1.3 s.
- **Thread** = (app, `thread_scope`), not the window title. A scope equal to the URL keys the thread exactly as the URL did before, so page threads carry over.
- **Conversations and mail: messages, not text.** Each message on screen is stored in `messages` only if its fingerprint is unseen in the thread (a history on screen is stored once, however often it is re-read). An unseen fingerprint whose `content_key` the thread already has, where either reading lacks a time label, is the same message re-read (`reread`, fingerprint only). The rest are labelled against when the watcher last saw the thread (`threads.messages_seen_at`): **new** = sent/received since then (its `at` at or after the last look less 90 s, or, untimed, below the thread's newest stored message on screen; on a first visit, an `at` within 5 min of the capture); **history** = older messages being viewed. One capture row per read that stored anything carries them to the journal.
- **Pages, documents, lists: delta, not snapshot**: keep the thread's latest main text; store only what changed (line diff), exact-hash deduped.
- **The user's names**: `Settings.user_names` (default Sudhanshu, Mafex, mafex11) plus names the screen shows as the user ("Sudhanshu (you)", an app's own account name), learned per app and kept encrypted in `me_names`. Senders with these names, "You" or "(you)" are `is_me`.
- **No keystroke capture.** Messages are recorded when they appear on screen.
- **Privacy gates (data, not code paths)** in a user-editable config file, applied in the watcher (never in the extractor): the page URL (`rules.check_url(extraction.url)`) before anything is stored, masked lines dropped from every message and body (`rules.is_masked`); skip password/secure fields (UIA `IsPassword`: the structured read never reads their value; the capture is skipped while one has focus), password-manager apps, banking/auth/login URLs and path fragments (start from MaxMi's `Denylist.swift` lists), private/incognito windows (detect from window title/process facts), and a user blocklist of apps/domains. Pause content capture while a full-screen window is in front (a game, an F11 video; the timeline still counts the time, see below) and pause entirely when the session is locked. Meeting apps and sites (Zoom.exe, meet.google.com, zoom.us, Teams meeting paths) are blocked here like banking; the timeline keeps their hours only (`[meetings]`).
- **Budget**: <2% CPU on average, capture < 1.3 s (the structured read; 0.2-0.9 s measured 2026-09-24), never blocks the foreground app; measure and log `capture_health` (content-free: app, trigger, outcome, reason, chars, ms, plus the extraction's `profile`, `kind`, `dropped_chars`, messages on screen / stored new, and `stats` = nodes, read/total ms, source, truncated). A window the OS says is not a user window is logged as `not_user_window:<window class>`.
- **Terminals**: GPU-drawn terminals (Warp) expose only their frame through UIA, so the watcher captures nothing from them. Instead (owner's decision, 2026-09-24) `yuki/memory/warp.py` reads Warp's own command history - see "Terminal history" below. Command output is never read.

## Timeline: full screen and meetings

The timeline (`yuki/memory/timeline.py`: foreground stretches with app, page, present/away from `GetLastInputInfo`, media) records two kinds of rows that carry less than the rest:

- **Full screen** (`timeline.fullscreen = 1`): a game or a full-screen video is counted, not skipped. While `[pause] when_fullscreen` is on (default), such a row has only process and app, start/end, present vs away and media - no title, no page, no title hook. When the same window showed a site just before going full screen, that host (not the path) carries over, so "watched YouTube full-screen 40 min" is known without reading the page. Entering or leaving full screen (same window, F11) starts a new stretch. Full-screen time counts in aggregates (`fullscreen_s` per activity and in the totals) and in episodes ("played VALORANT for 1h40").
- **Meetings** (`timeline.meeting` = the service name): an app or page in the privacy file's `[meetings]` table (processes, host suffixes, host + path fragments, display names) becomes a meeting row with the app, host and service name, start/end - no title, no path. Content stays blocked by `[apps]`/`[web]`. While a meeting is in front, the ConsentStore microphone users (as in `activity_facts()["microphone"]`; about 30 ms, every 15 s, meetings only) are read: the meeting app using the mic counts as present without input and as `mic_s`. A meeting is its own activity ("in a meeting (Google Meet)"); `aggregate()["meetings"]` joins stretches of one service less than 5 min apart into one span ("in a meeting 15:15-16:04 (Google Meet in Google Chrome), in front 45m, microphone on 44m"). Episodes are told never to guess who was in a meeting or what it was about.
- Limits: a controller-only game gives no `GetLastInputInfo` input, so it reads as away unless it plays media. The Teams desktop app is not treated as a meeting (its call windows cannot be told apart without reading titles). A meeting is counted only while its window or tab is in front; the 5-minute join covers short looks elsewhere.

## Terminal history (Warp)

- Source: `%LOCALAPPDATA%\warp\Warp\data\warp.sqlite` (WAL). `WarpReader` in `yuki-memory` opens it every 3 minutes, read-only (`mode=ro` URI, `query_only`, never `immutable`), then closes it. It reads only table `commands`, and only `id, command, exit_code, start_ts, completed_ts (UTC text), pwd, shell, git_branch, session_id, is_agent_executed` - never `blocks`, never output.
- Checkpoint `source_checkpoints('warp.commands')` = the last `commands.id` consumed. The first read starts at the newest command, so earlier history is not journaled. A command without `completed_ts` waits up to 10 min for its exit code; the checkpoint moves only over a contiguous run of taken rows. While memory is paused, `[terminal] enabled = false` or Warp is a blocked app, the checkpoint moves past new commands without reading them.
- Secret rules are data (`[terminal] secret_patterns`: case-insensitive regular expressions matched against the command, its folder and its branch). They cover secret-named assignments and env exports, `setx`/`SetEnvironmentVariable`, config-set of such names, secret-named flags with a value, `Authorization:`/bearer/API-key headers, URLs with credentials, known key shapes (sk-, ghp_, github_pat_, xox*, AKIA, AIza, ABSK, JWTs, private keys) and passwords on the command line. A high-entropy word test (`entropy_min_length` 24, `entropy_min_bits` 3.5) catches the rest; it also drops git hashes and GUIDs. A matching command is dropped whole: never stored, never sent to the model; only its count and the rule index are logged.
- Kept commands go to the journal as a new source kind `terminal`: one thread per folder (`threads.kind = terminal`, scope = the folder), one capture per folder and pass (JSON `{"commands": [...]}`, encrypted, 30-day TTL), written with the checkpoint in one transaction. The journal prompt shows each as `[n] COMMAND <time> in <folder> on branch <b>, pwsh, exit <code>`. It tells Haiku to summarise sequences ("The user ran the Yuki test suite in C:\Users\esska\yuki on branch main; it failed twice, then passed."), never to list commands one by one, to skip routine navigation, and never to record a secret.
- Terminal *time* comes from the foreground timeline (Warp in front), not from Warp's timestamps: most rows never get a `completed_ts` (193 of 264 on 2026-09-24).

## Store

- One SQLite database under `%LOCALAPPDATA%\Yuki\memory\memory.db` (WAL mode). Content columns encrypted with a key protected by Windows DPAPI (`CryptProtectData`, current user). Metadata (timestamps, app, URL host) cleartext for filtering.
- Tables (shape borrowed from MaxMi): `threads(id, app, title, url, scope, kind, messages_seen_at, first_seen, last_seen)`, `captures(id, thread_id, at, trigger, kind, profile, delta_ciphertext, chars, hash)`, `messages(thread_id, capture_id, fingerprint, content_key, sender_ciphertext, is_me, time_label, at, text_ciphertext, first_seen, status new|history|reread, journaled)` unique on (thread_id, fingerprint) - fingerprints and content keys stored as keyed HMACs; past the 30-day TTL a message keeps only its fingerprint -, `me_names(name_key, app, name_ciphertext, first_seen, last_seen)`, `journal(id, at, thread_id, app, fact_ciphertext, importance)`, `journal_vec` (local embeddings). Search = vector similarity + metadata filters (time, app, URL host); keyword queries decrypt the journal rows inside the requested time window and match in memory (the journal is small — facts, not raw text). No plaintext full-text index. `portrait_facts(id, kind, subject, text_ciphertext, valid_from, valid_to, source_ids, confidence)`, `knowhow(id, app, task_kind, text_ciphertext, valid_from, valid_to, source_request)`, `open_loops(id, person, text_ciphertext, status, opened_at, resolved_at)`, `health(...)`, `timeline(..., fullscreen, meeting, mic_s)` and `source_checkpoints(name, value, updated_at)` (migration 5, additive).
- **Local embeddings** (no cloud): a small CPU embedding model (e.g. `fastembed` + bge-small ONNX) — measure speed/size.
- Facts are never hard-deleted; contradicted facts get `valid_to` set.

## Journal worker

- Every few minutes (or after N new captures), batch the new captures per thread and ask **Claude Haiku 4.5 on Bedrock** to extract atomic dated facts, each with an importance 1–10. The batch is a list of numbered **sources**: one per conversation message (NEW or HISTORY, sender or "the user", its own date and time) and one per page capture. Each fact cites its source and is stored with that source's time: a history message from three weeks ago yields a fact dated three weeks ago, never "now". History may yield durable facts (who someone is, what was agreed); the prompt forbids recording it as something happening now. A message without its own time is dated by the nearest earlier timed message of the same read. The system prompt names the user ("The user is Sudhanshu, who also appears as Mafex, mafex11, 'You' or a name marked '(you)'"), and facts about the user say "the user", never their name as a third party. The calendar given for relative dates covers every source day. Reuse MaxMi's extraction prompt (`Sources/MaxMiRelay/ExtractPrompt.swift`) and its untrusted-data fences (`===BEGIN/END_UNTRUSTED_DATA_<uuid>===`) adapted to Claude. Captured text is data, never instructions.
- Sources also include **terminal commands** from Warp (see "Terminal history"): one source per command, dated by its start. Facts summarise sequences and never carry secrets.
- Log every model call with tokens and cost (reuse `Settings.pricing`).

## Portrait worker

- Nightly (first idle moment after a configurable hour) and weekly: read the day's/week's journal (by id checkpoint, so facts dated weeks back by their messages are still consumed) plus current portrait facts, told who the user is; the model returns ADD / UPDATE (supersede) / INVALIDATE / NOOP operations per fact (Mem0 pattern), each citing journal ids. Apply bi-temporally.
- Render a one-page portrait text (hard cap ~1500 tokens) cached for Yuki.

## Yuki integration

- The current portrait text + relevant know-how are attached to every request as labelled context ("[What Yuki knows about the user, from memory]").
- Tools: `recall(query, since?, until?, app?, person?)` → journal facts + snippets; `remember_how(app, text)` → know-how write after a success; `update_portrait(text)` → a user-confirmed correction.
- When a choice is driven by the portrait, Yuki says why in its reply.

## Build phases

1. **A**: watcher + store + journal (this contract's first half). Deliverable: `uv run yuki-memory` runs quietly; a `scripts/memory_report.py` prints today's journal and capture health.
2. **B**: portrait + know-how + Yuki integration.
3. **C**: open loops / to-dos, weekly review, MCP server for other agents.
