# Yuki

Yuki is a voice and text assistant for Windows. It reads what is on screen through Windows UI Automation (never screenshots), acts in your apps, and keeps an encrypted local memory of your activity that you can ask about later.

This is an early version (0.1.0). It runs only on Windows and uses Claude models to understand requests.

## What it does

- Takes requests by voice, starting with the wake word "Yuki", or typed into an ask bar.
- Acts on the desktop: apps and windows, browser tabs, media and volume, monitor brightness, messages in chat apps, and clicks, typing and PowerShell in any window.
- Does lasting actions (sending, deleting, closing, running commands) only when you ask for them directly, and asks first otherwise. A message is sent only after a short countdown that you can cancel.
- Remembers what it sees: dated facts from on-screen text, a timeline of app use, threads of work, calls (only with your consent) and a weekly review.
- Lets other AI tools read that memory through a read-only MCP server.

## Requirements

- Windows 10 version 1809 or later, x64
- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- A Claude key: `ANTHROPIC_API_KEY`, or `AWS_BEARER_TOKEN_BEDROCK` for Amazon Bedrock
- For voice: a microphone and `DEEPGRAM_API_KEY`

## Run from source

```powershell
uv sync
.venv\Scripts\python.exe -m yuki.onboarding
.venv\Scripts\yuki.exe doctor --offline
.venv\Scripts\pythonw.exe -m yuki.ui.boot
```

`yuki.onboarding` is the first-run setup (keys and privacy choices). `doctor` checks the install, and `yuki.ui.boot` starts the tray app. Settings, logs and memory live in `%LOCALAPPDATA%\Yuki`.

## Build the installer

```powershell
uv sync --group build
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

This builds `packaging\Output\YukiSetup-<version>.exe` with PyInstaller and Inno Setup 6. The installer is per-user and needs no admin rights.

## Tests

```powershell
.venv\Scripts\python.exe scripts\sweep.py
```

This runs the offline test suite, one file per process. Tests that call real models are opt-in.

## Privacy

Memory reads window text, never pixels, and never records keystrokes. By default it skips password fields, private browser windows, password managers, and sign-in, banking and payment sites. The rules are in `%LOCALAPPDATA%\Yuki\memory\privacy.toml`.

Stored content is encrypted with AES-256-GCM, with the key protected by Windows DPAPI. Requests and the on-screen context they need go to the model provider. During a voice conversation, microphone audio goes to Deepgram.

## Layout

- `yuki/`: the app (voice, brain, memory, skills, actions, perception, ui, mcp)
- `packaging/`: the installer build
- `scripts/`: developer scripts
- `tests/`: the test suite

## License

There is no license file yet.
