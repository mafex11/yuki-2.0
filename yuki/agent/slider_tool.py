"""The thinker's slider tool schema (report 47, N2), apart from :mod:`yuki.actions.slider` so importing the agent's
tool registry does not import the desktop hands."""

from __future__ import annotations

from typing import Any

SLIDER_TOOL: dict[str, Any] = {
    "name": "set_slider",
    "label": "Moving a slider",
    "description": (
        "Set a slider (an app's own in-app volume, a seek bar) without the mouse, read back. element: the slider, "
        "or the bar or label holding it (\"Change volume\"). Never drag or click along a bar. An app's shortcut for "
        "a level goes here as keys, never a bare hotkey. If it cannot reach it, say why: the mixer (app_volume) is "
        "never a stand-in."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "element": {"type": "integer"},
            "read": {"type": "integer"},
            "expect_hwnd": {"type": "integer"},
            "to": {"type": "number", "description": "0-100 percent of its range."},
            "by": {"type": "number", "description": "Or percent points to move (negative: down)."},
            "keys": {"type": "array", "items": {"type": "string"},
                     "description": "The app's shortcut for it (a playbook's, or one to try): pressed, read back."},
        },
        "required": ["element"],
        "additionalProperties": False,
    },
}

__all__ = ["SLIDER_TOOL"]
