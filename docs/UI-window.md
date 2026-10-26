# The Yuki window

Clicking the tray icon opens a Yuki app window with six pages: **Today, Me, Yuki, Status, Chat, Settings**. It explains itself in plain words, shows where everything it believes came from, and lets the user put things right in their own words. The same look covers the overlay (Ask bar), the check-in, question, notice and call cards, the call and voice pills, the status strip, the tray menu and panels, and the first-run wizard.

**2026-09-28: "Horizon x Field Unit".** The user found flat shadcn "plain, lame, AI generic" and picked a mix of two drafts (docs/ui-mockups/drafts/mix/, gitignored: real personal data): Horizon's navy-charcoal palette, Segoe UI Variable, floating dialogs, call consent card, skyline graph and wave indicators, and Field Unit's dot-matrix displays, day keys and hardware-key sidebar. See "Design system" below. The glass rounds and the shadcn round are history (2026-09-27).

Renders (offscreen, from a copy of the real memory; large and gitignored) are in `docs/ui-mockups/horizon-field/`:

    .venv\Scripts\python.exe -m yuki.ui.window.shots --db <copy>\memory.db                     # green (default)
    .venv\Scripts\python.exe -m yuki.ui.window.shots --db <copy>\memory.db --accent orange
    .venv\Scripts\python.exe -m yuki.ui.window.shots --db <copy>\memory.db --measure           # window + surfaces RAM

## Technology: Qt Widgets with custom painting

Measured 2026-09-26 on the offscreen platform. Each option ran in a fresh process from the same baseline: yuki-ui's QApplication, its hidden overlay and a MemoryClient on the DB copy, 43.6 MB private. `yuki/ui/window/measure.py`.

| | Qt Widgets (built, real data) | Qt Quick / QML (one simplified page, no data) | WebView (QtWebEngine, one page, no data) |
|---|---|---|---|
| Open, Today page | +28 MB private (+12 of it reading memory) | +33 MB | +65 MB private, +156 MB working set, +1 process |
| Open, all six pages | +62 MB | not built | not built |
| Closed again (in process) | +20–28 MB, flat from the 2nd open (5 cycles) | +6 MB private, +17 MB working set | +28 MB private, +59 MB working set |
| First paint | 0.6 s (0.3 s of it reading memory) | 0.8 s | 1.9 s |
| Background blur | DWM Acrylic/Mica behind the window: no cost to this process | same DWM, or MultiEffect (blurs only its own content) | CSS `backdrop-filter`, GPU process |
| Custom timeline | QPainter: already done | Shapes/Canvas | SVG/Canvas, easiest |
| Effort | lowest: same stack, same widgets, same skills as the rest of yuki-ui | new language and module set, two UI stacks | IPC to Python for every read and action |

The two comparison pages are stand-ins of similar size, without the memory read or the full six pages. Quick keeps the least after closing in process. But its open cost for one simplified page is already above the Widgets UI (+16 MB without the data), it measured the software renderer (on the real desktop the D3D11 renderer loads GPU driver DLLs on top), and it would mean a second UI stack. With the separate process below, the "closed" row is 0 MB for any option, so open cost and effort decide it: Widgets.

After close, every widget is gone (773 live widgets while open, back to the baseline 14) and the shadow cache is cleared. What stays is heap high-water: about 12 MB from reading and decrypting the day's rows, about 15 MB from fonts, glyphs and widgets. It is freed memory the allocator keeps and reuses on the next open, so it stops growing after the second open. `PRAGMA shrink_memory` did not return it.

**To cost nothing when closed:** run the window as its own short-lived process, `pythonw -m yuki.ui.window`, started when the tray icon is clicked and exiting when the window closes. Measured the same way: **0 MB while closed**, 70 MB private while open on Today (104 MB with all six pages), 1.1 s from the click to the first paint. Chat and the voice and model settings then reach yuki-ui's runtime over a local pipe, the same pattern as `yuki/memory/embed_ipc.py`. Memory reads and writes go straight to the database, as the tray already does. Recommended for phase 2. The fallback is in-process: simpler wiring, but about 28 MB stays after the first open.

Neither measurement includes the window's backing store (1280×840×4 bytes, 4.3 MB at 100% scaling, about 9.7 MB at 150%) or its surface in dwm.exe. Both cost the same for all three technologies.

