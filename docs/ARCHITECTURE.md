# Yuki — architecture contract (v1)

Yuki is a hands-free assistant that lives on the user's Windows PC. The user types (later: speaks) a request; Yuki sees the desktop, thinks, acts, and talks back. It must be fast, able to pause and ask the user mid-task, and prefer commands/shortcuts over mouse clicks.

The old project at `C:\Users\esska\LLM-OS` is REFERENCE ONLY. Salvage low-level Windows code from it where useful; never import from it.

## Non-negotiable rules

1. **No hardcoded behavior heuristics.** No keyword lists, regexes, or string matching that decide what the agent does (no "if 'chrome' in name", no app allowlists, no "is this a question" detectors, no fixed staleness timers steering logic). Behavior comes from the model reading the situation. Deterministic code is allowed only for: plumbing, safety confirmation gating, and parsing structured API responses.
2. **No regex/XML parsing of model output.** Use native tool calling (Anthropic Messages API `tools` + `tool_use` blocks) exclusively.
3. **No fixed sleeps.** Wait for a condition (window appeared, focus changed, tree stable, process exited) with a timeout. Never `time.sleep(0.8)` to "let things settle".
4. **Log everything.** Every model request (full system, messages, tools), every response (full content blocks, usage, latency), every tool call and result, every perception snapshot, every question to the user and their answer, every error. JSONL, one file per session, never truncated. Plus a human-readable pretty stream on the console.
5. **No LangChain/LangGraph.** Plain Python + `anthropic` SDK (`AnthropicBedrock`) + boto3 + Windows libs.
6. **Keyboard/mouse input is guarded.** Every input action (`click`, `type_text`, `hotkey`, `press`, `scroll`) accepts `expect_hwnd`; when set, input is sent only if that window is still in the foreground, otherwise the action fails without sending. Tests and scripts must never send Alt+F4 or any key without `expect_hwnd`, and must close windows they opened by terminating the PID. (On 2026-09-22 an unguarded Alt+F4 closed the user's terminal and killed the build session.)
7. **Windows-only, Python 3.13, `uv`.** Project root `C:\Users\esska\yuki`. Package `yuki/`. Run with `uv run ...`.

## Model access

- Client: `anthropic.AnthropicBedrock(aws_region=os.environ["AWS_REGION"])`. Auth comes from the `AWS_BEARER_TOKEN_BEDROCK` user env var automatically. Do not put keys in files.
- Model ids (config, default first): `us.anthropic.claude-sonnet-5`, `us.anthropic.claude-opus-5`.
- Use `thinking={"type": "adaptive"}`; do not send `temperature` or `budget_tokens`. Parse tool inputs as JSON objects (already parsed by SDK).
- Verified working on 2026-09-22 (~2.5–3s round trip).

## Package layout

```
yuki/
  config.py          # dataclass Settings: model ids, log dir, screenshot policy (never|ask|auto, default auto)
  log/
    events.py        # event schema (TypedDicts/dataclasses) + SessionLogger (JSONL + pretty console)
  perception/        # "eyes"
    windows.py       # desktop overview
    tree.py          # UIA tree for one window
    screenshot.py    # screenshot of screen or one window, downscaled
    system.py        # system facts (processes, memory, cpu, time)
  actions/           # "hands"
    launch.py        # launch app via shell:AppsFolder / Start-Process; focus by hwnd
    input.py         # click/type/scroll/hotkey/key (no glide, no fixed sleeps)
    shell.py         # PowerShell runner with correct arg passing + timeout + persistent session
  agent/
    tools.py         # tool JSON schemas + dispatcher (maps tool_use -> perception/actions)
    prompt.py        # system prompt (short, behavioral, no rules-lists of keywords)
    loop.py          # Agent: run(request) -> yields events; supports ask_user pause/resume, cancel
    context.py       # context hygiene: replace stale large perception results with stubs; running summary
  cli.py             # terminal REPL
tests/               # smoke tests; perception/actions tests may require a live desktop
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
    bounds: tuple[int, int, int, int]   # left, top, right, bottom (screen px)

@dataclass
class DesktopOverview:
    windows: list[WindowInfo]     # all top-level, visible, non-tool windows (Explorer shell windows excluded, taskbar excluded)
    foreground_hwnd: int | None
    cursor: tuple[int, int]
    screen_size: tuple[int, int]
    captured_at: float             # time.time()

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

@dataclass
class WindowTree:
    hwnd: int
    title: str
    process_name: str
    elements: list[UIElement]      # depth-first order, capped (see below)
    truncated: bool                # True if cap hit
    captured_at: float
    elapsed_ms: float

def get_window_tree(hwnd: int, *, max_elements: int = 400, timeout_s: float = 3.0) -> WindowTree: ...
def format_window_tree(tree: WindowTree) -> str: ...   # compact text for the model, one line per element:  [id] role "name" value=... @(x,y) [kb: shortcut]
def format_overview(o: DesktopOverview) -> str: ...

def screenshot(hwnd: int | None = None, *, max_width: int = 1280) -> bytes: ...  # PNG bytes, downscaled preserving aspect
def system_facts() -> dict: ...                # time, uptime, cpu %, memory total/used, top processes by memory with pid/name/rss
```

Notes for the tree: walk via `uiautomation` from the window's control, depth-first, skipping offscreen elements, include every element that has a name/value or is interactive/scrollable. For Chromium/Electron windows (Chrome, Edge, Spotify, VS Code, Discord) the UIA tree exposes web content only if the accessibility bridge is on; do not special-case by app name — just walk what UIA gives you, and report `truncated`/element counts honestly so the model can decide to fall back to a screenshot. Walk in a worker thread with COM initialised (`comtypes.CoInitializeEx` / `pythoncom`) and enforce `timeout_s`.

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
def launch_app(query: str, *, timeout_s: float = 8.0) -> ActionResult
    # Resolve via Get-StartApps (AppsFolder). Return candidates when ambiguous instead of guessing:
    # details = {"launched": bool, "hwnd": int|None, "candidates": [{"name","appid"}]}.
    # Waits until a new window from the launched process appears, or timeout.
def focus_window(hwnd: int, *, timeout_s: float = 2.0) -> ActionResult   # restore if minimized, SetForegroundWindow, verify foreground
def click(x: int, y: int, *, button: str = "left", clicks: int = 1) -> ActionResult   # instant move, no glide
def type_text(text: str, *, press_enter: bool = False) -> ActionResult  # types into current focus; use clipboard paste for long/unicode text
def hotkey(*keys: str) -> ActionResult                                  # e.g. ("ctrl","t"), ("win","r"), ("volume_mute",)
def press(key: str, *, times: int = 1) -> ActionResult
def scroll(x: int, y: int, *, dy: int = 0, dx: int = 0) -> ActionResult  # wheel notches at a point
def run_powershell(command: str, *, timeout_s: float = 20.0) -> ActionResult
    # Pass the script via -EncodedCommand (UTF-16LE base64) or stdin; NEVER split on whitespace. Capture stdout/stderr/exit code (UTF-8).
def open_url(url: str) -> ActionResult                                  # default browser via os.startfile / Start-Process
```

## Agent contract (module `yuki.agent`)

- `Agent.run(request: str) -> Iterator[AgentEvent]`. Events: `thinking(text)`, `tool_call(name, input)`, `tool_result(name, ok, summary)`, `ask_user(question)`, `final(text)`, `error(text)`. When `ask_user` is yielded the generator pauses; the caller sends the answer with `agent.answer(text)` and continues iterating. State (messages, running summary) survives the pause.
- Messages are append-only. Tool results from perception that are older than the last 2 turns are replaced in-place with a one-line stub like `[desktop overview from step 3 — superseded]` before the next request (this is context hygiene, not behavior steering).
- One model call may return several `tool_use` blocks; execute in order, stop at the first failure, return all results in one user message.
- Tools exposed to the model (names are final): `look_at_desktop`, `look_at_window(hwnd)`, `take_screenshot(hwnd?)`, `system_facts`, `launch_app(query)`, `focus_window(hwnd)`, `click(x,y,button,clicks)`, `type_text(text,press_enter)`, `hotkey(keys[])`, `press(key,times)`, `scroll(x,y,dy,dx)`, `run_powershell(command)`, `open_url(url)`, `ask_user(question)`, `note_to_self(text)` (updates the running summary the model sees each turn), `done(message)`.
- Every turn the model automatically gets a fresh `look_at_desktop` result appended (cheap), never a tree or screenshot unless it asks.
- System prompt: short, behavioral. Describe who Yuki is, what it can see, how to prefer fast paths (shell/shortcuts > tree clicks > screenshot), when to ask the user (ambiguity, risk, or genuinely stuck — not as a first move), and to confirm before irreversible actions. Do not include keyword-based rule lists.

## Logging contract (module `yuki.log`)

JSONL file `logs/sessions/<YYYYMMDD-HHMMSS>.jsonl`, one JSON object per line: `{"ts": float, "session": str, "turn": int, "type": <event type>, ...}`. Event types: `user_message`, `llm_request` (model, system, messages, tools), `llm_response` (content blocks, stop_reason, usage, latency_ms), `tool_call`, `tool_result` (full result, elapsed_ms), `perception` (kind, size_chars, elapsed_ms, payload), `ask_user`, `user_answer`, `context_edit` (what was stubbed), `error` (with traceback). Base64 screenshot payloads are written to `logs/sessions/<id>/shot-<n>.png` and referenced by path, not inlined. Console gets a coloured, one-line-per-event pretty view.
