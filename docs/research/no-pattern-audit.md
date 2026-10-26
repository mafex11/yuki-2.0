# No-pattern audit (2026-09-25)

A read-only audit of `yuki/` against the rule: **no hard coding, regex, pattern matching or keyword lists for behaviour**. What the user means, what something is, whose it is, how to reply, where to route, and what counts as a secret, a joke or a task must come from models (prompts, tool schemas) or from measured signals. Parsing machine formats is fine.

Scope: `yuki/agent`, `yuki/memory` (all, incl. `extract/`), `yuki/perception`, `yuki/actions`, `yuki/voice`, `yuki/ui`, `yuki/mcp`, `config.py`, `cli.py`, and the TOML defaults (`memory/privacy_default.toml`, `memory/extract/profiles_default.toml`). Method: grep for `re.*`, `fnmatch`, `startswith/endswith`, `in (...)`, string-literal `in`, word/app/site lists and alias maps, then read the code around every hit.

Verdicts:

- **BEHAVIOUR**: violates the rule.
- **Borderline**: defensible, with the reason stated.
- **PLUMBING / PARSING**: fine.

## Summary

| Area | Violations | Borderline | Plumbing reviewed |
|---|---|---|---|
| `memory/secret_filter.py` | 8 detector groups | 5 | 4 |
| `memory/warp.py` + `[terminal]` TOML | 1 | 0 | 4 |
| `memory/privacy.py` + privacy TOML (content gates) | 3 | 3 | 6 |
| Meetings and calls (TOML lists, `timeline.py`, `calls.py`) | 2 | 0 | 2 |
| Extraction (`profiles_default.toml` defaults, `extract/conversation.py`) | 1 | 2 | ~12 |
| Memory APIs (`api.py` `revoke_rule`, `nudges.py` `complete_item`) | 2 | 4 | ~20 |
| `actions/input.py` `_KEY_ALIASES` | 1 (8 entries) | 1 | 4 |
| Rest of actions + perception | 0 | 4 | ~12 |
| agent, voice, ui, mcp, config, cli | 0 | 5 | ~22 |
| **Total** | **18** | **~24** | **~85** |

