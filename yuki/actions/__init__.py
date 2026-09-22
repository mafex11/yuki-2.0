"""Yuki's hands.

Every action returns an :class:`ActionResult`: a one-line ``summary`` for the
model, a structured ``details`` dict for the log, and its own timing.  Actions
never raise for expected failures (an app that is not installed, a window that
closed, a script that exited non-zero) - they come back with ``ok=False`` and a
summary the model can act on.

No action sleeps on a timer: every wait polls a real condition (window exists,
foreground changed, process exited) against a deadline.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ActionResult:
    """Outcome of one action."""

    ok: bool
    summary: str
    details: dict = field(default_factory=dict)
    elapsed_ms: float = 0.0


from yuki.actions.input import (  # noqa: E402  (ActionResult must exist first)
    click,
    hotkey,
    normalize_key,
    press,
    scroll,
    type_text,
)
from yuki.actions.launch import (  # noqa: E402
    focus_window,
    get_start_apps,
    launch_app,
    open_url,
)
from yuki.actions.shell import run_powershell  # noqa: E402

__all__ = [
    "ActionResult",
    "click",
    "focus_window",
    "get_start_apps",
    "hotkey",
    "launch_app",
    "normalize_key",
    "open_url",
    "press",
    "run_powershell",
    "scroll",
    "type_text",
]
