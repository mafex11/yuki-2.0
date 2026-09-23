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
7. **Builders never touch the live desktop.** No sub-agent or script written by one may launch apps, send input, take screenshots, run smoke tests, benchmarks or end-to-end agent runs. Verify by import, compile and stub-driven offline checks only. The user does live testing. (Two terminal-closing incidents on 2026-09-22.)
8. **Windows-only, Python 3.13, `uv`.** Project root `C:\Users\esska\yuki`. Package `yuki/`. Run with `uv run ...`.

## Model access

- Client: `anthropic.AnthropicBedrock(aws_region=os.environ["AWS_REGION"])`. Auth comes from the `AWS_BEARER_TOKEN_BEDROCK` user env var automatically. Do not put keys in files.
- Model ids (config, default first): `us.anthropic.claude-sonnet-5`, `us.anthropic.claude-opus-5`.
- Use `thinking={"type": "adaptive"}`; do not send `temperature` or `budget_tokens`. Parse tool inputs as JSON objects (already parsed by SDK).
- Verified working on 2026-09-22 (~2.5–3s round trip).

## Package layout

```
yuki/
  config.py          # dataclass Settings: model ids, log dir, screenshot policy (never|ask|auto, default never (test phase))
  log/
    events.py        # event schema (TypedDicts/dataclasses) + SessionLogger (JSONL + pretty console)
  perception/        # "eyes"
    windows.py       # desktop overview
    tree.py          # UIA tree for one window
    screenshot.py    # screenshot of a window, region, monitor or the desktop; native resolution, origin + scale reported
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
tests/               # (none) — the user tests live; builders verify offline only
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
    bounds: tuple[int, int, int, int]   # left, top, right, bottom (virtual-desktop px)

@dataclass
class MonitorInfo:
    index: int                     # EnumDisplayMonitors order
    name: str                      # e.g. "\\.\DISPLAY1"
    bounds: tuple[int, int, int, int]      # virtual-desktop px
    work_area: tuple[int, int, int, int]   # bounds minus taskbar/appbars
    is_primary: bool
    dpi_scale: float               # 1.0 at 100%, 1.5 at 150% (informational: coordinates are physical px)
    dpi: int                       # effective DPI

@dataclass
class DesktopOverview:
    windows: list[WindowInfo]     # all top-level, visible, non-tool windows (Explorer shell windows excluded, taskbar excluded)
    foreground_hwnd: int | None
    cursor: tuple[int, int]
    screen_size: tuple[int, int]   # size of the whole virtual desktop (all monitors), not the primary display
    captured_at: float             # time.time()
    monitors: list[MonitorInfo]
    virtual_bounds: tuple[int, int, int, int]  # left, top, right, bottom of the virtual desktop (may be negative)

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
    patterns: tuple[str, ...]      # interactive elements only: subset of invoke, toggle, select (SelectionItem), expand (ExpandCollapse), value (writable Value)

@dataclass
class WindowTree:
    hwnd: int
    title: str
    process_name: str
    elements: list[UIElement]      # depth-first order, capped (see below)
    truncated: bool                # True if cap or timeout hit
    captured_at: float
    elapsed_ms: float
    passes: int                    # walks made (>1 when the first pass was thin and re-polled)
    child_windows: int             # descendant HWNDs walked alongside the top-level one
    first_pass_elements: int       # element count of the first walk (-1 unknown)
    status: str                    # "ok" UIA answered (however small) | "busy" on screen, process alive, no answer within ~1.5 s (returned then, elements=[], truncated) | "empty" answered with nothing usable, or not on screen and silent
    note: str                      # one sentence when status is not ok or the tree is partial, e.g. "window did not answer accessibility queries within 1.5 s (probably loading or rendering); look again shortly"

def get_window_tree(hwnd: int, *, max_elements: int = 400, timeout_s: float = 3.0) -> WindowTree: ...
def format_window_tree(tree: WindowTree) -> str: ...   # compact text for the model, one line per element:  [id] role "name" value=... @(x,y) {invoke,expand} [kb: shortcut]; header carries [status: busy|empty] and a note: line when set
def format_overview(o: DesktopOverview) -> str: ...

@dataclass
class Capture:
    png: bytes
    origin: tuple[int, int]        # top-left of the captured rectangle, virtual-desktop px
    source_size: tuple[int, int]   # screen px captured
    image_size: tuple[int, int]    # PNG size (== source_size unless scaled)
    scale: float                   # image px per screen px; 1.0 = 1:1.  screen = origin + image / scale
    target: str                    # "window <hwnd>", "monitor <i>", "region", "desktop"
    rendered_by_window: bool       # PrintWindow (True) vs desktop crop (False)

def screenshot(hwnd: int | None = None, *, region: tuple[int, int, int, int] | None = None,
               monitor: int | None = None, max_width: int = 2560) -> Capture: ...
    # at most one target; none = whole virtual desktop. Native resolution unless the longest edge exceeds
    # max_width, in which case it is scaled down and the exact scale reported. `capture` is the same function.
def system_facts() -> dict: ...                # time, uptime, cpu %, memory total/used, top processes by memory with pid/name/rss
```

