"""v3.15 actions: suggestion lists accepted by key, rich-text fields read from their parts, cheaper views.

Live 2026-09-30 21:30 (a Discord mention): the pasted "@name" opened no suggestion list, a message row was clicked
as if it were the suggestion, an element-targeted type_text whose read-back had arrived was taken for "could not be
read back" and escalated, an empty rich-text box ('\\ufeff\\n') was "not emptied", and every step re-sent the whole
~21.8k-character window. Offline only: stub trees and patched input internals, nothing touches the desktop.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import yuki.actions.input as inp
from yuki.agent.loop import action_verdict
from yuki.agent.tools import Dispatcher, ToolOutcome, carry_ids
from yuki.perception.tree import FieldState


def test_an_element_targeted_type_text_is_verified_by_its_own_read_back() -> None:
    typed = {"ok": True, "summary": "typed", "details": {"readback": "arrived", "readback_text": "@Hemanth hi"}}
    outcome = ToolOutcome(name="type_text", ok=True, summary="typed", content=[],
                          payload={"focus_click": {"ok": True, "details": {}}, "type_text": typed})
    assert action_verdict("type_text", outcome)[0] is True


def test_an_empty_rich_text_box_reads_blank_and_its_padding_is_not_text() -> None:
    assert inp._blank("﻿\n") and not inp._blank("﻿hi")
    assert inp._squash("﻿@Hemanth x ﻿ when") == "@hemanthxwhen"


def test_a_field_without_a_readable_value_is_checked_by_its_parts(monkeypatch: Any) -> None:
    unreadable = SimpleNamespace(ok=True, readable=False, reason="it exposes neither a Value nor a Text pattern")
    monkeypatch.setattr(inp, "_read_text", lambda _n: unreadable)
    monkeypatch.setattr(inp, "_read_field", lambda: FieldState(ok=True, parts_text="@Hemanth when will you join"))
    verdict, _phrase, facts = inp._read_back("when will you join", None)
    assert verdict is True and facts["readback_source"] == "parts"


def test_a_suggestion_is_accepted_by_key_and_checked_by_the_field(monkeypatch: Any) -> None:
    pressed: list[str] = []
    after = FieldState(ok=True, value="@Hemanth ", parts=3, parts_text="@Hemanth")
    monkeypatch.setattr(inp, "_send_presses", lambda key, times=1: pressed.append(key))
    monkeypatch.setattr(inp, "_foreground_mismatch", lambda _h: None)
    monkeypatch.setattr(inp, "_read_field", lambda: after)
    before = FieldState(ok=True, value="@Hem", parts=1, parts_text="@Hem", popup=True, popup_how="controls",
                        items=("Hemanth hemanth_q", "Hema Tran hemat"), selected=0)
    details: dict[str, Any] = {}
    phrase, state = inp._accept_suggestion(before, 8194, details)
    assert pressed == ["enter"] and details["suggestion_verified"] is True
    assert details["suggestion_accepted"] == "Hemanth hemanth_q" and state is after and "Hemanth" in phrase


def test_enter_is_withheld_while_a_selected_suggestion_would_take_it(monkeypatch: Any) -> None:
    pressed: list[str] = []
    popup = FieldState(ok=True, value="hi @Hem", popup=True, popup_how="controls", items=("Hemanth",), selected=0)
    monkeypatch.setattr(inp, "_send_presses", lambda key, times=1: pressed.append(key))
    for name, fn in {"_foreground_mismatch": lambda _h: None,
                     "_refuse_unless_ready": lambda *a, **k: None,
                     "_windows_before": lambda _h: (0, {}),
                     "_windows_appeared": lambda *a, **k: [],
                     "_focus_state": lambda: {"focus": None},
                     "_input_target": lambda _s: ("Edit \"Message\"", {}),
                     "_send_text": lambda text, _h: (len(text) * 2, len(text) * 2, None),
                     "_read_back": lambda text, _t: (True, f"field now contains {text!r}", {"readback": "arrived"}),
                     "_where_focus_is": lambda: ("", {}),
                     "_read_field": lambda: popup}.items():
        monkeypatch.setattr(inp, name, fn)
    result = inp.type_text("hi @Hem", press_enter=True)
    assert result.ok and pressed == [] and result.details["enter_withheld"] == "suggestion_selected"
    assert result.details["method"] == "keystrokes" and result.details["suggestions"]["items"] == ["Hemanth"]


class _Desk:
    """A window of dict elements; ``popup`` adds a list after the box."""

    def __init__(self) -> None:
        self.popup = False

    def get_window_tree(self, hwnd: int, **_: Any) -> dict[str, Any]:
        els = [{"id": 0, "role": "Window", "name": "Chat", "parent": -1, "center": (500, 500)}]
        for i in range(40):
            els.append({"id": len(els), "role": "Hyperlink", "name": f"channel {i}", "parent": 0,
                        "center": (50, 40 * i), "is_interactive": True})
        if self.popup:
            els.append({"id": len(els), "role": "ListItem", "name": "Hemanth", "parent": 0, "center": (400, 900),
                        "states": ["selected"]})
        els.append({"id": len(els), "role": "Edit", "name": "Message", "parent": 0, "center": (400, 1000),
                    "value": "@Hem" if self.popup else "", "is_focused": True})
        return {"hwnd": hwnd, "title": "Chat", "process_name": "chat.exe", "elements": els, "status": "ok",
                "truncated": False, "note": ""}

    def format_window_tree(self, tree: dict[str, Any]) -> str:
        return "header\n" + "\n".join(f"[{e['id']}] {e['role']} \"{e['name']}\"" for e in tree["elements"])

    def get_desktop_overview(self) -> dict[str, Any]:
        return {"foreground_hwnd": 7, "windows": []}


def test_the_view_after_an_action_lists_what_changed_and_keeps_the_ids() -> None:
    desk = _Desk()
    disp = Dispatcher(backend=desk)
    first = disp.window_view(7, settle=False)
    box_id = next(int(line[1:line.index("]")]) for line in first.text.splitlines() if "Edit" in line)
    desk.popup = True
    view = disp.window_view(7, settle=False, changes=True, near=box_id)
    assert view.changes and len(view.text) < view.full_chars / 2
    assert "\"Hemanth\"" in view.text and f"[{box_id}] Edit" in view.text and "channel 3" not in view.text
    # an id from the first read still points at the same element, and so does naming that read
    assert disp._element_target({"element": 5, "read": 1})[3].startswith('[5] Hyperlink "channel 4"')


def test_ids_are_carried_by_structure_and_new_ones_never_reuse_an_old_id() -> None:
    before = {0: {"role": "Window", "name": "W", "parent": -1}, 1: {"role": "Button", "name": "Send", "parent": 0},
              2: {"role": "Text", "name": "gone", "parent": 0}}
    new = [{"id": 0, "role": "Window", "name": "W", "parent": -1}, {"id": 1, "role": "Text", "name": "new", "parent": 0},
           {"id": 2, "role": "Button", "name": "Send", "parent": 0}]
    mapping, top = carry_ids(before, new, ceiling=2)
    assert mapping == {0: 0, 2: 1, 1: 3} and top == 3