The agent loop, voice, UI and MCP are clean. No code there reads the user's words or the model's reply to decide routing, interruption or when to speak. Those decisions come from tool calls (the mouth's `intent`), job state, timings, or Deepgram's end-of-turn model. The violations are concentrated in **memory**: what counts as a secret, a meeting, a call or "me", and which rule or to-do the user meant.

### Ranked by how much behaviour they drive

1. **Secret filter language grammar** (`secret_filter.py` `_labelled`, `_otp`, PIN, key labels, card and account words, secret-word gating). It runs on every capture (~8 a minute), every call utterance, every chat turn and every log line, and decides what is stored and sent to models. Fix: section A.
2. **`revoke_rule` / `complete_todo` word matching** (`memory/api.py:805-810`, `memory/nudges.py:622-628`). The code picks which standing rule or to-do the user meant: the newest item whose text contains *every word* of the model's phrase. It is a substring test (`t in text`), so "me" matches "name". Fix: ids in the standing block and in the to-do list; the tools take `id` only.
3. **Meetings = a TOML host/app list** (`[meetings]`, `timeline.py:225-275`, used at 1494 and 1574). It feeds the timeline, calls, and the coach, resume and question quiet rules. Fix: a measured signal (mic plus render session or camera held by the foreground app).
4. **Calls = a TOML list of communication apps** (`[calls] apps`, `calls.py:1441`). Decides whether a call is noticed and offered for recording at all; browser calls and unlisted apps are missed. Fix: the same measured signal.
5. **"Is this message mine" word lists** (`me_senders` "You"/"Me", `me_suffixes` "(you)", `extract/conversation.py:263-296`). Sets `is_me` on every generic-chat message, which the journal and portrait attribute from. Fix: per-app anchors (the sanctioned TOML place) plus the journal model.
6. **Warp `secret_patterns`** (22 regexes in TOML). Decides which terminal commands are dropped. Fix: the same local tagger as the secret filter.
7. **Privacy classifiers by keyword**: `blocked_path_fragments` (/login, /otp...), adult `host_substrings` and `query_terms`. They decide which pages are never read. Fix: the measured "a password field is present" probe, and deletion (the explicit denylists stay).
8. **Key alias guesses** (`input.py:357-391`: `play`/`mediapause`->playpause toggle, `next`->nexttrack, `cmd`->win, `plus`/`minus`->numpad). They occasionally press the wrong key. Fix: delete them; the schema and the error message already give the canonical names.

## A. Secret filter (`yuki/memory/secret_filter.py`, 954 lines, ~75 compiled regexes)

What it guards: every entry point redacts before storing or sending. Call sites: `memory/watcher.py:796` (every capture, ~8 a minute, before diffing), `memory/transcribe.py:464` and `memory/store.py:5058` (call utterances), `memory/api.py:299,384-385,590-592,752` (rules, know-how, chat turns, to-dos), `memory/acting.py:142`, `memory/warp.py:167` (terminal commands), `memory/scrub_secrets.py:86-94` (maintenance), `log/events.py:286-291` (every session-log line, in the tray/agent process, not in the memory service) and `log/requests.py:96` (the request ledger).

It is not one thing. Split by detector family:

| Lines | Detector | Verdict | Why |
|---|---|---|---|
| 280-316 `_KNOWN` | Issuer key formats: AWS `AKIA`, `sk-ant-`, `ghp_`, `xox?-`, `AIza`, JWT `eyJ..eyJ..`, PEM/PuTTY headers | Borderline: PARSING | Each one is a documented machine format with a fixed prefix and length, not a guess about language. It is the same kind of thing as parsing a JWT or a PEM block. It still hardcodes vendors, and it misses any vendor not on the list. |
| 319, 340-346 `_RE_AUTH` | `Bearer x` / `Basic x` / `Token x` | Borderline: PARSING | An HTTP `Authorization` scheme is a machine format. `token x` in prose is not one, and matches anyway. |
| 321, 347-355 `_RE_URL_CREDS` | `scheme://user:pass@host` | PARSING | This is URL userinfo. `urllib.parse.urlsplit(...).password` gives the same thing without a regex. |
| 636-704 cards | Luhn, IIN prefix ranges, 4-4-4-4 grouping | PARSING (checksum), BEHAVIOUR (the parts in the next cell) | Luhn and IIN are specs. The card-word proximity list `_RE_CARD_WORD` (card, visa, rupay, debit, कार्ड...) is a keyword list that decides whether the digits are a card. |
| 364-416, 419-569 `_labelled` | Password labels in English, Hinglish and Devanagari (`password|pass|pwd|पासवर्ड`), separators (`is|was|hai|h|he|है`), qualifiers (`for|of|ka|ki|ke|da|di`), possessives (`mera|meri|uska|tera|apna`), the `_NOT_VALUE` list (`your|my|the|changeme|example|os.environ|req|props|...`), the `"pass"` verb exception (475), `_prose`, combo words (`user|login|id|email|cred`), login anchors, `_RE_LABEL_AT_END` | **BEHAVIOUR** | Hand-written natural-language grammar that decides what the user meant ("is this word a password?"). This is the core of what the rule forbids, and it is the biggest part of the file. |
| 397-398, 510-516 PIN | `atm|upi|card|debit|net banking|login|m` before `pin`; 4 digits alone vs 6 (postal PINs) | **BEHAVIOUR** | A keyword list plus a length rule about Indian postal codes. |
| 399-401, 517-532 `_RE_KEY` | `api key|secret|token|key` label + value; kind chosen by substring (531) | **BEHAVIOUR** | A keyword label decides the value is a secret and what kind it is. |
| 385-388, 533-544 flags | `--password`, `--token`, `-u ... -p` (mysql) | Borderline: PARSING | CLI arguments are a machine format, but the flag names are a list of secret words. |
| 576-629 `_otp` | OTP words (`otp|verification code|2fa|access code|ओटीपी`, 24 anchors), `_RE_WEAK_BEFORE` (`your|the|use|enter|yeh|mera`), `_RE_OTP_USE` (`use N to sign in`), `_year` (19xx/20xx is not a code) | **BEHAVIOUR** | Keyword and phrase lists that decide whether a number is a one-time code. |
| 648-654, 693-703 bank | Account labels (`a/c|acc|khata|खाता|अकाउंट`) + 9-18 digits; IFSC shape after `ifsc` or next to an account | Mixed | The IFSC shape (`AAAA0XXXXXX`) is a published machine format. The labels are keywords. |
| 711-847 `_entropy` | Shannon entropy >= 3.5 bits/char, length >= 20, char-class mix | PLUMBING (measured signal) | Entropy is a numeric measurement. |
| 719-728, 810-846 | ...but a candidate is only kept after a secret word (`_SECRET_ANCHORS`: key, secret, token, pass, cookie, session, webhook...) within 48 chars | **BEHAVIOUR** | A keyword list gates the measured signal. |
| 723-724 `_RE_SECRET_PARAM` | Query parameter names containing key/token/sig/session | **BEHAVIOUR** | A keyword list on parameter names (the parsing itself would be `parse_qsl`). |
| 731-743 `_identifier`, `_hostname` | A token is a name when its word parts average 3+ letters | Borderline | A hand-tuned heuristic that stands in for "this is a name, not a secret". |
| 760-767 `_kind_for` | Placeholder kind from substrings | Borderline | It only labels the placeholder. It does not decide whether to hide. |
| 99-100, 881 `_PLACEHOLDER_RE` | Recognises Yuki's own `[password hidden]` so a second pass leaves it alone | PARSING | Yuki's own machine format. |

Count: 8 BEHAVIOUR detector groups, 5 borderline, 4 PARSING.

### Alternatives assessed

Requirements: nothing leaves the PC, nothing is stored before it is checked, and the check is light. It runs on about 8 captures a minute (up to 20k characters each, most much smaller), on every log line in the tray process, and on every call utterance. Current cost: p50 0.07 ms, p99 1.7 ms. Measured quality: 1.00/1.00 on the tuned set and 0.99/1.00 on the blind hold-out after fixes. The labelled harness (`scripts/secret_filter_eval.py`, `tests/secret_cases*.py`: 170 secrets, 167 hard negatives) is an asset. Any replacement can be scored on exactly the same cases.

(Candidate models, RAM and latency: section A.1.)

1. **Cheap cloud call** (Haiku as a classifier): rejected. The text containing the secret would be sent to a model, which is exactly the incident being prevented (a Discord password reached Haiku and then Sonnet). It also adds latency and cost to every capture.
2. **The memory service's embedding model** (`paraphrase-multilingual-MiniLM-L12-v2`, fastembed int8 ONNX, 384-d): possible, but not the recommended primary.
   - It pools one vector per text, so on its own it can say "this text is about logins", not which span is the secret.
   - fastembed's ONNX graph does output per-token 384-d states, and pooling happens afterwards in Python (`pooled_normalized_embedding.py`). So a tiny per-token head (384 x ~21 BIO weights, a few KB) could share its forward pass.
   - Against:
     - it was trained for sentence meaning, not for telling a random string from a word, and random strings shred into many SentencePiece pieces;
     - it was trained at 128 tokens, so it would need overlapping windows;
     - it cannot be fine-tuned without invalidating stored vectors, so only a frozen probe is possible;
     - it lives only in `yuki-memory`, and today it is unloaded after 5 minutes idle (service ~50-100 MB). Tagging every capture would pin ~550 MB for good;
     - the tray's log redaction (`log/events.py`) runs in another process that does not have it.
   - In favour: zero extra RAM when it is already loaded, and a tokenizer that handles Devanagari and Hinglish.
   - Verdict: worth a one-day probe experiment, not a plan.
3. **A small local token-classification model**: recommended, in the form below.

### A.1 Candidate local models (research pass, 2026-09-25; sources in the appendix)

| Repo | Size | Secret labels | Languages | License | Quality / notes |
|---|---|---|---|---|---|
| `openai/privacy-filter` | 1.5B MoE (50M active); ONNX q4 ~0.9 GB, int8 1.6 GB | `secret` (passwords, API keys, tokens), `account_number` | Mostly English; collapses on non-Latin scripts in one outside test | Apache 2.0 | The only model with a real secret class. A third-party test caught 38/50 secrets with no false positives, but missed AWS keys and connection strings. Estimated 0.9-1.7 GB resident and ~0.5-1.5 s per 2k-character capture on the Ryzen 2700 (estimate). **Too heavy to ship.** Good as a local teacher and benchmark. |
| `OpenMed/privacy-filter-multilingual-v2` | same as above; GGUF only | PASSWORD, PIN, CREDITCARD, CVV, BANKACCOUNT (no API_KEY) | 16 languages incl. Hindi | Apache 2.0 | No per-label numbers published. Same weight problem. |
| `knowledgator/gliner-pii-edge-v1.0` | ~45M; ONNX uint8 197 MB | Zero-shot labels chosen at run time (password, credit card, cvv, bank account; API key and OTP untested) | English | Apache 2.0 | F1 75.5 overall. Estimated ~250-400 MB resident, ~150-400 ms per capture (estimate). |
| `Isotonic/distilbert_finetuned_ai4privacy_v2` | 66M; ONNX fp32 266 MB, ~70 MB int8 once quantised | Password, PIN, card, CVV, account | English | **CC-BY-NC** | Password F1 1.00, but on synthetic data from its own training source. Estimated ~150-250 MB resident, ~150-300 ms per capture (estimate). |
| mDeBERTa ai4privacy, Piiranha | ~280M | Password, PIN, card, account | European languages, no Hindi | NC / NC-ND | Heavier. The license rules them out. |
| `bigcode/starpii` | encoder | KEY, PASSWORD in code | Code | Gated | Key F1 0.70. Not for chat. |
| gliner_multi_pii, nvidia/gliner-PII, ai4privacy openpii | - | No password or API-key label | - | - | Out. |
| Samsung CredSweeper | BiLSTM ONNX | Credentials | Code | MIT | Finds candidates with regex first. Violates the rule itself. |

No off-the-shelf model has been shown to match the current hold-out result (0.99 / 1.00), and none covers Hinglish chat-form credentials with an API-key class at a light size.

### Recommendation

**Train one small local secret tagger, and keep the machine-format parsers.**

1. **Tagger:**
   - a ~45-66M-parameter encoder (ModernBERT/Ettin-32M as in gliner-pii-edge, or a multilingual DistilBERT) fine-tuned for token classification;
   - labels: secret kinds only, `PASSWORD, API_KEY, TOKEN, PRIVATE_KEY, OTP, PIN, CARD, CVV, BANK_ACCOUNT` + O. Never names, e-mails or phones, which memory needs;
   - runtime: int8 ONNX on onnxruntime (already a dependency via Silero VAD and fastembed), 2 threads, no arena, in `yuki-memory` only.
   - Worth benchmarking alongside: a **character-level** tagger (a CNN or BiLSTM of ~1-3M parameters, <10 MB, estimated a few ms per 2k characters). Secrets are a character-level phenomenon and it handles every script, but it must be trained from scratch and its quality is unproven.
2. **Training data, none of it real:**
   - Claude generates English, Hinglish and Devanagari chat, SMS and call-transcript lines holding *fake* credentials. Sending fake data to the cloud leaks nothing;
   - add ai4privacy's PASSWORD/PIN/card spans (licence permitting);
   - add issuer-shaped fake keys and hard negatives (hashes, UUIDs, ids, "I forgot my password" talk);
   - optionally, label real captures **locally** with `openai/privacy-filter` run once on this PC as a teacher (it never ships);
   - `tests/secret_cases_holdout.py` stays held out.
   - Shipping bar: hold-out recall >= 0.99, precision >= 0.95, run through `scripts/secret_filter_eval.py`.
3. **Deterministic backstop kept, because it is PARSING of published machine formats:**
   - issuer prefixes and lengths (`_KNOWN`), PEM/PuTTY blocks, JWT structure;
   - `urlsplit(...).password`;
   - Luhn + IIN, the IFSC shape;
   - Shannon entropy as a *feature* or gate on the model's spans (a measured signal).
   - **Deleted:** every English/Hinglish/Devanagari word list and phrase grammar (`_labelled`, `_otp`, PIN, `_RE_KEY`, card and account words, `_SECRET_ANCHORS`, `_RE_SECRET_PARAM`, `_NOT_VALUE`), and the TOML `[terminal] secret_patterns`.
4. **Cost control:**
   - tag only new text: message fingerprints already exist, so each message is tagged once, and a page capture is tagged on its delta;
   - windows of up to 512 tokens, batched per capture;
   - the tagger unloads after idle like the embedder.
5. **Fail closed:** a capture waits in the in-memory queue until tagged. It is never stored untagged.
6. **Logs in the tray process** (`log/events.py`, `log/requests.py`): don't load a second model there. Send the user's words and tool inputs through the memory service's tagger over the existing pipe, and let the parsers backstop the rest. Or write session logs only after the memory service has tagged them.

Honest trade-offs:

- **RAM:** estimated +150-250 MB while loaded (DistilBERT class), or <20 MB for a char model.
- **CPU:** ~150-300 ms per *new* 2k-character capture, against 0.07 ms p50 today. At about 8 captures a minute that is ~2-4% of one core while active. It is lower with delta and fingerprint caching, because most captures change little.
- **Quality:** the first model will probably trail the regex on its own tuned set (1.00, which flatters the regex). The fair comparison is the blind hold-out, where the regex scored 0.93/0.88 before it was tuned on it.
- **Failure modes:** a model can miss a novel format with confidence. This is why the published-format parsers stay.
- **Effort:** roughly 1-2 days to generate data, fine-tune and measure, plus retraining when a miss is found (instead of adding a regex).
- **Gain:** it generalises to phrasings nobody listed ("naya wala hai X", Kannada or Tamil chat, a PIN said in a call) and ends the word-list growth.
## B. Warp terminal rules (`yuki/memory/warp.py`, `privacy_default.toml [terminal]`)

| Location | What | Verdict | Replacement + cost |
|---|---|---|---|
| `privacy_default.toml:193-226` `secret_patterns` (22 regexes), compiled at `warp.py:130-135`, matched at `warp.py:170-172` | A command line is a secret if it matches `key|token|secret|passw|credential|auth` names, `setx`, `config set`, `ConvertTo-SecureString`, `-AsPlainText`, `sshpass`, `mysql -p`, `cmdkey /pass`, `net user x y` | **BEHAVIOUR** (what counts as a secret, by keyword and regex, even though it is TOML data) | Run the same local secret tagger as A on the command, folder and branch; drop the whole command if any span is tagged. Keep the entropy test (measured). Cost: one tagger call per new command (commands average ~13 characters, so ~1-3 ms each, batched per 180 s pass). No tokens. |
| `warp.py:88` `_WORD_SPLIT`, `warp.py:89` `_CLASSES` | Word boundaries and char-class counts for the entropy test | PLUMBING | Tokenising for a numeric measurement. `_CLASSES` could be `str.islower/isupper/isdigit`. |
| `warp.py:139-144` `_random_word`, `entropy_min_length`/`entropy_min_bits` | Numeric entropy threshold | PLUMBING (measured signal) | - |
| `warp.py:106` `check_app("warp.exe", "Warp")` | Reader asks whether the Warp app is blocked | PLUMBING | It is Warp's own reader asking about its own source. |
| `warp.py:95` path `warp/Warp/data/warp.sqlite` | Where Warp keeps its DB | PLUMBING | A file location. |

## C. Privacy gates (`yuki/memory/privacy.py`, `privacy_default.toml`)

`privacy.py` says it relies on ARCHITECTURE rule 1's allowance for "safety gating". The user's rule for this audit is stricter: "what counts as a secret" and "what something is" must come from models or measured signals. So each gate is judged on whether it is **user policy** (an explicit list the user owns: fine, it is configuration, like a firewall rule) or a **classifier** (a word list standing in for understanding: violates).

| Location | What | Verdict | Replacement + cost |
|---|---|---|---|
| TOML 40-67 `blocked_processes`, 150-153 `[user] blocked_apps/domains`, 113-123 `blocked_host_suffixes` | Named apps and sites never read (password managers, sign-in, banks, meeting sites) | POLICY DATA (fine) | Explicit, user-editable denylist. It does not interpret content. Keep it. |
| TOML 69, `privacy.py:172` `blocked_process_substrings` (`1password, bitwarden, lastpass, dashlane, keepass`) | Any process whose name contains these is a password manager | Borderline | A substring guess about what an app is. Fold it into the explicit `blocked_processes` list. Free. |
| TOML 125-129, `privacy.py:217` `blocked_path_fragments` (`/login /signin /auth/ /oauth/ /2fa /mfa /otp`) | Any site whose path contains these is a sign-in page | **BEHAVIOUR** | Guesses what the page is from URL words. Replace with a measured structural signal the watcher already has: any UIA element with `IsPassword=true` in the window's tree (not just the focused one) means skip. That extends `skip_when_password_focused` to a `skip_when_password_present` probe. Cost: the password probe already walks the tree (`password_ms`), so this adds about a condition check. OTP pages without a password field are then covered by the tagger (A). |
| TOML 132-135, `privacy.py:219-221` `blocked_host_paths` | Stripe `/login`, Teams meeting paths | POLICY DATA | Per-site entries the user can edit. The Stripe one goes with the item above. |
| TOML 138-143 `adult.host_suffixes` | Named adult sites | POLICY DATA | An explicit denylist. |
| TOML 143, `privacy.py:210-211` `adult.host_substrings` (`porn, xvideos, xhamster, hentai, javhd`) | Any host containing these words is adult | **BEHAVIOUR** | Keyword classification of what a site is. Replace with a local NSFW signal: (a) the explicit host list only, plus (b) a small local image classifier on the screenshot the watcher does not take today (cost: RAM and CPU; see below), or, lighter, (c) the journal model (Haiku, which already reads the capture) returns `sensitive: adult` in its strict schema and the thread is then purged. Option (c) sends the page once, which the user may not accept for adult content. The honest light choice is to delete the substrings and rely on the explicit list plus the user's own `blocked_domains`. |
| TOML 145-147, `privacy.py:222-227` `adult.query_terms` (`porn, hentai, xxx, nsfw, jav , sex video`) | A search query containing these words is not read | **BEHAVIOUR** | A keyword classifier on the user's search text. Same options as above. The light one is deletion. |
| TOML 76-94 `[[private_windows]]` title marks (`InPrivate`, `(Incognito)`, `(Private)`, `Private Browsing`) | Private browser windows | Borderline: PARSING | Each browser writes a fixed mark into its own title, so this is an app's machine format kept as per-app data in TOML (the sanctioned place). Caveat: the marks are English only (a Hindi or Japanese UI writes something else). No reliable Windows API exposes incognito. Keep. |
| TOML 101-106 `blocked_schemes`, `privacy.py:199-205` | `chrome://`, `data:`, credentials in URL | PARSING | URL scheme and userinfo parsing. |
| TOML 32, `privacy.py:230-237` `mask_characters` | A value made only of bullet characters is a masked secret | PARSING | A rendering convention (the mask glyphs), not language. |
| TOML 72 `blocked_window_titles = ["Credential Manager"]` | An English window title | Borderline | An explicit user entry, English only. Keep as policy. |
| TOML 155-177 `[meetings] processes/host_suffixes/host_paths/names` | What counts as a meeting, and its display name | **BEHAVIOUR** (what something is) | Measured signal: a process (or browser tab's process) that holds the microphone (the ConsentStore key `calls.py` already watches) and has an active render session at the same time is in a call or meeting, whatever the app. Display name from the process's FileDescription (already read for app names, `extract/__init__.py:77`) or the site's host. Keep the explicit content block (`[apps]`, `[web]`) as policy. Cost: one audio-session enumeration per 5 s poll (a few ms), no tokens. |
| TOML 321-325 `[calls] apps` (Zoom, Discord, Teams, Slack, Telegram, Signal, Skype, Webex, WhatsApp) and 440-453 `[calls.names]` | Which apps holding the microphone are a call | **BEHAVIOUR** (what something is) | Same measured signal (mic plus render session, sustained N seconds). The card still asks the user, so a false positive costs one "Never for this app" click, which `call_prefs` already remembers. Names from FileDescription. |
| TOML 337 `headphone_form_factors` | Windows' form-factor enum values | PLUMBING (a measured device property) | - |
| TOML 470 `[calls.stt_prompts] hi` | A style example given to Whisper | Fine (a prompt) | - |

## D. Extraction profiles TOML (`yuki/memory/extract/profiles_default.toml`)

Per-app anchors (UIA class, id, aria paths) are the sanctioned place for per-app knowledge, and they parse the apps' own machine structure. They are fine. Two parts are not per-app:

| Location | What | Verdict | Replacement + cost |
|---|---|---|---|
| 26 `[defaults] text_noise = ["(edited)", "(bearbeitet)", "(modifié)", "(editado)"]` | Strips "edited" marks from any app's messages | Borderline | A cross-app word list. Harmless: the journal model reads through it. Move it into the profiles of the apps that emit it, or delete it and let the journal model ignore the mark (0 extra tokens). |
| 28-32 `me_senders = ["You","you","Me"]`, `me_suffixes = ["(you)","(You)"]`, `me_prefixes = ["You:"]`, used at `extract/conversation.py:267,291,294` | Decides which messages are the user's in any app | **BEHAVIOUR** (whose something is) | Per-app anchors already exist for this (`me_row`, `self_name`), so move these strings into the profiles whose UI emits them (the sanctioned place). For the generic detector, pass the raw sender plus the learned `me_names` to the journal model and let it set `is_me` in `record_facts` (it already reasons about who said what). Cost: ~0 to 20 tokens per batch. |
| 361, 368 process lists for `terminal` and `editor` profiles | Which apps are terminals or editors | PLUMBING (per-app TOML) | The sanctioned place. |
| 276 `claude_chat me_row name^="You said:"` | Claude's screen-reader heading | PLUMBING (per-app TOML) | An anchor on the app's own UI string. |

## E. Key and other alias maps

`yuki/actions/input.py:357-393 _KEY_ALIASES`, used by `normalize_key` (404-428, called at 2380 and 2438). Entries fall into three groups:

| Group | Entries | Verdict |
|---|---|---|
| Separator and case folding (`normalize_key` lowercasing, stripping `_ - space`) | `volume_mute` -> `volumemute`, `Page Up` -> `pageup` | PLUMBING: normalising a machine identifier. |
| Same-key spellings | `control/ctl`->ctrl, `return`->enter, `escape`->esc, `del`, `ins`, `pgup/pgdn/pgdown`, `spacebar`, `windows/super`->win, `mediaplaypause`, `medianexttrack`, `mediaprevtrack`, `mediastop`, `volumemuted` (Win32 VK_* names) | Borderline: PARSING. These are different published names for one key (VK_MEDIA_PLAY_PAUSE and so on). Nothing is guessed about meaning. |
| Meaning guesses | `play`->playpause, `mediaplay`/`mediapause`->playpause (a play key is not a toggle), `next`->nexttrack (could mean Tab, page down or "n"), `mediaprev`/`previoustrack`, `mute`->volumemute (mic mute?), `plus`->add and `minus`->subtract (numpad keys, not the main-row +/-), `cmd`/`meta`->win (Mac habit) | **BEHAVIOUR**: the code decides what the model meant. |

Replacement:

- **Delete the meaning-guess entries** (the third group). The same-key spellings may stay: they are one-to-one, and five are dead because pyautogui already knows `return`, `escape`, `del`, `pgup`, `pgdn`. The `press` schema (`agent/tools.py:305-322`) already names the canonical media keys (`playpause`, `nexttrack`, ...), and an unknown name already fails with the canonical list (`input.py:2383-2386`). Note: a Mac `cmd` means Ctrl on Windows, so `cmd`->win was also semantically wrong.
- **Make that error self-correcting:** list the valid names close to the given one (nearest canonical names by prefix, from `pyautogui.KEYBOARD_KEYS`, which is a machine vocabulary). The model picks.
- **Fix the schema example:** `hotkey`'s example says `['volume_mute']`. Change it to `['volumemute']`.
- **Optional enum:** give `key` / `keys.items` an `enum` of `pyautogui.KEYBOARD_KEYS` (~190 names, ~700-900 tokens, inside the cached tool block, so ~0.1x price on cache reads). The model then cannot emit a non-key.
- **Cost:**
  - without the enum: 0 tokens, plus one extra tool round trip on the rare wrong name (~1-2 s);
  - with the enum: ~800 cached tokens per request.

## F. Rest of memory

### Violations

| file:line | What it does | Verdict | Replacement + cost |
|---|---|---|---|
| `memory/api.py:783-826` (keyword pick 805-810), reached from `agent/memory.py:874-892` `forget_rule` | Picks the rule to revoke: the newest active rule whose text contains every word of the model's phrase (substring test), else embedding cosine >= 0.45 | **BEHAVIOUR**: code decides which rule the user meant | `standing_context()` prints ids ("R12: ..."), and `forget_rule` takes a required `rule_id` the model picks. Delete the keyword and cosine passes; keep only "this id exists and is active". Cost: ~4 tokens per rule in the always-attached block (cap 1,200 characters), about +20-40 tokens per request, cached. No embedder load on this path. |
| `memory/nudges.py:608-641` (match 622-628), via `api.py` `complete_todo` and `agent/memory.py:936` | Marks done the to-do or loop that contains every word of `ref`, else cosine >= 0.45 | **BEHAVIOUR**: code decides which item the user meant | `todo done` takes `id` only. `todo list` and the resume and standing blocks already show ids, and the model lists first when it lacks them. Cost: at most one extra tool round (~1 s, ~300-600 tokens) when the ids are not in context. It also saves an embed per candidate. |
| `memory/calls.py:1441` `_candidates` (`key in cfg.apps`) | Only listed apps holding the mic count as a call | **BEHAVIOUR** | Section C: mic holder plus an active render session for N seconds. The consent card still asks. Keep the list only as an optional user override. Cost: ~1-3 ms per 5 s poll. |
| `memory/timeline.py:225-275` `MeetingRules.match_process/match_url`, used at 1494 and 1574 | A host/process/path list decides a stretch is a meeting | **BEHAVIOUR** | Foreground process holding the mic (and optionally the camera, same ConsentStore read) for >= N seconds. Service name from FileDescription or host. Cost: none (these reads are already polled). |
| `memory/extract/conversation.py:263-271` `learn_me`, `:285-296` `_is_me` | "You", "Me", "(you)" decide `is_me` | **BEHAVIOUR** (whose something is) | Per-profile `me_row` / `self_name` anchors (TOML, sanctioned) where they exist. For generic chats, pass the raw sender plus learned `me_names` to `record_facts` and let the model attribute. Cost: ~10-30 tokens per journal batch. |

### Borderline

| file:line | What | Why borderline | Suggested fix + cost |
|---|---|---|---|
| `memory/extract/timeparse.py:29-49, 303-325` `is_time_label`, `_LABEL_WORDS`, English month/weekday tables | `parse()` reads app-rendered times (PARSING). But `is_time_label` uses an English word list to decide "this text is a time label", which gates list detection (`conversation.py:102,115`), row dates, authors (`page.py:163`) and the link-out check | Lexical and English-only | Month and day names from the OS locale (`GetLocaleInfoEx` LOCALE_SMONTHNAME*/SDAYNAME*), and prefer UIA/ARIA `time` elements. Cost: 0 at run time. |
| `memory/calls.py:742` `vocabulary_hint` | "A capitalised word in 2+ facts" is treated as a name for Whisper's hint | Capitalisation stands in for "is a name" | Use model-extracted subjects (portrait subjects, question subjects, me_names). Cost: 0. |
| `memory/portrait.py:2149, 2187` | Facts go under audit when their subject string appears in new journal text | String match selects context | Cosine between the fact vector and new journal vectors (`similar_journal` exists). Cost: a few ms. |
| `memory/questions.py:160-166` `relevant()` | Subject name is a substring of `about` | Context selection by string | Embedding similarity. Cost: one embed, ~6-14 ms. |
| `memory/timeline.py:1692-1699` `_shows` | Media title is a substring of the window title (watching vs. a background tab) | Both strings are machine-written by OS and app | Keep. |
| `memory/extract/conversation.py:517` + TOML `text_noise` | Strips "(edited)" in four languages from every app | App UI marks, cross-app | Move into the emitting apps' profiles, or delete. |

### Plumbing reviewed (fine)

- **Extraction internals:**
  - `extract/conversation.py:41-42` epoch and Discord snowflake id regexes (a declared id format);
  - `fingerprint.py:30`, `text.py:15-40` (UIA/ARIA role vocabulary, invisible characters);
  - `extract/__init__.py:112-125` `_stable_title` ("(3)" counter, Unicode categories), `:444-455` `_links_out` (URL parts), `:458-492` generic chat thresholds (structural counts and area shares);
  - `page.py` structural video and author detection;
  - `uia.py:570-588` title ranking.
- **Yuki's own formats:** `journal.py:532` `_ROW_DATE`, `journal.py:490` fences, `content_date` strptime, `nudges.py:552` `_split_loop`, "- "/"power: " line prefixes in episodes and nudges.
- **Enum-to-label maps and enum checks:** `personality.py:44-76` `_MARKS`/`ASPECT_TITLES`, `timeline.py:330` `POWER_WORDS`, `say == "none"` checks.
- **Id and key formats:** `api.py:170` ISO "Z", `:1004` question id, `:1161` ISO week "W"; `store.py:1840` `app_key`/`url_host`.
- **Measured thresholds and model-output checks:**
  - `conversations.py:380-388, 784-791` `quote_in` (verifies the model's quote is verbatim);
  - `remember_rule` supersede at cosine 0.85;
  - questions dedupe by HMAC plus cosine 0.70;
  - `calls.py:696-739` `screen_segments` (Whisper probabilities).
- **Search and bookkeeping:** `store.py` `keyword_*` search (retrieval fused with vectors, the model chooses the query), watcher trigger ranks, `power.py` event ids.
- **Devices and processes:** `calls.py:261-287` own-process exclusion, `:376` WASAPI host API name.
- **No findings:** embed, crypto, acting, audit, rejournal, service, transcribe.

## G. Actions and perception

| file:line | What it does | Verdict | Justification / replacement + cost |
|---|---|---|---|
| `actions/input.py:357-391` `_KEY_ALIASES`, guessing entries (`play`, `mediaplay`, `mediapause`, `next`, `mute`, `cmd`, `plus`, `minus`) | Map a word onto a key with different behaviour | **BEHAVIOUR** | See E. Delete. Cost: 0 tokens, plus one rare retry. |
| `actions/input.py:357-391`, translation entries (`control`, `ctl`, `windows`, `super`, `meta`, `escape`, `pgup`, `spacebar`, `media*` VK names, ...) | Other key vocabularies' names for the same key | Borderline, leaning PARSING | One-to-one names of the same physical key. Five are dead (pyautogui knows `return`, `escape`, `del`, `pgup`, `pgdn`). Keep, or replace with an `enum` (~400-900 cached tokens). |
| `actions/input.py:404-425` `normalize_key` folding | Lowercase, strip `_ - space` | PLUMBING | Normalises a machine identifier. |
| `actions/launch.py:233-247` `_match_apps` | Exact Start-menu name, else auto-pick when exactly one substring matches, else return candidates | Borderline | No synonyms or fuzzy scores, but the single-substring auto-pick is a small decision made by code. Strict option: always return candidates for a non-exact name. Cost: one round (~1.5-3 s) per non-exact launch. |
| `actions/launch.py:1328-1365` `_page_address` | Bare `host.tld` or `host:port` becomes `https://` | Borderline, leaning PARSING | Parses the argument's shape as a browser does. Only used to verify the load. |
| `actions/launch.py:1370-1418`, `:663, 836, 897, 934, 1057` | URL comparisons; KNOWNFOLDERID, `.exe`, AUMID `!`, manifest schemes | PLUMBING | Windows and URL formats. No app names in the module. |
| `actions/shell.py:184` `_CLIXML_ESCAPE`, `:88` prelude, sentinels | PowerShell `_xHHHH_` decoding and framing | PLUMBING | A machine format. |
| `perception/windows.py:128-142` `_SHELL_WINDOW_CLASSES` (used at 292) | 11 OS window classes hidden from the overview | Borderline | OS classes, no app names, but still a list. Replacement: `GetShellWindow()`, `WS_EX_NOACTIVATE`, and the existing cloaked, tool-window and untitled checks. Cost: 0. Low priority. |
| `perception/windows.py:770, 790` console class; `:862-872` `os_program` | Hidden console host; image under %SystemRoot% | PLUMBING | OS facts. |
| `perception/tree.py:1203-1214` `_title_rank`, `:1968, 3214, 3228` | Tab or window title vs. the page Name, to pick the page in front | Borderline, leaning PLUMBING | Compares two browser-written strings, combined with off-screen, focus and bounds. |
| `perception/tree.py:176-177, 240, 459, 511`; `:3077, 4118-4131, 4355` | UIA enum maps; URL-shape and dedupe | PLUMBING | Documented enums and output compaction. |
| `perception/system.py:388, 396, 576-603`; `audio.py:47, 324-332`; `screenshot.py` | Registry keys, WinRT enums, form factors, configured headphone names | PLUMBING | Measured device facts plus user config. |

## H. Agent, voice, UI, MCP, config, CLI

No violations.

| file:line | What it does | Verdict | Justification / fix + cost |
|---|---|---|---|
| `voice/session.py:367-374` `_plain_error` | Substrings of the exception text ("401", "unauthor", "timed out") pick the message shown to the user | Borderline | Display only, but it is string sniffing. Branch on exception class and HTTP status instead. Cost: 0. |
| `voice/pipeline.py:127-130` | `"missing_permissions" in body` on the ElevenLabs error | Borderline (PARSING) | Parse the JSON (`detail.status`). Cost: 0. |
| `agent/memory.py:73, 1073-1098` `split_standing` / `RULE_HEADINGS` | Splits standing text into rules and commitments by exact heading strings | Borderline (own format) | Return structured fields from `standing_context`. Pairs with the id fix in F. Cost: 0. |
| `ui/status.py:40-72, 103-108` `_participle` / `_phrase_from_name` | English "-ing" rules turn an unlabelled tool name into status text | Borderline (presentational) | Require a `label` on every tool schema (test it) and delete the fallback. Cost: 0. |
| `mcp/times.py:32-168` `resolve` | Phrase grammar ("yesterday", "last friday", "past 3 hours", number words to thirty) | Borderline, leaning PARSING | A closed, documented argument grammar that fails loudly. Still, the in-app agent is ISO-only (`agent/memory.py:1363`). Make MCP ISO plus "now", with today's date in the description. Cost: ~20-40 tokens per tool description. |
| `voice/narrator.py:31-89`, `voice/mouth.py:342-349` | Sentence splitting and clause cuts | PLUMBING | Trimming length only (explicitly allowed). |
| `voice/narrator.py:100-128` | When to speak: speaking flags, owes-reply, cooldowns, stale turn counts | PLUMBING | State and timing signals. |
| `voice/bridge.py:277-296, 470-490` | Routes follow-ups by the mouth's `intent` enum and job state | PLUMBING | Compliant: the model decides. |
| `voice/progress.py:25` `NOT_STEPS` | Tool names not shown as steps | PLUMBING | Own schema. It duplicates `tools.CONTROL_TOOLS`; reuse that. |
| `voice/pipeline.py:635-638, 787`; `voice/audio.py:46, 79-81`; `voice/grounding.py:175`; `voice/wake.py:71`; `voice/offline.py:18-21` | Metric names, model-id suffix, WASAPI name, a length threshold, KWS keyword file (the spotter model decides), test fixture | PLUMBING | - |
| `agent/memory.py:645-669` `block_todos`; `:955-975` `quote_in` guard; `:102, 121, 124, 1117, 1214, 1241` enum maps | Dedupe by text; verbatim-quote provenance check; labels | PLUMBING | Dedupe could use ids. |
| `agent/loop.py:90, 94, 1644, 1787, 1962-1977`; `agent/context.py:187, 792, 799`; `agent/tools.py:35-56, 638, 655, 1809`; `agent/prompt.py` | API enums, own tool names, process identity; prompt examples are prose for the model | PLUMBING | - |
| `ui/hotkey.py:69-122`; `ui/runtime.py:517`; `ui/nudges.py:187-194`; `ui/app.py`, `overlay.py` | Hotkey config parsing; routing on `worker_busy`; linger by length; tone from signal type | PLUMBING | - |
| `mcp/server.py:199, 424, 542`; `config.py:19, 29, 78`; `cli.py:59, 81-83, 137` | Own sentinel, CLI and model aliases, price table, slash commands | PLUMBING | CLI and config parsing. |

## Appendix: sources for A.1

- https://huggingface.co/openai/privacy-filter (and `/tree/main/onnx`)
- https://arxiv.org/abs/2608.02616 (outside multi-benchmark evaluation)
- https://grepture.com/blog/openai-privacy-filter-pii-redaction (secret test: 38/50)
- https://github.com/localai-org/privacy-filter.cpp (CPU timings: Ryzen 9 7900, 512 tokens in 0.14 s)
- https://huggingface.co/OpenMed/privacy-filter-multilingual-v2
- https://huggingface.co/knowledgator/gliner-pii-edge-v1.0
- https://huggingface.co/Isotonic/distilbert_finetuned_ai4privacy_v2
- https://huggingface.co/Isotonic/mdeberta-v3-base_finetuned_ai4privacy_v2
- https://huggingface.co/iiiorg/piiranha-v1-detect-personal-information
- https://huggingface.co/bigcode/starpii
- https://huggingface.co/nvidia/gliner-PII, https://huggingface.co/urchade/gliner_multi_pii-v1
- https://github.com/Samsung/CredSweeper

RAM and latency figures on the Ryzen 7 2700 marked "estimate" are extrapolated from parameter counts and the published timings above. They are not measured on this PC. The first step of any fix is to run the candidates through `scripts/secret_filter_eval.py` and time them here.
