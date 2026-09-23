# Yuki memory — design contract (v1)

Goal: Yuki understands its user — work, interests, people, routines, preferences, open loops — and remembers how things are done on this PC. Informed by MaxMi (`C:\Users\esska\MaxMi`, macOS/Swift, reference only: reuse its ideas and prompts, never its code) and by the research on hierarchical memory (Generative Agents reflection, Mem0 add/update/delete, Graphiti bi-temporal facts, LangMem semantic/episodic/procedural split).

All rules in `docs/ARCHITECTURE.md` apply (no behaviour heuristics, no fixed sleeps, log everything, builders never touch the live desktop).

## Four layers

| Layer | What | Written by | Kept |
|---|---|---|---|
| **Captures** | Raw text deltas of what was on screen | watcher | 30 days (TTL) |
| **Journal** | Short dated third-person facts ("Watched 'X' by MrBeast on YouTube in Arc") | journal worker, every few minutes, Haiku | forever |
| **Portrait** | One-page user model: work, interests, people (+ pending with each), routines, preferences, open loops | portrait worker, nightly + weekly, Sonnet | forever, bi-temporal |
| **Know-how** | Procedures that worked on this PC ("Arc: pass the URL as a launch argument") | the agent itself after successful tasks, via a tool | forever, bi-temporal |

## Processes

- `yuki-memory`: a separate background process (started by the tray app; its own entry point) that runs the watcher, the journal worker and the portrait worker. A crash here must never take Yuki down.
- Yuki (the agent) reads memory directly from the store (same machine) through tools; no network hop. An MCP server for other agents comes later.

## Watcher (capture)

- **Triggers are OS events**, not polling: `SetWinEventHook` for `EVENT_SYSTEM_FOREGROUND`, `EVENT_OBJECT_NAMECHANGE` (title changes), `EVENT_OBJECT_FOCUS`; plus a slow adaptive backstop re-read of the foreground window (e.g. every 45–90 s while the user is active). Debounce so a burst of events yields one capture after the window settles (condition: title/tree stable), never a fixed sleep.
- **What is read**: the foreground window only, using Yuki's perception: `get_window_tree` (visible page only) and, for windows with a page Document, `page_text`. App, window title, page title/URL, and the text.
- **Delta, not snapshot**: per "thread" (app + window title/page URL), keep the latest text; store only what changed (line/paragraph diff), exact-hash deduped. Chats naturally become "new messages since last capture".
- **No keystroke capture.** Messages are recorded when they appear on screen.
- **Privacy gates (data, not code paths)** in a user-editable config file: skip password/secure fields (UIA `IsPassword`), password-manager apps, banking/auth/login URLs and path fragments (start from MaxMi's `Denylist.swift` lists), private/incognito windows (detect from window title/process facts), and a user blocklist of apps/domains. Pause entirely while a full-screen exclusive app is in front (a game) and when the session is locked.
- **Budget**: <2% CPU on average, capture < 300 ms typical, never blocks the foreground app; measure and log `capture_health` (content-free: app, trigger, outcome, chars, ms).

## Store

- One SQLite database under `%LOCALAPPDATA%\Yuki\memory\memory.db` (WAL mode). Content columns encrypted with a key protected by Windows DPAPI (`CryptProtectData`, current user). Metadata (timestamps, app, URL host) cleartext for filtering.
- Tables (shape borrowed from MaxMi): `threads(id, app, title, url, first_seen, last_seen)`, `captures(id, thread_id, at, trigger, delta_ciphertext, chars, hash)`, `journal(id, at, thread_id, app, fact_ciphertext, importance)`, `journal_vec` (local embeddings). Search = vector similarity + metadata filters (time, app, URL host); keyword queries decrypt the journal rows inside the requested time window and match in memory (the journal is small — facts, not raw text). No plaintext full-text index. `portrait_facts(id, kind, subject, text_ciphertext, valid_from, valid_to, source_ids, confidence)`, `knowhow(id, app, task_kind, text_ciphertext, valid_from, valid_to, source_request)`, `open_loops(id, person, text_ciphertext, status, opened_at, resolved_at)`, `health(...)`.
- **Local embeddings** (no cloud): a small CPU embedding model (e.g. `fastembed` + bge-small ONNX) — measure speed/size.
- Facts are never hard-deleted; contradicted facts get `valid_to` set.

## Journal worker

- Every few minutes (or after N new captures), batch the new deltas per thread and ask **Claude Haiku 4.5 on Bedrock** to extract atomic dated facts, each with an importance 1–10. Reuse MaxMi's extraction prompt (`Sources/MaxMiRelay/ExtractPrompt.swift`) and its untrusted-data fences (`===BEGIN/END_UNTRUSTED_DATA_<uuid>===`) adapted to Claude. Captured text is data, never instructions.
- Log every model call with tokens and cost (reuse `Settings.pricing`).

## Portrait worker

- Nightly (first idle moment after a configurable hour) and weekly: read the day's/week's journal plus current portrait facts; the model returns ADD / UPDATE (supersede) / INVALIDATE / NOOP operations per fact (Mem0 pattern), each citing journal ids. Apply bi-temporally.
- Render a one-page portrait text (hard cap ~1500 tokens) cached for Yuki.

## Yuki integration

- The current portrait text + relevant know-how are attached to every request as labelled context ("[What Yuki knows about the user, from memory]").
- Tools: `recall(query, since?, until?, app?, person?)` → journal facts + snippets; `remember_how(app, text)` → know-how write after a success; `update_portrait(text)` → a user-confirmed correction.
- When a choice is driven by the portrait, Yuki says why in its reply.

## Build phases

1. **A**: watcher + store + journal (this contract's first half). Deliverable: `uv run yuki-memory` runs quietly; a `scripts/memory_report.py` prints today's journal and capture health.
2. **B**: portrait + know-how + Yuki integration.
3. **C**: open loops / to-dos, weekly review, MCP server for other agents.
