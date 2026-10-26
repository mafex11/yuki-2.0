# Yuki

Yuki is a desktop assistant for Windows that you talk or type to. Say "Yuki", press Ctrl+Alt+Y, or press Alt+Shift to type, then tell it what to do: "Yuki, close all the YouTube tabs." It manages windows and browser tabs, controls media, volume and monitor brightness, plays music in the Spotify app, and sends messages in Discord, WhatsApp and Slack. It also keeps an encrypted memory of what you do on the PC, so you can ask what you worked on yesterday or what was decided on a call.

It is meant for one person on their own Windows PC who is fine with sending requests and on-screen text to a cloud AI service. It reads the screen as text through Windows UI Automation and never takes screenshots.

Limits: it runs only on Windows. It needs a Claude key (Anthropic API or Amazon Bedrock). Voice also needs a Deepgram key, unless you use the local English-only recognizer. This is an early version (0.1.0).

To try it, check the [requirements](#requirements), then follow [running from source](#running-from-source). You can also [build the installer](#building-and-testing) yourself.

## Requirements

- Windows 10 version 1809 (build 17763) or later, x64. Recording the other side of a call needs build 19041 or later.
- Python 3.13 and uv, to run from source.
- A Claude key: `ANTHROPIC_API_KEY`, or `AWS_BEARER_TOKEN_BEDROCK` for Amazon Bedrock (`AWS_REGION` is optional and defaults to `ap-south-1`).
- For voice: a microphone, speakers or headphones, and `DEEPGRAM_API_KEY` for speech recognition (not needed with the local English-only option). `GOOGLE_API_KEY` gives the default Gemini voice; without it, Yuki uses Deepgram Aura. `ELEVENLABS_API_KEY` is optional.
- An NVIDIA GPU is optional. Only the call transcriber uses it. Without one, it runs the smaller faster-whisper `small` model on the CPU, which is slower and less accurate.

## Running from source

Clone this repository, then run these from its folder in PowerShell:

```powershell
uv sync
.venv\Scripts\python.exe -m yuki.onboarding
.venv\Scripts\yuki.exe doctor --offline
.venv\Scripts\pythonw.exe -m yuki.ui.boot
```

1. `uv sync` installs the dependencies and pytest.
2. `yuki.onboarding` is the first-run setup: model key, voice keys, privacy choices, and a download of the memory search model and the wake-word model. It saves keys as user environment variables. The installed app runs it automatically. From source, run it once or set the variables yourself.
3. `yuki doctor` runs a read-only health checklist and exits with code 1 if a check fails. `--offline` skips the checks that call the model and voice services.
4. `pythonw -m yuki.ui.boot` starts the tray without a console, the same way Windows starts it at sign-in. The tray starts the memory service. `.venv\Scripts\yuki-ui.exe` starts it from a console instead; closing that console ends Yuki.

If the doctor reports a missing wake-word model, fetch it (about 18 MB):

```powershell
.venv\Scripts\python.exe scripts\voice_models.py
```

Things to know:

- The first tray run turns on Start with Windows once, by adding a `Yuki` value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. Delete that value to turn it off.
- Do not run `uv sync` or `uv run` while Yuki runs from the same venv, because its executables are locked. Call `.venv\Scripts\python.exe` directly.
- A source checkout and an installed copy share `%LOCALAPPDATA%\Yuki`, so they share one memory.
- Logs go to `logs\` in the repository folder (`%LOCALAPPDATA%\Yuki\logs` when installed).

To list or stop Yuki's processes:

```powershell
.venv\Scripts\python.exe -m yuki.core.procs list
.venv\Scripts\python.exe -m yuki.core.procs stop yuki.ui.boot yuki.memory.service
```

The `yuki` command also has these modes:

```powershell
.venv\Scripts\yuki.exe
.venv\Scripts\yuki.exe cost today
.venv\Scripts\yuki.exe trace
```

With no arguments it opens a terminal REPL on the older step-by-step agent (`/model`, `/effort`, `/cancel`, `/quit`). That agent can use the mouse and keyboard. `cost today` prints the day's estimated AI spend. `trace` prints counts from "Record how I work".

These settings can be set as environment variables or as keys in `%LOCALAPPDATA%\Yuki\settings.json`:

- `YUKI_BRAIN` or `brain`: `all` (default), `one`, `loop` or `typed`. An unknown value counts as `all`.
- `YUKI_SKILLS` or `skills`: `on` (default), `capabilities` (built-in skills only) or `off`.
- `YUKI_MODEL_BACKEND` or `model_backend`: `anthropic` or `bedrock`. If neither is set, a Bedrock token picks `bedrock`, else an Anthropic key picks `anthropic`.
- `YUKI_MEMORY_DIR` moves the memory folder.

## Using Yuki

Say "Yuki" to start a voice conversation. You can give the request in the same breath: "Yuki, pause the music." A small sherpa-onnx keyword spotter listens for the name on the CPU, in its own process. Until it hears its name, it throws away each 20 ms block of audio, and nothing is saved or sent. Ctrl+Alt+Y also starts or ends a conversation. Alt+Shift on its own opens the ask bar for typing; Yuki stops that press from switching the input language, and Win+Space still switches it. Alt+Shift+X cancels the running task.

While a task runs you can change it, by voice or by typing in the chat drawer of Yuki's window: add to it, answer its question, or change your mind and replace it with something else. Talking over Yuki stops its voice, not the work. Saying goodbye ends the conversation. Otherwise it ends after 20 seconds of quiet, plus 8 seconds for each further exchange, up to 45 seconds. "Stop listening for an hour" turns the wake word off for that long. In a call with other people, Yuki answers only when you say its name or are plainly still talking to it. With a full-screen game in front or a call on, its replies are short.

Speech recognition is Deepgram Flux by default. A local Parakeet option works in English only. The voice is Gemini TTS by default, with Deepgram Aura as the fallback; ElevenLabs and local voices are options. On speakers, an echo canceller (WebRTC AEC3) removes Yuki's own voice from the microphone, so headphones are not required.

What you can ask:

- Windows and apps: open apps by their Start-menu name; focus, minimize, maximize, restore or close windows; move a window to another monitor and back. "Move this window to my other monitor."
- Browser tabs, in any browser: switch to, close, open and reopen tabs; open an address, a web search or a Windows settings page. "Reopen the tab I just closed." "Search the web for how to clear the DNS cache." "Open Bluetooth settings."
- Media and volume: play, pause and skip through Windows media controls; set one app's volume or the whole PC's. "Mute Chrome." "Turn Spotify down a bit."
- Monitors: brightness, and screen off, per monitor over DDC/CI. "Dim both screens." "Turn off the second monitor."
- Spotify: a playlist, album or Liked Songs in the Spotify desktop app, with no Spotify account or key. "Play my focus playlist on Spotify."
- YouTube: open a channel or a video, or pick a video from what you watch. "Put on a video I'd like."
- Messages: send or draft messages in Discord, WhatsApp and Slack. "Tell Jordan on WhatsApp I'm running ten minutes late." "Draft a reply to Jordan on Slack."
- Calls: join a Discord voice channel, or bring a meeting tab forward. "Join the general voice channel on Discord." "Bring up my meeting tab."
- Anything else in a window: click, type (typing never presses Enter on its own), scroll, and run PowerShell commands.
- Memory: "What did I work on yesterday?" "How long was I on YouTube this week?" "What did we decide on this morning's call?" "Continue where I left off."
- Lists and rules: "Remind me to send the invoice tomorrow at 10." "Tell me when Jordan replies." "From now on, keep your answers short."

Where an app has its own controls (UI Automation patterns, Windows media sessions, Core Audio, launch arguments and URIs), Yuki uses them instead of the mouse and keyboard.

The rules for acting are in code (`yuki/brain/consent.py`):

- Steps that can be undone (focus, open, switch tabs, volume, media, scroll, typing without Enter, reads): Yuki does them, then tells you.
- Lasting steps (send, join, delete, close, buy, change a setting, run a shell command, a click that commits) run only if your request asks for them directly. Otherwise Yuki asks you once.
- A message is sent only when your words say to send it and give the text. Otherwise it is left as a draft. Before a send, a 3-second countdown shows the exact text and the recipient. Stop or Esc cancels it, and speaking pauses it.
- Yuki never clicks Join on a meeting. It brings the meeting tab forward and leaves the click to you. It joins a Discord voice channel only when you ask, and asks first if you are already in a call.
- With a game in front, Yuki does not take focus, type, launch apps or open links. Volume and media still work. Games are recognised from a packaged list of kernel anti-cheat games, from Windows Game Bar, and from programs seen in exclusive full screen.
- If you press a key, click, or move the mouse a fair distance (40 px by default) while Yuki works, it pauses and offers Resume and Stop.
- Settings has an autonomy dial: seven kinds of action, each on one of four levels (Just suggest, Show me the plan, Ask before doing, Do it and tell me). Sending messages and spending money can never be set above Ask before doing. Each app also has a Look switch (Yuki may read it) and an Act switch (Yuki may control it).

Yuki never speaks first. What it notices comes as a card: a reminder, a check-in, a "while you were away" note, a break nudge after a long session. Cards wait during games, calls, meetings and quiet hours (00:30 to 08:00 by default), then appear at the next natural break, at least 10 minutes apart. A to-do due at an exact time is reminded on time, and reminders you set and "tell me when" answers still arrive if you turn Yuki's own cards off. A card button never hands a request to the AI agent. It can add something to your list, show what Yuki holds, bring back a page you had open, give you a draft to copy, or put a suggested request in the ask bar without sending it. The "Your usual" card offers to run a routine Yuki learned from you, around its usual time. Code runs it, without a model, and it never sends a message or joins a call.

Yuki's window has six pages (Home, Work, Me, Playbook, Activity, Settings) and the chat drawer. On Me you can correct, forget, mark as not yours, or pin anything Yuki holds about you. The island is a small dot-matrix face at the top centre of the monitor in use. It shows listening, a live caption, the current step with a Stop button, and the send countdown. It hides during a full-screen game, video or presentation, and while the session is locked. Click the tray icon to open the window. Right-click it to stop listening for the wake word or to quit.

## Memory and privacy

The memory service (`yuki.memory.service`, also installed as the `yuki-memory` command) reads the text of every window on screen, not only the one in front, and keeps:

- short dated facts, written every few minutes by a small model from the captured text;
- a timeline of which app or site was in front and for how long, cut into episodes at breaks;
- a one-page portrait of you, rebuilt each night;
- threads of work: projects and tasks across days, and where each one stands. "Continue where I left off" reads from these;
- your rules, corrections, to-dos and "tell me when..." requests;
- calls, only when you say yes (see below);
- a weekly review, written on Sunday evening (after 20:00, once you have been idle for 5 minutes): time against the week before, focus, what got done and what is still open. The numbers are computed in code.

With "Record how I work" turned on (it is off by default), Yuki also learns multi-step ways you do things in one app, from at least three demonstrations on three different days, or right away if you tell it the steps.

Memory reads text through UI Automation, never pixels. It also reads the Warp terminal's command history (command, folder, git branch and exit code, never output); this is on by default. It never records the letters you type. To tell if you are at the PC, it counts key and mouse events and notes their times. "Record how I work" keeps shortcuts and which controls you used, never typed text.

By default (`yuki/memory/privacy_default.toml`) memory never reads:

- anything while the session is locked, or the content of a full-screen app;
- password fields, and nothing at all while one is on screen;
- private and incognito browser windows;
- password managers, authenticators and Windows sign-in dialogs;
- sign-in, banking, payment and meeting sites, and a list of adult sites;
- apps and sites on your own block list.

Meetings are kept as times only. Secrets in known formats (API keys, tokens, private keys, JWTs, card numbers) are removed before anything is stored. The filter knows fixed formats only, so a password typed into a chat or said in a call is stored as it is.

The rules are copied on first run to `%LOCALAPPDATA%\Yuki\memory\privacy.toml`. Edit that copy. Changes apply without a restart, and a file that fails to parse keeps the last good rules.

Calls are recorded only after you answer Yes on that call's card, or "Always" for that app. No answer means nothing is kept. Yuki records your microphone (only when the output is headphones) and the call app's own sound through process loopback; for a browser call that is the whole browser's sound. The audio is encrypted on disk, transcribed later on this PC with faster-whisper when nothing is full screen and the GPU and CPU are quiet (by default this can happen while a call is still open), then deleted. Each line of the transcript says who spoke. Snoozing cards does not stop recording for an app set to "Always".

Memory lives in `%LOCALAPPDATA%\Yuki\memory\memory.db`. Content columns are encrypted with AES-256-GCM. Metadata used for filtering (times, app names, site hosts, counts) is stored unencrypted. The data key is wrapped with Windows DPAPI for the current user and stored next to the database as `memory.key`. This protects the data from other Windows accounts and from a copied file, but not from other programs running under your account. Raw captures are deleted after 30 days. Facts, the portrait and know-how are not deleted by age.

Logs are content-free, except memory's model-call logs, which keep each request and response encrypted with memory's key. `YUKI_LOG_CONTENT=1` keeps the words in the agents' session logs and `requests.csv`.

What leaves the PC:

- To the model service (Anthropic or Amazon Bedrock, or a provider you picked in Settings > AI): each request with context from memory and the text of the windows on screen; captured text after the privacy rules and the secret filter; facts for the nightly portrait and the weekly review; call transcripts, which are made locally.
- To Deepgram, during a voice conversation: your microphone audio, with up to 65 names from memory and up to 30 from the screen as recognition hints.
- To the voice service (Gemini TTS, or Deepgram Aura as the fallback): the text of Yuki's replies.
- One-time model downloads, and anonymous reads of public YouTube pages for channels you watch.

The database, call audio, wake-word audio and embeddings stay on the PC.

Controls and caveats:

- The Pause button in Settings > "Privacy and pause" stops capture and call recording until you press Resume.
- Quit Yuki does not stop the memory service. To stop capture, use that Pause button, or stop the service:

  ```powershell
  .venv\Scripts\python.exe -m yuki.core.procs stop yuki.memory.service
  ```

- First-run setup asks you to turn memory and call memory on or off. The installed app shows it only when no Claude key is set. If a key already exists, setup is skipped and the defaults apply: memory on, and each call asks first.
- The model and voice keys are plain Windows user environment variables. Keys typed into Settings > AI are stored DPAPI-encrypted in `%LOCALAPPDATA%\Yuki\ai-keys.dat`. Uninstalling does not remove the environment variables.
- You pay your model and voice providers per use, and memory makes model calls in the background even when you ask nothing. `yuki cost today` shows the day's estimated spend. There is no spend cap. Yuki shows one notice on a day whose spend is unusually high (about 1.5 times a usual day, and at least $3).

Other AI agents can read this memory through `yuki-mcp`, a stdio MCP server with 13 read tools: portrait, search, activity, episodes, know-how, standing rules, to-dos, weekly review, calls, call timeline, threads, links and related items. It has no write tools and makes no cloud AI calls. Search loads the local embedding model (about 270 MB) into the server, and opening the database may apply a pending schema update. Registering it gives that agent your decrypted memory, so add it only to agents you trust. To register it with Claude Code, replacing `C:\path\to\yuki` with your clone's folder:

```powershell
claude mcp add yuki-memory -- "C:\path\to\yuki\.venv\Scripts\python.exe" -m yuki.mcp.server
```

Put `-s user` before the name to use it from every project. `YUKI_MCP_DISABLED=1` makes the server refuse to start.

## How it works

Yuki is a set of Python 3.13 processes:

- The tray (`yuki.ui.boot`, then `yuki.ui.app`) owns the hotkeys, the island, the cards, the voice session and the agent runtime. It starts the memory service and restarts it if it stops. Its worker thread and the voice side's one-step actions take turns on the mouse and keyboard through one input lease (`yuki/actions/leases.py`). A read-only helper never sends input.
- The memory service (`yuki.memory.service`) runs at below-normal priority. It captures, writes the facts and the portrait, runs the cards and records calls. It also serves the embedding model, and an in-memory copy of the text on screen that is never written to disk, to the other processes.
- The wake-word spotter (`yuki.voice.wakeproc`) gets 20 ms microphone blocks on stdin and answers with JSON lines.
- The window (`yuki.ui.web_window`) runs only while it is open. It is HTML in WebView2, with a Qt window as the fallback, and loads local files only, under a content security policy with `connect-src 'none'`.
- The call transcriber (`yuki.memory.transcribe`) starts when the PC is quiet, transcribes, and exits.

The processes share one SQLite database in WAL mode. Live data moves over local named pipes that refuse remote clients, allow only the current user, and run an HMAC challenge in both directions. Messages are JSON (embedding replies add raw vectors after a JSON header); nothing is pickled.

The `brain` setting picks how a request runs. With the default, `all`, a fast model (the voice talker) handles spoken requests that need one action, such as pausing music, and hands anything bigger to the planner with your exact words. The planner (`yuki/brain`) makes one streamed plan call. An executor runs each step as it arrives, under the consent rules above and a commit check (a separate model call that approves actions). Each action ends with a check on the PC, such as reading the window again, and Yuki reports a task as done only after that check confirms it. Other values: `one` (a single agent owns the whole turn under a time and round budget), `typed` (the planner for typed requests only) and `loop` (the older step-by-step agent, kept as a rollback).

By default the planner, the voice talker and the `one` agent use Claude Sonnet 5.5. The commit check, voice side calls and the memory journal use Claude Haiku 4.5. The portrait, the weekly review and the card writer use Claude Sonnet 5. A failing call is retried twice, then moved along a fixed chain: usually the same model in another region or profile, then another Claude model (the Haiku 4.5 jobs try Claude Haiku 5.5 first). Settings > AI can move each of three jobs, "Voice replies", "Doing things" and "Memory upkeep", to OpenAI, Gemini, OpenRouter, DeepSeek, Ollama, any OpenAI-compatible service, or Anthropic with your own key. The commit check stays on Claude whichever service you pick, so a Claude key is always needed.

Capabilities such as volume, Spotify, messages and monitors are TOML files in `yuki/skills/connections/`. Each one tries routes in order: the app's own controls first, then a recorded UI Automation path, then the full agent with the skill as the starting plan.

`yuki/perception` reads windows through `EnumWindows`, DWM and UI Automation. Element ids stay stable across reads, and a before/after diff shows what an action changed.

## Building and testing

```powershell
uv sync --group build
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

The script needs no admin rights. It runs PyInstaller with `packaging/yuki.spec` to build `packaging\dist\Yuki` (`Yuki.exe` without a console, `yuki-cli.exe` with one). It then runs an offline self-check that loads every native piece, and builds `packaging\Output\YukiSetup-<version>.exe` with Inno Setup 6. Options: `-SkipInstaller`, `-SkipSelfCheck`, `-InstallerOnly`, `-Clean`, `-Version`, `-Python`, and `-Iscc` for the path to `ISCC.exe`. Models are not bundled; they download on first use.

The installer is per-user, needs no admin rights, and installs to `%LOCALAPPDATA%\Programs\Yuki Assistant`. It offers "Start Yuki when I sign in to Windows" and a desktop shortcut. `Yuki.exe` runs the first-run setup when no Claude key is set. The uninstaller asks whether to delete `%LOCALAPPDATA%\Yuki` as well; the default is No.

CI (`.github/workflows/build.yml`) builds the installer on `windows-latest` for `v*` tags, manual runs, and pull requests that touch the app or the packaging. It signs with Azure Artifact Signing only when the signing secrets are set; otherwise the installer is unsigned. Tagged builds are attached to a GitHub release. CI does not run the tests.

The tests are in `tests/` and run offline. `tests/conftest.py` points the memory folder and the AI settings at temporary folders and turns off extra model calls. A single pytest process dies silently about a fifth of the way through the suite, so `scripts/sweep.py` runs one process per file, re-runs each failure alone, and reports which tests failed and which are flaky:

```powershell
.venv\Scripts\python.exe scripts\sweep.py
.venv\Scripts\python.exe scripts\sweep.py tests\test_call_*.py
.venv\Scripts\python.exe -m pytest tests\test_secret_filter.py -q
```

`--jobs N` runs N files at once (default 1), `--json PATH` also writes the summary to a file, and `--clock HH:MM` pins the wall clock for every test.

Tests that call a real model or open a real WebView2 window run only when `YUKI_LIVE_BAR=1`, `YUKI_LIVE_PEOPLE=1` or `YUKI_WEBVIEW2_TEST=1` is set. Some window tests skip when Node.js or a headless Chromium is missing.

One test in `tests/test_call_digest.py` reads a read-only copy of your real memory database if one exists, and skips otherwise.

## Code and docs

```text
yuki/
  actions/      input, UI Automation patterns, app launch, media, volume, windows, tabs, PowerShell
  agent/        tool dispatch, model failover, other AI providers, the older step-by-step agent
  brain/        the default plan-first brain: planner, executor, consent rule, commit check
  connect/      site and app data (URL templates, UI labels) and the Spotify desktop reader
  core/         backend choice, paths, process control, cost, the doctor and the self-check
  eval/         developer evaluations, run on a copy of the database
  log/          content-free logging and log retention
  mcp/          the read-only MCP server
  memory/       the memory service: capture, facts, portrait, calls, cards, encrypted store
  onboarding/   first-run setup
  one/          the single-agent brain
  perception/   windows, visibility, UI Automation trees, diffs
  skills/       capabilities as data, the route engine, the skill learner
  ui/           tray, island, ask bar, cards, agent runtime, HTML window and Qt fallback
  voice/        Pipecat voice pipeline, wake word, talker, one-step actions, local speech
  cli.py        the yuki command
  config.py     the runtime settings dataclass
  frozen.py     entry point of the installed Yuki.exe and yuki-cli.exe
packaging/      build.ps1, yuki.spec (PyInstaller), installer.iss (Inno Setup)
scripts/        sweep.py, voice_models.py and other developer scripts
tests/          the pytest suite
docs/           design notes
```

Not every setting lives in `config.py`. Memory, calls, cards and the weekly review are set in `privacy.toml`, and the voice, window, autonomy and AI-provider choices are kept in their own JSON files.

- `docs/ARCHITECTURE-NOW.md`: a map of the processes and the code as of 10 Oct 2026. Start here.
- `docs/ARCHITECTURE.md`: the design contract.
- `docs/VOICE.md`: the voice pipeline.
- `docs/MEMORY.md`: capture, privacy rules, calls and storage.
- `docs/UI.md`: the tray, the island and the window. `docs/UI-window.md` covers the Qt fallback window.
- `docs/MCP.md`: the MCP server and how to register it.

Some docs lag behind the code. Where they disagree, the code is right. Known cases:

- some docs give `loop` as the default brain; the code uses `all`;
- some name ElevenLabs as the default voice; the code uses Gemini TTS;
- `docs/MCP.md` lists 8 tools; the server has 13;
- `docs/VOICE.md` puts the voice indicator at the bottom of the screen; the island is at the top;
- `docs/UI.md` describes an older tray menu.

## Status and license

Version 0.1.0. The repository has no license file.