Notes for the tree: walk via UIA cached subtrees from the window's element, depth-first, skipping offscreen elements, include every element that has a name/value or is interactive/scrollable. Child HWNDs are walked too: `EnumChildWindows` lists the visible descendants, each gets its own `ElementFromHandle` (its own WM_GETOBJECT, which is what makes Chromium/Electron/CEF build their lazily built tree), and its subtree is spliced in after the element that owns it, de-duplicated by RuntimeId (else role+name+bounds); ids are positions within the returned pass. If the first pass is thin, the walk is re-polled with a deadline until the count has grown and stopped growing, or has not grown within a short grace (longer for a process launched moments ago) — never a fixed sleep. No special-casing by app or class name; report `truncated`/element counts/`passes` honestly. Walk in a worker thread with COM initialised (`comtypes.CoInitializeEx` / `pythoncom`) and enforce `timeout_s`.

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
def launch_app(query: str, *, args: list[str] | None = None, timeout_s: float = 8.0) -> ActionResult
    # Resolve via Get-StartApps (AppsFolder). Return candidates when ambiguous instead of guessing:
    # details = {"launched": bool, "hwnd": int|None, "candidates": [{"name","appid"}]}.
    # Waits until a new window from the launched process appears (or an existing window of it changes title), or timeout.
    # With args: shell:AppsFolder cannot pass arguments, so a packaged app (AUMID "Family!App") is activated with its
    # arguments through IApplicationActivationManager (keeps package identity, so the app's own profile/data), and a
    # desktop app is started with Start-Process -FilePath <exe> -ArgumentList <args>, the exe coming from its Start-menu
    # .lnk target, a known-folder/absolute AppID path, or the image of a running window with that AppUserModelID.
    # No way to pass arguments -> ok=False, nothing started, reason in the summary.
def focus_window(hwnd: int, *, timeout_s: float = 2.0) -> ActionResult   # restore if minimized, SetForegroundWindow, verify foreground
def click(x: int, y: int, *, button: str = "left", clicks: int = 1) -> ActionResult   # instant move, no glide
    # summary + details report the outcome: foreground window after the click (foreground_hwnd/title/process,
    # foreground_changed), focused_control_hwnd, focus_role/focus_name/focus_value/focus_accepts_text, focus_changed.
def type_text(text: str, *, press_enter: bool = False, clear: bool = False) -> ActionResult  # types into current focus
    # details carry foreground_hwnd, focused_control_hwnd, focus_role/name/accepts_text; failures name the focus in the summary.
    # Path: paste for non-ASCII or >50 chars, and for >=12 chars when the focused control is a writable Value field or the window
    #   took >=100 ms to become ready; keystrokes otherwise. details["method"]/["method_reason"] say which and why.
    # clear: select-all + delete first, refused if the focused control does not take text, verified empty when it has a Value pattern.
    # Read-back: after sending (before Enter) the focused control is read via UIA (ValuePattern, else TextPattern before the caret);
    #   summary "field now contains '...'"; if the typed text is not there: ok=False, Enter NOT pressed, summary says what it holds.
    #   Unreadable -> "field contents not readable (...)" and it proceeds. Condition poll: ends when the text shows or the
    #   contents stop changing for 0.35 s, cap 2 s.
    # type_text/hotkey/press: details["new_windows"] lists top-level windows of the target process that showed during the action
    #   (before/after snapshot); the summary names them with class and bounds.
