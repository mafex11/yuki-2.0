"""The thinker's slider tool schema (report 47, N2), apart from :mod:`yuki.actions.slider` so importing the agent's
tool registry does not import the desktop hands."""

from __future__ import annotations

from typing import Any

SLIDER_TOOL: dict[str, Any] = {
    "name": "set_slider",
    "label": "Moving a slider",
    "description": (
        "Set a slider (an app's own volume, a seek bar, zoom) without the mouse: by its UI Automation value, else "
        "its own arrow keys, read back. element: the slider, or the bar or label holding it (\"Change volume\"). "
        "Never drag, click along a bar or script the mouse for one; when it finds none, use the app's keys a "
        "playbook names, or ask. Not the Windows mixer level (app_volume)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "element": {"type": "integer"},
            "read": {"type": "integer"},
            "expect_hwnd": {"type": "integer"},
            "to": {"type": "number", "description": "0-100 percent of its range."},
            "by": {"type": "number", "description": "Or percent points to move (negative: down)."},
        },
        "required": ["element"],
        "additionalProperties": False,
    },
}

__all__ = ["SLIDER_TOOL"]