## Round 2 (2026-09-28, the user: "still looks AI generated")

- **Darker.** A near-black charcoal (`#07080A`) with panels only a step up (`#0C0E11`, lines `#181B21`), not
  grey-blue; floating dialogs `#0C0E11` with `#1B1F26`. The tokens below list the round-1 values; `theme.PALETTE`
  holds these.
- **Accent, deeper.** Olive `#748F42` (still lights the dot-matrix digits on the darker base) or orange `#E4571C`.
- **Font: IBM Plex Sans** (the user's pick from Geist, IBM Plex Sans, Manrope and Onest: `docs/ui-mockups/horizon-field/fonts/index.html`),
  bundled (`yuki/ui/fonts/IBMPlexSans.ttf`, one variable file, and `IBMPlexSans-OFL.txt`; in `yuki.spec` datas),
  loaded in the tray, the window and the wizard. Set a step smaller than the Segoe scale (`theme.FAMILY_SCALE`
  0.94: Plex runs wider), titles Light or Regular, labels Medium (`FAMILY_WEIGHTS`: Semibold is 520), no walls
  of bold. Doto stays on every display; Nirmala UI stays the Devanagari fallback. `theme.set_family` swaps the
  face in one line.
- **Corners.** Every pill shape (buttons, toggles, segmented choices, keycaps, chips, the "All N" key) is a
  capsule; rectangles are rounder and scaled to their size (`theme.corner`): modules 18, dialogs 20, displays
  and keys 16, fields 14, small keys and initials about a third of their short side.
- **Today is a dashboard.** Under the four displays (each a link: the longest stretch at the PC, the longest
  time away, the longest call, your routines on Me), five cards lead to the other pages: your to-dos (Me), people
  with their initials (Me), questions waiting (Yuki), learned to do (Yuki), calls to remember (Status). They
  lift under the pointer and drop when pressed (`widgets.NavCard`, painted). `WindowApp.navigate(page, section)`
  scrolls to that section (eased) and lights its module in the accent for a moment; each page lists its
  `sections`. The moment panel folds open what you were doing and what Yuki noticed.
- **Me and Yuki are scannable** (`pages/fold.py`): each module is a title, a one-line headline ("14 people, 5
  you told Yuki"), the top three to five items as short rows (`widgets.FoldRow`: a title, one quiet line, the
  date; people with their initial), and the rest behind "All N". A row folds open to the full text, where it
  came from and every action (Why?, Not right, Done, Not needed, Not mine, Answer, Skip, Don't ask), built the
  first time it opens; a pending correction's row opens itself. Personality is one short trait line per side
  of you. Built in one go (the rows are painted): Me's first build 435 -> 133 ms.
- **Asked of memory** (the rows show the first sentence meanwhile): a model-written `summary` (at most eight
  words) for each portrait fact and person, and a `title` (three to six words) for each to-do and each know-how
  entry, written where they are extracted.

## Design system (`theme.py`, `paint.py`, `widgets.py`, `yuki/ui/surface.py`, `yuki/ui/wave.py`, `yuki/ui/fonts/`)

Horizon x Field Unit, dark only, flat and opaque. No gradients, glow, blur, glass or translucency, no light mode, no amber or yellow, no decorative dashes. The anti-generic rules hold: no all-caps eyebrow labels, no middle-dot meta lines (sources read "Seen in Discord, 25 Sep. Said as a joke"), no identical-card kit, no 01/02 markers, no scattered animation.

- **Colour** (`theme.PALETTE`, the tokens of the mockups' `mix.css`): window `#0B0E13`; page modules `#10141B` with a 1 px `#1E2632` line; quieter rules `#161D27`; "glass" insets and displays `#06090D` with `#1A222E`; key faces `#131821`, lit `#19212D`; ink `#E6E9EE`, secondary `#AEB6C2`, muted `#6F7886`, faint `#3E4858`; LED off `#283140`. Floating dialogs: the panel colour with a `#222A36` line and `#1B222C` separators, the user's bubble in the ask bar `#161C26`. Errors in words, a quiet red `#E5736B`. The call band is a cool grey `#8C9BB0`.
- **The accent** (Settings > General > Accent, `ui.json` `accent`): olive green `#8FAF5A` (default; chosen 2026-09-28 against forest `#4F9A6A`/`#5E9E6E` and olive `#7BA05B`: the most legible lit dot-matrix digit on navy without going neon) or orange `#FF5B14`. Only on dot-matrix displays, live and recording states (LEDs, the call pill's dot), the current or selected moment on the skyline, the selected page key and the speaking wave. Links are ink, underlined. `theme(accent=...)` caches one theme per accent; the window repaints at once (`WindowApp.set_accent`) and yuki-ui's surfaces follow over the pipe (`ui_settings` op).
- **Shape:** page modules 12 px; displays, keys and fields 8 px; bubbles 10 px; floating dialogs 14 px; buttons, switches and segmented choices fully round (pills).
- **Type:** Segoe UI Variable (Windows built-in; its `wght`/`opsz` axes set per role), else Segoe UI; Nirmala UI and Segoe UI Symbol as fallback families (`QFont.insertSubstitutions` + `setFamilies`), so Devanagari never shows as boxes. Page titles 36 px Display Light (Today's date part in muted ink), module titles 18 px Display Regular, lede 15 px, body 14, captions 13, sources 12. **Doto** (SIL OFL 1.1, `yuki/ui/fonts/Doto.ttf` + `Doto-OFL.txt`, in `packaging/yuki.spec` datas) for dot-matrix displays at weight 800, with the colon and point set in Segoe UI Variable Display Light as the mockups do (`paint.paint_dots`). `yuki.ui.fonts.load_fonts()` runs once per process from `theme.app_font()`, which the tray and the window call right after creating the application.
- **Components** (`widgets.py`): `Card` / `Panel` (a module: title, muted description, a key chip such as "All 11" on the right), `Button` (pills: `default` light with dark ink, `outline`, `ghost`, `destructive`, `key`), `TextAction` (the quiet "Why?" / "Not right" words with a glyph), `Toggle` (40 x 22), `Segmented` (glass track, light chosen pill), `Dropdown` (glass box), `Input` (glass pill or box), `Key` / `NavItem` (hardware keys: a 2 px bottom edge and an LED in the corner), `Led`, `DotDisplay` (a glass display: Doto value, unit bottom-right; the digits shrink to fit a narrow window), `TimeDisplay` (a time with up/down arrows; the quiet hours), `Keycaps` / `KeyCapture` (shortcuts as keycaps), `Avatar` (an initial on a small key), `Menu` (the tray menu's floating panel, an LED on its status line), `Tip`, `Toast`, `ListPopup`, `Shimmer`, `Masonry` (columns with optional weights).
- **Waves** (`yuki/ui/wave.py`): listening a white sine, hearing the same livelier, thinking a dotted grey line, speaking the accent sine, the call pill's small grey sine, the check-in's cool-grey wave. All waves share one timer (at most 30 fps; slow kinds skip frames); a wave is on it only while visible and repaints only its own rectangle; with nothing shown the timer stops. "Reduce motion" draws one still frame.
- **Layout:** pages 32/34/36/40 px from the edges (the caption buttons float over the top-right corner); modules 14 px apart in `Masonry` columns that fall to one column in a narrow window; `tests/test_window_phase2.py` checks every page fits the smallest window (900 px).
- **Window chrome:** opaque `#0B0E13`; a 96 px sidebar of 60 px keys (Today, Me, Yuki, Status, Chat, Settings) behind a `#161D27` rule; the current page's key lit with its LED in the accent; a page with something waiting shows its LED in secondary ink (the tooltip says what). The **listening key** sits at the bottom: its word says the voice state ("Listening", "Speaking", "Voice off", "Not running"), its LED is lit while Yuki listens, a click turns listening for "Yuki" on or off, and its tooltip carries memory's state and today's cost. DWM gets immersive dark mode and a `#161D27` border.
- **Floating surfaces** (`surface.SurfaceWindow`): the panel colour, 14 px corners (pills: half their height), a soft shadow drawn once per size, opaque so text reads over any screen. Buttons are the same pills as style sheets (`surface.button_css`).
- **Settings** in `ui.json`: motion, the accent, the window's place, the overlay's shortcut, the quiet hours to bring back.

## Pages

**Today.**
- **Header.** "Sunday 27 September" (the weekday in ink, the date muted; a click opens the last 14 days), the day's summary, and the day keys: the day before, the day after (off on today), and Today when looking back.
- **Displays.** At the PC (hh:mm, the accent, large), away, on calls, app switches, in Doto.
- **The skyline** (`skyline.py`). 96 quarter-hours: the near ridge is the minutes at the PC (lightly smoothed), the hill behind adds the minutes away with the PC on. "Nothing recorded" and "PC off / asleep / stopped" drop to the ground, the ground line breaks into dots and the reason is written there. Calls ride above as one thin cool-grey band with "Discord call, 5h 52m". Up to three peaks carry a flag: the app (and site) that held it and when. The land is one cached pixmap, rebuilt only when the data or the size changes.
- **A moment.** Hover picks a moment and a click pins it: a lit column over the stretch it belongs to (the session around it, else its hour), an accent hairline, a marker on the ridge and a dot-matrix time flag with the app's name. A click on the call band picks the call (its real times, even across midnight, and the microphone drops).
- **Under the chart:** the moment panel (its start in dot-matrix; what it was; "Editor 47m, Game 8m, Call 3m, from 20:00 to 21:00"; what Yuki noticed) and the open to-dos (an LED lit when due within three days). Both are fixed-size and painted, so hovering never moves the page.

**Me.** Work and projects, your to-dos (Done / Not needed / Not mine), interests, routines, people (initials on small keys), personality (how you talk, what you care about, running jokes, stories, how you are with people) and what Yuki may have misheard. The header's small display says when the portrait was rebuilt; a click rebuilds it.
- **Where each item came from.** "You told Yuki, today", "Seen in Discord, yesterday. Said as a joke".
- **Why?** Folds open the journal entries the item rests on, in a glass inset. **Not right** folds open a composer: the user types what is true in their own words, or "It's just wrong, forget it", through `correct_portrait` (unchanged).

**Yuki.** How Yuki talks to you, what it learned to do, what it said it would do (left); its personality with you and the questions it asked, with your answers (right).

**Status.** Four displays (things seen today in the accent while memory runs, facts written, calls waiting, dollars today), one row per thing with its one action (remembering your day: Pause; calls waiting: Erase; a call on: Stop; questions: Answer; the coach: Quiet 1 hour / Until tomorrow; listening for "Yuki"), and a Health module (memory service, last thing seen, portrait rebuilt, microphone, speaker, Start with Windows, last check-in), each with an LED.

**Chat.** The thread: your messages on glass on the right ("You, 01:15, typed"), Yuki's on navy panels on the left ("Yuki, 01:15, spoken", Copy), a day rule where the day changes, check-ins between two rules with a small grey wave, and under a response the quiet "worked for 9 s, 3 steps" fold (steps under a thin rule: label, result, time). While Yuki thinks, the thinking line is a dotted wave with its summary in muted words; the reply streams in. The composer is a glass box with a send key; the hint under it shows its keys as keycaps.

**Settings.** Voice (listen for "Yuki", microphone, speaker, Yuki's voice, the talk shortcut as keycaps), Coach (check-ins, quiet hours on two small dot-matrix displays with arrows, questions), General (Start with Windows, "Open Yuki anywhere" as keycaps you set by pressing, reduce motion, accent), Calls (Always / Ask / Never per app), Privacy (pause memory, blocked apps and sites, what is always private) and a one-line Advanced fold (model, effort, logs).

## Floating surfaces

- **Ask bar (overlay):** Horizon's layout: the thread, then a rule edge to edge and the input ("Ask Yuki", Segoe UI Variable Display Light 18 px) with its shortcut in quiet words on the right ("Alt Shift"). Only the user's own lines have a subtle bubble (`#161C26`, Display Light 16 px); Yuki's words stand plain with "Spoken at 21:04"; a check-in sits inline between two rules with its small wave ("Check-in at 21:40").
- **Check-in card:** a small cool-grey wave and the time, the text at 15 px, Dismiss (ghost) and Reply (light pill). Another kind says its kind beside the wave ("Weekly review"); a question leads with the question in Semibold, what it rests on under it, and Skip / Don't ask / Answer.
- **Call consent card:** an accent LED and "Discord started a call", the time, "Remember what you say in this call?" (19 px Display), what is and is not recorded, No and Yes, and under a rule "Never for Discord" and "Always for Discord". Without headphones it says why it would wait, in plain ink.
- **Call pill:** the accent recording dot, a small grey wave, the running time on a Doto timer (37:12) and "Remembering your side, Discord". Waiting for headphones: no dot, no timer. It ticks once a second only while shown.
- **Voice pill:** a wave and a word (listening, hearing, thinking, speaking, working); an error widens it and says what failed in red, with no wave.
- **Status strip:** the dotted thinking line and the step in words while a task runs; the final word after.
- **Tray menu and submenus:** a floating panel; the status line at the top ("Remembering today, 1,204 things") with its LED lit while memory runs; a subtle fill under the pointer.
- **Tray panels** (what Yuki knows, today's list, the week's review, health check): floating panels; headings in muted ink (never the accent); an overdue to-do in bold ink (no amber).
- **Onboarding wizard:** the same palette; the four steps as small keys across the top, the current one lit with the accent LED; pill buttons; the title in Display Light.

Behaviour (focus, fades, queueing, never taking focus) is unchanged. yuki-ui builds the ask bar, strip, cards and pills at start, kept hidden, and each warms itself on its first idle turn (native window, style, layout: `SurfaceWindow.warm`), so a hotkey or an event shows it within a frame.

## Smoothness (2026-09-28, the user: "everything performance-wise maximum and smooth")

- **Paint first.** The window process paints its frame, sidebar and the page's skeleton before it imports or opens memory (a worker thread does both, then `WindowApp.attach_memory`).
- **Snapshots.** The last data of Today, Me, Yuki, Status and Settings is kept in `window-snapshot.bin` next to `ui.json`, pickled, compressed and encrypted with memory's own field key (AES-256-GCM: authenticated before anything is unpickled; another database's or an older file is ignored; yesterday's Today is dropped). A reopened window shows the snapshot at once and refreshes; a page whose fresh data is the same is not rebuilt.
- **Prefetch.** Once Today is up, the data of Me, Yuki and Settings is read in the background (one at a time), so a first visit only builds widgets.
- **Never block the GUI thread.** Memory reads, the pipe and the audio device list (`WindowApp.refresh_devices`) run on worker threads; a device menu shows the last list and refreshes it for next time.
- **No jumps.** A page's placeholder is made in its own slot when first shown; a rebuilt page is swapped in with its slot's updates held until it is laid out and scrolled back to where it was; the Today panels are fixed-size and painted.
- **Cached layers.** The skyline's land is one pixmap per size, and a moment is computed once per stretch (`Skyline.moment`); dot-matrix strings are cached pixmaps (`paint.DOTS`); floating shadows are cached per size.
- **Long threads.** The chat builds its newest 12 bubbles before it first paints and fills in the rest of the newest 60 above them, 6 per turn of the event loop, the view staying on the newest (opening Chat on a 300-item thread was one 440 ms freeze).
- **Python itself.** The window process hands the GIL between threads every 1 ms (`tune_threads`: a worker decrypting memory no longer holds the GUI thread for whole frames) and, once every page's data is in, collects and freezes its heap (`settle_heap`), so a later full collection walks only new objects (a 100-180 ms pause mid-hover before).
- **Measured 2026-09-28** (offscreen, DB copy, the shadcn build vs this one): process start to first paint 0.68-0.73 s -> 0.41-0.43 s (Today filled 0.78-0.83 -> 0.87-0.90 s: it reads the to-dos too); skyline hover frame median 6.2 / max 30 ms (the old map) -> 1.4 / 4.4 ms; Chat opening on 300 items 230-340 ms -> ~105 ms to first paint, no gap over 14 ms after; chat scroll 2.5-3.1 -> 2.6 ms, streaming append 4.6-5.0 -> 4.2-4.7 ms; ask bar hotkey to painted, first 14-15 -> 10-18 ms, then ~5 -> ~7 ms; worst stall in a session ~80 ms both (the Me page's first build); window private RAM Today 58 -> 64 MB, all pages 81 -> 84 MB; tray surfaces built 37.6 -> 43 MB, shown 42.5 -> 45.3 MB.
- **Stall watchdog** (`yuki/ui/stall.py`, debug only: `YUKI_STALL_WATCH=1`): a heartbeat on the GUI thread and a checker thread log every stall over 32 ms with the GUI thread's stack to `logs/ui-stalls-<date>.jsonl`, in the tray and the window.

## Real vs placeholder in the phase-1 mockups

- **Real:** every number, lane, call, session, episode, fact, portrait item, evidence entry, rule, commitment, question, nudge, chat turn, device, quiet hour, call preference, cost, and the Start with Windows state. They come from the DB copy, `privacy.toml`, `voice.json` and the Run key.
- **Placeholders:**
  - the wallpaper;
  - the "6 s · 2¢" meta line on overlay cards;
  - "step 2 · 14 s" on the status strip;
  - the composer's example text;
  - the persona and talk lines, which paraphrase `agent/prompt.py`;
  - which Me item is shown expanded or composing (picked by `pick_examples`);
  - the hover position (17:30).
- **Empty in the real data** (shown empty, not faked): know-how, and milestones (the journal's `milestone` flag is not set on any row yet).

## Phase 2 (built 2026-09-26, offscreen-tested; needs the user's live check)

**Process.** A tray left-click (or the tray's "Open Yuki window") runs `pythonw -m yuki.ui.window` (`yuki/ui/window/app.py`): one window per session (a second start brings the first forward through `Local\YukiWindowFocus`), and the process exits when the window closes, so it costs 0 MB while closed. Reads and writes go straight through `MemoryClient` on background threads. Pages show "Reading your memory…" until their data arrives. Today re-reads itself each minute while it shows today.

**Pipe to yuki-ui** (`yuki/ui/window_ipc.py`, served by `yuki/ui/window_host.py`). It follows the same pattern as `embed_ipc`: local only, a user-only DACL, and an HMAC challenge both ways with a key derived from memory's data key. Messages are JSON. What it carries:
- chat submit and cancel;
- voice: wake word, microphone, speaker, voice id and devices;
- model and effort;
- snooze, start memory, open logs;
- "ui settings changed", so the overlay, cards, pill and strip restyle live.

yuki-ui pushes the live thread's events and live state (voice state, conversation, devices). When yuki-ui isn't running, the window still works: chat says so, and voice choices go straight into `voice.json`.

**Chrome** (`chrome.py`, `live.py`). It is frameless, but the caption and thick-frame styles are added back, with `WM_NCCALCSIZE` returning 0 and `WM_NCHITTEST` answering. That gives drag (the top band and the sidebar's head), resize edges, Aero Snap, Windows 11's snap layouts on the maximise button (`HTMAXBUTTON`), rounded corners and DWM's shadow (with dark mode and a `#27272a` border since the flat design). The window's place is kept in `ui.json`.

**Every button is wired** (`actions.py`, implemented in `app.py`):
- **Me:** corrections go through `correct_portrait` as `About "<subject>" (memory had: "<text>"), the user said in their own words: "<words>"`. "It's just wrong, forget it" and "That's right" send those words.
- **Yuki:** `revoke_rule(id)`, `remember_rule`, `answer_question` / `skip_question`.
- **Status:** `erase_call_audio` (asks once), `stop_call`, snooze, pause, the wake word, Start with Windows.
- **Settings:**
  - `voice.json` (through yuki-ui);
  - `privacy.toml`: `[user]` blocklists and `[nudges]` enabled, quiet hours and questions, edited in place with comments kept;
  - `set_call_pref`;
  - the Run key;
  - model and effort;
  - `ui.json`.
- **Today:** the day picker and a 14-day menu. The timeline zooms with the wheel around the pointer, a drag across a range, the arrows and +/-; double-click or "Whole day" resets. Clicking a lane's name highlights that app.

**MemoryClient reads added** (in a separate section at the end of `api.py`): `day_timeline`, `journal_entries`, `journal_by_ids`, `portrait_facts_with_evidence`, `rules`, `recent_turns`, `app_colors`. The 14-day colour rank is read each time; it is not persisted, which is still open.

### The live chat (window Chat page and Alt+Shift overlay)

There is one `ChatModel` (`yuki/ui/chat_model.py`, pure Python) in yuki-ui, fed by the runtime's signals, by what the user typed, and by the voice side's `chat_event`. The overlay shows it (`Overlay.attach_thread`; its card calls return stand-ins, so `app.py` is unchanged apart from two lines that record what was typed). The window keeps a replica: a snapshot on connect, then the same events.

Every message is its own bubble when sent, typed or spoken. Yuki's words stream in as they become audible. A barge-in cuts the bubble at what was heard, marks it "interrupted", and the user's words become the next bubble. Turns said to others on a call are dimmed. Hands and thinker jobs started by voice nest under the response that started them.

Under each response, a "worked for 12 s · 5 steps" fold lists every tool call and action: the label, the short result, ✓ or ✗, and the duration. It is open while running and closed when done. Text is selectable, and each response has Copy.

Bubbles are built only for the newest 60 items (older ones load on scroll or "Show earlier"). The model keeps 1,000 items. The only timer is the one-second "working 4 s" tick, and it runs only while something runs.

History when the thread is empty: the latest session log with its real tool steps, else memory's turns, plus the coach's recent cards.

**Voice events consumed** (the voice side emits them as `VoiceSession.chat_event`):
- `user_utterance{text, ts, turn_id}`;
- `assistant_speech{turn_id, text, final, interrupted}`;
- `hand_step{turn_id, name, args, ok, result, ms}`;
- `job_event{job_id, state, summary}` (only `submitted` is used; the rest comes from the runtime);
- `turn_closed{turn_id, addressed}`.

A job that arrives before its turn's words is adopted by that turn's bubble.

### Steps under a response (2026-09-27)

Each top-level step's time runs from the end of the step before it (or from when the message was sent) to its own end, so the model's thinking before a tool counts and the steps add up to "worked for" (`Item.step_times`); after the last tool a "Writing the reply" line takes the rest. Under a second reads "<1 s". Labels are plain words built from the tool's structured input (ISO dates as "yesterday", "on 20 Sep", "since 5 Jan"; record ids like `call:23` left out) and, once the result is in, from its structured payload ("Reading yesterday's 22:09 Discord call": `status.describe_result`, carried as `ToolResult.payload`, then `AgentRuntime.tool_labelled`, then the chat event `tool_label`). A streamed reply or thinking line shows only once it has three words (a lone first letter never shows).

### Measured: flat vs glass (offscreen, 1x, DB copy; each a fresh process)

| | Glass Off | Glass Light (default) | Glass Full | Flat (now) |
|---|---|---|---|---|
| Window open, Today (private) | 69 MB | 96 MB | 126 MB | 60 MB |
| Window, all 6 pages built | 118 MB | 148 MB | 323 MB | 84 MB |
| yuki-ui surfaces (overlay + card + strip + pill, shown) | +25 MB | +36 MB | +90 MB (GPU device) | +22 MB |
| GPU device loaded | no | no | yes | no |

**Needs the live desktop** (rule 7 kept it offscreen):
- the frameless chrome: drag, resize, snap layouts, maximise inset;
- DWM Acrylic in Off;
- focus hand-off from the tray;
- the pipe against the running yuki-ui;
- real voice turns streaming;
- RAM with the backing store and DWM included.

## Phase 3 (2026-09-27, the user's review)

Its glass parts were superseded by the flat design the same day. Still true:
- **Smooth scrolling** (`SmoothScroller`): eased wheel, pixel-exact touchpad with a friction glide; one timer only while moving. Pages are opaque so Qt scrolls by moving pixels.
- **Timeline:** the call band is clickable (call details) and hovers with its real times; a session's app list shows the call as a row.
- **Me:** built parented and progressively (skeleton placeholders); corrections show "Updating…" in the user's words until `correction_status` says done (kept in `window-pending.json`).
- **Yuki page:** How Yuki talks to you, Yuki's personality with you, what Yuki said it would do, Learned to do, Questions.

## Phase 4 (2026-09-27, round 4 review)

Its glass parts were superseded. Still true:
- **Chat:** typed replies render Markdown (`yuki/ui/markdown.py`); a plain reply shows no "worked for" fold; timing runs from the message to the reply; the reply streams from the `done` tool's partial input and the thinking summary shows as one muted line (`agent/stream_partial.py`, event `partial`).
- **Pages:** masonry layouts; one "Your to-dos" list on Me (Done / Not needed / Not mine); no tags or badges (`widgets.tag_words`); display text cleaned (`data.plain`); Status is status only; quiet hours with an on/off switch and a compact scrolling picker (`widgets.ListPopup`); "Open Yuki anywhere" set by pressing it (`widgets.KeyCapture`, `ui.json open_hotkey`, re-registered live by `YukiUi.reload_hotkeys`).