def hotkey(*keys: str) -> ActionResult                                  # e.g. ("ctrl","t"), ("win","r"), ("volume_mute",)
def press(key: str, *, times: int = 1) -> ActionResult
    # hotkey/press: with expect_hwnd, wait_for_input_ready first (~1 s, fail naming the focus); after sending, watch focus
    # for up to ~400 ms (condition poll) and report it like click: "pressed ctrl+l; focus now Edit '...' (takes text)".
    # type_text records the focused control before the first character and names it on success: "typed N chars ... into <control>".
def scroll(x: int, y: int, *, dy: int = 0, dx: int = 0) -> ActionResult  # wheel notches at a point
def run_powershell(command: str, *, timeout_s: float = 20.0) -> ActionResult
    # Pass the script via -EncodedCommand (UTF-16LE base64) or stdin; NEVER split on whitespace. Capture stdout/stderr/exit code (UTF-8).
def open_url(url: str, *, app: str | None = None) -> ActionResult     # default handler via os.startfile / Start-Process; with app: launch_app(app, args=[url])
```

## Agent contract (module `yuki.agent`)

- `Agent.run(request: str) -> Iterator[AgentEvent]`. Events: `thinking(text)`, `tool_call(name, input)`, `tool_result(name, ok, summary)`, `ask_user(question)`, `final(text)`, `error(text)`. When `ask_user` is yielded the generator pauses; the caller sends the answer with `agent.answer(text)` and continues iterating. State (messages, running summary) survives the pause.
- Messages are append-only. Tool results from perception that are older than the last 2 turns are replaced in-place with a one-line stub like `[desktop overview from step 3 — superseded]` before the next request (this is context hygiene, not behavior steering).
- One model call may return several `tool_use` blocks; execute in order, stop at the first failure, return all results in one user message.
- Tools exposed to the model (names are final): `look_at_desktop`, `look_at_window(hwnd)`, `take_screenshot(hwnd?, region?, monitor?)` (omitted from the tool list entirely when `screenshot_policy` is `never`; the dispatcher still refuses it as a backstop), `system_facts`, `launch_app(query, args?)`, `focus_window(hwnd)`, `click(x,y,button,clicks)`, `type_text(text,press_enter,clear?)`, `hotkey(keys[])`, `press(key,times)`, `scroll(x,y,dy,dx)`, `run_powershell(command)`, `open_url(url, app?)`, `ask_user(question)`, `note_to_self(text)` (updates the running summary the model sees each turn), `done(message)`.
- Every turn the model automatically gets a fresh `look_at_desktop` result appended (cheap), never a tree or screenshot unless it asks.
- System prompt: short, behavioral. Describe who Yuki is, what it can see, how to prefer fast paths (shell/shortcuts > tree clicks; the prompt does not mention screenshots), when to ask the user (ambiguity, risk, or genuinely stuck — not as a first move), and to confirm before irreversible actions. Do not include keyword-based rule lists.

## Logging contract (module `yuki.log`)

JSONL file `logs/sessions/<YYYYMMDD-HHMMSS>.jsonl`, one JSON object per line: `{"ts": float, "session": str, "turn": int, "type": <event type>, ...}`. Event types: `user_message`, `llm_request` (model, system, messages, tools), `llm_response` (content blocks, stop_reason, usage, latency_ms), `tool_call`, `tool_result` (full result, elapsed_ms), `perception` (kind, size_chars, elapsed_ms, payload), `ask_user`, `user_answer`, `context_edit` (what was stubbed), `error` (with traceback). Base64 screenshot payloads are written to `logs/sessions/<id>/shot-<n>.png` and referenced by path, not inlined. Console gets a coloured, one-line-per-event pretty view.
