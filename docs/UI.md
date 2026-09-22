# Yuki's desktop shell (`yuki-ui`)

- **Alt+Space** (`YUKI_HOTKEY`) toggles the overlay: dark glass, upper third of the primary screen. Enter submits, Shift+Enter adds a line, Esc or a click elsewhere hides it. Replies stack as cards, three at a time.
- **Ctrl+Alt+Space** (`YUKI_CANCEL_HOTKEY`) cancels whatever the task lane is doing, via `Agent.cancel()`.
- Tray icon: Show, model switch (sonnet/opus, both lanes), open the logs folder, Quit. One instance per session, held by a named mutex.
- **Task mode:** the first tool call of a task hides the overlay and puts a 360x56 status strip bottom-right — spinner plus one line built from the tool's own name and its most telling argument ("Launching app — spotify"). The strip never takes focus.
- **Questions:** `ask_user` dismisses the strip, brings the overlay back with the question as a card and a fresh input; the answer goes to `Agent.answer()`.
- **Endings:** the closing message lands wherever you are looking — as a card if the overlay is open, otherwise in the strip for four seconds. Errors are tinted red.
- **Two lanes** (`yuki/ui/runtime.py`), one `Agent` each, one `QThread` each, Qt signals only across the boundary:
  - `worker` — the full tool set, a FIFO queue of tasks, and sole ownership of the mouse and keyboard.
  - `front_desk` — used only while the worker is busy, built with `tool_names=LOOK_ONLY_TOOLS` and its own `extra_instructions`, on a backend whose action functions are stubbed out. It can look, read and answer, never touch.
- If the front desk reaches for a tool it does not have, that request needed hands: the runtime queues the original text for the worker and says so in a card. The signal is the refused tool call, never the wording of the reply.
- Every UI-side event (hotkey, overlay shown/hidden, submit, lane chosen, hand-off, queued, answer, cancel, model switch) is written through the existing `SessionLogger` as `type: "ui"` records in `logs/sessions/<id>.jsonl`; the two lanes keep their own `<id>-worker.jsonl` and `<id>-frontdesk.jsonl` transcripts.
